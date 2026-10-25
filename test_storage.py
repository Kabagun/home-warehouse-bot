from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from storage import Storage, Store, format_quantity


def test_household_invite_shares_stores_and_items(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    anna = storage.record_user(1, "anna", "Anna")
    max_user = storage.record_user(2, "max", "Max")

    home = storage.create_household(anna.chat_id, "Дом")
    kitchen = storage.create_store(anna.chat_id, home.id, "Кухня")
    joined = storage.join_household(max_user.chat_id, home.invite_code)

    assert joined is not None
    assert joined.id == home.id
    assert [store.name for store in storage.list_stores(joined.id)] == ["Основной", "Морозилка", "Холодос", "Кухня"]

    coffee = storage.add_or_update_item(kitchen.id, anna.chat_id, "Кофе", 2, "пачки", 1)
    storage.set_current_store(max_user.chat_id, kitchen.id)
    storage.change_item_quantity(kitchen.id, max_user.chat_id, coffee.id, -1)

    items = storage.list_items(kitchen.id)
    assert len(items) == 1
    assert items[0].name == "Кофе"
    assert items[0].quantity == 1
    assert items[0].is_low


def test_one_household_can_have_separate_stores(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    home = storage.create_household(user.chat_id, "Дом")
    kitchen = storage.create_store(user.chat_id, home.id, "Кухня")
    bathroom = storage.create_store(user.chat_id, home.id, "Ванная")

    storage.add_or_update_item(kitchen.id, user.chat_id, "Кофе", 1, "пачка", 1)
    storage.add_or_update_item(bathroom.id, user.chat_id, "Порошок", 3, "кг", 1)

    assert [item.name for item in storage.list_items(kitchen.id)] == ["Кофе"]
    assert [item.name for item in storage.list_items(bathroom.id)] == ["Порошок"]


def test_user_starts_without_household(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")

    assert storage.current_household(user.chat_id) is None
    assert storage.current_store(user.chat_id) is None


def test_remove_household_for_user(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    home = storage.create_household(user.chat_id, "Дом")

    assert storage.remove_household_for_user(user.chat_id, home.id) == "deleted"
    assert storage.current_household(user.chat_id) is None
    assert storage.current_store(user.chat_id) is None


def test_rename_household_and_store(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    home = storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)

    renamed_home = storage.rename_household(user.chat_id, home.id, "Наш дом")
    renamed_store = storage.rename_store(user.chat_id, store.id, "Кухня")

    assert renamed_home is not None
    assert renamed_home.name == "Наш дом"
    assert renamed_store is not None
    assert renamed_store.name == "Кухня"


def test_hide_item_and_readd_starts_from_new_quantity(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    home = storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)

    item = storage.add_or_update_item(store.id, user.chat_id, "Горох", 1, "шт", None)

    assert item.min_quantity == 1
    assert storage.low_items_by_store(home.id)
    assert storage.deactivate_item(store.id, item.id)
    assert storage.find_item(store.id, "горох") is None
    assert storage.low_items_by_store(home.id) == []

    new_item = storage.add_or_update_item(store.id, user.chat_id, "Горох", 2, "шт", None)

    assert new_item.quantity == 2


def test_rename_item(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)

    item = storage.add_or_update_item(store.id, user.chat_id, "Миг", 8, "табл", None)
    renamed = storage.rename_item(store.id, user.chat_id, item.id, "Миг 400")

    assert renamed is not None
    assert renamed.name == "Миг 400"
    assert storage.find_item(store.id, "Миг 400").id == item.id


def test_rename_item_rejects_duplicate_name(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)

    mig = storage.add_or_update_item(store.id, user.chat_id, "Миг", 8, "табл", None)
    storage.add_or_update_item(store.id, user.chat_id, "Нурофен", 1, "шт", None)

    assert storage.rename_item(store.id, user.chat_id, mig.id, "Нурофен") is None
    assert storage.find_item(store.id, "Миг").id == mig.id


def test_format_quantity() -> None:
    assert format_quantity(1.0) == "1"
    assert format_quantity(1.5) == "1.5"


def test_archive_restore_and_unit_change(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)
    item = storage.add_or_update_item(store.id, user.chat_id, "Корм", 4.5, "кг", 0.5)

    converted = storage.change_item_unit(store.id, user.chat_id, item.id, "г", 4500, 500)
    archived = storage.archive_item(store.id, user.chat_id, item.id)

    assert converted.quantity == 4500
    assert converted.min_quantity == 500
    assert converted.unit == "г"
    assert archived is not None and not archived.active
    assert storage.list_items(store.id) == []
    assert storage.list_items(store.id, include_inactive=True)[0].name == "Корм"

    restored = storage.restore_item(store.id, user.chat_id, item.id)

    assert restored is not None and restored.active
    assert restored.quantity == 4500
    assert restored.min_quantity == 500


def test_delete_item_permanently_removes_active_and_archived_items(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)
    active = storage.add_or_update_item(store.id, user.chat_id, "Миг", 8, "табл", 1)
    archived = storage.add_or_update_item(store.id, user.chat_id, "Горох", 0, "шт", 1)
    storage.archive_item(store.id, user.chat_id, archived.id)

    assert storage.delete_item(store.id, user.chat_id, active.id)
    assert storage.delete_item(store.id, user.chat_id, archived.id)
    assert storage.get_item(active.id) is None
    assert storage.get_item(archived.id) is None
    assert storage.list_items(store.id, include_inactive=True) == []


def test_quantity_change_is_clamped_and_active_view_is_persisted(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    home = storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)
    item = storage.add_or_update_item(store.id, user.chat_id, "Миг", 1, "табл", 1)

    updated = storage.change_item_quantity(store.id, user.chat_id, item.id, -5)
    view = storage.set_active_view(
        user.chat_id,
        55,
        "item",
        household_id=home.id,
        store_id=store.id,
        item_id=item.id,
        page=2,
    )

    assert updated.quantity == 0
    assert view.message_id == 55
    assert storage.get_active_view(user.chat_id) == view
    assert storage.active_views_for_household(home.id) == [view]

    storage.clear_active_view(user.chat_id, 55)

    assert storage.get_active_view(user.chat_id) is None


def test_household_role_distinguishes_delete_from_leave(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    owner = storage.record_user(1, "anna", "Anna")
    member = storage.record_user(2, "max", "Max")
    home = storage.create_household(owner.chat_id, "Дом")
    storage.join_household(member.chat_id, home.invite_code)

    assert storage.household_role(home.id, owner.chat_id) == "owner"
    assert storage.household_role(home.id, member.chat_id) == "member"


def test_former_member_cannot_change_items_from_stale_buttons(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    owner = storage.record_user(1, "anna", "Anna")
    member = storage.record_user(2, "max", "Max")
    home = storage.create_household(owner.chat_id, "Дом")
    storage.join_household(member.chat_id, home.invite_code)
    store = storage.current_store(owner.chat_id)
    item = storage.add_or_update_item(store.id, owner.chat_id, "Миг", 8, "табл", 1)

    assert storage.remove_household_for_user(member.chat_id, home.id) == "left"
    assert storage.change_item_quantity(store.id, member.chat_id, item.id, -1) is None
    assert storage.archive_item(store.id, member.chat_id, item.id) is None
    assert not storage.delete_item(store.id, member.chat_id, item.id)
    assert storage.change_item_unit(store.id, member.chat_id, item.id, "шт", 8, 1) is None
    assert storage.get_item(item.id).quantity == 8


def test_concurrent_quantity_changes_are_not_lost(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    user = storage.record_user(1, "anna", "Anna")
    storage.create_household(user.chat_id, "Дом")
    store = storage.current_store(user.chat_id)
    item = storage.add_or_update_item(store.id, user.chat_id, "Миг", 0, "табл", 1)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: storage.change_item_quantity(store.id, user.chat_id, item.id, 1),
                range(40),
            )
        )

    assert storage.get_item(item.id).quantity == 40


def test_store_has_only_ordinary_store_fields() -> None:
    assert tuple(Store.__dataclass_fields__) == ("id", "household_id", "name", "created_at")


def test_new_household_creates_three_ordinary_stores_and_selects_main(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    home = storage.create_household(1, "Дом")

    for opened in (storage, Storage(path)):
        stores = opened.list_stores(home.id)
        assert [store.name for store in stores] == ["Основной", "Морозилка", "Холодос"]
        assert opened.current_store(1) == stores[0]
    with sqlite3.connect(path) as connection:
        assert "kind" not in {row[1] for row in connection.execute("PRAGMA table_info(stores)")}


def test_store_seed_migration_preserves_existing_data_and_selection(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    storage.record_user(2, None, None)
    home = storage.create_household(1, "Тестовый дом")
    store = storage.create_store(1, home.id, "Кладовая")
    item = storage.add_or_update_item(store.id, 1, "Крупа", 2.5, "кг", 3)
    storage.change_item_quantity(store.id, 1, item.id, -0.5)
    storage.set_active_view(1, 55, "item", household_id=home.id, store_id=store.id, item_id=item.id)
    other_home = storage.create_household(2, "Второй дом")
    other_store = storage.create_store(2, other_home.id, "Лекарства")
    other_item = storage.add_or_update_item(other_store.id, 2, "Тестовый товар", 1, "шт", 1)
    storage.archive_item(other_store.id, 2, other_item.id)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory_items SET notes = 'тестовая заметка' WHERE id = ?", (item.id,))
        connection.execute("DELETE FROM stores WHERE normalized_name IN ('морозилка', 'холодос')")
        connection.execute("DROP TABLE schema_migrations")
        tables = ("users", "households", "household_members", "stores", "inventory_items", "item_events", "active_views")
        snapshots = {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}
        assert "kind" not in {row[1] for row in connection.execute("PRAGMA table_info(stores)")}

    for _ in range(2):
        reopened = Storage(path)
        assert reopened.current_store(1).id == store.id
        assert [value.name for value in reopened.list_stores(home.id)] == ["Основной", "Кладовая", "Морозилка", "Холодос"]
        assert [value.name for value in reopened.list_stores(other_home.id)] == ["Основной", "Лекарства", "Морозилка", "Холодос"]
        assert reopened.current_store(2).id == other_store.id
        assert reopened.get_item(item.id).quantity == 2
        assert reopened.get_item(item.id).notes == "тестовая заметка"
        with sqlite3.connect(path) as connection:
            for table, before in snapshots.items():
                query = f"SELECT * FROM {table}"
                params: tuple[int, ...] = ()
                if table == "stores":
                    params = tuple(row[0] for row in before)
                    query += f" WHERE id IN ({','.join('?' for _ in params)})"
                assert connection.execute(query + " ORDER BY 1", params).fetchall() == before
            assert connection.execute("SELECT name FROM schema_migrations").fetchall() == [("seed_initial_extra_stores",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_named_stores_keep_separate_inventory(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    storage.record_user(1, None, None)
    home = storage.create_household(1, "Дом")
    freezer, fridge = storage.list_stores(home.id)[1:]

    freezer_item = storage.add_or_update_item(freezer.id, 1, "Молоко", 1, "л", 1)
    fridge_item = storage.add_or_update_item(fridge.id, 1, "Молоко", 0.5, "л", 1)
    storage.change_item_quantity(fridge.id, 1, fridge_item.id, -0.25)

    assert storage.set_current_store(1, fridge.id) == fridge
    assert storage.get_item(freezer_item.id).quantity == 1
    assert storage.list_items(fridge.id)[0].quantity == 0.25
    assert [(store.name, [item.id for item in items]) for store, items in storage.low_items_by_store(home.id)] == [
        ("Морозилка", [freezer_item.id]),
        ("Холодос", [fridge_item.id]),
    ]
    assert storage.rename_store(1, fridge.id, "Кухня").name == "Кухня"


def test_seeded_stores_share_access_only_with_household_members(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    for chat_id in (1, 2, 3):
        storage.record_user(chat_id, None, None)
    home = storage.create_household(1, "Общий дом")
    fridge = storage.list_stores(home.id)[2]
    item = storage.add_or_update_item(fridge.id, 1, "Молоко", 1, "л", 0.5)
    other_home = storage.create_household(3, "Другой дом")
    other_stores = storage.list_stores(other_home.id)
    other_fridge = other_stores[2]
    storage.join_household(2, home.invite_code)

    assert storage.set_current_store(2, fridge.id) == fridge
    assert storage.change_item_quantity(fridge.id, 2, item.id, -0.25).quantity == 0.75
    assert storage.create_store(2, home.id, "Коридор").name == "Коридор"
    assert storage.set_current_store(3, fridge.id) is None
    assert storage.create_store(3, home.id, "Недоступный склад") is None
    with pytest.raises(KeyError):
        storage.add_or_update_item(fridge.id, 3, "Молоко", 1, "л")
    assert storage.change_item_quantity(fridge.id, 3, item.id, -1) is None
    assert storage.change_item_quantity(other_fridge.id, 2, item.id, -1) is None
    assert storage.rename_store(3, fridge.id, "Другое имя") is None
    assert not storage.remove_store(3, fridge.id)
    assert storage.get_item(item.id).quantity == 0.75
    assert storage.list_stores(other_home.id) == other_stores


def test_create_store_selects_target_household_and_store_together(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    storage.record_user(1, None, None)
    first_home = storage.create_household(1, "Первый дом")
    second_home = storage.create_household(1, "Второй дом")
    assert storage.current_household(1) == second_home

    fridge = storage.create_store(1, first_home.id, "Холодос")

    assert storage.current_household(1) == first_home
    assert storage.current_store(1) == fridge
    assert storage.get_user(1).current_household_id == first_home.id
    assert storage.get_user(1).current_store_id == fridge.id


def test_store_seed_migration_deduplicates_only_exact_normalized_names(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    home = storage.create_household(1, "Дом")
    freezer, fridge = storage.list_stores(home.id)[1:]
    freezer = storage.rename_store(1, freezer.id, "  МОРОЗИЛКА  ")
    fridge = storage.rename_store(1, fridge.id, "  ХоЛоДоС  ")
    near_match = storage.create_store(1, home.id, "Холодильник")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE schema_migrations")

    reopened = Storage(path)

    assert reopened.list_stores(home.id)[1:] == [freezer, fridge, near_match]
    assert reopened.current_store(1) == near_match


@pytest.mark.parametrize("migrate_existing", [False, True])
def test_deleted_or_renamed_seeded_stores_are_not_restored(tmp_path: Path, migrate_existing: bool) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    home = storage.create_household(1, "Дом")
    if migrate_existing:
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM stores WHERE normalized_name IN ('морозилка', 'холодос')")
            connection.execute("DROP TABLE schema_migrations")
        storage = Storage(path)
    freezer, fridge = storage.list_stores(home.id)[1:]
    storage.rename_store(1, freezer.id, "Запасы")
    assert storage.remove_store(1, fridge.id)

    for _ in range(2):
        storage = Storage(path)
        assert [store.name for store in storage.list_stores(home.id)] == ["Основной", "Запасы"]


def test_legacy_kind_column_is_preserved_and_ignored(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    home = storage.create_household(1, "Дом")
    freezer, fridge = storage.list_stores(home.id)[1:]
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE stores ADD COLUMN kind TEXT NOT NULL DEFAULT 'cupboard'")
        connection.execute("UPDATE stores SET kind = 'fridge' WHERE id = ?", (fridge.id,))
        connection.execute("UPDATE stores SET kind = 'unknown_legacy_kind' WHERE id = ?", (freezer.id,))
        connection.execute("DROP TABLE schema_migrations")

    reopened = Storage(path)

    assert reopened.get_store(fridge.id) == fridge
    assert reopened.create_store(1, home.id, "холодос") == fridge
    assert reopened.rename_store(1, freezer.id, "Лекарства").name == "Лекарства"
    assert reopened.create_store(1, home.id, "Кухня").name == "Кухня"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT kind FROM stores WHERE id = ?", (fridge.id,)).fetchone() == ("fridge",)
        assert connection.execute("SELECT kind FROM stores WHERE id = ?", (freezer.id,)).fetchone() == ("unknown_legacy_kind",)


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), float("-inf")])
def test_invalid_inventory_amounts_cannot_mutate_items(tmp_path: Path, amount: float) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    storage.create_household(1, "Дом")
    store = storage.current_store(1)
    item = storage.add_or_update_item(store.id, 1, "Крупа", 2, "кг", 1)

    mutations = (
        lambda: storage.add_or_update_item(store.id, 1, "Крупа", amount, "кг", 1),
        lambda: storage.add_or_update_item(store.id, 1, "Новый товар", 1, "кг", amount),
        lambda: storage.set_item_quantity(store.id, 1, item.id, amount),
        lambda: storage.set_item_minimum(store.id, item.id, amount, chat_id=1),
        lambda: storage.change_item_unit(store.id, 1, item.id, "г", amount, 1),
        lambda: storage.change_item_unit(store.id, 1, item.id, "г", 1, amount),
    )
    for mutate in mutations:
        with pytest.raises(ValueError, match="finite and nonnegative"):
            mutate()
        assert storage.get_item(item.id) == item
        assert storage.find_item(store.id, "Новый товар") is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == 1


@pytest.mark.parametrize("delta", [float("nan"), float("inf"), float("-inf")])
def test_quantity_delta_must_be_finite(tmp_path: Path, delta: float) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    storage.record_user(1, None, None)
    storage.create_household(1, "Дом")
    store = storage.current_store(1)
    item = storage.add_or_update_item(store.id, 1, "Крупа", 2, "кг", 1)

    with pytest.raises(ValueError, match="delta must be finite"):
        storage.change_item_quantity(store.id, 1, item.id, delta)
    assert storage.get_item(item.id) == item


def test_quantity_overflow_rolls_back_item_and_event(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite3"
    storage = Storage(path)
    storage.record_user(1, None, None)
    storage.create_household(1, "Дом")
    store = storage.current_store(1)
    item = storage.add_or_update_item(store.id, 1, "Тестовый товар", 1e308, "шт", 1)

    for mutate in (
        lambda: storage.add_or_update_item(store.id, 1, item.name, 1e308, "шт"),
        lambda: storage.change_item_quantity(store.id, 1, item.id, 1e308),
    ):
        with pytest.raises(ValueError, match="resulting quantity must be finite"):
            mutate()
        assert storage.get_item(item.id) == item
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == 1
