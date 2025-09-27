# app/utils/validation.py
import re

_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,31}$")

def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL.match(symbol):
        raise ValueError("Invalid symbol")
    return symbol.upper()

def validate_condition(cond: str) -> str:
    allowed = {"gt", "lt", "ge", "le", "eq", "ne"}
    c = str(cond).lower()
    if c not in allowed:
        raise ValueError(f"Invalid condition: {cond}")
    return c
