# app/routers/targetval_router.py
"""
TARGETVAL Gateway — Best-of build (live-only, no keys, no stubs)

- Richest public data sources with robust fallbacks (per bucket)
- User-friendly normalization (gene aliases, Ensembl, EFO term/ID)
- Cross-dataset synthesis endpoints (TargetCard + Graph)
- Literature layer (Europe PMC / PubMed) to surface mechanistic angles (e.g., ECL2 in GPCRs)
- Consistent Evidence envelope across routes

Public-only: absolutely no API keys required.

Last updated: 29 Sep 2025
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import urllib.parse
import gzip
import io
import itertools
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

try:
    from app.utils.validation import validate_symbol, validate_condition  # preferred package path
except Exception:
    # Fallback for deployments where validation.py sits at repo root
    from validation import validate_symbol, validate_condition

router = APIRouter()

# ------------------------------ Models ---------------------------------------

class Evidence(BaseModel):
    status: str              # "OK" | "NO_DATA" | "ERROR"
    source: str              # upstream(s) used
    fetched_n: int           # count before slicing
    data: Dict[str, Any]     # payload
    citations: List[str]     # URLs used
    fetched_at: float        # UNIX ts

# ------------------------ Outbound HTTP (bounded) ----------------------------

CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL: int = int(os.getenv("CACHE_TTL_SECONDS", str(24 * 60 * 60)))
DEFAULT_TIMEOUT = httpx.Timeout(float(os.getenv("OUTBOUND_TIMEOUT_S", "12.0")), connect=6.0)
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": os.getenv("OUTBOUND_USER_AGENT", "TargetVal/2.0 (+https://github.com/aureten/Targetval-gateway)"),
    "Accept": "application/json",
}
REQUEST_BUDGET_S: float = float(os.getenv("REQUEST_BUDGET_S", "25.0"))
OUTBOUND_TRIES: int = int(os.getenv("OUTBOUND_TRIES", "2"))
BACKOFF_BASE_S: float = float(os.getenv("BACKOFF_BASE_S", "0.6"))
MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "8"))
_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

def _now() -> float: return time.time()

async def _get_json(url: str, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None) -> Any:
    cached = CACHE.get(url)
    if cached and (_now() - cached.get("timestamp", 0) < CACHE_TTL):
        return cached["data"]
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                remaining = REQUEST_BUDGET_S - (_now() - t0)
                if remaining <= 0: break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(client.get(url, headers=merged), timeout=remaining)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        backoff = min((2**(attempt-1))*BACKOFF_BASE_S, 3.0) + random.random()*0.2
                        await asyncio.sleep(backoff); continue
                    resp.raise_for_status()
                    try:
                        data = resp.json()
                    except Exception:
                        try:
                            buf = io.BytesIO(resp.content)
                            with gzip.GzipFile(fileobj=buf) as gz:
                                data = json.loads(gz.read().decode("utf-8"))
                        except Exception as ge:
                            last_err = ge; raise
                    CACHE[url] = {"data": data, "timestamp": _now()}
                    return data
                except Exception as e:
                    last_err = e
                    backoff = min((2**(attempt-1))*BACKOFF_BASE_S, 3.0) + random.random()*0.2
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {last_err}")

async def _get_text(url: str, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None) -> str:
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                remaining = REQUEST_BUDGET_S - (_now() - t0)
                if remaining <= 0: break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(client.get(url, headers=merged), timeout=remaining)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        backoff = min((2**(attempt-1))*BACKOFF_BASE_S, 3.0) + random.random()*0.2
                        await asyncio.sleep(backoff); continue
                    resp.raise_for_status()
                    return resp.text
                except Exception as e:
                    last_err = e
                    backoff = min((2**(attempt-1))*BACKOFF_BASE_S, 3.0) + random.random()*0.2
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET text failed for {url}: {last_err}")

async def _post_json(url: str, payload: Dict[str, Any], tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None) -> Any:
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                remaining = REQUEST_BUDGET_S - (_now() - t0)
                if remaining <= 0: break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(client.post(url, json=payload, headers=merged), timeout=remaining)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        backoff = min((2**(attempt-1))*BACKOFF_BASE_S, 3.0) + random.random()*0.2
                        await asyncio.sleep(backoff); continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as e:
                    last_err = e
                    backoff = min((2**(attempt-1))*BACKOFF_BASE_S, 3.0) + random.random()*0.2
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"POST failed for {url}: {last_err}")

async def _safe_call(coro):
    try:
        return await coro
    except HTTPException as e:
        return Evidence(status="ERROR", source=str(e.detail), fetched_n=0, data={}, citations=[], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0, data={}, citations=[], fetched_at=_now())

# --------------------------- Id resolver helpers -----------------------------

_ALIAS_GENE_MAP: Dict[str, str] = {"CB1": "CNR1", "CB-1": "CNR1", "CNR-1": "CNR1", "TGFR2": "TGFBR2"}
_COMMON_GENE_SET = {"CNR1", "IL6", "TGFBR2", "FAP"} | set(_ALIAS_GENE_MAP.keys()) | set(_ALIAS_GENE_MAP.values())

def _looks_like_gene_token(s: str) -> bool:
    if not s: return False
    tok = s.strip()
    if " " in tok: return False
    up = tok.upper()
    if up in _COMMON_GENE_SET: return True
    if 2 <= len(up) <= 12 and up.replace("-", "").isalnum():
        if any(c.isdigit() for c in up) or up.isalpha(): return True
    if up.startswith("ENSG"): return True
    return False

# Sticky caches for normalization (avoid provider-specific alias drift)
_SYMBOL_CACHE: Dict[str, str] = {}
_EFO_CACHE: Dict[str, Tuple[str, str, str, List[str]]] = {}

async def _normalize_symbol(symbol: str) -> str:
    if not symbol:
        return symbol
    up = symbol.strip().upper()
    if up in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[up]
    if up in _ALIAS_GENE_MAP:
        _SYMBOL_CACHE[up] = _ALIAS_GENE_MAP[up]
        return _ALIAS_GENE_MAP[up]
    if up in _COMMON_GENE_SET or up.startswith("ENSG"):
        _SYMBOL_CACHE[up] = up
        return up
    url = ("https://rest.uniprot.org/uniprotkb/search?"
           f"query={urllib.parse.quote(symbol)}+AND+organism_id:9606&fields=genes&format=json&size=1")
    try:
        js = await _get_json(url, tries=1)
        res = js.get("results", []) if isinstance(js, dict) else []
        if res:
            genes = res[0].get("genes") or []
            for g in genes:
                gn = g.get("geneName", {}).get("value")
                if gn:
                    return gn.upper()
            for g in genes:
                for syn in (g.get("synonyms") or []):
                    val = syn.get("value")
                    if val:
                        return val.upper()
    except Exception:
        pass
    _SYMBOL_CACHE[up] = up
    return up

async def _ensembl_from_symbol_or_id(s: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    citations: List[str] = []
    if not s: return None, None, citations
    tok = s.strip()
    if tok.upper().startswith("ENSG"): return tok, None, citations
    url = f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{urllib.parse.quote(tok)}?content-type=application/json"
    try:
        arr = await _get_json(url, tries=1); citations.append(url)
        if isinstance(arr, list):
            for rec in arr:
                if (rec.get("type") or "").upper() == "GENE" and rec.get("id", "").upper().startswith("ENSG"):
                    return rec.get("id"), tok.upper(), citations
    except Exception: pass
    url2 = f"https://rest.ensembl.org/xrefs/name/homo_sapiens/{urllib.parse.quote(tok)}?content-type=application/json"
    try:
        arr = await _get_json(url2, tries=1); citations.append(url2)
        if isinstance(arr, list):
            for rec in arr:
                if (rec.get("type") or "").upper() == "GENE" and rec.get("id", "").upper().startswith("ENSG"):
                    return rec.get("id"), tok.upper(), citations
    except Exception: pass
    return None, tok.upper(), citations

async def _resolve_efo(efo_or_term: str) -> Tuple[str, str, str, List[str]]:
    """Return (efo_id_norm, efo_uri, disease_label, citations) for term or EFO id."""
    citations: List[str] = []
    efo = (efo_or_term or "").strip()
    if not efo: return "", "", "", citations
    # sticky cache
    if efo.upper() in _EFO_CACHE:
        idn, uri, label, cites = _EFO_CACHE[efo.upper()]
        return idn, uri, label, cites.copy()
    if ":" in efo: efo = efo.replace(":", "_")
    if efo.upper().startswith("EFO_"):
        _EFO_CACHE[efo.upper()] = (efo.upper(), f"https://www.ebi.ac.uk/efo/{efo.upper()}", efo.upper(), citations.copy())
        return efo.upper(), f"https://www.ebi.ac.uk/efo/{efo.upper()}", efo.upper(), citations
    # 1) EpiGraphDB mapping
    map_url = f"https://api.epigraphdb.org/ontology/disease-efo?efo_term={urllib.parse.quote(efo)}&fuzzy=true"
    try:
        mapping = await _get_json(map_url, tries=1); citations.append(map_url)
        results = mapping.get("results", []) if isinstance(mapping, dict) else []
        if results:
            top = results[0]
            label = top.get("disease_label") or (top.get("disease") or {}).get("label")
            raw = top.get("efo_term") or (top.get("disease") or {}).get("id") or ""
            if raw:
                efo_id_norm = raw.replace(":", "_").upper()
                uri = f"https://www.ebi.ac.uk/efo/{efo_id_norm}"
                return efo_id_norm, uri, label or efo, citations
    except Exception: pass
    # 2) OLS4 search
    ols = f"https://www.ebi.ac.uk/ols4/api/search?q={urllib.parse.quote(efo)}&ontology=efo&rows=5"
    try:
        js = await _get_json(ols, tries=1); citations.append(ols)
        docs = ((js.get("response") or {}).get("docs") or []) if isinstance(js, dict) else []
        for doc in docs:
            iri = doc.get("iri") or ""
            label = doc.get("label") or efo
            tail = iri.rsplit("/", 1)[-1]
            if tail.upper().startswith("EFO_"):
                efo_id_norm = tail.upper()
                uri = f"https://www.ebi.ac.uk/efo/{efo_id_norm}"
                _EFO_CACHE[(label or efo).upper()] = (efo_id_norm, uri, label, citations.copy())
                return efo_id_norm, uri, label, citations
    except Exception: pass
    _EFO_CACHE[efo.upper()] = ("", "", efo, citations.copy())
    return "", "", efo, citations

async def _uniprot_primary_accession(symbol: str) -> Optional[str]:
    url = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene_exact:{urllib.parse.quote(symbol)}+AND+organism_id:9606"
           "&fields=accession&format=json&size=1")
    try:
        body = await _get_json(url, tries=1)
        res = body.get("results", []) if isinstance(body, dict) else []
        if res: return res[0].get("primaryAccession") or res[0].get("uniProtkbId")
    except Exception: return None
    return None

async def _extracellular_len_from_uniprot(accession: str) -> int:
    if not accession: return 0
    entry_url = f"https://rest.uniprot.org/uniprotkb/{urllib.parse.quote(accession)}.json"
    try:
        entry = await _get_json(entry_url, tries=1)
        feats = entry.get("features", []) if isinstance(entry, dict) else []
        total = 0
        for f in feats:
            if f.get("type") == "Topological domain":
                desc = (f.get("description") or "").lower()
                if "extracellular" in desc:
                    loc = f.get("location", {})
                    start = (loc.get("start") or {}).get("value")
                    end = (loc.get("end") or {}).get("value")
                    if isinstance(start, int) and isinstance(end, int) and end >= start:
                        total += (end - start + 1)
        return int(total)
    except Exception: return 0

async def _iedb_counts(symbol: str, limit: int = 25) -> Tuple[int, int]:
    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    tc_url = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    epi_n, tc_n = 0, 0
    try:
        ej = await _get_json(epi_url, tries=1); epi_n = len(ej if isinstance(ej, list) else [])
    except Exception: pass
    try:
        tj = await _get_json(tc_url, tries=1); tc_n = len(tj if isinstance(tj, list) else [])
    except Exception: pass
    return epi_n, tc_n

async def _is_gpcr(symbol: str) -> bool:
    """Heuristic GPCR check via UniProt keyword field."""
    url = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene_exact:{urllib.parse.quote(symbol)}+AND+organism_id:9606"
           "&fields=keyword&format=json&size=1")
    try:
        js = await _get_json(url, tries=1)
        for r in (js.get("results", []) if isinstance(js, dict) else []):
            kws = [k.get("label","") for k in (r.get("keywords") or [])]
            if any("G-protein coupled receptor" in k for k in kws):
                return True
    except Exception: pass
    return False

# ------------------------------ Utility --------------------------------------

@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now()}

@router.get("/status")
def status() -> Dict[str, Any]:
    return {
        "service": "targetval-gateway (best-of, live-only)",
        "time": _now(),
        "synthesis": ["/synth/targetcard", "/synth/graph", "/lit/search", "/lit/angles"],
    }

# --------------------------- Literature layer --------------------------------



async def _lit_search(query: str, limit: int = 25) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Europe PMC free-text search with PubMed E-utilities fallback.

    Returns a list of dicts with: pmid, pmcid, title, journal, year, doi.
    """
    cites: List[str] = []
    out: List[Dict[str, Any]] = []
    # Europe PMC primary
    base_epmc = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    url_epmc = f"{base_epmc}?query={urllib.parse.quote(query)}&format=json&pageSize={{min(100, limit)}}".format(min=min)
    cites.append(url_epmc)
    try:
        js = await _get_json(url_epmc, tries=1)
        if isinstance(js, dict):
            hits = (js.get("resultList", {}) or {}).get("result", []) or []
            out = [{"pmid": r.get("pmid") or r.get("id"),
                    "pmcid": r.get("pmcid"),
                    "title": r.get("title"),
                    "journal": r.get("journalTitle"),
                    "year": r.get("pubYear"),
                    "doi": r.get("doi")} for r in hits[:limit]]
    except Exception:
        pass
    if out:
        return out, cites

    # PubMed E-utilities fallback (ESearch + ESummary)
    try:
        base_ncbi = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        url_esearch = f"{base_ncbi}/esearch.fcgi?db=pubmed&retmode=json&retmax={{min(100, limit)}}&term={{urllib.parse.quote(query)}}"
        url_esearch = url_esearch.format(min=min, urllib=urllib)
        ej = await _get_json(url_esearch, tries=1); cites.append(url_esearch)
        ids = ((ej.get("esearchresult") or {}).get("idlist") or []) if isinstance(ej, dict) else []
        if ids:
            id_csv = ",".join(ids[:limit])
            url_esum = f"{base_ncbi}/esummary.fcgi?db=pubmed&retmode=json&id={id_csv}"
            sj = await _get_json(url_esum, tries=1); cites.append(url_esum)
            recs = []
            if isinstance(sj, dict):
                uidlist = (sj.get("result") or {}).get("uids") or []
                resmap = sj.get("result") or {}
                for uid in uidlist[:limit]:
                    r = resmap.get(uid) or {}
                    recs.append({"pmid": uid,
                                 "pmcid": None,
                                 "title": r.get("title"),
                                 "journal": (r.get("fulljournalname") or r.get("source")),
                                 "year": r.get("pubdate", "")[:4],
                                 "doi": (r.get("elocationid") if isinstance(r.get("elocationid"), str) and r.get("elocationid","").lower().startswith("doi") else None)})
            if recs:
                return recs, cites
    except Exception:
        pass
    return out, cites
@router.get("/lit/search", response_model=Evidence)
async def lit_search(query: str, limit: int = Query(25, ge=1, le=100)) -> Evidence:
    hits, cites = await _lit_search(query, limit=limit)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="Europe PMC", fetched_n=len(hits),
                    data={"query": query, "results": hits}, citations=cites, fetched_at=_now())

