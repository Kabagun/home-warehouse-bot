from __future__ import annotations

import html
import logging
import os
import sqlite3
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from amounts import (
    COMMON_UNIT_ALIASES, UNIT_ALIASES, ParsedAmount, UnitInfo, convert_quantity,
    has_explicit_amount_unit, hidden_item_name, looks_like_number, minimum_in_item_unit,
    normalize_unit, parse_amount_list, parse_amount_text, parse_float, parse_quantity_with_optional_unit,
)
from inventory_service import InventoryService
import panels as ui
from panels import (
    BTN_STOCK, BTN_BUY, BTN_HOMES, BTN_HELP, MENU_BUTTONS, PAGE_SIZE,
    archived_item_panel, cancel_keyboard, display_user, help_text, home_text,
    item_create_keyboard, item_create_prompt, item_panel, item_settings_panel,
    main_menu, no_home_panel, page_slice, pagination_rows, remaining_word, sorted_items,
    store_edit_panel,
)
from storage import ActiveView, Household, InventoryItem, Storage, Store, User, format_quantity, normalize_name


logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Settings:
    token: str
    database_path: str


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Укажите TELEGRAM_BOT_TOKEN в .env или переменных окружения.")
    return Settings(token=token, database_path=os.getenv("DATABASE_PATH", "home_warehouse_bot.sqlite3"))


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


def homes_panel(storage: Storage, user: User) -> tuple[str, InlineKeyboardMarkup, Household | None]:
    current = storage.current_household(user.chat_id)
    return ui.homes_panel(current, storage.list_households(user.chat_id), storage.list_members(current.id) if current else [])


def home_edit_panel(storage: Storage, user: User) -> tuple[str, InlineKeyboardMarkup, Household] | None:
    current = storage.current_household(user.chat_id)
    return ui.home_edit_panel(current, storage.household_role(current.id, user.chat_id) if current else None)


def stores_panel(storage: Storage, user: User) -> tuple[str, InlineKeyboardMarkup, Household] | None:
    household = storage.current_household(user.chat_id)
    if household is None:
        return None
    refreshed = storage.get_user(user.chat_id)
    quick_store = storage.get_store(refreshed.current_store_id) if refreshed and refreshed.current_store_id else None
    stores = storage.list_stores(household.id)
    return ui.stores_panel(household, stores, quick_store, {store.id: len(storage.low_items(store.id)) for store in stores})


def store_pick_panel(storage: Storage, household: Household, action: str) -> tuple[str, InlineKeyboardMarkup]:
    return ui.store_pick_panel(household, storage.list_stores(household.id), action)


def store_panel(
    storage: Storage, store: Store, page: int, household: Household | None = None
) -> tuple[str, InlineKeyboardMarkup, int]:
    return ui.store_panel(store, storage.list_items(store.id), page, household or storage.get_household(store.household_id))


def archive_panel(
    storage: Storage, store: Store, page: int, household: Household | None = None
) -> tuple[str, InlineKeyboardMarkup, int]:
    return ui.archive_panel(
        store, storage.list_items(store.id, include_inactive=True), page,
        household or storage.get_household(store.household_id),
    )


