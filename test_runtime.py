from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import app_runtime
from app_runtime import RuntimeSettings, ensure_release_backup, main, prepare_runtime
from mcp_server import ConfigurationError
from storage import Storage


# Synthetic test credentials; they do not authenticate against a real service.
TOKEN = "synthetic-runtime-token-1234567890abcdef"
OTHER_TOKEN = "different-synthetic-token-1234567890abcdef"


def private_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def inventory(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "home_warehouse_bot.sqlite3"
    storage = Storage(database)
    storage.record_user(101, "synthetic", "Synthetic")
    home = storage.create_household(101, "Synthetic home")
    store = storage.list_stores(home.id)[0]
    storage.add_or_update_item(store.id, 101, "Rice", 2, "кг", 0.5)
    return storage, data_dir


def environment(data_dir: Path, **updates: str) -> dict[str, str]:
    return {"APP_DATA_DIR": str(data_dir), "MCP_RELEASE_ID": "synthetic-release", **updates}


def settings_for(data_dir: Path, bootstrap: Path | None = None, **updates: str) -> RuntimeSettings:
    if bootstrap is not None:
        updates["MCP_BOOTSTRAP_FILE"] = str(bootstrap)
    return RuntimeSettings.from_env(environment(data_dir, **updates))


def fingerprint(database: Path) -> str:
    with app_runtime.closing(app_runtime._open_readonly(database)) as connection:
        return app_runtime._database_fingerprint(connection)


def test_unique_registered_member_owner_is_selected_without_changing_profile(inventory, tmp_path):
    storage, data_dir = inventory
    # A registered non-owner must not be selected just because it sorts first.
    storage.record_user(-202, "another_synthetic", "Another")
    profile = storage.get_user(101)
    before = fingerprint(storage.path)
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "select_single_owner": True})
    runtime = prepare_runtime(settings_for(data_dir, bootstrap))
    assert runtime.user_id == 101
    assert runtime.auth_token == TOKEN
    assert runtime.storage.get_user(101) == profile
    assert fingerprint(storage.path) == before
    saved = json.loads((data_dir / "mcp-settings.json").read_text())
    assert saved == {"auth_token": TOKEN, "user_id": 101}
    assert str(101) not in repr(runtime) and TOKEN not in repr(runtime)


def test_explicit_principal_does_not_require_single_owner(inventory, tmp_path):
    storage, data_dir = inventory
    storage.record_user(-202, "second_synthetic", "Second")
    other_home = storage.create_household(-202, "Second home")
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": -202})
    runtime = prepare_runtime(settings_for(data_dir, bootstrap))
    assert runtime.user_id == -202
    assert runtime.storage.list_households(runtime.user_id)[0].id == other_home.id


@pytest.mark.parametrize("ownership", ["ambiguous", "none", "unregistered", "not-member"])
def test_owner_selection_rejects_missing_or_ambiguous_identity_without_mutation(inventory, tmp_path, ownership):
    storage, data_dir = inventory
    if ownership == "ambiguous":
        storage.record_user(202, "second_synthetic", "Second")
        storage.create_household(202, "Second home")
    else:
        with sqlite3.connect(storage.path) as connection:
            if ownership == "none":
                connection.execute("DELETE FROM households")
            elif ownership == "unregistered":
                connection.execute("DELETE FROM users")
            else:
                connection.execute("DELETE FROM household_members")
    # Auxiliary table creation would show that Storage had been called too soon.
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DROP TABLE active_views")
    before = storage.path.read_bytes()
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "select_single_owner": True})
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir, bootstrap))
    assert storage.path.read_bytes() == before
    assert not (data_dir / "mcp-settings.json").exists()
    assert not (data_dir / "backups").exists()


def test_multiple_homes_owned_by_same_user_are_unambiguous(inventory, tmp_path):
    storage, data_dir = inventory
    storage.create_household(101, "Another home")
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "select_single_owner": True})
    assert prepare_runtime(settings_for(data_dir, bootstrap)).user_id == 101


