from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from math import ceil

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from storage import ActiveView, Household, InventoryItem, Storage, Store, User, format_quantity


logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BTN_STOCK = "Склады"
BTN_BUY = "Купить"
BTN_HOMES = "Дом"
BTN_HELP = "Помощь"
MENU_BUTTONS = {BTN_STOCK, BTN_BUY, BTN_HOMES, BTN_HELP}
PAGE_SIZE = 10


@dataclass(frozen=True)
class Settings:
    token: str
    database_path: str


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
    return Settings(token=token, database_path=os.getenv("DATABASE_PATH", "home_warehouse_bot.sqlite3"))


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_STOCK), KeyboardButton(BTN_BUY)],
            [KeyboardButton(BTN_HOMES), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="+ кофе 1 или - кофе 1",
    )


def normalize_unit(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if normalized in COMMON_UNIT_ALIASES:
        return COMMON_UNIT_ALIASES[normalized]
    return UNIT_ALIASES.get(normalized, UnitInfo("", 1.0, normalized)).canonical


def convert_quantity(quantity: float, from_unit: str, to_unit: str) -> float | None:
    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)
    if source == target:
        return quantity
    from_info = UNIT_ALIASES.get(source)
    to_info = UNIT_ALIASES.get(target)
    if from_info is None or to_info is None or from_info.group != to_info.group:
        return None
    return quantity * from_info.base_factor / to_info.base_factor


def parse_float(value: str) -> float:
    result = float(value.replace(",", "."))
    if result < 0:
        raise ValueError("Количество не может быть отрицательным.")
    return result


def looks_like_number(value: str) -> bool:
    try:
        parse_float(value)
        return True
    except ValueError:
        return False


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
        match = re.fullmatch(r"(\d+(?:[.,]\d+)?)([^\d\s]+)", token, re.IGNORECASE)
        if match:
            amount_index = index
            attached_unit = match.group(2)
            tokens[index] = match.group(1)
            break
    if amount_index is None:
        return ParsedAmount(cleaned, 1.0, "шт", min_quantity, min_unit)
    name = " ".join(tokens[:amount_index]).strip()
    if not name:
        raise ValueError("Сначала напишите название, потом количество.")
    quantity = parse_float(tokens[amount_index])
    unit_parts = ([attached_unit] if attached_unit else []) + tokens[amount_index + 1 :]
    unit = normalize_unit(" ".join(unit_parts).strip() or "шт")
    return ParsedAmount(name, quantity, unit, min_quantity, min_unit)


def parse_quantity_with_optional_unit(text: str, default_unit: str) -> tuple[float, str]:
    cleaned = " ".join(text.strip().split())
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([^\d\s]+)?", cleaned, re.IGNORECASE)
    if not match:
        raise ValueError("Введите число, например: 1 или 500г.")
    quantity = parse_float(match.group(1))
    unit = normalize_unit(match.group(2)) if match.group(2) else default_unit
    return quantity, unit


def has_explicit_amount_unit(text: str) -> bool:
    cleaned = re.sub(
        r"\s+(?:мин|минимум|min)\s+\d+(?:[.,]\d+)?\s*[^\d\s]*\s*$",
        "",
        " ".join(text.strip().split()),
        flags=re.IGNORECASE,
    )
    tokens = cleaned.split()
    for index, token in enumerate(tokens):
        if looks_like_number(token):
            return index + 1 < len(tokens)
        if re.fullmatch(r"\d+(?:[.,]\d+)?[^\d\s]+", token, re.IGNORECASE):
            return True
    return False


def hidden_item_name(text: str) -> str | None:
    stripped = text.lstrip()
    if stripped.startswith("--"):
        return stripped[2:].strip()
    if stripped.startswith(("—", "–")):
        return stripped[1:].strip()
    return None


def minimum_in_item_unit(parsed: ParsedAmount, item_unit: str) -> float | None:
    if parsed.min_quantity is None:
        return None
    source_unit = parsed.min_unit or parsed.unit
    converted = convert_quantity(parsed.min_quantity, source_unit, item_unit)
    return converted if converted is not None else parsed.min_quantity


def remaining_word(quantity: float, unit: str) -> str:
    if quantity == 1:
        if unit in {"шт", "табл", "капс", "пачка"}:
            return "осталась"
        if unit in {"кг", "г", "л", "мл"}:
            return "остался"
    return "осталось"


def display_user(user: User) -> str:
    return user.first_name or (f"@{user.username}" if user.username else str(user.chat_id))


def sorted_items(items: list[InventoryItem]) -> list[InventoryItem]:
    return sorted(items, key=lambda item: (not item.is_low, item.normalized_name))


def page_slice(items: list[object], page: int) -> tuple[list[object], int, int]:
    pages = max(1, ceil(len(items) / PAGE_SIZE))
    safe_page = min(max(0, page), pages - 1)
    start = safe_page * PAGE_SIZE
    return items[start : start + PAGE_SIZE], safe_page, pages


def pagination_rows(prefix: str, page: int, pages: int) -> list[list[InlineKeyboardButton]]:
    if pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("Предыдущая", callback_data=f"{prefix}:{page - 1}"))
    if page + 1 < pages:
        row.append(InlineKeyboardButton("Следующая", callback_data=f"{prefix}:{page + 1}"))
    return [row]


def home_text(household: Household, members: list[User]) -> str:
    names = ", ".join(display_user(member) for member in members) or "пока только вы"
    return (
        f"Текущий дом: {html.escape(household.name)}\n"
        f"Участники: {html.escape(names)}\n"
        f"Код приглашения: <code>{household.invite_code}</code>"
    )


def help_text() -> str:
    return (
        "<b>Бот домашнего инвентаря</b>\n\n"
        "У каждого человека может быть несколько домов: общий дом, родители или дача. "
        "Дом можно открыть другому человеку через код приглашения. Внутри дома можно создать несколько складов.\n\n"
        "<b>Работа со складом</b>\n"
        "Откройте склад, выберите товар и используйте кнопки «Списать» или «Добавить». "
        "Для существующей позиции достаточно написать количество: <code>2</code>, <code>2табл</code> или <code>500г</code>.\n\n"
        "<b>Быстрый ввод</b>\n"
        "<code>+ Бумага 8</code> — добавить 8 штук\n"
        "<code>- Бумага 3</code> — списать 3 штуки\n"
        "<code>+ корм 9кг</code> — добавить вес\n"
        "<code>- корм 2,5кг</code> — списать вес\n"
        "<code>&#45;&#45; горох</code> — перенести позицию в архив\n"
        "<code>+ корм 9кг мин 500г</code> — добавить корм с минимумом 500г\n\n"
        "По умолчанию минимум равен 1. Если остаток меньше или равен минимуму, позиция появляется в разделе «Купить»."
    )