@router.get("/lit/angles", response_model=Evidence)
async def lit_angles(symbol: str, condition: Optional[str] = None, limit: int = Query(40, ge=1, le=100)) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym = await _normalize_symbol(symbol)
    gpcr = await _is_gpcr(sym)
    queries = _angle_queries(sym, condition, gpcr)
    tasks = [_lit_search(q, limit=max(5, limit//len(queries) or 5)) for q in queries[:8]]  # cap queries
    results = await asyncio.gather(*tasks)
    collated: List[Dict[str, Any]] = []
    cites: List[str] = []
    for hits, c in results:
        collated.extend(hits); cites.extend(c)
    # de-dup by pmid/pmcid
    seen = set(); dedup = []
    for r in collated:
        key = r.get("pmid") or r.get("pmcid") or (r.get("title") or "")[:40]
        if key and key not in seen:
            seen.add(key); dedup.append(r)
    return Evidence(status=("OK" if dedup else "NO_DATA"), source="Europe PMC (angles)", fetched_n=len(dedup),
                    data={"symbol": sym, "is_gpcr": gpcr, "results": dedup[:limit]}, citations=list(dict.fromkeys(cites)), fetched_at=_now())

# -------------------------- B1: Genetics & Causality -------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, efo: Optional[str] = Query(None), disease: Optional[str] = Query(None),
                       limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """GWAS Catalog associations; accepts EFO id or disease term (EFO resolved)."""
    from urllib.parse import urlencode
    validate_symbol(gene, field_name="gene")
    efo_in = efo or disease or ""
    efo_norm, _uri, disease_label, efo_cites = await _resolve_efo(efo_in) if efo_in else ("", "", "", [])
    symbol_norm = (await _normalize_symbol(gene)) or gene
    base = "https://www.ebi.ac.uk/gwas/rest/api/associations"
    params = {"size": max(20, min(200, limit)) }
    if efo_norm: params["efoId"] = efo_norm
    params["geneName"] = symbol_norm
    url = f"{base}?{urlencode(params)}"
    try:
        res = await _get_json(url, tries=1)
        rows = []
        if isinstance(res, dict):
            emb = res.get("_embedded") or {}
            rows = emb.get("associations") or emb.get("associationDtos") or []
        elif isinstance(res, list):
            rows = res
        payload = {"gene": symbol_norm, "efo_id": efo_norm or None, "disease_label": disease_label or None, "associations": rows[:limit]}
        return Evidence(status=("OK" if rows else "NO_DATA"), source="GWAS Catalog REST v2",
                        fetched_n=len(rows), data=payload, citations=[url]+efo_cites, fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=f"GWAS Catalog error: {e}", fetched_n=0,
                        data={"gene": symbol_norm, "efo_id": efo_norm or None}, citations=[url]+efo_cites, fetched_at=_now())

@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """ClinVar first; gnomAD GraphQL variants + constraint fallback."""
    validate_symbol(gene, field_name="gene")
    clinvar_url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                   f"db=clinvar&term={urllib.parse.quote(gene)}%5Bgene%5D&retmode=json")
    cites = [clinvar_url]
    try:
        js = await _get_json(clinvar_url, tries=1)
        ids = js.get("esearchresult", {}).get("idlist", [])
        if ids:
            return Evidence(status="OK", source="ClinVar E-utilities", fetched_n=len(ids),
                            data={"gene": gene, "variants": ids[:limit]}, citations=cites, fetched_at=_now())
    except Exception: pass
    gql_url = "https://gnomad.broadinstitute.org/api"
    query = {"query": """
        query ($symbol: String!) {
          gene(symbol: $symbol) {
            variants { variantId genome { ac an } exome { ac an } }
            constraint { pLI oe_lof oe_mis oe_syn lof_z mis_z syn_z }
          }
        }""", "variables": {"symbol": gene}}
    try:
        body = await _post_json(gql_url, query, tries=1)
        g = (body.get("data", {}) or {}).get("gene", {}) if isinstance(body, dict) else {}
        vars_ = g.get("variants", []) or []; constraint = g.get("constraint") or {}
        af_examples = []
        for v in vars_[: min(10, limit)]:
            ac = ((v.get("genome") or {}).get("ac") or 0) + ((v.get("exome") or {}).get("ac") or 0)
            an = ((v.get("genome") or {}).get("an") or 0) + ((v.get("exome") or {}).get("an") or 0)
            af = (float(ac) / float(an)) if an else None
            af_examples.append({"variantId": v.get("variantId"), "ac": ac, "an": an, "af": round(af, 6) if af else None})
        return Evidence(status=("OK" if vars_ or constraint else "NO_DATA"),
                        source="ClinVar (fallback gnomAD)", fetched_n=len(vars_),
                        data={"gene": gene, "gnomad_variants": vars_[:limit], "gnomad_constraint": constraint, "af_examples": af_examples},
                        citations=cites + [gql_url], fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="ClinVar+gnomAD empty", fetched_n=0,
                        data={"gene": gene, "variants": []}, citations=cites + [gql_url], fetched_at=_now())

@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """ClinGen gene–disease validity; Orphanet fallback (OMIM omitted: key required).
    Looser matching across geneSymbol/gene/name (case-insensitive). Adds G2P link when ClinGen empty."""
    from urllib.parse import quote
    validate_symbol(gene, field_name="gene")
    sym = (gene or "").upper(); citations: List[str] = []
    clingen_url = f"https://search.clinicalgenome.org/kb/gene-validity?format=json&search={quote(sym)}"
    try:
        cg = await _get_json(clingen_url, tries=1); citations.append(clingen_url)
    except Exception:
        cg = {}
    items = []
    def _match(row: Dict[str, Any]) -> bool:
        # Accept geneSymbol or gene string or nested gene label
        gs = str(row.get("geneSymbol") or "").upper()
        g1 = str(row.get("gene") or "").upper()
        gdict = row.get("gene") if isinstance(row.get("gene"), dict) else {}
        glabel = str(gdict.get("label") or gdict.get("name") or "").upper() if isinstance(gdict, dict) else ""
        return sym in {gs, g1, glabel}
    data = cg.get("data") or cg.get("items") or cg.get("results") or []
    if isinstance(data, list):
        items = [r for r in data if _match(r)]
    elif isinstance(cg, list):
        items = [r for r in cg if _match(r)]
    orphanet_items = []
    try:
        orpha_url = f"https://www.orphadata.com/cgi-bin/DiseaseGene.php?gene={quote(sym)}&format=json"
        oj = await _get_json(orpha_url, tries=1); citations.append(orpha_url)
        if isinstance(oj, dict): orphanet_items = (oj.get("associations") or oj.get("results") or [])
    except Exception: pass
    # Add Gene2Phenotype link (public) for manual review when empty
    if not items:
        g2p_link = f"https://www.ebi.ac.uk/gene2phenotype/search?search_term={quote(sym)}"
        citations.append(g2p_link)
    payload = {"gene": sym, "clingen_gene_validity": (items or [])[:limit], "orphanet": orphanet_items[:limit]}
    status = "OK" if (items or orphanet_items) else "NO_DATA"
    return Evidence(status=status, source="ClinGen (+ Orphanet)", fetched_n=len(items) + len(orphanet_items),
                    data=payload, citations=citations, fetched_at=_now())


@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(gene: str, efo: str = Query(...), limit: int = Query(25, ge=1, le=200)) -> Evidence:
    """EpiGraphDB MR-Base multi-SNP primary; OpenGWAS search link secondary.
    More tolerant to EFO/synonym naming; returns candidate outcomes when MR is empty."""
    from urllib.parse import quote
    validate_symbol(gene, field_name="gene"); validate_symbol(efo, field_name="efo")
    # Resolve EFO → (id, uri, label)
    efo_id, efo_uri, efo_label, efo_cites = await _resolve_efo(efo)
    cites: List[str] = [] + efo_cites
    # Build outcome candidates: prefer canonical EFO id, then label, then common synonyms
    candidates: List[str] = []
    if efo_id: candidates.append(efo_id)
    if efo_label: candidates.append(efo_label)
    label_up = (efo_label or efo).upper()
    _SYN = {
        "CORONARY ARTERY DISEASE": ["CAD", "CORONARY HEART DISEASE", "CHD"],
        "TYPE 2 DIABETES MELLITUS": ["T2D", "TYPE II DIABETES", "TYPE 2 DIABETES"],
        "BODY MASS INDEX": ["BMI"],
        "MYOCARDIAL INFARCTION": ["HEART ATTACK", "MI"],
        "ALZHEIMER'S DISEASE": ["ALZHEIMER DISEASE", "AD"],
    }
    for k,v in _SYN.items():
        if label_up == k: candidates.extend(v)
    # Deduplicate while preserving order
    seen=set(); candidates=[x for x in candidates if not (x in seen or seen.add(x))]
    mr_hits = []
    chosen_outcome = None
    for outcome in candidates or [efo]:
        mr_api = f"https://api.epigraphdb.org/mr?exposure={quote(gene)}&outcome={quote(outcome)}"
        try:
            mj = await _get_json(mr_api, tries=1); cites.append(mr_api)
            rows = (mj.get("results") or mj.get("data") or []) if isinstance(mj, dict) else (mj if isinstance(mj, list) else [])
            if rows:
                mr_hits = rows
                chosen_outcome = outcome
                break
        except Exception:
            continue
    # OpenGWAS: fetch search hits for transparency + template
    search_q = (efo_id or efo_label or efo).replace(":", "_")
    og_search = f"https://gwas.mrcieu.ac.uk/api/v1/studies?query={quote(search_q)}"
    og_hits = []
    try:
        og = await _get_json(og_search, tries=1); cites.append(og_search)
        og_hits = (og.get("data") or og.get("studies") or []) if isinstance(og, dict) else (og if isinstance(og, list) else [])
    except Exception:
        pass
    mr_tpl = "https://api.opengwas.io/api/v1/mr?exposure={exposure_id}&outcome={outcome_id}"
    payload = {"gene": gene, "efo_id": efo_id or "", "efo_label": efo_label or efo, "mr_epigraphdb": mr_hits[:limit],
               "chosen_outcome": chosen_outcome, "candidate_outcomes": candidates, "openGWAS_hits": og_hits[:10],
               "openGWAS_search_url": og_search, "mr_endpoint_template": mr_tpl}
    status = "OK" if (mr_hits or og_hits) else "NO_DATA"
    return Evidence(status=status, source="EpiGraphDB MR-Base + OpenGWAS link", fetched_n=len(mr_hits),
                    data=payload, citations=cites, fetched_at=_now())


@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_symbol(gene, field_name="gene")
    q = f"{gene} AND taxon:9606"
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(q)}&page_size={limit}"
    try:
        js = await _get_json(url, tries=1)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(status=("OK" if results else "NO_DATA"), source="RNAcentral",
                        fetched_n=len(results), data={"gene": gene, "lncRNAs": results[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="RNAcentral empty", fetched_n=0,
                        data={"gene": gene, "lncRNAs": []}, citations=[url], fetched_at=_now())

@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(gene: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    validate_symbol(gene, field_name="gene")
    mirnet_url = "https://api.mirnet.ca/table/gene"
    payload = {"org": "hsa", "idOpt": "symbol", "myList": gene, "selSource": "All"}
    try:
        r = await _post_json(mirnet_url, payload, tries=1)
        rows = r.get("data", []) if isinstance(r, dict) else (r or [])
        if rows:
            simplified = [{"miRNA": it.get("miRNA") or it.get("mirna") or it.get("ID"),
                           "target": it.get("Target") or it.get("Gene") or gene,
                           "evidence": it.get("Category") or it.get("Evidence") or it.get("Source"),
                           "pmid": it.get("PMID") or it.get("PubMedID"),
                           "source_db": it.get("Source") or "miRNet"} for it in rows[:limit]]
            return Evidence(status="OK", source="miRNet 2.0", fetched_n=len(rows),
                            data={"gene": gene, "interactions": simplified}, citations=[mirnet_url], fetched_at=_now())
    except Exception: pass
    encori_url = ("https://rnasysu.com/encori/api/miRNATarget/"
                  f"?assembly=hg38&geneType=mRNA&miRNA=all&clipExpNum=1&degraExpNum=0&pancancerNum=0&programNum=1&program=TargetScan"
                  f"&target={urllib.parse.quote(gene)}&cellType=all")
    try:
        tsv = await _get_text(encori_url, tries=1)
        lines = [ln for ln in tsv.splitlines() if ln.strip()]
        if len(lines) <= 1:
            return Evidence(status="NO_DATA", source="ENCORI/starBase empty", fetched_n=0,
                            data={"gene": gene, "interactions": []}, citations=[encori_url], fetched_at=_now())
        header = [h.strip() for h in lines[0].split("\t")]
        out = []
        for ln in lines[1:][:limit]:
            cols = ln.split("\t"); row = dict(zip(header, cols))
            out.append({"miRNA": row.get("miRNA") or row.get("miRNA_Name"),
                        "target": row.get("Target_Gene") or gene,
                        "support": row.get("SupportType") or row.get("Support_Type"),
                        "pmid": row.get("PMID") or row.get("CLIP-Data_PubMed_ID"),
                        "cell_type": row.get("Cell_Type")})
        return Evidence(status="OK", source="ENCORI/starBase", fetched_n=len(out),
                        data={"gene": gene, "interactions": out}, citations=[encori_url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="NO_DATA", source=f"ENCORI unavailable: {e}", fetched_n=0,
                        data={"gene": gene, "interactions": []}, citations=[mirnet_url, encori_url], fetched_at=_now())


@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(gene: str, limit: int = Query(50, ge=1, le=200), ensembl: Optional[str] = None) -> Evidence:
    """s/eQTL evidence. Primary: eQTL Catalogue sQTL API; Fallback: GTEx v2 independent sQTL → eQTL."""
    from urllib.parse import urlencode, quote
    validate_symbol(gene, field_name="gene")
    ensg = ensembl; sym_cites: List[str] = []
    if not ensg:
        ensg, _sym, sym_cites = await _ensembl_from_symbol_or_id(gene)
    if not ensg:
        return Evidence(status="ERROR", source="EQTLCatalogue/GTEx", fetched_n=0,
                        data={"message": "Could not resolve Ensembl gene id", "input": gene},
                        citations=sym_cites, fetched_at=_now())

    citations = list(sym_cites)
    rows: List[Any] = []

    # Primary: eQTL Catalogue sQTL
    cat_url = f"https://www.ebi.ac.uk/eqtl/api/stats/gene-sqtl?gene_id={quote(ensg)}"
    try:
        js = await _get_json(cat_url, tries=1); citations.append(cat_url)
        if isinstance(js, dict):
            rows = js.get("results") or js.get("data") or js.get("sqtls") or []
        elif isinstance(js, list):
            rows = js
    except Exception:
        pass

    # Fallback: GTEx v2 independent sQTL / eQTL
    if not rows:
        # get gencodeId with version if possible
        gencode_id = ensg
        try:
            url_lookup = f"https://rest.ensembl.org/lookup/id/{quote(ensg)}?expand=0;content-type=application/json"
            info = await _get_json(url_lookup, tries=1); citations.append(url_lookup)
            if isinstance(info, dict) and info.get("version"):
                gencode_id = f"{ensg}.{int(info.get('version'))}"
        except Exception:
            gencode_id = ensg
        url_sqtl = f"https://gtexportal.org/api/v2/association/independentSqtl?{urlencode({'gencodeId': gencode_id})}"
        url_eqtl = f"https://gtexportal.org/api/v2/association/independentEqtl?{urlencode({'gencodeId': gencode_id})}"
        try:
            js = await _get_json(url_sqtl, tries=1); citations.append(url_sqtl)
            if isinstance(js, dict):
                rows = js.get("data") or js.get("sqtls") or []
            elif isinstance(js, list):
                rows = js
            if not rows:
                js2 = await _get_json(url_eqtl, tries=1); citations.append(url_eqtl)
                if isinstance(js2, dict):
                    rows = js2.get("data") or js2.get("eqtls") or []
                elif isinstance(js2, list):
                    rows = js2
        except Exception as e:
            return Evidence(status="ERROR", source=f"GTEx API error: {e}", fetched_n=0,
                            data={"gene": gene, "ensembl_id": ensg}, citations=citations, fetched_at=_now())

    return Evidence(status=("OK" if rows else "NO_DATA"), source="eQTL Catalogue → GTEx fallback", fetched_n=len(rows),
                    data={"gene": gene, "ensembl_id": ensg, "sqtl": rows[:limit]}, citations=citations, fetched_at=_now())

@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(gene: str, flank_kb: int = Query(50, ge=0, le=1000)) -> Evidence:
    from urllib.parse import quote
    validate_symbol(gene, field_name="gene")
    ensg, _sym, ensg_cites = await _ensembl_from_symbol_or_id(gene)
    spans = None
    if ensg:
        try:
            url = f"https://rest.ensembl.org/lookup/id/{quote(ensg)}?expand=0;content-type=application/json"
            info = await _get_json(url, tries=1); ensg_cites.append(url)
            if isinstance(info, dict) and info.get("start") and info.get("end"):
                chrom = info.get("seq_region_name")
                start = max(1, int(info.get("start")) - flank_kb * 1000)
                end = int(info.get("end")) + flank_kb * 1000
                spans = {"chrom": str(chrom), "start": start, "end": end, "assembly": info.get("assembly_name")}
        except Exception: pass
    links = []
    if spans:
        links += [
            {"label": "Roadmap Epigenomics portal", "url": "http://egg2.wustl.edu/roadmap/web_portal/", "note": "WashU Browser"},
            {"label": "Roadmap on AWS (registry)", "url": "https://registry.opendata.aws/roadmapepigenomics/", "note": "S3 datasets"},
            {"label": "BLUEPRINT Data portal", "url": "https://projects.ensembl.org/blueprint/", "note": "Hematopoietic epigenomes"},
            {"label": "DeepBlue Epigenomic Data Server", "url": "http://deepblue.mpi-inf.mpg.de/", "note": "Aggregated region sets"},
        ]
    return Evidence(status=("OK" if links else "NO_DATA"), source="Roadmap + BLUEPRINT (links)", fetched_n=len(links),
                    data={"gene": gene, "ensembl_id": ensg, "region": spans, "resources": links},
                    citations=ensg_cites + ["https://egg2.wustl.edu/roadmap/web_portal/",
                                            "https://registry.opendata.aws/roadmapepigenomics/",
                                            "https://projects.ensembl.org/blueprint/",
                                            "http://deepblue.mpi-inf.mpg.de/"], fetched_at=_now())

# ------------------- B2: Association & Perturbation --------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(condition: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_condition(condition, field_name="condition")
    url_gtex = f"https://gtexportal.org/api/v2/association/genesByTissue?tissueSiteDetail={urllib.parse.quote(condition)}"
    genes, cites = [], []
    try:
        js = await _get_json(url_gtex, tries=1); cites.append(url_gtex)
        genes = js.get("genes", []) if isinstance(js, dict) else []
    except Exception: pass
    geo_q = f'({condition}) AND ("expression profiling by high throughput sequencing"[Filter] OR "expression profiling by array"[Filter])'
    url_geo = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
               f"?db=gds&term={urllib.parse.quote(geo_q)}&retmode=json&retmax={min(200, limit*2)}")
    geo_ids = []
    try:
        gj = await _get_json(url_geo, tries=1); cites.append(url_geo)
        geo_ids = (gj.get("esearchresult", {}) or {}).get("idlist", []) or []
    except Exception: pass
    return Evidence(status=("OK" if (genes or geo_ids) else "NO_DATA"),
                    source="GTEx genesByTissue + GEO E-utilities",
                    fetched_n=(len(genes) + len(geo_ids)),
                    data={"condition": condition, "gtex_genes": genes[:limit], "geo_datasets": geo_ids[:limit]},
                    citations=cites, fetched_at=_now())

@router.get("/assoc/geo-arrayexpress", response_model=Evidence)
async def assoc_geo_arrayexpress(condition: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """GEO + ArrayExpress (via BioStudies) transcriptome study search."""
    validate_condition(condition, field_name="condition")
    cites: List[str] = []
    geo_q = f'({condition}) AND (differential[Filter] OR "expression profiling")'
    url_geo = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
               f"?db=gds&term={urllib.parse.quote(geo_q)}&retmode=json&retmax={min(200, limit*2)}")
    geo_ids = []
    try:
        gj = await _get_json(url_geo, tries=1); cites.append(url_geo)
        geo_ids = (gj.get("esearchresult", {}) or {}).get("idlist", []) or []
    except Exception: pass
    ae_url = f"https://www.ebi.ac.uk/biostudies/api/v1/studies?search={urllib.parse.quote(condition)}&pageSize={min(200, limit*2)}"
    ae_items = []
    try:
        aj = await _get_json(ae_url, tries=1); cites.append(ae_url)
        if isinstance(aj, dict): ae_items = aj.get("hits") or aj.get("studies") or aj.get("content") or []
    except Exception: pass
    return Evidence(status=("OK" if (geo_ids or ae_items) else "NO_DATA"), source="GEO + ArrayExpress (BioStudies)",
                    fetched_n=(len(geo_ids) + len(ae_items)),
                    data={"condition": condition, "geo_ids": geo_ids[:limit], "arrayexpress": ae_items[:limit]},
                    citations=cites, fetched_at=_now())

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(condition: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_condition(condition, field_name="condition")
    cites: List[str] = []; records: List[Any] = []
    url_pdb = ("https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search"
               f"?search={urllib.parse.quote(condition)}")
    try:
        js = await _get_json(url_pdb, tries=1); cites.append(url_pdb)
        if isinstance(js, dict):
            records = js.get("items", js.get("proteins", js.get("results", []))) or []
        elif isinstance(js, list):
            records = js
    except Exception: pass
    url_pride = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    pride_items = []
    if not records:
        try:
            pj = await _get_json(url_pride, tries=1); cites.append(url_pride)
            pride_items = pj if isinstance(pj, list) else []
        except Exception: pass
    px_q = f"https://proteomecentral.proteomexchange.org/cgi/GetDataset?format=JSON&query={urllib.parse.quote(condition)}"
    px_items = []
    try:
        pxj = await _get_json(px_q, tries=1); cites.append(px_q)
        if isinstance(pxj, dict):
            px_items = (pxj.get("list") or [])[:limit]
    except Exception: pass
    return Evidence(status=("OK" if (records or pride_items or px_items) else "NO_DATA"),
                    source="ProteomicsDB + PRIDE + ProteomeXchange",
                    fetched_n=(len(records) + len(pride_items) + len(px_items)),
                    data={"condition": condition, "proteomicsdb": (records or [])[:limit],
                          "pride_projects": (pride_items or [])[:limit], "proteomexchange": (px_items or [])[:limit]},
                    citations=cites, fetched_at=_now())

@router.post("/assoc/cptac", response_model=Evidence)
async def assoc_cptac(condition: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """CPTAC via NCI PDC GraphQL; POST because GraphQL."""
    gql = "https://pdc.cancer.gov/graphql"
    query = {"query": """
        query Search($q: String!, $size: Int!) {
          projects(filter: { project_name: $q }, first: $size) {
            project_name disease_type program_name project_submitter_id
          }
        }""", "variables": {"q": condition, "size": min(200, limit)}}
    try:
        js = await _post_json(gql, query, tries=1)
        rows = js.get("data", {}).get("projects", []) if isinstance(js, dict) else []
        return Evidence(status=("OK" if rows else "NO_DATA"), source="NCI PDC GraphQL (CPTAC)",
                        fetched_n=len(rows), data={"condition": condition, "projects": rows[:limit]},
                        citations=[gql], fetched_at=_now())
    except Exception as e:
        return Evidence(status="NO_DATA", source=f"CPTAC/PDC error: {e}", fetched_n=0,
                        data={"condition": condition, "projects": []}, citations=["https://pdc.cancer.gov/graphql"], fetched_at=_now())

@router.get("/assoc/tabula-hca", response_model=Evidence)
async def assoc_tabula_hca(condition: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    """Tabula Sapiens + Human Cell Atlas quick lookups."""
    validate_condition(condition, field_name="condition")
    cites: List[str] = []
    tabula_url = "https://tabula-sapiens-portal.ds.czbiohub.org/api/genes?search=" + urllib.parse.quote(condition)
    tabula_hits = []
    try:
        tj = await _get_json(tabula_url, tries=1); cites.append(tabula_url)
        if isinstance(tj, dict): tabula_hits = tj.get("genes") or tj.get("results") or []
    except Exception: pass
    hca_url = "https://service.azul.data.humancellatlas.org/index/projects?filters=%7B%7D&size=10"
    hca_hits = []
    try:
        hj = await _get_json(hca_url, tries=1); cites.append(hca_url)
        if isinstance(hj, dict): hca_hits = (hj.get("hits") or [])[:limit]
    except Exception: pass
    return Evidence(status=("OK" if (tabula_hits or hca_hits) else "NO_DATA"), source="Tabula Sapiens + HCA (portal APIs)",
                    fetched_n=(len(tabula_hits) + len(hca_hits)),
                    data={"condition": condition, "tabula": tabula_hits[:limit], "hca": hca_hits[:limit]},
                    citations=cites, fetched_at=_now())

@router.get("/assoc/depmap-achilles", response_model=Evidence)
async def assoc_depmap_achilles(condition: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    """DepMap/Achilles portal dataset list (best-effort public endpoint)."""
    depmap_url = "https://depmap.org/portal/api/v1/datasets?search=" + urllib.parse.quote(condition)
    cites = [depmap_url]
    try:
        js = await _get_json(depmap_url, tries=1)
        items = js.get("results") or js.get("datasets") or js if isinstance(js, list) else []
        return Evidence(status=("OK" if items else "NO_DATA"), source="DepMap portal datasets",
                        fetched_n=len(items), data={"condition": condition, "datasets": items[:limit]},
                        citations=cites, fetched_at=_now())
    except Exception as e:
        return Evidence(status="NO_DATA", source=f"DepMap portal error: {e}", fetched_n=0,
                        data={"condition": condition, "datasets": []}, citations=cites, fetched_at=_now())

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(condition: str, symbol: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500)) -> Evidence:
    """Prefer DepMap/Achilles; supplement with OpenTargets essentiality when symbol provided."""
    validate_condition(condition, field_name="condition")
    results: List[Any] = []; cites: List[str] = []
    depmap = await assoc_depmap_achilles(condition, limit=limit)
    if depmap and depmap.status == "OK":
        results.extend(depmap.data.get("datasets", [])[:limit]); cites.extend(depmap.citations)
    if symbol:
        gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
        q = {"query": """
            query Essentiality($symbol: String!) {
              target(approvedSymbol: $symbol) { essentiality { rows { cellLineName score source } } }
            }""", "variables": {"symbol": symbol}}
        try:
            ej = await _post_json(gql_url, q, tries=1); cites.append(gql_url)
            rows = ej.get("data", {}).get("target", {}).get("essentiality", {}).get("rows", [])
            for r in rows or []: results.append({"essentiality": r})
        except Exception: pass
    return Evidence(status=("OK" if results else "NO_DATA"), source="Achilles + OpenTargets", fetched_n=len(results),
                    data={"condition": condition, "screens": results[:limit]}, citations=cites, fetched_at=_now())

# ------------------------ B3: Expression & Inducibility ----------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None),
                              limit: int = Query(50, ge=1, le=500)) -> Evidence:
    sym_in = symbol or gene; validate_symbol(sym_in, field_name="symbol")
    sym_norm = await _normalize_symbol(sym_in)
    hpa_url = ("https://www.proteinatlas.org/api/search_download.php"
               f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(sym_norm)}")
    try:
        js = await _get_json(hpa_url, tries=1)
        records = js if isinstance(js, list) else []
        if records:
            return Evidence(status="OK", source="Human Protein Atlas search_download", fetched_n=len(records),
                            data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": records[:limit]},
                            citations=[hpa_url], fetched_at=_now())
    except Exception: pass
    uniprot_url = ("https://rest.uniprot.org/uniprotkb/search?"
                   f"query={urllib.parse.quote(sym_norm)}&format=json&size={limit}")
    try:
        body = await _get_json(uniprot_url, tries=1)
        entries = body.get("results", []) if isinstance(body, dict) else []
        if entries:
            return Evidence(status="OK", source="UniProt search", fetched_n=len(entries),
                            data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": entries[:limit]},
                            citations=[uniprot_url], fetched_at=_now())
    except Exception: pass
    atlas_url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(sym_norm)}.json"
    try:
        body = await _get_json(atlas_url, tries=1)
        results: List[Dict[str, Any]] = []
        if isinstance(body, dict):
            for exp in body.get("experiments", []) or []:
                for d in exp.get("data", []):
                    results.append({"tissue": d.get("organismPart") or d.get("tissue") or "NA",
                                    "level": (d.get("expressions", [{}])[0].get("value") if d.get("expressions") else None)})
        return Evidence(status=("OK" if results else "NO_DATA"), source="Expression Atlas (baseline)",
                        fetched_n=len(results),
                        data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": results[:limit]},
                        citations=[atlas_url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="NO_DATA", source=f"GXA empty/unavailable: {e}", fetched_n=0,
                        data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": []},
                        citations=[hpa_url, uniprot_url, atlas_url], fetched_at=_now())

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(symbol: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    url = f"https://compartments.jensenlab.org/Service?gene_names={urllib.parse.quote(sym_norm)}&format=json"
    uni = ("https://rest.uniprot.org/uniprotkb/search?"
           f"query=gene_exact:{urllib.parse.quote(sym_norm)}+AND+organism_id:9606"
           "&fields=cc_subcellular_location&format=json&size=1")
    try:
        js = await _get_json(url, tries=1)
        locs = js.get(sym_norm, []) if isinstance(js, dict) else []
        if locs:
            return Evidence(status="OK", source="COMPARTMENTS API", fetched_n=len(locs),
                            data={"symbol": symbol, "normalized_symbol": sym_norm, "localization": locs[:limit]},
                            citations=[url], fetched_at=_now())
    except Exception: pass
    try:
        uj = await _get_json(uni, tries=1)
        locs = []
        for r in (uj.get("results", []) or []):
            for c in (r.get("comments", []) or []):
                if c.get("commentType") == "SUBCELLULAR LOCATION":
                    locs.append(c)
        return Evidence(status=("OK" if locs else "NO_DATA"), source="UniProt (fallback)", fetched_n=len(locs),
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "localization": locs[:limit]},
                        citations=[url, uni], fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="COMPARTMENTS+UniProt unavailable", fetched_n=0,
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "localization": []},
                        citations=[url, uni], fetched_at=_now())

@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(symbol: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """SigCom LINCS (LDP3) + GEO gene datasets."""
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    METADATA_API = "https://ldp3.cloud/metadata-api/"
    DATA_API = "https://ldp3.cloud/data-api/api/v1/"
    ent_payload = {"filter": {"where": {"meta.symbol": {"inq": [sym_norm]}}, "fields": ["id", "meta.symbol"]}}
    ldp3_cites = [METADATA_API + "entities/find", DATA_API + "enrich/ranktwosided"]
    sigcom_hits: List[Dict[str, Any]] = []
    try:
        ents = await _post_json(METADATA_API + "entities/find", ent_payload, tries=1)
        if isinstance(ents, list) and ents:
            uuids = [e["id"] for e in ents if isinstance(e, dict) and e.get("id")]
            query = {"up_entities": uuids, "down_entities": [], "limit": min(25, limit), "database": "l1000_xpr"}
            enr = await _post_json(DATA_API + "enrich/ranktwosided", query, tries=1)
            for r in (enr.get("results") or []):
                sigcom_hits.append({
                    "uuid": r.get("uuid"),
                    "z_up": r.get("z-up"),
                    "z_down": r.get("z-down"),
                    "direction_up": r.get("direction-up"),
                    "direction_down": r.get("direction-down"),
                    "library": r.get("library"),
                    "signatureName": r.get("signatureName") or r.get("sig_name"),
                })
    except Exception: pass
    url_geo = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
               f"?db=gds&term={urllib.parse.quote(sym_norm)}%5Bgene%5D&retmode=json&retmax={min(200, limit*2)}")
    geo_ids = []
    try:
        js = await _get_json(url_geo, tries=1); 
        geo_ids = js.get("esearchresult", {}).get("idlist", [])
    except Exception: pass
    return Evidence(status=("OK" if (sigcom_hits or geo_ids) else "NO_DATA"), source="SigCom LINCS (LDP3) + GEO",
                    fetched_n=(len(sigcom_hits) + len(geo_ids)),
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "lincs": sigcom_hits[:limit], "geo_datasets": geo_ids[:limit]},
                    citations=ldp3_cites + [url_geo], fetched_at=_now())

@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(condition: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    """HPA + SCEA + cellxgene (quick search)."""
    validate_condition(condition, field_name="condition")
    search_term = condition
    normalized_symbol: Optional[str] = None
    if _looks_like_gene_token(condition):
        normalized_symbol = await _normalize_symbol(condition); search_term = normalized_symbol
    hpa_url = ("https://www.proteinatlas.org/api/search_download.php"
               f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(search_term)}")
    out_hpa: List[Dict[str, Any]] = []; cites = []
    try:
        js = await _get_json(hpa_url, tries=1); cites.append(hpa_url)
        rows = js if isinstance(js, list) else []
        for r in rows:
            if any(k in r and r[k] for k in ("cell_type", "rna_cell_type")):
                out_hpa.append({"gene": r.get("gene"), "cell_type": r.get("cell_type") or r.get("rna_cell_type"),
                                "rna_gtex": r.get("rna_gtex"), "tissue": r.get("rna_tissue")})
    except Exception: pass
    scea_hits = []
    if normalized_symbol:
        scea_url = f"https://www.ebi.ac.uk/gxa/sc/genes/{urllib.parse.quote(normalized_symbol)}.json"
        try:
            sj = await _get_json(scea_url, tries=1); cites.append(scea_url)
            if isinstance(sj, dict):
                data = sj.get("data") or []
                for rec in data:
                    exp = rec.get("experimentAccession")
                    for ct in rec.get("cellTypes", []) or []:
                        scea_hits.append({"experiment": exp, "cell_type": ct.get("name")})
        except Exception: pass
    cxg_url = "https://api.cellxgene.cziscience.com/dp/v1/collections?limit=25&query=" + urllib.parse.quote(search_term)
    cxg_items = []
    try:
        cj = await _get_json(cxg_url, tries=1); cites.append(cxg_url)
        if isinstance(cj, dict): cxg_items = (cj.get("collections") or cj.get("data") or [])[:limit]
    except Exception: pass
    payload: Dict[str, Any] = {"condition": condition, "sc_records_hpa": out_hpa[:limit], "cellxgene": cxg_items[:limit]}
    if normalized_symbol:
        payload["normalized_symbol"] = normalized_symbol; payload["sc_records_scea"] = scea_hits[:limit]
    return Evidence(status=("OK" if (out_hpa or scea_hits or cxg_items) else "NO_DATA"),
                    source="HPA + SCEA + cellxgene", fetched_n=(len(out_hpa) + len(scea_hits) + len(cxg_items)),
                    data=payload, citations=cites, fetched_at=_now())

# ----------------------- B4: Mechanistic Wiring ------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(symbol: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    pathways: List[Dict[str, Any]] = []; cites: List[str] = []
    reactome_search = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(sym_norm)}&species=Homo%20sapiens"
    try:
        js = await _get_json(reactome_search, tries=1)
        hits = js.get("results", []) if isinstance(js, dict) else []
        for h in hits:
            if (h.get("stId", "") or "").startswith("R-HSA"):
                pathways.append({"name": h.get("name"), "id": h.get("stId"), "provider": "Reactome", "score": h.get("score")})
        cites.append(reactome_search)
    except Exception: pass
    def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set(); out = []
        for it in items:
            key = (it.get("provider"), it.get("id"), it.get("name"))
            if key not in seen: seen.add(key); out.append(it)
        return out
    kegg_items: List[Dict[str, Any]] = []
    try:
        mg = f"https://mygene.info/v3/query?q=symbol:{urllib.parse.quote(sym_norm)}&species=human&fields=entrezgene"
        mg_js = await _get_json(mg, tries=1); cites.append(mg)
        entrez = None
        if isinstance(mg_js, dict):
            hits = mg_js.get("hits", [])
            if hits: entrez = hits[0].get("entrezgene")
        if entrez:
            link_url = f"http://rest.kegg.jp/link/pathway/hsa:{entrez}"
            link_txt = await _get_text(link_url, tries=1); cites.append(link_url)
            pids = []
            for line in (link_txt or "").splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1].startswith("path:"):
                    pids.append(parts[1].split(":")[1])
            for pid in pids[:limit]:
                list_url = f"http://rest.kegg.jp/list/{pid}"
                lst = await _get_text(list_url, tries=1); cites.append(list_url)
                nm = lst.split("\t", 1)[1].strip() if "\t" in lst else lst.strip()
                kegg_items.append({"name": nm, "id": pid, "provider": "KEGG"})
    except Exception: pass
    pathways.extend(kegg_items)
    try:
        wp = f"https://webservice.wikipathways.org/findPathwaysByText?query={urllib.parse.quote(sym_norm)}&species=Homo%20sapiens&format=json"
        wp_js = await _get_json(wp, tries=1); cites.append(wp)
        hits = (wp_js.get("result") or []) if isinstance(wp_js, dict) else []
        for h in hits:
            pathways.append({"name": h.get("name"), "id": h.get("id"), "provider": "WikiPathways"})
    except Exception: pass
    merged = _dedupe(pathways)
    return Evidence(status=("OK" if merged else "NO_DATA"), source="Reactome + KEGG + WikiPathways", fetched_n=len(merged),
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "pathways": merged[:limit]},
                    citations=cites, fetched_at=_now())

@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(symbol: str, species: int = Query(9606, ge=1), cutoff: float = Query(0.9, ge=0.0, le=1.0),
                   limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    map_url = "https://string-db.org/api/json/get_string_ids?identifiers={id}&species={sp}".format(id=urllib.parse.quote(sym_norm), sp=int(species))
    net_tpl = "https://string-db.org/api/json/network?identifiers={id}&species={sp}"
    try:
        ids = await _get_json(map_url, tries=1)
        if not ids:
            return Evidence(status="NO_DATA", source="STRING id lookup empty", fetched_n=0,
                            data={"symbol": symbol, "normalized_symbol": sym_norm, "neighbors": []},
                            citations=[map_url], fetched_at=_now())
        string_id = ids[0].get("stringId")
        net = await _get_json(net_tpl.format(id=string_id, sp=int(species)), tries=1)
        neighbors: List[Dict[str, Any]] = []
        for edge in net:
            score = edge.get("score") or edge.get("combined_score")
            if score and float(score) >= cutoff:
                neighbors.append({"preferredName_A": edge.get("preferredName_A"),
                                  "preferredName_B": edge.get("preferredName_B"), "score": float(score)})
        neighbors = neighbors[:limit]
        return Evidence(status=("OK" if neighbors else "NO_DATA"), source="STRING REST", fetched_n=len(neighbors),
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "species": species, "neighbors": neighbors},
                        citations=[map_url, net_tpl.format(id=string_id, sp=int(species))], fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="STRING empty/unavailable", fetched_n=0,
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "species": species, "neighbors": []},
                        citations=[map_url], fetched_at=_now())

@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(symbol: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    """Ligand–receptor interactions from OmniPath.
    We query the curated `omnipath` dataset and the non-curated `ligrecextra` dataset.
    Filter to records where the symbol appears as source or target (genesymbols=1, human only).
    """
    from urllib.parse import urlencode, quote
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    base = "https://omnipathdb.org/interactions"
    params = dict(genesymbols=1, organisms=9606, fields="sources,references")
    # Query curated set first
    curated_url = f"{base}?{urlencode({**params, 'datasets': 'omnipath', 'partners': sym_norm})}"
    extra_url = f"{base}?{urlencode({**params, 'datasets': 'ligrecextra', 'partners': sym_norm})}"
    citations: List[str] = []
    records: List[Any] = []
    try:
        js = await _get_json(curated_url, tries=1); citations.append(curated_url)
        if isinstance(js, list):
            records.extend(js)
    except Exception:
        pass
    try:
        js2 = await _get_json(extra_url, tries=1); citations.append(extra_url)
        if isinstance(js2, list):
            records.extend(js2)
    except Exception:
        pass
    # De-duplicate conservatively by (source,target,direction) if present
    seen = set(); uniq: List[Any] = []
    for rec in records:
        key = (str(rec.get("source")), str(rec.get("target")), str(rec.get("is_directed")))
        if key not in seen:
            seen.add(key); uniq.append(rec)
    status = "OK" if uniq else "NO_DATA"
    return Evidence(
        status=status,
        source="OmniPath curated + ligrecextra",
        fetched_n=len(uniq),
        data={"symbol": symbol, "normalized_symbol": sym_norm, "interactions": uniq[:limit]},
        citations=citations,
        fetched_at=_now(),
    )
@router.get("/mech/structure", response_model=Evidence)
async def mech_structure(symbol: str, limit: int = Query(100, ge=1, le=1000)) -> Evidence:
    """Structural motifs/domains + structures.
    Sources: UniProt features (TM helices, domains, binding), PDBe structures, AlphaFold prediction.
    NEW: adds pocket predictions (PrankWeb/P2Rank) and AlphaFill ligand context.
    """
    from urllib.parse import quote
    validate_symbol(symbol, field_name="symbol")
    sym = await _normalize_symbol(symbol)
    cites: List[str] = []

    async def _uniprot_accession() -> str:
        url = ("https://rest.uniprot.org/uniprotkb/search"
               f"?query=gene_exact:{quote(sym)}+AND+organism_id:9606&size=1&format=json&fields=accession")
        try:
            js = await _get_json(url, tries=1); cites.append(url)
            if isinstance(js, dict) and js.get("results"):
                r = js["results"][0]
                return r.get("primaryAccession") or r.get("accession") or ""
        except Exception:
            pass
        return ""

    async def _uniprot_features(acc: str) -> Dict[str, List[Dict[str, Any]]]:
        if not acc: return {"tm_helices": [], "domains": [], "binding_sites": []}
        url = f"https://rest.uniprot.org/uniprotkb/{quote(acc)}.json"
        tm, dom, bind = [], [], []
        try:
            js = await _get_json(url, tries=1); cites.append(url)
            feats = js.get("features", []) if isinstance(js, dict) else []
            for f in feats:
                ftype = (f.get("type") or "").upper()
                loc = f.get("location") or {}
                pos = {"start": (loc.get("start") or {}).get("value"), "end": (loc.get("end") or {}).get("value")}
                if ftype == "TRANSMEM":
                    tm.append({"type": f.get("type"), "description": f.get("description"), "location": pos})
                elif ftype in ("DOMAIN","TOPO_DOM"):
                    dom.append({"type": f.get("type"), "description": f.get("description"), "location": pos})
                elif ftype in ("BINDING","METAL","SITE"):
                    bind.append({"type": f.get("type"), "description": f.get("description"), "location": pos})
        except Exception:
            pass
        return {"tm_helices": tm, "domains": dom, "binding_sites": bind}

    async def _pdbe_entries(acc: str) -> List[Dict[str, Any]]:
        if not acc: return []
        url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{quote(acc)}"
        try:
            js = await _get_json(url, tries=1); cites.append(url)
            if isinstance(js, dict):
                return js.get(acc.lower(), []) or js.get(acc.upper(), []) or []
        except Exception:
            pass
        return []

    async def _alphafold(acc: str) -> Any:
        if not acc: return None
        url = f"https://alphafold.ebi.ac.uk/api/prediction/{quote(acc)}"
        try:
            js = await _get_json(url, tries=1); cites.append(url)
            return js if isinstance(js, list) else None
        except Exception:
            return None

    async def _alphafill_json(acc: str) -> Dict[str, Any]:
        if not acc: return {}
        url = f"https://alphafill.eu/v1/aff/{quote(acc)}/json"
        try:
            js = await _get_json(url, tries=1); cites.append(url)
            return js if isinstance(js, dict) else {}
        except Exception:
            return {}

    async def _prankweb_pockets(acc: str) -> List[Dict[str, Any]]:
        """Try public PrankWeb REST. If unavailable, return []."""
        if not acc: return []
        try:
            # create analysis job for AlphaFold model by UniProt id
            job = await _post_json("https://prankweb.cz/api/predictions", {"uniprot": acc, "database": "alphafold"}, tries=1)
            pid = (job.get("id") if isinstance(job, dict) else None)
            if not pid: return []
            res = await _get_json(f"https://prankweb.cz/api/predictions/{pid}", tries=2)
            pockets = (res.get("pockets") or []) if isinstance(res, dict) else []
            # strip heavy fields
            out = []
            for p in pockets:
                out.append({
                    "rank": p.get("rank") or p.get("order"),
                    "score": p.get("score") or p.get("probability"),
                    "center": p.get("center") or p.get("centerOfMass"),
                    "volume": p.get("volume"),
                    "residue_ids": p.get("residueIds") or [],
                    "method": "PrankWeb/P2Rank",
                })
            return out
        except Exception:
            return []

    acc = await _uniprot_accession()
    feats = await _uniprot_features(acc)
    pdbe = await _pdbe_entries(acc)
    af = await _alphafold(acc)
    pockets = await _prankweb_pockets(acc)
    affill = await _alphafill_json(acc)

    is_gpcr = any("TRANSMEM" in (d.get("type") or "").upper() for d in feats.get("tm_helices", [])) and symbol.upper().startswith("GPR")

    status = "OK" if (feats["tm_helices"] or feats["domains"] or feats["binding_sites"] or pdbe or af or pockets or affill) else "NO_DATA"
    return Evidence(
        status=status,
        source="UniProt features + PDBe + AlphaFold + PrankWeb pockets + AlphaFill",
        fetched_n=sum(len(v) for v in [feats["tm_helices"], feats["domains"], feats["binding_sites"]]) + len(pdbe or []) + len(pockets or []),
        data={
            "symbol": symbol,
            "normalized_symbol": sym,
            "uniprot_accession": acc,
            "features": feats,
            "pdbe_entries": (pdbe or [])[:limit],
            "alphafold": af,
            "pockets": pockets[:limit],
            "alphafill": affill,
            "gpcr": is_gpcr,
        },
        citations=cites,
        fetched_at=_now(),
    )
@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(symbol: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    sources: List[str] = []; interactions: List[Any] = []
    gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {"query": """
        query KnownDrugs($symbol: String!) {
          target(approvedSymbol: $symbol) { knownDrugs { rows { drugId drugName mechanismOfAction } count } }
        }""", "variables": {"symbol": sym_norm}}
    try:
        res = await _post_json(gql_url, query, tries=1)
        rows = (res.get("data", {}).get("target", {}).get("knownDrugs", {}).get("rows", []))
        if rows: sources.append("OpenTargets"); interactions.extend(rows)
    except Exception: pass
    try:
        dg_url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(sym_norm)}"
        body = await _get_json(dg_url, tries=1)
        matched = body.get("matchedTerms", []) if isinstance(body, dict) else []
        for term in matched or []:
            interactions.extend(term.get("interactions", []))
        sources.append("DGIdb")
    except Exception: pass
    try:
        chembl_search = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(sym_norm)}&format=json"
        tjs = await _get_json(chembl_search, tries=1)
        tids = [t.get("target_chembl_id") for t in (tjs.get("targets", []) if isinstance(tjs, dict) else []) if t.get("target_chembl_id")]
        mech_rows = []
        for tid in tids[:2]:
            mech_url = f"https://www.ebi.ac.uk/chembl/api/data/mechanism.json?target_chembl_id={urllib.parse.quote(tid)}"
            mjs = await _get_json(mech_url, tries=1)
            for m in (mjs.get("mechanisms", []) if isinstance(mjs, dict) else []): mech_rows.append(m)
        if mech_rows: sources.append("ChEMBL")
        for m in mech_rows:
            interactions.append({"drugId": m.get("molecule_chembl_id"),
                                 "drugName": m.get("molecule_pref_name"),
                                 "mechanismOfAction": m.get("mechanism_of_action")})
    except Exception: pass
    status = "OK" if interactions else "NO_DATA"
    cites = [gql_url, "https://dgidb.org/api", "https://www.ebi.ac.uk/chembl/api/data/"]
    return Evidence(status=status, source=(" + ".join(sources) if sources else "OpenTargets+DGIdb+ChEMBL"),
                    fetched_n=len(interactions), data={"symbol": symbol, "normalized_symbol": sym_norm, "interactions": interactions[:limit]},
                    citations=cites, fetched_at=_now())

@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(symbol: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    """Small-molecule ligandability.
    Primary: OpenTargets Platform GraphQL (target.tractability).
    Complement: ChEMBL target search (by symbol) for quick sanity check.
    Returns NO_DATA (not ERROR) if neither source has records.
    """
    from urllib.parse import quote
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    ensg, _sym, ensg_cites = await _ensembl_from_symbol_or_id(sym_norm)
    citations: List[str] = list(ensg_cites)
    tract: List[Any] = []

    # OpenTargets GraphQL tractability
    if ensg:
        gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
        query = {
            "query": """
                query ($id: String!) {
                  target(ensemblId: $id) {
                    tractability { modality label value }
                  }
                }""",
            "variables": {"id": ensg},
        }
        try:
            res = await _post_json(gql_url, query, tries=1)
            citations.append(gql_url)
            tract = (res.get("data", {}).get("target", {}).get("tractability") or []) if isinstance(res, dict) else []
        except Exception:
            # GraphQL down → non-fatal
            pass

    # ChEMBL complement
    chembl_targets: List[Any] = []
    chembl_url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={quote(sym_norm)}&format=json"
    try:
        js = await _get_json(chembl_url, tries=1); citations.append(chembl_url)
        if isinstance(js, dict):
            chembl_targets = js.get("targets", []) or js.get("items", []) or []
    except Exception:
        pass

    status = "OK" if (tract or chembl_targets) else "NO_DATA"
    return Evidence(
        status=status,
        source="OpenTargets GraphQL tractability + ChEMBL complement",
        fetched_n=(len(tract) + len(chembl_targets)),
        data={
            "symbol": symbol,
            "normalized_symbol": sym_norm,
            "ensembl_id": ensg,
            "tractability": tract[:limit],
            "chembl_targets": chembl_targets[:limit],
        },
        citations=citations,
        fetched_at=_now(),
    )
@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(symbol: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """SAbDab/Thera-SAbDab primary; PDBe supplemental."""
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    sabdab_url = f"https://opig.stats.ox.ac.uk/webapps/newsabdab/sabdab/search/?target={urllib.parse.quote(sym_norm)}&output=json"
    ther_url = f"https://opig.stats.ox.ac.uk/webapps/therasabdab/search/?target={urllib.parse.quote(sym_norm)}&output=json"
    cites = []; sab_hits = []; ther_hits = []
    try:
        sj = await _get_json(sabdab_url, tries=1); cites.append(sabdab_url)
        if isinstance(sj, list): sab_hits = sj
        elif isinstance(sj, dict): sab_hits = sj.get("results") or sj.get("entries") or []
    except Exception: pass
    try:
        tj = await _get_json(ther_url, tries=1); cites.append(ther_url)
        if isinstance(tj, list): ther_hits = tj
        elif isinstance(tj, dict): ther_hits = tj.get("results") or tj.get("entries") or []
    except Exception: pass
    pdbe_url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(sym_norm)}"
    pdbe_entries: List[Any] = []
    try:
        js = await _get_json(pdbe_url, tries=1); cites.append(pdbe_url)
        if isinstance(js, dict):
            for _, vals in js.items():
                if isinstance(vals, list): pdbe_entries.extend(vals)
    except Exception: pass
    out = [{"source": "SAbDab", "entry": e} for e in sab_hits[:limit]] + [{"source": "Thera-SAbDab", "entry": e} for e in ther_hits[:limit]]
    if not out and pdbe_entries:
        out = [{"source": "PDBe", "entry": e} for e in pdbe_entries[:limit]]
    return Evidence(status=("OK" if out else "NO_DATA"),
                    source="SAbDab + Thera-SAbDab (+ PDBe)",
                    fetched_n=len(out),
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "antibody_targets": out},
                    citations=cites, fetched_at=_now())


@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(symbol: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    api_url = f"https://aptamer.ribocentre.org/api/?search={urllib.parse.quote(sym_norm)}"
    citations: List[str] = [api_url]
    try:
        js = await _get_json(api_url, tries=1)
        items = js.get("results") or js.get("items") or js.get("data") or js.get("entries") or []
        out = []
        for it in items:
            ligand = (it.get("Ligand") or it.get("Target") or it.get("ligand") or "")
            if isinstance(ligand, str) and sym_norm.lower() in ligand.lower():
                out.append({"id": it.get("id") or it.get("Name") or it.get("Sequence Name") or it.get("name"),
                            "ligand": ligand, "sequence": it.get("Sequence") or it.get("sequence"),
                            "kd": it.get("Affinity (Kd)") or it.get("Kd") or it.get("affinity"),
                            "year": it.get("Discovery Year") or it.get("year"),
                            "ref": it.get("Article name") or it.get("reference")})
        if out:
            return Evidence(status="OK", source="Ribocentre Aptamer API", fetched_n=len(out),
                            data={"symbol": symbol, "normalized_symbol": sym_norm, "aptamers": out[:limit]},
                            citations=citations, fetched_at=_now())
    except Exception:
        pass

    # PubMed fallback: search for aptamer papers mentioning the symbol
    q = f'("{sym_norm}"[Title/Abstract]) AND (aptamer[Title/Abstract])'
    try:
        base_ncbi = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        url_esearch = f"{base_ncbi}/esearch.fcgi?db=pubmed&retmode=json&retmax={{limit}}&term={{urllib.parse.quote(q)}}"
        url_esearch = url_esearch.format(limit=min(100, limit), urllib=urllib)
        ej = await _get_json(url_esearch, tries=1); citations.append(url_esearch)
        pmids = ((ej.get("esearchresult") or {}).get("idlist") or []) if isinstance(ej, dict) else []
        # We don’t parse sequences from PubMed; provide references as evidence of oligo tractability
        refs = [{"pmid": pmid} for pmid in pmids[:limit]]
        return Evidence(status=("OK" if refs else "NO_DATA"), source="PubMed E-utilities (fallback)",
                        fetched_n=len(refs),
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "aptamer_refs": refs},
                        citations=citations, fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="Aptamer API empty/unavailable; PubMed fallback failed", fetched_n=0,
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "aptamers": []}, citations=citations, fetched_at=_now())


