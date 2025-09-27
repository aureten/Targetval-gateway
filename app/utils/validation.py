"""Validation helpers for TARGETVAL inputs.

These functions perform sanity checks on incoming request parameters,
raising HTTP exceptions when values are missing or ill-formed.
"""

import re
from typing import Optional

from fastapi import HTTPException

# Regex patterns (conservative defaults)
HGNC_REGEX = re.compile(r"^[A-Z0-9\-]+$")
EFO_REGEX = re.compile(r"^EFO_\d+$")


def validate_symbol(symbol: Optional[str], field_name: str = "symbol") -> None:
    """Validate that a gene symbol or identifier is present and well-formed."""
    if symbol is None or not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: value must be a non-empty string",
        )
    value = symbol.strip()
    # Optional HGNC regex check (skip if it's an Ensembl/other ID)
    if not (HGNC_REGEX.match(value) or value.startswith("ENSG") or value.startswith("ENSP")):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: '{value}' does not look like a valid gene symbol or Ensembl ID",
        )


def validate_condition(condition: Optional[str], field_name: str = "condition") -> None:
    """Validate that a condition or disease name is present and non-empty."""
    if condition is None or not isinstance(condition, str) or not condition.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: value must be a non-empty string",
        )
    value = condition.strip()
    # Optional regex check if condition is already an EFO ID
    if value.upper().startswith("EFO") and not EFO_REGEX.match(value.upper()):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: '{value}' does not look like a valid EFO identifier",
        )


def normalize_gene_symbol(
    symbol: Optional[str] = None,
    gene: Optional[str] = None,
    field_name: str = "symbol",
) -> str:
    """
    Normalize gene input, accepting either `symbol` or `gene` as fallback.
    """
    value = symbol or gene
    validate_symbol(value, field_name=field_name)
    return value.strip()
