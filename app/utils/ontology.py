from __future__ import annotations

import os
from typing import Optional, List, Dict
from functools import lru_cache

from fastapi import HTTPException, Query

from .net import get_json, post_json

# Env configuration
STRATEGY = os.getenv("EFO_RESOLVE_STRATEGY", "ols").lower()  # ols | opentargets | none
REQUIRED = os.getenv("EFO_RESOLVE_REQUIRED", "false").lower() in {"1","true","yes","on"}

def _to_efo_underscore(efo_curie: str) -> str:
    # EFO:0000270 -> EFO_0000270
    if ":" in efo_curie:
        prefix, local = efo_curie.split(":", 1)
        return f"{prefix}_{local}"
    return efo_curie

# --- Endpoint/biomarker phrase maps ------------------------------------------

_ENDPOINT_EFO_MAP: Dict[str, str] = {
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
}

_ENDPOINT_DISEASE_SYNONYMS: Dict[str, List[str]] = {
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
    return _ENDPOINT_EFO_MAP.get(key) or _ENDPOINT_EFO_MAP.get(key.replace(" ", ""))

def map_endpoint_to_diseases(term: Optional[str]) -> List[str]:
    if not term:
        return []
    key = _canon_key(term).replace("-", " ")
    return _ENDPOINT_DISEASE_SYNONYMS.get(key, [])

# --- Resolvers ----------------------------------------------------------------

@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_ols(condition: str) -> Optional[str]:
    url = "https://www.ebi.ac.uk/ols4/api/search"
    params = {"q": condition, "ontology": "efo", "type": "class", "rows": 1}
    data = get_json(url, params=params)
    docs = (data.get("response", {}) or {}).get("docs", []) if isinstance(data, dict) else []
    if not docs and isinstance(data, dict):  # some deployments may return top-level docs
        docs = data.get("docs", [])
    if not docs:
        return None
    doc = docs[0]
    efo_curie = doc.get("obo_id") or doc.get("short_form") or doc.get("id")
    if not efo_curie:
        return None
    return _to_efo_underscore(efo_curie)

@lru_cache(maxsize=1024)
def resolve_condition_to_efo_via_opentargets(condition: str) -> Optional[str]:
    url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = """
    query ($q: String!) {
      search(queryString: $q) {
        diseases { id name }
      }
    }"""
    variables = {"q": condition}
    data = post_json(url, {"query": query, "variables": variables})
    diseases = ((data.get("data") or {}).get("search") or {}).get("diseases", []) or []
    if not diseases:
        return None
    return diseases[0].get("id")

def resolve_efo(efo: Optional[str], condition: Optional[str]) -> Optional[str]:
    if efo:
        return _to_efo_underscore(efo.strip())
    if not condition:
        return None
    mapped = map_endpoint_to_efo(condition)
    if mapped:
        return mapped
    if STRATEGY == "none":
        return None
    if STRATEGY == "opentargets":
        return resolve_condition_to_efo_via_opentargets(condition)
    return resolve_condition_to_efo_via_ols(condition)

def diseases_for_condition_if_endpoint(condition: Optional[str]) -> List[str]:
    return map_endpoint_to_diseases(condition)

# --- FastAPI dependency -------------------------------------------------------

async def require_efo_id(
    efo: Optional[str] = Query(None, description="EFO ID (e.g., EFO_0000270)."),
    condition: Optional[str] = Query(None, description="Free-text disease/phenotype (e.g., 'asthma' or 'LDL-C')."),
) -> str:
    efo_id = resolve_efo(efo, condition)
    if efo_id is None and REQUIRED:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Missing efo",
                "hint": "Provide ?efo=EFO_... or ?condition=...; set EFO_RESOLVE_REQUIRED=false to relax.",
            },
        )
    return efo_id or ""
