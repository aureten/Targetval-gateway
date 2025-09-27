
from __future__ import annotations

"""
EFO resolution helpers.

- Primary resolver via OLS4 (fast, no auth).
- Optional resolver via OpenTargets GraphQL.
- Small rule map for common endpoint/biomarker phrases (e.g., LDL-C) → EFO ids.
- Utility to map endpoint phrases to clinical disease terms for CT.gov/FAERS.

NOTE: These are synchronous by design (used in dependencies / gateway pre-pass).
"""

import os
from typing import Optional, List, Dict
from functools import lru_cache

try:
    import httpx as _http
except Exception:  # pragma: no cover
    try:
        import requests as _http  # type: ignore
    except Exception:  # pragma: no cover
        _http = None

from fastapi import HTTPException, Query

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
STRATEGY = os.getenv("EFO_RESOLVE_STRATEGY", "ols").lower()  # ols | opentargets | none
REQUIRED = os.getenv("EFO_RESOLVE_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}  # default permissive

USER_AGENT = os.getenv("OUTBOUND_USER_AGENT", "TargetVal/1.3 (+https://github.com/aureten/Targetval-gateway)")

def _http_get(url: str, params: dict):
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    # httpx
    if hasattr(_http, "Client"):
        with _http.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    # requests
    r = _http.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=headers)  # type: ignore
    r.raise_for_status()
    return r.json()

def _http_post_json(url: str, payload: dict):
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if hasattr(_http, "Client"):
        with _http.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            return r.json()
    r = _http.post(url, json=payload, timeout=REQUEST_TIMEOUT, headers=headers)  # type: ignore
    r.raise_for_status()
    return r.json()

def _to_efo_underscore(efo_curie: str) -> str:
    # EFO:0000270 -> EFO_0000270
    if ":" in efo_curie:
        prefix, local = efo_curie.split(":", 1)
        return f"{prefix}_{local}"
    return efo_curie

# --- Common endpoint/biomarker phrases -> EFO ids -----------------------------

_ENDPOINT_EFO_MAP: Dict[str, str] = {
    # LDL cholesterol (measurement)
    "ldl": "EFO_0004611",
    "ldl-c": "EFO_0004611",
    "ldl cholesterol": "EFO_0004611",
    "low density lipoprotein cholesterol": "EFO_0004611",
    "low-density lipoprotein cholesterol": "EFO_0004611",
    "low density lipoprotein cholesterol measurement": "EFO_0004611",
    "ldl lowering": "EFO_0004611",
    "ldl reduction": "EFO_0004611",
    "ldl-c lowering": "EFO_0004611",
    "ldl-c reduction": "EFO_0004611",
    # Extend as needed (hdl, triglycerides, hba1c, etc.)
}

_ENDPOINT_DISEASE_SYNONYMS: Dict[str, List[str]] = {
    # Endpoint/biomarker -> clinical conditions typically queried in CT.gov/FAERS
    "ldl": ["hypercholesterolemia", "familial hypercholesterolemia", "dyslipidemia"],
    "ldl-c": ["hypercholesterolemia", "familial hypercholesterolemia", "dyslipidemia"],
    "ldl cholesterol": ["hypercholesterolemia", "familial hypercholesterolemia", "dyslipidemia"],
    "low density lipoprotein cholesterol": ["hypercholesterolemia", "familial hypercholesterolemia", "dyslipidemia"],
    "ldl lowering": ["hypercholesterolemia", "familial hypercholesterolemia", "dyslipidemia"],
    "ldl reduction": ["hypercholesterolemia", "familial hypercholesterolemia", "dyslipidemia"],
}

def _canon_key(s: str) -> str:
    return " ".join(s.lower().strip().split())

def map_endpoint_to_efo(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    key = _canon_key(term).replace("-", " ")
    # try exact and hyphen/nospace variants
    return _ENDPOINT_EFO_MAP.get(key) or _ENDPOINT_EFO_MAP.get(key.replace(" ", ""))

def map_endpoint_to_diseases(term: Optional[str]) -> List[str]:
    if not term:
        return []
    key = _canon_key(term).replace("-", " ")
    return _ENDPOINT_DISEASE_SYNONYMS.get(key, [])

# --- Resolvers ----------------------------------------------------------------

@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_ols(condition: str) -> Optional[str]:
    """Use OLS4 REST to search EFO by label and return the top match's CURIE."""
    url = "https://www.ebi.ac.uk/ols4/api/search"
    params = {"q": condition, "ontology": "efo", "type": "class", "rows": 1}
    data = _http_get(url, params)
    # OLS4 returns Solr-like: response -> docs
    docs = (data.get("response", {}) or {}).get("docs", []) or data.get("docs", [])
    if not docs:
        return None
    doc = docs[0]
    efo_curie = doc.get("obo_id") or doc.get("short_form") or doc.get("id")
    if not efo_curie:
        return None
    return _to_efo_underscore(efo_curie)

@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_opentargets(condition: str) -> Optional[str]:
    """Resolve via OpenTargets GraphQL 'search' capability and return the first disease id."""
    url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = """
    query ($q: String!) {
      search(queryString: $q) {
        diseases { id name }
      }
    }"""
    variables = {"q": condition}
    data = _http_post_json(url, {"query": query, "variables": variables})
    diseases = ((data.get("data") or {}).get("search") or {}).get("diseases", []) or []
    if not diseases:
        return None
    return diseases[0].get("id")

def resolve_efo(efo: Optional[str], condition: Optional[str]) -> Optional[str]:
    """Best-effort: prefer explicit efo, then endpoint map, then chosen strategy."""
    if efo:
        return _to_efo_underscore(efo.strip())
    if not condition:
        return None
    # Endpoint/biomarker phrase short-circuit
    mapped = map_endpoint_to_efo(condition)
    if mapped:
        return mapped
    if STRATEGY == "none":
        return None
    if STRATEGY == "opentargets":
        return resolve_condition_to_efo_via_opentargets(condition)
    # default: OLS
    return resolve_condition_to_efo_via_ols(condition)

def diseases_for_condition_if_endpoint(condition: Optional[str]) -> List[str]:
    """
    If the provided 'condition' looks like an endpoint/biomarker phrase,
    return clinical disease synonyms to use with CT.gov/FAERS.
    """
    return map_endpoint_to_diseases(condition)

# --- FastAPI dependency -------------------------------------------------------

async def require_efo_id(
    efo: Optional[str] = Query(None, description="EFO ID (e.g., EFO_0000270)."),
    condition: Optional[str] = Query(None, description="Free-text disease/phenotype (e.g., 'asthma' or 'LDL-C')."),
) -> str:
    """
    FastAPI dependency: returns a final EFO ID or raises 400 when REQUIRED is true.
    Default is permissive: returns empty string if unresolved and not required.
    """
    efo_id = resolve_efo(efo, condition)
    if efo_id is None and REQUIRED:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Missing efo",
                "hint": "Provide ?efo=EFO_... or ?condition=...; you can disable strictness via EFO_RESOLVE_REQUIRED=false",
            },
        )
    return efo_id or ""