def storage_from(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def record_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[Storage, User]:
    if update.effective_chat is None or update.effective_user is None:
        raise RuntimeError("Update without chat or user")
    storage = storage_from(context)
    telegram_user = update.effective_user
    user = storage.record_user(
        update.effective_chat.id,
        telegram_user.username,
        telegram_user.first_name,
    )
    return storage, user


def accessible_item(storage: Storage, user: User, item_id: int) -> InventoryItem | None:
    item = storage.get_item(item_id)
    if item is None or not storage.is_household_member(item.household_id, user.chat_id):
        return None
    return item


def clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("flow", None)


def set_flow(context: ContextTypes.DEFAULT_TYPE, **values: object) -> dict[str, object]:
    flow = dict(values)
    context.user_data["flow"] = flow
    return flow


def current_flow(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object] | None:
    value = context.user_data.get("flow")
    return value if isinstance(value, dict) else None


async def edit_or_send_panel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    *,
    view_type: str,
    household_id: int | None = None,
    store_id: int | None = None,
    item_id: int | None = None,
    page: int = 0,
    message_id: int | None = None,
) -> int:
    storage = storage_from(context)
    resolved_message_id = message_id
    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                resolved_message_id = None
        except TelegramError:
            LOGGER.exception("Failed to edit panel %s in chat %s", message_id, chat_id)
            resolved_message_id = None
    if resolved_message_id is None:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        resolved_message_id = message.message_id
    storage.set_active_view(
        chat_id,
        resolved_message_id,
        view_type,
        household_id=household_id,
        store_id=store_id,
        item_id=item_id,
        page=page,
    )
    return resolved_message_id


async def edit_registered_panel(
    context: ContextTypes.DEFAULT_TYPE,
    view: ActiveView,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> bool:
    try:
        await context.bot.edit_message_text(
            chat_id=view.chat_id,
            message_id=view.message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return True
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return True
    except TelegramError:
        LOGGER.exception("Failed to refresh panel %s in chat %s", view.message_id, view.chat_id)
    storage_from(context).clear_active_view(view.chat_id, view.message_id)
    return False


def no_home_panel() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "Дом пока не выбран.\n\nСоздайте новый дом или присоединитесь по коду.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Создать дом", callback_data="v2:home_create")],
                [InlineKeyboardButton("Присоединиться по коду", callback_data="v2:home_join")],
            ]
        ),
    )


def homes_panel(storage: Storage, user: User) -> tuple[str, InlineKeyboardMarkup, Household | None]:
    current = storage.current_household(user.chat_id)
    if current is None:
        text, keyboard = no_home_panel()
        return text, keyboard, None
    rows: list[list[InlineKeyboardButton]] = []
    for household in storage.list_households(user.chat_id):
        marker = "• " if household.id == current.id else ""
        rows.append(
            [InlineKeyboardButton(f"{marker}{household.name}", callback_data=f"v2:home:{household.id}")]
        )
    rows.extend(
        [
            [InlineKeyboardButton("Редактировать", callback_data="v2:home_edit")],
            [InlineKeyboardButton("Присоединиться по коду", callback_data="v2:home_join")],
        ]
    )
    return home_text(current, storage.list_members(current.id)), InlineKeyboardMarkup(rows), current


def home_edit_panel(storage: Storage, user: User) -> tuple[str, InlineKeyboardMarkup, Household] | None:
    current = storage.current_household(user.chat_id)
    if current is None:
        return None
    role = storage.household_role(current.id, user.chat_id)
    remove_label = "Удалить дом" if role == "owner" else "Выйти из дома"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать дом", callback_data="v2:home_create")],
            [InlineKeyboardButton("Переименовать дом", callback_data="v2:home_rename")],
            [InlineKeyboardButton(remove_label, callback_data=f"v2:home_remove:{current.id}")],
            [InlineKeyboardButton("Назад", callback_data="v2:home_back")],
        ]
    )
    return f"<b>{html.escape(current.name)}</b>\n\nРедактирование дома:", keyboard, current


def stores_panel(storage: Storage, user: User) -> tuple[str, InlineKeyboardMarkup, Household] | None:
    household = storage.current_household(user.chat_id)
    if household is None:
        return None
    refreshed_user = storage.get_user(user.chat_id)
    quick_store = storage.get_store(refreshed_user.current_store_id) if refreshed_user else None
    quick_name = html.escape(quick_store.name) if quick_store and quick_store.household_id == household.id else "не выбран"
    rows: list[list[InlineKeyboardButton]] = []
    for store in storage.list_stores(household.id):
        low_count = len(storage.low_items(store.id))
        suffix = f" · купить {low_count}" if low_count else ""
        rows.append([InlineKeyboardButton(f"{store.name}{suffix}", callback_data=f"v2:store:{store.id}:0")])
    rows.append([InlineKeyboardButton("Редактировать", callback_data="v2:store_edit")])
    text = (
        f"Дом: {html.escape(household.name)}\n"
        f"Быстрый ввод: {quick_name}\n\n"
        "Выберите склад:"
    )
    return text, InlineKeyboardMarkup(rows), household


def store_edit_panel(household: Household) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"<b>{html.escape(household.name)}</b>\n\nРедактирование складов:",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Создать", callback_data="v2:store_create")],
                [InlineKeyboardButton("Переименовать", callback_data="v2:store_rename")],
                [InlineKeyboardButton("Удалить", callback_data="v2:store_delete")],
                [InlineKeyboardButton("Назад", callback_data="v2:stores_back")],
            ]
        ),
    )


def store_pick_panel(storage: Storage, household: Household, action: str) -> tuple[str, InlineKeyboardMarkup]:
    action_text = "переименовать" if action == "rename" else "удалить"
    rows = [
        [InlineKeyboardButton(store.name, callback_data=f"v2:store_{action}_pick:{store.id}")]
        for store in storage.list_stores(household.id)
    ]
    rows.append([InlineKeyboardButton("Назад", callback_data="v2:store_edit")])
    return (
        f"<b>{html.escape(household.name)}</b>\n\nВыберите склад, который нужно {action_text}:",
        InlineKeyboardMarkup(rows),
    )