@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(symbol: str,
                         prior_sm: float = Query(0.55, ge=0.0, le=1.0),
                         prior_ab: float = Query(0.50, ge=0.0, le=1.0),
                         prior_oligo: float = Query(0.35, ge=0.0, le=1.0),
                         w_sm: Optional[float] = Query(None, ge=0.0, le=1.0),
                         w_ab: Optional[float] = Query(None, ge=0.0, le=1.0),
                         w_oligo: Optional[float] = Query(None, ge=0.0, le=1.0)) -> Evidence:
    """Modality recommender with defensive error handling.
    Signals: COMPARTMENTS (localization), ChEMBL (targets), PDBe (structures), UniProt (topology), IEDB (immunogenicity).
    """
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    comp_url = f"https://compartments.jensenlab.org/Service?gene_names={urllib.parse.quote(sym_norm)}&format=json"
    chembl_url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(sym_norm)}&format=json"
    pdbe_url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(sym_norm)}"

    async def _get_comp():
        try:
            js = await _get_json(comp_url, tries=1)
            return js.get(sym_norm, []) if isinstance(js, dict) else []
        except Exception:
            return []

    async def _get_chembl():
        try:
            js = await _get_json(chembl_url, tries=1)
            if isinstance(js, dict):
                return (js.get("targets") or js.get("items") or js.get("data") or [])
            return []
        except Exception:
            return []

    async def _get_pdbe():
        try:
            js = await _get_json(pdbe_url, tries=1)
            return js.get(sym_norm.lower(), []) if isinstance(js, dict) else []
        except Exception:
            return []

    async def _get_uniprot_acc():
        url = ("https://rest.uniprot.org/uniprotkb/search"
               f"?query=gene_exact:{urllib.parse.quote(sym_norm)}+AND+organism_id:9606&size=1&format=json&fields=accession,cc_subcellular_location,ft_topo_dom,ft_transmem")
        try:
            js = await _get_json(url, tries=1)
            if isinstance(js, dict):
                res = (js.get("results") or [])
                if res:
                    return res[0].get("primaryAccession") or res[0].get("accession") or ""
        except Exception:
            return ""
        return ""

    async def _get_uniprot_feats(accession: str):
        if not accession:
            return {}
        url = ("https://rest.uniprot.org/uniprotkb/search"
               f"?query=accession:{urllib.parse.quote(accession)}&format=json&fields=cc_subcellular_location,ft_topo_dom,ft_transmem,ft_binding")
        try:
            js = await _get_json(url, tries=1)
            return js if isinstance(js, dict) else {}
        except Exception:
            return {}

    compres, chemblres, pdberes, accession = await asyncio.gather(_get_comp(), _get_chembl(), _get_pdbe(), _get_uniprot_acc())
    up_feats = await _get_uniprot_feats(accession)

    # Derive features
    is_extracellular = False; is_membrane = False; in_nucleus_or_cytosol = False
    extracellular_len = 0
    try:
        # COMPARTMENTS labels
        labels = [ (r.get("name") or r.get("term") or "").lower() for r in (compres or []) ]
        is_extracellular = any("extracellular" in l or "secreted" in l for l in labels)
        in_nucleus_or_cytosol = any(("nucleus" in l) or ("cytosol" in l) for l in labels)
    except Exception:
        pass
    try:
        # UniProt transmembrane features
        feats = json.dumps(up_feats).lower()
        is_membrane = ("transmembrane" in feats) or ("cell membrane" in feats)
        # crude estimate of extracellular aa length from topological domains
        # (when available in the JSON blob; otherwise 0)
        if "topological" in feats and "extracellular" in feats:
            extracellular_len = 100  # heuristic presence marker
    except Exception:
        pass

    has_chembl = bool(chemblres)
    has_structure = bool(pdberes)
    epi_n, tc_n = await _iedb_counts(sym_norm, limit=25)
    immunogenicity_signal = 0.05 if (epi_n + tc_n) > 0 else 0.0

    def clamp(x: float) -> float: return max(0.0, min(1.0, round(float(x), 3)))

    # Scores (defensive)
    try:
        sm_score = prior_sm + (0.15 if has_chembl else 0.0) + (0.10 if has_structure else 0.0) - (0.20 if (is_extracellular and not is_membrane) else 0.0)
        ab_score = prior_ab + (0.20 if (is_membrane or is_extracellular) else 0.0) + (0.10 if extracellular_len >= 100 else 0.0) + immunogenicity_signal
        oligo_score = prior_oligo + (0.10 if in_nucleus_or_cytosol else 0.0) + (0.10 if not has_structure else 0.0) + (0.05 if has_chembl else 0.0)
    except Exception:
        # last-resort: only priors
        sm_score, ab_score, oligo_score = prior_sm, prior_ab, prior_oligo

    # Apply user overrides and clamp
    sm_score = clamp(w_sm) if (w_sm is not None) else clamp(sm_score)
    ab_score = clamp(w_ab) if (w_ab is not None) else clamp(ab_score)
    oligo_score = clamp(w_oligo) if (w_oligo is not None) else clamp(oligo_score)

    recommendation = sorted([("small_molecule", sm_score), ("antibody", ab_score), ("oligo", oligo_score)],
                            key=lambda kv: kv[1], reverse=True)

    return Evidence(status="OK", source="Modality scorer (COMPARTMENTS + ChEMBL + PDBe + UniProt + IEDB)",
                    fetched_n=len(recommendation),
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "recommendation": recommendation,
                          "rationale": {"priors": {"sm": prior_sm, "ab": prior_ab, "oligo": prior_oligo},
                                        "is_extracellular": is_extracellular, "membrane": is_membrane, "nucleus_or_cytosol": in_nucleus_or_cytosol,
                                        "chembl_targets_n": len(chemblres or []), "pdbe_structures_n": len(pdberes or []),
                                        "uniprot_accession": accession, "extracellular_aa_flag": extracellular_len,
                                        "iedb_epitopes_n": epi_n, "iedb_tcell_assays_n": tc_n,
                                        "user_overrides": {"w_sm": w_sm, "w_ab": w_ab, "w_oligo": w_oligo}},
                          "snippets": {"compartments": (compres or [])[:10], "chembl_targets": (chemblres or [])[:10], "pdbe_entries": (pdberes or [])[:10]}},
                    citations=[comp_url, chembl_url, pdbe_url], fetched_at=_now())

