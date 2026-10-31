"""Standalone local inventory MCP server and operator-only database setup.

The host binds tools to an existing local identity through MCP_USER_ID.
MCP_CHAT_ID remains a legacy configuration alias; database identities continue
to use the existing users.chat_id column. Tools cannot select an identity or
database, and setup is available only through the explicit init CLI command.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from inventory_service import InventoryService
from storage import Household, InventoryItem, Storage, Store


class ConfigurationError(ValueError):
    """A safe, actionable startup error without database or account details."""


class InventoryMCPServer(MCPServer):
    """Reject unknown arguments rather than silently ignoring an identity override."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        tools = await self.list_tools()
        tool = next((tool for tool in tools if tool.name == name), None)
        if tool is not None and set(arguments) - set(tool.input_schema.get("properties", {})):
            raise ToolError("Unknown tool arguments. Identity and database are fixed by server configuration.")
        return await super().call_tool(name, arguments, context)


@dataclass(frozen=True)
class MCPSettings:
    database_path: Path
    user_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", database_path(self.database_path))
        validate_user_id(self.user_id)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MCPSettings:
        values = os.environ if environ is None else environ
        raw_path = values.get("DATABASE_PATH", "").strip()
        if not raw_path:
            raise ConfigurationError("Set DATABASE_PATH to an initialized inventory SQLite database.")
        primary = values.get("MCP_USER_ID")
        legacy = values.get("MCP_CHAT_ID")
        if primary is None and legacy is None:
            raise ConfigurationError("Set MCP_USER_ID to an existing registered local user's integer ID.")
        user_id = parse_user_id(primary if primary is not None else legacy)
        if legacy is not None and parse_user_id(legacy) != user_id:
            raise ConfigurationError("MCP_USER_ID and legacy MCP_CHAT_ID must identify the same user.")
        return cls(database_path(raw_path), user_id)

    def open_storage(self) -> Storage:
        """Fail before migrations if the file or registered identity is absent."""
        inspect_database(self.database_path, self.user_id)
        try:
            return Storage(self.database_path)
        except (sqlite3.Error, OSError):
            raise ConfigurationError("Cannot open or migrate the existing inventory database.") from None


def parse_user_id(value: str | None) -> int:
    """Parse configuration without accepting floats, booleans, or separators."""
    if value is None or re.fullmatch(r"[+-]?[0-9]+", value.strip()) is None:
        raise ConfigurationError("MCP_USER_ID (or legacy MCP_CHAT_ID) must be a nonzero signed 64-bit integer.")
    try:
        user_id = int(value)
    except ValueError:
        raise ConfigurationError("MCP_USER_ID must be a nonzero signed 64-bit integer.") from None
    validate_user_id(user_id)
    return user_id


def validate_user_id(user_id: int) -> None:
    if type(user_id) is not int or not user_id or not -(2**63) <= user_id < 2**63:
        raise ConfigurationError("MCP_USER_ID must be a nonzero signed 64-bit integer.")


def validate_profile_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"prf_[A-Za-z0-9_-]{43}", value) is None:
        raise ConfigurationError("Use the existing opaque profile ID from protected runtime configuration.")
    return value


def database_path(value: str | Path) -> Path:
    try:
        if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value):
            raise ValueError
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        raise ConfigurationError("DATABASE_PATH must be a valid inventory SQLite file path.") from None


# Recognize the existing schema and the pre-store inventory revision supported
# by Storage. Missing auxiliary tables are additive migrations; malformed tables
# must be rejected before Storage executes any CREATE/ALTER/seed statements.
_REQUIRED_COLUMNS = {
    "users": {"chat_id", "username", "first_name", "current_household_id", "reminders_enabled", "created_at", "last_seen"},
    "households": {"id", "name", "invite_code", "owner_chat_id", "created_at"},
    "household_members": {"household_id", "chat_id", "role", "joined_at"},
    "inventory_items": {
        "id", "household_id", "name", "normalized_name", "quantity", "unit", "min_quantity", "active",
        "notes", "last_reminded_at", "created_at", "updated_at",
    },
    "stores": {"id", "household_id", "name", "normalized_name", "created_at"},
    "item_events": {"id", "item_id", "chat_id", "action", "delta", "note", "created_at"},
    "active_views": {"chat_id", "message_id", "view_type", "household_id", "store_id", "item_id", "page", "updated_at"},
    "schema_migrations": {"name", "applied_at"},
}


