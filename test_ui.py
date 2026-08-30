from __future__ import annotations

import asyncio
from pathlib import Path

from inventory_bot import (
    Settings,
    archived_item_panel,
    buy_panel,
    build_app,
    handle_flow_text,
    has_explicit_amount_unit,
    item_create_prompt,
    item_panel,
    item_settings_panel,
    main_menu,
    parse_amount_list,
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


def test_new_item_form_accepts_multiline_list_with_optional_plus() -> None:
    parsed, errors = parse_amount_list("Миг 8табл\n\n+ корм 4,5кг мин 500г\n2 кг")

    assert errors == [(4, "Сначала напишите название, потом количество.")]
    assert [(line, item.name, item.quantity, item.unit) for line, item in parsed] == [
        (1, "Миг", 8, "табл"),
        (3, "корм", 4.5, "кг"),
    ]


def test_new_item_prompt_stays_ready_for_more_items(tmp_path: Path) -> None:
    _, _, _, _, store = make_shared_storage(tmp_path)

    text = item_create_prompt(store, ["Миг", "Корм"], [(3, "«Бумага» уже есть на складе.")])

    assert "Добавлено: 2" in text
    assert "Отправьте одну позицию или список" in text
    assert "строка 3" in text


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


def test_item_settings_and_archive_offer_permanent_delete(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Миг", 8, "табл", 1)

    _, settings_keyboard = item_settings_panel(item, 0)
    storage.archive_item(store.id, anna.chat_id, item.id)
    archived = storage.get_item(item.id)
    _, archived_keyboard = archived_item_panel(archived, 0)

    settings_labels = [[button.text for button in row] for row in settings_keyboard.inline_keyboard]
    archived_labels = [[button.text for button in row] for row in archived_keyboard.inline_keyboard]

    assert ["Удалить"] in settings_labels
    assert ["Удалить"] in archived_labels


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
        self.user_data: dict[str, object] = {}


def test_item_create_flow_adds_lists_and_remains_open(tmp_path: Path) -> None:
    storage, anna, _, home, store = make_shared_storage(tmp_path)
    context = FakeContext(storage)
    flow: dict[str, object] = {
        "mode": "item_create",
        "message_id": 55,
        "household_id": home.id,
        "store_id": store.id,
        "page": 0,
        "return_view": "store",
    }
    context.user_data["flow"] = flow
    update = type("FakeUpdate", (), {"effective_chat": type("FakeChat", (), {"id": anna.chat_id})()})()

    asyncio.run(handle_flow_text(update, context, storage, anna, flow, "Миг 8табл\nКорм 4,5кг"))

    assert context.user_data["flow"] == flow
    assert {item.name for item in storage.list_items(store.id)} == {"Миг", "Корм"}
    assert "Добавлено: 2" in str(context.bot.edits[-1]["text"])

    asyncio.run(handle_flow_text(update, context, storage, anna, flow, "Миг 2табл\nБумага 4"))

    assert storage.find_item(store.id, "Миг").quantity == 8
    assert storage.find_item(store.id, "Бумага").quantity == 4
    assert "уже есть на складе" in str(context.bot.edits[-1]["text"])


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