@pytest.mark.parametrize("identity", [0, True, "101", 303, 2**63, -(2**63) - 1])
def test_invalid_explicit_identity_never_runs_schema_migrations(inventory, tmp_path, identity):
    storage, data_dir = inventory
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DROP TABLE active_views")
    before = storage.path.read_bytes()
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": identity})
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir, bootstrap))
    assert storage.path.read_bytes() == before
    assert not (data_dir / "backups").exists()


def test_backup_matches_original_source_is_readonly_and_target_is_preserved(inventory):
    storage, data_dir = inventory
    settings = settings_for(data_dir)
    before_bytes = storage.path.read_bytes()
    before_stat = storage.path.stat()
    expected = fingerprint(storage.path)
    with app_runtime.closing(app_runtime._open_readonly(storage.path)) as source:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            source.execute("CREATE TABLE forbidden(value)")
    backup = ensure_release_backup(settings)
    assert fingerprint(backup) == expected
    assert storage.path.read_bytes() == before_bytes
    assert storage.path.stat().st_ino == before_stat.st_ino
    assert stat.S_IMODE(storage.path.stat().st_mode) == stat.S_IMODE(before_stat.st_mode)
    original_backup = backup.read_bytes()
    original_inode = backup.stat().st_ino
    storage.add_or_update_item(storage.list_stores(storage.list_households(101)[0].id)[0].id, 101, "Beans", 1, "кг", 0)
    assert ensure_release_backup(settings) == backup
    assert backup.read_bytes() == original_backup
    assert backup.stat().st_ino == original_inode


def test_release_snapshot_precedes_additive_migrations(inventory, tmp_path):
    storage, data_dir = inventory
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DROP TABLE active_views")
    before = fingerprint(storage.path)
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})
    runtime = prepare_runtime(settings_for(data_dir, bootstrap))
    backup = data_dir / "backups" / "before-mcp-synthetic-release.sqlite3"
    assert fingerprint(backup) == before
    with sqlite3.connect(runtime.storage.path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='active_views'").fetchone() == (1,)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='active_views'").fetchone() is None


def test_backup_includes_committed_wal_contents(inventory):
    storage, data_dir = inventory
    with sqlite3.connect(storage.path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE inventory_items SET quantity=7.25")
        writer.commit()
        assert Path(str(storage.path) + "-wal").stat().st_size > 0
        before = fingerprint(storage.path)
        backup = ensure_release_backup(settings_for(data_dir))
        assert fingerprint(backup) == before
        with sqlite3.connect(backup) as snapshot:
            assert snapshot.execute("SELECT quantity FROM inventory_items").fetchall() == [(7.25,)]


def test_unverified_mismatching_snapshot_is_preserved(inventory):
    storage, data_dir = inventory
    settings = settings_for(data_dir)
    backup = ensure_release_backup(settings)
    (backup.parent / "before-mcp-synthetic-release.receipt.json").unlink()
    original = backup.read_bytes()
    with sqlite3.connect(storage.path) as connection:
        connection.execute("UPDATE inventory_items SET quantity=8")
    with pytest.raises(ConfigurationError):
        ensure_release_backup(settings)
    assert backup.read_bytes() == original


def test_backup_failure_never_overwrites_existing_target(inventory):
    storage, data_dir = inventory
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "before-mcp-synthetic-release.sqlite3"
    backup.write_bytes(b"existing synthetic operator file")
    backup.chmod(0o600)
    before = storage.path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        ensure_release_backup(settings_for(data_dir))
    assert backup.read_bytes() == b"existing synthetic operator file"
    assert storage.path.read_bytes() == before


def test_missing_or_modified_recorded_backup_is_rejected(inventory):
    _, data_dir = inventory
    settings = settings_for(data_dir)
    backup = ensure_release_backup(settings)
    contents = backup.read_bytes()
    backup.unlink()
    with pytest.raises(ConfigurationError):
        ensure_release_backup(settings)
    backup.write_bytes(contents + b"changed")
    backup.chmod(0o600)
    with pytest.raises(ConfigurationError):
        ensure_release_backup(settings)
    assert backup.read_bytes() == contents + b"changed"


def test_release_receipt_cannot_be_reused_for_a_different_database(inventory):
    storage, data_dir = inventory
    settings = settings_for(data_dir)
    backup = ensure_release_backup(settings)
    second_database = data_dir / "second.sqlite3"
    second_database.write_bytes(storage.path.read_bytes())
    with pytest.raises(ConfigurationError):
        ensure_release_backup(settings_for(data_dir, DATABASE_PATH=second_database.name))
    assert backup.exists()


def test_existing_verified_backup_without_receipt_can_be_recovered(inventory):
    _, data_dir = inventory
    settings = settings_for(data_dir)
    backup = ensure_release_backup(settings)
    (backup.parent / "before-mcp-synthetic-release.receipt.json").unlink()
    before = backup.stat().st_ino
    assert ensure_release_backup(settings) == backup
    assert backup.stat().st_ino == before


def test_saved_configuration_remains_stable_across_restarts(inventory, tmp_path):
    _, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "select_single_owner": True})
    first = prepare_runtime(settings_for(data_dir, bootstrap))
    path = data_dir / "mcp-settings.json"
    original = path.read_bytes()
    original_inode = path.stat().st_ino
    bootstrap.unlink()
    second = prepare_runtime(settings_for(data_dir))
    assert (second.user_id, second.auth_token) == (first.user_id, first.auth_token)
    assert path.read_bytes() == original
    assert path.stat().st_ino == original_inode


