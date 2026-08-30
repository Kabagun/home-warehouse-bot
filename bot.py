from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from storage import Household, InventoryItem, Storage, Store, format_quantity


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BTN_STOCK = "Склады"
BTN_BUY = "Купить"
BTN_HOMES = "Дом"
BTN_HELP = "Помощь"

STATE_SET_MINIMUM = "set_minimum"
STATE_CREATE_HOUSEHOLD = "create_household"
STATE_JOIN_HOUSEHOLD = "join_household"
STATE_RENAME_HOUSEHOLD = "rename_household"
STATE_CREATE_STORE = "create_store"
STATE_RENAME_STORE = "rename_store"
STATE_CREATE_ITEM = "create_item"
STATE_RENAME_ITEM = "rename_item"
STATE_ADJUST_QUANTITY = "adjust_quantity"

MENU_BUTTONS = {BTN_STOCK, BTN_BUY, BTN_HOMES, BTN_HELP}


@dataclass(frozen=True)
class Settings:
    token: str
    database_path: str
    reminder_interval_hours: int
    reminder_check_interval_seconds: int
    reminder_hours: frozenset[int]
    timezone: ZoneInfo


@dataclass(frozen=True)
class UnitInfo:
    group: str
    base_factor: float
    canonical: str


@dataclass(frozen=True)
class ParsedAmount:
    name: str
    quantity: float
    unit: str
    min_quantity: float | None
    min_unit: str | None


UNIT_ALIASES = {
    "г": UnitInfo("mass", 1.0, "г"),
    "гр": UnitInfo("mass", 1.0, "г"),
    "грамм": UnitInfo("mass", 1.0, "г"),
    "грамма": UnitInfo("mass", 1.0, "г"),
    "граммов": UnitInfo("mass", 1.0, "г"),
    "g": UnitInfo("mass", 1.0, "г"),
    "кг": UnitInfo("mass", 1000.0, "кг"),
    "килограмм": UnitInfo("mass", 1000.0, "кг"),
    "килограмма": UnitInfo("mass", 1000.0, "кг"),
    "килограммов": UnitInfo("mass", 1000.0, "кг"),
    "kg": UnitInfo("mass", 1000.0, "кг"),
    "мл": UnitInfo("volume", 1.0, "мл"),
    "миллилитр": UnitInfo("volume", 1.0, "мл"),
    "миллилитра": UnitInfo("volume", 1.0, "мл"),
    "миллилитров": UnitInfo("volume", 1.0, "мл"),
    "ml": UnitInfo("volume", 1.0, "мл"),
    "л": UnitInfo("volume", 1000.0, "л"),
    "литр": UnitInfo("volume", 1000.0, "л"),
    "литра": UnitInfo("volume", 1000.0, "л"),
    "литров": UnitInfo("volume", 1000.0, "л"),
    "l": UnitInfo("volume", 1000.0, "л"),
}

COMMON_UNIT_ALIASES = {
    "т": "табл",
    "таб": "табл",
    "табл": "табл",
    "таблетка": "табл",
    "таблетки": "табл",
    "таблеток": "табл",
    "капс": "капс",
    "капсула": "капс",
    "капсулы": "капс",
    "капсул": "капс",
    "шт": "шт",
    "штука": "шт",
    "штуки": "шт",
    "штук": "шт",
    "пачка": "пачка",
    "пачки": "пачка",
    "пачек": "пачка",
}


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Укажите TELEGRAM_BOT_TOKEN в .env или переменных окружения.")
    timezone_name = os.getenv("TIMEZONE", "Europe/Minsk").strip() or "Europe/Minsk"
    reminder_hours = {
        int(part.strip())
        for part in os.getenv("REMINDER_HOURS", "9,18").split(",")
        if part.strip().isdigit()
    }
    return Settings(
        token=token,
        database_path=os.getenv("DATABASE_PATH", "home_warehouse_bot.sqlite3"),
        reminder_interval_hours=int(os.getenv("REMINDER_INTERVAL_HOURS", "24")),
        reminder_check_interval_seconds=int(os.getenv("REMINDER_CHECK_INTERVAL_SECONDS", "900")),
        reminder_hours=frozenset(reminder_hours or {9, 18}),
        timezone=ZoneInfo(timezone_name),
    )


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_STOCK), KeyboardButton(BTN_BUY)],
            [KeyboardButton(BTN_HOMES), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="+ кофе 1 или - кофе 1",
    )


def homes_keyboard(households: list[Household], current_id: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for household in households:
        mark = "• " if household.id == current_id else ""
        rows.append([InlineKeyboardButton(f"{mark}{household.name}", callback_data=f"home:switch:{household.id}")])
    rows.extend(
        [
            [InlineKeyboardButton("Редактировать", callback_data="home:edit")],
            [InlineKeyboardButton("Присоединиться по коду", callback_data="home:join")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def home_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать дом", callback_data="home:create")],
            [InlineKeyboardButton("Переименовать дом", callback_data="home:rename")],
            [InlineKeyboardButton("Удалить дом", callback_data="home:remove")],
            [InlineKeyboardButton("Назад", callback_data="home:back")],
        ]
    )


def no_household_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать дом", callback_data="home:create")],
            [InlineKeyboardButton("Присоединиться по коду", callback_data="home:join")],
        ]
    )


def remove_household_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да", callback_data="home:remove_confirm"),
                InlineKeyboardButton("Отмена", callback_data="home:remove_cancel"),
            ]
        ]
    )


def stores_keyboard(stores: list[Store], current_id: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for store in stores:
        rows.append([InlineKeyboardButton(store.name, callback_data=f"store:switch:{store.id}")])
    rows.append([InlineKeyboardButton("Редактировать", callback_data="store:edit")])
    return InlineKeyboardMarkup(rows)


def store_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать", callback_data="store:create")],
            [InlineKeyboardButton("Переименовать", callback_data="store:rename")],
            [InlineKeyboardButton("Удалить", callback_data="store:remove")],
            [InlineKeyboardButton("Назад", callback_data="store:back")],
        ]
    )


def store_select_action_keyboard(stores: list[Store], action: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for store in stores:
        rows.append([InlineKeyboardButton(store.name, callback_data=f"store:{action}_pick:{store.id}")])
    rows.append([InlineKeyboardButton("Назад", callback_data="store:edit")])
    return InlineKeyboardMarkup(rows)


def store_screen_keyboard(store: Store) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Добавить", callback_data=f"store:adjust:add:{store.id}"),
                InlineKeyboardButton("Списать", callback_data=f"store:adjust:use:{store.id}"),
            ],
            [
                InlineKeyboardButton("Редактировать", callback_data=f"store:item_edit:{store.id}"),
                InlineKeyboardButton("Обновить", callback_data=f"store:refresh:{store.id}"),
            ],
            [InlineKeyboardButton("Назад", callback_data="store:back")],
        ]
    )


