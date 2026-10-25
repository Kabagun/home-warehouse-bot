from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

import mcp_server
from mcp_server import ConfigurationError, MCPSettings, initialize_database
from storage import Storage
from test_migrations import pre_store_database, read_original_columns


SCRIPT = Path(__file__).with_name("mcp_server.py")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def cli(*arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_environment = {
        key: value for key, value in os.environ.items()
        if key not in {"DATABASE_PATH", "MCP_USER_ID", "MCP_CHAT_ID"}
    }
    clean_environment.update(environment or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True,
        cwd=SCRIPT.parent, env=clean_environment, timeout=20,
    )


def snapshot(path: Path) -> tuple[bytes, set[str]]:
    return path.read_bytes(), {entry.name for entry in path.parent.iterdir()}


def test_fresh_cli_init_registers_identity_home_and_initial_stores(tmp_path):
    path = tmp_path / "new" / "inventory.sqlite3"
    result = cli("init", "--database-path", str(path), "--user-id", "1", "--household-name", "  Local   home ")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Inventory database initialized.\n"
    assert result.stderr == ""
    storage = MCPSettings.from_env({"DATABASE_PATH": str(path), "MCP_USER_ID": "1"}).open_storage()
    user = storage.get_user(1)
    assert user is not None and user.username is None and user.first_name is None
    homes = storage.list_households(1)
    assert len(homes) == 1 and homes[0].name == "Local home"
    stores = storage.list_stores(homes[0].id)
    assert [store.name for store in stores] == ["Основной", "Морозилка", "Холодос"]
    assert user.current_household_id == homes[0].id
    assert user.current_store_id == stores[0].id
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT chat_id FROM users").fetchall() == [(1,)]


def test_failed_fresh_initialization_cleans_only_new_database_files(tmp_path, monkeypatch):
    path = tmp_path / "inventory.sqlite3"
    previous_sidecar = path.with_name(path.name + "-wal")
    previous_sidecar.write_bytes(b"Existing unrelated sidecar")

    def fail_initialization(database):
        database.with_name(database.name + "-journal").write_bytes(b"New incomplete journal")
        raise sqlite3.OperationalError("Synthetic initialization failure")

    monkeypatch.setattr(mcp_server, "Storage", fail_initialization)
    with pytest.raises(ConfigurationError, match="Cannot initialize"):
        initialize_database(path, 1, "Home")
    assert not path.exists()
    assert not path.with_name(path.name + "-journal").exists()
    assert previous_sidecar.read_bytes() == b"Existing unrelated sidecar"


def test_failed_initialization_never_cleans_an_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "inventory.sqlite3"
    initialize_database(path, 1, "Home")
    before = snapshot(path)

    def fail_initialization(database):
        raise sqlite3.OperationalError("Synthetic initialization failure")

    monkeypatch.setattr(mcp_server, "Storage", fail_initialization)
    with pytest.raises(ConfigurationError, match="Cannot initialize"):
        initialize_database(path, 1, "Home")
    assert snapshot(path) == before


def test_repeat_init_preserves_profiles_homes_stores_and_inventory(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    storage = initialize_database(path, 1, "First home")
    storage.record_user(1, "synthetic_user", "Synthetic")
    first = storage.list_households(1)[0]
    storage.rename_household(1, first.id, "Renamed home")
    second = storage.create_household(1, "Second home")
    store = storage.create_store(1, second.id, "Pantry")
    item = storage.add_or_update_item(store.id, 1, "Rice", 2.5, "кг", 1)
    storage.archive_item(store.id, 1, item.id)
    before = snapshot(path)
    for _ in range(2):
        result = cli("init", "--database-path", str(path), "--user-id", "1", "--household-name", "Replacement name")
        assert result.returncode == 0, result.stderr
        assert snapshot(path) == before
    assert [home.name for home in storage.list_households(1)] == ["Renamed home", "Second home"]
    assert storage.get_user(1).username == "synthetic_user"
    assert storage.get_item(item.id).quantity == 2.5 and not storage.get_item(item.id).active


def test_init_can_complete_recognized_database_with_no_users_or_user_without_home(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    Storage(path)
    storage = initialize_database(path, -1, "Home")
    assert storage.get_user(-1) is not None
    assert storage.list_households(-1)[0].name == "Home"
    second_path = tmp_path / "registered.sqlite3"
    registered = Storage(second_path)
    registered.record_user(2, "synthetic_user", "Synthetic")
    completed = initialize_database(second_path, 2, "Existing identity home")
    assert completed.get_user(2).username == "synthetic_user"
    assert completed.list_households(2)[0].name == "Existing identity home"


@pytest.mark.parametrize("identity", ["", "0", "1.5", "1_0", "unknown", str(2**63), str(-(2**63) - 1)])
def test_invalid_cli_identity_creates_no_files(tmp_path, identity):
    path = tmp_path / "new" / "inventory.sqlite3"
    result = cli("init", "--database-path", str(path), "--user-id", identity, "--household-name", "Home")
    assert result.returncode == 2 and result.stdout == ""
    assert "signed 64-bit integer" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["", "  \n\t "])
def test_invalid_cli_name_creates_no_files(tmp_path, name):
    path = tmp_path / "new" / "inventory.sqlite3"
    result = cli("init", "--database-path", str(path), "--user-id", "1", "--household-name", name)
    assert result.returncode == 2 and result.stdout == ""
    assert "household name" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("user_id", [True, 0, 1.5, 2**63, -(2**63) - 1])
def test_invalid_api_identity_creates_no_files(tmp_path, user_id):
    with pytest.raises(ConfigurationError):
        initialize_database(tmp_path / "new" / "inventory.sqlite3", user_id, "Home")
    assert list(tmp_path.iterdir()) == []


def test_invalid_api_name_and_path_create_no_files(tmp_path):
    for name in (None, "", "  ", "Invalid\0name"):
        with pytest.raises(ConfigurationError):
            initialize_database(tmp_path / "new" / "inventory.sqlite3", 1, name)
    for path in ("", "\0"):
        with pytest.raises(ConfigurationError):
            initialize_database(path, 1, "Home")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("user_id", [-(2**63), -1, 1, 2**63 - 1])
def test_signed_64_bit_identities_are_supported(tmp_path, user_id):
    path = tmp_path / "inventory.sqlite3"
    storage = initialize_database(path, user_id, "Home")
    assert storage.get_user(user_id).chat_id == user_id
    settings = MCPSettings.from_env({"DATABASE_PATH": str(path), "MCP_USER_ID": str(user_id)})
    assert settings.user_id == user_id
    assert settings.open_storage().list_households(user_id)


@pytest.mark.parametrize("entrypoint", ["init", "serve", "bare"])
def test_unknown_identity_cannot_migrate_or_register_in_existing_database(tmp_path, entrypoint):
    path = tmp_path / "inventory.sqlite3"
    initialize_database(path, 1, "Existing home")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE schema_migrations")
    before = snapshot(path)
    if entrypoint == "init":
        result = cli("init", "--database-path", str(path), "--user-id", "2", "--household-name", "Other home")
    else:
        result = cli(*(["serve"] if entrypoint == "serve" else []), environment={"DATABASE_PATH": str(path), "MCP_USER_ID": "2"})
    assert result.returncode == 2 and result.stdout == ""
    assert "already registered" in result.stderr
    assert snapshot(path) == before


@pytest.mark.parametrize("identity", [None, "", "0", "unknown"])
def test_missing_or_invalid_serve_identity_cannot_migrate_existing_database(tmp_path, identity):
    path = tmp_path / "inventory.sqlite3"
    initialize_database(path, 1, "Home")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE schema_migrations")
    before = snapshot(path)
    environment = {"DATABASE_PATH": str(path)}
    if identity is not None:
        environment["MCP_USER_ID"] = identity
    result = cli(environment=environment)
    assert result.returncode == 2 and result.stdout == ""
    assert snapshot(path) == before


def test_serving_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "new" / "inventory.sqlite3"
    result = cli("serve", environment={"DATABASE_PATH": str(path), "MCP_USER_ID": "1"})
    assert result.returncode == 2 and result.stdout == ""
    assert "existing" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("shape", ["empty", "not_sqlite", "foreign", "missing_column", "invalid_auxiliary"])
def test_unrecognized_database_shape_fails_before_any_migrations(tmp_path, shape):
    path = tmp_path / "inventory.sqlite3"
    if shape == "empty":
        path.touch()
    elif shape == "not_sqlite":
        path.write_bytes(b"Synthetic non-database input")
    elif shape == "foreign":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE users (chat_id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO users VALUES (1)")
    else:
        initialize_database(path, 1, "Home")
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE schema_migrations")
            if shape == "missing_column":
                connection.execute("ALTER TABLE households DROP COLUMN name")
            else:
                connection.execute("CREATE TABLE schema_migrations (name TEXT, applied_at TEXT)")
    before = snapshot(path)
    with pytest.raises(ConfigurationError):
        initialize_database(path, 1, "Home")
    assert snapshot(path) == before
    with pytest.raises(ConfigurationError):
        MCPSettings(path, 1).open_storage()
    assert snapshot(path) == before


@pytest.mark.parametrize("primary,legacy", [("1", "2"), ("1", ""), ("", "1"), ("1", "invalid")])
def test_conflicting_or_invalid_identity_aliases_do_not_change_database(tmp_path, primary, legacy):
    path = tmp_path / "inventory.sqlite3"
    initialize_database(path, 1, "Home")
    before = snapshot(path)
    result = cli(environment={"DATABASE_PATH": str(path), "MCP_USER_ID": primary, "MCP_CHAT_ID": legacy})
    assert result.returncode == 2 and result.stdout == ""
    assert "MCP_USER_ID" in result.stderr
    assert snapshot(path) == before


def test_legacy_configuration_alias_and_equal_aliases_preserve_existing_dataset(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    storage = Storage(path)
    storage.record_user(-101, "synthetic_user", "Synthetic")
    home = storage.create_household(-101, "Existing home")
    store = storage.create_store(-101, home.id, "Existing pantry")
    item = storage.add_or_update_item(store.id, -101, "Rice", 2, "кг", 0.5)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE stores ADD COLUMN kind TEXT NOT NULL DEFAULT 'legacy'")
    before = snapshot(path)
    for identities in ({"MCP_CHAT_ID": "-101"}, {"MCP_USER_ID": "-101", "MCP_CHAT_ID": "-101"}):
        settings = MCPSettings.from_env({"DATABASE_PATH": str(path), **identities})
        assert settings.user_id == -101
        reopened = settings.open_storage()
        assert reopened.get_item(item.id) == item
        assert reopened.list_households(-101) == [home]
        assert snapshot(path) == before


def open_lifecycle(path, entrypoint):
    if entrypoint == "init":
        return initialize_database(path, 1, "Keep the existing home")
    return MCPSettings(path, 1).open_storage()


@pytest.mark.parametrize("entrypoint", ["init", "serve"])
def test_pre_store_database_is_accepted_by_lifecycle(pre_store_database, entrypoint):
    path, columns, original = pre_store_database
    storage = open_lifecycle(path, entrypoint)
    assert read_original_columns(path, columns) == original
    assert storage.list_households(1)[0].name == "Existing home"
    assert storage.get_user(1).username == "synthetic_one"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permission semantics")
def test_new_database_is_private_and_existing_permissions_are_untouched(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    previous_umask = os.umask(0)
    try:
        initialize_database(path, 1, "Home")
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o640)
    initialize_database(path, 1, "Preserved home")
    MCPSettings(path, 1).open_storage()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.anyio
@pytest.mark.parametrize("mode,command,identity_name", [("auto", [], "MCP_USER_ID"), ("legacy", ["serve"], "MCP_CHAT_ID")])
async def test_real_stdio_works_after_operator_initialization(tmp_path, mode, command, identity_name):
    path = tmp_path / "inventory.sqlite3"
    result = cli("init", "--database-path", str(path), "--user-id", "1", "--household-name", "Local home")
    assert result.returncode == 0, result.stderr
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(SCRIPT), *command], cwd=SCRIPT.parent,
        env={"DATABASE_PATH": str(path), identity_name: "1", "PYTHONUTF8": "1"},
    )
    async with Client(parameters, mode=mode) as connected:
        names = {tool.name for tool in (await connected.list_tools()).tools}
        assert len(names) == 12 and "init" not in names and "bootstrap" not in names
        homes = await connected.call_tool("list_households", {})
        assert not homes.is_error
        home = homes.structured_content["households"][0]
        assert home["name"] == "Local home"
        stores = await connected.call_tool("list_stores", {"household_id": home["id"]})
        assert not stores.is_error
        store = stores.structured_content["stores"][0]
        added = await connected.call_tool("add_item", {"store_id": store["id"], "name": "Rice", "quantity": 2, "unit": "kg"})
        assert not added.is_error
        assert added.structured_content["quantity"] == 2
        assert added.structured_content["unit"] == "кг"