@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(symbol: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(sym_norm)}%7D&limit={limit}"
    tc_url = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(sym_norm)}%7D&limit={limit}"
    async def _epi():
        try: e = await _get_json(epi_url, tries=1); return e if isinstance(e, list) else []
        except Exception: return []
    async def _tc():
        try: t = await _get_json(tc_url, tries=1); return t if isinstance(t, list) else []
        except Exception: return []
    epi_list, tc_list = await asyncio.gather(_epi(), _tc())
    hla_counts: Dict[str, int] = {}
    for r in tc_list:
        allele = r.get("mhc_allele_name") or r.get("assay_mhc_allele_name") or r.get("mhc_name")
        if allele: hla_counts[allele] = hla_counts.get(allele, 0) + 1
    total = len(epi_list) + len(tc_list)
    return Evidence(status=("OK" if total > 0 else "NO_DATA"), source="IEDB IQ-API", fetched_n=total,
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "epitopes_n": len(epi_list), "tcell_assays_n": len(tc_list),
                          "hla_breakdown": sorted([[k, v] for k, v in hla_counts.items()], key=lambda kv: kv[1], reverse=True)[:25],
                          "examples": {"epitopes": epi_list[: min(10, limit)], "tcell_assays": tc_list[: min(10, limit)]}},
                    citations=[epi_url, tc_url], fetched_at=_now())