def store_adjust_items_keyboard(store: Store, items: list[InventoryItem], action: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if action == "add":
        rows.append([InlineKeyboardButton("Новая позиция", callback_data=f"store:add_new:{store.id}")])
    for item in items:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{item.name} — {format_quantity(item.quantity)} {item.unit}",
                    callback_data=f"store:adjust_item:{action}:{store.id}:{item.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Назад", callback_data=f"store:refresh:{store.id}")])
    return InlineKeyboardMarkup(rows)


def store_prompt_cancel_keyboard(store: Store) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data=f"store:cancel:{store.id}")]]
    )


def store_item_edit_keyboard(store: Store) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Переименовать", callback_data=f"store:item_action:rename:{store.id}"),
                InlineKeyboardButton("Минимум", callback_data=f"store:item_action:min:{store.id}"),
            ],
            [InlineKeyboardButton("Скрыть", callback_data=f"store:item_action:hide:{store.id}")],
            [InlineKeyboardButton("Назад", callback_data=f"store:refresh:{store.id}")],
        ]
    )


def store_item_action_keyboard(store: Store, items: list[InventoryItem], action: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{item.name} — {format_quantity(item.quantity)} {item.unit}",
                    callback_data=f"store:item_pick:{action}:{store.id}:{item.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Назад", callback_data=f"store:item_edit:{store.id}")])
    return InlineKeyboardMarkup(rows)


def hide_store_item_confirm_keyboard(store: Store, item: InventoryItem) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, скрыть", callback_data=f"store:item_hide_confirm:{store.id}:{item.id}"),
                InlineKeyboardButton("Отмена", callback_data=f"store:item_hide_cancel:{store.id}"),
            ]
        ]
    )


def remove_store_confirm_keyboard(store: Store) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, удалить", callback_data=f"store:remove_confirm:{store.id}"),
                InlineKeyboardButton("Отмена", callback_data=f"store:remove_cancel:{store.id}"),
            ]
        ]
    )


def item_keyboard(item: InventoryItem) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("-1", callback_data=f"item:dec:{item.id}:1"),
                InlineKeyboardButton("+1", callback_data=f"item:inc:{item.id}:1"),
            ],
            [
                InlineKeyboardButton("Купили", callback_data=f"item:bought:{item.id}"),
                InlineKeyboardButton("Минимум", callback_data=f"item:min:{item.id}"),
            ],
            [InlineKeyboardButton("Скрыть позицию", callback_data=f"item:hide:{item.id}")],
        ]
    )


def hide_item_confirm_keyboard(item: InventoryItem) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, скрыть", callback_data=f"item:hide_confirm:{item.id}"),
                InlineKeyboardButton("Отмена", callback_data=f"item:hide_cancel:{item.id}"),
            ]
        ]
    )


def format_item(item: InventoryItem) -> str:
    marker = "Нужно купить" if item.is_low else "В запасе"
    return (
        f"<b>{html.escape(item.name)}</b>\n\n"
        f"Количество: {format_quantity(item.quantity)} {html.escape(item.unit)}\n"
        f"Минимум: {format_quantity(item.min_quantity)} {html.escape(item.unit)}\n"
        f"Статус: {marker}"
    )


def format_stock(store: Store, items: list[InventoryItem]) -> str:
    if not items:
        return (
            f"<b>{html.escape(store.name)}</b>\n\n"
            "Склад пока пуст.\n\n"
            "Нажмите «Добавить» или напишите быстрым вводом:\n"
            "<code>+ Миг 8</code>\n"
            "<code>+ корм 4,5кг</code>"
        )
    lines = [f"<b>{html.escape(store.name)}</b>", ""]
    for item in items:
        prefix = "-" if item.is_low else "•"
        lines.append(
            f"{prefix} {html.escape(item.name)}: "
            f"{format_quantity(item.quantity)} {html.escape(item.unit)}"
            f"{format_minimum_suffix(item)}"
        )
    return "\n".join(lines)


def format_adjust_item_list(store: Store, items: list[InventoryItem], action: str) -> str:
    if not items:
        if action == "add":
            return (
                f"<b>{html.escape(store.name)}</b>\n\n"
                "Склад пока пуст. Нажмите «Новая позиция», чтобы добавить первый товар."
            )
        return (
            f"<b>{html.escape(store.name)}</b>\n\n"
            "Склад пока пуст. Списывать пока нечего."
        )
    if action == "add":
        return f"<b>{html.escape(store.name)}</b>\n\nВыберите позицию или добавьте новую:"
    action_text = "списать"
    return f"<b>{html.escape(store.name)}</b>\n\nВыберите позицию, которую нужно {action_text}:"


def format_create_item_prompt(store: Store) -> str:
    return (
        f"<b>{html.escape(store.name)}</b>\n\n"
        "Напишите название и количество новой позиции.\n\n"
        "<code>Миг 8</code>\n"
        "<code>корм 9кг мин 500г</code>"
    )


def format_adjust_prompt(item: InventoryItem, action: str) -> str:
    action_text = "добавить" if action == "add" else "списать"
    return (
        f"Сколько {action_text} у «{html.escape(item.name)}»?\n\n"
        f"Сейчас: {format_quantity(item.quantity)} {html.escape(item.unit)}.\n"
        f"Напишите количество, например: <code>2</code> или <code>2{html.escape(item.unit)}</code>."
    )


def format_item_edit_menu(store: Store) -> str:
    return f"<b>{html.escape(store.name)}</b>\n\nРедактирование позиций:"


def format_item_action_list(store: Store, items: list[InventoryItem], action: str) -> str:
    action_text = {
        "rename": "переименовать",
        "min": "минимум",
        "hide": "скрыть",
    }.get(action, "изменить")
    if not items:
        return f"<b>{html.escape(store.name)}</b>\n\nВ складе пока нет позиций."
    if action == "min":
        return f"<b>{html.escape(store.name)}</b>\n\nВыберите позицию, у которой нужно изменить минимум:"
    return f"<b>{html.escape(store.name)}</b>\n\nВыберите позицию, которую нужно {action_text}:"


def format_rename_item_prompt(item: InventoryItem) -> str:
    return f"Новое название для «{html.escape(item.name)}»:"


def format_minimum_prompt(item: InventoryItem) -> str:
    return (
        f"Минимум для «{html.escape(item.name)}».\n\n"
        f"Сейчас: {format_quantity(item.min_quantity)} {html.escape(item.unit)}.\n"
        f"Напишите новое значение, например: <code>1</code> или <code>500г</code>."
    )


def format_buy_list(household: Household, store: Store, items: list[InventoryItem]) -> str:
    if not items:
        return (
            f"<b>{html.escape(household.name)} / {html.escape(store.name)}</b>\n\n"
            "Покупать пока ничего не нужно."
        )
    lines = [f"<b>Купить для «{html.escape(household.name)} / {html.escape(store.name)}»</b>", ""]
    for item in items:
        lines.append(
            f"• {html.escape(item.name)}: {remaining_word(item.quantity, item.unit)} "
            f"{format_quantity(item.quantity)} {html.escape(item.unit)}"
        )
    return "\n".join(lines)


