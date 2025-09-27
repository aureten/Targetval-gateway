from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional, List, Dict, Any

from fastapi import HTTPException

from .net import get_json, post_json
from .ontology import resolve_efo, diseases_for_condition_if_endpoint

# ----------------------------- Validation -------------------------------------

_SYMBOLISH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,15}$")
_EFO_RE = re.compile(r"^EFO[_:]\d{7}$", re.IGNORECASE)

def validate_symbol(symbol: Optional[str], field_name: str = "symbol") -> None:
    if symbol is None or not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: value must be a non-empty string")
    s = symbol.strip()
    if s.upper().startswith("ENSG"):
        return
    if not _SYMBOLISH_RE.match(s):
        return  # permissive: don't hard-fail odd-but-valid tokens

def validate_condition(condition: Optional[str], field_name: str = "condition") -> None:
    if condition is None or not isinstance(condition, str) or not condition.strip():
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: value must be a non-empty string")

def normalize_gene_symbol(symbol: Optional[str] = None, gene: Optional[str] = None, field_name: str = "symbol") -> str:
    value = symbol or gene
    if value is None or not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: value must be a non-empty string (gene or symbol)")
    return value.strip()

def is_symbolish(value: Optional[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    up = value.strip().upper()
    if up.startswith("ENSG") or (":" in up) or ("_" in up):
        return False
    return bool(_SYMBOLISH_RE.match(up))

def coerce_efo_id(efo: Optional[str]) -> Optional[str]:
    if efo is None:
        return None
    s = efo.strip().upper().replace(":", "_")
    if not _EFO_RE.match(s):
        raise HTTPException(status_code=400, detail="Invalid efo: expected EFO_####### (e.g., EFO_0004611)")
    return s

# ---------------------- Normalization & Mapping -------------------------------

# local alias map for cheap normalization
_ALIAS_GENE_MAP: Dict[str, str] = {
    "CB1": "CNR1",
    "CB-1": "CNR1",
    "CNR-1": "CNR1",
    "TGFR2": "TGFBR2",
}

@lru_cache(maxsize=2048)
def normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=400, detail="Invalid symbol")
    up = symbol.strip().upper()
    if up in _ALIAS_GENE_MAP:
        return _ALIAS_GENE_MAP[up]
    # fast accept if token-like
    if up.isalnum() or "-" in up:
        pass

    # UniProt probe for canonical gene name (organism 9606)
    try:
        url = ("https://rest.uniprot.org/uniprotkb/search?"
               f"query={up}+AND+organism_id:9606&fields=genes&format=json&size=1")
        js = get_json(url)
        res = js.get("results", []) if isinstance(js, dict) else []
        if res:
            genes = res[0].get("genes") or []
            for g in genes:
                gn = (g.get("geneName") or {}).get("value")
                if gn:
                    return str(gn).upper()
            for g in genes:
                for syn in (g.get("synonyms") or []):
                    v = syn.get("value")
                    if v:
                        return str(v).upper()
    except Exception:
        pass
    return up

@lru_cache(maxsize=2048)
def symbol_to_ensembl(symbol: str) -> Optional[str]:
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    sym = symbol.strip().upper()

    # Ensembl REST xrefs
    try:
        url = f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{sym}"
        js = get_json(url, params={"content-type": "application/json"})
        if isinstance(js, list):
            for rec in js:
                _id = (rec.get("id") or "").upper()
                _type = (rec.get("type") or "").lower()
                if _id.startswith("ENSG") and ("gene" in _type):
                    return _id
            for rec in js:
                _id = (rec.get("id") or "").upper()
                if _id.startswith("ENSG"):
                    return _id
    except Exception:
        pass

    # OpenTargets GraphQL fallback
    try:
        url = "https://api.platform.opentargets.org/api/v4/graphql"
        query = """
        query ($q:String!){
          search(queryString:$q){
            targets { id approvedSymbol }
          }
        }"""
        data = post_json(url, {"query": query, "variables": {"q": sym}})
        targets = (((data.get("data") or {}).get("search") or {}).get("targets") or [])
        for t in targets:
            if (t.get("approvedSymbol") or "").upper() == sym and str(t.get("id","")).upper().startswith("ENSG"):
                return t.get("id")
        for t in targets:
            _id = (t.get("id") or "").upper()
            if _id.startswith("ENSG"):
                return t.get("id")
    except Exception:
        pass
    return None

# ------------------------- Tissue heuristics ----------------------------------

def _parse_hpa_tissues(rows: List[Dict[str, Any]], limit: int) -> List[str]:
    counts: Dict[str, int] = {}
    for r in rows:
        tstr = r.get("rna_tissue") or r.get("tissue") or ""
        parts = [p.strip() for p in str(tstr).replace(";", ",").split(",") if p.strip()]
        for p in parts:
            counts[p] = counts.get(p, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in ranked[:max(1, limit)]]

@lru_cache(maxsize=512)
def primary_tissues_for_target(symbol: str, limit: int = 3) -> List[str]:
    sym = normalize_symbol(symbol)
    # curated seeds (extend as needed)
    curated = {
        "PCSK9": ["Liver"],
        "LDLR": ["Liver"],
        "IL6": ["Blood", "Liver"],
    }
    if sym in curated:
        return curated[sym][:max(1, limit)]
    # HPA probe
    try:
        url = ("https://www.proteinatlas.org/api/search_download.php"
               f"?format=json&columns=gene,rna_tissue,rna_gtex&search={sym}")
        js = get_json(url)
        rows = js if isinstance(js, list) else []
        tissues = _parse_hpa_tissues(rows, limit)
        if tissues:
            return tissues
    except Exception:
        pass
    return []

# ---------------------- Per-module input normalization ------------------------

def infer_bucket_from_module(name: str) -> str:
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
    for token in ("genetics","assoc","expr","mech","tract","clin","comp"):
        if token in n:
            return token
    return "other"

def normalize_for_module(
    module_name: str,
    symbol: Optional[str] = None,
    gene: Optional[str] = None,
    condition: Optional[str] = None,
    efo: Optional[str] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # choose & normalize symbol
    sym_in = symbol or (gene if is_symbolish(gene) else None)
    if sym_in:
        sym_norm = normalize_symbol(sym_in)
        out["symbol"] = sym_norm
        ens = symbol_to_ensembl(sym_norm)
        if ens:
            out["ensembl_id"] = ens

    # EFO resolution (handles endpoint phrases)
    efo_id = resolve_efo(efo, condition)
    if efo_id:
        out["efo"] = efo_id

    bucket = infer_bucket_from_module(module_name)

    # condition policy
    if condition:
        endpoint_diseases = diseases_for_condition_if_endpoint(condition)
        if bucket == "clin":
            out["condition"] = endpoint_diseases[0] if endpoint_diseases else condition
        elif bucket == "assoc":
            if endpoint_diseases and "symbol" in out:
                tissues = primary_tissues_for_target(out["symbol"], limit=1)
                out["condition"] = tissues[0] if tissues else condition
            else:
                out["condition"] = condition
        else:
            out["condition"] = condition

    return out
