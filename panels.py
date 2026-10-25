"""Telegram panel rendering from domain data; no update handling or database access."""

from __future__ import annotations

import html
from hashlib import sha256
from math import ceil

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from amounts import normalize_unit
from storage_types import Household, InventoryItem, Store, User, format_quantity


BTN_STOCK = "Склады"
BTN_BUY = "Купить"
BTN_HOMES = "Дом"
BTN_HELP = "Помощь"
MENU_BUTTONS = {BTN_STOCK, BTN_BUY, BTN_HOMES, BTN_HELP}
PAGE_SIZE = 10


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_STOCK), KeyboardButton(BTN_BUY)],
            [KeyboardButton(BTN_HOMES), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="+ кофе 1 или - кофе 1",
    )


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
    row.append(InlineKeyboardButton(f"{page + 1} / {pages}", callback_data="v2:page_info"))
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
        "Дом можно открыть другому человеку через код приглашения. "
        "Внутри дома можно создать склады с любыми названиями: «Лекарства», «Кухня» или «Морозилка».\n\n"
        "<b>Работа со складом</b>\n"
        "Откройте склад, выберите товар и используйте кнопки «Списать» или «Добавить». "
        "Для существующей позиции достаточно написать количество: <code>2</code>, <code>2табл</code> или <code>500г</code>.\n\n"
        "Кнопка «Новая позиция» принимает один товар или список — каждый товар с новой строки. "
        "После добавления форма остаётся открытой для следующих позиций.\n\n"
        "<b>Быстрый ввод</b>\n"
        "<code>+ Бумага 8</code> — добавить 8 штук\n"
        "<code>- Бумага 3</code> — списать 3 штуки\n"
        "<code>+ корм 9кг</code> — добавить вес\n"
        "<code>- корм 2,5кг</code> — списать вес\n"
        "<code>&#45;&#45; горох</code> — перенести позицию в архив\n"
        "<code>+ корм 9кг мин 500г</code> — добавить корм с минимумом 500г\n\n"
        "<code>/id</code> — ваш ID для подключения агента через MCP (в личном чате).\n\n"
        "По умолчанию минимум равен 1. Если остаток меньше или равен минимуму, позиция появляется в разделе «Купить»."
    )


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


def homes_panel(current: Household | None, households: list[Household], members: list[User]) -> tuple[str, InlineKeyboardMarkup, Household | None]:
    if current is None:
        text, keyboard = no_home_panel()
        return text, keyboard, None
    rows: list[list[InlineKeyboardButton]] = []
    for household in households:
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
    return home_text(current, members), InlineKeyboardMarkup(rows), current


def home_edit_panel(current: Household | None, role: str | None) -> tuple[str, InlineKeyboardMarkup, Household] | None:
    if current is None:
        return None
    remove_label = "Удалить дом" if role == "owner" else "Выйти из дома"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать дом", callback_data="v2:home_create")],
            [InlineKeyboardButton("Переименовать дом", callback_data="v2:home_rename")],
            [InlineKeyboardButton(remove_label, callback_data=f"v2:home_remove:{current.id}")],
            [InlineKeyboardButton("К домам", callback_data="v2:home_back")],
        ]
    )
    return f"<b>{html.escape(current.name)}</b>\n\nРедактирование дома:", keyboard, current


def stores_panel(household: Household | None, stores: list[Store], quick_store: Store | None, low_counts: dict[int, int]) -> tuple[str, InlineKeyboardMarkup, Household] | None:
    if household is None:
        return None
    quick_name = html.escape(quick_store.name) if quick_store and quick_store.household_id == household.id else "не выбран"
    rows: list[list[InlineKeyboardButton]] = []
    for store in stores:
        low_count = low_counts.get(store.id, 0)
        suffix = f" · купить {low_count}" if low_count else ""
        rows.append(
            [
                InlineKeyboardButton(f"{store.name}{suffix}", callback_data=f"v2:store:{store.id}:0"),
                InlineKeyboardButton("⚙️", callback_data=f"v2:store_settings:{store.id}:0"),
            ]
        )
    rows.append([InlineKeyboardButton("Добавить склад", callback_data="v2:store_create")])
    text = (
        f"Дом: {html.escape(household.name)}\n"
        f"Быстрый ввод: {quick_name}\n\n"
        "Выберите склад:"
    )
    return text, InlineKeyboardMarkup(rows), household