def format_buy_list_for_household(household: Household, groups: list[tuple[Store, list[InventoryItem]]]) -> str:
    if not groups:
        return f"<b>{html.escape(household.name)}</b>\n\nПокупать пока ничего не нужно."
    lines = [f"<b>Купить для «{html.escape(household.name)}»</b>"]
    for store, items in groups:
        lines.extend(["", f"<b>{html.escape(store.name)}</b>"])
        for item in items:
            lines.append(
                f"• {html.escape(item.name)}: {remaining_word(item.quantity, item.unit)} "
                f"{format_quantity(item.quantity)} {html.escape(item.unit)}"
            )
    return "\n".join(lines)


def format_minimum_suffix(item: InventoryItem) -> str:
    if item.min_quantity == 1:
        return ""
    return f" (min {format_quantity(item.min_quantity)})"


def remaining_word(quantity: float, unit: str) -> str:
    if quantity == 1:
        if unit in {"шт", "табл", "капс", "пачка"}:
            return "осталась"
        if unit in {"кг", "г", "л", "мл"}:
            return "остался"
    return "осталось"


def format_home_info(household: Household, members: list[object]) -> str:
    member_names = ", ".join(display_user(member) for member in members) or "пока только вы"
    return (
        f"Текущий дом: {html.escape(household.name)}\n"
        f"Участники: {html.escape(member_names)}\n"
        f"Код приглашения: <code>{household.invite_code}</code>"
    )


def help_text() -> str:
    return (
        "<b>Бот домашнего инвентаря</b>\n\n"
        "У каждого человека может быть несколько домов: ваш общий дом, родители, дача. "
        "Дом можно открыть другому человеку через код приглашения. "
        "Внутри дома можно создать несколько складов.\n\n"
        "<b>Работа со складом</b>\n"
        "Откройте склад и используйте кнопки «Добавить» и «Списать». "
        "Для существующей позиции достаточно выбрать её и написать только количество: "
        "<code>2</code>, <code>2табл</code>, <code>500г</code>.\n\n"
        "<b>Быстрый ввод</b>\n"
        "<code>+ Бумага 8</code> — добавить 8 штук\n"
        "<code>- Бумага 3</code> — списать 3 штуки\n\n"
        "<code>+ корм 9кг</code> — добавить вес\n"
        "<code>- корм 2,5кг</code> — списать вес\n"
        "\n"
        "<code>&#45;&#45; горох</code> — скрыть позицию, если покупать не надо\n"
        "<code>+ корм 9кг мин 500г</code> — добавить 9кг корма с минимумом 500г\n\n"
        "По умолчанию минимум равен 1: если осталось 1 или 0, позиция попадает в список «Купить». "
        "Когда количество становится меньше или равно минимуму, бот периодически напоминает участникам склада."
    )


def parse_amount_text(text: str) -> ParsedAmount:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        raise ValueError("Введите название и количество.")
    min_quantity: float | None = None
    min_unit: str | None = None
    min_match = re.search(
        r"\s+(?:мин|минимум|min)\s+(\d+(?:[.,]\d+)?)\s*([^\d\s]+)?\s*$",
        cleaned,
        re.IGNORECASE,
    )
    if min_match:
        min_quantity = parse_float(min_match.group(1))
        min_unit = normalize_unit(min_match.group(2)) if min_match.group(2) else None
        cleaned = cleaned[: min_match.start()].strip()

    tokens = cleaned.split()
    amount_index: int | None = None
    attached_unit: str | None = None
    for index, token in enumerate(tokens):
        if looks_like_number(token):
            amount_index = index
            break
        attached_match = re.fullmatch(r"(\d+(?:[.,]\d+)?)([^\d\s]+)", token, re.IGNORECASE)
        if attached_match:
            amount_index = index
            attached_unit = attached_match.group(2)
            tokens[index] = attached_match.group(1)
            break
    if amount_index is None:
        return ParsedAmount(cleaned, 1.0, "шт", min_quantity, min_unit)
    name = " ".join(tokens[:amount_index]).strip()
    if not name:
        raise ValueError("Сначала напишите название, потом количество.")
    quantity = parse_float(tokens[amount_index])
    unit_parts = []
    if attached_unit:
        unit_parts.append(attached_unit)
    unit_parts.extend(tokens[amount_index + 1 :])
    unit = normalize_unit(" ".join(unit_parts).strip() or "шт")
    return ParsedAmount(name, quantity, unit, min_quantity, min_unit)


def hidden_item_name(text: str) -> str | None:
    stripped = text.lstrip()
    if stripped.startswith("--"):
        return stripped[2:].strip()
    if stripped.startswith(("—", "–")):
        return stripped[1:].strip()
    return None


def looks_like_number(value: str) -> bool:
    try:
        parse_float(value)
        return True
    except ValueError:
        return False


def parse_float(value: str) -> float:
    result = float(value.replace(",", "."))
    if result < 0:
        raise ValueError("Количество не может быть отрицательным.")
    return result


def parse_quantity_with_optional_unit(text: str, default_unit: str) -> tuple[float, str]:
    cleaned = " ".join(text.strip().split())
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([^\d\s]+)?", cleaned, re.IGNORECASE)
    if not match:
        raise ValueError("Введите число, например: 1 или 500г")
    quantity = parse_float(match.group(1))
    unit = normalize_unit(match.group(2)) if match.group(2) else default_unit
    return quantity, unit


def normalize_unit(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if normalized in COMMON_UNIT_ALIASES:
        return COMMON_UNIT_ALIASES[normalized]
    return UNIT_ALIASES.get(normalized, UnitInfo("", 1.0, normalized)).canonical


def convert_quantity(quantity: float, from_unit: str, to_unit: str) -> float | None:
    from_info = UNIT_ALIASES.get(normalize_unit(from_unit))
    to_info = UNIT_ALIASES.get(normalize_unit(to_unit))
    if from_info is None or to_info is None:
        return quantity if normalize_unit(from_unit) == normalize_unit(to_unit) else None
    if from_info.group != to_info.group:
        return None
    return quantity * from_info.base_factor / to_info.base_factor


def existing_unit_quantity(
    item: InventoryItem,
    quantity: float,
    unit: str,
    *,
    fallback_to_raw: bool = False,
) -> float | None:
    converted = convert_quantity(quantity, unit, item.unit)
    if converted is not None:
        return converted
    return quantity if fallback_to_raw else None


def minimum_in_item_unit(parsed: ParsedAmount, item_unit: str) -> float | None:
    if parsed.min_quantity is None:
        return None
    source_unit = parsed.min_unit or parsed.unit
    converted = convert_quantity(parsed.min_quantity, source_unit, item_unit)
    return converted if converted is not None else parsed.min_quantity


async def edit_message_safely(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = ParseMode.HTML,
) -> bool:
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return True
        LOGGER.exception("Failed to edit message %s in chat %s", message_id, chat_id)
        return False
    except Exception:
        LOGGER.exception("Failed to edit message %s in chat %s", message_id, chat_id)
        return False


async def show_store_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    *,
    message_id: int | None = None,
) -> None:
    storage: Storage = context.application.bot_data["storage"]
    text = format_stock(store, storage.list_items(store.id))
    if message_id is not None and update.effective_chat is not None:
        edited = await edit_message_safely(
            context,
            update.effective_chat.id,
            message_id,
            text,
            reply_markup=store_screen_keyboard(store),
        )
        if edited:
            return
    await update.effective_message.reply_text(
        text,
        reply_markup=store_screen_keyboard(store),
        parse_mode=ParseMode.HTML,
    )


