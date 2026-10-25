from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from inventory_bot import (
    Settings,
    archived_item_panel,
    buy_panel,
    build_app,
    handle_flow_text,
    handle_legacy_callback,
    handle_v2_callback,
    has_explicit_amount_unit,
    item_create_prompt,
    item_panel,
    item_settings_panel,
    id_command,
    main_menu,
    parse_amount_list,
    refresh_household_views,
    stores_panel,
    store_panel,
)
from storage import Storage
from panels import pagination_rows, quick_step, step_unit_tag, store_settings_panel


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
    assert any(button.text == "Следующая" for row in keyboard.inline_keyboard for button in row)
    assert any(button.text == "1 / 2" and button.callback_data == "v2:page_info" for row in keyboard.inline_keyboard for button in row)


def test_item_panel_contains_only_context_actions_and_back(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Миг", 8, "табл", 1)

    text, keyboard = item_panel(item, 0)
    labels = [[button.text for button in row] for row in keyboard.inline_keyboard]

    assert "Остаток: 8 табл" in text
    assert labels == [["−1 табл", "+1 табл"], ["Списать", "Добавить"], ["Настройки"], ["К складу"]]


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
    assert all(label.startswith("Купил · ") for label in labels)


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


class FakeQuery:
    def __init__(self, data: str, message_id: int = 55) -> None:
        self.data = data
        self.message = SimpleNamespace(message_id=message_id)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def callback_update(user: object, data: str, message_id: int = 55) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=FakeQuery(data, message_id),
        effective_chat=SimpleNamespace(id=user.chat_id, type="private"),
        effective_user=SimpleNamespace(username=user.username, first_name=user.first_name),
    )


def test_main_stores_screen_exposes_create_and_ordinary_names(tmp_path: Path) -> None:
    storage, anna, _, home, _ = make_shared_storage(tmp_path)
    kitchen = storage.create_store(anna.chat_id, home.id, "Кухня")

    text, keyboard, _ = stores_panel(storage, anna)
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert "Дом: Дом" in text
    assert any(button.text == "Добавить склад" and button.callback_data == "v2:store_create" for button in buttons)
    store_labels = {button.text for button in buttons if button.callback_data.startswith("v2:store:")}
    assert store_labels == {"Основной", "Морозилка", "Холодос", "Кухня"}
    store_rows = [row for row in keyboard.inline_keyboard if row[0].callback_data.startswith("v2:store:")]
    assert all(len(row) == 2 for row in store_rows)
    for row in store_rows:
        assert row[1].text == "⚙️"
        store_id = row[0].callback_data.split(":")[2]
        assert row[1].callback_data == f"v2:store_settings:{store_id}:0"
    assert not any(button.text == "Редактировать" for button in buttons)
    panel_text, _, _ = store_panel(storage, kitchen, 0)
    assert "Дом: Дом · Склад: Кухня" in panel_text


@pytest.mark.parametrize("name", ["Лекарства", "Коридор", "Холодильник"])
def test_storage_creation_asks_for_name_and_returns_to_created_store(tmp_path: Path, name: str) -> None:
    storage, anna, _, home, _ = make_shared_storage(tmp_path)
    context = FakeContext(storage)
    update = callback_update(anna, "v2:store_create")

    asyncio.run(handle_v2_callback(update, context))

    flow = context.user_data["flow"]
    assert flow["mode"] == "store_create"
    assert "store_kind" not in flow
    assert "Дом: Дом" in context.bot.edits[-1]["text"]
    assert "Как назвать новый склад?" in context.bot.edits[-1]["text"]
    assert context.bot.edits[-1]["reply_markup"].inline_keyboard[0][0].text == "Отмена · к складам"
    assert len(context.bot.edits[-1]["reply_markup"].inline_keyboard) == 1

    asyncio.run(handle_flow_text(update, context, storage, anna, flow, name))
    created = storage.current_store(anna.chat_id)
    assert created.name == name
    assert created.household_id == home.id
    assert "flow" not in context.user_data
    assert f"Склад: {name}" in context.bot.edits[-1]["text"]


