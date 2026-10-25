"""Bot entry point with compatibility exports for existing quantity callers."""

from amounts import convert_quantity, hidden_item_name, parse_amount_text
from inventory_bot import main

__all__ = ["convert_quantity", "hidden_item_name", "parse_amount_text", "main"]


if __name__ == "__main__":
    main()
