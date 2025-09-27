# app/routers/targetval_router.py
"""
Routes implementing the TARGETVAL gateway (bounded, resilient).

This module exposes a suite of REST endpoints grouped by functional buckets
(Human Genetics; Disease Association; Expression; Mechanistic Wiring; Tractability; Clinical Translation; Competition & IP).

**September 2025 refresh**
- Migrated *fundamental genetics* endpoints to the Open Targets **Platform** v4 GraphQL API.
  Open Targets Genetics endpoints are deprecated and have been shut down.
- genetics_l2g now queries Platform GraphQL and expects **Ensembl gene ID + EFO ID**.
  If the client sends a symbol or free-text disease, we normalise to the required identifiers.
- genetics_mendelian now sources ClinVar / EVA / gene-burden evidence via the Platform API.
- Added robust normalisation helpers for Ensembl gene ID and EFO ID (OLS fallback).
- Kept backward-compatible signatures: still accept `gene` and `efo` query params,
  while also supporting optional `ensembl` (Ensembl gene id) in relevant endpoints.
- Consistent Evidence payloads and pragmatic fallbacks remain unchanged.

Public gateway only â no API keys required.
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

from app.utils.validation import validate_symbol, validate_condition

# -----------------------------------------------------------------------------
# Router & models
# -----------------------------------------------------------------------------
router = APIRouter()


class Evidence(BaseModel):
    status: str  # "OK" | "ERROR" | "NO_DATA"
    source: str  # upstream API / method used
    fetched_n: int  # count before limit slicing (if applicable)
    data: Dict[str, Any]  # module-specific payload
    citations: List[str]  # URLs used as evidence
    fetched_at: float  # UNIX timestamp


# -----------------------------------------------------------------------------
# Caching, concurrency & outbound client defaults
# -----------------------------------------------------------------------------
CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL: int = int(os.getenv("CACHE_TTL_SECONDS", str(24 * 60 * 60)))

# Outbound HTTP client settings (configurable)
DEFAULT_TIMEOUT = httpx.Timeout(
    float(os.getenv("OUTBOUND_TIMEOUT_S", "12.0")),  # per-attempt read/write
    connect=6.0,
)
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": os.getenv(
        "OUTBOUND_USER_AGENT",
        "TargetVal/1.4 (+https://github.com/aureten/Targetval-gateway)",
    ),
    "Accept": "application/json",
}
REQUEST_BUDGET_S: float = float(os.getenv("REQUEST_BUDGET_S", "25.0"))
OUTBOUND_TRIES: int = int(os.getenv("OUTBOUND_TRIES", "2"))
BACKOFF_BASE_S: float = float(os.getenv("BACKOFF_BASE_S", "0.6"))
MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "8"))
_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


def _now() -> float:
    return time.time()


# -----------------------------------------------------------------------------
# Bounded outbound wrappers
# -----------------------------------------------------------------------------
async def _get_json(
    url: str, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None
) -> Any:
    """HTTP GET â JSON with cache, bounded total time, limited retries, jittered backoff.
    More robust to content-encoding: attempts gzip decode if JSON parse fails.
    """
    cached = CACHE.get(url)
    if cached and (_now() - cached.get("timestamp", 0) < CACHE_TTL):
        return cached["data"]

    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                remaining = REQUEST_BUDGET_S - (_now() - t0)
                if remaining <= 0:
                    break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(
                        client.get(url, headers=merged), timeout=remaining
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(
                            status_code=resp.status_code, detail=resp.text[:500]
                        )
                        backoff = min((2 ** (attempt - 1)) * BACKOFF_BASE_S, 3.0) + random.random() * 0.2
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    # Primary: try JSON directly
                    try:
                        data = resp.json()
                    except Exception:
                        # Fallback: attempt to gunzip + json if server mislabels content-type
                        try:
                            buf = io.BytesIO(resp.content)
                            with gzip.GzipFile(fileobj=buf) as gz:
                                data = json.loads(gz.read().decode("utf-8"))
                        except Exception as ge:
                            last_err = ge
                            raise
                    CACHE[url] = {"data": data, "timestamp": _now()}
                    return data
                except Exception as e:
                    last_err = e
                    backoff = min((2 ** (attempt - 1)) * BACKOFF_BASE_S, 3.0) + random.random() * 0.2
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {last_err}")


async def _get_text(
    url: str, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None
) -> str:
    """HTTP GET â text with bounded total time, limited retries, jittered backoff."""
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                remaining = REQUEST_BUDGET_S - (_now() - t0)
                if remaining <= 0:
                    break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(
                        client.get(url, headers=merged), timeout=remaining
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(
                            status_code=resp.status_code, detail=resp.text[:500]
                        )
                        backoff = min((2 ** (attempt - 1)) * BACKOFF_BASE_S, 3.0) + random.random() * 0.2
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    return resp.text
                except Exception as e:
                    last_err = e
                    backoff = min((2 ** (attempt - 1)) * BACKOFF_BASE_S, 3.0) + random.random() * 0.2
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET text failed for {url}: {last_err}")


async def _post_json(
    url: str,
    payload: Dict[str, Any],
    tries: int = OUTBOUND_TRIES,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """HTTP POST â JSON with bounded total time, limited retries, jittered backoff."""
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                remaining = REQUEST_BUDGET_S - (_now() - t0)
                if remaining <= 0:
                    break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(
                        client.post(url, json=payload, headers=merged), timeout=remaining
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(
                            status_code=resp.status_code, detail=resp.text[:500]
                        )
                        backoff = min((2 ** (attempt - 1)) * BACKOFF_BASE_S, 3.0) + random.random() * 0.2
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as e:
                    last_err = e
                    backoff = min((2 ** (attempt - 1)) * BACKOFF_BASE_S, 3.0) + random.random() * 0.2
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"POST failed for {url}: {last_err}")


async def _safe_call(coro):
    """Wrap a coroutine call in an Evidence object, catching exceptions."""
    try:
        return await coro
    except HTTPException as e:
        return Evidence(
            status="ERROR",
            source=str(e.detail),
            fetched_n=0,
            data={},
            citations=[],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={},
            citations=[],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# Helpers for EFO/Ensembl resolution, identifiers, and simple heuristics
# -----------------------------------------------------------------------------
_OT_GQL = "https://api.platform.opentargets.org/api/v4/graphql"


async def _resolve_efo(efo_or_term: str) -> Tuple[str, str, str, List[str]]:
    """
    Resolve EFO input to (efo_id_norm, efo_uri, disease_label, citations).

    Behaviour:
      - If input looks like EFO:* or EFO_*, normalise to underscore form and return.
      - Else: try EpiGraphDB fuzzy term â EFO id mapping.
      - Else: fall back to EBI OLS4 search (ontology=efo) and pick the top hit.
    """
    citations: List[str] = []
    efo = (efo_or_term or "").strip()
    if not efo:
        return "", "", "", citations

    # Normalise colon to underscore
    if ":" in efo:
        efo = efo.replace(":", "_")

    # If already an EFO ID
    if efo.upper().startswith("EFO_"):
        efo_norm = efo.upper()
        uri = f"http://www.ebi.ac.uk/efo/{efo_norm}"
        return efo_norm, uri, efo_norm, citations

    # 1) Try EpiGraphDB mapping as before
    map_url = (
        f"https://api.epigraphdb.org/ontology/disease-efo?efo_term={urllib.parse.quote(efo)}&fuzzy=true"
    )
    try:
        mapping = await _get_json(map_url, tries=1)
        citations.append(map_url)
        results = mapping.get("results", []) if isinstance(mapping, dict) else []
        if results:
            top = results[0]
            label = top.get("disease_label") or (top.get("disease") or {}).get("label")
            raw = top.get("efo_term") or (top.get("disease") or {}).get("id") or ""
            if raw:
                efo_id_norm = raw.replace(":", "_").upper()
                uri = f"http://www.ebi.ac.uk/efo/{efo_id_norm}"
                return efo_id_norm, uri, label or efo, citations
    except Exception:
        pass

    # 2) Fallback to OLS4 term search
    ols = f"https://www.ebi.ac.uk/ols4/api/search?q={urllib.parse.quote(efo)}&ontology=efo&rows=5"
    try:
        js = await _get_json(ols, tries=1)
        citations.append(ols)
        docs = ((js.get("response") or {}).get("docs") or []) if isinstance(js, dict) else []
        for doc in docs:
            iri = doc.get("iri") or ""
            label = doc.get("label") or efo
            # Try to extract EFO id from IRI tail
            tail = iri.rsplit("/", 1)[-1]
            if tail.upper().startswith("EFO_"):
                efo_id_norm = tail.upper()
                uri = f"http://www.ebi.ac.uk/efo/{efo_id_norm}"
                return efo_id_norm, uri, label, citations
    except Exception:
        pass

    # Fall back to returning the input as label
    return "", "", efo, citations


async def _ensembl_from_symbol_or_id(s: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Given a gene token, return (ensembl_id, symbol_norm, citations).

    - If s already looks like ENSG..., return as ensembl_id.
    - Else resolve via Ensembl REST xrefs (symbol â Ensembl gene id).
    """
    citations: List[str] = []
    if not s:
        return None, None, citations

    tok = s.strip()
    if tok.upper().startswith("ENSG"):
        return tok, None, citations

    # Primary: exact xref by symbol
    url = f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{urllib.parse.quote(tok)}?content-type=application/json"
    try:
        arr = await _get_json(url, tries=1)
        citations.append(url)
        if isinstance(arr, list):
            for rec in arr:
                if (rec.get("type") or "").upper() == "GENE" and rec.get("id", "").upper().startswith("ENSG"):
                    return rec.get("id"), tok.upper(), citations
    except Exception:
        pass

    # Fallback: name-based xref
    url2 = f"https://rest.ensembl.org/xrefs/name/homo_sapiens/{urllib.parse.quote(tok)}?content-type=application/json"
    try:
        arr = await _get_json(url2, tries=1)
        citations.append(url2)
        if isinstance(arr, list):
            for rec in arr:
                if (rec.get("type") or "").upper() == "GENE" and rec.get("id", "").upper().startswith("ENSG"):
                    return rec.get("id"), tok.upper(), citations
    except Exception:
        pass

    return None, tok.upper(), citations