def store_settings_panel(store: Store, household: Household | None = None) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"<b>Настройки склада</b>\n{store_context(store, household)}",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Переименовать", callback_data=f"v2:store_rename_pick:{store.id}")],
                [InlineKeyboardButton("Удалить склад", callback_data=f"v2:store_delete_pick:{store.id}")],
                [InlineKeyboardButton("К складу", callback_data=f"v2:store:{store.id}:0")],
                [InlineKeyboardButton("К списку складов", callback_data="v2:stores_back")],
            ]
        ),
    )


def store_edit_panel(household: Household) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"<b>{html.escape(household.name)}</b>\n\nРедактирование складов:",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Создать", callback_data="v2:store_create")],
                [InlineKeyboardButton("Переименовать", callback_data="v2:store_rename")],
                [InlineKeyboardButton("Удалить", callback_data="v2:store_delete")],
                [InlineKeyboardButton("К складам", callback_data="v2:stores_back")],
            ]
        ),
    )


def store_pick_panel(household: Household, stores: list[Store], action: str) -> tuple[str, InlineKeyboardMarkup]:
    action_text = "переименовать" if action == "rename" else "удалить"
    rows = [
        [InlineKeyboardButton(store.name, callback_data=f"v2:store_{action}_pick:{store.id}")]
        for store in stores
    ]
    rows.append([InlineKeyboardButton("К настройкам складов", callback_data="v2:store_edit")])
    return (
        f"<b>{html.escape(household.name)}</b>\n\nВыберите склад, который нужно {action_text}:",
        InlineKeyboardMarkup(rows),
    )


def store_panel(store: Store, items: list[InventoryItem], page: int, household: Household | None = None) -> tuple[str, InlineKeyboardMarkup, int]:
    items = sorted_items(items)
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
                InlineKeyboardButton("К складам", callback_data="v2:stores_back"),
            ],
        ]
    )
    text = (
        f"<b>{html.escape(store.name)}</b>\n"
        f"{store_context(store, household)}\n"
        f"Позиций: {len(items)} · купить: {low_count}"
    )
    if not items:
        text += "\n\nСклад пока пуст."
    return text, InlineKeyboardMarkup(rows), safe_page


def item_panel(
    item: InventoryItem, page: int, store: Store | None = None, household: Household | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    step, unit = quick_step(item)
    step_text = format_quantity(step)
    unit_tag = step_unit_tag(unit)
    status = "нужно купить" if item.is_low else "в запасе"
    text = (
        f"<b>{html.escape(item.name)}</b>\n"
        f"{item_context(store, household)}\n"
        f"Остаток: {format_quantity(item.quantity)} {html.escape(item.unit)}\n"
        f"Минимум: {format_quantity(item.min_quantity)} {html.escape(item.unit)}\n"
        f"Статус: {status}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"−{step_text} {unit}", callback_data=f"v2:step:{item.id}:-{step_text}:{unit_tag}:{page}"
                ),
                InlineKeyboardButton(
                    f"+{step_text} {unit}", callback_data=f"v2:step:{item.id}:{step_text}:{unit_tag}:{page}"
                ),
            ],
            [
                InlineKeyboardButton("Списать", callback_data=f"v2:adjust:{item.id}:use:{page}"),
                InlineKeyboardButton("Добавить", callback_data=f"v2:adjust:{item.id}:add:{page}"),
            ],
            [InlineKeyboardButton("Настройки", callback_data=f"v2:item_settings:{item.id}:{page}")],
            [InlineKeyboardButton("К складу", callback_data=f"v2:store:{item.store_id}:{page}")],
        ]
    )
    return text, keyboard


def quick_step(item: InventoryItem) -> tuple[float, str]:
    """Return a convenient stock adjustment in the item's canonical unit."""
    unit = normalize_unit(item.unit)
    return {"г": 100.0, "мл": 100.0, "кг": 0.5, "л": 0.5}.get(unit, 1.0), unit


def step_unit_tag(unit: str) -> str:
    """Bind a button to its unit without placing arbitrary unit text in callbacks."""
    normalized = normalize_unit(unit)
    known_tag = {"г": "g", "кг": "kg", "мл": "ml", "л": "l"}.get(normalized)
    return known_tag or f"u{sha256(normalized.encode('utf-8')).hexdigest()[:8]}"


