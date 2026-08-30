from __future__ import annotations

import asyncio
from pathlib import Path

from inventory_bot import (
    Settings,
    buy_panel,
    build_app,
    has_explicit_amount_unit,
    item_panel,
    main_menu,
    refresh_household_views,
    store_panel,
)
from storage import Storage


def make_shared_storage(tmp_path: Path) -> tuple[Storage, object, object, object, object]:
    storage = Storage(tmp_path / "inventory.sqlite3")
    anna = storage.record_user(1, "anna", "Anna")
    max_user = storage.record_user(2, "max", "Max")
    home = storage.create_household(anna.chat_id, "Дом")
    storage.join_household(max_user.chat_id, home.invite_code)
    store = storage.current_store(anna.chat_id)
    storage.set_current_store(max_user.chat_id, store.id)
    return storage, anna, max_user, home, store


def test_main_menu_is_stable_four_button_navigation() -> None:
    menu = main_menu()

    assert [[button.text for button in row] for row in menu.keyboard] == [
        ["Склады", "Купить"],
        ["Дом", "Помощь"],
    ]


def test_quick_amount_without_unit_inherits_existing_item_unit() -> None:
    assert not has_explicit_amount_unit("Миг 2")
    assert not has_explicit_amount_unit("корм 2 мин 500г")
    assert has_explicit_amount_unit("Миг 2табл")
    assert has_explicit_amount_unit("корм 1,5 кг")


def test_store_panel_sorts_low_items_first_and_paginates(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    storage.add_or_update_item(store.id, anna.chat_id, "Запас", 5, "шт", 1)
    storage.add_or_update_item(store.id, anna.chat_id, "Нужно", 1, "шт", 1)
    for index in range(11):
        storage.add_or_update_item(store.id, anna.chat_id, f"Товар {index:02d}", 3, "шт", 1)

    text, keyboard, page = store_panel(storage, store, 0)
    labels = [row[0].text for row in keyboard.inline_keyboard]

    assert page == 0
    assert "Позиций: 13 · купить: 1" in text
    assert labels[0].startswith("− Нужно")
    assert len([label for label in labels if label.startswith(("−", "•"))]) == 10
    assert any("Следующая" in label for label in labels)


def test_item_panel_contains_only_context_actions_and_back(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Миг", 8, "табл", 1)

    text, keyboard = item_panel(item, 0)
    labels = [[button.text for button in row] for row in keyboard.inline_keyboard]

    assert "Остаток: 8 табл" in text
    assert labels == [["−1", "+1"], ["Списать", "Добавить"], ["Настройки"], ["Назад"]]


def test_buy_panel_groups_all_stores_as_buttons(tmp_path: Path) -> None:
    storage, anna, _, home, store = make_shared_storage(tmp_path)
    second = storage.create_store(anna.chat_id, home.id, "Кухня")
    storage.add_or_update_item(store.id, anna.chat_id, "Миг", 1, "табл", 1)
    storage.add_or_update_item(second.id, anna.chat_id, "Горох", 0, "шт", 1)

    text, keyboard, page = buy_panel(storage, home, 0)
    labels = [row[0].text for row in keyboard.inline_keyboard[:-1]]

    assert page == 0
    assert text == '<b>Купить для «Дом»</b>'
    assert any("Основной · Миг" in label for label in labels)
    assert any("Кухня · Горох" in label for label in labels)


def test_build_app_has_no_reminder_scheduler(tmp_path: Path) -> None:
    application = build_app(Settings("123456:ABCDEF_fake_token", str(tmp_path / "app.sqlite3")))

    assert "scheduler" not in application.bot_data
    assert application.post_init is None


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


class FakeApplication:
    def __init__(self, storage: Storage) -> None:
        self.bot_data = {"storage": storage}


class FakeContext:
    def __init__(self, storage: Storage) -> None:
        self.application = FakeApplication(storage)
        self.bot = FakeBot()


def test_shared_store_views_refresh_without_new_messages(tmp_path: Path) -> None:
    storage, anna, max_user, home, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Миг", 2, "табл", 1)
    storage.set_active_view(anna.chat_id, 101, "store", household_id=home.id, store_id=store.id)
    storage.set_active_view(max_user.chat_id, 202, "store", household_id=home.id, store_id=store.id)
    storage.change_item_quantity(store.id, anna.chat_id, item.id, -1)
    context = FakeContext(storage)

    asyncio.run(refresh_household_views(context, home.id))

    assert {(edit["chat_id"], edit["message_id"]) for edit in context.bot.edits} == {(1, 101), (2, 202)}
    assert all("купить: 1" in str(edit["text"]) for edit in context.bot.edits)