def buy_panel(storage: Storage, household: Household, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    entries = [(store, item) for store, items in storage.low_items_by_store(household.id) for item in items]
    return ui.buy_panel(household, entries, page)


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
    text, keyboard, safe_page = store_panel(storage, store, page, storage.current_household(user.chat_id))
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
    text, keyboard = item_panel(item, page, storage.get_store(item.store_id), storage.current_household(user.chat_id))
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


async def show_store_settings_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store_id: int,
    *,
    message_id: int | None = None,
) -> None:
    storage, user = record_user(update, context)
    clear_flow(context)
    store = storage.set_current_store(user.chat_id, store_id)
    if store is None:
        await show_stores_panel(update, context, message_id=message_id)
        return
    text, keyboard = ui.store_settings_panel(store, storage.current_household(user.chat_id))
    await edit_or_send_panel(
        context,
        user.chat_id,
        text,
        keyboard,
        view_type="store_settings",
        household_id=store.household_id,
        store_id=store.id,
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
        if not storage.is_household_member(household_id, view.chat_id):
            storage.clear_active_view(view.chat_id, view.message_id)
            continue
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
        elif view.view_type in {"store", "store_settings"}:
            store = storage.get_store(view.store_id or 0)
            if store:
                if view.view_type == "store_settings":
                    text, keyboard = ui.store_settings_panel(store, storage.get_household(store.household_id))
                else:
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
                text, keyboard = item_panel(
                    item, view.page, storage.get_store(item.store_id), storage.get_household(item.household_id)
                )
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
    keyboard: InlineKeyboardMarkup | None = None,
) -> int:
    flow = current_flow(context)
    if keyboard is None:
        destinations = {
            "item": "к позиции", "store": "к складу", "stores": "к складам",
            "buy": "к покупкам", "home": "к домам", "store_settings": "к настройкам склада",
        }
        destination = destinations.get(str(flow.get("return_view", "")), "") if flow else ""
        keyboard = (
            item_create_keyboard() if flow and flow.get("mode") == "item_create"
            else cancel_keyboard(f"Отмена · {destination}" if destination else "Отмена")
        )
    storage = storage_from(context)
    if household_id is not None and storage.is_household_member(household_id, update.effective_chat.id):
        household = storage.get_household(household_id)
        store = storage.get_store(store_id) if store_id is not None else None
        if store and store.household_id == household_id:
            text = f"{ui.store_context(store, household)}\n\n{text}"
        elif household:
            text = f"Дом: {html.escape(household.name)}\n\n{text}"
    resolved_message_id = await edit_or_send_panel(
        context,
        update.effective_chat.id,
        text,
        keyboard,
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


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Отправьте /id в личном чате с ботом.")
        return
    _, user = record_user(update, context)
    await update.message.reply_text(f"Ваш ID: {user.chat_id}")


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
    elif return_view == "store_settings" and flow.get("store_id") is not None:
        await show_store_settings_panel(update, context, int(flow["store_id"]), message_id=message_id)
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
            store = InventoryService(storage, user.chat_id).create_store(household_id, text)
        except ValueError as exc:
            await show_prompt(
                update, context, html.escape(str(exc)), message_id=message_id, household_id=household_id
            )
            return
        clear_flow(context)
        await show_store_panel(update, context, store.id, 0, message_id=message_id)
        return

    if mode == "store_rename" and store_id is not None:
        try:
            store = storage.rename_store(user.chat_id, store_id, text)
        except sqlite3.IntegrityError:
            store = None
        if store is None:
            await show_prompt(update, context, "Не получилось переименовать склад. Проверьте название.", message_id=message_id)
            return
        clear_flow(context)
        await show_store_settings_panel(update, context, store.id, message_id=message_id)
        return

    if mode == "item_create" and store_id is not None:
        store = storage.get_store(store_id)
        if store is None or not storage.is_household_member(store.household_id, user.chat_id):
            clear_flow(context)
            await show_stores_panel(update, context, message_id=message_id)
            return
        parsed_lines, problems = parse_amount_list(text)
        existing_items = {
            item.normalized_name: item
            for item in storage.list_items(store.id, include_inactive=True)
        }
        added_names: list[str] = []
        for line_number, parsed in parsed_lines:
            normalized_name = normalize_name(parsed.name)
            existing = existing_items.get(normalized_name)
            if existing is not None:
                location = "в архиве" if not existing.active else "на складе"
                problems.append((line_number, f"«{existing.name}» уже есть {location}."))
                continue
            try:
                item = InventoryService(storage, user.chat_id).create_item(
                    store.id, parsed.name, parsed.quantity, parsed.unit, parsed.min_quantity, parsed.min_unit
                )
            except ValueError as exc:
                problems.append((line_number, str(exc)))
                continue
            existing_items[item.normalized_name] = item
            added_names.append(item.name)
        await show_prompt(
            update,
            context,
            item_create_prompt(store, added_names, sorted(problems)),
            message_id=message_id,
            household_id=store.household_id,
            store_id=store.id,
            keyboard=item_create_keyboard(),
        )
        if added_names:
            await refresh_household_views(context, store.household_id)
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
            action = str(flow.get("action", "add"))
            delta = quantity if action == "add" else -quantity
            InventoryService(storage, user.chat_id).change_quantity(item.id, delta, unit)
        except ValueError as exc:
            await show_prompt(update, context, f"<b>{html.escape(str(exc))}</b>\n\nСейчас: {format_quantity(item.quantity)} {html.escape(item.unit)}.", message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
        clear_flow(context)
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
            InventoryService(storage, user.chat_id).set_minimum(item.id, quantity, unit)
        except ValueError as exc:
            await show_prompt(update, context, html.escape(str(exc)), message_id=message_id, household_id=item.household_id, store_id=item.store_id, item_id=item.id)
            return
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
    source_unit: str | None = None
    try:
        item_id = int(parts[2])
        if data.startswith("v2:step:") and len(parts) == 6:
            delta = float(parts[3])
            page = int(parts[5])
        elif data.startswith("v2:delta:") and len(parts) == 5 and parts[3] in {"minus", "plus"}:
            delta = -1.0 if parts[3] == "minus" else 1.0
            page = int(parts[4])
        else:
            raise ValueError("Invalid quantity button")
        if not 0 < item_id < 2**63 or not 0 <= page < 2**31:
            raise ValueError("Invalid quantity button")
    except (ValueError, IndexError):
        await query.answer("Кнопка устарела. Откройте товар заново.", show_alert=True)
        return
    item = accessible_item(storage, user, item_id)
    if item is None or not item.active:
        await query.answer("Позиция больше недоступна.", show_alert=True)
        await show_stores_panel(update, context, message_id=query.message.message_id)
        return
    if data.startswith("v2:step:"):
        tag = parts[4]
        source_unit = {"g": "г", "kg": "кг", "ml": "мл", "l": "л"}.get(tag)
        if source_unit is None and tag == ui.step_unit_tag(item.unit):
            source_unit = item.unit
        if source_unit is None or convert_quantity(delta, source_unit, item.unit) is None:
            await query.answer("Единица измерения изменилась. Кнопки обновлены.", show_alert=True)
            await show_item_panel(update, context, item.id, page, message_id=query.message.message_id)
            return
    try:
        updated = InventoryService(storage, user.chat_id).change_quantity(item.id, delta, source_unit)
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer(f"Остаток: {format_quantity(updated.quantity)} {updated.unit}")
    await refresh_household_views(context, item.household_id)


async def handle_v2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if data == "v2:page_info":
        await query.answer()
        return
    if data.startswith(("v2:delta:", "v2:step:")):
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
        if data.startswith("v2:store_settings:"):
            await show_store_settings_panel(update, context, int(data.split(":")[2]), message_id=message_id)
            return
        if data == "v2:store_create" or data.startswith("v2:store_create_kind:"):
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
                return_view="store_settings",
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
                        InlineKeyboardButton("Отмена", callback_data=f"v2:store_settings:{store.id}:0"),
                    ]
                ]
            )
            await edit_or_send_panel(
                context,
                user.chat_id,
                text,
                keyboard,
                view_type="prompt",
                household_id=store.household_id,
                store_id=store.id,
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
                item_create_prompt(store),
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
            text, keyboard = item_settings_panel(
                item, page, storage.get_store(item.store_id), storage.get_household(item.household_id)
            )
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
        if data.startswith("v2:item_delete:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            return_view = parts[4]
            if item is None:
                await show_stores_panel(update, context, message_id=message_id)
                return
            cancel_callback = (
                f"v2:archived:{item.id}:{page}"
                if return_view == "archive"
                else f"v2:item_settings:{item.id}:{page}"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, удалить",
                            callback_data=f"v2:item_delete_yes:{item.id}:{page}:{return_view}",
                        ),
                        InlineKeyboardButton("Отмена", callback_data=cancel_callback),
                    ]
                ]
            )
            await edit_or_send_panel(
                context,
                user.chat_id,
                f"Удалить позицию «{html.escape(item.name)}»?\n\n"
                "Она будет удалена без возможности восстановления.",
                keyboard,
                view_type="archive" if return_view == "archive" else "item",
                household_id=item.household_id,
                store_id=item.store_id,
                item_id=item.id,
                page=page,
                message_id=message_id,
            )
            return
        if data.startswith("v2:item_delete_yes:"):
            parts = data.split(":")
            item = accessible_item(storage, user, int(parts[2]))
            page = int(parts[3])
            return_view = parts[4]
            if item is None:
                await show_stores_panel(update, context, message_id=message_id)
                return
            household_id = item.household_id
            store_id = item.store_id
            if not storage.delete_item(store_id, user.chat_id, item.id):
                await show_stores_panel(update, context, message_id=message_id)
                return
            if return_view == "archive":
                store = storage.get_store(store_id)
                if store is None:
                    await show_stores_panel(update, context, message_id=message_id)
                else:
                    text, keyboard, safe_page = archive_panel(storage, store, page)
                    await edit_or_send_panel(
                        context,
                        user.chat_id,
                        text,
                        keyboard,
                        view_type="archive",
                        household_id=household_id,
                        store_id=store_id,
                        page=safe_page,
                        message_id=message_id,
                    )
            else:
                await show_store_panel(update, context, store_id, page, message_id=message_id)
            await refresh_household_views(context, household_id)
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
            text, keyboard = archived_item_panel(
                item, page, storage.get_store(item.store_id), storage.get_household(item.household_id)
            )
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
                f"Сколько купили: «{html.escape(item.name)}»?\n\n"
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
        item = InventoryService(storage, user.chat_id).add_item(
            store.id, parsed.name, parsed.quantity,
            parsed.unit if has_explicit_amount_unit(text) else None,
            parsed.min_quantity, parsed.min_unit,
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text(
        f"{store.name} · {item.name}: {format_quantity(item.quantity)} {item.unit}.",
        parse_mode=None,
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
        service = InventoryService(storage, user.chat_id)
        item = service.find_item(store.id, parsed.name)
        if item is None:
            await update.message.reply_text(f"Склад «{store.name}»: такая позиция не найдена.", parse_mode=None)
            return
        updated = service.change_quantity(
            item.id, -parsed.quantity, parsed.unit if has_explicit_amount_unit(text) else None
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc), parse_mode=None)
        return
    await update.message.reply_text(
        f"{store.name} · {updated.name}: {format_quantity(updated.quantity)} {updated.unit}.",
        parse_mode=None,
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
    application.add_handler(CommandHandler("id", id_command))
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
