"""
Validation helpers for TARGETVAL inputs.

- Keep checks permissive enough to not block real-world identifiers.
- Provide a lightweight "symbolish" heuristic used by the gateway when deciding
  whether to reuse `gene` as `symbol`.
- Add optional EFO coercion utility so everything converges to EFO_#######.

NOTE: Normalization (e.g., mapping aliases to canonical symbols) should happen
in a dedicated normalizer (e.g., utils.normalize or within router functions).
"""

from typing import Optional
from fastapi import HTTPException
import re

__all__ = [
    "validate_symbol",
    "validate_condition",
    "normalize_gene_symbol",
    "is_symbolish",
    "coerce_efo_id",
]

# Simple patterns tuned for common cases
_SYMBOLISH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\\-]{0,31}$")
# Accept both EFO_0000000 and EFO:0000000, case-insensitive; we normalize to "EFO_#######"
_EFO_RE = re.compile(r"^EFO[_:]\\d{7}$", re.IGNORECASE)


def validate_symbol(symbol: Optional[str], field_name: str = "symbol") -> None:
    """
    Validate that a gene symbol or identifier is present and non-empty.

    Accepts HGNC-like symbols, Ensembl gene IDs (ENSG...), and keeps permissive
    character set (alnum and hyphen). Does not uppercase or modify the value.
    """
    if symbol is None or not isinstance(symbol, str) or not symbol.strip():
        # Use 422 to align with FastAPI's validation semantics
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name}: value must be a non-empty string",
        )
    # Allow ENSG* to pass without further checks
    s = symbol.strip()
    if s.upper().startswith("ENSG"):
        return
    # Permissive heuristic; do not hard-fail on uncommon but legitimate tokens
    if not _SYMBOLISH_RE.match(s):
        # Intentionally do not raise: we avoid false negatives and let downstream
        # normalizers / resolvers handle edge-case identifiers.
        return


def validate_condition(condition: Optional[str], field_name: str = "condition") -> None:
    """
    Validate that a condition or disease name is present and non-empty.

    Keep permissive because many APIs take free text and we will resolve/normalize later.
    """
    if condition is None or not isinstance(condition, str) or not condition.strip():
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name}: value must be a non-empty string",
        )


def normalize_gene_symbol(
    symbol: Optional[str] = None,
    gene: Optional[str] = None,
    field_name: str = "symbol",
) -> str:
    """
    Normalize gene input, accepting either `symbol` or `gene` as fallback.
    This does NOT perform biological alias normalization (done elsewhere).
    """
    value = symbol or gene
    if value is None or not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name}: value must be a non-empty string (gene or symbol)",
        )
    return value.strip()


def is_symbolish(value: Optional[str]) -> bool:
    """
    Heuristic used by the gateway to decide if a token looks like an HGNC-like symbol.
    Returns False for Ensembl (ENSG...), CURIEs (with ":"), and tokens with underscores.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    up = value.strip().upper()
    if up.startswith("ENSG") or (":" in up) or ("_" in up):
        return False
    return bool(_SYMBOLISH_RE.match(up))


def coerce_efo_id(efo: Optional[str]) -> Optional[str]:
    """
    Best-effort coercion to canonical EFO CURIE style (EFO_0000000).
    Returns normalized value or raises if clearly malformed.
    """
    if efo is None:
        return None
    s = efo.strip().upper().replace(":", "_")
    if not _EFO_RE.match(s):
        raise HTTPException(
            status_code=422,
            detail="Invalid efo: expected EFO_####### (e.g., EFO_0004611)",
        )
    return s