def has_unique_key(connection: sqlite3.Connection, table: str, columns: set[str]) -> bool:
    for (index,) in connection.execute(
        'SELECT name FROM pragma_index_list(?) WHERE "unique" = 1 AND partial = 0', (table,)
    ):
        indexed = [row[0] for row in connection.execute("SELECT name FROM pragma_index_info(?)", (index,))]
        if len(indexed) == len(columns) and set(indexed) == columns:
            return True
    return False


def validate_database_schema(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"users", "households", "household_members", "inventory_items"} <= tables:
        raise ConfigurationError("DATABASE_PATH is not an initialized inventory database.")
    columns_by_table = {}
    for table, required in _REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        columns = {row[1]: row for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required <= columns.keys():
            raise ConfigurationError("DATABASE_PATH has an unsupported inventory database schema.")
        columns_by_table[table] = columns
        identity_column = "chat_id" if table in {"users", "active_views"} else "id"
        if identity_column in columns:
            column = columns[identity_column]
            if column[2].upper() != "INTEGER" or column[5] != 1:
                raise ConfigurationError("DATABASE_PATH has an unsupported inventory database schema.")
    if "store_id" in columns_by_table["inventory_items"] and "stores" not in tables:
        raise ConfigurationError("DATABASE_PATH has an unsupported inventory database schema.")
    unique_keys = {"household_members": {"household_id", "chat_id"}, "households": {"invite_code"}}
    if "stores" in tables:
        unique_keys["stores"] = {"household_id", "normalized_name"}
    if "schema_migrations" in tables:
        unique_keys["schema_migrations"] = {"name"}
    inventory_scope = "store_id" if "store_id" in columns_by_table["inventory_items"] else "household_id"
    unique_keys["inventory_items"] = {inventory_scope, "normalized_name"}
    if any(not has_unique_key(connection, table, columns) for table, columns in unique_keys.items()):
        raise ConfigurationError("DATABASE_PATH has an unsupported inventory database schema.")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ConfigurationError("DATABASE_PATH is not a valid inventory database.")


def inspect_database(path: Path, user_id: int, *, allow_empty_users: bool = False) -> bool:
    """Check recognized schema and identity read-only; return whether user exists."""
    try:
        if not path.is_file():
            raise ConfigurationError("DATABASE_PATH must point to an existing inventory SQLite database file.")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            validate_database_schema(connection)
            known = connection.execute("SELECT 1 FROM users WHERE chat_id = ?", (user_id,)).fetchone() is not None
            if not known and (not allow_empty_users or connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()):
                raise ConfigurationError("MCP_USER_ID must identify a user already registered in this inventory database.")
            return known
    except (sqlite3.Error, OSError):
        raise ConfigurationError("Cannot read the existing inventory database in DATABASE_PATH.") from None


def initialize_database(path: str | Path, user_id: int, household_name: str) -> Storage:
    """Explicit operator setup, preserving all existing users and inventory."""
    validate_user_id(user_id)
    if not isinstance(household_name, str) or not (name := " ".join(household_name.split())) or "\0" in name:
        raise ConfigurationError("The initial household name must be nonempty text.")
    path = database_path(path)
    known = False
    created = False
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm")]
    cleanup_sidecars: list[Path] = []
    try:
        if path.exists():
            known = inspect_database(path, user_id, allow_empty_users=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            cleanup_sidecars = [sidecar for sidecar in sidecars if not sidecar.exists()]
            # Set permissions at exclusive creation, never chmod an existing file.
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            os.close(descriptor)
        storage = Storage(path)
        if not known:
            storage.record_user(user_id, None, None)
        if not storage.list_households(user_id):
            storage.create_household(user_id, name)
        return storage
    except (sqlite3.Error, OSError):
        if created:
            for created_path in [*cleanup_sidecars, path]:
                try:
                    created_path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise ConfigurationError("Cannot initialize the inventory database.") from None


class ProfileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"$schema": "https://json-schema.org/draft/2020-12/schema"})

    id: str = Field(min_length=1, pattern=r"\S", description="Opaque profile identity, unchanged across token refresh and reconnection.")
    name: str
    nickname: str