async def edit_state_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, object],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    message_id = state.get("message_id")
    if message_id is None or update.effective_chat is None:
        return False
    return await edit_message_safely(
        context,
        update.effective_chat.id,
        int(message_id),
        text,
        reply_markup=reply_markup,
    )


def state_message_id(state: dict[str, object]) -> int | None:
    value = state.get("message_id")
    return int(value) if value is not None else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    clear_state(context, user.chat_id)
    if household is None:
        await update.message.reply_text(
            "Привет. Дом пока не выбран.\n\n"
            "Можно создать новый дом или присоединиться к уже существующему по коду.",
            reply_markup=no_household_keyboard(),
        )
        return
    await update.message.reply_text(
        "Привет.\n\n"
        f"Дом: {household.name}\n"
        "Выберите склад в меню «Склады», а потом можно писать: + Миг 8 или + корм 4,5кг",
        reply_markup=main_menu(),
    )


async def show_stores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    if household is None:
        await prompt_no_household(update, context)
        return
    clear_state(context, user.chat_id)
    current_store = storage.current_store(user.chat_id)
    stores = storage.list_stores(household.id)
    await update.message.reply_text(
        f"Дом: {household.name}\n\nВыберите склад:",
        reply_markup=stores_keyboard(stores, current_store.id if current_store else None),
    )


async def show_store_items(update: Update, context: ContextTypes.DEFAULT_TYPE, store: Store) -> None:
    await show_store_screen(update, context, store)


async def show_buy_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    if household is None:
        await prompt_no_household(update, context)
        return
    clear_state(context, user.chat_id)
    await update.message.reply_text(
        format_buy_list_for_household(household, storage.low_items_by_store(household.id)),
        parse_mode=ParseMode.HTML,
    )


async def show_homes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    clear_state(context, user.chat_id)
    current = storage.current_household(user.chat_id)
    households = storage.list_households(user.chat_id)
    if current is None:
        await update.message.reply_text(
            "Дом пока не выбран.",
            reply_markup=no_household_keyboard(),
        )
        return
    members = storage.list_members(current.id)
    await update.message.reply_text(
        format_home_info(current, members),
        reply_markup=homes_keyboard(households, current.id),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    record_user(update, context)
    await update.message.reply_text(help_text(), reply_markup=main_menu(), parse_mode=ParseMode.HTML)


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    if not context.args:
        set_state(context, user.chat_id, {"mode": STATE_JOIN_HOUSEHOLD})
        await update.message.reply_text("Пришлите код приглашения.")
        return
    await join_by_code(update, context, " ".join(context.args))


async def new_house_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    name = " ".join(context.args).strip()
    if not name:
        set_state(context, user.chat_id, {"mode": STATE_CREATE_HOUSEHOLD})
        await update.message.reply_text("Как назвать новый дом? Например: Родители")
        return
    household = storage.create_household(user.chat_id, name)
    await update.message.reply_text(
        f"Создала дом «{household.name}» и переключилась на него.",
        reply_markup=main_menu(),
    )


async def rename_house_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    if household is None:
        await prompt_no_household(update, context)
        return
    name = " ".join(context.args).strip()
    if not name:
        set_state(context, user.chat_id, {"mode": STATE_RENAME_HOUSEHOLD, "household_id": household.id})
        await update.message.reply_text("Как назвать текущий дом?")
        return
    renamed = storage.rename_household(user.chat_id, household.id, name)
    if renamed is None:
        await update.message.reply_text("Не получилось переименовать дом.")
        return
    await update.message.reply_text(f"Готово, теперь это «{renamed.name}».", reply_markup=main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return
    text = update.message.text.strip()
    storage, user = record_user(update, context)

    if text == BTN_STOCK:
        await show_stores(update, context)
        return
    if text == BTN_BUY:
        await show_buy_list(update, context)
        return
    if text == BTN_HOMES:
        await show_homes(update, context)
        return
    if text == BTN_HELP:
        await help_command(update, context)
        return

    state = get_state(context, user.chat_id)
    if state and state.get("mode") == STATE_CREATE_HOUSEHOLD:
        clear_state(context, user.chat_id)
        created = storage.create_household(user.chat_id, text)
        await update.message.reply_text(
            f"Создала дом «{created.name}» и переключилась на него.",
            reply_markup=main_menu(),
        )
        return
    if state and state.get("mode") == STATE_RENAME_HOUSEHOLD:
        household = storage.current_household(user.chat_id)
        if household is None:
            await prompt_no_household(update, context)
            return
        clear_state(context, user.chat_id)
        household_id = int(state.get("household_id", household.id))
        renamed = storage.rename_household(user.chat_id, household_id, text)
        if renamed is None:
            await update.message.reply_text("Не получилось переименовать дом.")
            return
        await update.message.reply_text(f"Готово, теперь это «{renamed.name}».", reply_markup=main_menu())
        return
    if state and state.get("mode") == STATE_CREATE_STORE:
        clear_state(context, user.chat_id)
        household = storage.current_household(user.chat_id)
        if household is None:
            await prompt_no_household(update, context)
            return
        store = storage.create_store(user.chat_id, household.id, text)
        if store is None:
            await update.message.reply_text("Не получилось создать склад.")
            return
        await update.message.reply_text(f"Создала склад «{store.name}».")
        await show_store_items(update, context, store)
        return
    if state and state.get("mode") == STATE_RENAME_STORE:
        clear_state(context, user.chat_id)
        store_id = int(state["store_id"])
        store = storage.rename_store(user.chat_id, store_id, text)
        if store is None:
            await update.message.reply_text("Не получилось переименовать склад.")
            return
        await update.message.reply_text(f"Готово, склад теперь называется «{store.name}».")
        await show_store_items(update, context, store)
        return
    if state and state.get("mode") == STATE_JOIN_HOUSEHOLD:
        clear_state(context, user.chat_id)
        await join_by_code(update, context, text)
        return
    if state and state.get("mode") == STATE_CREATE_ITEM:
        if text.casefold() in {"отмена", "cancel"}:
            await cancel_store_state(update, context, state)
            return
        await create_item_from_text(update, context, state, text)
        return
    if state and state.get("mode") == STATE_RENAME_ITEM:
        if text.casefold() in {"отмена", "cancel"}:
            await cancel_store_state(update, context, state)
            return
        await rename_item_from_text(update, context, state, text)
        return
    if state and state.get("mode") == STATE_SET_MINIMUM:
        if text.casefold() in {"отмена", "cancel"}:
            await cancel_store_state(update, context, state)
            return
        store_id = state.get("store_id")
        store = storage.set_current_store(user.chat_id, int(store_id)) if store_id is not None else storage.current_store(user.chat_id)
        if store is None:
            await prompt_no_store(update, context)
            return
        await set_minimum_from_text(update, context, store, int(state["item_id"]), text, state)
        return
    if state and state.get("mode") == STATE_ADJUST_QUANTITY:
        if text.casefold() in {"отмена", "cancel"}:
            await cancel_store_state(update, context, state)
            return
        await adjust_item_from_text(update, context, state, text)
        return
    hidden_name = hidden_item_name(text)
    if hidden_name is not None:
        store = storage.current_store(user.chat_id)
        if store is None:
            await prompt_no_store(update, context)
            return
        try:
            await hide_item_from_text(update, context, store, hidden_name)
        except Exception:
            LOGGER.exception("Failed to hide inventory item")
            await update.message.reply_text("Не получилось скрыть позицию. Я записала ошибку в лог.")
        return
    if text.startswith("+"):
        store = storage.current_store(user.chat_id)
        if store is None:
            await prompt_no_store(update, context)
            return
        try:
            await add_item_from_text(update, context, store, text[1:].strip())
        except Exception:
            LOGGER.exception("Failed to add inventory item")
            await update.message.reply_text("Не получилось добавить позицию. Я записала ошибку в лог.")
        return
    if text.startswith("-"):
        store = storage.current_store(user.chat_id)
        if store is None:
            await prompt_no_store(update, context)
            return
        try:
            await use_item_from_text(update, context, store, text[1:].strip())
        except Exception:
            LOGGER.exception("Failed to use inventory item")
            await update.message.reply_text("Не получилось списать позицию. Я записала ошибку в лог.")
        return

    await update.message.reply_text(
        "Я не совсем понял. Для быстрого ввода используйте, например:\n"
        "<code>+ Миг 8</code>\n"
        "<code>+ Миг 8табл</code>\n"
        "<code>- Миг 2табл</code>\n"
        "<code>&#45;&#45; горох</code>",
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML,
    )


async def add_item_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    text: str,
) -> None:
    storage: Storage = context.application.bot_data["storage"]
    try:
        parsed = parse_amount_text(text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=main_menu())
        return
    existing = storage.find_item(store.id, parsed.name)
    if existing is not None:
        quantity = existing_unit_quantity(existing, parsed.quantity, parsed.unit, fallback_to_raw=True)
        minimum = minimum_in_item_unit(parsed, existing.unit)
        unit = existing.unit
    else:
        quantity = parsed.quantity
        minimum = minimum_in_item_unit(parsed, parsed.unit)
        unit = parsed.unit
    item = storage.add_or_update_item(
        store.id,
        update.effective_chat.id,
        parsed.name,
        quantity,
        unit,
        minimum,
    )
    await update.message.reply_text(
        f"Добавлено: {item.name} — {format_quantity(item.quantity)} {item.unit}."
    )


async def use_item_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    text: str,
) -> None:
    storage: Storage = context.application.bot_data["storage"]
    try:
        parsed = parse_amount_text(text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=main_menu())
        return
    item = storage.find_item(store.id, parsed.name)
    if item is None:
        await update.message.reply_text("Такой позиции не нашла в текущем складе.", reply_markup=main_menu())
        return
    quantity = existing_unit_quantity(item, parsed.quantity, parsed.unit)
    if quantity is None:
        await update.message.reply_text(
            f"Не могу списать {parsed.unit} из позиции, которая хранится в {item.unit}. "
            "Напишите количество в тех же единицах.",
            reply_markup=main_menu(),
        )
        return
    updated = storage.change_item_quantity(store.id, update.effective_chat.id, item.id, -quantity)
    await update.message.reply_text(
        f"Готово: {updated.name} — {format_quantity(updated.quantity)} {updated.unit}."
    )