@pytest.mark.parametrize("old_type", ["cupboard", "fridge", "unknown"])
def test_stale_storage_type_callback_opens_ordinary_name_prompt(tmp_path: Path, old_type: str) -> None:
    storage, anna, _, _, _ = make_shared_storage(tmp_path)
    context = FakeContext(storage)
    update = callback_update(anna, f"v2:store_create_kind:{old_type}")

    asyncio.run(handle_v2_callback(update, context))

    assert context.user_data["flow"]["mode"] == "store_create"
    assert "store_kind" not in context.user_data["flow"]
    assert "Как назвать новый склад?" in context.bot.edits[-1]["text"]
    assert len(context.bot.edits[-1]["reply_markup"].inline_keyboard) == 1


def test_storage_creation_cancel_preserves_inventory(tmp_path: Path) -> None:
    storage, anna, _, home, _ = make_shared_storage(tmp_path)
    original_stores = storage.list_stores(home.id)
    context = FakeContext(storage)
    update = callback_update(anna, "v2:store_create")
    asyncio.run(handle_v2_callback(update, context))
    update.callback_query.data = "v2:flow_cancel"
    asyncio.run(handle_v2_callback(update, context))

    assert "flow" not in context.user_data
    assert storage.list_stores(home.id) == original_stores
    assert "Выберите склад" in context.bot.edits[-1]["text"]


def test_old_storage_create_flow_ignores_obsolete_type(tmp_path: Path) -> None:
    storage, anna, _, home, _ = make_shared_storage(tmp_path)
    context = FakeContext(storage)
    update = callback_update(anna, "")
    flow = {
        "mode": "store_create", "message_id": 55, "household_id": home.id,
        "return_view": "stores", "store_kind": "fridge",
    }
    context.user_data["flow"] = flow

    asyncio.run(handle_flow_text(update, context, storage, anna, flow, "Кладовая"))

    assert storage.current_store(anna.chat_id).name == "Кладовая"
    assert "Склад: Кладовая" in context.bot.edits[-1]["text"]


def test_new_item_form_keeps_partial_names_distinct_and_rejects_bad_minimum(tmp_path: Path) -> None:
    storage, anna, _, home, store = make_shared_storage(tmp_path)
    storage.add_or_update_item(store.id, anna.chat_id, "Рис басмати", 3, "кг", 1)
    context = FakeContext(storage)
    flow = {
        "mode": "item_create", "message_id": 55, "household_id": home.id,
        "store_id": store.id, "return_view": "store",
    }
    context.user_data["flow"] = flow
    update = callback_update(anna, "")

    asyncio.run(handle_flow_text(update, context, storage, anna, flow, "Рис 2кг\nСок 1л мин 500г"))

    assert {item.name for item in storage.list_items(store.id)} == {"Рис", "Рис басмати"}
    assert storage.find_item(store.id, "Рис басмати").quantity == 3
    assert "Минимум" in context.bot.edits[-1]["text"] or "Количество" in context.bot.edits[-1]["text"]
    assert "Добавлено: 1" in context.bot.edits[-1]["text"]


def test_refresh_callbacks_load_changes_from_another_connection(tmp_path: Path) -> None:
    storage, anna, _, home, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Молоко", 2, "л", 1)
    _, store_keyboard, _ = store_panel(storage, store, 0)
    _, buy_keyboard, _ = buy_panel(storage, home, 0)
    store_refresh = next(button.callback_data for row in store_keyboard.inline_keyboard for button in row if button.text == "Обновить")
    buy_refresh = next(button.callback_data for row in buy_keyboard.inline_keyboard for button in row if button.text == "Обновить")
    context = FakeContext(storage)
    external = Storage(storage.path)
    external.change_item_quantity(store.id, anna.chat_id, item.id, -1.5)

    asyncio.run(handle_v2_callback(callback_update(anna, store_refresh), context))
    assert "купить: 1" in context.bot.edits[-1]["text"]
    assert "0.5 л" in context.bot.edits[-1]["reply_markup"].inline_keyboard[0][0].text
    asyncio.run(handle_v2_callback(callback_update(anna, buy_refresh), context))
    assert "Молоко" in context.bot.edits[-1]["reply_markup"].inline_keyboard[0][0].text

    external.change_item_quantity(store.id, anna.chat_id, item.id, 2)
    asyncio.run(handle_v2_callback(callback_update(anna, buy_refresh), context))
    assert "Покупать пока ничего не нужно" in context.bot.edits[-1]["text"]
    assert len(context.bot.edits) == 3