def test_second_bootstrap_updates_public_url_and_env_override_persists(inventory, tmp_path):
    _, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})
    prepare_runtime(settings_for(data_dir, bootstrap))
    private_json(bootstrap, {"public_url": "https://synthetic.example/"})
    runtime = prepare_runtime(settings_for(data_dir, bootstrap))
    assert runtime.public_url == "https://synthetic.example"
    runtime = prepare_runtime(settings_for(data_dir, MCP_PUBLIC_URL="https://override.example"))
    assert runtime.public_url == "https://override.example"
    saved = json.loads((data_dir / "mcp-settings.json").read_text())
    assert saved == {"auth_token": TOKEN, "user_id": 101, "public_url": "https://override.example"}
    assert prepare_runtime(settings_for(data_dir)).public_url == "https://override.example"


@pytest.mark.parametrize("changed", [{"auth_token": OTHER_TOKEN}, {"user_id": 202}])
def test_bootstrap_cannot_rotate_saved_token_or_identity(inventory, tmp_path, changed):
    storage, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})
    prepare_runtime(settings_for(data_dir, bootstrap))
    saved = (data_dir / "mcp-settings.json").read_bytes()
    original = storage.path.read_bytes()
    private_json(bootstrap, changed)
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir, bootstrap))
    assert (data_dir / "mcp-settings.json").read_bytes() == saved
    assert storage.path.read_bytes() == original


@pytest.mark.parametrize("token", [None, "", "short", "x" * 31, "x" * 32 + "\n", "х" * 32, "x" * 32 + " space"])
def test_invalid_tokens_cannot_mutate_inventory(inventory, tmp_path, token):
    storage, data_dir = inventory
    original = storage.path.read_bytes()
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": token, "user_id": 101})
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir, bootstrap))
    assert storage.path.read_bytes() == original
    assert not (data_dir / "mcp-settings.json").exists()


@pytest.mark.parametrize("overrides", [
    {"APP_DATA_DIR": ""}, {"MCP_RELEASE_ID": ""}, {"MCP_RELEASE_ID": "../escape"},
    {"MCP_RELEASE_ID": ".."}, {"PORT": "0"}, {"PORT": "65536"}, {"PORT": "1.5"},
    {"PORT": "1_000"}, {"HOST": ""}, {"DATABASE_PATH": "missing.sqlite3"},
])
def test_invalid_environment_is_rejected_without_creating_database(inventory, overrides):
    _, data_dir = inventory
    before = sorted(path.name for path in data_dir.iterdir())
    with pytest.raises(ConfigurationError):
        RuntimeSettings.from_env(environment(data_dir, **overrides))
    assert sorted(path.name for path in data_dir.iterdir()) == before


