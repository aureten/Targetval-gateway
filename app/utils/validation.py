"""Validation helpers for TARGETVAL inputs.

These functions perform simple sanity checks on incoming request
parameters, raising HTTP exceptions when values are missing or
ill-formed.  The goal is to provide clear error messages early in
request processing.

As the gateway evolves, additional checks (e.g. regex matching for
gene symbols, length limits) can be implemented here.  The current
validators simply ensure that values are non-empty strings.  See
``app/main.py`` for normalisation routines that map synonyms to
canonical identifiers.
"""

from typing import Optional

from fastapi import HTTPException


def validate_symbol(symbol: Optional[str], field_name: str = "symbol") -> None:
    """Validate that a gene symbol or identifier is present and non-empty.

    Parameters
    ----------
    symbol : Optional[str]
        The gene symbol, Ensembl ID or other identifier to validate.
    field_name : str, optional
        The name of the field in the incoming request.  Used for error
        messages.

    Raises
    ------
    HTTPException
        If the symbol is None or an empty/whitespace-only string.
    """
    if symbol is None or not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: value must be a non-empty string",
        )


def validate_condition(condition: Optional[str], field_name: str = "condition") -> None:
    """Validate that a condition or disease name is present and non-empty.

    Parameters
    ----------
    condition : Optional[str]
        The condition name, EFO ID or other disease identifier to validate.
    field_name : str, optional
        The name of the field in the incoming request.  Used for error
        messages.

    Raises
    ------
    HTTPException
        If the condition is None or an empty/whitespace-only string.
    """
    if condition is None or not isinstance(condition, str) or not condition.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: value must be a non-empty string",
        )