def store_panel(storage: Storage, store: Store, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    items = sorted_items(storage.list_items(store.id))
    visible, safe_page, pages = page_slice(items, page)
    low_count = sum(1 for item in items if item.is_low)
    rows: list[list[InlineKeyboardButton]] = []
    for item in visible:
        marker = "−" if item.is_low else "•"
        label = f"{marker} {item.name} · {format_quantity(item.quantity)} {item.unit}"
        rows.append([InlineKeyboardButton(label, callback_data=f"v2:item:{item.id}:{safe_page}")])
    rows.extend(pagination_rows(f"v2:store:{store.id}", safe_page, pages))
    rows.extend(
        [
            [
                InlineKeyboardButton("Новая позиция", callback_data=f"v2:item_create:{store.id}:{safe_page}"),
                InlineKeyboardButton("Архив", callback_data=f"v2:archive:{store.id}:0"),
            ],
            [
                InlineKeyboardButton("Обновить", callback_data=f"v2:store:{store.id}:{safe_page}"),
                InlineKeyboardButton("Назад", callback_data="v2:stores_back"),
            ],
        ]
    )
    text = (
        f"<b>{html.escape(store.name)}</b>\n"
        f"Позиций: {len(items)} · купить: {low_count}"
    )
    if not items:
        text += "\n\nСклад пока пуст."
    return text, InlineKeyboardMarkup(rows), safe_page


def item_panel(item: InventoryItem, page: int) -> tuple[str, InlineKeyboardMarkup]:
    status = "нужно купить" if item.is_low else "в запасе"
    text = (
        f"<b>{html.escape(item.name)}</b>\n\n"
        f"Остаток: {format_quantity(item.quantity)} {html.escape(item.unit)}\n"
        f"Минимум: {format_quantity(item.min_quantity)} {html.escape(item.unit)}\n"
        f"Статус: {status}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("−1", callback_data=f"v2:delta:{item.id}:minus:{page}"),
                InlineKeyboardButton("+1", callback_data=f"v2:delta:{item.id}:plus:{page}"),
            ],
            [
                InlineKeyboardButton("Списать", callback_data=f"v2:adjust:{item.id}:use:{page}"),
                InlineKeyboardButton("Добавить", callback_data=f"v2:adjust:{item.id}:add:{page}"),
            ],
            [InlineKeyboardButton("Настройки", callback_data=f"v2:item_settings:{item.id}:{page}")],
            [InlineKeyboardButton("Назад", callback_data=f"v2:store:{item.store_id}:{page}")],
        ]
    )
    return text, keyboard


def item_settings_panel(item: InventoryItem, page: int) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"<b>{html.escape(item.name)}</b>\n\nНастройки позиции:",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Переименовать", callback_data=f"v2:item_rename:{item.id}:{page}"),
                    InlineKeyboardButton("Минимум", callback_data=f"v2:item_min:{item.id}:{page}"),
                ],
                [InlineKeyboardButton("Единица", callback_data=f"v2:item_unit:{item.id}:{page}")],
                [InlineKeyboardButton("В архив", callback_data=f"v2:item_archive:{item.id}:{page}")],
                [InlineKeyboardButton("Назад", callback_data=f"v2:item:{item.id}:{page}")],
            ]
        ),
    )


def archive_panel(storage: Storage, store: Store, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    items = sorted(
        [item for item in storage.list_items(store.id, include_inactive=True) if not item.active],
        key=lambda item: item.normalized_name,
    )
    visible, safe_page, pages = page_slice(items, page)
    rows = [
        [
            InlineKeyboardButton(
                f"{item.name} · {format_quantity(item.quantity)} {item.unit}",
                callback_data=f"v2:archived:{item.id}:{safe_page}",
            )
        ]
        for item in visible
    ]
    rows.extend(pagination_rows(f"v2:archive:{store.id}", safe_page, pages))
    rows.append([InlineKeyboardButton("Назад", callback_data=f"v2:store:{store.id}:0")])
    text = f"<b>{html.escape(store.name)}</b>\n\nАрхив"
    if not items:
        text += " пока пуст."
    return text, InlineKeyboardMarkup(rows), safe_page


def archived_item_panel(item: InventoryItem, page: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"<b>{html.escape(item.name)}</b>\n\n"
        f"Остаток: {format_quantity(item.quantity)} {html.escape(item.unit)}\n"
        f"Минимум: {format_quantity(item.min_quantity)} {html.escape(item.unit)}\n"
        "Статус: в архиве"
    )
    return (
        text,
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Восстановить", callback_data=f"v2:item_restore:{item.id}:{page}")],
                [InlineKeyboardButton("Назад", callback_data=f"v2:archive:{item.store_id}:{page}")],
            ]
        ),
    )


def buy_panel(storage: Storage, household: Household, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    entries: list[tuple[Store, InventoryItem]] = []
    for store, items in storage.low_items_by_store(household.id):
        entries.extend((store, item) for item in items)
    visible, safe_page, pages = page_slice(entries, page)
    rows: list[list[InlineKeyboardButton]] = []
    for store, item in visible:
        label = (
            f"{store.name} · {item.name} · {remaining_word(item.quantity, item.unit)} "
            f"{format_quantity(item.quantity)} {item.unit}"
        )
        rows.append([InlineKeyboardButton(label, callback_data=f"v2:buy_item:{item.id}:{safe_page}")])
    rows.extend(pagination_rows("v2:buy", safe_page, pages))
    rows.append([InlineKeyboardButton("Обновить", callback_data=f"v2:buy:{safe_page}")])
    text = f"<b>Купить для «{html.escape(household.name)}»</b>"
    if not entries:
        text += "\n\nПокупать пока ничего не нужно."
    return text, InlineKeyboardMarkup(rows), safe_page


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="v2:flow_cancel")]])


async def show_homes_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message_id: int | None = None,
) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    text, keyboard, household = homes_panel(storage, user)
    await edit_or_send_panel(
        context,
        user.chat_id,
        text,
        keyboard,
        view_type="home",
        household_id=household.id if household else None,
        message_id=message_id,
    )


async def show_stores_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message_id: int | None = None,
) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    panel = stores_panel(storage, user)
    if panel is None:
        await show_homes_panel(update, context, message_id=message_id)
        return
    text, keyboard, household = panel
    await edit_or_send_panel(
        context,
        user.chat_id,
        text,
        keyboard,
        view_type="stores",
        household_id=household.id,
        message_id=message_id,
    )


async def show_store_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store_id: int,
    page: int = 0,
    *,
    message_id: int | None = None,
) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    store = storage.set_current_store(user.chat_id, store_id)
    if store is None:
        await show_stores_panel(update, context, message_id=message_id)
        return
    text, keyboard, safe_page = store_panel(storage, store, page)
    await edit_or_send_panel(
        context,
        user.chat_id,
        text,
        keyboard,
        view_type="store",
        household_id=store.household_id,
        store_id=store.id,
        page=safe_page,
        message_id=message_id,
    )


async def show_item_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    item = accessible_item(storage, user, item_id)
    if item is None or not item.active or storage.set_current_store(user.chat_id, item.store_id) is None:
        await show_stores_panel(update, context, message_id=message_id)
        return
    text, keyboard = item_panel(item, page)
    await edit_or_send_panel(
        context,
        user.chat_id,
        text,
        keyboard,
        view_type="item",
        household_id=item.household_id,
        store_id=item.store_id,
        item_id=item.id,
        page=page,
        message_id=message_id,
    )


