"""Inventory values and compatibility models for persisted SQLite data."""

from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class Household:
    id: int
    name: str
    invite_code: str
    owner_chat_id: int
    created_at: str


@dataclass(frozen=True)
class Store:
    id: int
    household_id: int
    name: str
    created_at: str


@dataclass(frozen=True)
class InventoryItem:
    id: int
    household_id: int
    store_id: int
    name: str
    normalized_name: str
    quantity: float
    unit: str
    min_quantity: float
    active: bool
    notes: str | None
    updated_at: str

    @property
    def is_low(self) -> bool:
        return self.active and self.quantity <= self.min_quantity


@dataclass(frozen=True)
class User:
    chat_id: int
    username: str | None
    first_name: str | None
    current_household_id: int | None
    current_store_id: int | None
    last_seen: str | None


@dataclass(frozen=True)
class ActiveView:
    chat_id: int
    message_id: int
    view_type: str
    household_id: int | None
    store_id: int | None
    item_id: int | None
    page: int
    updated_at: str


def normalize_name(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def format_quantity(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