async def hide_item_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    text: str,
) -> None:
    name = " ".join(text.split())
    if not name:
        await update.message.reply_text(
            "Напишите, что скрыть. Например: <code>&#45;&#45; горох</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    storage: Storage = context.application.bot_data["storage"]
    item = storage.find_item(store.id, name)
    if item is None:
        await update.message.reply_text("Такой позиции не нашла в текущем складе.")
        return
    if storage.deactivate_item(store.id, item.id):
        await update.message.reply_text(f"Скрыла: {item.name}.")
    else:
        await update.message.reply_text("Не получилось скрыть позицию.")


async def cancel_store_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, object],
) -> None:
    storage, user = record_user(update, context)
    clear_state(context, user.chat_id)
    store_id = state.get("store_id")
    store = storage.set_current_store(user.chat_id, int(store_id)) if store_id is not None else storage.current_store(user.chat_id)
    if store is None:
        await update.effective_message.reply_text("Отменено.", reply_markup=main_menu())
        return
    await show_store_screen(update, context, store, message_id=state_message_id(state))


async def create_item_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, object],
    text: str,
) -> None:
    storage, user = record_user(update, context)
    store = storage.set_current_store(user.chat_id, int(state["store_id"]))
    if store is None:
        clear_state(context, user.chat_id)
        await update.message.reply_text("Не получилось найти этот склад.", reply_markup=main_menu())
        return
    try:
        parsed = parse_amount_text(text)
    except ValueError as exc:
        edited = await edit_state_prompt(
            update,
            context,
            state,
            f"{format_create_item_prompt(store)}\n\n<b>{html.escape(str(exc))}</b>",
            reply_markup=store_prompt_cancel_keyboard(store),
        )
        if not edited:
            await update.message.reply_text(str(exc))
        return
    existing = storage.find_item(store.id, parsed.name)
    if existing is not None:
        quantity = existing_unit_quantity(existing, parsed.quantity, parsed.unit, fallback_to_raw=True)
        minimum = minimum_in_item_unit(parsed, existing.unit)
        unit = existing.unit
    else:
        quantity = parsed.quantity
        minimum = minimum_in_item_unit(parsed, parsed.unit)
        unit = parsed.unit
    storage.add_or_update_item(
        store.id,
        user.chat_id,
        parsed.name,
        quantity,
        unit,
        minimum,
    )
    clear_state(context, user.chat_id)
    await show_store_screen(update, context, store, message_id=state_message_id(state))