# --------------------- B6: Clinical Translation & Safety ---------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: Optional[str] = Query(None), symbol: Optional[str] = Query(None), limit: int = Query(5, ge=1, le=100)) -> Evidence:
    # Prefer a disease/condition string; if missing, fall back to gene symbol
    cond = (condition or "").strip()
    if not cond and symbol:
        cond = (await _normalize_symbol(symbol))
    if not cond:
        return Evidence(status="ERROR", source="CT.gov/WHO/EU-CTR", fetched_n=0,
                        data={"message": "Provide either condition or symbol"}, citations=[], fetched_at=_now())
    ct_url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(cond)}&pageSize={limit}"
    who_url = f"https://trialsearch.who.int/api/Trial?query={urllib.parse.quote(cond)}"
    eu_url = f"https://www.clinicaltrialsregister.eu/ctr-search/rest/search?query={urllib.parse.quote(cond)}"
    studies, cites = [], []
    try:
        js = await _get_json(ct_url, tries=1); cites.append(ct_url)
        studies = js.get("studies", []) if isinstance(js, dict) else []
    except Exception: pass
    if not studies:
        try:
            wj = await _get_json(who_url, tries=1); cites.append(who_url)
            studies = (wj if isinstance(wj, list) else wj.get("Trials", [])) or []
        except Exception: pass
    if not studies:
        try:
            eu = await _get_json(eu_url, tries=1); cites.append(eu_url)
            studies = (eu if isinstance(eu, list) else eu.get("trials") or eu.get("results") or [])
        except Exception: pass
    # If still nothing and we had both a condition and a symbol, try a second pass with the symbol text
    if not studies and symbol and cond != symbol:
        cond2 = await _normalize_symbol(symbol)
        ct2 = f"https://clinicaltrials.gov/api/v2/studies?query.term={urllib.parse.quote(cond2)}&pageSize={limit}"
        try:
            j2 = await _get_json(ct2, tries=1); cites.append(ct2)
            studies = j2.get("studies", []) if isinstance(j2, dict) else []
        except Exception: pass
    return Evidence(status=("OK" if studies else "NO_DATA"), source="CT.gov v2 + WHO ICTRP + EU CTR",
                    fetched_n=len(studies), data={"condition": cond, "studies": studies[:limit], "truncated": bool(len(studies) > limit)},
                    citations=cites, fetched_at=_now())


