"""Private Apps-platform startup for an existing inventory database.

Runtime credentials bind the HTTP tools to one existing local identity. This
entry point deliberately has no database initialization or identity creation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import struct
import sys
import tempfile
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_server import ConfigurationError, MCPSettings, validate_database_schema, validate_profile_id, validate_user_id
from storage import Storage


_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_RELEASE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_STARTUP_ERROR = (
    "MCP startup failed. Check the protected runtime configuration, existing "
    "inventory database, and release backup; then restart."
)


@dataclass(frozen=True)
class RuntimeSettings:
    data_dir: Path = field(repr=False)
    database_path: Path = field(repr=False)
    release_id: str
    host: str = "127.0.0.1"
    port: int = 8000
    bootstrap_file: Path | None = field(default=None, repr=False)
    public_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
        values = os.environ if environ is None else environ
        try:
            raw_dir = values.get("APP_DATA_DIR", "").strip()
            if not raw_dir or "\0" in raw_dir:
                raise ValueError
            data_dir = Path(raw_dir).expanduser().resolve(strict=True)
            if not data_dir.is_dir():
                raise ValueError
            raw_database = values.get("DATABASE_PATH", "").strip()
            database = Path(raw_database).expanduser() if raw_database else Path("home_warehouse_bot.sqlite3")
            if not database.is_absolute():
                database = data_dir / database
            if database.is_symlink():
                raise ValueError
            database = database.resolve(strict=True)
            if database.parent != data_dir or not database.is_file():
                raise ValueError
            release = values.get("MCP_RELEASE_ID", "")
            if _RELEASE.fullmatch(release) is None or release in {".", ".."}:
                raise ValueError
            host = values.get("HOST", "127.0.0.1")
            if not host or any(character.isspace() or ord(character) < 32 for character in host):
                raise ValueError
            raw_port = values.get("PORT", "8000")
            if re.fullmatch(r"[0-9]+", raw_port) is None or not 1 <= (port := int(raw_port)) <= 65535:
                raise ValueError
            raw_bootstrap = values.get("MCP_BOOTSTRAP_FILE", "").strip()
            bootstrap = Path(os.path.abspath(Path(raw_bootstrap).expanduser())) if raw_bootstrap else None
            public_url = values.get("MCP_PUBLIC_URL")
            if public_url is not None and not public_url:
                raise ValueError
            return cls(data_dir, database, release, host, port, bootstrap, public_url)
        except (OSError, ValueError, RuntimeError, TypeError):
            raise ConfigurationError("Set a valid APP_DATA_DIR, existing DATABASE_PATH, MCP_RELEASE_ID, HOST and PORT.") from None


@dataclass(frozen=True)
class RuntimeContext:
    storage: Storage = field(repr=False)
    user_id: int = field(repr=False)
    auth_token: str = field(repr=False)
    public_url: str | None = field(default=None, repr=False)
    profile_id: str | None = field(default=None, repr=False)


def _private_file(path: Path) -> int:
    """Open a regular private file without following a credential symlink."""
    descriptor = -1
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise ValueError
        return descriptor
    except (OSError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise ConfigurationError("Use a regular protected runtime configuration or backup file.") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        with os.fdopen(_private_file(path), "r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError("Use valid protected JSON runtime configuration.") from None


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_private_json(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".mcp-settings-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            # Reject a replaced credential symlink even though replace itself
            # would replace the link rather than follow it.
            with os.fdopen(_private_file(path), "rb"):
                pass
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_token(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 32 or _TOKEN.fullmatch(value) is None:
        raise ConfigurationError("Provide a protected, randomly generated bearer token of at least 32 characters.")
    return value


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _check_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ConfigurationError("Repair the existing inventory database or release backup before restarting.")


def _bind_identity(database: Path, supplied: dict[str, Any]) -> int:
    """Resolve bootstrap identity without invoking migrations or user setters."""
    with closing(_open_readonly(database)) as connection:
        validate_database_schema(connection)
        if "user_id" in supplied:
            user_id = supplied["user_id"]
            validate_user_id(user_id)
            if supplied.get("select_single_owner", False) is not False:
                raise ConfigurationError("Choose one explicit bootstrap identity selection method.")
        elif supplied.get("select_single_owner") is True:
            owners = connection.execute(
                """SELECT DISTINCT h.owner_chat_id
                   FROM households h
                   JOIN users u ON u.chat_id = h.owner_chat_id
                   JOIN household_members m ON m.household_id = h.id AND m.chat_id = h.owner_chat_id"""
            ).fetchall()
            if len(owners) != 1:
                raise ConfigurationError("Set an explicit existing identity when inventory ownership is absent or ambiguous.")
            user_id = owners[0][0]
            validate_user_id(user_id)
        else:
            raise ConfigurationError("Provide an explicit existing identity or select_single_owner in protected bootstrap JSON.")
        if connection.execute("SELECT 1 FROM users WHERE chat_id = ?", (user_id,)).fetchone() is None:
            raise ConfigurationError("Configure an identity already registered in the existing inventory.")
        return user_id


def _row_digest(row: tuple[Any, ...]) -> bytes:
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            encoded = b"n"
        elif isinstance(value, int):
            encoded = b"i" + str(value).encode("ascii")
        elif isinstance(value, float):
            encoded = b"f" + struct.pack(">d", value)
        elif isinstance(value, bytes):
            encoded = b"b" + value
        else:
            encoded = b"s" + value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _database_fingerprint(connection: sqlite3.Connection) -> str:
    """Compare all schema and rows without logging any inventory contents."""
    digest = hashlib.sha256()
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    for row in schema:
        digest.update(_row_digest(row))
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        rows = sorted(_row_digest(row) for row in connection.execute(f"SELECT * FROM {quoted}"))
        digest.update(_row_digest((name, len(rows))))
        for row_digest in rows:
            digest.update(row_digest)
    for pragma in ("user_version", "application_id"):
        digest.update(_row_digest((pragma, connection.execute(f"PRAGMA {pragma}").fetchone()[0])))
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with os.fdopen(_private_file(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_release_backup(settings: RuntimeSettings) -> Path:
    """Create one checked snapshot per release, never replace an existing one."""
    directory = settings.data_dir / "backups"
    if directory.is_symlink():
        raise ConfigurationError("Use a regular private release backup directory.")
    directory.mkdir(mode=0o700, exist_ok=True)
    if not directory.is_dir():
        raise ConfigurationError("Use a regular private release backup directory.")
    backup = directory / f"before-mcp-{settings.release_id}.sqlite3"
    receipt = directory / f"before-mcp-{settings.release_id}.receipt.json"
    if receipt.exists() or receipt.is_symlink():
        saved = _read_private_json(receipt)
        if (
            saved.get("release_id") != settings.release_id
            or saved.get("database_name") != settings.database_path.name
            or saved.get("backup_sha256") != _file_digest(backup)
        ):
            raise ConfigurationError("Restore the recorded release backup before restarting.")
        with closing(_open_readonly(backup)) as connection:
            _check_integrity(connection)
        return backup

    with closing(_open_readonly(settings.database_path)) as source:
        source.execute("BEGIN")
        _check_integrity(source)
        expected = _database_fingerprint(source)
        if backup.exists() or backup.is_symlink():
            # Recover a crash between publishing the snapshot and its receipt
            # only when the existing snapshot still matches the source exactly.
            _file_digest(backup)
            with closing(_open_readonly(backup)) as existing:
                _check_integrity(existing)
                if _database_fingerprint(existing) != expected:
                    raise ConfigurationError("Preserve and inspect the existing release backup before restarting.")
        else:
            descriptor, name = tempfile.mkstemp(prefix=".before-mcp-", suffix=".sqlite3", dir=directory)
            temporary = Path(name)
            os.close(descriptor)
            try:
                with closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination)
                    _check_integrity(destination)
                    if _database_fingerprint(destination) != expected:
                        raise ConfigurationError("The release snapshot could not be verified; restart after checking storage.")
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.link(temporary, backup)
                _fsync_directory(directory)
            finally:
                temporary.unlink(missing_ok=True)
        source.execute("ROLLBACK")
    _write_private_json(receipt, {
        "release_id": settings.release_id,
        "database_name": settings.database_path.name,
        "backup_sha256": _file_digest(backup),
    })
    return backup


def prepare_runtime(settings: RuntimeSettings) -> RuntimeContext:
    # The HTTP module is loaded only during startup, allowing ordinary imports
    # and focused lifecycle tests without starting an HTTP server.
    from mcp_http import validate_public_url

    path = settings.data_dir / "mcp-settings.json"
    bootstrap = _read_private_json(settings.bootstrap_file) if settings.bootstrap_file is not None else {}
    if set(bootstrap) - {"auth_token", "user_id", "select_single_owner", "public_url"}:
        raise ConfigurationError("Use only supported protected bootstrap fields.")
    if "select_single_owner" in bootstrap and type(bootstrap["select_single_owner"]) is not bool:
        raise ConfigurationError("select_single_owner must be a JSON boolean.")
    exists = path.exists() or path.is_symlink()
    saved = _read_private_json(path) if exists else {}
    if exists:
        if set(saved) - {"auth_token", "user_id", "public_url", "profile_id"} or not {"auth_token", "user_id"} <= saved.keys():
            raise ConfigurationError("Use a valid protected runtime configuration.")
        token = _validate_token(saved["auth_token"])
        user_id = _bind_identity(settings.database_path, {"user_id": saved["user_id"]})
        if "auth_token" in bootstrap and _validate_token(bootstrap["auth_token"]) != token:
            raise ConfigurationError("Bootstrap credentials must match the saved runtime configuration.")
        if "user_id" in bootstrap:
            validate_user_id(bootstrap["user_id"])
            if bootstrap["user_id"] != user_id:
                raise ConfigurationError("Bootstrap identity must match the saved runtime configuration.")
        if "user_id" in bootstrap and bootstrap.get("select_single_owner") is True:
            raise ConfigurationError("Choose one explicit bootstrap identity selection method.")
    else:
        token = _validate_token(bootstrap.get("auth_token"))
        user_id = _bind_identity(settings.database_path, bootstrap)
    # Validate every supplied URL even when a higher-precedence override exists.
    saved_url = validate_public_url(saved.get("public_url"))
    bootstrap_url = validate_public_url(bootstrap.get("public_url"))
    override_url = validate_public_url(settings.public_url)
    public_url = override_url or bootstrap_url or saved_url
    # Profile identity belongs to the configured principal, not a credential,
    # connection, or display label. Migrate once; never replace an invalid ID.
    profile_id = validate_profile_id(saved["profile_id"]) if "profile_id" in saved else "prf_" + secrets.token_urlsafe(32)
    configuration: dict[str, Any] = {"auth_token": token, "user_id": user_id, "profile_id": profile_id}
    if public_url is not None:
        configuration["public_url"] = public_url

    ensure_release_backup(settings)
    storage = MCPSettings(settings.database_path, user_id).open_storage()
    if configuration != saved or not exists:
        _write_private_json(path, configuration, replace=exists)
    return RuntimeContext(storage, user_id, token, public_url, profile_id=profile_id)


def main(environ: Mapping[str, str] | None = None) -> int:
    try:
        settings = RuntimeSettings.from_env(environ)
        context = prepare_runtime(settings)
        from mcp_http import serve_http

        serve_http(
            context.storage,
            context.user_id,
            context.auth_token,
            host=settings.host,
            port=settings.port,
            public_url=context.public_url,
            profile_id=context.profile_id,
            oauth_database=settings.data_dir / "oauth.sqlite3" if context.public_url else None,
        )
        return 0
    except Exception:
        print(_STARTUP_ERROR, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
