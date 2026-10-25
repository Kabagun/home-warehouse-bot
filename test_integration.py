from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from mcp import Client

from inventory_bot import buy_panel, quick_use, store_panel
from mcp_server import build_server
from storage import Storage


def test_agent_and_telegram_share_named_stores_and_unit_rules(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "shared.sqlite3"
        agent_storage = Storage(database)
        user = agent_storage.record_user(101, None, None)
        home = agent_storage.create_household(user.chat_id, "Тестовый дом")
        kitchen = agent_storage.create_store(user.chat_id, home.id, "Кухня")
        agent_storage.add_or_update_item(kitchen.id, user.chat_id, "Горох", 9, "кг", 1)

        async with Client(build_server(agent_storage, user.chat_id)) as client:
            created = await client.call_tool(
                "create_store", {"household_id": home.id, "name": "Морозилка"}
            )
            assert not created.is_error
            freezer_id = created.structured_content["id"]
            added = await client.call_tool(
                "add_item",
                {"store_id": freezer_id, "name": "Горох", "quantity": 1, "unit": "кг", "min_quantity": 0.5},
            )
            assert not added.is_error
            item_id = added.structured_content["id"]

            # A separately opened connection represents the already running Telegram bot.
            telegram_storage = Storage(database)
            freezer = telegram_storage.get_store(freezer_id)
            text, _, _ = store_panel(telegram_storage, freezer, 0)
            assert "Морозилка" in text
            replies: list[str] = []

            async def reply_text(text: str, **kwargs: object) -> None:
                replies.append(text)

            update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"storage": telegram_storage}),
                user_data={},
            )
            await quick_use(update, context, telegram_storage, user, freezer, "Горох 750г")
            assert replies

            listed = await client.call_tool("list_items", {"store_id": freezer_id})
            assert not listed.is_error
            assert listed.structured_content["items"][0]["quantity"] == 0.25
            assert listed.structured_content["items"][0]["is_low"] is True
            assert telegram_storage.find_item(kitchen.id, "Горох").quantity == 9
            _, keyboard, _ = buy_panel(telegram_storage, home, 0)
            assert any("Морозилка · Горох" in button.text for row in keyboard.inline_keyboard for button in row)

            restocked = await client.call_tool(
                "change_item_quantity", {"item_id": item_id, "delta": 500, "unit": "г"}
            )
            assert not restocked.is_error
            assert restocked.structured_content["quantity"] == 0.75
            assert telegram_storage.find_item(freezer_id, "Горох").quantity == 0.75
            _, keyboard, _ = buy_panel(telegram_storage, home, 0)
            assert all("Горох" not in button.text for row in keyboard.inline_keyboard for button in row)

    asyncio.run(scenario())