async def show_buy_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    *,
    message_id: int | None = None,
) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    household = storage.current_household(user.chat_id)
    if household is None:
        await show_homes_panel(update, context, message_id=message_id)
        return
    text, keyboard, safe_page = buy_panel(storage, household, page)
    await edit_or_send_panel(
        context,
        user.chat_id,
        text,
        keyboard,
        view_type="buy",
        household_id=household.id,
        page=safe_page,
        message_id=message_id,
    )


async def refresh_household_views(context: ContextTypes.DEFAULT_TYPE, household_id: int) -> None:
    storage = storage_from(context)
    for view in storage.active_views_for_household(household_id):
        if view.view_type == "prompt" or view.view_type == "home":
            continue
        text: str | None = None
        keyboard: InlineKeyboardMarkup | None = None
        new_type = view.view_type
        new_store_id = view.store_id
        new_item_id = view.item_id
        new_page = view.page

        if view.view_type == "stores":
            user = storage.get_user(view.chat_id)
            panel = stores_panel(storage, user) if user else None
            if panel:
                text, keyboard, _ = panel
        elif view.view_type == "store":
            store = storage.get_store(view.store_id or 0)
            if store:
                text, keyboard, new_page = store_panel(storage, store, view.page)
            else:
                user = storage.get_user(view.chat_id)
                panel = stores_panel(storage, user) if user else None
                if panel:
                    text, keyboard, _ = panel
                    new_type, new_store_id, new_item_id, new_page = "stores", None, None, 0
        elif view.view_type == "item":
            item = storage.get_item(view.item_id or 0)
            if item and item.active:
                text, keyboard = item_panel(item, view.page)
            else:
                store = storage.get_store(view.store_id or 0)
                if store:
                    text, keyboard, new_page = store_panel(storage, store, view.page)
                    new_type, new_item_id = "store", None
                else:
                    user = storage.get_user(view.chat_id)
                    panel = stores_panel(storage, user) if user else None
                    if panel:
                        text, keyboard, _ = panel
                        new_type, new_store_id, new_item_id, new_page = "stores", None, None, 0
        elif view.view_type == "buy":
            try:
                household = next(home for home in storage.list_households(view.chat_id) if home.id == household_id)
            except StopIteration:
                household = None
            if household:
                text, keyboard, new_page = buy_panel(storage, household, view.page)
        elif view.view_type == "archive":
            store = storage.get_store(view.store_id or 0)
            if store:
                text, keyboard, new_page = archive_panel(storage, store, view.page)
            else:
                user = storage.get_user(view.chat_id)
                panel = stores_panel(storage, user) if user else None
                if panel:
                    text, keyboard, _ = panel
                    new_type, new_store_id, new_item_id, new_page = "stores", None, None, 0

        if text is None:
            storage.clear_active_view(view.chat_id, view.message_id)
            continue
        if await edit_registered_panel(context, view, text, keyboard):
            storage.set_active_view(
                view.chat_id,
                view.message_id,
                new_type,
                household_id=household_id,
                store_id=new_store_id,
                item_id=new_item_id,
                page=new_page,
            )


async def show_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    message_id: int,
    household_id: int | None = None,
    store_id: int | None = None,
    item_id: int | None = None,
) -> int:
    resolved_message_id = await edit_or_send_panel(
        context,
        update.effective_chat.id,
        text,
        cancel_keyboard(),
        view_type="prompt",
        household_id=household_id,
        store_id=store_id,
        item_id=item_id,
        message_id=message_id,
    )
    flow = current_flow(context)
    if flow is not None and int(flow.get("message_id", 0)) == message_id:
        flow["message_id"] = resolved_message_id
        context.user_data["flow"] = flow
    return resolved_message_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    storage.clear_active_view(user.chat_id)
    household = storage.current_household(user.chat_id)
    if household is None:
        text, keyboard = no_home_panel()
        message = await update.message.reply_text(text, reply_markup=keyboard)
        storage.set_active_view(user.chat_id, message.message_id, "home")
        return
    await update.message.reply_text(
        f"Дом: {html.escape(household.name)}\n\nВыберите раздел в меню.",
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    storage.clear_active_view(user.chat_id)
    await update.message.reply_text(help_text(), reply_markup=main_menu(), parse_mode=ParseMode.HTML)


async def new_home_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Укажите название: /newhome Родители")
        return
    household = storage.create_household(user.chat_id, name)
    storage.clear_active_view(user.chat_id)
    await update.message.reply_text(
        f"Дом «{html.escape(household.name)}» создан.",
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML,
    )


async def rename_home_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    household = storage.current_household(user.chat_id)
    name = " ".join(context.args).strip()
    if household is None:
        await show_homes_panel(update, context)
        return
    if not name:
        await update.message.reply_text("Укажите название: /renamehome Наш дом")
        return
    renamed = storage.rename_household(user.chat_id, household.id, name)
    await update.message.reply_text(f"Дом переименован в «{html.escape(renamed.name)}».", parse_mode=ParseMode.HTML)


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    code = "".join(context.args).strip()
    if not code:
        await update.message.reply_text("Укажите код: /join GE8PNCC")
        return
    household = storage.join_household(user.chat_id, code)
    if household is None:
        await update.message.reply_text("Код не найден. Проверьте символы и попробуйте ещё раз.")
        return
    storage.clear_active_view(user.chat_id)
    await update.message.reply_text(
        f"Вы присоединились к дому «{html.escape(household.name)}».",
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML,
    )


async def begin_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    prompt: str,
    *,
    message_id: int,
    household_id: int | None = None,
    store_id: int | None = None,
    item_id: int | None = None,
    page: int = 0,
    return_view: str = "",
    action: str = "",
    onboarding: bool = False,
) -> None:
    set_flow(
        context,
        mode=mode,
        message_id=message_id,
        household_id=household_id,
        store_id=store_id,
        item_id=item_id,
        page=page,
        return_view=return_view,
        action=action,
        onboarding=onboarding,
    )
    await show_prompt(
        update,
        context,
        prompt,
        message_id=message_id,
        household_id=household_id,
        store_id=store_id,
        item_id=item_id,
    )


async def restore_flow_parent(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict[str, object]) -> None:
    message_id = int(flow.get("message_id", 0)) or None
    return_view = str(flow.get("return_view", ""))
    page = int(flow.get("page", 0))
    if return_view == "item" and flow.get("item_id") is not None:
        await show_item_panel(update, context, int(flow["item_id"]), page, message_id=message_id)
    elif return_view == "store" and flow.get("store_id") is not None:
        await show_store_panel(update, context, int(flow["store_id"]), page, message_id=message_id)
    elif return_view == "buy":
        await show_buy_panel(update, context, page, message_id=message_id)
    elif return_view == "home":
        await show_homes_panel(update, context, message_id=message_id)
    else:
        await show_stores_panel(update, context, message_id=message_id)


