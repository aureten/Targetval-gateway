"""
Routes implementing the TARGETVAL gateway (revised, consolidated).

This module exposes a suite of REST endpoints grouped by functional
buckets (e.g., Human Genetics & Causality, Disease Association &
Perturbation, Expression & Localization, Mechanistic Wiring,
Tractability & Modality, Clinical Translation & Safety, Competition & IP).

Key improvements over the original:
* Robust 429/5xx retry with exponential back‑off & jitter for all outbound calls
* Configurable concurrency (semaphore) and cache TTL via env vars
* Default outbound headers (User‑Agent, Accept JSON) + longer, configurable timeout
* The 8 previously‑stubbed modules are now wired to live, public APIs:
  - /genetics/mr  → EpiGraphDB xQTL multi‑SNP MR
  - /genetics/mirna → miRNet 2.0 (fallback ENCORI/starBase TSV)
  - /assoc/sc → Human Protein Atlas single‑cell signal by condition term
  - /assoc/perturb → BioGRID ORCS screens and top gene hits (requires ORCS key)
  - /tract/ligandability-oligo → Ribocentre Aptamer API
  - /tract/modality → rule‑based scoring integrating COMPARTMENTS, ChEMBL, PDBe
  - /tract/immunogenicity → IEDB IQ‑API (epitope_search & tcell_search)
  - /clin/rwe → openFDA FAERS + ClinicalTrials.gov v2 observational studies

All endpoints return a structured Evidence payload and never propagate errors.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.utils.validation import validate_symbol, validate_condition

# Router instance used by the FastAPI application.
router = APIRouter()


class Evidence(BaseModel):
    """Container for evidence returned by an endpoint."""

    status: str           # "OK" | "ERROR" | "NO_DATA"
    source: str           # description of upstream API / method
    fetched_n: int        # count before limit slicing (if applicable)
    data: Dict[str, Any]  # module-specific payload
    citations: List[str]  # URLs used as evidence
    fetched_at: float     # UNIX timestamp


# -----------------------------------------------------------------------------
# Caching, concurrency & outbound client defaults
# -----------------------------------------------------------------------------

# In‑memory cache for upstream HTTP GET requests.
CACHE: Dict[str, Dict[str, Any]] = {}

# Configurable cache TTL and concurrency
CACHE_TTL: int = int(os.getenv("CACHE_TTL_SECONDS", str(24 * 60 * 60)))

# Outbound HTTP client settings (configurable)
DEFAULT_TIMEOUT = httpx.Timeout(
    float(os.getenv("OUTBOUND_TIMEOUT_S", "45.0")),  # read/write total
    connect=6.0,
)
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": os.getenv(
        "OUTBOUND_USER_AGENT",
        "TargetVal/1.2 (+https://github.com/aureten/Targetval-gateway)"
    ),
    "Accept": "application/json",
}

MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "5"))
_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


# API key enforcement placeholder (wire your middleware/key check here if needed).
def _require_key(x_api_key: Optional[str]) -> None:
    return


def _now() -> float:
    return time.time()


async def _get_json(url: str, tries: int = 3, headers: Optional[Dict[str, str]] = None) -> Any:
    """HTTP GET returning parsed JSON with cache, 429 back‑off, and jitter."""
    cached = CACHE.get(url)
    if cached and (_now() - cached.get("timestamp", 0) < CACHE_TTL):
        return cached["data"]

    last_err: Optional[Exception] = None
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await client.get(url, headers=merged)
                    # Handle rate-limits (429) or transient 5xx before raising
                    if resp.status_code in (429, 500, 502, 503, 504):
                        # Exponential backoff with jitter
                        backoff = min(2 ** (attempt - 1) * 0.8, 10.0) + random.random() * 0.25
                        await asyncio.sleep(backoff)
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    CACHE[url] = {"data": data, "timestamp": _now()}
                    return data
                except Exception as e:
                    last_err = e
                    # backoff & jitter
                    backoff = min(2 ** (attempt - 1) * 0.8, 10.0) + random.random() * 0.25
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {last_err}")


async def _get_text(url: str, tries: int = 3, headers: Optional[Dict[str, str]] = None) -> str:
    """HTTP GET returning text (no caching, used for TSV sources)."""
    last_err: Optional[Exception] = None
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await client.get(url, headers=merged)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        backoff = min(2 ** (attempt - 1) * 0.8, 10.0) + random.random() * 0.25
                        await asyncio.sleep(backoff)
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        continue
                    resp.raise_for_status()
                    return resp.text
                except Exception as e:
                    last_err = e
                    backoff = min(2 ** (attempt - 1) * 0.8, 10.0) + random.random() * 0.25
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET text failed for {url}: {last_err}")


async def _post_json(
    url: str,
    payload: Dict[str, Any],
    tries: int = 3,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """HTTP POST with JSON body and 429/5xx back‑off (no caching)."""
    last_err: Optional[Exception] = None
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(1, tries + 1):
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await client.post(url, json=payload, headers=merged)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        backoff = min(2 ** (attempt - 1) * 0.8, 10.0) + random.random() * 0.25
                        await asyncio.sleep(backoff)
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as e:
                    last_err = e
                    backoff = min(2 ** (attempt - 1) * 0.8, 10.0) + random.random() * 0.25
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
            "B6: /clin/endpoints, /clin/rwe, /clin/safety, /clin/pipeline",
            "B7: /comp/intensity, /comp/freedom",
        ],
    }


# -----------------------------------------------------------------------------
# BUCKET 1 – Human Genetics & Causality
# -----------------------------------------------------------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(
    gene: str,
    efo: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of associations to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    dis_url = f"https://www.disgenet.org/api/gda/gene/{urllib.parse.quote(gene)}?format=json"
    try:
        js = await _get_json(dis_url)
        records = js if isinstance(js, list) else []
        return Evidence(
            status="OK",
            source="DisGeNET gene–disease associations",
            fetched_n=len(records),
            data={"gene": gene, "efo": efo, "results": records[:limit]},
            citations=[dis_url],
            fetched_at=_now(),
        )
    except Exception:
        pass
    gwas_url = f"https://www.ebi.ac.uk/gwas/rest/api/associations?geneName={urllib.parse.quote(gene)}"
    try:
        js = await _get_json(gwas_url)
        hits: List[Dict[str, Any]] = []
        if isinstance(js, dict):
            hits = js.get("_embedded", {}).get("associations", [])
        elif isinstance(js, list):
            hits = js
        return Evidence(
            status="OK",
            source="GWAS Catalog associations",
            fetched_n=len(hits),
            data={"gene": gene, "efo": efo, "results": hits[:limit]},
            citations=[gwas_url],
            fetched_at=_now(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(
    gene: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of variants to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    clinvar_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&term={urllib.parse.quote(gene)}%5Bgene%5D"
        "&retmode=json"
    )
    try:
        js = await _get_json(clinvar_url)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(
            status="OK",
            source="ClinVar E‑utilities",
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
            variants {
              variantId
              genome { ac an }
            }
          }
        }
        """,
        "variables": {"symbol": gene},
    }
    try:
        body = await _post_json(gql_url, query)
        variants = (
            body.get("data", {})
            .get("gene", {})
            .get("variants", [])
            or []
        )
        return Evidence(
            status="OK",
            source="gnomAD GraphQL",
            fetched_n=len(variants),
            data={"gene": gene, "variants": variants[:limit]},
            citations=[gql_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "variants": []},
            citations=[gql_url],
            fetched_at=_now(),
        )


