"""Resolver for mapping free-text conditions to EFO IDs.

Supports OLS4 (default) or OpenTargets GraphQL. Adds retries, caching,
and better error handling.
"""

from __future__ import annotations
import os
import time
import re
from typing import Optional, Any, Dict, Callable
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
REQUIRED = os.getenv("EFO_RESOLVE_REQUIRED", "true").lower() in {"1", "true", "yes", "on"}
RETRIES = int(os.getenv("EFO_RESOLVE_RETRIES", "2"))


def _retry(func: Callable[[], Any]) -> Any:
    """Simple retry wrapper with exponential backoff."""
    delay = 0.5
    for attempt in range(RETRIES + 1):
        try:
            return func()
        except Exception as e:
            if attempt == RETRIES:
                raise
            time.sleep(delay)
            delay *= 2


def _http_get(url: str, params: dict, client: Any = None):
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    # Allow injection of a mock client (for testing)
    if client is not None:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def _call():
        if hasattr(_http, "Client"):
            with _http.Client(timeout=REQUEST_TIMEOUT) as c:
                r = c.get(url, params=params)
                r.raise_for_status()
                return r.json()
        r = _http.get(url, params=params, timeout=REQUEST_TIMEOUT)  # type: ignore
        r.raise_for_status()
        return r.json()

    return _retry(_call)


def _http_post(url: str, json: dict, client: Any = None):
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    if client is not None:
        r = client.post(url, json=json)
        r.raise_for_status()
        return r.json()

    def _call():
        if hasattr(_http, "Client"):
            with _http.Client(timeout=REQUEST_TIMEOUT) as c:
                r = c.post(url, json=json)
                r.raise_for_status()
                return r.json()
        r = _http.post(url, json=json, timeout=REQUEST_TIMEOUT)  # type: ignore
        r.raise_for_status()
        return r.json()

    return _retry(_call)


def _to_efo_underscore(efo_curie: str) -> str:
    # EFO:0000270 -> EFO_0000270
    if ":" in efo_curie:
        prefix, local = efo_curie.split(":", 1)
        return f"{prefix}_{local}"
    return efo_curie


@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_ols(condition: str, client: Any = None) -> Optional[str]:
    url = "https://www.ebi.ac.uk/ols4/api/search"
    params = {"q": condition, "ontology": "efo", "type": "class", "rows": 1}
    try:
        data = _http_get(url, params, client=client)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"EFO OLS resolution failed: {e}")

    docs = data.get("response", {}).get("docs", []) or data.get("docs", [])
    if not docs:
        return None
    doc = docs[0]
    efo_curie = doc.get("obo_id") or doc.get("short_form") or doc.get("id")
    if not efo_curie:
        return None
    return _to_efo_underscore(efo_curie)


@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_opentargets(condition: str, client: Any = None) -> Optional[str]:
    url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = """
    query ($q: String!) {
      search(queryString: $q) {
        diseases { id name }
      }
    }"""
    variables = {"q": condition}
    try:
        data = _http_post(url, {"query": query, "variables": variables}, client=client)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"EFO OpenTargets resolution failed: {e}")

    diseases = (data.get("data") or {}).get("search", {}).get("diseases", []) or []
    if not diseases:
        return None
    return diseases[0].get("id")


def resolve_efo(efo: Optional[str], condition: Optional[str], client: Any = None) -> Optional[str]:
    if efo:
        if not re.match(r"^EFO[_:]\d+$", efo):
            raise HTTPException(status_code=400, detail=f"Invalid EFO format: {efo}")
        return _to_efo_underscore(efo)
    if not condition:
        return None
    if STRATEGY == "none":
        return None
    if STRATEGY == "opentargets":
        return resolve_condition_to_efo_via_opentargets(condition, client=client)
    return resolve_condition_to_efo_via_ols(condition, client=client)


async def require_efo_id(
    efo: Optional[str] = Query(None, description="EFO ID (e.g., EFO_0000270)."),
    condition: Optional[str] = Query(None, description="Free-text disease/phenotype (e.g., 'asthma')."),
) -> str:
    """FastAPI dependency: returns a final EFO ID or raises 400 when REQUIRED."""
    efo_id = resolve_efo(efo, condition)
    if efo_id is None and REQUIRED:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Missing efo",
                "hint": "Provide ?efo=EFO_... or ?condition=...; disable strictness via EFO_RESOLVE_REQUIRED=false",
            },
        )
    return efo_id or ""