async def handle_flow_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    storage: Storage,
    user: User,
    flow: dict[str, object],
    text: str,
) -> None:
    mode = str(flow["mode"])
    message_id = int(flow["message_id"])
    household_id = int(flow["household_id"]) if flow.get("household_id") is not None else None
    store_id = int(flow["store_id"]) if flow.get("store_id") is not None else None
    item_id = int(flow["item_id"]) if flow.get("item_id") is not None else None
    page = int(flow.get("page", 0))

    if mode == "home_create":
        name = " ".join(text.split())
        if not name:
            await show_prompt(update, context, "Название не может быть пустым.", message_id=message_id)
            return
        household = storage.create_household(user.chat_id, name)
        clear_flow(context)
        if bool(flow.get("onboarding")):
            storage.clear_active_view(user.chat_id)
            await update.message.reply_text(
                f"Дом «{html.escape(household.name)}» создан.",
                reply_markup=main_menu(),
                parse_mode=ParseMode.HTML,
            )
        else:
            await show_homes_panel(update, context, message_id=message_id)
        return

    if mode == "home_join":
        household = storage.join_household(user.chat_id, text)
        if household is None:
            await show_prompt(update, context, "Код не найден. Проверьте символы и попробуйте ещё раз.", message_id=message_id)
            return
        clear_flow(context)
        if bool(flow.get("onboarding")):
            storage.clear_active_view(user.chat_id)
            await update.message.reply_text(
                f"Вы присоединились к дому «{html.escape(household.name)}».",
                reply_markup=main_menu(),
                parse_mode=ParseMode.HTML,
            )
        else:
            await show_homes_panel(update, context, message_id=message_id)
        return

    if mode == "home_rename" and household_id is not None:
        renamed = storage.rename_household(user.chat_id, household_id, text)
        if renamed is None:
            await show_prompt(update, context, "Введите непустое название дома.", message_id=message_id)
            return
        clear_flow(context)
        await show_homes_panel(update, context, message_id=message_id)
        return

    if mode == "store_create" and household_id is not None:
        try:
            store = storage.create_store(user.chat_id, household_id, text)
        except Exception:
            store = None
        if store is None:
            await show_prompt(update, context, "Не получилось создать склад. Возможно, такое название уже есть.", message_id=message_id)
            return
        clear_flow(context)
        await show_store_panel(update, context, store.id, 0, message_id=message_id)
        return

    if mode == "store_rename" and store_id is not None:
        try:
            store = storage.rename_store(user.chat_id, store_id, text)
        except Exception:
            store = None
        if store is None:
            await show_prompt(update, context, "Не получилось переименовать склад. Проверьте название.", message_id=message_id)
            return
        clear_flow(context)
        await show_stores_panel(update, context, message_id=message_id)
        return

    if mode == "item_create" and store_id is not None:
        store = storage.get_store(store_id)
        if store is None or not storage.is_household_member(store.household_id, user.chat_id):
            clear_flow(context)
            await show_stores_panel(update, context, message_id=message_id)
            return
        try:
            parsed = parse_amount_text(text)
        except ValueError as exc:
            await show_prompt(update, context, f"<b>{html.escape(str(exc))}</b>\n\nНапример: <code>Миг 8табл</code>", message_id=message_id, household_id=store.household_id, store_id=store.id)
            return
        existing = next(
            (candidate for candidate in storage.list_items(store.id, include_inactive=True) if candidate.normalized_name == " ".join(parsed.name.casefold().replace("ё", "е").split())),
            None,
        )
        if existing is not None:
            location = "в архиве" if not existing.active else "на складе"
            await show_prompt(update, context, f"Позиция «{html.escape(existing.name)}» уже есть {location}.", message_id=message_id, household_id=store.household_id, store_id=store.id)
            return
        minimum = minimum_in_item_unit(parsed, parsed.unit)
        item = storage.add_or_update_item(store.id, user.chat_id, parsed.name, parsed.quantity, parsed.unit, minimum)
        clear_flow(context)
        await show_item_panel(update, context, item.id, page, message_id=message_id)
        await refresh_household_views(context, item.household_id)
        return

    if item_id is None:
        clear_flow(context)
        await show_stores_panel(update, context, message_id=message_id)
        return
    item = accessible_item(storage, user, item_id)
    if item is None:
        clear_flow(context)
        await show_stores_panel(update, context, message_id=message_id)
        return

    if mode in {"item_adjust", "buy_adjust"}:
        try:
            quantity, unit = parse_quantity_with_optional_unit(text, item.unit)
        except ValueError as exc:
            await show_prompt(update, context, f"<b>{html.escape(str(exc))}</b>\n\nСейчас: {format_quantity(item.quantity)} {html.escape(item.unit)}.", message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        converted = convert_quantity(quantity, unit, item.unit)
        if converted is None:
            await show_prompt(update, context, f"Для этой позиции количество нужно указать в {html.escape(item.unit)}.", message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        action = str(flow.get("action", "add"))
        delta = converted if action == "add" else -converted
        updated = storage.change_item_quantity(item.store_id, user.chat_id, item.id, delta)
        clear_flow(context)
        if updated is None:
            await show_stores_panel(update, context, message_id=message_id)
            return
        if mode == "buy_adjust":
            await show_buy_panel(update, context, page, message_id=message_id)
        else:
            await show_item_panel(update, context, item.id, page, message_id=message_id)
        await refresh_household_views(context, item.household_id)
        return

    if mode == "item_rename":
        renamed = storage.rename_item(item.store_id, user.chat_id, item.id, text)
        if renamed is None:
            await show_prompt(update, context, "Название пустое или уже занято другой позицией.", message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        clear_flow(context)
        await show_item_panel(update, context, item.id, page, message_id=message_id)
        await refresh_household_views(context, item.household_id)
        return

    if mode == "item_min":
        try:
            quantity, unit = parse_quantity_with_optional_unit(text, item.unit)
        except ValueError as exc:
            await show_prompt(update, context, html.escape(str(exc)), message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        converted = convert_quantity(quantity, unit, item.unit)
        if converted is None:
            await show_prompt(update, context, f"Минимум нужно указать в {html.escape(item.unit)}.", message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        storage.set_item_minimum(item.store_id, item.id, converted, user.chat_id)
        clear_flow(context)
        await show_item_panel(update, context, item.id, page, message_id=message_id)
        await refresh_household_views(context, item.household_id)
        return

    if mode == "item_unit":
        new_unit = normalize_unit(text)
        if not new_unit:
            await show_prompt(update, context, "Введите единицу измерения.", message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        converted_quantity = convert_quantity(item.quantity, item.unit, new_unit)
        converted_minimum = convert_quantity(item.min_quantity, item.unit, new_unit)
        if converted_quantity is not None and converted_minimum is not None:
            storage.change_item_unit(item.store_id, user.chat_id, item.id, new_unit, converted_quantity, converted_minimum)
            clear_flow(context)
            await show_item_panel(update, context, item.id, page, message_id=message_id)
            await refresh_household_views(context, item.household_id)
            return
        flow["new_unit"] = new_unit
        context.user_data["flow"] = flow
        confirm_text = (
            f"Сменить единицу у «{html.escape(item.name)}» с {html.escape(item.unit)} на {html.escape(new_unit)}?\n\n"
            f"Остаток {format_quantity(item.quantity)} и минимум {format_quantity(item.min_quantity)} сохранятся без пересчёта."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Да, изменить", callback_data="v2:unit_confirm"),
                    InlineKeyboardButton("Отмена", callback_data="v2:flow_cancel"),
                ]
            ]
        )
        await edit_or_send_panel(
            context,
            user.chat_id,
            confirm_text,
            keyboard,
            view_type="prompt",
            household_id=item.household_id,
            store_id=item.store_id,
            item_id=item.id,
            message_id=message_id,
        )


async def handle_delta(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    storage, user = record_user(update, context)
    parts = data.split(":")
    item_id = int(parts[2])
    delta = -1.0 if parts[3] == "minus" else 1.0
    item = accessible_item(storage, user, item_id)
    if item is None or not item.active:
        await query.answer("Позиция больше недоступна.", show_alert=True)
        await show_stores_panel(update, context, message_id=query.message.message_id)
        return
    updated = storage.change_item_quantity(item.store_id, user.chat_id, item.id, delta)
    if updated is None:
        await query.answer("Не получилось изменить остаток.", show_alert=True)
        return
    await query.answer(f"Остаток: {format_quantity(updated.quantity)} {updated.unit}")
    await refresh_household_views(context, item.household_id)


async def handle_v2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if data.startswith("v2:delta:"):
        await handle_delta(update, context, data)
        return
    await query.answer()
    storage, user = record_user(update, context)
    message_id = query.message.message_id

    try:
        if data == "v2:flow_cancel":
            flow = current_flow(context)
            if flow:
                clear_flow(context)
                await restore_flow_parent(update, context, flow)
            else:
                await show_stores_panel(update, context, message_id=message_id)
            return

        if data == "v2:unit_confirm":
            flow = current_flow(context)
            if not flow or flow.get("item_id") is None or not flow.get("new_unit"):
                await show_stores_panel(update, context, message_id=message_id)
                return
            item = accessible_item(storage, user, int(flow["item_id"]))
            if item is None:
                clear_flow(context)
                await show_stores_panel(update, context, message_id=message_id)
                return
            storage.change_item_unit(
                item.store_id,
                user.chat_id,
                item.id,
                str(flow["new_unit"]),
                item.quantity,
                item.min_quantity,
            )
            page = int(flow.get("page", 0))
            clear_flow(context)
            await show_item_panel(update, context, item.id, page, message_id=message_id)
            await refresh_household_views(context, item.household_id)
            return

        if data == "v2:home_back":
            await show_homes_panel(update, context, message_id=message_id)
            return
        if data.startswith("v2:home:"):
            household_id = int(data.split(":")[2])
            if storage.set_current_household(user.chat_id, household_id) is None:
                await show_homes_panel(update, context, message_id=message_id)
                return
            await show_homes_panel(update, context, message_id=message_id)
            return
        if data == "v2:home_edit":
            panel = home_edit_panel(storage, user)
            if panel is None:
                await show_homes_panel(update, context, message_id=message_id)
                return
            text, keyboard, household = panel
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="home",
                household_id=household.id,
                message_id=message_id,
            )
            return
        if data == "v2:home_create":
            current = storage.current_household(user.chat_id)
            await begin_flow(
                update,
                context,
                "home_create",
                "Как назвать новый дом?\n\nНапример: <code>Родители</code>",
                message_id=message_id,
                household_id=current.id if current else None,
                return_view="home",
                onboarding=current is None,
            )
            return
        if data == "v2:home_join":
            current = storage.current_household(user.chat_id)
            await begin_flow(
                update,
                context,
                "home_join",
                "Введите код приглашения.",
                message_id=message_id,
                household_id=current.id if current else None,
                return_view="home",
                onboarding=current is None,
            )
            return
        if data == "v2:home_rename":
            household = storage.current_household(user.chat_id)
            if household is None:
                await show_homes_panel(update, context, message_id=message_id)
                return
            await begin_flow(
                update,
                context,
                "home_rename",
                f"Новое название для «{html.escape(household.name)}»: ",
                message_id=message_id,
                household_id=household.id,
                return_view="home",
            )
            return
        if data.startswith("v2:home_remove:"):
            household_id = int(data.split(":")[2])
            household = storage.current_household(user.chat_id)
            if household is None or household.id != household_id:
                await show_homes_panel(update, context, message_id=message_id)
                return
            role = storage.household_role(household.id, user.chat_id)
            if role == "owner":
                prompt = (
                    f"Удалить дом «{html.escape(household.name)}»?\n\n"
                    "Все склады и товары этого дома будут удалены без возможности восстановления."
                )
                label = "Да, удалить"
            else:
                prompt = f"Выйти из дома «{html.escape(household.name)}»?"
                label = "Да, выйти"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(label, callback_data=f"v2:home_remove_yes:{household.id}"),
                        InlineKeyboardButton("Отмена", callback_data="v2:home_edit"),
                    ]
                ]
            )
            await edit_or_send_panel(
                context,
                user.chat_id,
                prompt,
                keyboard,
                view_type="home",
                household_id=household.id,
                message_id=message_id,
            )
            return
        if data.startswith("v2:home_remove_yes:"):
            household_id = int(data.split(":")[2])
            storage.remove_household_for_user(user.chat_id, household_id)
            await show_homes_panel(update, context, message_id=message_id)
            return

        if data in {"v2:stores_back", "v2:stores"}:
            await show_stores_panel(update, context, message_id=message_id)
            return
        if data.startswith("v2:store:"):
            parts = data.split(":")
            await show_store_panel(update, context, int(parts[2]), int(parts[3]), message_id=message_id)
            return
        if data == "v2:store_edit":
            household = storage.current_household(user.chat_id)
            if household is None:
                await show_homes_panel(update, context, message_id=message_id)
                return
            text, keyboard = store_edit_panel(household)
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="stores",
                household_id=household.id,
                message_id=message_id,
            )
            return
        if data == "v2:store_create":
            household = storage.current_household(user.chat_id)
            if household is None:
                await show_homes_panel(update, context, message_id=message_id)
                return
            await begin_flow(
                update,
                context,
                "store_create",
                "Как назвать новый склад?",
                message_id=message_id,
                household_id=household.id,
                return_view="stores",
            )
            return
        if data in {"v2:store_rename", "v2:store_delete"}:
            household = storage.current_household(user.chat_id)
            if household is None:
                await show_homes_panel(update, context, message_id=message_id)
                return
            action = "rename" if data.endswith("rename") else "delete"
            text, keyboard = store_pick_panel(storage, household, action)
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="stores",
                household_id=household.id,
                message_id=message_id,
            )
            return
        if data.startswith("v2:store_rename_pick:"):
            store = storage.get_store(int(data.split(":")[2]))
            if store is None or not storage.is_household_member(store.household_id, user.chat_id):
                await show_stores_panel(update, context, message_id=message_id)
                return
            await begin_flow(
                update,
                context,
                "store_rename",
                f"Новое название для «{html.escape(store.name)}»: ",
                message_id=message_id,
                household_id=store.household_id,
                store_id=store.id,
                return_view="stores",
            )
            return
        if data.startswith("v2:store_delete_pick:"):
            store = storage.get_store(int(data.split(":")[2]))
            if store is None or not storage.is_household_member(store.household_id, user.chat_id):
                await show_stores_panel(update, context, message_id=message_id)
                return
            count = len(storage.list_items(store.id, include_inactive=True))
            text = (
                f"Удалить склад «{html.escape(store.name)}»?\n\n"
                f"Будет удалено позиций: {count}. Восстановить их будет нельзя."
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, удалить", callback_data=f"v2:store_delete_yes:{store.id}"),
                        InlineKeyboardButton("Отмена", callback_data="v2:store_edit"),
                    ]
                ]
            )
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="stores",
                household_id=store.household_id,
                message_id=message_id,
            )
            return
        if data.startswith("v2:store_delete_yes:"):
            store = storage.get_store(int(data.split(":")[2]))
            if store:
                storage.remove_store(user.chat_id, store.id)
                await show_stores_panel(update, context, message_id=message_id)
                await refresh_household_views(context, store.household_id)
            else:
                await show_stores_panel(update, context, message_id=message_id)
            return

        if data.startswith("v2:item_create:"):
            parts = data.split(":")
            store = storage.get_store(int(parts[2]))
            page = int(parts[3])
            if store is None or not storage.is_household_member(store.household_id, user.chat_id):
                await show_stores_panel(update, context, message_id=message_id)
                return
            await begin_flow(
                update,
                context,
                "item_create",
                "Напишите название и количество новой позиции.\n\n"
                "<code>Миг 8табл</code>\n"
                "<code>корм 9кг мин 500г</code>",
                message_id=message_id,
                household_id=store.household_id,
                store_id=store.id,
                page=page,
                return_view="store",
            )
            return
        if data.startswith("v2:item:"):
            parts = data.split(":")
            await show_item_panel(update, context, int(parts[2]), int(parts[3]), message_id=message_id)
            return
        if data.startswith("v2:adjust:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            action = parts[3]
            page = int(parts[4])
            if item is None:
                await show_stores_panel(update, context, message_id=message_id)
                return
            action_text = "добавить" if action == "add" else "списать"
            await begin_flow(
                update,
                context,
                "item_adjust",
                f"Сколько {action_text} у «{html.escape(item.name)}»?\n\n"
                f"Сейчас: {format_quantity(item.quantity)} {html.escape(item.unit)}.\n"
                f"Напишите <code>2</code> или <code>2{html.escape(item.unit)}</code>.",
                message_id=message_id,
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                return_view="item",
                action=action,
            )
            return
        if data.startswith("v2:item_settings:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item is None:
                await show_stores_panel(update, context, message_id=message_id)
                return
            text, keyboard = item_settings_panel(item, page)
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="item",
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                message_id=message_id,
            )
            return
        if data.startswith(("v2:item_rename:", "v2:item_min:", "v2:item_unit:")):
            parts = data.split(":")
            action = parts[1]
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item is None:
                await show_stores_panel(update, context, message_id=message_id)
                return
            prompts = {
                "item_rename": f"Новое название для «{html.escape(item.name)}»: ",
                "item_min": (
                    f"Новый минимум для «{html.escape(item.name)}».\n\n"
                    f"Сейчас: {format_quantity(item.min_quantity)} {html.escape(item.unit)}."
                ),
                "item_unit": (
                    f"Новая единица для «{html.escape(item.name)}».\n\n"
                    f"Сейчас: {html.escape(item.unit)}."
                ),
            }
            await begin_flow(
                update,
                context,
                action,
                prompts[action],
                message_id=message_id,
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                return_view="item",
            )
            return
        if data.startswith("v2:item_archive:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item is None:
                await show_stores_panel(update, context, message_id=message_id)
                return
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, в архив", callback_data=f"v2:item_archive_yes:{item.id}:{page}"),
                        InlineKeyboardButton("Отмена", callback_data=f"v2:item_settings:{item.id}:{page}"),
                    ]
                ]
            )
            await edit_or_send_panel(
                context,
                user.chat_id,
                f"Перенести «{html.escape(item.name)}» в архив?",
                keyboard,
                view_type="item",
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                message_id=message_id,
            )
            return
        if data.startswith("v2:item_archive_yes:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item:
                storage.archive_item(item.store_id, user.chat_id, item.id)
                await show_store_panel(update, context, item.store_id, page, message_id=message_id)
                await refresh_household_views(context, item.household_id)
            else:
                await show_stores_panel(update, context, message_id=message_id)
            return
        if data.startswith("v2:archive:"):
            parts = data.split(":")
            store = storage.get_store(int(parts[2]))
            if store is None or not storage.is_household_member(store.household_id, user.chat_id):
                await show_stores_panel(update, context, message_id=message_id)
                return
            text, keyboard, page = archive_panel(storage, store, int(parts[3]))
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="archive",
                household_id=store.household_id,
                store_id=store.id,
                page=page,
                message_id=message_id,
            )
            return
        if data.startswith("v2:archived:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item is None or item.active:
                await show_stores_panel(update, context, message_id=message_id)
                return
            text, keyboard = archived_item_panel(item, page)
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="archive",
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                message_id=message_id,
            )
            return
        if data.startswith("v2:item_restore:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item:
                restored = storage.restore_item(item.store_id, user.chat_id, item.id)
                if restored:
                    await show_item_panel(update, context, restored.id, 0, message_id=message_id)
                    await refresh_household_views(context, restored.household_id)
                    return
            await show_stores_panel(update, context, message_id=message_id)
            return

        if data.startswith("v2:buy:"):
            await show_buy_panel(update, context, int(data.split(":")[2]), message_id=message_id)
            return
        if data.startswith("v2:buy_item:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            if item is None or not item.active:
                await show_buy_panel(update, context, page, message_id=message_id)
                return
            await begin_flow(
                update,
                context,
                "buy_adjust",
                f"Сколько купили у «{html.escape(item.name)}»?\n\n"
                f"Сейчас: {format_quantity(item.quantity)} {html.escape(item.unit)}.\n"
                f"Напишите <code>2</code> или <code>2{html.escape(item.unit)}</code>.",
                message_id=message_id,
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                return_view="buy",
                action="add",
            )
            return
    except (ValueError, IndexError):
        LOGGER.exception("Invalid callback data: %s", data)

    await show_stores_panel(update, context, message_id=message_id)


async def quick_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    storage: Storage,
    user: User,
    store: Store,
    text: str,
) -> None:
    try:
        parsed = parse_amount_text(text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    existing = storage.find_item(store.id, parsed.name)
    if existing:
        quantity = (
            convert_quantity(parsed.quantity, parsed.unit, existing.unit)
            if has_explicit_amount_unit(text)
            else parsed.quantity
        )
        if quantity is None:
            await update.message.reply_text(
                f"Склад «{store.name}»: для «{existing.name}» количество нужно указать в {existing.unit}."
            )
            return
        minimum = minimum_in_item_unit(parsed, existing.unit)
        item = storage.add_or_update_item(
            store.id,
            user.chat_id,
            existing.name,
            quantity,
            existing.unit,
            minimum,
        )
    else:
        minimum = minimum_in_item_unit(parsed, parsed.unit)
        item = storage.add_or_update_item(
            store.id,
            user.chat_id,
            parsed.name,
            parsed.quantity,
            parsed.unit,
            minimum,
        )
    await update.message.reply_text(
        f"{store.name} · {item.name}: {format_quantity(item.quantity)} {item.unit}."
    )
    await refresh_household_views(context, store.household_id)


async def quick_use(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    storage: Storage,
    user: User,
    store: Store,
    text: str,
) -> None:
    try:
        parsed = parse_amount_text(text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    item = storage.find_item(store.id, parsed.name)
    if item is None:
        await update.message.reply_text(f"Склад «{store.name}»: такая позиция не найдена.")
        return
    quantity = (
        convert_quantity(parsed.quantity, parsed.unit, item.unit)
        if has_explicit_amount_unit(text)
        else parsed.quantity
    )
    if quantity is None:
        await update.message.reply_text(
            f"Склад «{store.name}»: для «{item.name}» количество нужно указать в {item.unit}."
        )
        return
    updated = storage.change_item_quantity(store.id, user.chat_id, item.id, -quantity)
    await update.message.reply_text(
        f"{store.name} · {updated.name}: {format_quantity(updated.quantity)} {updated.unit}."
    )
    await refresh_household_views(context, store.household_id)


async def quick_archive(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    storage: Storage,
    user: User,
    store: Store,
    name: str,
) -> None:
    if not name:
        await update.message.reply_text("Напишите название после двух минусов. Например: -- горох")
        return
    item = storage.find_item(store.id, name)
    if item is None:
        await update.message.reply_text(f"Склад «{store.name}»: такая позиция не найдена.")
        return
    archived = storage.archive_item(store.id, user.chat_id, item.id)
    if archived is None:
        await update.message.reply_text(f"Склад «{store.name}»: не получилось перенести позицию в архив.")
        return
    await update.message.reply_text(f"{store.name} · {item.name}: перенесено в архив.")
    await refresh_household_views(context, store.household_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage, user = record_user(update, context)
    text = (update.message.text or "").strip()

    if text == BTN_STOCK:
        await show_stores_panel(update, context)
        return
    if text == BTN_BUY:
        await show_buy_panel(update, context)
        return
    if text == BTN_HOMES:
        await show_homes_panel(update, context)
        return
    if text == BTN_HELP:
        await help_command(update, context)
        return

    flow = current_flow(context)
    if flow:
        await handle_flow_text(update, context, storage, user, flow, text)
        return

    hidden_name = hidden_item_name(text)
    if hidden_name is not None:
        store = storage.current_store(user.chat_id)
        if store is None:
            await show_stores_panel(update, context)
            return
        await quick_archive(update, context, storage, user, store, hidden_name)
        return
    if text.startswith("+"):
        store = storage.current_store(user.chat_id)
        if store is None:
            await show_stores_panel(update, context)
            return
        await quick_add(update, context, storage, user, store, text[1:].strip())
        return
    if text.startswith("-"):
        store = storage.current_store(user.chat_id)
        if store is None:
            await show_stores_panel(update, context)
            return
        await quick_use(update, context, storage, user, store, text[1:].strip())
        return

    await update.message.reply_text(
        "Используйте кнопки меню или быстрый ввод:\n"
        "<code>+ Миг 8табл</code>\n"
        "<code>- Миг 2табл</code>\n"
        "<code>&#45;&#45; горох</code>",
        parse_mode=ParseMode.HTML,
    )


async def handle_legacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    await query.answer("Экран обновлён")
    storage, user = record_user(update, context)
    message_id = query.message.message_id
    if data.startswith("home:"):
        await show_homes_panel(update, context, message_id=message_id)
        return
    if data.startswith("item:"):
        parts = data.split(":")
        item_id: int | None = None
        if len(parts) > 2 and parts[2].isdigit():
            item_id = int(parts[2])
        if item_id is not None:
            item = storage.get_item(item_id)
            if item and item.active and storage.set_current_store(user.chat_id, item.store_id):
                await show_item_panel(update, context, item.id, 0, message_id=message_id)
                return
    store = storage.current_store(user.chat_id)
    if store:
        await show_store_panel(update, context, store.id, 0, message_id=message_id)
    else:
        await show_stores_panel(update, context, message_id=message_id)


def build_app(settings: Settings) -> Application:
    application = (
        Application.builder()
        .token(settings.token)
        .defaults(Defaults(do_quote=False))
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["storage"] = Storage(settings.database_path)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stock", show_stores_panel))
    application.add_handler(CommandHandler("buy", show_buy_panel))
    application.add_handler(CommandHandler("homes", show_homes_panel))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("newhome", new_home_command))
    application.add_handler(CommandHandler("renamehome", rename_home_command))
    application.add_handler(CallbackQueryHandler(handle_v2_callback, pattern=r"^v2:"))
    application.add_handler(CallbackQueryHandler(handle_legacy_callback, pattern=r"^(home|store|item):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def main() -> None:
    settings = load_settings()
    build_app(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