def test_legacy_item_callback_opens_new_panel_and_enforces_membership(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Кофе", 3, "пачка", 1)
    outsider = storage.record_user(3, None, "Guest")
    storage.create_household(outsider.chat_id, "Другой дом")
    context = FakeContext(storage)

    asyncio.run(handle_legacy_callback(callback_update(anna, f"item:open:{item.id}"), context))
    assert "Остаток: 3 пачка" in context.bot.edits[-1]["text"]
    assert "Дом: Дом · Склад: Основной" in context.bot.edits[-1]["text"]
    asyncio.run(handle_legacy_callback(callback_update(outsider, f"item:open:{item.id}"), context))
    assert "Кофе" not in context.bot.edits[-1]["text"]
    assert storage.current_household(outsider.chat_id).name == "Другой дом"


def test_v2_store_callback_enforces_membership(tmp_path: Path) -> None:
    storage, anna, _, home, _ = make_shared_storage(tmp_path)
    outsider = storage.record_user(3, None, "Guest")
    outsider_home = storage.create_household(outsider.chat_id, "Другой дом")
    foreign_store = storage.current_store(outsider.chat_id)
    context = FakeContext(storage)

    asyncio.run(handle_v2_callback(callback_update(anna, f"v2:store:{foreign_store.id}:0"), context))

    assert "Другой дом" not in context.bot.edits[-1]["text"]
    assert storage.current_household(anna.chat_id).id == home.id
    assert outsider_home.id != home.id


def test_id_command_returns_only_own_id_in_private_chat(tmp_path: Path) -> None:
    storage, anna, _, _, _ = make_shared_storage(tmp_path)
    context = FakeContext(storage)
    replies: list[str] = []

    async def reply_text(text: str, **kwargs: object) -> None:
        replies.append(text)

    update = callback_update(anna, "")
    update.message = SimpleNamespace(reply_text=reply_text)
    asyncio.run(id_command(update, context))
    assert replies == ["Ваш ID: 1"]

    update.effective_chat = SimpleNamespace(id=-1001, type="group")
    asyncio.run(id_command(update, context))
    assert replies[-1] == "Отправьте /id в личном чате с ботом."
    assert storage.get_user(-1001) is None


def test_refactored_modules_keep_transport_and_storage_dependencies_separate() -> None:
    root = Path(__file__).parent

    def imports(module: str) -> set[str]:
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    assert not {"telegram", "mcp", "inventory_bot", "storage", "sqlite3"} & imports("amounts.py")
    assert not {"inventory_bot", "storage", "sqlite3", "mcp"} & imports("panels.py")
    assert item_panel.__module__ == "panels"
    assert parse_amount_list.__module__ == "amounts"


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "1e309"])
def test_quantity_parser_rejects_nonfinite_values(text: str) -> None:
    from amounts import parse_float

    with pytest.raises(ValueError, match="конечное число"):
        parse_float(text)