async def rename_item_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, object],
    text: str,
) -> None:
    storage, user = record_user(update, context)
    store = storage.set_current_store(user.chat_id, int(state["store_id"]))
    if store is None:
        clear_state(context, user.chat_id)
        await update.message.reply_text("Не получилось найти этот склад.", reply_markup=main_menu())
        return
    item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == int(state["item_id"])), None)
    if item is None:
        clear_state(context, user.chat_id)
        await update.message.reply_text("Позиция не найдена.", reply_markup=main_menu())
        return
    clean_name = " ".join(text.split())
    if not clean_name:
        edited = await edit_state_prompt(
            update,
            context,
            state,
            f"{format_rename_item_prompt(item)}\n\n<b>Название не должно быть пустым.</b>",
            reply_markup=store_prompt_cancel_keyboard(store),
        )
        if not edited:
            await update.message.reply_text("Название не должно быть пустым.")
        return
    renamed = storage.rename_item(store.id, user.chat_id, item.id, clean_name)
    if renamed is None:
        edited = await edit_state_prompt(
            update,
            context,
            state,
            f"{format_rename_item_prompt(item)}\n\n<b>Не получилось переименовать. Возможно, такая позиция уже есть.</b>",
            reply_markup=store_prompt_cancel_keyboard(store),
        )
        if not edited:
            await update.message.reply_text("Не получилось переименовать. Возможно, такая позиция уже есть.")
        return
    clear_state(context, user.chat_id)
    await show_store_screen(update, context, store, message_id=state_message_id(state))


async def set_minimum_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    item_id: int,
    text: str,
    state: dict[str, object] | None = None,
) -> None:
    storage: Storage = context.application.bot_data["storage"]
    item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
    if item is None:
        if state:
            clear_state(context, update.effective_chat.id)
        await update.message.reply_text("Позиция не найдена.")
        return
    try:
        quantity, unit = parse_quantity_with_optional_unit(text, item.unit)
    except ValueError as exc:
        if state:
            edited = await edit_state_prompt(
                update,
                context,
                state,
                f"{format_minimum_prompt(item)}\n\n<b>{html.escape(str(exc))}</b>",
                reply_markup=store_prompt_cancel_keyboard(store),
            )
            if edited:
                return
        await update.message.reply_text(str(exc))
        return
    minimum = convert_quantity(quantity, unit, item.unit)
    if minimum is None:
        error_text = f"Минимум для этой позиции нужно указать в {item.unit}."
        if state:
            edited = await edit_state_prompt(
                update,
                context,
                state,
                f"{format_minimum_prompt(item)}\n\n<b>{html.escape(error_text)}</b>",
                reply_markup=store_prompt_cancel_keyboard(store),
            )
            if edited:
                return
        await update.message.reply_text(error_text)
        return
    updated = storage.set_item_minimum(store.id, item_id, minimum)
    if updated is None:
        if state:
            clear_state(context, update.effective_chat.id)
        await update.message.reply_text("Позиция не найдена.")
        return
    if state:
        clear_state(context, update.effective_chat.id)
        await show_store_screen(update, context, store, message_id=state_message_id(state))
        return
    await update.message.reply_text(
        f"Минимум для «{updated.name}» теперь {format_quantity(updated.min_quantity)} {updated.unit}."
    )


async def adjust_item_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, object],
    text: str,
) -> None:
    storage, user = record_user(update, context)
    store_id = int(state["store_id"])
    item_id = int(state["item_id"])
    action = str(state["action"])
    store = storage.set_current_store(user.chat_id, store_id)
    if store is None:
        clear_state(context, user.chat_id)
        await update.message.reply_text("Не получилось найти этот склад.", reply_markup=main_menu())
        return
    item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
    if item is None:
        clear_state(context, user.chat_id)
        await update.message.reply_text("Позиция не найдена.", reply_markup=main_menu())
        return
    try:
        quantity, unit = parse_quantity_with_optional_unit(text, item.unit)
    except ValueError as exc:
        edited = await edit_state_prompt(
            update,
            context,
            state,
            f"{format_adjust_prompt(item, action)}\n\n<b>{html.escape(str(exc))}</b>",
            reply_markup=store_prompt_cancel_keyboard(store),
        )
        if not edited:
            await update.message.reply_text(str(exc))
        return
    converted = convert_quantity(quantity, unit, item.unit)
    if converted is None:
        error_text = f"Для этой позиции количество нужно указать в {item.unit}."
        edited = await edit_state_prompt(
            update,
            context,
            state,
            f"{format_adjust_prompt(item, action)}\n\n<b>{html.escape(error_text)}</b>",
            reply_markup=store_prompt_cancel_keyboard(store),
        )
        if not edited:
            await update.message.reply_text(error_text)
        return
    delta = converted if action == "add" else -converted
    updated = storage.change_item_quantity(store.id, user.chat_id, item.id, delta)
    clear_state(context, user.chat_id)
    if updated is None:
        await update.message.reply_text("Позиция не найдена.", reply_markup=main_menu())
        return
    await show_store_screen(update, context, store, message_id=state_message_id(state))


async def join_by_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    storage, user = record_user(update, context)
    household = storage.join_household(user.chat_id, code)
    if household is None:
        await update.message.reply_text("Код не найден. Проверьте символы и попробуйте ещё раз.")
        return
    await update.message.reply_text(
        f"Готово, вы присоединились к дому «{household.name}».",
        reply_markup=main_menu(),
    )


async def handle_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    storage, user = record_user(update, context)
    current = storage.current_household(user.chat_id)
    data = query.data or ""

    if data == "home:edit":
        if current is None:
            await query.message.reply_text("Сначала создайте дом или присоединитесь по коду.")
            return
        await query.edit_message_text("Редактирование дома:", reply_markup=home_edit_keyboard())
        return
    if data == "home:back":
        if current is None:
            await query.edit_message_text("Дом пока не выбран.", reply_markup=no_household_keyboard())
            return
        await query.edit_message_text(
            format_home_info(current, storage.list_members(current.id)),
            reply_markup=homes_keyboard(storage.list_households(user.chat_id), current.id),
            parse_mode=ParseMode.HTML,
        )
        return
    if data == "home:create":
        set_state(context, user.chat_id, {"mode": STATE_CREATE_HOUSEHOLD})
        await query.message.reply_text("Как назвать новый дом? Например: Родители")
        return
    if data == "home:rename":
        if current is None:
            await query.message.reply_text("Сначала создайте дом или присоединитесь по коду.")
            return
        set_state(context, user.chat_id, {"mode": STATE_RENAME_HOUSEHOLD, "household_id": current.id})
        await query.message.reply_text(f"Как назвать дом «{current.name}»?")
        return
    if data == "home:join":
        set_state(context, user.chat_id, {"mode": STATE_JOIN_HOUSEHOLD})
        await query.message.reply_text("Пришлите код приглашения.")
        return
    if data == "home:remove":
        if current is None:
            await query.message.reply_text("Сейчас нет выбранного дома.")
            return
        await query.message.reply_text(
            f"Точно удалить/выйти из дома «{current.name}»?",
            reply_markup=remove_household_confirm_keyboard(),
        )
        return
    if data == "home:remove_cancel":
        await query.message.reply_text("Отменено.")
        return
    if data == "home:remove_confirm":
        if current is None:
            await query.message.reply_text("Сейчас нет выбранного дома.")
            return
        action = storage.remove_household_for_user(user.chat_id, current.id)
        if action is None:
            await query.message.reply_text("Не получилось удалить/выйти из дома.")
            return
        new_current = storage.current_household(user.chat_id)
        if new_current is None:
            await query.message.reply_text(
                "Готово. Домов больше нет.",
                reply_markup=no_household_keyboard(),
            )
            return
        await query.message.reply_text(
            f"Готово. Сейчас выбран дом «{new_current.name}».",
            reply_markup=homes_keyboard(storage.list_households(user.chat_id), new_current.id),
        )
        return
    if data.startswith("home:switch:"):
        household_id = int(data.rsplit(":", 1)[1])
        household = storage.set_current_household(user.chat_id, household_id)
        if household is None:
            await query.message.reply_text("Не получилось переключиться на этот дом.")
            return
        households = storage.list_households(user.chat_id)
        await query.edit_message_text(
            format_home_info(household, storage.list_members(household.id)),
            reply_markup=homes_keyboard(households, household.id),
            parse_mode=ParseMode.HTML,
        )


