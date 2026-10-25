from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from storage import Storage


@pytest.fixture
def pre_store_database(tmp_path):
    """Historical household inventory with no stores or selected-store column."""
    path = tmp_path / "pre-store.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                current_household_id INTEGER,
                reminders_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT
            );
            CREATE TABLE households (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                invite_code TEXT NOT NULL UNIQUE,
                owner_chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE household_members (
                household_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (household_id, chat_id),
                FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            );
            CREATE TABLE inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL,
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
                UNIQUE (household_id, normalized_name),
                FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
            );
            CREATE TABLE item_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                delta REAL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES inventory_items(id) ON DELETE CASCADE
            );
            INSERT INTO users (chat_id, username, first_name, current_household_id)
                VALUES (1, 'synthetic_one', 'Synthetic', 7), (2, 'synthetic_two', NULL, 19);
            INSERT INTO households (id, name, invite_code, owner_chat_id)
                VALUES (7, 'Existing home', 'SAMPLE1', 1), (19, 'Other home', 'SAMPLE2', 2);
            INSERT INTO household_members (household_id, chat_id, role)
                VALUES (7, 1, 'owner'), (19, 2, 'owner');
            INSERT INTO inventory_items (
                id, household_id, name, normalized_name, quantity, unit, min_quantity,
                active, notes, last_reminded_at, created_at, updated_at
            ) VALUES
                (11, 7, 'Rice', 'rice', 2.5, 'кг', 0.5, 1, 'Synthetic note', NULL,
                    '2001-01-01 00:00:00', '2001-01-02 00:00:00'),
                (53, 19, 'Milk', 'milk', 0.75, 'л', 1, 0, NULL, '2001-01-03 00:00:00',
                    '2001-01-01 00:00:00', '2001-01-03 00:00:00');
            INSERT INTO item_events (id, item_id, chat_id, action, delta, note, created_at)
                VALUES (4, 11, 1, 'add', 3, 'Synthetic addition', '2001-01-01 00:00:00'),
                    (9, 11, 1, 'change', -0.5, NULL, '2001-01-02 00:00:00'),
                    (14, 53, 2, 'archive', NULL, 'Synthetic archive', '2001-01-03 00:00:00');
        """)
        tables = ("users", "households", "household_members", "inventory_items", "item_events")
        original_columns = {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
    return path, original_columns, read_original_columns(path, original_columns)


def read_original_columns(path, columns):
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY 1").fetchall()
            for table, names in columns.items()
        }



def database_snapshot(path):
    return path.read_bytes(), {entry.name for entry in path.parent.iterdir()}


def test_pre_store_migration_preserves_all_items_events_and_ids(pre_store_database):
    path, columns, original = pre_store_database
    for _ in range(2):
        storage = Storage(path)
        assert read_original_columns(path, columns) == original
        assert storage.list_households(1)[0].name == "Existing home"
        for household_id, item_id in ((7, 11), (19, 53)):
            stores = storage.list_stores(household_id)
            assert [store.name for store in stores] == ["Основной", "Морозилка", "Холодос"]
            assert storage.get_item(item_id).store_id == stores[0].id
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            event_foreign_keys = connection.execute("PRAGMA foreign_key_list(item_events)").fetchall()
            assert len(event_foreign_keys) == 1
            assert event_foreign_keys[0][2:5] == ("inventory_items", "item_id", "id")
            assert event_foreign_keys[0][6] == "CASCADE"
            assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_old'").fetchall() == []


def test_failed_pre_store_migration_rolls_back_schema_and_data(pre_store_database, monkeypatch):
    path, columns, original = pre_store_database
    before = database_snapshot(path)

    def fail_after_rebuilding_inventory(self, connection):
        raise sqlite3.OperationalError("Synthetic late migration failure")

    monkeypatch.setattr(Storage, "_migrate_initial_extra_stores", fail_after_rebuilding_inventory)
    with pytest.raises(sqlite3.OperationalError, match="Synthetic late migration failure"):
        Storage(path)
    assert database_snapshot(path) == before
    assert read_original_columns(path, columns) == original


def test_pre_store_orphan_items_fail_without_dropping_original_data(pre_store_database):
    path, columns, _ = pre_store_database
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("UPDATE inventory_items SET household_id = 999 WHERE id = 11")
    before = database_snapshot(path)
    original = read_original_columns(path, columns)
    with pytest.raises(sqlite3.IntegrityError, match="without its household"):
        Storage(path)
    assert database_snapshot(path) == before
    assert read_original_columns(path, columns) == original