def test_store_settings_link_actions_to_selected_store(tmp_path: Path) -> None:
    storage, anna, _, home, _ = make_shared_storage(tmp_path)
    kitchen = storage.create_store(anna.chat_id, home.id, "Кухня")

    text, keyboard = store_settings_panel(kitchen, home)

    assert "Дом: Дом · Склад: Кухня" in text
    assert [[(button.text, button.callback_data) for button in row] for row in keyboard.inline_keyboard] == [
        [("Переименовать", f"v2:store_rename_pick:{kitchen.id}")],
        [("Удалить склад", f"v2:store_delete_pick:{kitchen.id}")],
        [("К складу", f"v2:store:{kitchen.id}:0")],
        [("К списку складов", "v2:stores_back")],
    ]


def test_store_list_keeps_low_count_next_to_name_and_settings(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    storage.add_or_update_item(store.id, anna.chat_id, "Чай", 1, "пачка", 1)

    _, keyboard, _ = stores_panel(storage, anna)
    row = next(row for row in keyboard.inline_keyboard if row[0].callback_data == f"v2:store:{store.id}:0")

    assert [button.text for button in row] == ["Основной · купить 1", "⚙️"]


@pytest.mark.parametrize(
    ("unit", "expected_step", "label_unit", "tag"),
    [
        ("шт", 1, "шт", None),
        ("таблетки", 1, "табл", None),
        ("капсула", 1, "капс", None),
        ("пачки", 1, "пачка", None),
        ("г", 100, "г", "g"),
        ("гр.", 100, "г", "g"),
        ("кг", 0.5, "кг", "kg"),
        ("kg", 0.5, "кг", "kg"),
        ("мл", 100, "мл", "ml"),
        ("ml", 100, "мл", "ml"),
        ("л", 0.5, "л", "l"),
        ("коробка", 1, "коробка", None),
    ],
)
def test_item_quick_buttons_show_unit_appropriate_steps(
    tmp_path: Path, unit: str, expected_step: float, label_unit: str, tag: str | None
) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, anna.chat_id, "Товар", 500, unit, 1)

    _, keyboard = item_panel(item, 3)
    buttons = keyboard.inline_keyboard[0]
    step_text = f"{expected_step:g}"
    expected_tag = tag or step_unit_tag(label_unit)

    assert quick_step(item) == (expected_step, label_unit)
    assert [button.text for button in buttons] == [f"−{step_text} {label_unit}", f"+{step_text} {label_unit}"]
    assert [button.callback_data for button in buttons] == [
        f"v2:step:{item.id}:-{step_text}:{expected_tag}:3",
        f"v2:step:{item.id}:{step_text}:{expected_tag}:3",
    ]


def test_custom_unit_buttons_have_bounded_callback_and_detect_unit_changes(tmp_path: Path) -> None:
    storage, anna, _, _, store = make_shared_storage(tmp_path)
    unit = "Очень длинная пользовательская единица : " * 4
    item = storage.add_or_update_item(store.id, anna.chat_id, "Товар", 5, unit, 1)

    _, keyboard = item_panel(item, 0)
    old_tag = step_unit_tag(unit)

    assert len(old_tag) == 9 and old_tag.startswith("u")
    assert step_unit_tag("ТАБЛЕТКИ.") == step_unit_tag("табл")
    assert old_tag != step_unit_tag("другая единица")
    assert old_tag != step_unit_tag("шт")
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in keyboard.inline_keyboard[0])


@pytest.mark.parametrize(
    ("page", "labels", "callbacks"),
    [
        (0, ["1 / 3", "Следующая"], ["v2:page_info", "v2:store:7:1"]),
        (1, ["Предыдущая", "2 / 3", "Следующая"], ["v2:store:7:0", "v2:page_info", "v2:store:7:2"]),
        (2, ["Предыдущая", "3 / 3"], ["v2:store:7:1", "v2:page_info"]),
    ],
)
def test_pagination_shows_current_page_between_available_arrows(
    page: int, labels: list[str], callbacks: list[str]
) -> None:
    rows = pagination_rows("v2:store:7", page, 3)

    assert len(rows) == 1
    assert [button.text for button in rows[0]] == labels
    assert [button.callback_data for button in rows[0]] == callbacks
    assert pagination_rows("v2:store:7", 0, 1) == []
