"""Reviewed hard deletion using one caller-owned SQLite transaction.

Previews store only an opaque token's hash and a digest of the affected data.
No inventory data or current selection is written until a reviewed deletion.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import struct
import time
from dataclasses import dataclass
from typing import Any


PREVIEW_TTL = 600
_TARGETS = {"item": "inventory_items", "store": "stores", "household": "households"}
_UNAVAILABLE = "Объект не найден или недоступен."
_INVALID_PREVIEW = "Предпросмотр недействителен или истёк. Получите новый предпросмотр удаления."


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS deletion_previews (
        token_hash TEXT PRIMARY KEY,
        actor_id INTEGER NOT NULL,
        target_type TEXT NOT NULL CHECK (target_type IN ('item', 'store', 'household')),
        target_id INTEGER NOT NULL,
        snapshot_digest TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        FOREIGN KEY (actor_id) REFERENCES users(chat_id) ON DELETE CASCADE
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS deletion_previews_expiry ON deletion_previews(expires_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS deletion_previews_target ON deletion_previews(target_type, target_id)")
    _gc(connection)


def _gc(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM deletion_previews WHERE expires_at <= ?", (int(time.time()),))


def _validate_target(actor_id: int, target_type: str, target_id: int) -> None:
    if (type(actor_id) is not int or not actor_id or not -(2**63) <= actor_id < 2**63
            or not isinstance(target_type, str) or target_type not in _TARGETS
            or type(target_id) is not int or not 0 < target_id < 2**63):
        raise ValueError(_UNAVAILABLE)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _frame(digest, value: Any) -> None:
    if value is None:
        encoded = b"n"
    elif isinstance(value, int):
        encoded = b"i" + str(value).encode("ascii")
    elif isinstance(value, float):
        encoded = b"f" + struct.pack(">d", value)
    elif isinstance(value, bytes):
        encoded = b"b" + value
    else:
        encoded = b"s" + value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _read(connection, digest, label, query, arguments):
    cursor = connection.execute(query, arguments)
    rows = cursor.fetchall()
    _frame(digest, label)
    # SELECT * plus the column names makes the digest follow the persisted
    # schema, including fields that a compact preview does not display.
    for column in cursor.description:
        _frame(digest, column[0])
    _frame(digest, len(rows))
    for row in rows:
        for value in row:
            _frame(digest, value)
    return rows


@dataclass(frozen=True)
class _Snapshot:
    summary: dict[str, Any]
    digest: str
    item_predicate: str
    item_arguments: tuple[int, ...]


def _snapshot(connection: sqlite3.Connection, actor_id: int, target_type: str, target_id: int) -> _Snapshot:
    _validate_target(actor_id, target_type, target_id)
    target = connection.execute(f"SELECT * FROM {_TARGETS[target_type]} WHERE id = ?", (target_id,)).fetchone()
    if target is None or connection.execute("SELECT 1 FROM users WHERE chat_id = ?", (actor_id,)).fetchone() is None:
        raise ValueError(_UNAVAILABLE)
    household_id = target_id if target_type == "household" else target["household_id"]
    household = connection.execute("SELECT * FROM households WHERE id = ?", (household_id,)).fetchone()
    member = connection.execute(
        "SELECT role FROM household_members WHERE household_id = ? AND chat_id = ?", (household_id, actor_id),
    ).fetchone()
    if household is None or member is None:
        raise ValueError(_UNAVAILABLE)
    if target_type == "household" and (household["owner_chat_id"] != actor_id or member["role"] != "owner"):
        raise ValueError(_UNAVAILABLE)

    digest = hashlib.sha256()
    _frame(digest, target_type)
    _frame(digest, target_id)
    _read(connection, digest, "household", "SELECT * FROM households WHERE id = ?", (household_id,))
    members = _read(connection, digest, "members", "SELECT * FROM household_members WHERE household_id = ? ORDER BY chat_id", (household_id,))
    stores = _read(connection, digest, "stores", "SELECT * FROM stores WHERE household_id = ? ORDER BY id", (household_id,))
    if target_type == "store" and len(stores) <= 1:
        raise ValueError("Последний склад дома удалить нельзя. Создайте другой склад или удалите дом целиком.")
    if target_type == "household":
        predicate = "i.household_id = ? OR i.store_id IN (SELECT id FROM stores WHERE household_id = ?)"
        arguments = (household_id, household_id)
        affected_stores = stores
    elif target_type == "store":
        predicate, arguments = "i.store_id = ?", (target_id,)
        affected_stores = [store for store in stores if store["id"] == target_id]
    else:
        predicate, arguments = "i.id = ?", (target_id,)
        affected_stores = [store for store in stores if store["id"] == target["store_id"]]
    if not affected_stores and target_type != "household":
        raise ValueError(_UNAVAILABLE)
    items = _read(connection, digest, "items", f"SELECT i.* FROM inventory_items i WHERE {predicate} ORDER BY i.id", arguments)
    store_ids = {store["id"] for store in stores}
    if any(item["household_id"] != household_id or item["store_id"] not in store_ids for item in items):
        raise ValueError("Проверьте целостность данных перед удалением.")
    events = _read(connection, digest, "events", f"""SELECT e.* FROM item_events e
        JOIN inventory_items i ON i.id = e.item_id WHERE {predicate} ORDER BY e.id""", arguments)
    summary = {
        "target_type": target_type, "target_id": target_id, "name": target["name"],
        "household_id": household_id, "household_name": household["name"],
        "stores": [{"id": store["id"], "name": store["name"]} for store in affected_stores],
        "active_items": sum(bool(item["active"]) for item in items),
        "archived_items": sum(not item["active"] for item in items),
        "events": len(events), "members": len(members), "shared": len(members) > 1,
    }
    return _Snapshot(summary, digest.hexdigest(), predicate, arguments)


def preview(connection: sqlite3.Connection, actor_id: int, target_type: str, target_id: int) -> dict:
    snapshot = _snapshot(connection, actor_id, target_type, target_id)
    _gc(connection)
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + PREVIEW_TTL
    connection.execute("INSERT INTO deletion_previews VALUES (?, ?, ?, ?, ?, ?)", (
        _hash_token(token), actor_id, target_type, target_id, snapshot.digest, expires_at,
    ))
    return {"preview_token": token, **snapshot.summary, "expires_at": expires_at}


def _repair_selection(connection: sqlite3.Connection, actor_ids: list[int]) -> None:
    for actor_id in actor_ids:
        user = connection.execute("SELECT current_household_id, current_store_id FROM users WHERE chat_id = ?", (actor_id,)).fetchone()
        household_id = user["current_household_id"]
        member = connection.execute(
            "SELECT 1 FROM household_members WHERE household_id = ? AND chat_id = ?", (household_id, actor_id),
        ).fetchone()
        if member is None:
            replacement = connection.execute("""SELECT h.id FROM households h
                JOIN household_members m ON m.household_id = h.id WHERE m.chat_id = ?
                ORDER BY m.joined_at, h.id LIMIT 1""", (actor_id,)).fetchone()
            household_id = replacement["id"] if replacement else None
        store = connection.execute("SELECT id FROM stores WHERE id = ? AND household_id = ?", (user["current_store_id"], household_id)).fetchone()
        if store is None:
            store = connection.execute("SELECT id FROM stores WHERE household_id = ? ORDER BY created_at, id LIMIT 1", (household_id,)).fetchone()
        connection.execute("UPDATE users SET current_household_id = ?, current_store_id = ? WHERE chat_id = ?", (
            household_id, store["id"] if store else None, actor_id,
        ))


def _invalidate_household_previews(connection: sqlite3.Connection, household_id: int) -> None:
    # IDs can be restored or reused with byte-identical data. Invalidate every
    # review in this household before cascade removes the relationships needed
    # to find descendant tokens, including tokens belonging to other actors.
    connection.execute("""DELETE FROM deletion_previews
        WHERE (target_type = 'household' AND target_id = ?)
        OR (target_type = 'store' AND target_id IN (SELECT id FROM stores WHERE household_id = ?))
        OR (target_type = 'item' AND target_id IN (
            SELECT i.id FROM inventory_items i JOIN stores s ON s.id = i.store_id
            WHERE i.household_id = ? OR s.household_id = ?
        ))""", (household_id, household_id, household_id, household_id))


def delete_reviewed(connection: sqlite3.Connection, actor_id: int, target_type: str, target_id: int, token: str) -> dict:
    _validate_target(actor_id, target_type, target_id)
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise ValueError(_INVALID_PREVIEW)
    token_hash = _hash_token(token)
    preview_row = connection.execute("""SELECT snapshot_digest FROM deletion_previews
        WHERE token_hash = ? AND actor_id = ? AND target_type = ? AND target_id = ? AND expires_at > ?""",
        (token_hash, actor_id, target_type, target_id, int(time.time())),
    ).fetchone()
    if preview_row is None:
        raise ValueError(_INVALID_PREVIEW)
    snapshot = _snapshot(connection, actor_id, target_type, target_id)
    if not hmac.compare_digest(snapshot.digest, preview_row["snapshot_digest"]):
        raise ValueError("Данные изменились после предпросмотра. Получите новый предпросмотр удаления.")

    summary = snapshot.summary
    _invalidate_household_previews(connection, summary["household_id"])
    affected_users: list[int] = []
    if target_type == "household":
        affected_users = [row[0] for row in connection.execute("""SELECT chat_id FROM users
            WHERE current_household_id = ? OR current_store_id IN (SELECT id FROM stores WHERE household_id = ?)""",
            (target_id, target_id))]
        connection.execute("""DELETE FROM active_views WHERE household_id = ?
            OR store_id IN (SELECT id FROM stores WHERE household_id = ?)
            OR item_id IN (SELECT i.id FROM inventory_items i WHERE """ + snapshot.item_predicate + ")",
            (target_id, target_id, *snapshot.item_arguments))
    elif target_type == "store":
        affected_users = [row[0] for row in connection.execute("SELECT chat_id FROM users WHERE current_store_id = ?", (target_id,))]
        connection.execute("DELETE FROM active_views WHERE store_id = ? OR item_id IN (SELECT i.id FROM inventory_items i WHERE i.store_id = ?)", (target_id, target_id))
    else:
        connection.execute("DELETE FROM active_views WHERE item_id = ?", (target_id,))
    connection.execute(f"DELETE FROM {_TARGETS[target_type]} WHERE id = ?", (target_id,))
    _repair_selection(connection, affected_users)
    _gc(connection)
    return {
        "target_type": target_type, "target_id": target_id, "name": summary["name"],
        "household_id": summary["household_id"], "deleted": True,
        "stores": len(summary["stores"]) if target_type != "item" else 0,
        "active_items": summary["active_items"], "archived_items": summary["archived_items"],
        "events": summary["events"], "members": summary["members"] if target_type == "household" else 0,
    }
