"""Local stdio MCP access to the same SQLite inventory used by Telegram.

The host chooses one existing bot user through MCP_CHAT_ID. Tool arguments
cannot select an identity or database. No Telegram token or network listener
is needed.
"""

from __future__ import annotations

import argparse
import os
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
from pydantic import BaseModel, Field

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
    chat_id: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MCPSettings:
        values = os.environ if environ is None else environ
        raw_path = values.get("DATABASE_PATH", "").strip()
        if not raw_path:
            raise ConfigurationError("Set DATABASE_PATH to the existing bot SQLite database.")
        raw_identity = values.get("MCP_CHAT_ID", "").strip()
        try:
            chat_id = int(raw_identity)
        except ValueError:
            raise ConfigurationError("Set MCP_CHAT_ID to an existing registered bot user's integer ID.") from None
        if not chat_id or not -(2**63) <= chat_id < 2**63:
            raise ConfigurationError("MCP_CHAT_ID must be a nonzero signed 64-bit integer.")
        try:
            database_path = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError, RuntimeError):
            raise ConfigurationError("DATABASE_PATH must be a valid existing bot SQLite file path.") from None
        return cls(database_path, chat_id)

    def open_storage(self) -> Storage:
        """Fail before migrations if the file or registered identity is absent."""
        if not self.database_path.is_file():
            raise ConfigurationError("DATABASE_PATH must point to an existing bot SQLite database file.")
        try:
            with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True)) as connection:
                user = connection.execute("SELECT 1 FROM users WHERE chat_id = ?", (self.chat_id,)).fetchone()
                required = {"households", "household_members", "inventory_items"}
                tables = {
                    row[0]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                if not required <= tables:
                    raise ConfigurationError("DATABASE_PATH is not an initialized bot inventory database.")
        except (sqlite3.Error, OSError):
            raise ConfigurationError("Cannot read the existing bot inventory database in DATABASE_PATH.") from None
        if user is None:
            raise ConfigurationError("MCP_CHAT_ID must identify a user already registered in this bot database.")
        try:
            return Storage(self.database_path)
        except (sqlite3.Error, OSError):
            raise ConfigurationError("Cannot open or migrate the existing bot inventory database.") from None


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
            raise ToolError("Inventory operation failed. Retry after checking the bot database.") from None

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


def build_server(storage: Storage, chat_id: int) -> MCPServer:
    """Build a server bound to one registered user; useful for embedding/tests."""
    service = InventoryService(storage, chat_id)
    server = InventoryMCPServer(
        "home-warehouse",
        instructions=(
            "Manage the configured user's home inventory. First list households, then stores and items. "
            "IDs are scoped by household membership. Omitted units use the existing item's unit, or шт "
            "for a new item. Read data is current SQLite state. Mutations do not send Telegram messages. "
            "Archived items must be restored before editing; adding their exact name instead starts new stock."
        ),
        log_level="WARNING",
    )

    @server.tool(annotations=READ)
    @domain_errors
    def list_households() -> HouseholdsResult:
        """List homes accessible to the configured bot user, without invitation codes or member data."""
        return HouseholdsResult(households=[household_result(h) for h in service.list_households()])

    @server.tool(annotations=CREATE)
    @domain_errors
    def create_household(name: Name) -> HouseholdResult:
        """Create a home and its initial stores; the bot selects the new home for this user."""
        return household_result(service.create_household(name))

    @server.tool(annotations=READ)
    @domain_errors
    def list_stores(household_id: ObjectId) -> StoresResult:
        """List named stores in an accessible home."""
        return StoresResult(household_id=household_id, stores=[store_result(s) for s in service.list_stores(household_id)])

    @server.tool(annotations=CREATE_STORE)
    @domain_errors
    def create_store(household_id: ObjectId, name: Name) -> StoreResult:
        """Create a named store, or return the existing store with that name; selects it in the bot."""
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

        Matches the bot's exact normalized name, then active partial-name lookup.
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
        epilog="Required environment: DATABASE_PATH (existing bot SQLite file), MCP_CHAT_ID (registered bot user ID).",
    )
    parser.parse_args(argv)
    try:
        settings = MCPSettings.from_env()
        server = build_server(settings.open_storage(), settings.chat_id)
    except (ConfigurationError, ValueError) as exc:
        print(f"MCP configuration error: {exc}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
