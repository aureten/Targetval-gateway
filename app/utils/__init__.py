"""Consolidated utilities for TARGETVAL Gateway.

This package provides a small, coherent surface area:

- net:     single HTTP layer (async-first) with retries, UA, degraded-mode gate
- ontology:EFO + endpoint/disease helpers and FastAPI dependency
- ids:     validation + normalization (symbols, Ensembl mapping, tissues, per-module input)

Import either from submodules or via the facade here.
"""

from .net import (
    aget_json, apost_json, get_json, post_json, require_or_degrade,
    DEFAULT_TIMEOUT, USER_AGENT, REQUEST_TIMEOUT_SECONDS
)

from .ontology import (
    resolve_efo, diseases_for_condition_if_endpoint, require_efo_id,
    map_endpoint_to_efo, map_endpoint_to_diseases
)

from .ids import (
    validate_symbol, validate_condition, normalize_gene_symbol, is_symbolish, coerce_efo_id,
    normalize_symbol, symbol_to_ensembl, primary_tissues_for_target, normalize_for_module,
    infer_bucket_from_module
)

__all__ = [
    # net
    "aget_json","apost_json","get_json","post_json","require_or_degrade",
    "DEFAULT_TIMEOUT","USER_AGENT","REQUEST_TIMEOUT_SECONDS",
    # ontology
    "resolve_efo","diseases_for_condition_if_endpoint","require_efo_id",
    "map_endpoint_to_efo","map_endpoint_to_diseases",
    # ids
    "validate_symbol","validate_condition","normalize_gene_symbol","is_symbolish","coerce_efo_id",
    "normalize_symbol","symbol_to_ensembl","primary_tissues_for_target","normalize_for_module",
    "infer_bucket_from_module",
]