def item_settings_panel(
    item: InventoryItem, page: int, store: Store | None = None, household: Household | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"<b>{html.escape(item.name)}</b>\n{item_context(store, household)}\nНастройки позиции:",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Переименовать", callback_data=f"v2:item_rename:{item.id}:{page}"),
                    InlineKeyboardButton("Минимум", callback_data=f"v2:item_min:{item.id}:{page}"),
                ],
                [InlineKeyboardButton("Единица", callback_data=f"v2:item_unit:{item.id}:{page}")],
                [InlineKeyboardButton("В архив", callback_data=f"v2:item_archive:{item.id}:{page}")],
                [InlineKeyboardButton("Удалить", callback_data=f"v2:item_delete:{item.id}:{page}:item")],
                [InlineKeyboardButton("К позиции", callback_data=f"v2:item:{item.id}:{page}")],
            ]
        ),
    )


def archive_panel(store: Store, items: list[InventoryItem], page: int, household: Household | None = None) -> tuple[str, InlineKeyboardMarkup, int]:
    items = sorted(
        [item for item in items if not item.active],
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
    rows.append([InlineKeyboardButton("К складу", callback_data=f"v2:store:{store.id}:0")])
    text = f"<b>{html.escape(store.name)}</b>\n{store_context(store, household)}\n\nАрхив"
    if not items:
        text += " пока пуст."
    return text, InlineKeyboardMarkup(rows), safe_page


def archived_item_panel(
    item: InventoryItem, page: int, store: Store | None = None, household: Household | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"<b>{html.escape(item.name)}</b>\n"
        f"{item_context(store, household)}\n"
        f"Остаток: {format_quantity(item.quantity)} {html.escape(item.unit)}\n"
        f"Минимум: {format_quantity(item.min_quantity)} {html.escape(item.unit)}\n"
        "Статус: в архиве"
    )
    return (
        text,
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Восстановить", callback_data=f"v2:item_restore:{item.id}:{page}")],
                [InlineKeyboardButton("Удалить", callback_data=f"v2:item_delete:{item.id}:{page}:archive")],
                [InlineKeyboardButton("К архиву", callback_data=f"v2:archive:{item.store_id}:{page}")],
            ]
        ),
    )


def buy_panel(household: Household, entries: list[tuple[Store, InventoryItem]], page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    visible, safe_page, pages = page_slice(entries, page)
    rows: list[list[InlineKeyboardButton]] = []
    for store, item in visible:
        label = (
            f"Купил · {store.name} · {item.name} · {remaining_word(item.quantity, item.unit)} "
            f"{format_quantity(item.quantity)} {item.unit}"
        )
        rows.append([InlineKeyboardButton(label, callback_data=f"v2:buy_item:{item.id}:{safe_page}")])
    rows.extend(pagination_rows("v2:buy", safe_page, pages))
    rows.append([InlineKeyboardButton("Обновить", callback_data=f"v2:buy:{safe_page}")])
    text = f"<b>Купить для «{html.escape(household.name)}»</b>"
    if not entries:
        text += "\n\nПокупать пока ничего не нужно."
    return text, InlineKeyboardMarkup(rows), safe_page


def cancel_keyboard(label: str = "Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="v2:flow_cancel")]])


def item_create_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("К складу", callback_data="v2:flow_cancel")]])


def item_create_prompt(
    store: Store,
    added_names: list[str] | None = None,
    problems: list[tuple[int, str]] | None = None,
) -> str:
    parts = [f"<b>Новая позиция · {html.escape(store.name)}</b>"]
    added_names = added_names or []
    problems = problems or []
    if added_names:
        parts.append(f"Добавлено: {len(added_names)}")
        parts.extend(f"• {html.escape(name)}" for name in added_names[:8])
        if len(added_names) > 8:
            parts.append(f"• и ещё {len(added_names) - 8}")
    if problems:
        problem_lines = ["Не добавлено:"]
        problem_lines.extend(
            f"• строка {line_number}: {html.escape(reason)}"
            for line_number, reason in problems[:8]
        )
        if len(problems) > 8:
            problem_lines.append(f"• и ещё {len(problems) - 8}")
        parts.append("\n".join(problem_lines))
    parts.append(
        "Отправьте одну позицию или список — каждый товар с новой строки.\n\n"
        "<code>Миг 8табл\n"
        "корм 9кг мин 500г\n"
        "бумага 4</code>"
    )
    return "\n\n".join(parts)


def store_context(store: Store, household: Household | None = None) -> str:
    prefix = f"Дом: {html.escape(household.name)} · " if household else ""
    return f"{prefix}Склад: {html.escape(store.name)}"


def item_context(store: Store | None, household: Household | None) -> str:
    if store:
        return store_context(store, household)
    return f"Дом: {html.escape(household.name)}" if household else ""
