from __future__ import annotations
"""
Normalization helpers for TARGETVAL.

What this module does:
- Canonicalize gene symbols (cheap alias map + UniProt probe).
- Map HGNC symbols to Ensembl gene IDs (Ensembl REST, fallback: OpenTargets search).
- Suggest primary tissues for a target (simple heuristics + HPA best-effort).
- Provide a convenience normalizer that adjusts inputs per module kind
  (e.g., endpoint phrases → disease terms for clinical modules).

This file deliberately keeps an extremely small dependency surface (httpx or requests).
"""

import os
from typing import Optional, List, Dict, Any
from functools import lru_cache

try:
    import httpx as _http
except Exception:  # pragma: no cover
    try:
        import requests as _http  # type: ignore
    except Exception:  # pragma: no cover
        _http = None

from fastapi import HTTPException

from .validation import is_symbolish
from .efo_resolver import resolve_efo, diseases_for_condition_if_endpoint

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
USER_AGENT = os.getenv("OUTBOUND_USER_AGENT", "TargetVal/1.3 (+https://github.com/aureten/Targetval-gateway)")

# --------------------------------------------------------------------------------------
# HTTP helpers (sync; small footprint)
# --------------------------------------------------------------------------------------

def _http_get_json(url: str, params: Optional[dict] = None) -> Any:
    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if hasattr(_http, "Client"):
        with _http.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    r = _http.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=headers)  # type: ignore
    r.raise_for_status()
    return r.json()

def _http_post_json(url: str, payload: dict) -> Any:
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

# --------------------------------------------------------------------------------------
# Symbol normalization
# --------------------------------------------------------------------------------------

_ALIAS_GENE_MAP: Dict[str, str] = {
    "CB1": "CNR1",
    "CB-1": "CNR1",
    "CNR-1": "CNR1",
    "TGFR2": "TGFBR2",
}

