from __future__ import annotations

from pathlib import Path

from bot import convert_quantity, hidden_item_name, parse_amount_text
from storage import Storage, format_quantity


def test_household_invite_shares_stores_and_items(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "inventory.sqlite3")
    anna = storage.record_user(1, "anna", "Anna")
    max_user = storage.record_user(2, "max", "Max")

    home = storage.create_household(anna.chat_id, "Дом")
    kitchen = storage.create_store(anna.chat_id, home.id, "Кухня")
    joined = storage.join_household(max_user.chat_id, home.invite_code)

    assert joined is not None
    assert joined.id == home.id
    assert [store.name for store in storage.list_stores(joined.id)] == ["Основной", "Кухня"]

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


def test_parse_attached_units() -> None:
    parsed = parse_amount_text("корм 9кг мин 500г")

    assert parsed.name == "корм"
    assert parsed.quantity == 9
    assert parsed.unit == "кг"
    assert parsed.min_quantity == 500
    assert parsed.min_unit == "г"


def test_convert_compatible_units() -> None:
    assert convert_quantity(2500, "г", "кг") == 2.5
    assert convert_quantity(2.5, "кг", "г") == 2500
    assert convert_quantity(500, "мл", "л") == 0.5
    assert convert_quantity(1, "шт", "кг") is None


def test_hide_command_accepts_smart_dashes() -> None:
    assert hidden_item_name("-- горох") == "горох"
    assert hidden_item_name("— горох") == "горох"
    assert hidden_item_name("– горох") == "горох"
    assert hidden_item_name("- горох 1") is None