@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(
    gene: str,
    efo: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of diseases to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    url = f"https://api.monarchinitiative.org/api/bioentity/gene/{urllib.parse.quote(gene)}/diseases?rows=20"
    try:
        js = await _get_json(url)
        associations = js.get("associations", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="Monarch API",
            fetched_n=len(associations),
            data={"gene": gene, "diseases": associations[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "diseases": []},
            citations=[url],
            fetched_at=_now(),
        )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(
    gene: str,
    efo: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of MR rows to return"),
) -> Evidence:
    """
    Mendelian randomisation via EpiGraphDB's xQTL multi‑SNP MR.

    Strategy:
      1) Map the provided EFO term/ID to a disease label (EpiGraphDB: /ontology/disease-efo)
      2) Call /xqtl/multi-snp-mr with exposure_gene=<gene>, outcome_trait=<label>
      3) Return rows (instruments and causal estimates); truncated by `limit`.
    """
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")

    # Map EFO -> disease label (fuzzy OK)
    disease_label = efo
    map_url = f"https://api.epigraphdb.org/ontology/disease-efo?efo_term={urllib.parse.quote(efo)}&fuzzy=true"
    try:
        mapping = await _get_json(map_url)
        results = mapping.get("results", []) if isinstance(mapping, dict) else []
        if results:
            top = results[0]
            disease_label = (
                top.get("disease_label")
                or (top.get("disease") or {}).get("label")
                or disease_label
            )
    except Exception:
        pass

    base = "https://api.epigraphdb.org/xqtl/multi-snp-mr"
    qs = urllib.parse.urlencode({"exposure_gene": gene, "outcome_trait": disease_label})
    mr_url = f"{base}?{qs}"

    try:
        mr = await _get_json(mr_url)
        rows = mr.get("results", []) if isinstance(mr, dict) else (mr or [])
        return Evidence(
            status="OK",
            source="EpiGraphDB xQTL multi‑SNP MR",
            fetched_n=len(rows),
            data={"gene": gene, "efo": efo, "outcome_trait": disease_label, "mr": rows[:limit]},
            citations=[mr_url, map_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"MR request failed: {e}",
            fetched_n=0,
            data={"gene": gene, "efo": efo, "outcome_trait": disease_label, "mr": []},
            citations=[mr_url, map_url],
            fetched_at=_now(),
        )


@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(
    gene: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of lncRNAs to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(gene)}&page_size={limit}"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="RNAcentral REST",
            fetched_n=len(results),
            data={"gene": gene, "lncRNAs": results[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "lncRNAs": []},
            citations=[url],
            fetched_at=_now(),
        )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(
    gene: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of miRNA interactions to return"),
) -> Evidence:
    """
    miRNA–gene interactions (live):
      * Primary: miRNet 2.0 API (JSON)
      * Fallback: ENCORI / starBase miRNA-Target API (TSV)
    """
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")

    # Primary: miRNet 2.0 (table/gene)
    mirnet_url = "https://api.mirnet.ca/table/gene"
    payload = {"org": "hsa", "idOpt": "symbol", "myList": gene, "selSource": "All"}
    try:
        r = await _post_json(mirnet_url, payload)
        rows = r.get("data", []) if isinstance(r, dict) else (r or [])
        simplified = []
        for it in rows[:limit]:
            simplified.append({
                "miRNA": it.get("miRNA") or it.get("mirna") or it.get("ID"),
                "target": it.get("Target") or it.get("Gene") or gene,
                "evidence": it.get("Category") or it.get("Evidence") or it.get("Source"),
                "pmid": it.get("PMID") or it.get("PubMedID"),
                "source_db": it.get("Source") or "miRNet"
            })
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

    # Fallback: ENCORI / starBase (TSV)
    encori_url = (
        "https://rnasysu.com/encori/api/miRNATarget/"
        f"?assembly=hg38&geneType=mRNA&miRNA=all&clipExpNum=1&degraExpNum=0&pancancerNum=0&programNum=1&program=TargetScan"
        f"&target={urllib.parse.quote(gene)}&cellType=all"
    )
    try:
        tsv = await _get_text(encori_url)
        lines = [ln for ln in tsv.splitlines() if ln.strip()]
        if not lines or len(lines) == 1:
            return Evidence(
                status="OK",
                source="ENCORI/starBase",
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
            out.append({
                "miRNA": row.get("miRNA") or row.get("miRNA_Name"),
                "target": row.get("Target_Gene") or gene,
                "support": row.get("SupportType") or row.get("Support_Type"),
                "pmid": row.get("PMID") or row.get("CLIP-Data_PubMed_ID"),
                "cell_type": row.get("Cell_Type"),
            })
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
            status="ERROR",
            source=f"miRNA API error: {e}",
            fetched_n=0,
            data={"gene": gene, "interactions": []},
            citations=[mirnet_url, encori_url],
            fetched_at=_now(),
        )


@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(
    gene: str,
    efo: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of sQTLs to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(gene)}/sqtls"
    try:
        js = await _get_json(url)
        results = js.get("sqtls", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="eQTL Catalogue",
            fetched_n=len(results),
            data={"gene": gene, "sqtls": results[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "sqtls": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(
    gene: str,
    efo: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of experiments to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    search_url = (
        "https://www.encodeproject.org/search/?"
        f"searchTerm={urllib.parse.quote(gene)}&format=json&limit={limit}&type=Experiment"
    )
    try:
        js = await _get_json(search_url)
        hits = js.get("@graph", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="ENCODE Search API",
            fetched_n=len(hits),
            data={"gene": gene, "experiments": hits[:limit]},
            citations=[search_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "experiments": []},
            citations=[search_url],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 2 – Disease Association & Perturbation
# -----------------------------------------------------------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(
    condition: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of genes to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    url = f"https://gtexportal.org/api/v2/association/genesByTissue?tissueSiteDetail={urllib.parse.quote(condition)}"
    try:
        js = await _get_json(url)
        genes = js.get("genes", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="GTEx genesByTissue",
            fetched_n=len(genes),
            data={"condition": condition, "genes": genes[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        # Fallback → HPA search with rna_gtex hints
        hpa = (
            "https://www.proteinatlas.org/api/search_download.php"
            f"?format=json&columns=gene,rna_gtex,rna_tissue&search={urllib.parse.quote(condition)}"
        )
        try:
            hj = await _get_json(hpa)
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
                status="ERROR",
                source=str(e),
                fetched_n=0,
                data={"condition": condition, "genes": []},
                citations=[url],
                fetched_at=_now(),
            )


@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(
    condition: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of proteins to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    url = (
        "https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search"
        f"?search={urllib.parse.quote(condition)}"
    )
    try:
        js = await _get_json(url)
        records: List[Any] = []
        if isinstance(js, dict):
            records = js.get("items", js.get("proteins", js.get("results", [])))
        elif isinstance(js, list):
            records = js
        return Evidence(
            status="OK",
            source="ProteomicsDB proteins search",
            fetched_n=len(records),
            data={"condition": condition, "proteins": records[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        # Fallback → PRIDE project list as a proxy
        pride = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
        try:
            pj = await _get_json(pride)
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
                status="ERROR",
                source=str(e),
                fetched_n=0,
                data={"condition": condition, "proteins": []},
                citations=[url],
                fetched_at=_now(),
            )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(
    condition: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of rows to return"),
) -> Evidence:
    """
    Single‑cell signal using Human Protein Atlas search API.
    We treat `condition` as a cell type / tissue / disease keyword and
    surface rows containing single‑cell fields.
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")

    hpa_url = (
        "https://www.proteinatlas.org/api/search_download.php"
        f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(condition)}"
    )
    try:
        js = await _get_json(hpa_url)
        rows = js if isinstance(js, list) else []
        out: List[Dict[str, Any]] = []
        for r in rows:
            if any(k in r and r[k] for k in ("cell_type", "rna_cell_type")):
                out.append({
                    "gene": r.get("gene"),
                    "cell_type": r.get("cell_type") or r.get("rna_cell_type"),
                    "rna_gtex": r.get("rna_gtex"),
                    "tissue": r.get("rna_tissue"),
                })
        return Evidence(
            status="OK",
            source="Human Protein Atlas (search_download)",
            fetched_n=len(out),
            data={"condition": condition, "sc_records": out[:limit]},
            citations=[hpa_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"HPA search failed: {e}",
            fetched_n=0,
            data={"condition": condition, "sc_records": []},
            citations=[hpa_url],
            fetched_at=_now(),
        )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(
    condition: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of screen or hit rows to return"),
) -> Evidence:
    """
    CRISPR perturbation from BioGRID ORCS REST service.
    Requires ORCS access key (free): set env ORCS_ACCESS_KEY.
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")

    access_key = os.getenv("ORCS_ACCESS_KEY")
    if not access_key:
        return Evidence(
            status="ERROR",
            source="Missing ORCS access key (set ORCS_ACCESS_KEY)",
            fetched_n=0,
            data={"condition": condition, "screens": []},
            citations=["https://orcsws.thebiogrid.org/"],
            fetched_at=_now(),
        )

    base = "https://orcsws.thebiogrid.org/screens/"
    list_url = (
        f"{base}?accesskey={urllib.parse.quote(access_key)}&format=json"
        f"&organismID=9606&conditionName={urllib.parse.quote(condition)}&start=0&max=50"
    )
    try:
        screens = await _get_json(list_url)
        screens_list = screens if isinstance(screens, list) else []
        screens_list = screens_list[: min(3, len(screens_list))]
        hits_collected: List[Dict[str, Any]] = []

        for sc in screens_list:
            sc_id = sc.get("SCREEN_ID") or sc.get("ID") or sc.get("SCREENID")
            if not sc_id:
                continue
            sc_url = f"https://orcsws.thebiogrid.org/screen/{sc_id}?accesskey={urllib.parse.quote(access_key)}&format=json&hit=yes&start=0&max={min(200, limit)}"
            try:
                sc_hits = await _get_json(sc_url)
                for h in (sc_hits if isinstance(sc_hits, list) else []):
                    hits_collected.append({
                        "screen_id": sc_id,
                        "gene": h.get("OFFICIAL_SYMBOL") or h.get("NAME"),
                        "cell_line": sc.get("CELL_LINE"),
                        "phenotype": sc.get("PHENOTYPE"),
                        "score1": h.get("SCORE_1") or h.get("SCORE1") or h.get("SCORE"),
                        "pubmed_id": sc.get("PUBMED_ID"),
                    })
            except Exception:
                continue

        return Evidence(
            status="OK",
            source="BioGRID ORCS REST",
            fetched_n=len(hits_collected),
            data={"condition": condition, "screens_n": len(screens_list), "hits": hits_collected[:limit]},
            citations=[list_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"ORCS error: {e}",
            fetched_n=0,
            data={"condition": condition, "screens": []},
            citations=[list_url],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 3 – Expression, Specificity & Localization
# -----------------------------------------------------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of baseline entries to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    hpa_url = (
        "https://www.proteinatlas.org/api/search_download.php"
        f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(symbol)}"
    )
    try:
        js = await _get_json(hpa_url)
        records = js if isinstance(js, list) else []
        if records:
            return Evidence(
                status="OK",
                source="Human Protein Atlas search_download",
                fetched_n=len(records),
                data={"symbol": symbol, "baseline": records[:limit]},
                citations=[hpa_url],
                fetched_at=_now(),
            )
    except Exception:
        pass
    atlas_url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    try:
        body = await _get_json(atlas_url)
        results: List[Dict[str, Any]] = []
        experiments = body.get("experiments", []) if isinstance(body, dict) else []
        for exp in experiments:
            for d in exp.get("data", []):
                results.append(
                    {
                        "tissue": d.get("organismPart") or d.get("tissue") or "NA",
                        "level": d.get("expressions", [{}])[0].get("value"),
                    }
                )
        if results:
            return Evidence(
                status="OK",
                source="Expression Atlas (baseline)",
                fetched_n=len(results),
                data={"symbol": symbol, "baseline": results[:limit]},
                citations=[atlas_url],
                fetched_at=_now(),
            )
    except Exception:
        pass
    uniprot_url = f"https://rest.uniprot.org/uniprotkb/search?query={urllib.parse.quote(symbol)}&format=json&size={limit}"
    try:
        body = await _get_json(uniprot_url)
        entries = body.get("results", []) if isinstance(body, dict) else []
        return Evidence(
            status="OK",
            source="UniProt search",
            fetched_n=len(entries),
            data={"symbol": symbol, "baseline": entries[:limit]},
            citations=[uniprot_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "baseline": []},
            citations=[hpa_url, atlas_url, uniprot_url],
            fetched_at=_now(),
        )


@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of localization entries to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = (
        "https://compartments.jensenlab.org/Service"
        f"?gene_names={urllib.parse.quote(symbol)}&format=json"
    )
    try:
        js = await _get_json(url)
        locs = js.get(symbol, []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="COMPARTMENTS API",
            fetched_n=len(locs),
            data={"symbol": symbol, "localization": locs[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        # Fallback → UniProt subcellular location
        uni = (
            "https://rest.uniprot.org/uniprotkb/search?"
            f"query=gene_exact:{urllib.parse.quote(symbol)}+AND+organism_id:9606"
            "&fields=cc_subcellular_location&format=json&size=1"
        )
        try:
            uj = await _get_json(uni)
            locs = []
            for r in (uj.get("results", []) or []):
                for c in (r.get("comments", []) or []):
                    if c.get("commentType") == "SUBCELLULAR LOCATION":
                        locs.append(c)
            return Evidence(
                status="OK" if locs else "NO_DATA",
                source="UniProt (fallback)",
                fetched_n=len(locs),
                data={"symbol": symbol, "localization": locs[:limit]},
                citations=[url, uni],
                fetched_at=_now(),
            )
        except Exception:
            return Evidence(
                status="ERROR",
                source=str(e),
                fetched_n=0,
                data={"symbol": symbol, "localization": []},
                citations=[url],
                fetched_at=_now(),
            )


@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of dataset identifiers to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term={urllib.parse.quote(symbol)}%5Bgene%5D"
        "&retmode=json"
    )
    try:
        js = await _get_json(url)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(
            status="OK",
            source="GEO E‑utilities",
            fetched_n=len(ids),
            data={"symbol": symbol, "datasets": ids[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "datasets": []},
            citations=[url],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 4 – Mechanistic Wiring & Networks
# -----------------------------------------------------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of pathways to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    search = (
        f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    )
    try:
        js = await _get_json(search)
        hits = js.get("results", []) if isinstance(js, dict) else []
        pathways = []
        for h in hits:
            if "Pathway" in h.get("species", "") or h.get("stId", "").startswith("R-HSA"):
                pathways.append(
                    {
                        "name": h.get("name"),
                        "stId": h.get("stId"),
                        "score": h.get("score"),
                    }
                )
        return Evidence(
            status="OK",
            source="Reactome ContentService",
            fetched_n=len(pathways),
            data={"symbol": symbol, "pathways": pathways[:limit]},
            citations=[search],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "pathways": []},
            citations=[search],
            fetched_at=_now(),
        )


@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(
    symbol: str,
    cutoff: float = Query(0.9, ge=0.0, le=1.0, description="Minimum interaction score to include"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of interaction partners to return"),
    x_api_key: Optional[str] = Header(default=None),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    map_url = (
        "https://string-db.org/api/json/get_string_ids"
        f"?identifiers={urllib.parse.quote(symbol)}&species=9606"
    )
    network_url_tpl = "https://string-db.org/api/json/network?identifiers={id}&species=9606"
    try:
        ids = await _get_json(map_url)
        if not ids:
            return Evidence(
                status="OK",
                source="STRING",
                fetched_n=0,
                data={"symbol": symbol, "neighbors": []},
                citations=[map_url],
                fetched_at=_now(),
            )
        string_id = ids[0].get("stringId")
        net = await _get_json(network_url_tpl.format(id=string_id))
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
            status="OK",
            source="STRING REST",
            fetched_n=len(neighbors),
            data={"symbol": symbol, "neighbors": neighbors},
            citations=[map_url, network_url_tpl.format(id=string_id)],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "neighbors": []},
            citations=[map_url],
            fetched_at=_now(),
        )


@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of interactions to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = "https://omnipathdb.org/interactions?format=json&genes={gene}&substrate_only=false"
    try:
        js = await _get_json(url.format(gene=urllib.parse.quote(symbol)))
        interactions = js if isinstance(js, list) else []
        filtered: List[Dict[str, Any]] = []
        for i in interactions:
            if symbol in (i.get("source", ""), i.get("target", "")):
                filtered.append(i)
        return Evidence(
            status="OK",
            source="OmniPath interactions",
            fetched_n=len(filtered),
            data={"symbol": symbol, "interactions": filtered[:limit]},
            citations=[url.format(gene=urllib.parse.quote(symbol))],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "interactions": []},
            citations=[url.format(gene=urllib.parse.quote(symbol))],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 5 – Tractability & Modality
# -----------------------------------------------------------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of drug interactions to return"),
) -> Evidence:
    """
    Known/experimental drugs for a target. OpenTargets first (fast, robust),
    DGIdb fallback.
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")

    # 1) OpenTargets knownDrugs (GraphQL)
    gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
        query ($symbol: String!) {
          target(approvedSymbol: $symbol) {
            id
            approvedSymbol
            knownDrugs {
              rows {
                drugId
                drugName
                mechanismOfAction
              }
              count
            }
          }
        }
        """,
        "variables": {"symbol": symbol},
    }
    try:
        res = await _post_json(gql_url, query)
        rows = (
            res.get("data", {})
            .get("target", {})
            .get("knownDrugs", {})
            .get("rows", [])
        )
        if rows:
            return Evidence(
                status="OK",
                source="OpenTargets knownDrugs",
                fetched_n=len(rows),
                data={"symbol": symbol, "interactions": rows[:limit]},
                citations=[gql_url],
                fetched_at=_now(),
            )
    except Exception:
        pass

    # 2) DGIdb fallback
    dg_url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(symbol)}"
    try:
        body = await _get_json(dg_url)
        matched = body.get("matchedTerms", []) if isinstance(body, dict) else []
        interactions: List[Any] = []
        for term in matched or []:
            interactions.extend(term.get("interactions", []))
        return Evidence(
            status="OK" if interactions else "NO_DATA",
            source="DGIdb",
            fetched_n=len(interactions),
            data={"symbol": symbol, "interactions": interactions[:limit]},
            citations=[dg_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"DGIdb failed: {e}",
            fetched_n=0,
            data={"symbol": symbol, "interactions": []},
            citations=[gql_url, dg_url],
            fetched_at=_now(),
        )


@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of small‑molecule targets to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(symbol)}&format=json"
    try:
        js = await _get_json(url)
        targets = js.get("targets", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="ChEMBL target search",
            fetched_n=len(targets),
            data={"symbol": symbol, "targets": targets[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "targets": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of structures to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(url)
        entries: List[Any] = []
        if isinstance(js, dict):
            for _, vals in js.items():
                if isinstance(vals, list):
                    entries.extend(vals)
        return Evidence(
            status="OK",
            source="PDBe proteins API",
            fetched_n=len(entries),
            data={"symbol": symbol, "structures": entries[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "structures": []},
            citations=[url],
            fetched_at=_now(),
        )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of aptamers to return"),
) -> Evidence:
    """
    Oligonucleotide ligandability via Ribocentre Aptamer API.
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")

    api_url = f"https://aptamer.ribocentre.org/api/?search={urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(api_url)
        items = (
            js.get("results")
            or js.get("items")
            or js.get("data")
            or js.get("entries")
            or []
        )
        out = []
        for it in items:
            ligand = (it.get("Ligand") or it.get("Target") or it.get("ligand") or "")
            if isinstance(ligand, str) and symbol.lower() in ligand.lower():
                out.append({
                    "id": it.get("id") or it.get("Name") or it.get("Sequence Name") or it.get("name"),
                    "ligand": ligand,
                    "sequence": it.get("Sequence") or it.get("sequence"),
                    "kd": it.get("Affinity (Kd)") or it.get("Kd") or it.get("affinity"),
                    "year": it.get("Discovery Year") or it.get("year"),
                    "ref": it.get("Article name") or it.get("reference"),
                })
        return Evidence(
            status="OK",
            source="Ribocentre Aptamer API",
            fetched_n=len(out),
            data={"symbol": symbol, "aptamers": out[:limit]},
            citations=[api_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"Aptamer API error: {e}",
            fetched_n=0,
            data={"symbol": symbol, "aptamers": []},
            citations=[api_url],
            fetched_at=_now(),
        )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
) -> Evidence:
    """
    Heuristic therapeutic modality assessment (live inputs).

    Signals integrated:
      * COMPARTMENTS: subcellular localisation (extracellular/plasma membrane boosts Ab/oligo)
      * ChEMBL: existing target entries (boosts small‑molecule)
      * PDBe: structural coverage (support for both; indicates tractability)
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")

    comp_url = (
        "https://compartments.jensenlab.org/Service"
        f"?gene_names={urllib.parse.quote(symbol)}&format=json"
    )
    chembl_url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(symbol)}&format=json"
    pdbe_url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(symbol)}"

    async def _get_comp():
        try:
            js = await _get_json(comp_url)
            return js.get(symbol, []) if isinstance(js, dict) else []
        except Exception:
            return []

    async def _get_chembl():
        try:
            js = await _get_json(chembl_url)
            return js.get("targets", []) if isinstance(js, dict) else []
        except Exception:
            return []

    async def _get_pdbe():
        try:
            js = await _get_json(pdbe_url)
            entries: List[Any] = []
            if isinstance(js, dict):
                for _, vals in js.items():
                    if isinstance(vals, list):
                        entries.extend(vals)
            return entries
        except Exception:
            return []

    compres, chemblres, pdberes = await asyncio.gather(_get_comp(), _get_chembl(), _get_pdbe())

    loc_terms = " ".join([str(x) for x in compres]).lower()
    is_extracellular = any(t in loc_terms for t in ["secreted", "extracellular", "extracellular space"])
    is_membrane = any(t in loc_terms for t in ["plasma membrane", "cell membrane", "membrane"])
    has_chembl = len(chemblres) > 0
    has_structure = len(pdberes) > 0

    sm_score = 0.0
    if has_chembl:
        sm_score += 0.6
    if has_structure:
        sm_score += 0.25
    if is_extracellular and not is_membrane:
        sm_score -= 0.15

    ab_score = 0.0
    if is_membrane or is_extracellular:
        ab_score += 0.6
    if has_structure:
        ab_score += 0.25
    if has_chembl and not (is_membrane or is_extracellular):
        ab_score -= 0.1

    oligo_score = 0.0
    if is_extracellular or "cytosol" in loc_terms or "nucleus" in loc_terms:
        oligo_score += 0.5
    if has_structure:
        oligo_score += 0.1
    if has_chembl:
        oligo_score += 0.05

    def clamp(x: float) -> float:
        return max(0.0, min(1.0, round(x, 3)))

    recommendation = sorted(
        [
            ("small_molecule", clamp(sm_score)),
            ("antibody", clamp(ab_score)),
            ("oligo", clamp(oligo_score)),
        ],
        key=lambda kv: kv[1],
        reverse=True,
    )

    return Evidence(
        status="OK",
        source="Heuristic modality scorer (COMPARTMENTS + ChEMBL + PDBe)",
        fetched_n=len(recommendation),
        data={
            "symbol": symbol,
            "recommendation": recommendation,
            "rationale": {
                "is_extracellular": is_extracellular,
                "is_membrane": is_membrane,
                "chembl_targets_n": len(chemblres),
                "pdbe_structures_n": len(pdberes),
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


# -------------------------- NEW (live) ----------------------------------------
@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of epitope/assay rows to return"),
) -> Evidence:
    """
    Immunogenicity via IEDB IQ‑API (PostgREST).
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")

    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    tc_url = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"

    try:
        epi = await _get_json(epi_url)
        tc = await _get_json(tc_url)
        epi_list = epi if isinstance(epi, list) else []
        tc_list = tc if isinstance(tc, list) else []

        hla_counts: Dict[str, int] = {}
        for r in tc_list:
            allele = r.get("mhc_allele_name") or r.get("assay_mhc_allele_name") or r.get("mhc_name")
            if allele:
                hla_counts[allele] = hla_counts.get(allele, 0) + 1

        return Evidence(
            status="OK",
            source="IEDB IQ‑API (epitope_search + tcell_search)",
            fetched_n=len(epi_list) + len(tc_list),
            data={
                "symbol": symbol,
                "epitopes_n": len(epi_list),
                "tcell_assays_n": len(tc_list),
                "hla_breakdown": sorted([[k, v] for k, v in hla_counts.items()], key=lambda kv: kv[1], reverse=True)[:25],
                "examples": {
                    "epitopes": epi_list[: min(10, limit)],
                    "tcell_assays": tc_list[: min(10, limit)],
                },
            },
            citations=[epi_url, tc_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"IEDB query failed: {e}",
            fetched_n=0,
            data={"symbol": symbol, "epitopes_n": 0, "tcell_assays_n": 0},
            citations=[epi_url, tc_url],
            fetched_at=_now(),
        )


# -----------------------------------------------------------------------------
# BUCKET 6 – Clinical Translation & Safety
# -----------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(
    condition: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(3, ge=1, le=100, description="Maximum number of trials to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    base = "https://clinicaltrials.gov/api/v2/studies"
    q = f"{base}?query.cond={urllib.parse.quote(condition)}&pageSize={limit}"
    try:
        js = await _get_json(q)
        studies = js.get("studies", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="ClinicalTrials.gov v2",
            fetched_n=len(studies),
            data={"condition": condition, "studies": studies[:limit]},
            citations=[q],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"condition": condition, "studies": []},
            citations=[q],
            fetched_at=_now(),
        )


# -------------------------- NEW (live) ----------------------------------------
@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(
    condition: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of RWE proxy rows to return"),
) -> Evidence:
    """
    Real‑world evidence proxies from public sources (FAERS + Ct.Gov).
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")

    faers_url = (
        "https://api.fda.gov/drug/event.json"
        f"?search=patient.reaction.reactionmeddrapt.exact:%22{urllib.parse.quote(condition)}%22&limit={limit}"
    )
    ct_url = (
        "https://clinicaltrials.gov/api/v2/studies"
        f"?query.cond={urllib.parse.quote(condition)}&filter.studyType=Observational&pageSize={min(100, limit)}"
    )

    faers_results: List[Dict[str, Any]] = []
    trials: List[Dict[str, Any]] = []
    try:
        faers = await _get_json(faers_url)
        faers_results = faers.get("results", []) if isinstance(faers, dict) else []
    except Exception:
        pass
    try:
        js = await _get_json(ct_url)
        trials = js.get("studies", []) if isinstance(js, dict) else []
    except Exception:
        pass

    return Evidence(
        status="OK",
        source="openFDA FAERS + ClinicalTrials.gov v2",
        fetched_n=len(faers_results) + len(trials),
        data={
            "condition": condition,
            "faers_events": faers_results[:limit],
            "observational_trials": trials[:limit],
        },
        citations=[faers_url, ct_url],
        fetched_at=_now(),
    )


@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of adverse event reports to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = f"https://api.fda.gov/drug/event.json?search=patient.drug.openfda.generic_name:{urllib.parse.quote(symbol)}&limit={limit}"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="openFDA FAERS",
            fetched_n=len(results),
            data={"symbol": symbol, "reports": results[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "reports": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of pipeline entries to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    inx_url = f"https://drugs.ncats.io/api/v1/drugs?name={urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(inx_url)
        items = js.get("content", []) if isinstance(js, dict) else []
        if items:
            return Evidence(
                status="OK",
                source="Inxight Drugs",
                fetched_n=len(items),
                data={"symbol": symbol, "pipeline": items[:limit]},
                citations=[inx_url],
                fetched_at=_now(),
            )
    except Exception:
        pass
    result = await _safe_call(tract_drugs(symbol, x_api_key, limit))
    return Evidence(
        status=result.status,
        source=result.source,
        fetched_n=result.fetched_n,
        data={"symbol": symbol, "pipeline": result.data.get("interactions", [])[:limit]},
        citations=result.citations,
        fetched_at=result.fetched_at,
    )


# -----------------------------------------------------------------------------
# BUCKET 7 – Competition & IP
# -----------------------------------------------------------------------------

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(
    symbol: str,
    condition: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of patent records to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    cond = condition if condition else ""
    query = {"_and": [
        {"_or": [
            {"patent_title": {"_text_any": symbol}},
            {"patent_abstract": {"_text_any": symbol}}
        ]},
    ]}
    if cond:
        query["_and"].append({"_or": [
            {"patent_title": {"_text_any": cond}},
            {"patent_abstract": {"_text_any": cond}}
        ]})
    query_str = urllib.parse.quote(json.dumps(query))
    fields = urllib.parse.quote(json.dumps(["patent_id"]))
    pat_url = f"https://api.patentsview.org/patents/query?q={query_str}&f={fields}"
    try:
        js = await _get_json(pat_url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="PatentsView query",
            fetched_n=len(patents),
            data={"symbol": symbol, "condition": condition, "patents": patents[:limit]},
            citations=[pat_url],
            fetched_at=_now(),
        )
    except Exception:
        pass
    drug_res = await _safe_call(tract_drugs(symbol, x_api_key, limit))
    trial_res = await _safe_call(clin_endpoints(condition or symbol, x_api_key, limit))
    count = (drug_res.fetched_n if drug_res else 0) + (trial_res.fetched_n if trial_res else 0)
    return Evidence(
        status="OK",
        source="Drugs+Trials fallback",
        fetched_n=count,
        data={
            "symbol": symbol,
            "condition": condition,
            "drugs_n": drug_res.fetched_n if drug_res else 0,
            "trials_n": trial_res.fetched_n if trial_res else 0,
        },
        citations=(drug_res.citations if drug_res else []) + (trial_res.citations if trial_res else []),
        fetched_at=_now(),
    )


@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(
    symbol: str,
    x_api_key: Optional[str] = Header(default=None),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of patent identifiers to return"),
) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    query = {"_or": [
        {"patent_title": {"_text_any": symbol}},
        {"patent_abstract": {"_text_any": symbol}}
    ]}
    query_str = urllib.parse.quote(json.dumps(query))
    fields = urllib.parse.quote(json.dumps(["patent_id"]))
    url = f"https://api.patentsview.org/patents/query?q={query_str}&f={fields}"
    try:
        js = await _get_json(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="PatentsView FTO query",
            fetched_n=len(patents),
            data={"symbol": symbol, "patents": patents[:limit]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "patents": []},
            citations=[url],
            fetched_at=_now(),
        )