class HouseholdResult(BaseModel):
    id: int
    name: str


class HouseholdsResult(BaseModel):
    households: list[HouseholdResult]


class StoreResult(BaseModel):
    id: int
    household_id: int
    name: str


class StoresResult(BaseModel):
    household_id: int
    stores: list[StoreResult]


class ItemResult(BaseModel):
    id: int
    household_id: int
    store_id: int
    name: str
    quantity: float
    unit: str
    min_quantity: float
    active: bool
    is_low: bool
    updated_at: str


class ItemsResult(BaseModel):
    store_id: int
    items: list[ItemResult]


class ShoppingStoreResult(BaseModel):
    store: StoreResult
    items: list[ItemResult]


class ShoppingResult(BaseModel):
    household_id: int
    stores: list[ShoppingStoreResult]


def household_result(household: Household) -> HouseholdResult:
    # Deliberately omit invite codes, ownership IDs, and member profiles.
    return HouseholdResult(id=household.id, name=household.name)


def store_result(store: Store) -> StoreResult:
    return StoreResult(id=store.id, household_id=store.household_id, name=store.name)


def item_result(item: InventoryItem) -> ItemResult:
    return ItemResult(
        id=item.id,
        household_id=item.household_id,
        store_id=item.store_id,
        name=item.name,
        quantity=item.quantity,
        unit=item.unit,
        min_quantity=item.min_quantity,
        active=item.active,
        is_low=item.is_low,
        updated_at=item.updated_at,
    )


P = ParamSpec("P")
R = TypeVar("R")