# Existing helpers -----------------------------------------------------------------

# Minimal alias coverage for common cases; extend as needed
_ALIAS_GENE_MAP: Dict[str, str] = {
    "CB1": "CNR1",
    "CB-1": "CNR1",
    "CNR-1": "CNR1",
    "TGFR2": "TGFBR2",
}
_COMMON_GENE_SET = {"CNR1", "IL6", "TGFBR2", "FAP"} | set(_ALIAS_GENE_MAP.keys()) | set(_ALIAS_GENE_MAP.values())
_CONDITION_ALIAS_MAP: Dict[str, str] = {
    # Ambiguous acronyms handled as diseases when explicitly intended
    "FAP": "Familial adenomatous polyposis",
    # Add more as needed, e.g., "COPD": "Chronic obstructive pulmonary disease"
}


def _looks_like_gene_token(s: str) -> bool:
    """Heuristic: short uppercase token with no spaces; in known sets or alnum with digits."""
    if not s:
        return False
    tok = s.strip()
    if " " in tok:
        return False
    up = tok.upper()
    if up in _COMMON_GENE_SET:
        return True
    # simple shape heuristics: 2-12 chars, alnum, often with a digit (e.g., IL6)
    if 2 <= len(up) <= 12 and up.replace("-", "").isalnum():
        if any(c.isdigit() for c in up) or up.isalpha():
            return True
    # also allow Ensembl IDs
    if up.startswith("ENSG"):
        return True
    return False


def _expand_condition_alias(cond: str) -> str:
    up = (cond or "").strip().upper()
    return _CONDITION_ALIAS_MAP.get(up, cond)


async def _normalize_symbol(symbol: str) -> str:
    """
    Normalize gene 'symbol' to an official HGNC-like symbol when possible.
    Fast path via alias map; slow path via UniProt gene field if needed.
    """
    if not symbol:
        return symbol
    up = symbol.strip().upper()
    if up in _ALIAS_GENE_MAP:
        return _ALIAS_GENE_MAP[up]
    if up in _COMMON_GENE_SET or up.startswith("ENSG"):
        return up

    # Try UniProt: look for gene field to capture canonical geneName
    url = (
        "https://rest.uniprot.org/uniprotkb/search?"
        f"query={urllib.parse.quote(symbol)}+AND+organism_id:9606&fields=genes&format=json&size=1"
    )
    try:
        js = await _get_json(url, tries=1)
        res = js.get("results", []) if isinstance(js, dict) else []
        if res:
            genes = res[0].get("genes") or []
            # Prefer primary geneName if present
            for g in genes:
                gn = g.get("geneName", {}).get("value")
                if gn:
                    return gn.upper()
            # Fallback: any synonym
            for g in genes:
                syns = g.get("synonyms") or []
                for syn in syns:
                    val = syn.get("value")
                    if val:
                        return val.upper()
    except Exception:
        pass
    return up


async def _uniprot_primary_accession(symbol: str) -> Optional[str]:
    url = (
        "https://rest.uniprot.org/uniprotkb/search"
        f"?query=gene_exact:{urllib.parse.quote(symbol)}+AND+organism_id:9606"
        "&fields=accession&format=json&size=1"
    )
    try:
        body = await _get_json(url, tries=1)
        res = body.get("results", []) if isinstance(body, dict) else []
        if res:
            return (
                res[0].get("primaryAccession")
                or (res[0].get("uniProtkbId") if isinstance(res[0], dict) else None)
            )
    except Exception:
        return None
    return None


async def _extracellular_len_from_uniprot(accession: str) -> int:
    """From UniProt entry JSON, approximate extracellular aa by summing 'Topological domain' with 'Extracellular' in description."""
    if not accession:
        return 0
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
    except Exception:
        return 0


async def _iedb_counts(symbol: str, limit: int = 25) -> Tuple[int, int]:
    """Return (epitope_count, tcell_assay_count) using IEDB IQ-API (best-effort)."""
    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    tc_url = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    epi_n = 0
    tc_n = 0
    try:
        ej = await _get_json(epi_url, tries=1)
        epi_n = len(ej if isinstance(ej, list) else [])
    except Exception:
        pass
    try:
        tj = await _get_json(tc_url, tries=1)
        tc_n = len(tj if isinstance(tj, list) else [])
    except Exception:
        pass
    return epi_n, tc_n


# -----------------------------------------------------------------------------
# Utility endpoints
# -----------------------------------------------------------------------------
@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now()}


@router.get("/status")
def status() -> Dict[str, Any]:
    return {
        "service": "targetval-gateway",
        "time": _now(),
        "modules": [
            "B1: /genetics/l2g, /genetics/rare, /genetics/mendelian, /genetics/mr, /genetics/lncrna, /genetics/mirna, /genetics/sqtl, /genetics/epigenetics",
            "B2: /assoc/bulk-rna, /assoc/bulk-prot, /assoc/sc, /assoc/perturb",
            "B3: /expr/baseline, /expr/localization, /expr/inducibility",
            "B4: /mech/pathways, /mech/ppi, /mech/ligrec",
            "B5: /tract/drugs, /tract/ligandability-sm, /tract/ligandability-ab, /tract/ligandability-oligo, /tract/modality, /tract/immunogenicity",
            "B6: /clin/endpoints, /clin/rwe, /clin/safety, /clin/pipeline, /clin/biomarker-fit",
            "B7: /comp/intensity, /comp/freedom",
        ],
    }


# -----------------------------------------------------------------------------
# BUCKET 1 â Human Genetics & Causality
# -----------------------------------------------------------------------------
@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(
    gene: str,
    efo_id: str = Query(..., alias="efo"),
    limit: int = Query(50, ge=1, le=200),
    ensembl: Optional[str] = Query(None, description="Optional Ensembl gene ID override"),
) -> Evidence:
    """
    L2G-style evidence via **NHGRI-EBI GWAS Catalog REST API v2** with gene + disease filters.
    We avoid Open Targets GraphQL due to instability and rely on the Catalog's curated associations.

    Behaviour:
      - Resolve inputs to **HGNC symbol** and **EFO id**.
      - Query /associations filtered by efoId and geneName (page through up to `limit` records).
      - Return the association list along with the mapped disease label and identifiers.
      - If no results, status = "NO_DATA" with helpful context.

    Docs: https://www.ebi.ac.uk/gwas/ (v2 REST API), https://www.ebi.ac.uk/gwas/rest/docs/api
    """
    from urllib.parse import urlencode, quote

    validate_symbol(gene, field_name="gene")
    validate_symbol(efo_id, field_name="efo")

    # Resolve symbol/Ensembl and EFO
    ensg = ensembl
    ensg_cites: List[str] = []
    symbol_norm = None
    if not ensg:
        ensg, symbol_norm, ensg_cites = await _ensembl_from_symbol_or_id(gene)
    if not symbol_norm:
        symbol_norm = (gene or "").upper()

    efo_norm, efo_uri, disease_label, efo_cites = await _resolve_efo(efo_id)
    if not efo_norm:
        return Evidence(
            status="ERROR",
            source="GWAS Catalog REST v2",
            fetched_n=0,
            data={"message": "Could not resolve EFO id from input", "input": efo_id},
            citations=efo_cites,
            fetched_at=_now(),
        )

    base = "https://www.ebi.ac.uk/gwas/rest/api/associations"
    params = {"efoId": efo_norm, "geneName": symbol_norm, "size": max(20, min(200, limit))}
    url = f"{base}?{urlencode(params)}"

    try:
        res = await _get_json(url, tries=1)
        cites = [url] + efo_cites + ensg_cites
        rows = []
        # v2 returns a HAL-style response with _embedded perhaps; handle both list/dict
        if isinstance(res, dict):
            emb = res.get("_embedded") or {}
            arr = emb.get("associations") or emb.get("associationDtos") or []
            if isinstance(arr, list):
                rows = arr
        elif isinstance(res, list):
            rows = res

        payload = {
            "gene": symbol_norm,
            "ensembl_id": ensg,
            "efo_id": efo_norm,
            "efo_uri": efo_uri,
            "disease_label": disease_label,
            "associations": rows[:limit],
            "support_links": [
                {"label": "Pan-UKBB portal", "url": "https://pan.ukbb.broadinstitute.org/"},
                {"label": "FinnGen results", "url": "https://www.finngen.fi/en/access_results"}
            ],
        }
        status = "OK" if rows else "NO_DATA"
        return Evidence(
            status=status,
            source="GWAS Catalog REST v2",
            fetched_n=len(rows),
            data=payload,
            citations=cites,
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"GWAS Catalog REST v2 error: {e}",
            fetched_n=0,
            data={"gene": symbol_norm, "efo_id": efo_norm},
            citations=[url],
            fetched_at=_now(),
        )