def test_database_must_be_directly_inside_data_dir(inventory, tmp_path):
    storage, data_dir = inventory
    elsewhere = tmp_path / "foreign.sqlite3"
    elsewhere.write_bytes(storage.path.read_bytes())
    nested = data_dir / "nested"
    nested.mkdir()
    (nested / "nested.sqlite3").write_bytes(storage.path.read_bytes())
    for path in (elsewhere, nested / "nested.sqlite3"):
        with pytest.raises(ConfigurationError):
            settings_for(data_dir, DATABASE_PATH=str(path))
    assert settings_for(data_dir, DATABASE_PATH=storage.path.name).database_path == storage.path


def test_missing_inventory_is_never_initialized(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert main(environment(data_dir)) == 2
    assert list(data_dir.iterdir()) == []
    assert capsys.readouterr().err == app_runtime._STARTUP_ERROR + "\n"


def test_first_start_requires_explicit_bootstrap(inventory):
    storage, data_dir = inventory
    original = storage.path.read_bytes()
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir))
    assert storage.path.read_bytes() == original


@pytest.mark.parametrize("target", ["bootstrap", "saved", "database"])
def test_symlink_credentials_and_database_are_rejected(inventory, tmp_path, target):
    storage, data_dir = inventory
    original = storage.path.read_bytes()
    genuine = private_json(tmp_path / "genuine.json", {"auth_token": TOKEN, "user_id": 101})
    link = data_dir / "mcp-settings.json" if target == "saved" else tmp_path / "link"
    try:
        link.symlink_to(storage.path if target == "database" else genuine)
    except OSError:
        pytest.skip("Symlink creation is not available for this Windows account")
    with pytest.raises(ConfigurationError):
        if target == "database":
            settings_for(data_dir, DATABASE_PATH=str(link))
        else:
            prepare_runtime(settings_for(data_dir, link if target == "bootstrap" else None))
    assert storage.path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not describe Windows ACLs")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620])
def test_insecure_credentials_are_rejected(inventory, tmp_path, mode):
    _, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})
    bootstrap.chmod(mode)
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir, bootstrap))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not describe Windows ACLs")
def test_runtime_generated_files_and_directory_are_private(inventory, tmp_path):
    _, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})
    prepare_runtime(settings_for(data_dir, bootstrap))
    assert stat.S_IMODE((data_dir / "backups").stat().st_mode) == 0o700
    for path in (data_dir / "mcp-settings.json", *(data_dir / "backups").iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_runner_passes_bound_context_and_host_port_without_output(inventory, tmp_path, monkeypatch, capsys):
    import mcp_http

    _, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})
    calls = []
    monkeypatch.setattr(mcp_http, "serve_http", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert main(environment(
        data_dir, MCP_BOOTSTRAP_FILE=str(bootstrap), HOST="0.0.0.0", PORT="8080", MCP_PUBLIC_URL="https://synthetic.example",
    )) == 0
    args, kwargs = calls[0]
    assert isinstance(args[0], Storage) and args[0].path == data_dir / "home_warehouse_bot.sqlite3"
    assert args[1:] == (101, TOKEN)
    assert kwargs == {
        "host": "0.0.0.0", "port": 8080, "public_url": "https://synthetic.example",
        "oauth_database": data_dir / "oauth.sqlite3",
    }
    assert capsys.readouterr() == ("", "")


def test_startup_errors_never_print_exception_secrets_or_identifiers(inventory, tmp_path, monkeypatch, capsys):
    import mcp_http

    _, data_dir = inventory
    bootstrap = private_json(tmp_path / "bootstrap.json", {"auth_token": TOKEN, "user_id": 101})

    def fail(*args, **kwargs):
        raise RuntimeError(f"{TOKEN} 101 private inventory information")

    monkeypatch.setattr(mcp_http, "serve_http", fail)
    assert main(environment(data_dir, MCP_BOOTSTRAP_FILE=str(bootstrap))) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == app_runtime._STARTUP_ERROR + "\n"
    assert TOKEN not in output.err and "101" not in output.err


@pytest.mark.skipif(os.name == "nt", reason="Named pipes are a POSIX file type")
def test_nonregular_credentials_are_rejected_without_opening(inventory, tmp_path):
    _, data_dir = inventory
    bootstrap = tmp_path / "bootstrap.pipe"
    os.mkfifo(bootstrap, mode=0o600)
    with pytest.raises(ConfigurationError):
        prepare_runtime(settings_for(data_dir, bootstrap))
