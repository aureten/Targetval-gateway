import re
from fastapi import HTTPException

def validate_symbol(symbol: str, field_name: str = "symbol") -> str:
    """
    Ensure gene symbol is a non-empty string and contains only acceptable characters.
    """
    if not isinstance(symbol, str) or not symbol:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a non-empty string")
    if not re.match(r"^[A-Za-z0-9._-]{1,50}$", symbol):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must contain only letters, numbers, dots, underscores or hyphens",
        )
    return symbol

def validate_condition(condition: str, field_name: str = "condition") -> str:
    """
    Ensure condition or disease identifier is a non-empty string of reasonable length.
    """
    if not isinstance(condition, str) or not condition:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a non-empty string")
    if len(condition) > 100:
        raise HTTPException(status_code=422, detail=f"{field_name} is too long")
    return condition