@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(
    gene: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_symbol(gene, field_name="gene")
    clinvar_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        f"db=clinvar&term={urllib.parse.quote(gene)}%5Bgene%5D&retmode=json"
    )
    try:
        js = await _get_json(clinvar_url, tries=1)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(
            status="OK" if ids else "NO_DATA",
            source="ClinVar E-utilities",
            fetched_n=len(ids),
            data={"gene": gene, "variants": ids[:limit]},
            citations=[clinvar_url],
            fetched_at=_now(),
        )
    except Exception:
        pass

    gql_url = "https://gnomad.broadinstitute.org/api"
    query = {
        "query": """
        query ($symbol: String!) {
          gene(symbol: $symbol) {
            variants { variantId genome { ac an } }
          }
        }""",
        "variables": {"symbol": gene},
    }
    try:
        body = await _post_json(gql_url, query, tries=1)
        variants = (body.get("data", {}).get("gene", {}).get("variants", []) or [])
        return Evidence(
            status="OK" if variants else "NO_DATA",
            source="gnomAD GraphQL",
            fetched_n=len(variants),
            data={"gene": gene, "variants": variants[:limit]},
            citations=[gql_url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="ClinVar+gnomAD empty/unavailable",
            fetched_n=0,
            data={"gene": gene, "variants": []},
            citations=[clinvar_url, gql_url],
            fetched_at=_now(),
        )


@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(
    gene: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    """
    Mendelian disease gene evidence from **ClinGen Gene-Disease Validity** (+ optional **OMIM** if API key configured).
    Replaces prior Open Targets calls.

    - Primary: ClinGen geneâdisease validity table (JSON export) filtered by HGNC symbol.
    - Optional: OMIM REST (requires OMIM_API_KEY) to pull phenotype mappings.
    Docs: ClinGen downloads & APIs; OMIM downloads/API (key required).
    """
    from urllib.parse import urlencode, quote

    validate_symbol(gene, field_name="gene")
    sym = (gene or "").upper()

    citations: List[str] = []

    # 1) ClinGen gene-disease validity JSON export
    # The listing page allows JSON export; we use the query param to filter by gene symbol if supported.
    # If the endpoint changes, we still return the full table and filter client-side.
    clingen_url = f"https://search.clinicalgenome.org/kb/gene-validity?format=json&search={quote(sym)}"
    try:
        cg = await _get_json(clingen_url, tries=1)
        citations.append(clingen_url)
        items = []
        # The JSON structure includes 'data' or array; be defensive.
        if isinstance(cg, dict):
            data = cg.get("data") or cg.get("items") or cg.get("results") or []
            if isinstance(data, list):
                items = [r for r in data if (str(r.get("geneSymbol") or r.get("gene", "")).upper() == sym)]
        elif isinstance(cg, list):
            items = [r for r in cg if (str(r.get("geneSymbol") or r.get("gene", "")).upper() == sym)]
    except Exception:
        items = []

    # 2) OMIM (optional)
    omim_key = os.getenv("OMIM_API_KEY") or os.getenv("OMIM_KEY")
    omim_payload = None
    if omim_key:
        omim_base = "https://api.omim.org/api/entry"
        qs = urlencode({"search": sym, "include": "geneMap", "format": "json", "apiKey": omim_key})
        omim_url = f"{omim_base}?{qs}"
        try:
            omim = await _get_json(omim_url, tries=1)
            citations.append("https://omim.org/downloads")
            citations.append(omim_url)
            # parse phenotype list crudely
            omim_payload = omim
        except Exception:
            pass
    else:
        # cite OMIM downloads page to indicate optional config
        citations.append("https://omim.org/downloads")

    payload = {
        "gene": sym,
        "clingen_gene_validity": (items or [])[:limit],
        "omim": omim_payload,
    }
    status = "OK" if items or omim_payload else "NO_DATA"
    return Evidence(
        status=status,
        source="ClinGen Gene Validity (+ OMIM optional)",
        fetched_n=(len(items) if items else 0),
        data=payload,
        citations=citations,
        fetched_at=_now(),
    )
@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(
    gene: str,
    efo_id: str = Query(..., alias="efo"),
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    """
    Mendelian randomization via **MRC IEU OpenGWAS API**.
    NOTE: Many OpenGWAS endpoints now require authentication; set OPENGWAS_TOKEN env var if needed.

    Strategy (pragmatic):
      1) Resolve disease label from EFO and search OpenGWAS for candidate outcome GWAS IDs.
      2) Return top matching studies plus a ready-to-run MR query URL template.
      3) If OPENGWAS provides a direct MR endpoint accessible without auth, attempt it; otherwise surface the search.
    """
    from urllib.parse import urlencode, quote

    validate_symbol(gene, field_name="gene")
    validate_symbol(efo_id, field_name="efo")

    efo_id_norm, efo_uri, disease_label, map_url = await _resolve_efo(efo_id)

    token = os.getenv("OPENGWAS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None

    # 1) Search OpenGWAS studies by disease label
    search_url = f"https://gwas.mrcieu.ac.uk/api/v1/studies?query={quote(disease_label)}"
    studies = []
    try:
        js = await _get_json(search_url, tries=1, headers=headers)
        # Expect list of studies; be defensive
        if isinstance(js, dict):
            arr = js.get("data") or js.get("results") or js.get("studies") or []
        else:
            arr = js
        if isinstance(arr, list):
            studies = arr[:limit]
    except Exception:
        pass

    # Provide a generic MR endpoint template (requires specific exposure/outcome IDs).
    # OpenGWAS classic MR endpoint pattern:
    mr_tpl = "https://api.opengwas.io/api/v1/mr?exposure={exposure_id}&outcome={outcome_id}"
    example = None
    if studies:
        # pick first as outcome example
        outcome_id = studies[0].get("id") or studies[0].get("study_id") or studies[0].get("accession")
        if outcome_id:
            example = mr_tpl.format(exposure_id="PUT_EXPOSURE_GWAS_ID", outcome_id=outcome_id)

    payload = {
        "gene": gene,
        "efo_id": efo_id_norm,
        "efo_uri": efo_uri,
        "outcome_trait": disease_label,
        "openGWAS_search_url": search_url,
        "candidate_outcomes": studies,
        "mr_endpoint_template": mr_tpl,
        "mr_example": example,
    }
    status = "OK" if studies else "NO_DATA"
    cites = [search_url, "https://api.opengwas.io/api/", "https://mrcieu.github.io/ieugwasr/"]
    if map_url:
        cites.insert(0, map_url)
    return Evidence(
        status=status,
        source="OpenGWAS (search + MR template)",
        fetched_n=len(studies),
        data=payload,
        citations=cites,
        fetched_at=_now(),
    )
@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(
    gene: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_symbol(gene, field_name="gene")
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(gene)}&page_size={limit}"
    try:
        js = await _get_json(url, tries=1)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK" if results else "NO_DATA",
            source="RNAcentral",
            fetched_n=len(results),
            data={"gene": gene, "lncRNAs": results[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="RNAcentral empty/unavailable",
            fetched_n=0,
            data={"gene": gene, "lncRNAs": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(
    gene: str,
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    """miRNAâgene interactions: miRNet (primary), ENCORI (fallback)."""
    validate_symbol(gene, field_name="gene")

    mirnet_url = "https://api.mirnet.ca/table/gene"
    payload = {"org": "hsa", "idOpt": "symbol", "myList": gene, "selSource": "All"}
    try:
        r = await _post_json(mirnet_url, payload, tries=1)
        rows = r.get("data", []) if isinstance(r, dict) else (r or [])
        if rows:
            simplified = [
                {
                    "miRNA": it.get("miRNA") or it.get("mirna") or it.get("ID"),
                    "target": it.get("Target") or it.get("Gene") or gene,
                    "evidence": it.get("Category") or it.get("Evidence") or it.get("Source"),
                    "pmid": it.get("PMID") or it.get("PubMedID"),
                    "source_db": it.get("Source") or "miRNet",
                }
                for it in rows[:limit]
            ]
            return Evidence(
                status="OK",
                source="miRNet 2.0",
                fetched_n=len(rows),
                data={"gene": gene, "interactions": simplified},
                citations=[mirnet_url],
                fetched_at=_now(),
            )
    except Exception:
        pass

    encori_url = (
        "https://rnasysu.com/encori/api/miRNATarget/"
        f"?assembly=hg38&geneType=mRNA&miRNA=all&clipExpNum=1&degraExpNum=0&pancancerNum=0&programNum=1&program=TargetScan"
        f"&target={urllib.parse.quote(gene)}&cellType=all"
    )
    try:
        tsv = await _get_text(encori_url, tries=1)
        lines = [ln for ln in tsv.splitlines() if ln.strip()]
        if len(lines) <= 1:
            return Evidence(
                status="NO_DATA",
                source="ENCORI/starBase empty",
                fetched_n=0,
                data={"gene": gene, "interactions": []},
                citations=[encori_url],
                fetched_at=_now(),
            )
        header = [h.strip() for h in lines[0].split("\t")]
        out = []
        for ln in lines[1:][:limit]:
            cols = ln.split("\t")
            row = dict(zip(header, cols))
            out.append(
                {
                    "miRNA": row.get("miRNA") or row.get("miRNA_Name"),
                    "target": row.get("Target_Gene") or gene,
                    "support": row.get("SupportType") or row.get("Support_Type"),
                    "pmid": row.get("PMID") or row.get("CLIP-Data_PubMed_ID"),
                    "cell_type": row.get("Cell_Type"),
                }
            )
        return Evidence(
            status="OK",
            source="ENCORI/starBase",
            fetched_n=len(out),
            data={"gene": gene, "interactions": out},
            citations=[encori_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="NO_DATA",
            source=f"ENCORI unavailable: {e}",
            fetched_n=0,
            data={"gene": gene, "interactions": []},
            citations=[mirnet_url, encori_url],
            fetched_at=_now(),
        )


@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(
    gene: str,
    limit: int = Query(50, ge=1, le=200),
    ensembl: Optional[str] = Query(None, description="Optional Ensembl gene ID override"),
) -> Evidence:
    """
    Splicing QTLs via **GTEx Portal API v2** (prefers sQTL; falls back to independent eQTLs).
    Replaces eQTL Catalogue, which was often empty.

    - Endpoint: /api/v2/association/independentSqtl (preferred)
    - Fallback: /api/v2/association/independentEqtl
    NOTE: GTEx expects **versioned GENCODE IDs** (e.g., ENSG00000122971.9).
    Docs: https://gtexportal.org/api/v2/redoc
    """
    from urllib.parse import urlencode, quote

    validate_symbol(gene, field_name="gene")

    ensg = ensembl
    symbol_norm = None
    sym_cites: List[str] = []
    if not ensg:
        ensg, symbol_norm, sym_cites = await _ensembl_from_symbol_or_id(gene)
    if not ensg:
        return Evidence(
            status="ERROR",
            source="GTEx API v2",
            fetched_n=0,
            data={"message": "Could not resolve Ensembl gene id", "input": gene},
            citations=sym_cites,
            fetched_at=_now(),
        )

    # Fetch Ensembl version to construct versioned GENCODE id
    gencode_id = ensg
    try:
        lookup_url = f"https://rest.ensembl.org/lookup/id/{quote(ensg)}?expand=0;content-type=application/json"
        info = await _get_json(lookup_url, tries=1)
        sym_cites.append(lookup_url)
        if isinstance(info, dict) and info.get("version"):
            gencode_id = f"{ensg}.{int(info.get('version'))}"
    except Exception:
        pass

    # Try sQTL first
    base = "https://gtexportal.org/api/v2/association/independentSqtl"
    url_sqtl = f"{base}?{urlencode({'gencodeId': gencode_id})}"
    url_eqtl = f"https://gtexportal.org/api/v2/association/independentEqtl?{urlencode({'gencodeId': gencode_id})}"

    citations = sym_cites + [url_sqtl]
    try:
        js = await _get_json(url_sqtl, tries=1)
        rows = []
        if isinstance(js, dict):
            rows = js.get("data") or js.get("sqtls") or js.get("associations") or []
        elif isinstance(js, list):
            rows = js
        if not rows:
            # fallback to eQTL
            js2 = await _get_json(url_eqtl, tries=1)
            citations.append(url_eqtl)
            if isinstance(js2, dict):
                rows = js2.get("data") or js2.get("eqtls") or js2.get("associations") or []
            elif isinstance(js2, list):
                rows = js2
        status = "OK" if rows else "NO_DATA"
        return Evidence(
            status=status,
            source="GTEx Portal API v2",
            fetched_n=len(rows),
            data={"gene": gene, "ensembl_id": ensg, "gencode_id": gencode_id, "sqtl_or_eqtl": rows[:limit]},
            citations=citations,
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"GTEx API v2 error: {e}",
            fetched_n=0,
            data={"gene": gene, "ensembl_id": ensg, "gencode_id": gencode_id},
            citations=citations,
            fetched_at=_now(),
        )
@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(
    gene: str,
    flank_kb: int = Query(50, ge=0, le=1000),
) -> Evidence:
    """
    Functional epigenomics support via **Roadmap Epigenomics** and **BLUEPRINT** resources.

    Implementation (lightweight but robust):
      - Resolve gene â genomic locus (Ensembl REST).
      - Return resource links for Roadmap (WashU portal/AWS) and BLUEPRINT (BDAP/IHEC) for that locus.
      - This replaces ENCODE-only queries that were sparse.

    Docs: Roadmap portal/AWS registry; BLUEPRINT BDAP; DeepBlue API (aggregates ENCODE/ROADMAP/BLUEPRINT).
    """
    from urllib.parse import urlencode, quote

    validate_symbol(gene, field_name="gene")
    ensg, _sym, ensg_cites = await _ensembl_from_symbol_or_id(gene)
    # fetch gene location from Ensembl REST overlap endpoint
    spans = None
    if ensg:
        try:
            # lookup gene region
            url = f"https://rest.ensembl.org/lookup/id/{quote(ensg)}?expand=0;content-type=application/json"
            info = await _get_json(url, tries=1)
            ensg_cites.append(url)
            if isinstance(info, dict) and info.get("start") and info.get("end"):
                chrom = info.get("seq_region_name")
                start = max(1, int(info.get("start")) - flank_kb * 1000)
                end = int(info.get("end")) + flank_kb * 1000
                spans = {"chrom": str(chrom), "start": start, "end": end, "assembly": info.get("assembly_name")}
        except Exception:
            pass

    # Build resource links
    links = []
    if spans:
        chrom = spans["chrom"]
        start = spans["start"]
        end = spans["end"]
        # Roadmap portal grid page
        links.append({
            "label": "Roadmap Epigenomics portal",
            "url": "http://egg2.wustl.edu/roadmap/web_portal/",
            "note": "Browse tracks; use WashU Browser to zoom to locus"
        })
        # Roadmap AWS registry dataset
        links.append({
            "label": "Roadmap on AWS (registry)",
            "url": "https://registry.opendata.aws/roadmapepigenomics/",
            "note": "Raw/processed files in S3; requires downstream tooling"
        })
        # BLUEPRINT general
        links.append({
            "label": "BLUEPRINT Data portal",
            "url": "https://projects.ensembl.org/blueprint/",
            "note": "Hematopoietic epigenomes overview"
        })
        # DeepBlue (aggregator/API)
        links.append({
            "label": "DeepBlue Epigenomic Data Server",
            "url": "http://deepblue.mpi-inf.mpg.de/",
            "note": "Programmatic access to ENCODE/ROADMAP/BLUEPRINT region sets"
        })

    payload = {
        "gene": gene,
        "ensembl_id": ensg,
        "region": spans,
        "resources": links,
    }
    citations = ensg_cites + [
        "https://egg2.wustl.edu/roadmap/web_portal/",
        "https://registry.opendata.aws/roadmapepigenomics/",
        "https://projects.ensembl.org/blueprint/",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC4987868/",
    ]
    status = "OK" if links else "NO_DATA"
    return Evidence(
        status=status,
        source="Roadmap Epigenomics + BLUEPRINT (links)",
        fetched_n=len(links),
        data=payload,
        citations=citations,
        fetched_at=_now(),
    )
@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(
    condition: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_condition(condition, field_name="condition")
    url = f"https://gtexportal.org/api/v2/association/genesByTissue?tissueSiteDetail={urllib.parse.quote(condition)}"
    try:
        js = await _get_json(url, tries=1)
        genes = js.get("genes", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK" if genes else "NO_DATA",
            source="GTEx genesByTissue",
            fetched_n=len(genes),
            data={"condition": condition, "genes": genes[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        # Fallback â HPA search with rna_gtex hints
        hpa = (
            "https://www.proteinatlas.org/api/search_download.php"
            f"?format=json&columns=gene,rna_gtex,rna_tissue&search={urllib.parse.quote(condition)}"
        )
        try:
            hj = await _get_json(hpa, tries=1)
            rows = hj if isinstance(hj, list) else []
            genes = [{"gene": r.get("gene"), "rna_gtex": r.get("rna_gtex")} for r in rows if r.get("rna_gtex")]
            return Evidence(
                status="OK" if genes else "NO_DATA",
                source="Human Protein Atlas (fallback)",
                fetched_n=len(genes),
                data={"condition": condition, "genes": genes[:limit]},
                citations=[url, hpa],
                fetched_at=_now(),
            )
        except Exception:
            return Evidence(
                status="NO_DATA",
                source="GTEx+HPA unavailable or empty",
                fetched_n=0,
                data={"condition": condition, "genes": []},
                citations=[url, hpa],
                fetched_at=_now(),
            )


@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(
    condition: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_condition(condition, field_name="condition")
    url = (
        "https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search"
        f"?search={urllib.parse.quote(condition)}"
    )
    try:
        js = await _get_json(url, tries=1)
        records: List[Any] = []
        if isinstance(js, dict):
            records = js.get("items", js.get("proteins", js.get("results", [])))
        elif isinstance(js, list):
            records = js
        return Evidence(
            status="OK" if records else "NO_DATA",
            source="ProteomicsDB proteins search",
            fetched_n=len(records),
            data={"condition": condition, "proteins": records[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        # Fallback â PRIDE project list as a proxy
        pride = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
        try:
            pj = await _get_json(pride, tries=1)
            items = pj if isinstance(pj, list) else []
            return Evidence(
                status="OK" if items else "NO_DATA",
                source="PRIDE Archive (fallback)",
                fetched_n=len(items),
                data={"condition": condition, "proteins": items[:limit]},
                citations=[url, pride],
                fetched_at=_now(),
            )
        except Exception:
            return Evidence(
                status="NO_DATA",
                source="ProteomicsDB+PRIDE unavailable or empty",
                fetched_n=0,
                data={"condition": condition, "proteins": []},
                citations=[url, pride],
                fetched_at=_now(),
            )


@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(
    condition: str,
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    """Singleâcell signal using Human Protein Atlas search API.
    Accepts either a disease term or a gene-like token; if gene-like, normalizes and searches by gene.
    """
    validate_condition(condition, field_name="condition")
    search_term = condition
    normalized_symbol: Optional[str] = None
    if _looks_like_gene_token(condition):
        normalized_symbol = await _normalize_symbol(condition)
        search_term = normalized_symbol

    hpa_url = (
        "https://www.proteinatlas.org/api/search_download.php"
        f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(search_term)}"
    )
    try:
        js = await _get_json(hpa_url, tries=1)
        rows = js if isinstance(js, list) else []
        out: List[Dict[str, Any]] = []
        for r in rows:
            if any(k in r and r[k] for k in ("cell_type", "rna_cell_type")):
                out.append(
                    {
                        "gene": r.get("gene"),
                        "cell_type": r.get("cell_type") or r.get("rna_cell_type"),
                        "rna_gtex": r.get("rna_gtex"),
                        "tissue": r.get("rna_tissue"),
                    }
                )
        status = "OK" if out else "NO_DATA"
        payload = {"condition": condition, "sc_records": out[:limit]}
        if normalized_symbol:
            payload["normalized_symbol"] = normalized_symbol
        return Evidence(
            status=status,
            source="Human Protein Atlas (search_download)",
            fetched_n=len(out),
            data=payload,
            citations=[hpa_url],
            fetched_at=_now(),
        )
    except Exception as e:
        payload = {"condition": condition, "sc_records": []}
        if normalized_symbol:
            payload["normalized_symbol"] = normalized_symbol
        return Evidence(
            status="NO_DATA",
            source=f"HPA search empty/unavailable: {e}",
            fetched_n=0,
            data=payload,
            citations=[hpa_url],
            fetched_at=_now(),
        )



@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(
    condition: str,
    symbol: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    """CRISPR perturbation evidence.
    Order: BioGRID ORCS (if ORCS_ACCESS_KEY) â DepMap CSV URL (DEPMAP_GENE_EFFECT_CSV_URL) â OpenTargets essentiality (GraphQL).
    """
    validate_condition(condition, field_name="condition")

    results: List[Any] = []
    cites: List[str] = []

    # 1) BioGRID ORCS (requires key)
    access_key = os.getenv("ORCS_ACCESS_KEY")
    if access_key:
        base = "https://orcsws.thebiogrid.org/screens/"
        list_url = (
            f"{base}?accesskey={urllib.parse.quote(access_key)}&format=json"
            f"&organismID=9606&conditionName={urllib.parse.quote(condition)}&start=0&max=1000"
        )
        try:
            js = await _get_json(list_url, tries=1)
            cites.append(list_url)
            items = js if isinstance(js, list) else js.get("data") or []
            for it in items or []:
                results.append(it)
        except Exception:
            pass

    # 2) DepMap CSV (optional) â expects a matrix with gene_effect columns keyed by gene
    depmap_csv = os.getenv("DEPMAP_GENE_EFFECT_CSV_URL")
    if depmap_csv and symbol:
        try:
            # fetch small head to avoid huge download; many servers support Range but httpx hides it; we just try full with budget
            csv_txt = await _get_text(depmap_csv, tries=1)
            cites.append(depmap_csv)
            # naive filter: keep rows mentioning symbol in first 200 chars
            for line in (csv_txt or "").splitlines()[:5000]:
                if symbol.upper() in line.upper():
                    results.append({"depmap_row": line[:5000]})
        except Exception:
            pass

    # 3) OpenTargets essentiality (GraphQL) â if symbol provided
    if symbol:
        gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
        q = {
            "query": """
            query Essentiality($symbol: String!) {
              target(approvedSymbol: $symbol) {
                essentiality {
                  # shape depends on API; return raw blob
                  rows { cellLineName score source }
                }
              }
            }""",
            "variables": {"symbol": symbol},
        }
        try:
            ej = await _post_json(gql_url, q, tries=1)
            cites.append(gql_url)
            rows = (
                ej.get("data", {})
                  .get("target", {})
                  .get("essentiality", {})
                  .get("rows", [])
            )
            for r in rows or []:
                results.append({"essentiality": r})
        except Exception:
            pass

    status = "OK" if results else "NO_DATA"
    return Evidence(
        status=status,
        source="ORCS + DepMap (opt) + OpenTargets",
        fetched_n=len(results),
        data={"condition": condition, "screens": results[:limit]},
        citations=cites,
        fetched_at=_now(),
    )
@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(
    symbol: Optional[str] = Query(None),
    gene: Optional[str] = Query(None),  # accept both ?symbol= and ?gene=
    limit: int = Query(50, ge=1, le=500),
) -> Evidence:
    """Baseline expression via HPA â UniProt â Expression Atlas.
    Accepts both 'symbol' and 'gene' query parameters; normalizes aliases (e.g., CB1âCNR1).
    """
    sym_in = symbol or gene
    validate_symbol(sym_in, field_name="symbol")
    sym_norm = await _normalize_symbol(sym_in)

    # 1) HPA
    hpa_url = (
        "https://www.proteinatlas.org/api/search_download.php"
        f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(sym_norm)}"
    )
    try:
        js = await _get_json(hpa_url, tries=1)
        records = js if isinstance(js, list) else []
        if records:
            return Evidence(
                status="OK",
                source="Human Protein Atlas search_download",
                fetched_n=len(records),
                data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": records[:limit]},
                citations=[hpa_url],
                fetched_at=_now(),
            )
    except Exception:
        pass

    # 2) UniProt (less specific, but quick presence / basic info)
    uniprot_url = (
        "https://rest.uniprot.org/uniprotkb/search?"
        f"query={urllib.parse.quote(sym_norm)}&format=json&size={limit}"
    )
    try:
        body = await _get_json(uniprot_url, tries=1)
        entries = body.get("results", []) if isinstance(body, dict) else []
        if entries:
            return Evidence(
                status="OK",
                source="UniProt search",
                fetched_n=len(entries),
                data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": entries[:limit]},
                citations=[uniprot_url],
                fetched_at=_now(),
            )
    except Exception:
        pass

    # 3) Expression Atlas
    atlas_url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(sym_norm)}.json"
    try:
        body = await _get_json(atlas_url, tries=1)
        results: List[Dict[str, Any]] = []
        if isinstance(body, dict):
            for exp in body.get("experiments", []) or []:
                for d in exp.get("data", []):
                    results.append({"tissue": d.get("organismPart") or d.get("tissue") or "NA", "level": d.get("expressions", [{}])[0].get("value")})
        return Evidence(
            status="OK" if results else "NO_DATA",
            source="Expression Atlas (baseline)",
            fetched_n=len(results),
            data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": results[:limit]},
            citations=[atlas_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="NO_DATA",
            source=f"GXA empty/unavailable: {e}",
            fetched_n=0,
            data={"symbol": sym_in, "normalized_symbol": sym_norm, "baseline": []},
            citations=[hpa_url, uniprot_url, atlas_url],
            fetched_at=_now(),
        )


@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(
    symbol: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    url = f"https://compartments.jensenlab.org/Service?gene_names={urllib.parse.quote(sym_norm)}&format=json"
    uni = (
        "https://rest.uniprot.org/uniprotkb/search?"
        f"query=gene_exact:{urllib.parse.quote(sym_norm)}+AND+organism_id:9606"
        "&fields=cc_subcellular_location&format=json&size=1"
    )
    try:
        js = await _get_json(url, tries=1)
        locs = js.get(sym_norm, []) if isinstance(js, dict) else []
        if locs:
            return Evidence(
                status="OK",
                source="COMPARTMENTS API",
                fetched_n=len(locs),
                data={"symbol": symbol, "normalized_symbol": sym_norm, "localization": locs[:limit]},
                citations=[url],
                fetched_at=_now(),
            )
    except Exception:
        pass

    # Fallback â UniProt subcellular location (runs if COMPARTMENTS failed or returned no data)
    try:
        uj = await _get_json(uni, tries=1)
        locs = []
        for r in (uj.get("results", []) or []):
            for c in (r.get("comments", []) or []):
                if c.get("commentType") == "SUBCELLULAR LOCATION":
                    locs.append(c)
        return Evidence(
            status="OK" if locs else "NO_DATA",
            source="UniProt (fallback)",
            fetched_n=len(locs),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "localization": locs[:limit]},
            citations=[url, uni],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="COMPARTMENTS+UniProt unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "localization": []},
            citations=[url, uni],
            fetched_at=_now(),
        )


@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(
    symbol: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=gds&term={urllib.parse.quote(sym_norm)}%5Bgene%5D&retmode=json"
    )
    try:
        js = await _get_json(url, tries=1)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(
            status="OK" if ids else "NO_DATA",
            source="GEO E-utilities",
            fetched_n=len(ids),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "datasets": ids[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="GEO empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "datasets": []},
            citations=[url],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 4 â Mechanistic Wiring & Networks
# -----------------------------------------------------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(
    symbol: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    """
    Pathway membership across multiple providers:
      - Reactome ContentService (search by symbol)
      - KEGG REST (maps Entrez â hsa pathway)
      - WikiPathways REST (text search)
    Returns merged unique pathway list with provider tags.
    """
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

    pathways: List[Dict[str, Any]] = []
    cites: List[str] = []

    # 1) Reactome (existing)
    reactome_search = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(sym_norm)}&species=Homo%20sapiens"
    try:
        js = await _get_json(reactome_search, tries=1)
        hits = js.get("results", []) if isinstance(js, dict) else []
        for h in hits:
            if (h.get("stId", "") or "").startswith("R-HSA"):
                pathways.append({"name": h.get("name"), "id": h.get("stId"), "provider": "Reactome", "score": h.get("score")})
        cites.append(reactome_search)
    except Exception:
        pass

    # Helper to ensure uniqueness by (provider,id,name)
    def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        out = []
        for it in items:
            key = (it.get("provider"), it.get("id"), it.get("name"))
            if key not in seen:
                seen.add(key); out.append(it)
        return out

    # 2) KEGG: need Entrez gene id â link to pathways
    kegg_items: List[Dict[str, Any]] = []
    try:
        # Resolve Entrez Gene ID via MyGene.info
        mg = f"https://mygene.info/v3/query?q=symbol:{urllib.parse.quote(sym_norm)}&species=human&fields=entrezgene"
        mg_js = await _get_json(mg, tries=1)
        cites.append(mg)
        entrez = None
        if isinstance(mg_js, dict):
            hits = mg_js.get("hits", [])
            if hits:
                entrez = hits[0].get("entrezgene")
        if entrez:
            # link gene â pathways
            link_url = f"http://rest.kegg.jp/link/pathway/hsa:{entrez}"
            link_txt = await _get_text(link_url, tries=1)  # we'll add _get_text helper shortly if missing
            cites.append(link_url)
            pids = []
            for line in (link_txt or "").splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1].startswith("path:"):
                    pids.append(parts[1].split(":")[1])
            for pid in pids[:limit]:
                list_url = f"http://rest.kegg.jp/list/{pid}"
                lst = await _get_text(list_url, tries=1)
                cites.append(list_url)
                if lst:
                    # format: path:hsaXXXXX	Name - Homo sapiens (human)
                    nm = lst.split("\t", 1)[1].strip() if "\t" in lst else lst.strip()
                    kegg_items.append({"name": nm, "id": pid, "provider": "KEGG"})
    except Exception:
        pass
    pathways.extend(kegg_items)

    # 3) WikiPathways text search
    try:
        wp = f"https://webservice.wikipathways.org/findPathwaysByText?query={urllib.parse.quote(sym_norm)}&species=Homo%20sapiens&format=json"
        wp_js = await _get_json(wp, tries=1)
        cites.append(wp)
        hits = (wp_js.get("result") or []) if isinstance(wp_js, dict) else []
        for h in hits:
            pathways.append({"name": h.get("name"), "id": h.get("id"), "provider": "WikiPathways"})
    except Exception:
        pass

    merged = _dedupe(pathways)
    return Evidence(
        status="OK" if merged else "NO_DATA",
        source="Reactome + KEGG + WikiPathways",
        fetched_n=len(merged),
        data={"symbol": symbol, "normalized_symbol": sym_norm, "pathways": merged[:limit]},
        citations=cites,
        fetched_at=_now(),
    )
@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(
    symbol: str,
    species: int = Query(9606, ge=1),
    cutoff: float = Query(0.9, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

    map_url = "https://string-db.org/api/json/get_string_ids?identifiers={id}&species={sp}".format(
        id=urllib.parse.quote(sym_norm), sp=int(species)
    )
    net_tpl = "https://string-db.org/api/json/network?identifiers={id}&species={sp}"
    try:
        ids = await _get_json(map_url, tries=1)
        if not ids:
            return Evidence(
                status="NO_DATA",
                source="STRING id lookup empty",
                fetched_n=0,
                data={"symbol": symbol, "normalized_symbol": sym_norm, "neighbors": []},
                citations=[map_url],
                fetched_at=_now(),
            )
        string_id = ids[0].get("stringId")
        net = await _get_json(net_tpl.format(id=string_id, sp=int(species)), tries=1)
        neighbors: List[Dict[str, Any]] = []
        for edge in net:
            score = edge.get("score") or edge.get("combined_score")
            if score and float(score) >= cutoff:
                neighbors.append(
                    {
                        "preferredName_A": edge.get("preferredName_A"),
                        "preferredName_B": edge.get("preferredName_B"),
                        "score": float(score),
                    }
                )
        neighbors = neighbors[:limit]
        return Evidence(
            status="OK" if neighbors else "NO_DATA",
            source="STRING REST",
            fetched_n=len(neighbors),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "species": species, "neighbors": neighbors},
            citations=[map_url, net_tpl.format(id=string_id, sp=int(species))],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="STRING empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "species": species, "neighbors": []},
            citations=[map_url],
            fetched_at=_now(),
        )


@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    url = "https://omnipathdb.org/interactions?format=json&genes={gene}&substrate_only=false"
    try:
        js = await _get_json(url.format(gene=urllib.parse.quote(sym_norm)), tries=1)
        interactions = js if isinstance(js, list) else []
        filtered = [i for i in interactions if sym_norm in (i.get("source", ""), i.get("target", ""))]
        return Evidence(
            status="OK" if filtered else "NO_DATA",
            source="OmniPath interactions",
            fetched_n=len(filtered),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "interactions": filtered[:limit]},
            citations=[url.format(gene=urllib.parse.quote(sym_norm))],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="OmniPath empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "interactions": []},
            citations=[url.format(gene=urllib.parse.quote(sym_norm))],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 5 â Tractability & Modality
# -----------------------------------------------------------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    """Known/experimental drugs for a target.
    Order: OpenTargets knownDrugs â DGIdb â ChEMBL (mechanisms/indications) â DrugBank (if DRUGBANK_API_KEY).
    """
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

    sources: List[str] = []
    interactions: List[Any] = []

    # 1) OpenTargets knownDrugs (GraphQL)
    gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
        query KnownDrugs($symbol: String!) {
          target(approvedSymbol: $symbol) {
            knownDrugs { rows { drugId drugName mechanismOfAction } count }
          }
        }""",
        "variables": {"symbol": sym_norm},
    }
    try:
        res = await _post_json(gql_url, query, tries=1)
        rows = (res.get("data", {}).get("target", {}).get("knownDrugs", {}).get("rows", []))
        if rows:
            sources.append("OpenTargets")
            interactions.extend(rows)
    except Exception:
        pass

    # 2) DGIdb fallback
    try:
        dg_url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(sym_norm)}"
        body = await _get_json(dg_url, tries=1)
        matched = body.get("matchedTerms", []) if isinstance(body, dict) else []
        for term in matched or []:
            interactions.extend(term.get("interactions", []))
        sources.append("DGIdb")
    except Exception:
        pass

    # 3) ChEMBL enrichment (target â mechanisms â molecules)
    try:
        chembl_search = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(sym_norm)}&format=json"
        tjs = await _get_json(chembl_search, tries=1)
        tids = []
        for t in (tjs.get("targets", []) if isinstance(tjs, dict) else []):
            tid = t.get("target_chembl_id")
            if tid: tids.append(tid)
        cites_chembl = [chembl_search]
        mech_rows = []
        for tid in tids[:2]:  # cap to avoid explosion
            mech_url = f"https://www.ebi.ac.uk/chembl/api/data/mechanism.json?target_chembl_id={urllib.parse.quote(tid)}"
            mjs = await _get_json(mech_url, tries=1)
            cites_chembl.append(mech_url)
            for m in (mjs.get("mechanisms", []) if isinstance(mjs, dict) else []):
                mech_rows.append(m)
        if mech_rows:
            sources.append("ChEMBL")
            # Normalize to interaction-like rows
            for m in mech_rows:
                interactions.append({
                    "drugId": m.get("molecule_chembl_id"),
                    "drugName": m.get("molecule_pref_name"),
                    "mechanismOfAction": m.get("mechanism_of_action"),
                })
    except Exception:
        pass

    # 4) DrugBank (optional; paid) minimal enrichment by target gene symbol
    db_key = os.getenv("DRUGBANK_API_KEY") or os.getenv("DRUGBANK_KEY")
    if db_key:
        try:
            # DrugBank public API v1 has limited endpoints; for paid use, users can route via their proxy.
            # Here we try the 'targets' search if available on their deployment.
            db_url = f"https://api.drugbank.com/v1/us/targets/{urllib.parse.quote(sym_norm)}?api_key={urllib.parse.quote(db_key)}"
            db = await _get_json(db_url, tries=1)
            # shape varies; just attach raw
            interactions.append({"drugbank": db})
            sources.append("DrugBank")
        except Exception:
            pass

    status = "OK" if interactions else "NO_DATA"
    cites = [gql_url, "https://dgidb.org/api", "https://www.ebi.ac.uk/chembl/api/data/"] + (["https://api.drugbank.com/"] if db_key else [])
    return Evidence(
        status=status,
        source=" + ".join(sources) if sources else "OpenTargets+DGIdb+ChEMBL(+DrugBank opt)",
        fetched_n=len(interactions),
        data={"symbol": symbol, "normalized_symbol": sym_norm, "interactions": interactions[:limit]},
        citations=cites,
        fetched_at=_now(),
    )
@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(sym_norm)}&format=json"
    try:
        js = await _get_json(url, tries=1)
        targets = js.get("targets", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK" if targets else "NO_DATA",
            source="ChEMBL target search",
            fetched_n=len(targets),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "targets": targets[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="ChEMBL empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "targets": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(
    symbol: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(sym_norm)}"
    try:
        js = await _get_json(url, tries=1)
        entries: List[Any] = []
        if isinstance(js, dict):
            for _, vals in js.items():
                if isinstance(vals, list):
                    entries.extend(vals)
        return Evidence(
            status="OK" if entries else "NO_DATA",
            source="PDBe proteins API",
            fetched_n=len(entries),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "structures": entries[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="PDBe empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "structures": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
) -> Evidence:
    """Oligonucleotide ligandability via Ribocentre Aptamer API."""
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    api_url = f"https://aptamer.ribocentre.org/api/?search={urllib.parse.quote(sym_norm)}"
    try:
        js = await _get_json(api_url, tries=1)
        items = js.get("results") or js.get("items") or js.get("data") or js.get("entries") or []
        out = []
        for it in items:
            ligand = (it.get("Ligand") or it.get("Target") or it.get("ligand") or "")
            if isinstance(ligand, str) and sym_norm.lower() in ligand.lower():
                out.append(
                    {
                        "id": it.get("id") or it.get("Name") or it.get("Sequence Name") or it.get("name"),
                        "ligand": ligand,
                        "sequence": it.get("Sequence") or it.get("sequence"),
                        "kd": it.get("Affinity (Kd)") or it.get("Kd") or it.get("affinity"),
                        "year": it.get("Discovery Year") or it.get("year"),
                        "ref": it.get("Article name") or it.get("reference"),
                    }
                )
        return Evidence(
            status="OK" if out else "NO_DATA",
            source="Ribocentre Aptamer API",
            fetched_n=len(out),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "aptamers": out[:limit]},
            citations=[api_url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="Aptamer API empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "aptamers": []},
            citations=[api_url],
            fetched_at=_now(),
        )


@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(
    symbol: str,
    # Optional priors (rough class success priors). Override if you want.
    prior_sm: float = Query(0.55, ge=0.0, le=1.0),
    prior_ab: float = Query(0.50, ge=0.0, le=1.0),
    prior_oligo: float = Query(0.35, ge=0.0, le=1.0),
    # Optional user overrides for the final scores; if provided, they replace computed scores.
    w_sm: Optional[float] = Query(None, ge=0.0, le=1.0),
    w_ab: Optional[float] = Query(None, ge=0.0, le=1.0),
    w_oligo: Optional[float] = Query(None, ge=0.0, le=1.0),
) -> Evidence:
    """
    Enhanced heuristic modality assessment using COMPARTMENTS + ChEMBL + PDBe + UniProt + IEDB.

    Additions over the baseline:
    * Optional priors (success-rate-inspired).
    * Extracellular domain length estimate from UniProt topology.
    * Immunogenicity signal from IEDB (epitopes / T-cell assays).
    * User can override the resulting per-modality scores with w_* query params.
    """
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

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
            return js.get("targets", []) if isinstance(js, dict) else []
        except Exception:
            return []

    async def _get_pdbe():
        try:
            js = await _get_json(pdbe_url, tries=1)
            entries: List[Any] = []
            if isinstance(js, dict):
                for _, vals in js.items():
                    if isinstance(vals, list):
                        entries.extend(vals)
            return entries
        except Exception:
            return []

    # Fetch base evidence concurrently
    compres, chemblres, pdberes = await asyncio.gather(_get_comp(), _get_chembl(), _get_pdbe())

    loc_terms = " ".join([str(x) for x in compres]).lower()
    is_extracellular = any(t in loc_terms for t in ["secreted", "extracellular", "extracellular space"])
    is_membrane = any(t in loc_terms for t in ["plasma membrane", "cell membrane", "membrane"])
    in_nucleus_or_cytosol = any(t in loc_terms for t in ["nucleus", "nuclear", "cytosol"])
    has_chembl = len(chemblres) > 0
    has_structure = len(pdberes) > 0

    # Extra features: extracellular domain length and immunogenicity counts
    accession = await _uniprot_primary_accession(sym_norm)
    extracellular_len = await _extracellular_len_from_uniprot(accession) if accession else 0
    epi_n, tc_n = await _iedb_counts(sym_norm, limit=25)
    immunogenicity_signal = min((epi_n + tc_n) / 50.0, 0.20)  # cap contribution at +0.20

    # Scoring (bounded and explainable); start from priors and nudge with evidence
    def clamp(x: float) -> float:
        return max(0.0, min(1.0, round(x, 3)))

    sm_score = prior_sm
    sm_score += 0.15 if has_chembl else 0.0
    sm_score += 0.10 if has_structure else 0.0
    sm_score -= 0.20 if (is_extracellular and not is_membrane) else 0.0

    ab_score = prior_ab
    ab_score += 0.20 if (is_membrane or is_extracellular) else 0.0
    ab_score += 0.10 if has_structure else 0.0
    ab_score += 0.10 if extracellular_len >= 100 else 0.0
    ab_score += immunogenicity_signal  # more epitopes / assays â more amenable for biologics

    oligo_score = prior_oligo
    oligo_score += 0.10 if in_nucleus_or_cytosol else 0.0
    oligo_score += 0.10 if has_structure else 0.0
    oligo_score += 0.05 if has_chembl else 0.0  # weak positive if there's chemical precedent

    # Apply user overrides if provided
    sm_score = clamp(w_sm) if (w_sm is not None) else clamp(sm_score)
    ab_score = clamp(w_ab) if (w_ab is not None) else clamp(ab_score)
    oligo_score = clamp(w_oligo) if (w_oligo is not None) else clamp(oligo_score)

    recommendation = sorted(
        [("small_molecule", sm_score), ("antibody", ab_score), ("oligo", oligo_score)],
        key=lambda kv: kv[1],
        reverse=True,
    )

    return Evidence(
        status="OK",
        source="Enhanced modality scorer (COMPARTMENTS + ChEMBL + PDBe + UniProt + IEDB)",
        fetched_n=len(recommendation),
        data={
            "symbol": symbol,
            "normalized_symbol": sym_norm,
            "recommendation": recommendation,
            "rationale": {
                "priors": {"sm": prior_sm, "ab": prior_ab, "oligo": prior_oligo},
                "is_extracellular": is_extracellular,
                "is_membrane": is_membrane,
                "nucleus_or_cytosol": in_nucleus_or_cytosol,
                "chembl_targets_n": len(chemblres),
                "pdbe_structures_n": len(pdberes),
                "uniprot_accession": accession,
                "extracellular_aa": extracellular_len,
                "iedb_epitopes_n": epi_n,
                "iedb_tcell_assays_n": tc_n,
                "user_overrides": {"w_sm": w_sm, "w_ab": w_ab, "w_oligo": w_oligo},
            },
            "snippets": {
                "compartments": compres[:25],
                "chembl_targets": chemblres[:10],
                "pdbe_entries": pdberes[:10],
            },
        },
        citations=[comp_url, chembl_url, pdbe_url],
        fetched_at=_now(),
    )


@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(
    symbol: str,
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    """Immunogenicity via IEDB IQâAPI (epitope_search + tcell_search)."""
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(sym_norm)}%7D&limit={limit}"
    tc_url = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(sym_norm)}%7D&limit={limit}"

    async def _epi():
        try:
            e = await _get_json(epi_url, tries=1)
            return e if isinstance(e, list) else []
        except Exception:
            return []

    async def _tc():
        try:
            t = await _get_json(tc_url, tries=1)
            return t if isinstance(t, list) else []
        except Exception:
            return []

    epi_list, tc_list = await asyncio.gather(_epi(), _tc())

    hla_counts: Dict[str, int] = {}
    for r in tc_list:
        allele = r.get("mhc_allele_name") or r.get("assay_mhc_allele_name") or r.get("mhc_name")
        if allele:
            hla_counts[allele] = hla_counts.get(allele, 0) + 1

    total = len(epi_list) + len(tc_list)
    status = "OK" if total > 0 else "NO_DATA"

    return Evidence(
        status=status,
        source="IEDB IQâAPI",
        fetched_n=total,
        data={
            "symbol": symbol,
            "normalized_symbol": sym_norm,
            "epitopes_n": len(epi_list),
            "tcell_assays_n": len(tc_list),
            "hla_breakdown": sorted([[k, v] for k, v in hla_counts.items()], key=lambda kv: kv[1], reverse=True)[:25],
            "examples": {"epitopes": epi_list[: min(10, limit)], "tcell_assays": tc_list[: min(10, limit)]},
        },
        citations=[epi_url, tc_url],
        fetched_at=_now(),
    )


# -----------------------------------------------------------------------------
# BUCKET 6 â Clinical Translation & Safety
# -----------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(
    condition: str,
    limit: int = Query(3, ge=1, le=100),
) -> Evidence:
    validate_condition(condition, field_name="condition")
    base = "https://clinicaltrials.gov/api/v2/studies"
    q = f"{base}?query.cond={urllib.parse.quote(condition)}&pageSize={limit}"
    # WHO ICTRP fallback
    who_url = f"https://trialsearch.who.int/api/Trial?query={urllib.parse.quote(condition)}"
    studies = []
    cites = []
    try:
        js = await _get_json(q, tries=1)
        cites.append(q)
        studies = js.get("studies", []) if isinstance(js, dict) else []
    except Exception:
        pass

    if not studies:
        try:
            wj = await _get_json(who_url, tries=1)
            cites.append(who_url)
            # ICTRP returns list of trial dicts; shape varies; just slice
            studies = (wj if isinstance(wj, list) else wj.get("Trials", [])) or []
        except Exception:
            pass

    return Evidence(
        status="OK" if studies else "NO_DATA",
        source="ClinicalTrials.gov v2 or WHO ICTRP",
        fetched_n=len(studies),
        data={"condition": condition, "studies": studies[:limit]},
        citations=cites,
        fetched_at=_now(),
    )

@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(
    condition: str,
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Evidence:
    """
    RWE proxies: FAERS (openFDA) + ClinicalTrials.gov.
    Enhancement: if a PCSK9âaxis symbol is provided (e.g., PCSK9), also query brand/generic mAbs in FAERS.
    """
    validate_condition(condition, field_name="condition")
    faers_base = "https://api.fda.gov/drug/event.json"
    ct_url = ("https://clinicaltrials.gov/api/v2/studies"
              f"?query.cond={urllib.parse.quote(condition)}&pageSize={min(100, limit)}")

    # Primary FAERS by condition
    faers_url = f"{faers_base}?search=patient.reaction.reactionmeddrapt.exact:{urllib.parse.quote(condition)}&limit={min(100, limit)}"

    # If symbol hints PCSK9 program, also query alirocumab/evolocumab directly
    faers_drug_urls: List[str] = []
    if (symbol or "").upper() in {"PCSK9", "LDLR", "LDLRAP1"} or "cholesterol" in condition.lower():
        for drug in ["alirocumab", "evolocumab"]:
            faers_drug_urls.append(f"{faers_base}?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug)}&limit={min(100, limit)}")

    async def _get(url):
        try:
            j = await _get_json(url, tries=1)
            return j.get("results", []) if isinstance(j, dict) else []
        except Exception:
            return []

    tasks = [_get(faers_url)] + [_get(u) for u in faers_drug_urls]

    async def _ct():
        try:
            c = await _get_json(ct_url, tries=1)
            return c.get("studies", []) if isinstance(c, dict) else []
        except Exception:
            return []

    faers_lists, trials = await asyncio.gather(asyncio.gather(*tasks), _ct())
    faers_flat = list(itertools.chain.from_iterable(faers_lists))
    total = len(faers_flat) + len(trials)
    cites = [faers_url] + faers_drug_urls + [ct_url]
    return Evidence(
        status="OK" if total > 0 else "NO_DATA",
        source="openFDA FAERS (+drug names) + ClinicalTrials.gov v2",
        fetched_n=total,
        data={"condition": condition, "faers_events": faers_flat[:limit], "observational_trials": trials[:limit]},
        citations=cites,
        fetched_at=_now(),
    )
@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(
    symbol: str,
    limit: int = Query(50, ge=1, le=500),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

    url = (
        "https://api.fda.gov/drug/event.json?"
        f"search=patient.drug.openfda.generic_name:{urllib.parse.quote(sym_norm)}&limit={limit}"
    )
    try:
        js = await _get_json(url, tries=1)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK" if results else "NO_DATA",
            source="openFDA FAERS",
            fetched_n=len(results),
            data={"symbol": symbol, "normalized_symbol": sym_norm, "reports": results[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="FAERS empty/unavailable",
            fetched_n=0,
            data={"symbol": symbol, "normalized_symbol": sym_norm, "reports": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(
    symbol: str,
    limit: int = Query(50, ge=1, le=500),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    inx_url = f"https://drugs.ncats.io/api/v1/drugs?name={urllib.parse.quote(sym_norm)}"
    try:
        js = await _get_json(inx_url, tries=1)
        items = js.get("content", []) if isinstance(js, dict) else []
        if items:
            return Evidence(
                status="OK",
                source="Inxight Drugs",
                fetched_n=len(items),
                data={"symbol": symbol, "normalized_symbol": sym_norm, "pipeline": items[:limit]},
                citations=[inx_url],
                fetched_at=_now(),
            )
    except Exception:
        pass

    result = await _safe_call(tract_drugs(symbol, limit=limit))
    return Evidence(
        status=result.status if result.status in ("OK", "NO_DATA") else "NO_DATA",
        source=result.source,
        fetched_n=result.fetched_n,
        data={"symbol": symbol, "pipeline": result.data.get("interactions", [])[:limit]},
        citations=result.citations,
        fetched_at=result.fetched_at,
    )


# -----------------------------------------------------------------------------
# BUCKET 7 â Competition & IP
# -----------------------------------------------------------------------------




@router.get("/clin/biomarker-fit", response_model=Evidence)
async def clin_biomarker_fit(
    symbol: str,
    condition: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> Evidence:
    """
    Biomarker fit summary for a target:
      - Detectability & compartment: HPA (plasma/secretome) + subcellular location
      - UniProt annotations: 'Biomarker' keyword, disease involvement, secretion
      - Trials where the marker is an endpoint (ClinicalTrials.gov v2; WHO ICTRP fallback)
    Returns a pragmatic score (0â1) and supporting evidence.
    """
    validate_symbol(symbol, field_name="symbol")
    sym = await _normalize_symbol(symbol)

    cites: List[str] = []
    payload: Dict[str, Any] = {"symbol": symbol, "normalized_symbol": sym}

    # 1) HPA: detectability and secretome/plasma hints
    hpa = ("https://www.proteinatlas.org/api/search_download.php"
           f"?format=json&columns=gene,secretome,subcell_location,plasma_protein,rna_tissue,rna_gtex&search={urllib.parse.quote(sym)}")
    hpa_rows: List[Dict[str, Any]] = []
    try:
        hj = await _get_json(hpa, tries=1)
        cites.append(hpa)
        hpa_rows = hj if isinstance(hj, list) else []
    except Exception:
        pass
    payload["hpa"] = hpa_rows[:limit]

    # 2) UniProtKB: annotation scan
    uni = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+organism_id:9606&fields=accession,protein_name,keyword,cc_disease,cc_subcellular_location")
    unij = None
    try:
        unij = await _get_json(uni, tries=1)
        cites.append(uni)
    except Exception:
        pass
    payload["uniprot"] = unij

    # 3) Trials mentioning the symbol as endpoint/biomarker
    trials: List[Any] = []
    ct1 = ("https://clinicaltrials.gov/api/v2/studies"
           f"?query.term={urllib.parse.quote(sym)}&pageSize={min(100,limit)}")
    try:
        ctj = await _get_json(ct1, tries=1)
        cites.append(ct1)
        trials = ctj.get("studies", []) if isinstance(ctj, dict) else []
    except Exception:
        pass
    if not trials:
        who = f"https://trialsearch.who.int/api/Trial?query={urllib.parse.quote(sym)}"
        try:
            wt = await _get_json(who, tries=1)
            cites.append(who)
            trials = (wt if isinstance(wt, list) else wt.get("Trials", [])) or []
        except Exception:
            pass
    payload["trials"] = trials[:limit]

    # Scoring: simple heuristic
    detectable = any(r.get("plasma_protein") or r.get("secretome") for r in (hpa_rows or []))
    uniprot_flags = False
    try:
        # search for keyword 'Biomarker' or subcellular location terms
        txt = json.dumps(unij) if unij else ""
        uniprot_flags = ("Biomarker" in txt) or ("Secreted" in txt) or ("Extracellular" in txt)
    except Exception:
        pass
    trials_signal = len(trials) > 0

    score = (0.4 if detectable else 0.0) + (0.3 if uniprot_flags else 0.0) + (0.3 if trials_signal else 0.0)

    return Evidence(
        status="OK" if (hpa_rows or unij or trials) else "NO_DATA",
        source="HPA + UniProt + ClinicalTrials/ICTRP",
        fetched_n=(len(hpa_rows) + (1 if unij else 0) + len(trials)),
        data={"symbol": symbol, "normalized_symbol": sym, "score": round(score, 2), **payload},
        citations=cites,
        fetched_at=_now(),
    )


@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(
    symbol: str,
    condition: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)
    cond = condition or ""

    citations: List[str] = []
    results: List[Any] = []

    # 0) Lens.org patents (optional, if token available)
    lens_token = os.getenv("LENS_API_TOKEN")
    if lens_token:
        try:
            url = "https://api.lens.org/patent/search"
            payload = {
                "query": {
                    "bool": {
                        "must": [{"terms": {"title": [sym_norm]}},],
                        "should": [{"terms": {"abstract": [cond]}}] if cond else [],
                    }
                },
                "size": min(1000, limit),
                "include": ["lens_id"],
            }
            headers = {"Authorization": f"Bearer {lens_token}"}
            js = await _post_json(url, payload, tries=1, headers=headers)
            citations.append(url)
            hits = js.get("data", []) or js.get("results", []) or []
            for h in hits:
                results.append({"lens_id": h.get("lens_id")})
        except Exception:
            pass

    # 1) PatentsView (public)
    if not results:
        query = {
            "_and": [
                {"_or": [{"patent_title": {"_text_any": sym_norm}}, {"patent_abstract": {"_text_any": sym_norm}}]},
            ]
        }
        if cond:
            query["_and"].append(
                {"_or": [{"patent_title": {"_text_any": cond}}, {"patent_abstract": {"_text_any": cond}}]}
            )
        query_str = urllib.parse.quote(json.dumps(query))
        fields = urllib.parse.quote(json.dumps(["patent_id"]))
        pat_url = f"https://api.patentsview.org/patents/query?q={query_str}&f={fields}"
        try:
            js = await _get_json(pat_url, tries=1)
            citations.append(pat_url)
            patents = js.get("patents", []) if isinstance(js, dict) else []
            for p in patents:
                results.append({"patent_id": p.get("patent_id")})
        except Exception:
            pass

    # 2) SureChEMBL (optional public)
    if not results:
        try:
            sc = f"https://www.ebi.ac.uk/surechembl/api/search?query={urllib.parse.quote(sym_norm)}"
            sj = await _get_json(sc, tries=1)
            citations.append(sc)
            items = sj.get("results", []) if isinstance(sj, dict) else []
            for it in items:
                results.append({"surechembl_id": it.get("id")})
        except Exception:
            pass

    # Fallback: combine drugs+trials counts
    if not results:
        drug_res = await _safe_call(tract_drugs(symbol, limit=limit))
        trial_res = await _safe_call(clin_endpoints(condition or symbol, limit=limit))
        count = (drug_res.fetched_n if drug_res else 0) + (trial_res.fetched_n if trial_res else 0)
        return Evidence(
            status="OK" if count > 0 else "NO_DATA",
            source="Drugs+Trials fallback",
            fetched_n=count,
            data={
                "symbol": symbol,
                "normalized_symbol": sym_norm,
                "condition": condition,
                "drugs_n": drug_res.fetched_n if drug_res else 0,
                "trials_n": trial_res.fetched_n if trial_res else 0,
            },
            citations=(drug_res.citations if drug_res else []) + (trial_res.citations if trial_res else []),
            fetched_at=_now(),
        )

    return Evidence(
        status="OK",
        source="Lens (opt) + PatentsView + SureChEMBL",
        fetched_n=len(results),
        data={"symbol": symbol, "normalized_symbol": sym_norm, "condition": condition, "patents": results[:limit]},
        citations=citations,
        fetched_at=_now(),
    )

@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(
    symbol: str,
    limit: int = Query(100, ge=1, le=1000),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    sym_norm = await _normalize_symbol(symbol)

    citations: List[str] = []
    results: List[Any] = []

    # Lens.org (optional)
    lens_token = os.getenv("LENS_API_TOKEN")
    if lens_token:
        try:
            url = "https://api.lens.org/patent/search"
            payload = {
                "query": {"bool": {"must": [{"terms": {"title": [sym_norm]}}]}},
                "size": min(1000, limit),
                "include": ["lens_id"],
            }
            headers = {"Authorization": f"Bearer {lens_token}"}
            js = await _post_json(url, payload, tries=1, headers=headers)
            citations.append(url)
            hits = js.get("data", []) or js.get("results", []) or []
            for h in hits:
                results.append({"lens_id": h.get("lens_id")})
        except Exception:
            pass

    # PatentsView
    if not results:
        query = {"_or": [{"patent_title": {"_text_any": sym_norm}}, {"patent_abstract": {"_text_any": sym_norm}}]}
        query_str = urllib.parse.quote(json.dumps(query))
        fields = urllib.parse.quote(json.dumps(["patent_id"]))
        url = f"https://api.patentsview.org/patents/query?q={query_str}&f={fields}"
        try:
            js = await _get_json(url, tries=1)
            citations.append(url)
            patents = js.get("patents", []) if isinstance(js, dict) else []
            for p in patents:
                results.append({"patent_id": p.get("patent_id")})
        except Exception:
            pass

    # SureChEMBL (optional)
    if not results:
        try:
            sc = f"https://www.ebi.ac.uk/surechembl/api/search?query={urllib.parse.quote(sym_norm)}"
            sj = await _get_json(sc, tries=1)
            citations.append(sc)
            items = sj.get("results", []) if isinstance(sj, dict) else []
            for it in items:
                results.append({"surechembl_id": it.get("id")})
        except Exception:
            pass

    return Evidence(
        status="OK" if results else "NO_DATA",
        source="Lens (opt) + PatentsView + SureChEMBL",
        fetched_n=len(results),
        data={"symbol": symbol, "normalized_symbol": sym_norm, "patents": results[:limit]},
        citations=citations,
        fetched_at=_now(),
    )
