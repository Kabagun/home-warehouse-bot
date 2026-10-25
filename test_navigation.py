from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from inventory_bot import (
    buy_panel,
    handle_flow_text,
    handle_v2_callback,
    item_panel,
    refresh_household_views,
    stores_panel,
)
from test_ui import FakeContext, callback_update, make_shared_storage


@pytest.mark.parametrize(
    ("unit", "initial", "expected"),
    [("табл", 8, 7), ("г", 350, 250), ("мл", 200, 100), ("кг", 1.5, 1), ("л", 0.25, 0)],
)
def test_quick_buttons_apply_the_labelled_amount_and_refresh_shared_views(
    tmp_path: Path, unit: str, initial: float, expected: float
) -> None:
    storage, user, member, home, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Запас", initial, unit)
    storage.set_active_view(user.chat_id, 55, "item", household_id=home.id, store_id=store.id, item_id=item.id)
    storage.set_active_view(member.chat_id, 77, "store", household_id=home.id, store_id=store.id)
    _, keyboard = item_panel(item, 0)
    context = FakeContext(storage)
    update = callback_update(user, keyboard.inline_keyboard[0][0].callback_data)

    asyncio.run(handle_v2_callback(update, context))

    assert storage.get_item(item.id).quantity == expected
    assert update.callback_query.answers == [(f"Остаток: {expected:g} {unit}", False)]
    assert {edit["chat_id"] for edit in context.bot.edits} == {user.chat_id, member.chat_id}


def test_quick_button_preserves_amount_when_unit_was_converted(tmp_path: Path) -> None:
    storage, user, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Горох", 2, "кг")
    _, keyboard = item_panel(item, 0)
    old_button = keyboard.inline_keyboard[0][0].callback_data
    storage.change_item_unit(store.id, user.chat_id, item.id, "г", 2000, 1000)
    context = FakeContext(storage)

    asyncio.run(handle_v2_callback(callback_update(user, old_button), context))

    assert storage.get_item(item.id).quantity == 1500


@pytest.mark.parametrize(("old_unit", "new_unit"), [("кг", "л"), ("пакет", "коробка")])
def test_stale_incompatible_unit_button_refreshes_without_changing_stock(
    tmp_path: Path, old_unit: str, new_unit: str
) -> None:
    storage, user, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Запас", 3, old_unit)
    _, keyboard = item_panel(item, 0)
    old_button = keyboard.inline_keyboard[0][0].callback_data
    storage.change_item_unit(store.id, user.chat_id, item.id, new_unit, 3, 1)
    context = FakeContext(storage)
    update = callback_update(user, old_button)

    asyncio.run(handle_v2_callback(update, context))

    assert storage.get_item(item.id).quantity == 3
    assert update.callback_query.answers[0][1] is True
    assert f"Остаток: 3 {new_unit}" in context.bot.edits[-1]["text"]


def test_quick_buttons_and_store_settings_enforce_household_membership(tmp_path: Path) -> None:
    storage, user, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Приватная позиция", 3, "кг")
    outsider = storage.record_user(999, None, None)
    outsider_home = storage.create_household(outsider.chat_id, "Другой дом")
    _, keyboard = item_panel(item, 0)
    context = FakeContext(storage)
    for data in (keyboard.inline_keyboard[0][0].callback_data, f"v2:store_settings:{store.id}:0"):
        asyncio.run(handle_v2_callback(callback_update(outsider, data), context))
        assert "Приватная позиция" not in context.bot.edits[-1]["text"]
    assert storage.get_item(item.id).quantity == 3
    assert storage.current_household(outsider.chat_id) == outsider_home


@pytest.mark.parametrize("data", ["v2:step:bad:-100:g:0", "v2:step:1:nan:g:0", "v2:delta:1:bad:0"])
def test_invalid_quantity_buttons_do_not_mutate_inventory(tmp_path: Path, data: str) -> None:
    storage, user, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Запас", 3, "г")
    context = FakeContext(storage)
    update = callback_update(user, data.replace("step:1:", f"step:{item.id}:"))

    asyncio.run(handle_v2_callback(update, context))

    assert storage.get_item(item.id).quantity == 3
    assert update.callback_query.answers[-1][1] is True