@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(condition: str, symbol: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_condition(condition, field_name="condition")
    faers_base = "https://api.fda.gov/drug/event.json"
    # Count-first: get frequency of MedDRA PT events for the condition
    q = f"patient.reaction.reactionmeddrapt.exact:{urllib.parse.quote(condition)}"
    count_url = f"{faers_base}?search={q}&count=patient.reaction.reactionmeddrapt.exact"
    cites: List[str] = [count_url]
    counts = []
    try:
        cj = await _get_json(count_url, tries=1)
        counts = cj.get("results", []) if isinstance(cj, dict) else []
    except Exception:
        counts = []
    total_events = sum([int(r.get("count") or 0) for r in counts]) if counts else 0
    top_counts = counts[:limit] if isinstance(counts, list) else []
    # If a symbol is provided, filter example cases by drug name as well (stricter query)
    examples: List[Any] = []
    if top_counts:
        # Fetch up to 5 example cases per top event (capped globally to ~50)
        per_term_cap = max(1, min(5, 50 // len(top_counts)))
        for r in top_counts:
            term = r.get("term")
            if not term: continue
            term_q = f"patient.reaction.reactionmeddrapt.exact:\"{urllib.parse.quote(term)}\""
            if symbol:
                sym_norm = await _normalize_symbol(symbol)
                term_q += f"+AND+patient.drug.openfda.generic_name:\"{urllib.parse.quote(sym_norm)}\""
            ex_url = f"{faers_base}?search={term_q}&limit={per_term_cap}"
            try:
                ej = await _get_json(ex_url, tries=1); cites.append(ex_url)
                for it in (ej.get("results", []) if isinstance(ej, dict) else []):
                    examples.append({"event": term, "safety_report_id": it.get("safetyreportid"), "receivedate": it.get("receivedate")})
            except Exception:
                continue
    # ClinicalTrials.gov observational trials (context)
    ct_url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&pageSize={min(100, limit)}"
    trials = []
    try:
        ct = await _get_json(ct_url, tries=1); cites.append(ct_url)
        trials = ct.get("studies", []) if isinstance(ct, dict) else []
    except Exception:
        trials = []
    data = {
        "condition": condition,
        "top_events": top_counts,
        "total_events": total_events,
        "examples": {"faers_cases": examples[:min(50, len(examples))], "observational_trials": trials[:limit]},
        "truncated": bool(counts and len(counts) > len(top_counts))
    }
    return Evidence(status=("OK" if (total_events or trials) else "NO_DATA"),
                    source="openFDA FAERS (count-first) + ClinicalTrials.gov v2",
                    fetched_n=(total_events + len(trials)),
                    data=data, citations=cites, fetched_at=_now())


@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(symbol: str, limit: int = Query(50, ge=1, le=500)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    base = "https://api.fda.gov/drug/event.json"
    q = f"patient.drug.openfda.generic_name:{urllib.parse.quote(sym_norm)}"
    count_url = f"{base}?search={q}&count=patient.reaction.reactionmeddrapt.exact"
    cites: List[str] = [count_url]
    counts = []
    try:
        cj = await _get_json(count_url, tries=1)
        counts = cj.get("results", []) if isinstance(cj, dict) else []
    except Exception:
        counts = []
    total_events = sum([int(r.get("count") or 0) for r in counts]) if counts else 0
    # Fetch a paged sample of raw reports, but cap tight to avoid huge payloads
    list_url = f"{base}?search={q}&limit={min(100, limit)}"
    reports = []
    try:
        lj = await _get_json(list_url, tries=1); cites.append(list_url)
        reports = lj.get("results", []) if isinstance(lj, dict) else []
    except Exception:
        reports = []
    data = {"symbol": symbol, "normalized_symbol": sym_norm,
            "top_events": counts[:limit], "total_events": total_events,
            "reports": reports[:limit], "truncated": bool(counts and len(counts) > min(limit, len(counts)))}
    return Evidence(status=("OK" if (total_events or reports) else "NO_DATA"), source="openFDA FAERS",
                    fetched_n=(total_events + len(reports)), data=data, citations=cites, fetched_at=_now())


@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(symbol: str, limit: int = Query(50, ge=1, le=500)) -> Evidence:
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    inx_url = f"https://drugs.ncats.io/api/v1/drugs?name={urllib.parse.quote(sym_norm)}"
    try:
        js = await _get_json(inx_url, tries=1)
        items = js.get("content", []) if isinstance(js, dict) else []
        if items:
            return Evidence(status="OK", source="Inxight Drugs", fetched_n=len(items),
                            data={"symbol": symbol, "normalized_symbol": sym_norm, "pipeline": items[:limit]},
                            citations=[inx_url], fetched_at=_now())
    except Exception: pass
    drugs = await tract_drugs(symbol, limit=limit)
    return Evidence(status=drugs.status if drugs.status in ("OK", "NO_DATA") else "NO_DATA",
                    source=drugs.source, fetched_n=drugs.fetched_n,
                    data={"symbol": symbol, "pipeline": drugs.data.get("interactions", [])[:limit]},
                    citations=drugs.citations, fetched_at=drugs.fetched_at)


@router.get("/clin/biomarker-fit", response_model=Evidence)
async def clin_biomarker_fit(symbol: str, condition: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=500)) -> Evidence:
    """Biomarker fit: HPA + UniProt + Trials; Fallbacks via GTEx (assayability proxy) and OpenTargets biomarkers lookup."""
    validate_symbol(symbol, field_name="symbol"); sym = await _normalize_symbol(symbol)
    cites: List[str] = []; payload: Dict[str, Any] = {"symbol": symbol, "normalized_symbol": sym}

    # HPA baseline/secretome
    hpa = ("https://www.proteinatlas.org/api/search_download.php"
           f"?format=json&columns=gene,secretome,subcell_location,protein_class,uniprot,main_tissue,main_tissue_rna,main_tissue_protein,rna_tissue,rna_gtex&search={urllib.parse.quote(sym)}")
    hpa_rows: List[Dict[str, Any]] = []
    try:
        hj = await _get_json(hpa, tries=1); cites.append(hpa)
        hpa_rows = hj if isinstance(hj, list) else []
    except Exception:
        pass
    payload["hpa"] = hpa_rows[:limit]

    # UniProt keywords (biomarker, secreted, extracellular)
    uni = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+organism_id:9606&format=json&fields=accession,keyword,cc_subcellular_location")
    unij = None
    try:
        unij = await _get_json(uni, tries=1); cites.append(uni)
    except Exception:
        unij = None
    payload["uniprot"] = unij if isinstance(unij, dict) else None

    # GTEx expression (assayability proxy) — best effort
    # Needs Ensembl id
    ensg, _, ensg_cites = await _ensembl_from_symbol_or_id(sym)
    cites.extend(ensg_cites)
    gtex = []
    if ensg:
        try:
            url_gtex = ("https://gtexportal.org/api/v2/expression/medianGeneExpression?datasetId=GTEx_v8&"
                        f"tissueSiteDetailId=all&gencodeId={urllib.parse.quote(ensg)}")
            gj = await _get_json(url_gtex, tries=1); cites.append(url_gtex)
            if isinstance(gj, dict):
                gtex = gj.get("data") or gj.get("medianGeneExpression") or []
        except Exception:
            gtex = []
    payload["gtex_median_tpm"] = (gtex or [])[:limit]

    # Trials signals
    trials = []
    try:
        ct = await clin_endpoints(condition or sym, limit=min(limit, 100))
        trials = (ct.data or {}).get("trials") or [] if isinstance(ct, Evidence) else []
        cites.extend(ct.citations if isinstance(ct, Evidence) else [])
    except Exception:
        trials = []
    payload["trials"] = trials[:limit]

    # OpenTargets biomarkers (lookup only, as a hint)
    ot_search = f"https://www.targetvalidation.org/search?query={urllib.parse.quote(sym)}"
    cites.append(ot_search)

    # Scoring
    detectable = False
    try:
        # If HPA reports protein class or secretome/extracellular, mark detectable
        hpa_txt = json.dumps(hpa_rows).lower()
        uniprot_txt = json.dumps(unij or {}).lower()
        detectable = any(k in hpa_txt for k in ["secretome", "plasma", "blood"]) or ("extracellular" in uniprot_txt)
        # Boost if GTEx has many tissues with non-zero TPM
        if gtex:
            nz = sum(1 for r in gtex if float(r.get("median", 0) or r.get("medianExpression", 0) or 0) > 0.5)
            detectable = detectable or (nz >= 5)
    except Exception:
        pass

    uniprot_flags = False
    try:
        txt = json.dumps(unij) if unij else ""
        uniprot_flags = ("Biomarker" in txt) or ("Secreted" in txt) or ("Extracellular" in txt)
    except Exception:
        pass

    trials_signal = len(trials) > 0
    score = (0.4 if detectable else 0.0) + (0.3 if uniprot_flags else 0.0) + (0.3 if trials_signal else 0.0)
    return Evidence(status=("OK" if (hpa_rows or unij or trials or gtex) else "NO_DATA"),
                    source="HPA + UniProt + Trials (+ GTEx/OT hint)",
                    fetched_n=(len(hpa_rows) + (1 if unij else 0) + len(trials) + (1 if gtex else 0)),
                    data={"symbol": symbol, "normalized_symbol": sym, "score": round(score, 2), **payload},
                    citations=cites, fetched_at=_now())

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(symbol: str, condition: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)) -> Evidence:
    """How crowded is the competitive/IP space?
    Primary: PatentsView. Secondary: SureChEMBL. Fallback: Drugs+Trials counts and EuropePMC signals.
    """
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    cond = condition or ""; citations: List[str] = []; results: List[Any] = []

    # PatentsView
    query = {"_and": [{"_or": [{"patent_title": {"_text_any": sym_norm}}, {"patent_abstract": {"_text_any": sym_norm}}]}]}
    if cond:
        query["_and"].append({"_or": [{"patent_title": {"_text_any": cond}}, {"patent_abstract": {"_text_any": cond}}]})
    query_str = urllib.parse.quote(json.dumps(query)); fields = urllib.parse.quote(json.dumps(["patent_id"]))
    pat_url = f"https://api.patentsview.org/patents/query?q={query_str}&f={fields}"
    try:
        js = await _get_json(pat_url, tries=1); citations.append(pat_url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        for p in patents: results.append({"patent_id": p.get("patent_id")})
    except Exception:
        pass

    # SureChEMBL (chemical patents)
    if not results:
        try:
            sc_url = f"https://www.surechembl.org/api/search?query={urllib.parse.quote(sym_norm)}"
            sj = await _get_json(sc_url, tries=1); citations.append(sc_url)
            items = (sj.get("results") or sj.get("items") or sj.get("data") or []) if isinstance(sj, dict) else []
            for it in items:
                results.append({"surechembl_id": it.get("id")})
        except Exception:
            pass

    # Fallback signals
    if not results:
        # 1) Drugs + Trials as proxy for competitive intensity
        drug_res = await tract_drugs(symbol, limit=limit)
        trial_res = await clin_endpoints(condition or symbol, limit=limit)
        count = (drug_res.fetched_n if drug_res else 0) + (trial_res.fetched_n if trial_res else 0)

        # 2) EuropePMC "activity" papers
        epmc_q = f'("{sym_norm}") AND (inhibitor OR antagonist OR agonist OR PROTAC OR degraders)'
        ep_hits, ep_cites = await _lit_search(epmc_q, limit=min(100, limit))
        citations.extend(ep_cites)
        papers_n = len(ep_hits)

        return Evidence(status=("OK" if (count > 0 or papers_n > 0) else "NO_DATA"),
                        source="Drugs+Trials+EuropePMC fallback",
                        fetched_n=(count + papers_n),
                        data={"symbol": symbol, "normalized_symbol": sym_norm, "condition": condition,
                              "drugs_n": (drug_res.fetched_n if drug_res else 0),
                              "trials_n": (trial_res.fetched_n if trial_res else 0),
                              "epmc_signals_n": papers_n},
                        citations=(drug_res.citations if drug_res else []) + (trial_res.citations if trial_res else []) + citations,
                        fetched_at=_now())

    return Evidence(status="OK", source="PatentsView + SureChEMBL", fetched_n=len(results),
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "condition": condition, "patents": results[:limit]},
                    citations=citations, fetched_at=_now())


@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(symbol: str, limit: int = Query(100, ge=1, le=1000)) -> Evidence:
    """Freedom-to-operate summary: PatentsView with assignee/CPC summaries; fallbacks via SureChEMBL and Google Patents link."""
    validate_symbol(symbol, field_name="symbol"); sym_norm = await _normalize_symbol(symbol)
    citations: List[str] = []; results: List[Dict[str, Any]] = []; patents: List[Dict[str, Any]] = []

    # PatentsView primary
    query = {"_or": [{"patent_title": {"_text_any": sym_norm}}, {"patent_abstract": {"_text_any": sym_norm}}]}
    query_str = urllib.parse.quote(json.dumps(query))
    fields = urllib.parse.quote(json.dumps(["patent_id", "assignees.assignee_organization", "cpcs.cpc_subsection_id"]))
    url = f"https://api.patentsview.org/patents/query?q={query_str}&f={fields}"
    try:
        js = await _get_json(url, tries=1); citations.append(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        for p in patents:
            results.append({"patent_id": p.get("patent_id")})
    except Exception:
        patents = []

    # SureChEMBL secondary
    if not results:
        try:
            sc_url = f"https://www.surechembl.org/api/search?query={urllib.parse.quote(sym_norm)}"
            sj = await _get_json(sc_url, tries=1); citations.append(sc_url)
            items = (sj.get("results") or sj.get("items") or sj.get("data") or []) if isinstance(sj, dict) else []
            for it in items:
                pid = it.get("id")
                if pid:
                    results.append({"surechembl_id": pid})
        except Exception:
            pass

    # Always include a Google Patents search link (manual follow-up)
    gpat_url = f"https://patents.google.com/?q={urllib.parse.quote(sym_norm)}"
    citations.append(gpat_url)

    # Summaries from PatentsView data if available
    assignee_counts: Dict[str,int] = {}
    cpc_counts: Dict[str,int] = {}
    for p in patents:
        for a in (p.get("assignees") or []):
            org = (a.get("assignee_organization") or "").strip()
            if org:
                assignee_counts[org] = assignee_counts.get(org, 0) + 1
        for c in (p.get("cpcs") or []):
            sec = c.get("cpc_subsection_id")
            if sec:
                cpc_counts[sec] = cpc_counts.get(sec, 0) + 1

    summary = {"assignees_top": sorted([[k, v] for k, v in assignee_counts.items()], key=lambda kv: kv[1], reverse=True)[:25],
               "cpc_top": sorted([[k, v] for k, v in cpc_counts.items()], key=lambda kv: kv[1], reverse=True)[:25],
               "counts": {"patentsview_n": len(patents), "total_ids_returned": len(results)}}

    return Evidence(status=("OK" if results else "NO_DATA"), source="PatentsView (+ SureChEMBL; Google Patents link)", fetched_n=len(results),
                    data={"symbol": symbol, "normalized_symbol": sym_norm, "patents": results[:limit], "summary": summary},
                    citations=citations, fetched_at=_now())

@router.get("/synth/targetcard", response_model=Evidence)
async def synth_targetcard(symbol: str, condition: Optional[str] = None,
                           include_angles: bool = Query(True), limit: int = Query(25, ge=1, le=200)) -> Evidence:
    """One call, many views: composes key modules + optional literature angles."""
    validate_symbol(symbol, field_name="symbol"); sym = await _normalize_symbol(symbol)
    tasks = [
        _safe_call(expression_baseline(symbol=sym, limit=limit)),
        _safe_call(expr_localization(symbol=sym, limit=limit)),
        _safe_call(tract_drugs(symbol=sym, limit=limit)),
        _safe_call(mech_pathways(symbol=sym, limit=min(25, limit))),
        _safe_call(mech_ppi(symbol=sym, limit=min(50, limit))),
                _safe_call(mech_structure(symbol=sym, limit=min(50, limit))),
_safe_call(tract_modality(symbol=sym)),
        _safe_call(tract_immunogenicity(symbol=sym)),
        _safe_call(genetics_rare(gene=sym, limit=min(50, limit))),
    ]
    if condition:
        tasks += [
            _safe_call(genetics_l2g(gene=sym, efo=None, disease=condition, limit=min(50, limit))),
            _safe_call(clin_endpoints(condition=condition, limit=min(10, limit))),
            _safe_call(clin_rwe(condition=condition, symbol=sym, limit=min(50, limit))),
        ]
    results = await asyncio.gather(*tasks)
    # Flatten payload
    card: Dict[str, Any] = {"symbol": sym, "condition": condition, "sections": {}}
    cites: List[str] = []
    for ev in results:
        tag = ev.source.split(" ")[0] if ev and ev.source else "module"
        card["sections"].setdefault(tag, []).append(ev.data)
        cites.extend(ev.citations or [])
    angles = []
    if include_angles:
        ang = await lit_angles(symbol=sym, condition=condition, limit=limit)
        if ang and ang.status == "OK":
            angles = ang.data.get("results", [])
            cites.extend(ang.citations or [])
            card["angles"] = {"is_gpcr": ang.data.get("is_gpcr"), "highlights": angles[:min(10, limit)]}
    return Evidence(status="OK", source="TargetCard synthesis", fetched_n=len(results) + len(angles),
                    data=card, citations=list(dict.fromkeys(cites))[:200], fetched_at=_now())

@router.get("/synth/graph", response_model=Evidence)
async def synth_graph(symbol: str, condition: Optional[str] = None, limit: int = Query(25, ge=1, le=200)) -> Evidence:
    """Return a simple cross-dataset graph (nodes/edges) for quick visualization."""
    validate_symbol(symbol, field_name="symbol"); sym = await _normalize_symbol(symbol)
    # pull a few core modules
    drugs_ev, ppi_ev, path_ev, trials_ev = await asyncio.gather(
        _safe_call(tract_drugs(symbol=sym, limit=limit)),
        _safe_call(mech_ppi(symbol=sym, limit=limit)),
        _safe_call(mech_pathways(symbol=sym, limit=limit)),
        _safe_call(clin_endpoints(condition=(condition or sym), limit=min(10, limit)))
    )
    nodes = [{"id": f"T:{sym}", "type": "target", "label": sym}]
    edges = []
    cites: List[str] = []
    # drugs
    for row in (drugs_ev.data.get("interactions") or [])[:limit]:
        did = row.get("drugId") or row.get("drug_id") or row.get("drugName")
        if not did: continue
        nid = f"D:{did}"
        nodes.append({"id": nid, "type": "drug", "label": row.get("drugName") or did})
        edges.append({"source": f"T:{sym}", "target": nid, "type": "modulates"})
    cites.extend(drugs_ev.citations or [])
    # ppi neighbors
    for n in (ppi_ev.data.get("neighbors") or [])[:limit]:
        b = n.get("preferredName_B"); a = n.get("preferredName_A")
        if not b or not a: continue
        partner = b if a.upper() == sym.upper() else a
        nid = f"G:{partner}"
        nodes.append({"id": nid, "type": "gene", "label": partner})
        edges.append({"source": f"T:{sym}", "target": nid, "type": "ppi"})
    cites.extend(ppi_ev.citations or [])
    # pathways
    for p in (path_ev.data.get("pathways") or [])[:limit]:
        pid = p.get("id") or p.get("name")
        nid = f"P:{pid}"
        nodes.append({"id": nid, "type": "pathway", "label": p.get("name")})
        edges.append({"source": f"T:{sym}", "target": nid, "type": "in_pathway"})
    cites.extend(path_ev.citations or [])
    # trials
    for tr in (trials_ev.data.get("studies") or [])[:limit]:
        tid = tr.get("NCTId") or tr.get("TrialID") or tr.get("ProtocolID") or tr.get("id") or tr.get("trial_id")
        if not tid: continue
        nid = f"CT:{tid}"
        lab = tr.get("BriefTitle") or tr.get("Scientific_title") or "trial"
        nodes.append({"id": nid, "type": "trial", "label": lab})
        edges.append({"source": nid, "target": f"T:{sym}", "type": "mentions"})
    cites.extend(trials_ev.citations or [])
    # dedupe nodes
    uniq = {}
    for n in nodes:
        uniq[n["id"]] = n
    nodes = list(uniq.values())
    return Evidence(status=("OK" if nodes else "NO_DATA"), source="Synthesis graph",
                    fetched_n=len(nodes) + len(edges),
                    data={"symbol": sym, "condition": condition, "nodes": nodes[:500], "edges": edges[:1000]},
                    citations=list(dict.fromkeys(cites))[:200], fetched_at=_now())


# =====================
# Knowledge-Graph Synthesis (bucket-level, no scoring)
# =====================
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple
import asyncio
import networkx as nx
from fastapi import Query

# ---- Shared models for synthesis ----
class KGHighlight(BaseModel):
    pattern: str
    nodes: Optional[List[str]] = None
    detail: Optional[Dict[str, Any]] = None

class BucketInsight(BaseModel):
    claim: str
    rationale: Optional[str] = None
    orthogonal_angles: Optional[List[str]] = None
    citations: Optional[List[str]] = None

class BucketContradiction(BaseModel):
    issue: str
    hypotheses: Optional[List[str]] = None
    tests: Optional[List[str]] = None

class BucketSynthesis(BaseModel):
    bucket: str
    graph: Dict[str, Any]
    insights: List[BucketInsight] = Field(default_factory=list)
    contradictions: List[BucketContradiction] = Field(default_factory=list)
    gaps: List[Dict[str, Any]] = Field(default_factory=list)
    scores: Optional[Dict[str, Any]] = None
    components: Optional[Dict[str, float]] = None
    fetched_at: str

# ---- Utility helpers ----
def _kg():
    return nx.MultiDiGraph()

def _kg_add(g, ntype: str, nid: str, **attrs):
    key = f"{ntype}:{nid}"
    if not g.has_node(key):
        g.add_node(key, type=ntype, id=nid, **attrs)
    else:
        # merge attrs (non-destructive)
        g.nodes[key].update({k: v for k, v in attrs.items() if v is not None})
    return key

def _kg_edge(g, a, b, kind: str, **attrs):
    g.add_edge(a, b, kind=kind, **attrs)

def _kg_summary(g):
    return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges()}

def _flatten_url_list(*chunks):
    out = []
    for ch in chunks:
        if not ch:
            continue
        if isinstance(ch, list):
            out.extend([c for c in ch if isinstance(c, str)])
        elif isinstance(ch, str):
            out.append(ch)
    # de-dup
    return list(dict.fromkeys(out))

def _claim(text: str, rationale: str = None, angles: List[str] = None, cites: List[str] = None) -> BucketInsight:
    return BucketInsight(claim=text, rationale=rationale, orthogonal_angles=angles or [], citations=cites or [])

def _contradiction(issue: str, hypotheses: List[str] = None, tests: List[str] = None) -> BucketContradiction:
    return BucketContradiction(issue=issue, hypotheses=hypotheses or [], tests=tests or [])

# ---- Synthesis builders (per consolidated bucket) ----

async def _synth_genetics(gene: str, condition: Optional[str] = None, tissue: Optional[str] = None):
    """
    Uses existing genetics endpoints in this router + new live endpoints (coloc, functional, cross)
    to build a causal evidence graph and emit insights (no scoring).
    """
    g = _kg()
    citations = []

    # Always create the focal gene node
    n_gene = _kg_add(g, "Gene", gene)

    # Fan-out calls (silently ignore failures)
    tasks = []
    # Existing endpoints in this file
    try:
        tasks.append(genetics_sqtl(gene=gene))  # sQTL/eQTL-like
    except Exception:
        pass
    try:
        tasks.append(genetics_mr(gene=gene, efo=condition))
    except Exception:
        pass
    try:
        tasks.append(genetics_mendelian(gene=gene))
    except Exception:
        pass
    try:
        tasks.append(genetics_rare(gene=gene))
    except Exception:
        pass
    try:
        tasks.append(genetics_l2g(gene=gene, efo=condition, disease=condition))
    except Exception:
        pass
    try:
        tasks.append(genetics_epigenetics(gene=gene))
    except Exception:
        pass
    # New ones we added (may or may not exist if running against the base without append)
    try:
        tasks.append(genetics_coloc(gene=gene, trait_filter=condition))
    except Exception:
        pass
    try:
        tasks.append(genetics_functional(gene=gene))
    except Exception:
        pass
    try:
        tasks.append(genetics_cross_species(gene=gene))
    except Exception:
        pass

    results = []
    for coro in tasks:
        try:
            res = await coro
            results.append(res)
            citations += (res.citations or [])
        except Exception:
            continue

    # Heuristic graph projection from Evidence.data of each module
    for res in results:
        src = (res.source or "").lower()
        data = res.data or {}
        # Generic add of any credible set / coloc information
        if "coloc" in src or "colocalis" in src or "colocalisations" in data:
            items = data.get("colocalisations") or []
            for it in items:
                lv = ((it.get("left") or {}).get("lead_variant"))
                rv = ((it.get("right") or {}).get("lead_variant"))
                ltrait = ((it.get("left") or {}).get("trait"))
                rtype = ((it.get("right") or {}).get("study_type"))
                h4 = ((it.get("metrics") or {}).get("h4"))
                if lv:
                    n_lv = _kg_add(g, "Variant", lv)
                    _kg_edge(g, n_lv, n_gene, "cis-QTL-coloc", trait=ltrait, h4=h4, study_type=rtype)
        # sQTL / eQTL
        if res.status == "OK" and ("sqtl" in src or "eqtl" in src or "genetics_sqtl" in (res.title or "").lower() if hasattr(res, "title") else False):
            # best effort: look for list under "rows" or "results"
            hits = data.get("rows") or data.get("results") or []
            for h in hits:
                var = h.get("variant") or h.get("snp") or h.get("rsid")
                tis = h.get("tissue") or tissue
                direction = h.get("direction") or h.get("beta_dir") or None
                if var:
                    n_var = _kg_add(g, "Variant", var)
                    _kg_edge(g, n_var, n_gene, "cis-QTL", tissue=tis, direction=direction)
        # MR
        if "mr" in src or ("mr" in (res.title or "").lower() if hasattr(res, "title") else False):
            assoc = data.get("associations") or data.get("results") or []
            for a in assoc:
                beta = a.get("beta") or a.get("estimate")
                efo = a.get("efo") or condition
                _kg_edge(g, n_gene, _kg_add(g, "Trait", efo or "condition"), "MR", beta=beta)
        # Rare / Mendelian
        if "mendelian" in src or "orphanet" in src or "clinvar" in src or "rare" in src:
            conds = data.get("conditions") or data.get("diseases") or []
            for c in conds:
                _kg_edge(g, n_gene, _kg_add(g, "Trait", str(c)), "Mendelian/Rare")
        # Functional noncoding (MPRA/CRISPR-QTL)
        if "encode" in src or "mpra" in src or "functional" in src:
            exps = data.get("experiments") or []
            for e in exps[:5]:
                acc = e.get("accession") or e.get("href")
                if acc:
                    _kg_edge(g, _kg_add(g, "Assay", acc), n_gene, "MPRA/Reporter")
        # Cross-species IMPC
        if "impc" in src:
            phenos = data.get("phenotypes") or []
            for p in phenos[:10]:
                mp = p.get("mp_term_id") or p.get("mp_term_name")
                if mp:
                    _kg_edge(g, n_gene, _kg_add(g, "Phenotype", str(mp)), "MouseKO")

    # --- Motif detection (lightweight, signature-based) ---
    highlights: List[KGHighlight] = []
    insights: List[BucketInsight] = []
    contradictions: List[BucketContradiction] = []
    gaps: List[Dict[str, Any]] = []

    # Causal chain motif: cis-QTL + MR edge to condition
    has_qtl = any(d.get("kind") in ("cis-QTL", "cis-QTL-coloc") for _,_,d in g.edges(data=True))
    has_mr = any(d.get("kind") == "MR" for _,_,d in g.edges(data=True))
    if has_qtl and has_mr:
        insights.append(_claim(
            f"Causal chain present: variants affecting {gene} expression and MR linking {gene} to the condition.",
            rationale="cis-QTL + MR",
            angles=["direction-of-effect", "tissue relevance"],
            cites=_flatten_url_list(citations)
        ))
        highlights.append(KGHighlight(pattern="causal_chain", nodes=[f"Gene:{gene}"]))

    # Protein-level confirmation: any MPRA + (implied) QTL
    has_mpra = any(d.get("kind") == "MPRA/Reporter" for _,_,d in g.edges(data=True))
    if has_qtl and has_mpra:
        insights.append(_claim(
            "Regulatory variant evidence converges (cis-QTL with functional reporter assays).",
            rationale="MPRA/STARR-seq near credible variants",
            angles=["regulatory mechanism"],
            cites=_flatten_url_list(citations)
        ))
        highlights.append(KGHighlight(pattern="regulatory_validation"))

    # Cross-species echo
    has_impc = any(d.get("kind") == "MouseKO" for _,_,d in g.edges(data=True))
    if has_impc:
        insights.append(_claim(
            "Mouse knockout phenotypes overlap disease-relevant axes.",
            rationale="IMPC genotype–phenotype associations",
            angles=["evolutionary conservation"],
            cites=_flatten_url_list(citations)
        ))

    if not insights:
        gaps.append({"missing": "No convergent motifs detected", "next_action": "Fetch more QTL/coloc/MR or disease-tissue functional assays"})

    return BucketSynthesis(
        bucket="genetics",
        graph={**_kg_summary(g), "highlights": [h.dict() for h in highlights]},
        insights=insights,
        contradictions=contradictions,
        gaps=gaps,
        fetched_at=_now()
    )


async def _synth_association(gene: str, condition: Optional[str] = None):
    g = _kg(); n_gene = _kg_add(g, "Gene", gene)
    citations = []
    res = []
    # Use assoc endpoints as observational signal sources
    for fn in (assoc_bulk_rna, assoc_bulk_prot, assoc_sc, assoc_geo_arrayexpress, assoc_depmap_achilles, assoc_tabula_hca):
        try:
            r = await fn(gene=gene)
            res.append(r); citations += (r.citations or [])
        except Exception:
            continue
    # Build a simple signal breadth
    for r in res:
        src = (r.source or "").lower(); data = r.data or {}
        # generic: create Dataset node
        ds = data.get("dataset") or r.source
        n_ds = _kg_add(g, "Dataset", ds)
        _kg_edge(g, n_gene, n_ds, "Association", source=r.source)
    insights = []
    if res:
        insights.append(_claim("Multiple observational signals across cohorts/datasets.", rationale="bulk/scRNA/proteomics/perturbation", cites=_flatten_url_list(citations)))
    else:
        insights.append(_claim("No observational associations available from configured sources.", cites=_flatten_url_list(citations)))
    return BucketSynthesis(bucket="association", graph=_kg_summary(g), insights=insights, contradictions=[], gaps=[], fetched_at=_now())


async def _synth_biology(gene: str, tissue: Optional[str] = None):
    g = _kg(); n_gene = _kg_add(g, "Gene", gene)
    citations = []
    res = []
    for fn in (expression_baseline, expr_inducibility, expr_localization, mech_pathways, mech_ppi, mech_structure):
        try:
            # pathways/ppi likely require symbol param names; we pass what we have
            r = await fn(symbol=gene) if "mech_" in fn.__name__ else await fn(gene=gene)
            res.append(r); citations += (r.citations or [])
        except Exception:
            continue
    for r in res:
        src = (r.source or "").lower(); data = r.data or {}
        if "pathway" in src:
            for p in data.get("pathways", [])[:10]:
                n_pw = _kg_add(g, "Pathway", str(p))
                _kg_edge(g, n_gene, n_pw, "PathwayMember")
        if "ppi" in src or "string" in src:
            for p in data.get("partners", [])[:20]:
                n_p = _kg_add(g, "Gene", str(p))
                _kg_edge(g, n_gene, n_p, "PPI")
        if "localization" in src:
            loc = data.get("localization") or data.get("locations") or []
            for L in (loc if isinstance(loc, list) else [loc]):
                _kg_edge(g, n_gene, _kg_add(g, "Localization", str(L)), "Localization")
        if "induc" in src or "baseline" in src:
            ctxs = data.get("contexts") or []
            for c in ctxs[:10]:
                _kg_edge(g, n_gene, _kg_add(g, "Context", str(c)), "ExpressionContext")
    insights = []
    if any(k for _,_,k in g.edges(data=True) if k.get("kind") in ("PPI","PathwayMember")):
        insights.append(_claim("Mechanistic context is populated (pathways and PPIs present).", angles=["complex membership","pathway convergence"], cites=_flatten_url_list(citations)))
    if not insights:
        insights.append(_claim("Biology context underpopulated; add spatial/proteogenomic sources."))
    return BucketSynthesis(bucket="biology", graph=_kg_summary(g), insights=insights, contradictions=[], gaps=[], fetched_at=_now())


async def _synth_tractability(gene: str, tissue: Optional[str] = None):
    g = _kg(); n_gene = _kg_add(g, "Gene", gene); citations = []
    res = []
    for fn in (tract_drugs, tract_ligandability_sm, tract_ligandability_ab, tract_ligandability_oligo, tract_modality, tract_immunogenicity):
        try:
            r = await fn(symbol=gene) if "tract_" in fn.__name__ else await fn(gene=gene)
            res.append(r); citations += (r.citations or [])
        except Exception:
            continue
    for r in res:
        data = r.data or {}
        if "compounds" in data:
            for c in data["compounds"][:20]:
                _kg_edge(g, _kg_add(g, "Compound", str(c)), n_gene, "Binder")
        if "pockets" in data:
            for pk in data["pockets"][:10]:
                _kg_edge(g, n_gene, _kg_add(g, "Pocket", str(pk)), "StructurePocket")
        if "modality" in data:
            _kg_edge(g, n_gene, _kg_add(g, "Modality", str(data["modality"])), "ModalityFit")
    insights = []
    if any(d.get("kind") == "Binder" for _,_,d in g.edges(data=True)):
        insights.append(_claim("Known binders/chemotypes present; tractability supported.", angles=["SM/Ab/Oligo options"], cites=_flatten_url_list(citations)))
    else:
        insights.append(_claim("No known binders found; consider structure-based or modality alternatives."))
    return BucketSynthesis(bucket="tractability", graph=_kg_summary(g), insights=insights, contradictions=[], gaps=[], fetched_at=_now())


async def _synth_clinical(gene: str, condition: Optional[str] = None):
    g = _kg(); n_gene = _kg_add(g, "Gene", gene); citations = []
    res = []
    for fn in (clin_endpoints, clin_pipeline, clin_rwe, clin_biomarker_fit):
        try:
            r = await fn(symbol=gene, condition=condition) if "endpoint" in fn.__name__ else await fn(gene=gene)
            res.append(r); citations += (r.citations or [])
        except Exception:
            continue
    for r in res:
        data = r.data or {}
        if "endpoints" in data:
            for ep in data["endpoints"][:10]:
                _kg_edge(g, n_gene, _kg_add(g, "Endpoint", str(ep)), "Endpoint")
        if "biomarkers" in data:
            for bm in data["biomarkers"][:10]:
                _kg_edge(g, n_gene, _kg_add(g, "Biomarker", str(bm)), "Biomarker")
        if "trials" in data:
            _kg_edge(g, n_gene, _kg_add(g, "Trials", "present"), "Trials")
    insights = []
    if any(d.get("kind") == "Endpoint" for _,_,d in g.edges(data=True)):
        insights.append(_claim("Clinical endpoints/biomarkers identified; translational hooks exist.", cites=_flatten_url_list(citations)))
    else:
        insights.append(_claim("No mature endpoints found; consider surrogate development."))
    return BucketSynthesis(bucket="clinical_landscape", graph=_kg_summary(g), insights=insights, contradictions=[], gaps=[], fetched_at=_now())


async def _synth_readiness(gene: str):
    g = _kg(); n_gene = _kg_add(g, "Gene", gene); citations=[]
    res = []
    for fn in (clin_safety,):
        try:
            r = await fn(gene=gene); res.append(r); citations += (r.citations or [])
        except Exception:
            continue
    for r in res:
        data = r.data or {}
        if "safety_signals" in data:
            for s in data["safety_signals"][:10]:
                _kg_edge(g, n_gene, _kg_add(g, "SafetySignal", str(s)), "Safety")
    insights = []
    if any(d.get("kind") == "Safety" for _,_,d in g.edges(data=True)):
        insights.append(_claim("Safety liabilities present; design around critical tissues/PD windows.", angles=["local delivery","conditional activation"], cites=_flatten_url_list(citations)))
    else:
        insights.append(_claim("No major safety flags identified in configured sources."))
    return BucketSynthesis(bucket="development_readiness", graph=_kg_summary(g), insights=insights, contradictions=[], gaps=[], fetched_at=_now())


# ---- Public synthesis endpoints ----

@router.get("/synth/bucket", response_model=BucketSynthesis)
async def synth_bucket(
    name: str = Query(..., description="One of: genetics, association, biology, tractability, clinical, readiness"),
    gene: str = Query(...),
    condition: Optional[str] = Query(None),
    tissue: Optional[str] = Query(None)
) -> BucketSynthesis:
    name = name.lower().strip()
    if name == "genetics":
        return await _synth_genetics(gene=gene, condition=condition, tissue=tissue)
    if name == "association":
        return await _synth_association(gene=gene, condition=condition)
    if name == "biology":
        return await _synth_biology(gene=gene, tissue=tissue)
    if name == "tractability":
        return await _synth_tractability(gene=gene, tissue=tissue)
    if name in ("clinical", "clinical_landscape"):
        return await _synth_clinical(gene=gene, condition=condition)
    if name in ("readiness", "development_readiness"):
        return await _synth_readiness(gene=gene)
    raise HTTPException(status_code=400, detail=f"Unknown bucket '{name}'")


# ------------------------ NEW: Colocalisation (Open Targets v4) ---------------

@router.get("/genetics/coloc", response_model=Evidence)
async def genetics_coloc(
    gene: str,
    trait_filter: Optional[str] = Query(None, description="Filter by left/right trait substring"),
    study_types: Optional[str] = Query("eqtl,pqtl,sqtl,tuqtl", description="Comma-separated StudyTypeEnum list"),
    limit: int = Query(200, ge=1, le=500)
) -> Evidence:
    validate_symbol(gene, field_name="gene")
    ensg, sym_norm, citations = await _ensembl_from_symbol_or_id(gene)
    if not ensg:
        return Evidence(status="ERROR", source="Ensembl resolver empty", fetched_n=0,
                        data={"gene": gene}, citations=citations, fetched_at=_now())

    stypes = [s.strip() for s in (study_types or "").split(",") if s.strip()]
    gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
        query Coloc($ensg: String!, $studyTypes: [StudyTypeEnum!]) {
          target(ensemblId: $ensg) {
            credibleSets(page: {index: 0, size: 400}) {
              rows {
                study { id traitFromSource studyType }
                leadVariant { id }
                studyLocusId
                colocalisation(studyTypes: $studyTypes, page: {index: 0, size: 400}) {
                  rows {
                    h4 h3 clpp rightStudyType colocalisationMethod
                    otherStudyLocus {
                      study { id studyType traitFromSource }
                      leadVariant { id }
                      qtlGeneId
                    }
                  }
                }
              }
            }
          }
        }""",
        "variables": {"ensg": ensg, "studyTypes": stypes or None}
    }
    try:
        body = await _post_json(gql_url, query, tries=1); citations.append(gql_url)
    except Exception as e:
        return Evidence(status="ERROR", source=f"OpenTargets GraphQL error: {e}", fetched_n=0,
                        data={"gene": gene}, citations=citations, fetched_at=_now())

    rows = (((body or {}).get("data") or {}).get("target") or {}).get("credibleSets", {}) or {}
    rows = rows.get("rows") or []
    out = []
    ensg_root = ensg.split(".")[0]
    tf = (trait_filter or "").lower().strip() if trait_filter else None

    for r in rows:
        lstudy = (r.get("study") or {})
        if (lstudy.get("studyType") or "").lower() != "gwas":
            continue
        ltrait = (lstudy.get("traitFromSource") or "")
        for c in (((r.get("colocalisation") or {}).get("rows")) or []):
            other = (c.get("otherStudyLocus") or {})
            qtl_gene = (other.get("qtlGeneId") or "").split(".")[0]
            if qtl_gene != ensg_root:
                continue  # enforce cis-QTL for the requested gene
            rstudy = (other.get("study") or {})
            rtrait = (rstudy.get("traitFromSource") or "")
            if tf and (tf not in ltrait.lower()) and (tf not in rtrait.lower()):
                continue
            out.append({
                "left": {
                    "study_id": lstudy.get("id"),
                    "trait": ltrait,
                    "lead_variant": (r.get("leadVariant") or {}).get("id")
                },
                "right": {
                    "study_id": rstudy.get("id"),
                    "study_type": rstudy.get("studyType"),
                    "trait": rtrait,
                    "lead_variant": (other.get("leadVariant") or {}).get("id")
                },
                "metrics": {
                    "h4": c.get("h4"),
                    "h3": c.get("h3"),
                    "clpp": c.get("clpp"),
                    "method": c.get("colocalisationMethod")
                }
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    status = "OK" if out else "NO_DATA"
    return Evidence(status=status, source="OpenTargets Platform GraphQL (credibleSet.colocalisation)",
                    fetched_n=len(out),
                    data={"gene": gene, "ensembl_id": ensg, "colocalisations": out[:limit],
                          "study_types": (stypes or ["eqtl", "pqtl", "sqtl", "tuqtl"]), "trait_filter": trait_filter},
                    citations=citations, fetched_at=_now())


# --------- Functional noncoding assays (ENCODE MPRA / STARR-seq) --------------

@router.get("/genetics/functional", response_model=Evidence)
async def genetics_functional(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    validate_symbol(gene, field_name="gene")
    from urllib.parse import quote
    queries = [
        f"https://www.encodeproject.org/search/?type=FunctionalCharacterizationExperiment&assay_title=MPRA&target.label={quote(gene)}&status=released&format=json",
        f"https://www.encodeproject.org/search/?type=FunctionalCharacterizationExperiment&assay_title=MPRA&searchTerm={quote(gene)}&status=released&format=json",
        f"https://www.encodeproject.org/search/?type=FunctionalCharacterizationExperiment&assay_title=STARR-seq&searchTerm={quote(gene)}&status=released&format=json",
    ]
    items, cites = [], []
    for url in queries:
        try:
            js = await _get_json(url, tries=1); cites.append(url)
            hits = js.get("@graph") or js.get("graph") or []
            if isinstance(hits, list):
                for r in hits:
                    items.append({
                        "accession": r.get("accession"),
                        "assay_title": r.get("assay_title"),
                        "lab": (r.get("lab") or {}).get("title"),
                        "biosample": (r.get("biosample_ontology") or {}).get("term_name"),
                        "target": (r.get("target") or {}).get("label"),
                        "href": r.get("@id") or r.get("url"),
                    })
        except Exception:
            continue
    # de-duplicate by accession
    seen, uniq = set(), []
    for it in items:
        acc = it.get("accession") or it.get("href")
        if acc and acc not in seen:
            seen.add(acc); uniq.append(it)
    return Evidence(status=("OK" if uniq else "NO_DATA"), source="ENCODE search (MPRA/STARR-seq)",
                    fetched_n=len(uniq),
                    data={"gene": gene, "experiments": uniq[:limit]}, citations=cites, fetched_at=_now())


# --------- Cross-species knockouts (Ensembl orthology + IMPC Solr) -------------

@router.get("/genetics/cross", response_model=Evidence)
async def genetics_cross_species(gene: str, limit: int = Query(100, ge=1, le=500)) -> Evidence:
    validate_symbol(gene, field_name="gene")
    from urllib.parse import quote
    ensg, sym_norm, cites = await _ensembl_from_symbol_or_id(gene)
    if not ensg:
        return Evidence(status="ERROR", source="Ensembl resolver empty", fetched_n=0,
                        data={"gene": gene}, citations=cites, fetched_at=_now())

    # Orthologue in mouse
    homo_url = f"https://rest.ensembl.org/homology/id/homo_sapiens/{quote(ensg)}?target_species=mus_musculus;type=orthologues;content-type=application/json"
    try:
        hj = await _get_json(homo_url, tries=1); cites.append(homo_url)
        mouse_symbol = None
        d = hj.get("data", []) if isinstance(hj, dict) else []
        if d:
            for h in (d[0].get("homologies") or []):
                if (h.get("target") or {}).get("species") == "mus_musculus":
                    mouse_symbol = (h.get("target") or {}).get("display_id") or (h.get("target") or {}).get("gene_symbol")
                    break
    except Exception:
        mouse_symbol = None

    if not mouse_symbol:
        # Try symbol-based
        homo2_url = f"https://rest.ensembl.org/homology/symbol/homo_sapiens/{quote(sym_norm)}?target_species=mus_musculus;type=orthologues;content-type=application/json"
        try:
            hj2 = await _get_json(homo2_url, tries=1); cites.append(homo2_url)
            d2 = hj2.get("data", []) if isinstance(hj2, dict) else []
            if d2:
                for h in (d2[0].get("homologies") or []):
                    if (h.get("target") or {}).get("species") == "mus_musculus":
                        mouse_symbol = (h.get("target") or {}).get("display_id") or (h.get("target") or {}).get("gene_symbol")
                        break
        except Exception:
            pass

    if not mouse_symbol:
        return Evidence(status="NO_DATA", source="Ensembl homology (mouse) empty", fetched_n=0,
                        data={"gene": gene, "ensembl_id": ensg, "mouse_symbol": None, "phenotypes": []},
                        citations=cites, fetched_at=_now())

    # IMPC Solr genotype-phenotype
    impc_url = f"https://www.ebi.ac.uk/mi/impc/solr/genotype-phenotype/select?q=marker_symbol:{quote(mouse_symbol)}&rows={min(500, limit)}&wt=json"
    try:
        impc = await _get_json(impc_url, tries=1); cites.append(impc_url)
        docs = (((impc.get('response') or {}).get('docs')) or []) if isinstance(impc, dict) else []
        simple = [{
            "marker_symbol": d.get("marker_symbol"),
            "mp_term_id": d.get("mp_term_id"),
            "mp_term_name": d.get("mp_term_name"),
            "zygosity": d.get("zygosity"),
            "sex": d.get("sex"),
            "p_value": d.get("p_value"),
            "procedure": d.get("procedure_name"),
            "phenotyping_center": d.get("phenotyping_center"),
        } for d in docs][:limit]
        return Evidence(status=("OK" if simple else "NO_DATA"),
                        source="Ensembl Homology + IMPC Solr (genotype-phenotype)",
                        fetched_n=len(simple),
                        data={"gene": gene, "ensembl_id": ensg, "mouse_symbol": mouse_symbol,
                              "phenotypes": simple},
                        citations=cites, fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=f"IMPC Solr error: {e}", fetched_n=0,
                        data={"gene": gene, "ensembl_id": ensg, "mouse_symbol": mouse_symbol},
                        citations=cites, fetched_at=_now())


# ========================== Targetval Registry & Synthesis (2025-10) ==========================
# Non-breaking additions: registry endpoints + synthesis plan stub, keeping layout intact.
# This implements Alp's 38 modules / 55 sources spec with NEW markers and TTL policy.

from typing import Literal, TypedDict, Optional
from fastapi import Body

# ---- Registry (constants; replace with file-backed registries later if desired) ----

MODULES: list[dict] = [
    # TARGET_CHARACTERIZATION (5)
    {"id":"expr_baseline","bucket":"TARGET_CHARACTERIZATION","new":False,
     "sources":["hpa","expression_atlas","cellxgene","mygene","compartments"]},
    {"id":"expr_localization","bucket":"TARGET_CHARACTERIZATION","new":False,
     "sources":["compartments","hpa","uniprot"]},
    {"id":"mech_structure","bucket":"TARGET_CHARACTERIZATION","new":False,
     "sources":["alphafold","pdbe","uniprot"]},
    {"id":"mech_ppi","bucket":"TARGET_CHARACTERIZATION","new":False,
     "sources":["string","omnipath"]},
    {"id":"mech_pathways","bucket":"TARGET_CHARACTERIZATION","new":False,
     "sources":["reactome","kegg","wikipathways","omnipath"]},
    # GENETIC_CAUSALITY (8)
    {"id":"genetics_l2g","bucket":"GENETIC_CAUSALITY","new":False,
     "sources":["opentargets","gwas_catalog"]},
    {"id":"genetics_coloc","bucket":"GENETIC_CAUSALITY","new":True,
     "sources":["gtex","eqtl_catalogue","gwas_catalog"]},
    {"id":"genetics_mr","bucket":"GENETIC_CAUSALITY","new":False,
     "sources":["mrbase_open_gwas","gwas_catalog","gtex"]},
    {"id":"genetics_sqtl","bucket":"GENETIC_CAUSALITY","new":False,
     "sources":["gtex","eqtl_catalogue"]},
    {"id":"genetics_rare","bucket":"GENETIC_CAUSALITY","new":False,
     "sources":["gnomad","clinvar"]},
    {"id":"genetics_mendelian","bucket":"GENETIC_CAUSALITY","new":False,
     "sources":["clinvar","clingen","orphanet"]},
    {"id":"genetics_functional","bucket":"GENETIC_CAUSALITY","new":True,
     "sources":["encode","depmap"]},
    {"id":"genetics_cross","bucket":"GENETIC_CAUSALITY","new":True,
     "sources":["gwas_catalog","mrbase_open_gwas"]},
    # MULTI_OMICS_ASSOCIATION (7)
    {"id":"assoc_bulk_rna","bucket":"MULTI_OMICS_ASSOCIATION","new":False,
     "sources":["expression_atlas","geo","arrayexpress"]},
    {"id":"assoc_sc","bucket":"MULTI_OMICS_ASSOCIATION","new":False,
     "sources":["cellxgene","scea"]},
    {"id":"spatial_expression","bucket":"MULTI_OMICS_ASSOCIATION","new":True,
     "sources":["stomicsdb","htan","cellxgene"]},
    {"id":"assoc_bulk_prot","bucket":"MULTI_OMICS_ASSOCIATION","new":False,
     "sources":["proteomicsdb","pride"]},
    {"id":"omics_phosphoproteomics","bucket":"MULTI_OMICS_ASSOCIATION","new":True,
     "sources":["phosphositeplus","pride"]},
    {"id":"omics_metabolites","bucket":"MULTI_OMICS_ASSOCIATION","new":True,
     "sources":["metabolights","hmdb"]},
    {"id":"assoc_perturb","bucket":"MULTI_OMICS_ASSOCIATION","new":False,
     "sources":["lincs","depmap"]},
    # MECHANISM_BIOLOGY (5)
    {"id":"expr_inducibility","bucket":"MECHANISM_BIOLOGY","new":False,
     "sources":["expression_atlas","hpa","encode"]},
    {"id":"mech_ligrec","bucket":"MECHANISM_BIOLOGY","new":False,
     "sources":["omnipath","reactome"]},
    {"id":"genetics_lncrna","bucket":"MECHANISM_BIOLOGY","new":False,
     "sources":["rnacentral"]},
    {"id":"genetics_mirna","bucket":"MECHANISM_BIOLOGY","new":False,
     "sources":["mirnet","encori"]},
    {"id":"spatial_neighborhoods","bucket":"MECHANISM_BIOLOGY","new":True,
     "sources":["stomicsdb","htan"]},
    # TRACTABILITY_MODALITY (6)
    {"id":"tract_drugs","bucket":"TRACTABILITY_MODALITY","new":False,
     "sources":["chembl","dgidb","inxight"]},
    {"id":"tract_ligandability_sm","bucket":"TRACTABILITY_MODALITY","new":False,
     "sources":["pdbe","alphafold","chembl"]},
    {"id":"tract_ligandability_ab","bucket":"TRACTABILITY_MODALITY","new":False,
     "sources":["sabdab","thera_sabdab","hpa"]},
    {"id":"tract_ligandability_oligo","bucket":"TRACTABILITY_MODALITY","new":False,
     "sources":["ribocentre","iedb"]},
    {"id":"tract_modality","bucket":"TRACTABILITY_MODALITY","new":False,
     "sources":["uniprot","hpa","iedb"]},
    {"id":"tract_immunogenicity","bucket":"TRACTABILITY_MODALITY","new":False,
     "sources":["iedb"]},
    # CLINICAL_READINESS (5)
    {"id":"clin_endpoints","bucket":"CLINICAL_READINESS","new":False,
     "sources":["clinicaltrials","who_ictrp","eu_ctr"]},
    {"id":"clin_biomarker_fit","bucket":"CLINICAL_READINESS","new":False,
     "sources":["hpa","expression_atlas","cellxgene"]},
    {"id":"clin_safety","bucket":"CLINICAL_READINESS","new":False,
     "sources":["faers","gnomad","clinvar"]},
    {"id":"clin_rwe","bucket":"CLINICAL_READINESS","new":False,
     "sources":["faers"]},
    {"id":"clin_pipeline","bucket":"CLINICAL_READINESS","new":False,
     "sources":["clinicaltrials","inxight"]},
    # COMPETITIVE_LANDSCAPE (2)
    {"id":"comp_intensity","bucket":"COMPETITIVE_LANDSCAPE","new":False,
     "sources":["clinicaltrials","chembl","inxight"]},
    {"id":"comp_freedom","bucket":"COMPETITIVE_LANDSCAPE","new":False,
     "sources":["patentsview","surechembl"], "optional_sources":["google_patents"]},
]
assert len(MODULES) == 38

SOURCES: list[dict] = [
    # Core Biological Databases (10)
    {"id":"uniprot","name":"UniProt","category":"core","ttl":"monthly","tags":["protein"]},
    {"id":"pdbe","name":"PDBe","category":"core","ttl":"quarterly","tags":["structure"]},
    {"id":"alphafold","name":"AlphaFold","category":"core","ttl":"quarterly","tags":["structure","prediction"]},
    {"id":"string","name":"STRING","category":"core","ttl":"quarterly","tags":["ppi"]},
    {"id":"reactome","name":"Reactome","category":"core","ttl":"quarterly","tags":["pathway"]},
    {"id":"kegg","name":"KEGG","category":"core","ttl":"quarterly","tags":["pathway"]},
    {"id":"wikipathways","name":"WikiPathways","category":"core","ttl":"quarterly","tags":["pathway"]},
    {"id":"omnipath","name":"OmniPath","category":"core","ttl":"quarterly","tags":["ligrec","pathway"]},
    {"id":"compartments","name":"COMPARTMENTS","category":"core","ttl":"quarterly","tags":["localization"]},
    {"id":"mygene","name":"MyGene.info","category":"core","ttl":"monthly","tags":["gene","annotation"]},
    # Genetics & Genomics (10)
    {"id":"gwas_catalog","name":"GWAS Catalog","category":"genetics","ttl":"monthly","tags":["gwas"]},
    {"id":"opentargets","name":"OpenTargets Platform","category":"genetics","ttl":"monthly","tags":["l2g","association"]},
    {"id":"gtex","name":"GTEx","category":"genetics","ttl":"monthly","tags":["eqtl","sqtl","expression"]},
    {"id":"eqtl_catalogue","name":"eQTL Catalogue","category":"genetics","ttl":"monthly","tags":["eqtl"],"new":True},
    {"id":"clinvar","name":"ClinVar","category":"genetics","ttl":"monthly","tags":["clinical_variation"]},
    {"id":"gnomad","name":"gnomAD","category":"genetics","ttl":"monthly","tags":["constraint","variants"]},
    {"id":"encode","name":"ENCODE","category":"genetics","ttl":"monthly","tags":["functional"]},
    {"id":"impc","name":"IMPC","category":"genetics","ttl":"monthly","tags":["mouse","phenotype"],"new":True},
    {"id":"mrbase_open_gwas","name":"MR-Base/OpenGWAS","category":"genetics","ttl":"monthly","tags":["mr","instruments"],"new":True},
    {"id":"clingen","name":"ClinGen","category":"genetics","ttl":"monthly","tags":["curation"]},
    # Omics & Expression (12) — Tabula Sapiens alias -> cellxgene
    {"id":"hpa","name":"HPA","category":"omics","ttl":"monthly","tags":["protein_expression"]},
    {"id":"expression_atlas","name":"Expression Atlas","category":"omics","ttl":"monthly","tags":["bulk_rna"]},
    {"id":"geo","name":"GEO","category":"omics","ttl":"monthly","tags":["bulk_rna"]},
    {"id":"arrayexpress","name":"ArrayExpress (BioStudies)","category":"omics","ttl":"monthly","tags":["bulk_rna"]},
    {"id":"cellxgene","name":"cellxgene","category":"omics","ttl":"monthly","tags":["scRNA","atlas"],"aliases":["tabula_sapiens"]},
    {"id":"scea","name":"SCEA","category":"omics","ttl":"monthly","tags":["single_cell"]},
    {"id":"htan","name":"HTAN","category":"omics","ttl":"monthly","tags":["spatial","tumor_atlas"],"new":True},
    {"id":"stomicsdb","name":"STOmicsDB","category":"omics","ttl":"monthly","tags":["spatial"],"new":True},
    {"id":"proteomicsdb","name":"ProteomicsDB","category":"omics","ttl":"monthly","tags":["proteomics"]},
    {"id":"pride","name":"PRIDE","category":"omics","ttl":"monthly","tags":["proteomics"]},
    {"id":"metabolights","name":"MetaboLights","category":"omics","ttl":"monthly","tags":["metabolomics"],"new":True},
    {"id":"hmdb","name":"HMDB","category":"omics","ttl":"monthly","tags":["metabolomics"],"new":True},
    # Perturbation & Regulation (6)
    {"id":"depmap","name":"DepMap","category":"perturbation","ttl":"monthly","tags":["dependency"]},
    {"id":"lincs","name":"LINCS (SigCom/LDP3)","category":"perturbation","ttl":"monthly","tags":["perturbation"]},
    {"id":"rnacentral","name":"RNAcentral","category":"perturbation","ttl":"monthly","tags":["lncrna"]},
    {"id":"mirnet","name":"miRNet","category":"perturbation","ttl":"monthly","tags":["miRNA"]},
    {"id":"encori","name":"ENCORI/starBase","category":"perturbation","ttl":"monthly","tags":["miRNA_interactions"]},
    {"id":"phosphositeplus","name":"PhosphoSitePlus","category":"perturbation","ttl":"monthly","tags":["phospho"],"new":True},
    # Tractability & Drugs (7)
    {"id":"chembl","name":"ChEMBL","category":"tractability","ttl":"monthly","tags":["compounds"]},
    {"id":"dgidb","name":"DGIdb","category":"tractability","ttl":"monthly","tags":["drug_gene"]},
    {"id":"sabdab","name":"SAbDab","category":"tractability","ttl":"quarterly","tags":["antibodies"]},
    {"id":"thera_sabdab","name":"Thera-SAbDab","category":"tractability","ttl":"quarterly","tags":["antibody_therapeutics"]},
    {"id":"iedb","name":"IEDB","category":"tractability","ttl":"monthly","tags":["epitopes"]},
    {"id":"ribocentre","name":"Ribocentre","category":"tractability","ttl":"quarterly","tags":["aptamers"],"new":True},
    {"id":"inxight","name":"Inxight Drugs","category":"tractability","ttl":"monthly","tags":["drugs","synonyms"]},
    # Clinical & Safety (5 after consolidation)
    {"id":"clinicaltrials","name":"ClinicalTrials.gov","category":"clinical","ttl":"daily","tags":["trials"]},
    {"id":"who_ictrp","name":"WHO ICTRP","category":"clinical","ttl":"daily","tags":["trials"]},
    {"id":"eu_ctr","name":"EU CTR","category":"clinical","ttl":"weekly","tags":["trials"]},
    {"id":"faers","name":"openFDA (FAERS)","category":"clinical","ttl":"weekly","tags":["safety"]},
    {"id":"orphanet","name":"Orphanet","category":"clinical","ttl":"monthly","tags":["rare_disease"],"new":True},
    # Competitive Intelligence (3)
    {"id":"patentsview","name":"PatentsView","category":"competitive","ttl":"annually","tags":["patents"]},
    {"id":"surechembl","name":"SureChEMBL","category":"competitive","ttl":"annually","tags":["patents"]},
    {"id":"google_patents","name":"Google Patents","category":"competitive","ttl":"on_demand","tags":["patents","fulltext"]},
    # Literature (2)
    {"id":"europe_pmc","name":"Europe PMC","category":"literature","ttl":"monthly","tags":["papers"]},
    {"id":"pubmed_eutils","name":"PubMed E-utilities","category":"literature","ttl":"monthly","tags":["papers"]},
]
assert len(SOURCES) == 55

ALIASES = {"tabula_sapiens": "cellxgene"}

TTL_MAP = {
    "daily": 1*24*60*60,
    "weekly": 7*24*60*60,
    "monthly": 30*24*60*60,
    "quarterly": 90*24*60*60,
    "annually": 365*24*60*60,
    "on_demand": None,
}

HEAVY_MODULES = {
    "genetics_mr","genetics_coloc","assoc_sc","spatial_expression",
    "omics_phosphoproteomics","omics_metabolites","clin_endpoints","comp_freedom"
}

def _modules_by_bucket() -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = {}
    for m in MODULES:
        d.setdefault(m["bucket"], []).append(m)
    return d

def _resolve_source(sid: str) -> str:
    return ALIASES.get(sid, sid)

def _ttl_seconds(sid: str) -> Optional[int]:
    # Explicit ttl field wins; map to seconds
    sid = _resolve_source(sid)
    src = next((s for s in SOURCES if s["id"] == sid), None)
    if not src:
        return None
    return TTL_MAP.get(src.get("ttl","monthly"), 30*24*60*60)

@router.get("/registry/counts")
def registry_counts() -> Dict[str, Any]:
    new_mods = sum(1 for m in MODULES if m.get("new"))
    new_srcs = sum(1 for s in SOURCES if s.get("new"))
    buckets = _modules_by_bucket()
    return {
        "modules_total": len(MODULES),
        "modules_new": new_mods,
        "modules_by_bucket": {k: len(v) for k,v in buckets.items()},
        "sources_total": len(SOURCES),
        "sources_new": new_srcs,
        "alias_map": ALIASES,
    }

@router.get("/registry/modules")
def registry_modules() -> Dict[str, Any]:
    return {"count": len(MODULES), "buckets": _modules_by_bucket(), "items": MODULES}

@router.get("/registry/sources")
def registry_sources() -> Dict[str, Any]:
    # attach ttl_seconds for quick consumption by clients
    enriched = []
    for s in SOURCES:
        s2 = dict(s)
        s2["ttl_seconds"] = _ttl_seconds(s["id"])
        enriched.append(s2)
    return {"count": len(SOURCES), "items": enriched, "aliases": ALIASES}

# ---- Synthesis plan (non-executing orchestrator) ----

class SynthesisRequest(BaseModel):
    target: str
    disease: str
    modules: Optional[List[str]] = None
    buckets: Optional[List[str]] = None
    concurrency: Optional[int] = 8
    max_calls: Optional[int] = 110

def _plan(buckets: Optional[List[str]]) -> Dict[str, Any]:
    sel_buckets = buckets or [
        "TARGET_CHARACTERIZATION","GENETIC_CAUSALITY","MULTI_OMICS_ASSOCIATION",
        "MECHANISM_BIOLOGY","TRACTABILITY_MODALITY","CLINICAL_READINESS","COMPETITIVE_LANDSCAPE"
    ]
    mods = [m for m in MODULES if m["bucket"] in sel_buckets]
    budgets = {m["id"]: (4 if m["id"] in HEAVY_MODULES else 2) for m in mods}
    total_calls = sum(budgets.values())
    return {"buckets": sel_buckets, "budgets": budgets, "estimated_calls": total_calls}

@router.post("/synthesis/run")
def synthesis_run(req: SynthesisRequest = Body(...)) -> Dict[str, Any]:
    # NOTE: This is a planning stub only; fan-out execution stays in existing services.
    plan = _plan(req.buckets)
    # crude runtime estimate: assume 150ms avg per call with concurrency sem
    conc = max(1, int(req.concurrency or 8))
    total = plan["estimated_calls"]
    avg_ms = 150
    est_seq_ms = total * avg_ms
    est_conc_ms = int(est_seq_ms / conc)
    return {
        "target": req.target,
        "disease": req.disease,
        "plan": plan,
        "concurrency": conc,
        "max_calls": int(req.max_calls or 110),
        "est_runtime_ms": {"sequential": est_seq_ms, "concurrent": est_conc_ms},
        "notes": "This endpoint plans only. Execution uses existing module endpoints and caching."
    }
# ======================== End Registry & Synthesis (2025-10) ========================



# ===================== Synthesis v2 Add-ons =====================
from pydantic import BaseModel

async def _math_competitive(symbol: str, condition: Optional[str] = None) -> dict:
    cites = []; trials_n=0; patents_n=0; programs_n=0; assignees=[]
    try:
        ev1 = await comp_intensity(symbol=symbol); d1 = ev1.data or {}; cites.extend(ev1.citations or [])
        trials_n = d1.get("trials_count") or (len(d1.get("trials") or []) if isinstance(d1.get("trials"), list) else 0) or (ev1.fetched_n or 0)
        programs_n = d1.get("programs_count") or 0
    except Exception: pass
    try:
        ev2 = await comp_freedom(symbol=symbol); d2 = ev2.data or {}; cites.extend(ev2.citations or [])
        pats = d2.get("patents") or d2.get("results") or []
        patents_n = (len(pats) if isinstance(pats, list) else (d2.get("count") or d2.get("total") or 0))
        if isinstance(pats, list):
            for p in pats:
                owner = (p.get("assignee") or p.get("assignee_org") or p.get("applicant") or "").strip()
                if owner: assignees.append(owner)
        else:
            assignees = list(d2.get("assignees") or d2.get("owners") or [])
    except Exception: pass
    N = trials_n + patents_n + programs_n
    crowd_pressure = 0.0 if N<=0 else min(1.0, (trials_n*1.0 + patents_n*0.5 + programs_n*0.7)/max(10.0, float(N)))
    from collections import Counter
    c = Counter([a for a in assignees if a]); tot = sum(c.values()) or 1
    hhi = sum((v/tot)**2 for v in c.values())
    concentration = float(hhi); novelty = 1.0 - concentration if N>0 else 0.5
    return {"scores":{"crowd_pressure": crowd_pressure, "concentration": concentration, "novelty": novelty},
            "components":{"trials_n":trials_n,"patents_n":patents_n,"programs_n":programs_n,"assignees":list(c.keys())[:10]},
            "citations": list(dict.fromkeys(cites))[:200]}

async def _lit_meta_angles(symbol: str, condition: Optional[str] = None, limit: int = 80) -> dict:
    try:
        ev = await lit_angles(symbol=symbol, condition=condition, limit=limit)
        hits = (ev.data or {}).get("results") or []; cites = list(ev.citations or [])
    except Exception:
        try:
            query = f"{symbol} {condition}" if condition else symbol
            ev = await lit_search(query=query, limit=limit)
            hits = (ev.data or {}).get("results") or []; cites = list(ev.citations or [])
        except Exception:
            hits=[]; cites=[]
    def _ngrams(s, n=2):
        toks = [t for t in (s or "").lower().split() if t.isalnum()]; 
        return [" ".join(toks[i:i+n]) for i in range(len(toks)-n+1)]
    theme_counts={}; by_year={}
    for r in hits:
        title = r.get("title") or ""; year = r.get("year")
        if year and str(year).isdigit():
            by_year[int(str(year)[:4])] = by_year.get(int(str(year)[:4]), 0) + 1
        for bg in _ngrams(title):
            if len(bg) < 5: continue
            theme_counts[bg] = theme_counts.get(bg, 0) + 1
    top_themes = sorted(theme_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    themes_out=[]; 
    for theme, cnt in top_themes:
        papers = [r for r in hits if theme in ((r.get("title") or "").lower())][:10]
        pmids = [p.get("pmid") or p.get("pmcid") for p in papers if (p.get("pmid") or p.get("pmcid"))]
        themes_out.append({"theme": theme, "count": cnt, "pmids": pmids})
    contradictions=[]
    for t in themes_out:
        pos=neg=0
        for p in hits:
            title=(p.get("title") or "").lower()
            if t["theme"] in title:
                pos += int("activat" in title or "upregulat" in title)
                neg += int("inhibit" in title or "downregulat" in title or "block" in title)
        if pos and neg:
            contradictions.append({"theme": t["theme"], "pro": pos, "contra": neg})
    if by_year:
        years=sorted(by_year); recent=sum(v for y,v in by_year.items() if y>= (years[-1]-2)); prior=sum(v for y,v in by_year.items() if y < (years[-1]-2))
        recency_boost = float(recent/max(1,prior)) if prior else 1.5
    else: recency_boost=1.0
    return {"scores":{"angles_n": len(themes_out), "recency_boost": recency_boost},
            "components":{"themes": themes_out, "contradictions": contradictions},
            "citations": list(dict.fromkeys(cites))[:200]}

class CrossBucketSynthesis(BaseModel):
    target: str
    disease: Optional[str] = None
    evidence_strength: float
    druggability: float
    clinical_potential: float
    strategic_priority: float
    target_score: float
    ledger: Dict[str, float]
    pieces: Dict[str, Any]
    fetched_at: str

def _cap01(x: float) -> float:
    try: return float(max(0.0, min(1.0, x)))
    except Exception: return 0.0

@router.get("/synth/integrate", response_model=CrossBucketSynthesis)
async def synth_integrate(gene: str, condition: Optional[str] = None, mode: str = Query("math", description="math only (uses live endpoints)")):
    symbol = await _normalize_symbol(gene)
    G, A, B, T, C, K = await asyncio.gather(
        _math_genetics(gene=symbol, condition=condition),
        _math_association(gene=symbol, condition=condition),
        _math_biology(gene=symbol),
        _math_tractability(symbol=symbol),
        _math_clinical(symbol=symbol, condition=condition),
        _math_competitive(symbol=symbol, condition=condition)
    )
    g_logLR = float((G.get("components") or {}).get("mr_logLR", 0.0) + (G.get("components") or {}).get("coloc_logLR", 0.0) + (G.get("components") or {}).get("rare_logLR", 0.0) + (G.get("components") or {}).get("functional_logLR", 0.0))
    om_p = float((A.get("scores") or {}).get("hmp_p", 1.0)); om_lr = _sbb_p_to_lr(om_p); a_logLR = 0.5 * math.log(max(1e-9, om_lr))
    prox = float((B.get("scores") or {}).get("mechanistic_proximity", 0.5)); mech_lr = max(0.5, min(2.0, 1.0 + (prox-0.5)*1.5)); b_logLR = 0.3 * math.log(mech_lr)
    prior=0.02; evidence_strength = _cap01(_inv_logit(_logit(prior) + g_logLR + a_logLR + b_logLR))
    sc = T.get("scores") or {}; druggability = _cap01(max(float(sc.get("P.SM") or 0.0), float(sc.get("P.Ab") or 0.0), float(sc.get("P.Oligo") or 0.0)))
    clinical_potential = _cap01(float((C.get("scores") or {}).get("p_PoC", 0.0)))
    crowd = float((K.get("scores") or {}).get("crowd_pressure", 0.5)); strategic_priority = _cap01(1.0 - 0.6*crowd)
    target_score = evidence_strength * druggability * clinical_potential * strategic_priority
    pieces = {"genetics": G, "association": A, "biology": B, "tractability": T, "clinical": C, "competitive": K}
    ledger = {"genetics_logLR": g_logLR, "omics_logLR": a_logLR, "mechanism_logLR": b_logLR}
    return CrossBucketSynthesis(target=symbol, disease=condition, evidence_strength=float(evidence_strength), druggability=float(druggability), clinical_potential=float(clinical_potential), strategic_priority=float(strategic_priority), target_score=float(target_score), ledger=ledger, pieces=pieces, fetched_at=str(_now()))

@router.get("/lit/meta", response_model=Evidence)
async def lit_meta(symbol: str, condition: Optional[str] = None, limit: int = Query(80, ge=10, le=200)):
    out = await _lit_meta_angles(symbol=symbol, condition=condition, limit=limit)
    return Evidence(status="OK", source="Europe PMC (meta)", fetched_n=(out.get("scores") or {}).get("angles_n", 0),
                    data=out, citations=out.get("citations") or [], fetched_at=_now())