def domain_errors(function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        except (sqlite3.Error, KeyError):
            raise ToolError("Inventory operation failed. Retry after checking the inventory database.") from None

    return wrapped


READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CREATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
CREATE_STORE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
ADD = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
SET = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
RESTORE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

ObjectId = Annotated[int, Field(gt=0, strict=True, description="ID returned by an inventory tool.")]
Quantity = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
Delta = Annotated[float, Field(allow_inf_nan=False, strict=True)]
Name = Annotated[str, Field(min_length=1)]


def build_server(storage: Storage, user_id: int, *, profile_id: str | None = None) -> MCPServer:
    """Build a server bound to one registered user; useful for embedding/tests."""
    service = InventoryService(storage, user_id)
    server = InventoryMCPServer(
        "home-warehouse",
        instructions=(
            "Manage the configured user's home inventory. First list households, then stores and items. "
            "IDs are scoped by household membership. Omitted units use the existing item's unit, or шт "
            "for a new item. Read data is current SQLite state. "
            "Archived items must be restored before editing; adding their exact name instead starts new stock."
        ),
        log_level="WARNING",
    )

    if profile_id is not None:
        profile_id = validate_profile_id(profile_id)

        @server.tool(
            annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
            meta={"openai/profile": True, "securitySchemes": [{"type": "oauth2", "scopes": ["inventory"]}]},
        )
        @domain_errors
        def get_profile() -> ProfileResult:
            """Return the authenticated configured profile, with a stable identity across reconnections."""
            service.require_user()
            return ProfileResult(id=profile_id, name="Склад", nickname="Дом")

    @server.tool(annotations=READ)
    @domain_errors
    def list_households() -> HouseholdsResult:
        """List homes accessible to the configured local user, without invitation codes or member data."""
        return HouseholdsResult(households=[household_result(h) for h in service.list_households()])

    @server.tool(annotations=CREATE)
    @domain_errors
    def create_household(name: Name) -> HouseholdResult:
        """Create a home and its initial stores; selects the new home for this user."""
        return household_result(service.create_household(name))

    @server.tool(annotations=READ)
    @domain_errors
    def list_stores(household_id: ObjectId) -> StoresResult:
        """List named stores in an accessible home."""
        return StoresResult(household_id=household_id, stores=[store_result(s) for s in service.list_stores(household_id)])

    @server.tool(annotations=CREATE_STORE)
    @domain_errors
    def create_store(household_id: ObjectId, name: Name) -> StoreResult:
        """Create a named store, or return the existing store with that name; selects it for this user."""
        return store_result(service.create_store(household_id, name))

    @server.tool(annotations=READ)
    @domain_errors
    def list_items(store_id: ObjectId, include_archived: bool = False) -> ItemsResult:
        """List items in an accessible store. Archived items are hidden unless explicitly requested."""
        items = service.list_items(store_id, include_inactive=include_archived)
        return ItemsResult(store_id=store_id, items=[item_result(i) for i in items])

    @server.tool(annotations=ADD)
    @domain_errors
    def add_item(
        store_id: ObjectId,
        name: Name,
        quantity: Quantity = 1.0,
        unit: str | None = None,
        min_quantity: Quantity | None = None,
        min_unit: str | None = None,
    ) -> ItemResult:
        """Add stock with unit conversion; quantity/minimum must be finite and nonnegative.

        Matches an exact normalized name, then active partial-name lookup.
        An existing active item's unit stays unchanged. Omitted unit uses that
        unit (шт for new items). An archived exact-name item starts again with
        the supplied stock and becomes active; restore_item preserves old stock.
        Omitted minimum preserves an existing minimum (new items default to 1).
        A supplied minimum uses min_unit, then unit, then the item's unit.
        """
        return item_result(service.add_item(store_id, name, quantity, unit, min_quantity, min_unit))

    @server.tool(annotations=CHANGE)
    @domain_errors
    def change_item_quantity(item_id: ObjectId, delta: Delta, unit: str | None = None) -> ItemResult:
        """Add a signed amount to an active item; negative consumes stock and the result is clamped at zero."""
        return item_result(service.change_quantity(item_id, delta, unit))

    @server.tool(annotations=SET)
    @domain_errors
    def set_item_quantity(item_id: ObjectId, quantity: Quantity, unit: str | None = None) -> ItemResult:
        """Replace an active item's stock with a finite nonnegative quantity, converting a supplied unit."""
        return item_result(service.set_quantity(item_id, quantity, unit))

    @server.tool(annotations=SET)
    @domain_errors
    def set_item_minimum(item_id: ObjectId, min_quantity: Quantity, unit: str | None = None) -> ItemResult:
        """Set an active item's shopping threshold; stock at or below it appears in shopping_list."""
        return item_result(service.set_minimum(item_id, min_quantity, unit))

    @server.tool(annotations=SET)
    @domain_errors
    def archive_item(item_id: ObjectId) -> ItemResult:
        """Archive an item without deleting its quantity. Repeated calls leave it archived."""
        return item_result(service.archive_item(item_id))

    @server.tool(annotations=RESTORE)
    @domain_errors
    def restore_item(item_id: ObjectId) -> ItemResult:
        """Restore an archived item with its saved stock and minimum. Repeated calls leave it active."""
        return item_result(service.restore_item(item_id))

    @server.tool(annotations=READ)
    @domain_errors
    def shopping_list(household_id: ObjectId) -> ShoppingResult:
        """List active items at or below their minimum, grouped by store in an accessible home."""
        return ShoppingResult(
            household_id=household_id,
            stores=[
                ShoppingStoreResult(store=store_result(store), items=[item_result(i) for i in items])
                for store, items in service.shopping_list(household_id)
            ],
        )

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the home warehouse MCP server over local stdin/stdout.",
        epilog=(
            "Serving environment: DATABASE_PATH (initialized inventory SQLite file), MCP_USER_ID (registered local user ID). "
            "MCP_CHAT_ID is a legacy alias for MCP_USER_ID. Bare invocation serves over stdio."
        ),
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Serve an initialized database over local stdin/stdout.")
    initialize = commands.add_parser("init", help="Initialize local inventory as an operator; safe to repeat.")
    initialize.add_argument("--database-path", required=True, help="New or recognized existing inventory SQLite file.")
    initialize.add_argument("--user-id", required=True, help="Nonzero signed 64-bit local identity.")
    initialize.add_argument("--household-name", required=True, help="Initial home name, used only when this user has no homes.")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            initialize_database(arguments.database_path, parse_user_id(arguments.user_id), arguments.household_name)
            print("Inventory database initialized.")
            return 0
        settings = MCPSettings.from_env()
        server = build_server(settings.open_storage(), settings.user_id)
    except (ConfigurationError, ValueError) as exc:
        print(f"MCP configuration error: {exc}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
