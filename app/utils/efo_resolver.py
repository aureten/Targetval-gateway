from __future__ import annotations
import os
from typing import Optional
from functools import lru_cache

try:
    import httpx as _http
except Exception:  # pragma: no cover
    try:
        import requests as _http  # type: ignore
    except Exception:  # pragma: no cover
        _http = None

from fastapi import Depends, HTTPException, Query

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
STRATEGY = os.getenv("EFO_RESOLVE_STRATEGY", "ols").lower()  # ols | opentargets | none
REQUIRED = os.getenv("EFO_RESOLVE_REQUIRED", "true").lower() in {"1","true","yes","on"}


def _http_get(url: str, params: dict):
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    # httpx
    if hasattr(_http, "Client"):
        with _http.Client(timeout=REQUEST_TIMEOUT) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    # requests
    r = _http.get(url, params=params, timeout=REQUEST_TIMEOUT)  # type: ignore
    r.raise_for_status()
    return r.json()


def _to_efo_underscore(efo_curie: str) -> str:
    # EFO:0000270 -> EFO_0000270
    if ":" in efo_curie:
        prefix, local = efo_curie.split(":", 1)
        return f"{prefix}_{local}"
    return efo_curie


@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_ols(condition: str) -> Optional[str]:
    """
    Use OLS4 REST to search EFO by label and return the top match's CURIE.
    """
    url = "https://www.ebi.ac.uk/ols4/api/search"
    params = {"q": condition, "ontology": "efo", "type": "class", "rows": 1}
    data = _http_get(url, params)
    # OLS4 returns 'response'->'docs' in Solr-like format
    docs = (
        data.get("response", {}).get("docs", [])
        or data.get("response", {}).get("docs")
        or []
    )
    if not docs and "response" not in data:
        # alternative structure for some deployments:
        # try items/docs field fallback
        docs = data.get("response", {}).get("docs", []) or data.get("docs", [])
    if not docs:
        return None
    doc = docs[0]
    # prefer 'obo_id' like EFO:0000270; else try 'short_form' or 'id'
    efo_curie = doc.get("obo_id") or doc.get("short_form") or doc.get("id")
    if not efo_curie:
        return None
    return _to_efo_underscore(efo_curie)


@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_opentargets(condition: str) -> Optional[str]:
    """
    Resolve via the Open Targets GraphQL API 'search' capability.
    Minimal payload to avoid heavy deps.
    """
    # GraphQL endpoint and query
    url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = """
    query ($q: String!) {
      search(queryString: $q) {
        diseases {
          id
          name
        }
      }
    }"""
    variables = {"q": condition}
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    # httpx or requests: POST JSON
    if hasattr(_http, "Client"):
        with _http.Client(timeout=REQUEST_TIMEOUT) as client:
            r = client.post(url, json={"query": query, "variables": variables})
            r.raise_for_status()
            data = r.json()
    else:
        r = _http.post(url, json={"query": query, "variables": variables}, timeout=REQUEST_TIMEOUT)  # type: ignore
        r.raise_for_status()
        data = r.json()
    diseases = (data.get("data") or {}).get("search", {}).get("diseases", []) or []
    if not diseases:
        return None
    # returns EFO IDs like EFO_0000270 already
    return diseases[0].get("id")


def resolve_efo(efo: Optional[str], condition: Optional[str]) -> Optional[str]:
    if efo:
        return efo
    if not condition:
        return None
    if STRATEGY == "none":
        return None
    if STRATEGY == "opentargets":
        return resolve_condition_to_efo_via_opentargets(condition)
    # default: OLS
    return resolve_condition_to_efo_via_ols(condition)


async def require_efo_id(
    efo: Optional[str] = Query(None, description="EFO ID (e.g., EFO_0000270)."),
    condition: Optional[str] = Query(None, description="Free-text disease/phenotype (e.g., 'asthma')."),
) -> str:
    """
    FastAPI dependency: returns a final EFO ID or raises 400 when REQUIRED.
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
