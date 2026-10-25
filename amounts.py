"""Pure quantity parsing and unit conversion shared by all interfaces."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


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
    if not math.isfinite(result):
        raise ValueError("Введите конечное число.")
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


def parse_amount_list(text: str) -> tuple[list[tuple[int, ParsedAmount]], list[tuple[int, str]]]:
    parsed_lines: list[tuple[int, ParsedAmount]] = []
    errors: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("+"):
            line = line[1:].strip()
        try:
            parsed_lines.append((line_number, parse_amount_text(line)))
        except ValueError as exc:
            errors.append((line_number, str(exc)))
    if not parsed_lines and not errors:
        errors.append((1, "Введите название и количество."))
    return parsed_lines, errors


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
    if converted is None:
        raise ValueError(f"Минимум нужно указать в {item_unit} или совместимой единице.")
    return converted