async def handle_store_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    if household is None:
        await query.message.reply_text("Сначала создайте дом или присоединитесь по коду.")
        return
    data = query.data or ""

    if data.startswith("store:cancel:") or data.startswith("store:adjust_cancel:"):
        clear_state(context, user.chat_id)
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.edit_message_text("Не получилось найти этот склад.")
            return
        await query.edit_message_text(
            format_stock(store, storage.list_items(store.id)),
            reply_markup=store_screen_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:add_new:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось найти этот склад.")
            return
        set_state(
            context,
            user.chat_id,
            {
                "mode": STATE_CREATE_ITEM,
                "store_id": store.id,
                "message_id": query.message.message_id,
            },
        )
        await query.edit_message_text(
            format_create_item_prompt(store),
            reply_markup=store_prompt_cancel_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:adjust_item:"):
        parts = data.split(":")
        action = parts[2]
        store_id = int(parts[3])
        item_id = int(parts[4])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось найти этот склад.")
            return
        item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
        if item is None:
            await query.message.reply_text("Позиция не найдена.")
            return
        set_state(
            context,
            user.chat_id,
            {
                "mode": STATE_ADJUST_QUANTITY,
                "action": action,
                "store_id": store.id,
                "item_id": item.id,
                "message_id": query.message.message_id,
            },
        )
        await query.edit_message_text(
            format_adjust_prompt(item, action),
            reply_markup=store_prompt_cancel_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:adjust:"):
        parts = data.split(":")
        action = parts[2]
        store_id = int(parts[3])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось найти этот склад.")
            return
        items = storage.list_items(store.id)
        await query.edit_message_text(
            format_adjust_item_list(store, items, action),
            reply_markup=store_adjust_items_keyboard(store, items, action),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:item_hide_cancel:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.edit_message_text("Не получилось найти этот склад.")
            return
        await query.edit_message_text(
            format_item_edit_menu(store),
            reply_markup=store_item_edit_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:item_hide_confirm:"):
        parts = data.split(":")
        store_id = int(parts[2])
        item_id = int(parts[3])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.edit_message_text("Не получилось найти этот склад.")
            return
        if not storage.deactivate_item(store.id, item_id):
            await query.message.reply_text("Позиция не найдена.")
            return
        await query.edit_message_text(
            format_stock(store, storage.list_items(store.id)),
            reply_markup=store_screen_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:item_pick:"):
        parts = data.split(":")
        action = parts[2]
        store_id = int(parts[3])
        item_id = int(parts[4])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось найти этот склад.")
            return
        item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
        if item is None:
            await query.message.reply_text("Позиция не найдена.")
            return
        if action == "rename":
            set_state(
                context,
                user.chat_id,
                {
                    "mode": STATE_RENAME_ITEM,
                    "store_id": store.id,
                    "item_id": item.id,
                    "message_id": query.message.message_id,
                },
            )
            await query.edit_message_text(
                format_rename_item_prompt(item),
                reply_markup=store_prompt_cancel_keyboard(store),
                parse_mode=ParseMode.HTML,
            )
            return
        if action == "min":
            set_state(
                context,
                user.chat_id,
                {
                    "mode": STATE_SET_MINIMUM,
                    "store_id": store.id,
                    "item_id": item.id,
                    "message_id": query.message.message_id,
                },
            )
            await query.edit_message_text(
                format_minimum_prompt(item),
                reply_markup=store_prompt_cancel_keyboard(store),
                parse_mode=ParseMode.HTML,
            )
            return
        if action == "hide":
            await query.edit_message_text(
                f"Скрыть позицию «{html.escape(item.name)}»?",
                reply_markup=hide_store_item_confirm_keyboard(store, item),
                parse_mode=ParseMode.HTML,
            )
            return
    if data.startswith("store:item_action:"):
        parts = data.split(":")
        action = parts[2]
        store_id = int(parts[3])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось найти этот склад.")
            return
        items = storage.list_items(store.id)
        await query.edit_message_text(
            format_item_action_list(store, items, action),
            reply_markup=store_item_action_keyboard(store, items, action),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:item_edit:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось найти этот склад.")
            return
        await query.edit_message_text(
            format_item_edit_menu(store),
            reply_markup=store_item_edit_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data == "store:edit":
        await query.edit_message_text("Редактирование складов:", reply_markup=store_edit_keyboard())
        return
    if data == "store:back":
        stores = storage.list_stores(household.id)
        await query.edit_message_text(f"Дом: {household.name}\n\nВыберите склад:", reply_markup=stores_keyboard(stores, None))
        return
    if data == "store:create":
        set_state(context, user.chat_id, {"mode": STATE_CREATE_STORE})
        await query.message.reply_text("Как назвать новый склад? Например: Кухня")
        return
    if data == "store:rename":
        stores = storage.list_stores(household.id)
        if not stores:
            await query.message.reply_text("Сначала создайте склад.")
            return
        await query.edit_message_text(
            "Какой склад переименовать?",
            reply_markup=store_select_action_keyboard(stores, "rename"),
        )
        return
    if data.startswith("store:rename_pick:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось выбрать этот склад.")
            return
        set_state(context, user.chat_id, {"mode": STATE_RENAME_STORE, "store_id": store.id})
        await query.message.reply_text(f"Как назвать склад «{store.name}»?")
        return
    if data == "store:remove":
        stores = storage.list_stores(household.id)
        if not stores:
            await query.message.reply_text("Сначала выберите склад.")
            return
        await query.edit_message_text(
            "Какой склад удалить?",
            reply_markup=store_select_action_keyboard(stores, "remove"),
        )
        return
    if data.startswith("store:remove_pick:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось выбрать этот склад.")
            return
        await query.edit_message_text(
            f"Удалить склад «{html.escape(store.name)}»?",
            reply_markup=remove_store_confirm_keyboard(store),
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith("store:remove_cancel:"):
        await query.edit_message_text("Отменено.")
        return
    if data.startswith("store:remove_confirm:"):
        store_id = int(data.rsplit(":", 1)[1])
        removed = storage.remove_store(user.chat_id, store_id)
        if not removed:
            await query.edit_message_text("Не получилось удалить склад.")
            return
        stores = storage.list_stores(household.id)
        await query.edit_message_text("Склад удален.")
        await query.message.reply_text(
            f"Дом: {household.name}\n\nВыберите склад:",
            reply_markup=stores_keyboard(stores, None),
        )
        return
    if data.startswith("store:refresh:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось обновить этот склад.")
            return
        await edit_message_safely(
            context,
            query.message.chat_id,
            query.message.message_id,
            format_stock(store, storage.list_items(store.id)),
            reply_markup=store_screen_keyboard(store),
        )
        return
    if data.startswith("store:switch:"):
        store_id = int(data.rsplit(":", 1)[1])
        store = storage.set_current_store(user.chat_id, store_id)
        if store is None:
            await query.message.reply_text("Не получилось выбрать этот склад.")
            return
        await query.edit_message_text(
            format_stock(store, storage.list_items(store.id)),
            reply_markup=store_screen_keyboard(store),
            parse_mode=ParseMode.HTML,
        )


async def handle_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    storage, user = record_user(update, context)
    store = storage.current_store(user.chat_id)
    if store is None:
        await query.message.reply_text("Сначала выберите или создайте склад.")
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1]
    item_id = int(parts[2])

    if action == "view":
        item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
        if item is None:
            await query.message.reply_text("Позиция не найдена.")
            return
        await query.message.reply_text(format_item(item), reply_markup=item_keyboard(item), parse_mode=ParseMode.HTML)
        return

    if action in {"inc", "dec"}:
        amount = parse_float(parts[3]) if len(parts) > 3 else 1.0
        delta = amount if action == "inc" else -amount
        item = storage.change_item_quantity(store.id, user.chat_id, item_id, delta)
        if item is None:
            await query.message.reply_text("Позиция не найдена.")
            return
        await query.edit_message_text(format_item(item), reply_markup=item_keyboard(item), parse_mode=ParseMode.HTML)
        return

    if action == "bought":
        item = storage.change_item_quantity(store.id, user.chat_id, item_id, 1.0)
        if item is None:
            await query.message.reply_text("Позиция не найдена.")
            return
        await query.edit_message_text(format_item(item), reply_markup=item_keyboard(item), parse_mode=ParseMode.HTML)
        return

    if action == "min":
        set_state(context, user.chat_id, {"mode": STATE_SET_MINIMUM, "item_id": item_id})
        await query.message.reply_text("Введите новый минимум числом. Например: 1")
        return

    if action == "hide":
        item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
        if item is None:
            await query.message.reply_text("Позиция не найдена.")
            return
        await query.edit_message_text(
            f"Скрыть позицию «{item.name}»?",
            reply_markup=hide_item_confirm_keyboard(item),
        )
        return

    if action == "hide_cancel":
        item = next((candidate for candidate in storage.list_items(store.id) if candidate.id == item_id), None)
        if item is None:
            await query.edit_message_text("Позиция не найдена.")
            return
        await query.edit_message_text(format_item(item), reply_markup=item_keyboard(item), parse_mode=ParseMode.HTML)
        return

    if action == "hide_confirm":
        if storage.deactivate_item(store.id, item_id):
            await query.edit_message_text("Позиция скрыта.")
        else:
            await query.message.reply_text("Позиция не найдена.")
        return


async def send_reminders(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    now = datetime.now(settings.timezone)
    if now.hour not in settings.reminder_hours:
        return
    storage: Storage = application.bot_data["storage"]
    targets = storage.reminder_targets(settings.reminder_interval_hours)
    for household, store, items, members in targets:
        if not items or not members:
            continue
        text = format_buy_list(household, store, items)
        sent_to_anyone = False
        for member in members:
            try:
                await application.bot.send_message(
                    chat_id=member.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                sent_to_anyone = True
            except Exception:
                LOGGER.exception("Failed to send reminder to %s", member.chat_id)
            await asyncio.sleep(0.05)
        if sent_to_anyone:
            storage.mark_items_reminded([item.id for item in items])


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    scheduler = AsyncIOScheduler(timezone=str(settings.timezone))
    scheduler.add_job(
        send_reminders,
        "interval",
        seconds=settings.reminder_check_interval_seconds,
        args=[application],
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler


async def post_shutdown(application: Application) -> None:
    scheduler: AsyncIOScheduler | None = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)


def build_app(settings: Settings) -> Application:
    application = (
        Application.builder()
        .token(settings.token)
        .defaults(Defaults(do_quote=False))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["storage"] = Storage(settings.database_path)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stock", show_stores))
    application.add_handler(CommandHandler("buy", show_buy_list))
    application.add_handler(CommandHandler("homes", show_homes))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("newhome", new_house_command))
    application.add_handler(CommandHandler("renamehome", rename_house_command))
    application.add_handler(CallbackQueryHandler(handle_home_callback, pattern=r"^home:"))
    application.add_handler(CallbackQueryHandler(handle_store_callback, pattern=r"^store:"))
    application.add_handler(CallbackQueryHandler(handle_item_callback, pattern=r"^item:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def record_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[Storage, object]:
    if update.effective_chat is None or update.effective_user is None:
        raise RuntimeError("Update without chat/user")
    storage: Storage = context.application.bot_data["storage"]
    telegram_user = update.effective_user
    user = storage.record_user(
        chat_id=update.effective_chat.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name,
    )
    return storage, user


async def prompt_no_household(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_state(context, update.effective_chat.id)
    await update.message.reply_text(
        "Сначала нужно выбрать дом: создать новый или присоединиться по коду.",
        reply_markup=no_household_keyboard(),
    )


async def prompt_no_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    if household is None:
        await prompt_no_household(update, context)
        return
    clear_state(context, user.chat_id)
    stores = storage.list_stores(household.id)
    current_store = storage.current_store(user.chat_id)
    await update.message.reply_text(
        "Сначала выберите или создайте склад.",
        reply_markup=stores_keyboard(stores, current_store.id if current_store else None),
    )


def display_user(user: object) -> str:
    first_name = getattr(user, "first_name", None)
    username = getattr(user, "username", None)
    chat_id = getattr(user, "chat_id", "")
    if first_name:
        return first_name
    if username:
        return f"@{username}"
    return str(chat_id)


def set_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, value: dict[str, object]) -> None:
    context.application.bot_data.setdefault("user_state", {})[chat_id] = value


def get_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict[str, object] | None:
    return context.application.bot_data.setdefault("user_state", {}).get(chat_id)


def clear_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    context.application.bot_data.setdefault("user_state", {}).pop(chat_id, None)


def main() -> None:
    settings = load_settings()
    app = build_app(settings)
    LOGGER.info("Home warehouse bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
