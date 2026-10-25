"""Inventory rules shared by interactive clients, without transport dependencies."""

from __future__ import annotations

import math

from amounts import convert_quantity, normalize_unit
from storage import Household, InventoryItem, Storage, Store, normalize_name


class InventoryService:
    """Perform inventory operations as one already registered user.

    Storage remains responsible for transactions and membership checks on writes.
    This layer also checks access before reads and consistently handles units.
    """

    def __init__(self, storage: Storage, chat_id: int) -> None:
        self.storage = storage
        self.chat_id = chat_id
        self.require_user()

    def require_user(self) -> None:
        if self.storage.get_user(self.chat_id) is None:
            raise ValueError("Пользователь не зарегистрирован в боте.")

    def require_household(self, household_id: int) -> None:
        self.require_user()
        if not self.storage.is_household_member(household_id, self.chat_id):
            raise ValueError("Дом не найден или недоступен.")

    def require_store(self, store_id: int) -> Store:
        self.require_user()
        store = self.storage.get_store(store_id)
        if store is None or not self.storage.is_household_member(store.household_id, self.chat_id):
            raise ValueError("Склад не найден или недоступен.")
        return store

    def require_item(self, item_id: int, *, active: bool | None = None) -> InventoryItem:
        self.require_user()
        item = self.storage.get_item(item_id)
        if item is None or not self.storage.is_household_member(item.household_id, self.chat_id):
            raise ValueError("Позиция не найдена или недоступна.")
        if active is True and not item.active:
            raise ValueError("Позиция в архиве. Сначала восстановите её.")
        return item

    def list_households(self) -> list[Household]:
        self.require_user()
        return self.storage.list_households(self.chat_id)

    def create_household(self, name: str) -> Household:
        self.require_user()
        return self.storage.create_household(self.chat_id, clean_name(name))

    def list_stores(self, household_id: int) -> list[Store]:
        self.require_household(household_id)
        return self.storage.list_stores(household_id)

    def create_store(self, household_id: int, name: str) -> Store:
        self.require_household(household_id)
        store = self.storage.create_store(self.chat_id, household_id, clean_name(name))
        if store is None:
            raise ValueError("Склад не найден или недоступен.")
        return store

    def list_items(self, store_id: int, *, include_inactive: bool = False) -> list[InventoryItem]:
        self.require_store(store_id)
        return self.storage.list_items(store_id, include_inactive=include_inactive)

    def find_item(self, store_id: int, query: str) -> InventoryItem | None:
        self.require_store(store_id)
        return self.storage.find_item(store_id, clean_name(query))

    def shopping_list(self, household_id: int) -> list[tuple[Store, list[InventoryItem]]]:
        self.require_household(household_id)
        return self.storage.low_items_by_store(household_id)

    def add_item(
        self,
        store_id: int,
        name: str,
        quantity: float = 1.0,
        unit: str | None = None,
        min_quantity: float | None = None,
        min_unit: str | None = None,
    ) -> InventoryItem:
        """Add stock, preserving an active matching item's unit.

        Name lookup follows the bot: exact normalized name, then an active
        partial match. Re-adding an archived exact name starts its stock anew,
        as Storage.add_or_update_item does; restore_item preserves old stock.
        """
        return self._add_item(store_id, name, quantity, unit, min_quantity, min_unit, match_existing=True)

    def create_item(
        self,
        store_id: int,
        name: str,
        quantity: float = 1.0,
        unit: str | None = None,
        min_quantity: float | None = None,
        min_unit: str | None = None,
    ) -> InventoryItem:
        """Create a distinct item; exact names, including archived ones, exist already.

        Unlike quick replenishment, the explicit new-item form never matches a
        partial name such as "Rice" to "Rice basmati".
        """
        self.require_store(store_id)
        name = clean_name(name)
        normalized = normalize_name(name)
        if any(item.normalized_name == normalized for item in self.storage.list_items(store_id, include_inactive=True)):
            raise ValueError("Позиция с таким названием уже есть на складе.")
        return self._add_item(store_id, name, quantity, unit, min_quantity, min_unit, match_existing=False)

    def _add_item(
        self,
        store_id: int,
        name: str,
        quantity: float,
        unit: str | None,
        min_quantity: float | None,
        min_unit: str | None,
        *,
        match_existing: bool,
    ) -> InventoryItem:
        self.require_store(store_id)
        name = clean_name(name)
        quantity = finite_quantity(quantity)
        existing = self.storage.find_item(store_id, name) if match_existing else None
        target_unit = existing.unit if existing else clean_unit(unit if unit is not None else "шт")
        quantity = converted_quantity(quantity, unit if unit is not None else target_unit, target_unit)
        if existing:
            finite_quantity(existing.quantity)
            finite_quantity(existing.quantity + quantity)
        minimum = None
        if min_quantity is not None:
            source_unit = min_unit if min_unit is not None else unit if unit is not None else target_unit
            minimum = converted_quantity(min_quantity, source_unit, target_unit)
        elif min_unit is not None:
            raise ValueError("Укажите минимальное количество вместе с единицей.")
        return self.storage.add_or_update_item(
            store_id, self.chat_id, existing.name if existing else name, quantity, target_unit, minimum
        )

    def change_quantity(self, item_id: int, delta: float, unit: str | None = None) -> InventoryItem:
        item = self.require_item(item_id, active=True)
        delta = converted_quantity(delta, unit if unit is not None else item.unit, item.unit, signed=True)
        finite_quantity(item.quantity)
        finite_quantity(max(0.0, item.quantity + delta))
        updated = self.storage.change_item_quantity(item.store_id, self.chat_id, item.id, delta)
        return require_updated(updated)

    def set_quantity(self, item_id: int, quantity: float, unit: str | None = None) -> InventoryItem:
        item = self.require_item(item_id, active=True)
        quantity = converted_quantity(quantity, unit if unit is not None else item.unit, item.unit)
        updated = self.storage.set_item_quantity(item.store_id, self.chat_id, item.id, quantity)
        return require_updated(updated)

    def set_minimum(self, item_id: int, quantity: float, unit: str | None = None) -> InventoryItem:
        item = self.require_item(item_id, active=True)
        minimum = converted_quantity(quantity, unit if unit is not None else item.unit, item.unit)
        updated = self.storage.set_item_minimum(item.store_id, item.id, minimum, self.chat_id)
        return require_updated(updated)

    def archive_item(self, item_id: int) -> InventoryItem:
        item = self.require_item(item_id)
        if not item.active:
            return item
        return require_updated(self.storage.archive_item(item.store_id, self.chat_id, item.id))

    def restore_item(self, item_id: int) -> InventoryItem:
        item = self.require_item(item_id)
        if item.active:
            return item
        return require_updated(self.storage.restore_item(item.store_id, self.chat_id, item.id))


def clean_name(value: str) -> str:
    result = " ".join(value.split())
    if not result:
        raise ValueError("Название не может быть пустым.")
    return result


def clean_unit(value: str) -> str:
    result = normalize_unit(value)
    if not result:
        raise ValueError("Единица измерения не может быть пустой.")
    return result


def finite_quantity(value: float, *, signed: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("Введите конечное число.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Введите конечное число.") from None
    if not math.isfinite(result):
        raise ValueError("Введите конечное число.")
    if not signed and result < 0:
        raise ValueError("Количество не может быть отрицательным.")
    return result


def converted_quantity(value: float, source: str, target: str, *, signed: bool = False) -> float:
    value = finite_quantity(value, signed=signed)
    converted = convert_quantity(value, clean_unit(source), clean_unit(target))
    if converted is None:
        raise ValueError(f"Количество нужно указать в {target} или совместимой единице.")
    return finite_quantity(converted, signed=signed)


def require_updated(item: InventoryItem | None) -> InventoryItem:
    if item is None:
        raise ValueError("Позиция не найдена или недоступна.")
    return item