@lru_cache(maxsize=2048)
def normalize_symbol(symbol: str) -> str:
    """
    Fast path: alias map and capitalization.
    Slow path: UniProt -> prefer primary geneName for Homo sapiens.

    Returns an uppercase HGNC-like symbol when possible. Falls back to input uppercased.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=400, detail="Invalid symbol")
    up = symbol.strip().upper()
    if up in _ALIAS_GENE_MAP:
        return _ALIAS_GENE_MAP[up]
    # Minimal guard: if already looks like a clean token, accept
    if up.isalnum() or "-" in up:
        pass

    # Probe UniProt for canonical gene name
    try:
        url = (
            "https://rest.uniprot.org/uniprotkb/search?"
            f"query={up}+AND+organism_id:9606&fields=genes&format=json&size=1"
        )
        js = _http_get_json(url)
        res = js.get("results", []) if isinstance(js, dict) else []
        if res:
            genes = res[0].get("genes") or []
            for g in genes:
                gn = (g.get("geneName") or {}).get("value")
                if gn:
                    return gn.upper()
            for g in genes:
                for syn in (g.get("synonyms") or []):
                    v = syn.get("value")
                    if v:
                        return v.upper()
    except Exception:
        pass
    return up

# --------------------------------------------------------------------------------------
# Symbol → Ensembl mapping
# --------------------------------------------------------------------------------------

@lru_cache(maxsize=2048)
def symbol_to_ensembl(symbol: str) -> Optional[str]:
    """
    Map a (normalized) HGNC symbol to an Ensembl gene ID.
    Strategy:
      1) Ensembl REST xrefs/symbol/homo_sapiens/{symbol}
      2) Fallback: OpenTargets GraphQL search → targets { id approvedSymbol }
    """
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    sym = symbol.strip().upper()

    # 1) Ensembl REST
    try:
        url = f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{sym}"
        js = _http_get_json(url, params={"content-type": "application/json"})
        if isinstance(js, list):
            for rec in js:
                _id = (rec.get("id") or "").upper()
                _type = (rec.get("type") or "").lower()
                if _id.startswith("ENSG") and ("gene" in _type):
                    return _id
            # secondary pass: any ENSG
            for rec in js:
                _id = (rec.get("id") or "").upper()
                if _id.startswith("ENSG"):
                    return _id
    except Exception:
        pass

    # 2) OpenTargets GraphQL fallback
    try:
        url = "https://api.platform.opentargets.org/api/v4/graphql"
        query = """
        query ($q:String!){
          search(queryString:$q){
            targets { id approvedSymbol }
          }
        }"""
        data = _http_post_json(url, {"query": query, "variables": {"q": sym}})
        targets = (((data.get("data") or {}).get("search") or {}).get("targets") or [])
        # prefer exact approvedSymbol match
        for t in targets:
            if (t.get("approvedSymbol") or "").upper() == sym and str(t.get("id","")).upper().startswith("ENSG"):
                return t.get("id")
        # otherwise first ENSG id
        for t in targets:
            _id = (t.get("id") or "").upper()
            if _id.startswith("ENSG"):
                return t.get("id")
    except Exception:
        pass

    return None

# --------------------------------------------------------------------------------------
# Primary tissues inference
# --------------------------------------------------------------------------------------

_CURATED_TISSUES: Dict[str, List[str]] = {
    "PCSK9": ["Liver"],
    "LDLR": ["Liver"],
    "IL6": ["Blood", "Liver"],
}

def _parse_hpa_tissues(rows: List[Dict[str, Any]], limit: int) -> List[str]:
    counts: Dict[str, int] = {}
    for r in rows:
        tstr = r.get("rna_tissue") or r.get("tissue") or ""
        # split on comma/semicolon; trim
        parts = [p.strip() for p in str(tstr).replace(";", ",").split(",") if p.strip()]
        for p in parts:
            counts[p] = counts.get(p, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in ranked[:max(1, limit)]]

def primary_tissues_for_target(symbol: str, limit: int = 3) -> List[str]:
    """
    Heuristic, best-effort:
      - return curated tissues when available (PCSK9 → Liver)
      - else try HPA search_download to see top-mentioned tissues
      - else return []
    """
    sym = normalize_symbol(symbol)
    if sym in _CURATED_TISSUES:
        return _CURATED_TISSUES[sym][:max(1, limit)]
    # HPA probe (best-effort)
    try:
        url = (
            "https://www.proteinatlas.org/api/search_download.php"
            f"?format=json&columns=gene,rna_tissue,rna_gtex&search={sym}"
        )
        js = _http_get_json(url)
        rows = js if isinstance(js, list) else []
        tissues = _parse_hpa_tissues(rows, limit)
        if tissues:
            return tissues
    except Exception:
        pass
    return []

# --------------------------------------------------------------------------------------
# Per-module normalization convenience
# --------------------------------------------------------------------------------------

def infer_bucket_from_module(name: str) -> str:
    """
    Map module name to a high-level bucket. Accepts 'mech_ppi' or '/mech/ppi' styles.
    """
    n = (name or "").strip().lower().replace("/", "_")
    for key, bucket in [
        ("genetics_", "genetics"),
        ("assoc_", "assoc"),
        ("expr_", "expr"),
        ("mech_", "mech"),
        ("tract_", "tract"),
        ("clin_", "clin"),
        ("comp_", "comp"),
    ]:
        if n.startswith(key):
            return bucket
    # heuristic fallback
    if "genetics" in n: return "genetics"
    if "assoc" in n: return "assoc"
    if "expr" in n: return "expr"
    if "mech" in n: return "mech"
    if "tract" in n: return "tract"
    if "clin" in n: return "clin"
    if "comp" in n: return "comp"
    return "other"

def normalize_for_module(
    module_name: str,
    symbol: Optional[str] = None,
    gene: Optional[str] = None,
    condition: Optional[str] = None,
    efo: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Produce a best-effort normalized input dict for a module.

    - Chooses symbol from (symbol, gene) when gene looks HGNC-like.
    - Normalizes symbol and attaches Ensembl id when available.
    - Resolves EFO from (efo, condition), including endpoint phrases (e.g., LDL-C).
    - If module is clinical-like and condition is an endpoint phrase,
      replace condition with a clinical disease synonym.
    - If module is assoc-like and condition is an endpoint phrase,
      suggest a primary tissue from the target as the condition.

    Returns a dict with possible keys: symbol, ensembl_id, condition, efo
    (only the keys we can infer are included).
    """
    out: Dict[str, Any] = {}
    # symbol resolution
    sym_in = symbol or (gene if is_symbolish(gene) else None)
    if sym_in:
        sym_norm = normalize_symbol(sym_in)
        out["symbol"] = sym_norm
        ens = symbol_to_ensembl(sym_norm)
        if ens:
            out["ensembl_id"] = ens

    # EFO from efo/condition (handles endpoint phrases)
    efo_id = resolve_efo(efo, condition)
    if efo_id:
        out["efo"] = efo_id

    bucket = infer_bucket_from_module(module_name)

    # Condition normalization depending on bucket
    if condition:
        endpoint_diseases = diseases_for_condition_if_endpoint(condition)
        if bucket == "clin":
            # clinical modules prefer disease terms (CT.gov/FAERS indexing)
            if endpoint_diseases:
                out["condition"] = endpoint_diseases[0]
            else:
                out["condition"] = condition
        elif bucket == "assoc":
            # assoc modules often expect tissue; if endpoint phrase, try tissue from target
            if endpoint_diseases:
                # If we have a target symbol, use tissue recommendation
                sym_norm = out.get("symbol")
                if sym_norm:
                    tissues = primary_tissues_for_target(sym_norm, limit=1)
                    if tissues:
                        out["condition"] = tissues[0]
                    else:
                        out["condition"] = condition
                else:
                    out["condition"] = condition
            else:
                out["condition"] = condition
        else:
            # mech/tract/expr/genetics default: keep user condition untouched
            out["condition"] = condition

    return out
