from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

from inventory_service import InventoryService
from mcp_server import ConfigurationError, MCPSettings, build_server
from storage import Storage


PROFILE_ID = "prf_" + "a" * 43


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def inventory(tmp_path):
    storage = Storage(tmp_path / "inventory.sqlite3")
    storage.record_user(101, "synthetic_user", "Synthetic")
    home = storage.create_household(101, "Test home")
    store = storage.list_stores(home.id)[0]
    storage.record_user(202, "other_user", "Other")
    other_home = storage.create_household(202, "Other home")
    other_store = storage.list_stores(other_home.id)[0]
    other_item = storage.add_or_update_item(other_store.id, 202, "Other rice", 4, "кг", 1)
    return storage, home, store, other_home, other_store, other_item


@pytest.fixture
async def client(inventory):
    storage, *_ = inventory
    async with Client(build_server(storage, 101), raise_exceptions=True) as connected:
        yield connected


async def call(client, tool_name, **arguments):
    result = await client.call_tool(tool_name, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def test_configuration_requires_explicit_database_and_existing_identity(tmp_path):
    with pytest.raises(ConfigurationError, match="DATABASE_PATH"):
        MCPSettings.from_env({"MCP_USER_ID": "101"})
    for identity in ("", "not-an-id", "1.5", "1_0", "0", str(2**63), str(-(2**63) - 1)):
        with pytest.raises(ConfigurationError, match="MCP_USER_ID"):
            MCPSettings.from_env({"DATABASE_PATH": str(tmp_path / "missing.sqlite3"), "MCP_USER_ID": identity})
    settings = MCPSettings.from_env({"DATABASE_PATH": str(tmp_path / "missing.sqlite3"), "MCP_USER_ID": "101"})
    with pytest.raises(ConfigurationError, match="existing"):
        settings.open_storage()
    assert not settings.database_path.exists()


def test_configuration_rejects_empty_and_foreign_database_without_initializing(tmp_path):
    database = tmp_path / "empty.sqlite3"
    database.touch()
    with pytest.raises(ConfigurationError, match="database"):
        MCPSettings(database, 101).open_storage()
    assert database.stat().st_size == 0
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE users (chat_id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO users VALUES (101)")
    with pytest.raises(ConfigurationError, match="initialized"):
        MCPSettings(database, 101).open_storage()
    with sqlite3.connect(database) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == [("users",)]


def test_configuration_rejects_unknown_user_and_opens_real_inventory(inventory):
    storage, home, *_ = inventory
    with pytest.raises(ConfigurationError, match="already registered"):
        MCPSettings(storage.path, 303).open_storage()
    with pytest.raises(ValueError, match="зарегистрирован"):
        build_server(storage, 303)
    reopened = MCPSettings(storage.path, 101).open_storage()
    assert reopened.list_households(101)[0].id == home.id


@pytest.mark.anyio
async def test_tools_have_structured_schemas_annotations_and_no_identity_argument(client):
    tools = (await client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {
        "list_households", "create_household", "list_stores", "create_store", "list_items", "add_item",
        "change_item_quantity", "set_item_quantity", "set_item_minimum", "archive_item", "restore_item", "shopping_list",
    }
    for tool in tools:
        assert tool.output_schema
        assert "chat_id" not in tool.input_schema.get("properties", {})
        assert "user_id" not in tool.input_schema.get("properties", {})
        assert "database_path" not in tool.input_schema.get("properties", {})
        assert tool.input_schema["additionalProperties"] is False
        assert tool.annotations.open_world_hint is False
    assert by_name["list_items"].annotations.read_only_hint is True
    assert by_name["add_item"].annotations.idempotent_hint is False
    assert by_name["change_item_quantity"].annotations.destructive_hint is True
    assert by_name["set_item_quantity"].annotations.idempotent_hint is True
    assert by_name["archive_item"].annotations.destructive_hint is True
    assert by_name["restore_item"].annotations.idempotent_hint is True
    assert set(by_name["create_store"].input_schema["properties"]) == {"household_id", "name"}
    assert set(by_name["create_store"].output_schema["properties"]) == {"id", "household_id", "name"}


@pytest.mark.anyio
async def test_profile_tool_is_flat_strict_readonly_and_uses_configured_identity(inventory):
    storage, *_ = inventory
    before = storage.path.read_bytes()
    async with Client(build_server(storage, 101, profile_id=PROFILE_ID)) as connected:
        tools = (await connected.list_tools()).tools
        assert len(tools) == 13
        profile_tool = next(tool for tool in tools if tool.name == "get_profile")
        assert profile_tool.meta == {"openai/profile": True, "securitySchemes": [{"type": "oauth2", "scopes": ["inventory"]}]}
        assert profile_tool.input_schema["properties"] == {}
        assert profile_tool.input_schema["additionalProperties"] is False
        assert profile_tool.output_schema["additionalProperties"] is False
        assert set(profile_tool.output_schema["properties"]) == {"id", "name", "nickname"}
        assert profile_tool.output_schema["properties"]["id"]["minLength"] == 1
        assert profile_tool.output_schema["properties"]["id"]["pattern"] == r"\S"
        assert profile_tool.annotations.read_only_hint is True
        assert profile_tool.annotations.destructive_hint is False
        assert profile_tool.annotations.open_world_hint is False
        expected = {"id": PROFILE_ID, "name": "Склад", "nickname": "Дом"}
        for _ in range(2):
            result = await connected.call_tool("get_profile", {})
            assert not result.is_error and result.structured_content == expected
            assert json.loads(result.content[0].text) == expected
        assert (await connected.call_tool("get_profile", {"user_id": 202})).is_error
    assert storage.path.read_bytes() == before
    async with Client(build_server(storage, 101, profile_id=PROFILE_ID)) as restarted:
        assert await call(restarted, "get_profile") == expected


@pytest.mark.anyio
async def test_profile_tool_rejects_missing_configured_principal(inventory):
    storage, *_ = inventory
    server = build_server(storage, 101, profile_id=PROFILE_ID)
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DELETE FROM users WHERE chat_id = 101")
    async with Client(server) as connected:
        result = await connected.call_tool("get_profile", {})
        assert result.is_error and not result.structured_content


@pytest.mark.parametrize("profile_id", ["", " ", "synthetic@example.test", 101])
def test_profile_tool_requires_a_valid_persisted_profile_identity(inventory, profile_id):
    with pytest.raises(ConfigurationError, match="profile ID"):
        build_server(inventory[0], 101, profile_id=profile_id)


@pytest.mark.anyio
async def test_home_and_store_tools_only_return_inventory_context(client, inventory):
    storage, home, *_ = inventory
    homes = await call(client, "list_households")
    assert homes == {"households": [{"id": home.id, "name": "Test home"}]}
    assert "invite" not in json.dumps(homes)
    assert "synthetic_user" not in json.dumps(homes)
    new_home = await call(client, "create_household", name="  Second   home ")
    assert new_home["name"] == "Second home"
    initial_stores = await call(client, "list_stores", household_id=new_home["id"])
    assert {s["name"] for s in initial_stores["stores"]} == {"Основной", "Морозилка", "Холодос"}
    freezer = await call(client, "create_store", household_id=new_home["id"], name="Морозилка")
    assert set(freezer) == {"id", "household_id", "name"}
    assert freezer == await call(client, "create_store", household_id=new_home["id"], name=" морозилка ")
    ordinary_stores = [
        await call(client, "create_store", household_id=new_home["id"], name=name)
        for name in ("Холодос", "Лекарства", "Кухня", "Коридор")
    ]
    assert len({s["id"] for s in [freezer, *ordinary_stores]}) == 5
    stores = await call(client, "list_stores", household_id=new_home["id"])
    assert {s["name"] for s in stores["stores"]} == {"Основной", "Морозилка", "Холодос", "Лекарства", "Кухня", "Коридор"}
    assert all(set(s) == {"id", "household_id", "name"} for s in stores["stores"])
    assert storage.get_user(101).current_store_id == ordinary_stores[-1]["id"]


@pytest.mark.anyio
async def test_add_change_set_minimum_and_shopping_use_same_units(client, inventory):
    storage, home, store, *_ = inventory
    item = await call(client, "add_item", store_id=store.id, name="Rice", quantity=2, unit="kg", min_quantity=500, min_unit="g")
    assert (item["quantity"], item["unit"], item["min_quantity"]) == (2, "кг", 0.5)
    item_id = item["id"]
    item = await call(client, "add_item", store_id=store.id, name="rice", quantity=250, unit="г")
    assert item["quantity"] == 2.25
    assert item["unit"] == "кг"
    item = await call(client, "add_item", store_id=store.id, name="Rice", quantity=1)
    assert item["quantity"] == 3.25
    assert item["min_quantity"] == 0.5
    item = await call(client, "change_item_quantity", item_id=item_id, delta=-500, unit="г")
    assert item["quantity"] == 2.75
    item = await call(client, "set_item_quantity", item_id=item_id, quantity=200, unit="г")
    assert item["quantity"] == 0.2
    item = await call(client, "set_item_minimum", item_id=item_id, min_quantity=250, unit="г")
    assert item["min_quantity"] == 0.25 and item["is_low"]
    shopping = await call(client, "shopping_list", household_id=home.id)
    assert shopping["stores"][0]["items"][0]["id"] == item_id
    assert storage.get_item(item_id).quantity == 0.2
    item = await call(client, "change_item_quantity", item_id=item_id, delta=-20)
    assert item["quantity"] == 0


@pytest.mark.anyio
async def test_archived_items_require_restore_and_keep_stock(client, inventory):
    _, home, store, *_ = inventory
    item = await call(client, "add_item", store_id=store.id, name="Milk", quantity=0.5, unit="л", min_quantity=1)
    item_id = item["id"]
    archived = await call(client, "archive_item", item_id=item_id)
    assert archived["active"] is False and archived["quantity"] == 0.5
    assert (await call(client, "archive_item", item_id=item_id))["active"] is False
    assert (await call(client, "list_items", store_id=store.id))["items"] == []
    assert len((await call(client, "list_items", store_id=store.id, include_archived=True))["items"]) == 1
    assert (await call(client, "shopping_list", household_id=home.id))["stores"] == []
    for tool, args in (
        ("change_item_quantity", {"delta": 1}),
        ("set_item_quantity", {"quantity": 1}),
        ("set_item_minimum", {"min_quantity": 1}),
    ):
        result = await client.call_tool(tool, {"item_id": item_id, **args})
        assert result.is_error
    restored = await call(client, "restore_item", item_id=item_id)
    assert restored["active"] is True and restored["quantity"] == 0.5
    assert restored["min_quantity"] == 1
    assert (await call(client, "restore_item", item_id=item_id))["active"] is True
    await call(client, "archive_item", item_id=item_id)
    readded = await call(client, "add_item", store_id=store.id, name="Milk", quantity=2, unit="л")
    assert readded["id"] == item_id and readded["quantity"] == 2 and readded["active"] is True


@pytest.mark.anyio
async def test_every_tool_scopes_foreign_ids_to_membership(client, inventory):
    storage, _, _, other_home, other_store, other_item = inventory
    original_stores = storage.list_stores(other_home.id)
    attempts = (
        ("list_stores", {"household_id": other_home.id}),
        ("create_store", {"household_id": other_home.id, "name": "Intruder"}),
        ("shopping_list", {"household_id": other_home.id}),
        ("list_items", {"store_id": other_store.id, "include_archived": True}),
        ("add_item", {"store_id": other_store.id, "name": "Intruder", "quantity": 1}),
        ("change_item_quantity", {"item_id": other_item.id, "delta": -1}),
        ("set_item_quantity", {"item_id": other_item.id, "quantity": 0}),
        ("set_item_minimum", {"item_id": other_item.id, "min_quantity": 5}),
        ("archive_item", {"item_id": other_item.id}),
        ("restore_item", {"item_id": other_item.id}),
    )
    for name, arguments in attempts:
        result = await client.call_tool(name, arguments)
        assert result.is_error, (name, result)
        assert "Other rice" not in str(result.content)
        assert "Other home" not in str(result.content)
    assert storage.get_item(other_item.id) == other_item
    assert storage.list_stores(other_home.id) == original_stores


@pytest.mark.anyio
async def test_identity_cannot_be_overridden_and_revoked_membership_is_checked(client, inventory):
    storage, home, store, _, other_store, _ = inventory
    result = await client.call_tool("list_items", {"store_id": other_store.id, "chat_id": 202})
    assert result.is_error
    result = await client.call_tool("list_items", {"store_id": other_store.id, "user_id": 202})
    assert result.is_error
    item = await call(client, "add_item", store_id=store.id, name="Rice", quantity=2)
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DELETE FROM household_members WHERE household_id=? AND chat_id=?", (home.id, 101))
    assert (await call(client, "list_households"))["households"] == []
    for tool, arguments in (
        ("list_items", {"store_id": store.id}),
        ("archive_item", {"item_id": item["id"]}),
    ):
        assert (await client.call_tool(tool, arguments)).is_error
    assert storage.get_item(item["id"]).active


@pytest.mark.anyio
async def test_invalid_quantities_and_units_leave_stock_unchanged(client, inventory):
    storage, _, store, *_ = inventory
    item = await call(client, "add_item", store_id=store.id, name="Rice", quantity=1, unit="кг")
    item_id = item["id"]
    for tool, field, base in (
        ("add_item", "quantity", {"store_id": store.id, "name": "Rice"}),
        ("set_item_quantity", "quantity", {"item_id": item_id}),
        ("set_item_minimum", "min_quantity", {"item_id": item_id}),
        ("change_item_quantity", "delta", {"item_id": item_id}),
    ):
        invalid = (True, float("inf"), float("-inf"), float("nan"))
        if tool != "change_item_quantity":
            invalid += (-1,)
        for value in invalid:
            result = await client.call_tool(tool, {**base, field: value})
            assert result.is_error, (tool, value, result)
    for tool, args in (
        ("add_item", {"store_id": store.id, "name": "Rice", "quantity": 1, "unit": "л"}),
        ("add_item", {"store_id": store.id, "name": "Rice", "min_quantity": 1, "min_unit": "л"}),
        ("add_item", {"store_id": store.id, "name": "  "}),
        ("add_item", {"store_id": store.id, "name": "Rice", "unit": " "}),
        ("set_item_quantity", {"item_id": item_id, "quantity": 2, "unit": "л"}),
        ("change_item_quantity", {"item_id": item_id, "delta": 1, "unit": "л"}),
        ("set_item_minimum", {"item_id": item_id, "min_quantity": 1, "unit": "л"}),
    ):
        assert (await client.call_tool(tool, args)).is_error
    assert storage.get_item(item_id).quantity == 1
    assert storage.get_item(item_id).min_quantity == 1


def test_service_rejects_conversion_and_addition_overflow(inventory):
    storage, _, store, *_ = inventory
    service = InventoryService(storage, 101)
    item = service.add_item(store.id, "Huge", 1e308, "г")
    with pytest.raises(ValueError, match="конечное"):
        service.add_item(store.id, "Huge", 1e308, "г")
    with pytest.raises(ValueError, match="конечное"):
        service.change_quantity(item.id, 1e308, "кг")
    assert storage.get_item(item.id).quantity == 1e308


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_initialization_discovery_and_call(inventory, mode):
    storage, home, store, *_ = inventory
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("mcp_server.py"))],
        cwd=Path(__file__).parent,
        env={"DATABASE_PATH": str(storage.path), "MCP_USER_ID": "101", "PYTHONUTF8": "1"},
    )
    async with Client(parameters, mode=mode) as connected:
        names = {tool.name for tool in (await connected.list_tools()).tools}
        assert "add_item" in names and "shopping_list" in names
        homes = await call(connected, "list_households")
        assert homes["households"][0]["id"] == home.id
        named_store = await call(connected, "create_store", household_id=home.id, name="Морозилка")
        assert set(named_store) == {"id", "household_id", "name"}
        added = await call(connected, "add_item", store_id=named_store["id"], name="Pasta", quantity=3)
        assert added["quantity"] == 3
        items = await call(connected, "list_items", store_id=named_store["id"])
        assert items["items"][0]["name"] == "Pasta"
        override = await connected.call_tool("list_items", {"store_id": store.id, "chat_id": 202})
        assert override.is_error
    assert storage.get_item(added["id"]).quantity == 3


def test_cli_help_and_configuration_errors_and_transport_independent_import(tmp_path):
    script = Path(__file__).with_name("mcp_server.py")
    environment = {key: value for key, value in os.environ.items() if key not in {"MCP_USER_ID", "MCP_CHAT_ID", "DATABASE_PATH", "TELEGRAM_BOT_TOKEN"}}
    help_result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, env=environment, timeout=20)
    assert help_result.returncode == 0
    assert all(name in help_result.stdout for name in ("MCP_USER_ID", "MCP_CHAT_ID", "DATABASE_PATH", "init", "serve"))
    failure = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=environment, timeout=20)
    assert failure.returncode == 2
    assert failure.stdout == ""
    assert "MCP configuration error" in failure.stderr
    imports = subprocess.run(
        [sys.executable, "-c", "import sys; import mcp_server; assert 'telegram' not in sys.modules"],
        capture_output=True, text=True, cwd=script.parent, env=environment, timeout=20,
    )
    assert imports.returncode == 0, imports.stderr