def test_legacy_quantity_button_still_changes_one_unit(tmp_path: Path) -> None:
    storage, user, _, _, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Запас", 3, "кг")

    asyncio.run(handle_v2_callback(callback_update(user, f"v2:delta:{item.id}:minus:0"), FakeContext(storage)))

    assert storage.get_item(item.id).quantity == 2


def test_store_gear_opens_settings_and_rename_cancel_returns_there(tmp_path: Path) -> None:
    storage, user, _, _, store = make_shared_storage(tmp_path)
    _, keyboard, _ = stores_panel(storage, user)
    gear = keyboard.inline_keyboard[0][1].callback_data
    context = FakeContext(storage)
    update = callback_update(user, gear)
    asyncio.run(handle_v2_callback(update, context))
    settings = context.bot.edits[-1]["reply_markup"]
    assert storage.get_active_view(user.chat_id).view_type == "store_settings"
    rename = next(button.callback_data for row in settings.inline_keyboard for button in row if button.text == "Переименовать")
    update.callback_query.data = rename
    asyncio.run(handle_v2_callback(update, context))
    assert context.user_data["flow"]["return_view"] == "store_settings"
    update.callback_query.data = "v2:flow_cancel"
    asyncio.run(handle_v2_callback(update, context))

    assert "flow" not in context.user_data
    assert storage.get_store(store.id).name == store.name
    assert storage.get_active_view(user.chat_id).view_type == "store_settings"


def test_rename_returns_to_settings_and_delete_requires_confirmation(tmp_path: Path) -> None:
    storage, user, _, home, store = make_shared_storage(tmp_path)
    context = FakeContext(storage)
    update = callback_update(user, f"v2:store_rename_pick:{store.id}")
    asyncio.run(handle_v2_callback(update, context))
    asyncio.run(handle_flow_text(update, context, storage, user, context.user_data["flow"], "Кладовая"))
    assert storage.get_store(store.id).name == "Кладовая"
    assert storage.get_active_view(user.chat_id).view_type == "store_settings"
    asyncio.run(refresh_household_views(context, home.id))
    assert "Кладовая" in context.bot.edits[-1]["text"]
    assert storage.get_active_view(user.chat_id).view_type == "store_settings"

    update.callback_query.data = f"v2:store_delete_pick:{store.id}"
    asyncio.run(handle_v2_callback(update, context))
    assert storage.get_store(store.id) is not None
    confirmation = context.bot.edits[-1]["reply_markup"].inline_keyboard[0]
    assert confirmation[1].callback_data == f"v2:store_settings:{store.id}:0"
    update.callback_query.data = confirmation[1].callback_data
    asyncio.run(handle_v2_callback(update, context))
    assert storage.get_store(store.id) is not None
    assert storage.get_active_view(user.chat_id).view_type == "store_settings"


def test_page_indicator_only_acknowledges_without_changing_screen(tmp_path: Path) -> None:
    storage, user, _, home, store = make_shared_storage(tmp_path)
    storage.set_active_view(user.chat_id, 55, "store", household_id=home.id, store_id=store.id)
    before = storage.get_active_view(user.chat_id)
    context = FakeContext(storage)
    update = callback_update(user, "v2:page_info")

    asyncio.run(handle_v2_callback(update, context))

    assert update.callback_query.answers == [("", False)]
    assert context.bot.edits == []
    assert storage.get_active_view(user.chat_id) == before


def test_bought_button_asks_for_actual_quantity_and_returns_to_shopping(tmp_path: Path) -> None:
    storage, user, _, home, store = make_shared_storage(tmp_path)
    item = storage.add_or_update_item(store.id, user.chat_id, "Горох", 0.25, "кг", 0.5)
    _, keyboard, _ = buy_panel(storage, home, 0)
    context = FakeContext(storage)
    update = callback_update(user, keyboard.inline_keyboard[0][0].callback_data)

    asyncio.run(handle_v2_callback(update, context))

    assert storage.get_item(item.id).quantity == 0.25
    assert context.user_data["flow"]["mode"] == "buy_adjust"
    asyncio.run(handle_flow_text(update, context, storage, user, context.user_data["flow"], "500г"))
    assert storage.get_item(item.id).quantity == 0.75
    assert "Покупать пока ничего не нужно" in context.bot.edits[-1]["text"]
    assert storage.get_active_view(user.chat_id).view_type == "buy"
