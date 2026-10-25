from __future__ import annotations

import math
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from storage_types import (
    ActiveView,
    Household,
    InventoryItem,
    Store,
    User,
    format_quantity,
    normalize_name,
)


def _validate_amount(value: float, field: str, *, signed: bool = False) -> None:
    if not math.isfinite(value) or (not signed and value < 0):
        qualifier = "finite" if signed else "finite and nonnegative"
        raise ValueError(f"{field} must be {qualifier}")


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                # sqlite3's implicit transactions start at DML, not CREATE or
                # ALTER. Include every schema change in the same rollback unit.
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        current_household_id INTEGER,
                        current_store_id INTEGER,
                        reminders_enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen TEXT
                    )
                    """
                )
                self._ensure_column(connection, "users", "current_store_id", "INTEGER")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS households (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        invite_code TEXT NOT NULL UNIQUE,
                        owner_chat_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS household_members (
                        household_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        role TEXT NOT NULL DEFAULT 'member',
                        joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (household_id, chat_id),
                        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
                        FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stores (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        household_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (household_id, normalized_name),
                        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
                    )
                    """
                )
                self._migrate_inventory_items(connection)
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS item_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        delta REAL,
                        note TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (item_id) REFERENCES inventory_items(id) ON DELETE CASCADE
                    )
                    """
                )
                self._migrate_item_events_fk(connection)
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_views (
                        chat_id INTEGER PRIMARY KEY,
                        message_id INTEGER NOT NULL,
                        view_type TEXT NOT NULL,
                        household_id INTEGER,
                        store_id INTEGER,
                        item_id INTEGER,
                        page INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                self._ensure_default_stores(connection)
                self._migrate_initial_extra_stores(connection)

    def record_user(self, chat_id: int, username: str | None, first_name: str | None) -> User:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO users (chat_id, username, first_name, last_seen)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        username = excluded.username,
                        first_name = excluded.first_name,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (chat_id, username, first_name),
                )
                return self._get_user(connection, chat_id)

    def ensure_default_household(self, chat_id: int, first_name: str | None = None) -> Household:
        current = self.current_household(chat_id)
        if current:
            return current
        name = f"Дом {first_name}" if first_name else "Мой дом"
        return self.create_household(chat_id, name)

    def create_household(self, chat_id: int, name: str) -> Household:
        clean_name = " ".join(name.split()) or "Мой дом"
        with closing(self._connect()) as connection:
            with connection:
                invite_code = self._new_invite_code(connection)
                cursor = connection.execute(
                    """
                    INSERT INTO households (name, invite_code, owner_chat_id)
                    VALUES (?, ?, ?)
                    """,
                    (clean_name, invite_code, chat_id),
                )
                household_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO household_members (household_id, chat_id, role)
                    VALUES (?, ?, 'owner')
                    """,
                    (household_id, chat_id),
                )
                store = self._create_initial_stores(connection, household_id)
                connection.execute(
                    """
                    UPDATE users
                    SET current_household_id = ?, current_store_id = ?
                    WHERE chat_id = ?
                    """,
                    (household_id, store.id, chat_id),
                )
                return self._get_household(connection, household_id)

    def join_household(self, chat_id: int, invite_code: str) -> Household | None:
        code = invite_code.strip().upper()
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute("SELECT id FROM households WHERE invite_code = ?", (code,)).fetchone()
                if row is None:
                    return None
                household_id = int(row["id"])
                connection.execute(
                    """
                    INSERT INTO household_members (household_id, chat_id, role)
                    VALUES (?, ?, 'member')
                    ON CONFLICT(household_id, chat_id) DO NOTHING
                    """,
                    (household_id, chat_id),
                )
                store = self._first_store(connection, household_id)
                connection.execute(
                    """
                    UPDATE users
                    SET current_household_id = ?, current_store_id = ?
                    WHERE chat_id = ?
                    """,
                    (household_id, store.id if store else None, chat_id),
                )
                return self._get_household(connection, household_id)

    def get_user(self, chat_id: int) -> User | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
            return self._row_to_user(row) if row else None

    def current_household(self, chat_id: int) -> Household | None:
        with closing(self._connect()) as connection:
            user = self._get_user(connection, chat_id)
            if user.current_household_id is not None and self._is_member(connection, user.current_household_id, chat_id):
                return self._get_household(connection, user.current_household_id)
            row = connection.execute(
                """
                SELECT h.id
                FROM households h
                JOIN household_members m ON m.household_id = h.id
                WHERE m.chat_id = ?
                ORDER BY m.joined_at, h.id
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "UPDATE users SET current_household_id = NULL, current_store_id = NULL WHERE chat_id = ?",
                    (chat_id,),
                )
                connection.commit()
                return None
            household_id = int(row["id"])
            store = self._first_store(connection, household_id)
            connection.execute(
                """
                UPDATE users
                SET current_household_id = ?, current_store_id = ?
                WHERE chat_id = ?
                """,
                (household_id, store.id if store else None, chat_id),
            )
            connection.commit()
            return self._get_household(connection, household_id)

    def set_current_household(self, chat_id: int, household_id: int) -> Household | None:
        with closing(self._connect()) as connection:
            with connection:
                if not self._is_member(connection, household_id, chat_id):
                    return None
                store = self._first_store(connection, household_id)
                connection.execute(
                    """
                    UPDATE users
                    SET current_household_id = ?, current_store_id = ?
                    WHERE chat_id = ?
                    """,
                    (household_id, store.id if store else None, chat_id),
                )
                return self._get_household(connection, household_id)

    def rename_household(self, chat_id: int, household_id: int, name: str) -> Household | None:
        clean_name = " ".join(name.split())
        if not clean_name:
            return None
        with closing(self._connect()) as connection:
            with connection:
                if not self._is_member(connection, household_id, chat_id):
                    return None
                connection.execute("UPDATE households SET name = ? WHERE id = ?", (clean_name, household_id))
                return self._get_household(connection, household_id)

    def remove_household_for_user(self, chat_id: int, household_id: int) -> str | None:
        with closing(self._connect()) as connection:
            with connection:
                member = connection.execute(
                    "SELECT role FROM household_members WHERE household_id = ? AND chat_id = ?",
                    (household_id, chat_id),
                ).fetchone()
                if member is None:
                    return None
                if member["role"] == "owner":
                    connection.execute("DELETE FROM active_views WHERE household_id = ?", (household_id,))
                    connection.execute("DELETE FROM households WHERE id = ?", (household_id,))
                    connection.execute(
                        """
                        UPDATE users
                        SET current_household_id = NULL, current_store_id = NULL
                        WHERE current_household_id = ?
                        """,
                        (household_id,),
                    )
                    action = "deleted"
                else:
                    connection.execute(
                        "DELETE FROM active_views WHERE household_id = ? AND chat_id = ?",
                        (household_id, chat_id),
                    )
                    connection.execute(
                        "DELETE FROM household_members WHERE household_id = ? AND chat_id = ?",
                        (household_id, chat_id),
                    )
                    action = "left"

                replacement = connection.execute(
                    """
                    SELECT h.id
                    FROM households h
                    JOIN household_members m ON m.household_id = h.id
                    WHERE m.chat_id = ?
                    ORDER BY m.joined_at, h.id
                    LIMIT 1
                    """,
                    (chat_id,),
                ).fetchone()
                replacement_id = replacement["id"] if replacement else None
                store = self._first_store(connection, replacement_id) if replacement_id else None
                connection.execute(
                    """
                    UPDATE users
                    SET current_household_id = ?, current_store_id = ?
                    WHERE chat_id = ?
                    """,
                    (replacement_id, store.id if store else None, chat_id),
                )
                return action

    def list_households(self, chat_id: int) -> list[Household]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT h.*
                FROM households h
                JOIN household_members m ON m.household_id = h.id
                WHERE m.chat_id = ?
                ORDER BY m.joined_at, h.id
                """,
                (chat_id,),
            ).fetchall()
            return [self._row_to_household(row) for row in rows]

    def list_members(self, household_id: int) -> list[User]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT u.*
                FROM users u
                JOIN household_members m ON m.chat_id = u.chat_id
                WHERE m.household_id = ?
                ORDER BY m.role DESC, u.first_name, u.username, u.chat_id
                """,
                (household_id,),
            ).fetchall()
            return [self._row_to_user(row) for row in rows]

    def household_role(self, household_id: int, chat_id: int) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT role FROM household_members WHERE household_id = ? AND chat_id = ?",
                (household_id, chat_id),
            ).fetchone()
            return str(row["role"]) if row else None

    def is_household_member(self, household_id: int, chat_id: int) -> bool:
        with closing(self._connect()) as connection:
            return self._is_member(connection, household_id, chat_id)

    def list_stores(self, household_id: int) -> list[Store]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM stores WHERE household_id = ? ORDER BY created_at, id",
                (household_id,),
            ).fetchall()
            return [self._row_to_store(row) for row in rows]

    def current_store(self, chat_id: int) -> Store | None:
        with closing(self._connect()) as connection:
            user = self._get_user(connection, chat_id)
            household = self.current_household(chat_id)
            if household is None:
                return None
            if user.current_store_id is not None:
                store = self._get_store(connection, user.current_store_id)
                if store and store.household_id == household.id:
                    return store
            store = self._first_store(connection, household.id)
            connection.execute(
                "UPDATE users SET current_store_id = ? WHERE chat_id = ?",
                (store.id if store else None, chat_id),
            )
            connection.commit()
            return store

    def create_store(
        self,
        chat_id: int,
        household_id: int,
        name: str,
    ) -> Store | None:
        with closing(self._connect()) as connection:
            with connection:
                if not self._is_member(connection, household_id, chat_id):
                    return None
                store = self._create_store(connection, household_id, name)
                connection.execute(
                    "UPDATE users SET current_household_id = ?, current_store_id = ? WHERE chat_id = ?",
                    (household_id, store.id, chat_id),
                )
                return store

    def set_current_store(self, chat_id: int, store_id: int) -> Store | None:
        with closing(self._connect()) as connection:
            with connection:
                store = self._get_store(connection, store_id)
                if store is None or not self._is_member(connection, store.household_id, chat_id):
                    return None
                connection.execute(
                    """
                    UPDATE users
                    SET current_household_id = ?, current_store_id = ?
                    WHERE chat_id = ?
                    """,
                    (store.household_id, store.id, chat_id),
                )
                return store

    def rename_store(self, chat_id: int, store_id: int, name: str) -> Store | None:
        clean_name = " ".join(name.split())
        if not clean_name:
            return None
        with closing(self._connect()) as connection:
            with connection:
                store = self._get_store(connection, store_id)
                if store is None or not self._is_member(connection, store.household_id, chat_id):
                    return None
                connection.execute(
                    """
                    UPDATE stores
                    SET name = ?, normalized_name = ?
                    WHERE id = ?
                    """,
                    (clean_name, normalize_name(clean_name), store_id),
                )
                return self._get_store(connection, store_id)

    def remove_store(self, chat_id: int, store_id: int) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                store = self._get_store(connection, store_id)
                if store is None or not self._is_member(connection, store.household_id, chat_id):
                    return False
                connection.execute("DELETE FROM stores WHERE id = ?", (store_id,))
                replacement = self._first_store(connection, store.household_id)
                connection.execute(
                    """
                    UPDATE users
                    SET current_store_id = ?
                    WHERE current_household_id = ? AND current_store_id = ?
                    """,
                    (replacement.id if replacement else None, store.household_id, store_id),
                )
                return True

    def add_or_update_item(
        self,
        store_id: int,
        chat_id: int,
        name: str,
        quantity: float,
        unit: str,
        min_quantity: float | None = None,
    ) -> InventoryItem:
        _validate_amount(quantity, "quantity")
        if min_quantity is not None:
            _validate_amount(min_quantity, "min_quantity")
        normalized_name = normalize_name(name)
        clean_name = " ".join(name.split())
        clean_unit = unit.strip() or "шт"
        minimum = 1.0 if min_quantity is None else min_quantity
        with closing(self._connect()) as connection:
            with connection:
                store = self._get_store(connection, store_id)
                if store is None or not self._is_member(connection, store.household_id, chat_id):
                    raise KeyError(f"Store {store_id} does not exist")
                connection.execute(
                    """
                    INSERT INTO inventory_items (
                        household_id, store_id, name, normalized_name, quantity, unit, min_quantity, active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(store_id, normalized_name) DO UPDATE SET
                        name = excluded.name,
                        quantity = CASE
                            WHEN inventory_items.active = 0 THEN excluded.quantity
                            ELSE inventory_items.quantity + excluded.quantity
                        END,
                        unit = excluded.unit,
                        min_quantity = CASE
                            WHEN ? IS NULL THEN inventory_items.min_quantity
                            ELSE excluded.min_quantity
                        END,
                        active = 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (store.household_id, store_id, clean_name, normalized_name, quantity, clean_unit, minimum, min_quantity),
                )
                item = self._get_item_by_name(connection, store_id, normalized_name)
                _validate_amount(item.quantity, "resulting quantity")
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action, delta) VALUES (?, ?, 'add', ?)",
                    (item.id, chat_id, quantity),
                )
                return item

    def set_item_quantity(self, store_id: int, chat_id: int, item_id: int, quantity: float) -> InventoryItem | None:
        _validate_amount(quantity, "quantity")
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return None
                delta = quantity - item.quantity
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET quantity = ?, active = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (quantity, item_id),
                )
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action, delta) VALUES (?, ?, 'set_quantity', ?)",
                    (item_id, chat_id, delta),
                )
                return self._get_item(connection, item_id)

    def change_item_quantity(self, store_id: int, chat_id: int, item_id: int, delta: float) -> InventoryItem | None:
        _validate_amount(delta, "delta", signed=True)
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return None
                row = connection.execute(
                    """
                    UPDATE inventory_items
                    SET quantity = MAX(0.0, quantity + ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND store_id = ?
                    RETURNING *
                    """,
                    (delta, item_id, store_id),
                ).fetchone()
                if row is None:
                    return None
                _validate_amount(float(row["quantity"]), "resulting quantity")
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action, delta) VALUES (?, ?, 'change_quantity', ?)",
                    (item_id, chat_id, delta),
                )
                return self._row_to_item(row)

    def set_item_minimum(
        self,
        store_id: int,
        item_id: int,
        min_quantity: float,
        chat_id: int | None = None,
    ) -> InventoryItem | None:
        _validate_amount(min_quantity, "min_quantity")
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or (chat_id is not None and not self._is_member(connection, item.household_id, chat_id))
                ):
                    return None
                connection.execute(
                    "UPDATE inventory_items SET min_quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (min_quantity, item_id),
                )
                return self._get_item(connection, item_id)

    def rename_item(self, store_id: int, chat_id: int, item_id: int, name: str) -> InventoryItem | None:
        clean_name = " ".join(name.split())
        if not clean_name:
            return None
        normalized_name = normalize_name(clean_name)
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return None
                try:
                    connection.execute(
                        """
                        UPDATE inventory_items
                        SET name = ?, normalized_name = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (clean_name, normalized_name, item_id),
                    )
                except sqlite3.IntegrityError:
                    return None
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action, note) VALUES (?, ?, 'rename', ?)",
                    (item_id, chat_id, f"{item.name} -> {clean_name}"),
                )
                return self._get_item(connection, item_id)

    def change_item_unit(
        self,
        store_id: int,
        chat_id: int,
        item_id: int,
        unit: str,
        quantity: float,
        min_quantity: float,
    ) -> InventoryItem | None:
        _validate_amount(quantity, "quantity")
        _validate_amount(min_quantity, "min_quantity")
        clean_unit = " ".join(unit.split())
        if not clean_unit:
            return None
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return None
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET unit = ?, quantity = ?, min_quantity = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND store_id = ?
                    """,
                    (clean_unit, quantity, min_quantity, item_id, store_id),
                )
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action, note) VALUES (?, ?, 'change_unit', ?)",
                    (item_id, chat_id, f"{item.unit} -> {clean_unit}"),
                )
                return self._get_item(connection, item_id)

    def archive_item(self, store_id: int, chat_id: int, item_id: int) -> InventoryItem | None:
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or not item.active
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return None
                connection.execute(
                    "UPDATE inventory_items SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item_id,),
                )
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action) VALUES (?, ?, 'archive')",
                    (item_id, chat_id),
                )
                return self._get_item(connection, item_id)

    def restore_item(self, store_id: int, chat_id: int, item_id: int) -> InventoryItem | None:
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or item.active
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return None
                connection.execute(
                    "UPDATE inventory_items SET active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item_id,),
                )
                connection.execute(
                    "INSERT INTO item_events (item_id, chat_id, action) VALUES (?, ?, 'restore')",
                    (item_id, chat_id),
                )
                return self._get_item(connection, item_id)

    def delete_item(self, store_id: int, chat_id: int, item_id: int) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                item = self._get_item(connection, item_id)
                if (
                    item is None
                    or item.store_id != store_id
                    or not self._is_member(connection, item.household_id, chat_id)
                ):
                    return False
                cursor = connection.execute(
                    "DELETE FROM inventory_items WHERE id = ? AND store_id = ?",
                    (item_id, store_id),
                )
                return cursor.rowcount > 0

    def deactivate_item(self, store_id: int, item_id: int) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE inventory_items SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE store_id = ? AND id = ?",
                    (store_id, item_id),
                )
                return cursor.rowcount > 0

    def get_store(self, store_id: int) -> Store | None:
        with closing(self._connect()) as connection:
            return self._get_store(connection, store_id)

    def get_household(self, household_id: int) -> Household | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM households WHERE id = ?", (household_id,)).fetchone()
            return self._row_to_household(row) if row else None

    def get_item(self, item_id: int) -> InventoryItem | None:
        with closing(self._connect()) as connection:
            return self._get_item(connection, item_id)

    def list_items(self, store_id: int, include_inactive: bool = False) -> list[InventoryItem]:
        query = "SELECT * FROM inventory_items WHERE store_id = ?"
        params: list[object] = [store_id]
        if not include_inactive:
            query += " AND active = 1"
        query += " ORDER BY active DESC, normalized_name"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._row_to_item(row) for row in rows]

    def low_items(self, store_id: int) -> list[InventoryItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM inventory_items
                WHERE store_id = ? AND active = 1 AND quantity <= min_quantity
                ORDER BY normalized_name
                """,
                (store_id,),
            ).fetchall()
            return [self._row_to_item(row) for row in rows]

    def low_items_by_store(self, household_id: int) -> list[tuple[Store, list[InventoryItem]]]:
        result: list[tuple[Store, list[InventoryItem]]] = []
        with closing(self._connect()) as connection:
            store_rows = connection.execute(
                "SELECT * FROM stores WHERE household_id = ? ORDER BY created_at, id",
                (household_id,),
            ).fetchall()
            for store_row in store_rows:
                store = self._row_to_store(store_row)
                item_rows = connection.execute(
                    """
                    SELECT *
                    FROM inventory_items
                    WHERE store_id = ? AND active = 1 AND quantity <= min_quantity
                    ORDER BY normalized_name
                    """,
                    (store.id,),
                ).fetchall()
                items = [self._row_to_item(row) for row in item_rows]
                if items:
                    result.append((store, items))
        return result

    def find_item(self, store_id: int, query: str) -> InventoryItem | None:
        normalized = normalize_name(query)
        with closing(self._connect()) as connection:
            item = self._get_item_by_name_or_none(connection, store_id, normalized)
            if item is not None:
                return item
            rows = connection.execute(
                """
                SELECT *
                FROM inventory_items
                WHERE store_id = ? AND active = 1 AND normalized_name LIKE ?
                ORDER BY LENGTH(normalized_name), normalized_name
                LIMIT 1
                """,
                (store_id, f"%{normalized}%"),
            ).fetchall()
            return self._row_to_item(rows[0]) if rows else None

    def set_active_view(
        self,
        chat_id: int,
        message_id: int,
        view_type: str,
        *,
        household_id: int | None = None,
        store_id: int | None = None,
        item_id: int | None = None,
        page: int = 0,
    ) -> ActiveView:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO active_views (
                        chat_id, message_id, view_type, household_id, store_id, item_id, page, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        message_id = excluded.message_id,
                        view_type = excluded.view_type,
                        household_id = excluded.household_id,
                        store_id = excluded.store_id,
                        item_id = excluded.item_id,
                        page = excluded.page,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (chat_id, message_id, view_type, household_id, store_id, item_id, max(0, page)),
                )
                row = connection.execute("SELECT * FROM active_views WHERE chat_id = ?", (chat_id,)).fetchone()
                return self._row_to_active_view(row)

    def get_active_view(self, chat_id: int) -> ActiveView | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM active_views WHERE chat_id = ?", (chat_id,)).fetchone()
            return self._row_to_active_view(row) if row else None

    def active_views_for_household(self, household_id: int) -> list[ActiveView]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM active_views WHERE household_id = ? ORDER BY chat_id",
                (household_id,),
            ).fetchall()
            return [self._row_to_active_view(row) for row in rows]

    def clear_active_view(self, chat_id: int, message_id: int | None = None) -> None:
        with closing(self._connect()) as connection:
            with connection:
                if message_id is None:
                    connection.execute("DELETE FROM active_views WHERE chat_id = ?", (chat_id,))
                else:
                    connection.execute(
                        "DELETE FROM active_views WHERE chat_id = ? AND message_id = ?",
                        (chat_id, message_id),
                    )

    def _migrate_inventory_items(self, connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inventory_items'"
        ).fetchone()
        if exists is None:
            self._create_inventory_table(connection)
            return

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(inventory_items)").fetchall()}
        if "store_id" in columns:
            return

        connection.execute("ALTER TABLE inventory_items RENAME TO inventory_items_old")
        self._create_inventory_table(connection)
        household_rows = connection.execute("SELECT id FROM households ORDER BY id").fetchall()
        store_by_household: dict[int, int] = {}
        for row in household_rows:
            household_id = int(row["id"])
            store = self._first_store(connection, household_id) or self._create_store(connection, household_id, "Основной")
            store_by_household[household_id] = store.id
        old_rows = connection.execute("SELECT * FROM inventory_items_old ORDER BY id").fetchall()
        for row in old_rows:
            household_id = int(row["household_id"])
            store_id = store_by_household.get(household_id)
            if store_id is None:
                raise sqlite3.IntegrityError("Cannot migrate an item without its household")
            connection.execute(
                """
                INSERT INTO inventory_items (
                    id, household_id, store_id, name, normalized_name, quantity, unit, min_quantity,
                    active, notes, last_reminded_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    household_id,
                    store_id,
                    row["name"],
                    row["normalized_name"],
                    row["quantity"],
                    row["unit"],
                    row["min_quantity"],
                    row["active"],
                    row["notes"],
                    row["last_reminded_at"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        # RENAME retargets item_events' foreign key to inventory_items_old.
        # Rebind its rows to the new inventory before DROP can cascade them.
        self._migrate_item_events_fk(connection)
        connection.execute("DROP TABLE inventory_items_old")

    def _migrate_item_events_fk(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'item_events'"
        ).fetchone()
        if row is None:
            return
        sql = row["sql"] or ""
        if "inventory_items_old" not in sql:
            return
        connection.execute("ALTER TABLE item_events RENAME TO item_events_old")
        connection.execute(
            """
            CREATE TABLE item_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                delta REAL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES inventory_items(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO item_events (id, item_id, chat_id, action, delta, note, created_at)
            SELECT id, item_id, chat_id, action, delta, note, created_at
            FROM item_events_old
            """
        )
        connection.execute("DROP TABLE item_events_old")

    def _create_inventory_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL,
                store_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT 'шт',
                min_quantity REAL NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                last_reminded_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (store_id, normalized_name),
                FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
                FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE
            )
            """
        )

    def _ensure_default_stores(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute("SELECT id FROM households ORDER BY id").fetchall():
            household_id = int(row["id"])
            store = self._first_store(connection, household_id)
            if store is None:
                self._create_store(connection, household_id, "Основной")
        users = connection.execute("SELECT chat_id, current_household_id, current_store_id FROM users").fetchall()
        for user in users:
            household_id = user["current_household_id"]
            store_id = user["current_store_id"]
            store = self._get_store(connection, store_id) if store_id is not None else None
            if household_id is not None and (store is None or store.household_id != household_id):
                replacement = self._first_store(connection, household_id)
                connection.execute(
                    "UPDATE users SET current_store_id = ? WHERE chat_id = ?",
                    (replacement.id if replacement else None, user["chat_id"]),
                )

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _migrate_initial_extra_stores(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor = connection.execute(
            """
            INSERT INTO schema_migrations (name) VALUES ('seed_initial_extra_stores')
            ON CONFLICT(name) DO NOTHING
            """
        )
        if cursor.rowcount == 0:
            return
        for row in connection.execute("SELECT id FROM households ORDER BY id").fetchall():
            self._create_initial_extra_stores(connection, int(row["id"]))

    def _create_initial_stores(self, connection: sqlite3.Connection, household_id: int) -> Store:
        store = self._create_store(connection, household_id, "Основной")
        self._create_initial_extra_stores(connection, household_id)
        return store

    def _create_initial_extra_stores(self, connection: sqlite3.Connection, household_id: int) -> None:
        for name in ("Морозилка", "Холодос"):
            self._create_store(connection, household_id, name)

    def _create_store(
        self,
        connection: sqlite3.Connection,
        household_id: int,
        name: str,
    ) -> Store:
        clean_name = " ".join(name.split()) or "Основной"
        normalized = normalize_name(clean_name)
        connection.execute(
            """
            INSERT INTO stores (household_id, name, normalized_name)
            VALUES (?, ?, ?)
            ON CONFLICT(household_id, normalized_name) DO NOTHING
            """,
            (household_id, clean_name, normalized),
        )
        row = connection.execute(
            "SELECT * FROM stores WHERE household_id = ? AND normalized_name = ?",
            (household_id, normalized),
        ).fetchone()
        if row is None:
            raise KeyError("Failed to create store")
        return self._row_to_store(row)

    def _first_store(self, connection: sqlite3.Connection, household_id: int | None) -> Store | None:
        if household_id is None:
            return None
        row = connection.execute(
            "SELECT * FROM stores WHERE household_id = ? ORDER BY created_at, id LIMIT 1",
            (household_id,),
        ).fetchone()
        return self._row_to_store(row) if row else None

    def _new_invite_code(self, connection: sqlite3.Connection) -> str:
        while True:
            code = secrets.token_urlsafe(5).replace("-", "").replace("_", "").upper()[:7]
            exists = connection.execute("SELECT 1 FROM households WHERE invite_code = ?", (code,)).fetchone()
            if exists is None:
                return code

    def _get_user(self, connection: sqlite3.Connection, chat_id: int) -> User:
        row = connection.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            raise KeyError(f"User {chat_id} does not exist")
        return self._row_to_user(row)

    def _get_household(self, connection: sqlite3.Connection, household_id: int) -> Household:
        row = connection.execute("SELECT * FROM households WHERE id = ?", (household_id,)).fetchone()
        if row is None:
            raise KeyError(f"Household {household_id} does not exist")
        return self._row_to_household(row)

    def _get_store(self, connection: sqlite3.Connection, store_id: int | None) -> Store | None:
        if store_id is None:
            return None
        row = connection.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        return self._row_to_store(row) if row else None

    def _get_item(self, connection: sqlite3.Connection, item_id: int) -> InventoryItem | None:
        row = connection.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def _get_item_by_name(self, connection: sqlite3.Connection, store_id: int, normalized_name: str) -> InventoryItem:
        item = self._get_item_by_name_or_none(connection, store_id, normalized_name)
        if item is None:
            raise KeyError(f"Item {normalized_name} does not exist")
        return item

    def _get_item_by_name_or_none(self, connection: sqlite3.Connection, store_id: int, normalized_name: str) -> InventoryItem | None:
        row = connection.execute(
            "SELECT * FROM inventory_items WHERE store_id = ? AND normalized_name = ? AND active = 1",
            (store_id, normalized_name),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def _is_member(self, connection: sqlite3.Connection, household_id: int, chat_id: int) -> bool:
        row = connection.execute(
            "SELECT 1 FROM household_members WHERE household_id = ? AND chat_id = ?",
            (household_id, chat_id),
        ).fetchone()
        return row is not None

    def _row_to_household(self, row: sqlite3.Row) -> Household:
        return Household(
            id=row["id"],
            name=row["name"],
            invite_code=row["invite_code"],
            owner_chat_id=row["owner_chat_id"],
            created_at=row["created_at"],
        )

    def _row_to_store(self, row: sqlite3.Row) -> Store:
        return Store(
            id=row["id"],
            household_id=row["household_id"],
            name=row["name"],
            created_at=row["created_at"],
        )

    def _row_to_item(self, row: sqlite3.Row) -> InventoryItem:
        return InventoryItem(
            id=row["id"],
            household_id=row["household_id"],
            store_id=row["store_id"],
            name=row["name"],
            normalized_name=row["normalized_name"],
            quantity=float(row["quantity"]),
            unit=row["unit"],
            min_quantity=float(row["min_quantity"]),
            active=bool(row["active"]),
            notes=row["notes"],
            updated_at=row["updated_at"],
        )

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            chat_id=row["chat_id"],
            username=row["username"],
            first_name=row["first_name"],
            current_household_id=row["current_household_id"],
            current_store_id=row["current_store_id"],
            last_seen=row["last_seen"],
        )

    def _row_to_active_view(self, row: sqlite3.Row) -> ActiveView:
        return ActiveView(
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            view_type=row["view_type"],
            household_id=row["household_id"],
            store_id=row["store_id"],
            item_id=row["item_id"],
            page=int(row["page"]),
            updated_at=row["updated_at"],
        )
