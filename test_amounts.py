from __future__ import annotations

import pytest

from amounts import (
    convert_quantity,
    has_explicit_amount_unit,
    hidden_item_name,
    parse_amount_list,
    parse_amount_text,
    parse_float,
)


def test_quick_amount_without_unit_inherits_existing_item_unit() -> None:
    assert not has_explicit_amount_unit("Миг 2")
    assert not has_explicit_amount_unit("корм 2 мин 500г")
    assert has_explicit_amount_unit("Миг 2табл")
    assert has_explicit_amount_unit("корм 1,5 кг")


def test_new_item_form_accepts_multiline_list_with_optional_plus() -> None:
    parsed, errors = parse_amount_list("Миг 8табл\n\n+ корм 4,5кг мин 500г\n2 кг")

    assert errors == [(4, "Сначала напишите название, потом количество.")]
    assert [(line, item.name, item.quantity, item.unit) for line, item in parsed] == [
        (1, "Миг", 8, "табл"),
        (3, "корм", 4.5, "кг"),
    ]


def test_parse_attached_units() -> None:
    parsed = parse_amount_text("корм 9кг мин 500г")

    assert parsed.name == "корм"
    assert parsed.quantity == 9
    assert parsed.unit == "кг"
    assert parsed.min_quantity == 500
    assert parsed.min_unit == "г"


def test_convert_compatible_units() -> None:
    assert convert_quantity(2500, "г", "кг") == 2.5
    assert convert_quantity(2.5, "кг", "г") == 2500
    assert convert_quantity(500, "мл", "л") == 0.5
    assert convert_quantity(1, "шт", "кг") is None


def test_hide_command_accepts_smart_dashes() -> None:
    assert hidden_item_name("-- горох") == "горох"
    assert hidden_item_name("— горох") == "горох"
    assert hidden_item_name("– горох") == "горох"
    assert hidden_item_name("- горох 1") is None


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "1e309"])
def test_quantity_parser_rejects_nonfinite_values(text: str) -> None:
    with pytest.raises(ValueError, match="конечное число"):
        parse_float(text)
