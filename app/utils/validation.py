import re
from fastapi import HTTPException


def validate_symbol(symbol: str, field_name: str = "symbol") -> str:
    """Validate a gene or target symbol.

    Ensures the symbol is a non‑empty string consisting only of
    alphanumeric characters, underscores, hyphens or dots.  Raises an
    HTTPException with status 422 if the symbol is invalid.
    """
    if not isinstance(symbol, str) or not symbol:
        raise HTTPException(
            status_code=422, detail=f"{field_name} must be a non‑empty string"
        )
    if not re.match(r"^[A-Za-z0-9._-]{1,50}$", symbol):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field_name} must contain only letters, numbers, dots, underscores or hyphens"
            ),
        )
    return symbol


def validate_condition(condition: str) -> str:
    """Validate a condition or disease identifier.

    Conditions should be non‑empty strings no longer than 100 characters.  Raises
    an HTTPException with status 422 if the value is invalid.
    """
    if not isinstance(condition, str) or not condition:
        raise HTTPException(
            status_code=422, detail="condition must be a non‑empty string"
        )
    if len(condition) > 100:
        raise HTTPException(status_code=422, detail="condition is too long")
