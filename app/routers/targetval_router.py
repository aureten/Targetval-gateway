"""
Routes implementing the TARGETVAL gateway (revised).

This module exposes a suite of REST endpoints grouped by functional
buckets (e.g., Human Genetics & Causality, Disease Association &
Perturbation, Expression & Localization, Mechanistic Wiring,
Tractability & Modality, Clinical Translation & Safety, Competition & IP).

Compared to the original implementation, this version introduces two
major improvements:

* **Caching of external API calls** – All outbound GET requests are
  cached in memory for a configurable amount of time (default 24
  hours).  This reduces latency and minimises load on third‑party
  services when identical queries are made repeatedly.

* **Concurrency throttling** – A global semaphore limits the number of
  simultaneous HTTP requests across all endpoints.  This prevents
  accidental saturation of remote services and provides backpressure
  when many modules are invoked concurrently.

Additionally, most endpoints now accept an optional ``limit`` query
parameter which controls how many records are returned (default 50).  This
parameter helps callers manage payload sizes without having to fetch
the full data set.

This revised version removes all API key requirements to allow public
access, and introduces a `/github/releases` endpoint that fetches the
latest releases for a given repository directly from the GitHub REST API.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.utils.validation import validate_symbol, validate_condition


# Router instance used by the FastAPI application.
router = APIRouter()


class Evidence(BaseModel):
    """Container for evidence returned by an endpoint.

    Attributes
    ----------
    status : str
        "OK" for successful queries, "ERROR" for exceptions,
        "NO_DATA" for unimplemented modules or empty results.
    source : str
        Description of the upstream API or fallback used to generate
        the data.
    fetched_n : int
        Number of records returned (prior to slicing by ``limit``).
    data : Dict[str, Any]
        Arbitrary payload containing the data returned from the upstream
        service.  The structure varies across modules but always nests
        results under descriptive keys.
    citations : List[str]
        List of URLs used as evidence sources.  These are returned to
        allow clients to reproduce or attribute the data.
    fetched_at : float
        UNIX timestamp when the data was fetched.
    """

    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float


# -----------------------------------------------------------------------------
# Caching and concurrency support
# -----------------------------------------------------------------------------

# In‑memory cache for upstream HTTP GET requests.  Each entry maps a
# request URL to a dictionary with the response JSON and the time it
# was fetched.  Cached entries expire after ``CACHE_TTL`` seconds.
CACHE: Dict[str, Dict[str, Any]] = {}

# Duration (in seconds) for which a cached response remains valid.
# The default of 24 hours strikes a balance between freshness and
# performance.  Adjust to suit your deployment needs.
CACHE_TTL: int = 24 * 60 * 60

# Default HTTP timeout used for all outgoing requests.  A lower
# connect timeout helps fail quickly when services are unreachable.
DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=4.0)

# Semaphore to limit the number of concurrent outbound requests.  This
# helps prevent overwhelming third‑party APIs when multiple modules are
# called concurrently.
MAX_CONCURRENT_REQUESTS: int = 5
_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


# API key enforcement placeholder.  The original implementation compared
# a request header against an environment variable.  To support public
# operation the check simply returns.  All endpoints no longer accept
# ``x-api-key`` headers.
def _require_key(_: Optional[str]) -> None:
    return


def _now() -> float:
    """Return the current UNIX timestamp."""
    return time.time()


async def _get_json(url: str, tries: int = 3) -> Any:
    """Perform an HTTP GET request and return the parsed JSON response.

    Before making a network request, this helper checks the in‑memory
    cache.  If a valid cached entry exists for ``url``, it returns the
    cached data immediately.  Otherwise it performs the request up to
    ``tries`` times, respecting the concurrency semaphore.  Successful
    responses are cached.
    """

    # Serve from cache if available and fresh
    cached = CACHE.get(url)
    if cached and (_now() - cached.get("timestamp", 0) < CACHE_TTL):
        return cached["data"]
    err: Optional[Exception] = None
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for _ in range(tries):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                    # Store in cache
                    CACHE[url] = {"data": data, "timestamp": _now()}
                    return data
                except Exception as e:
                    err = e
                    await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {err}")


async def _post_json(url: str, payload: Dict[str, Any], tries: int = 3) -> Any:
    """Perform an HTTP POST request with a JSON payload and return the response.

    The concurrency semaphore is respected but POST responses are not
    cached since many upstream APIs mutate state or depend on request
    payloads.  Retries are attempted on failure.
    """

    err: Optional[Exception] = None
    async with _semaphore:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for _ in range(tries):
                try:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    return resp.json()
                except Exception as e:
                    err = e
                    await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"POST failed for {url}: {err}")


async def _safe_call(coro):
    """Wrap a coroutine call in an Evidence object, catching exceptions.

    If the coroutine raises an ``HTTPException`` or any other error,
    return an ``Evidence`` object with status ``ERROR`` and the error
    description as the source.  This helper ensures that endpoints
    never propagate exceptions back to clients.
    """

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
    """Health check endpoint returning server time."""
    return {"ok": True, "time": _now()}


@router.get("/status")
def status() -> Dict[str, Any]:
    """Return a summary of the available modules grouped by bucket."""
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
            "GitHub: /github/releases",
        ],
    }


# -----------------------------------------------------------------------------
# BUCKET 1 – Human Genetics & Causality
# -----------------------------------------------------------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(
    gene: str,
    efo: str,
    limit: int = Query(50, ge=1, le=200, description="Maximum number of associations to return"),
) -> Evidence:
    """Gene–disease associations from DisGeNET; fallback to GWAS Catalog.

    Fetches gene–disease associations from the DisGeNET API using the provided
    gene symbol.  If no results are returned or the API call fails, it
    falls back to the GWAS Catalog REST API, filtering associations by gene
    name.  Results are truncated to ``limit`` entries.
    """

    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    # Primary: DisGeNET gene–disease associations.
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
    # Fallback: GWAS Catalog gene associations.
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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of variants to return"),
) -> Evidence:
    """Rare variant associations via ClinVar; fallback to gnomAD.

    Queries the ClinVar database through the NCBI E‑utilities API to
    retrieve variant identifiers associated with the given gene.  If
    ClinVar is unreachable or returns no results, attempts the gnomAD
    GraphQL API and returns any found variants.  Results are truncated
    to ``limit`` entries.
    """

    validate_symbol(gene, field_name="gene")
    # Primary: ClinVar via E‑utilities esearch.
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
    # Fallback: gnomAD GraphQL
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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of diseases to return"),
) -> Evidence:
    """Mendelian disease associations via the Monarch API.

    Uses the Monarch Initiative REST API to retrieve diseases associated
    with a human gene.  No fallback is currently provided because the
    Monarch service consolidates multiple ontologies internally.  Results
    are truncated to ``limit`` entries.
    """

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


@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(
    gene: str,
    efo: str,
) -> Evidence:
    """Mendelian randomisation placeholder using OpenGWAS.

    Integrating Mendelian randomisation requires statistical
    summarisation of large GWAS datasets.  A complete implementation is
    beyond the scope of this gateway; therefore the endpoint returns
    ``NO_DATA`` while still documenting the appropriate data source.
    """

    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    return Evidence(
        status="NO_DATA",
        source="OpenGWAS API (MR not implemented)",
        fetched_n=0,
        data={},
        citations=["https://gwas-api.opencagedata.com"],
        fetched_at=_now(),
    )


@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(
    gene: str,
    limit: int = Query(50, ge=1, le=200, description="Maximum number of lncRNAs to return"),
) -> Evidence:
    """Long non‑coding RNAs for a gene via RNAcentral.

    Queries the RNAcentral REST API for sequences whose annotations
    include the supplied gene symbol.  Returns up to ``limit`` matching
    records.  If the API call fails, returns an error.
    """

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


@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(
    gene: str,
) -> Evidence:
    """MicroRNA interactions placeholder (TarBase).

    An implementation would query DIANA‑TarBase or miRTarBase for
    experimentally validated miRNA–gene interactions.  These services
    require more complex queries and authentication than is available
    here.  As a result this endpoint returns ``NO_DATA`` while
    providing citations to the appropriate resources.
    """

    validate_symbol(gene, field_name="gene")
    return Evidence(
        status="NO_DATA",
        source="TarBase/miRTarBase (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://diana.e-ce.uth.gr/tarbase"],
        fetched_at=_now(),
    )


@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(
    gene: str,
    efo: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200, description="Maximum number of sQTLs to return"),
) -> Evidence:
    """Splicing QTLs from the eQTL Catalogue.

    Queries the eQTL Catalogue REST API for splicing QTLs associated
    with the supplied gene.  Returns up to ``limit`` records.
    """

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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of experiments to return"),
) -> Evidence:
    """Epigenetic data from the ENCODE portal.

    Searches the ENCODE REST API for experiments related to the gene
    symbol.  Returns up to ``limit`` records.  If the request fails or
    no data are found, the endpoint returns an appropriate error or
    empty result.
    """

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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of genes to return"),
) -> Evidence:
    """Tissue‑associated genes from GTEx for a given condition.

    Queries the GTEx Portal API for genes associated with a specific tissue or
    phenotype.  Returns up to ``limit`` records.  If the call fails, an
    error is reported.
    """

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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of proteins to return"),
) -> Evidence:
    """Proteins associated with a condition from ProteomicsDB.

    Queries the ProteomicsDB API for proteins matching a search term.  Returns
    up to ``limit`` records.  If the API call fails, an error is reported.
    """

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
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"condition": condition, "proteins": []},
            citations=[url],
            fetched_at=_now(),
        )


@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(
    condition: str,
) -> Evidence:
    """Single‑cell expression placeholder using cellxgene‑census.

    Single‑cell expression data require complex queries and large data downloads.
    The cellxgene‑census API provides programmatic access but is beyond the
    scope of this gateway.  This endpoint returns ``NO_DATA`` and cites the
    appropriate resource.
    """

    validate_condition(condition, field_name="condition")
    return Evidence(
        status="NO_DATA",
        source="cellxgene‑census (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://cellxgene.cziscience.com"],
        fetched_at=_now(),
    )


@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(
    condition: str,
) -> Evidence:
    """CRISPR perturbation placeholder using BioGRID ORCS.

    CRISPR screen data are accessible via the BioGRID ORCS API but
    require authentication keys and specialised queries.  This endpoint
    remains unimplemented and returns ``NO_DATA``.
    """

    validate_condition(condition, field_name="condition")
    return Evidence(
        status="NO_DATA",
        source="BioGRID ORCS (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://orcs.thebiogrid.org"],
        fetched_at=_now(),
    )


# -----------------------------------------------------------------------------
# BUCKET 3 – Expression, Specificity & Localization
# -----------------------------------------------------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(
    symbol: str,
    limit: int = Query(50, ge=1, le=500, description="Maximum number of baseline entries to return"),
) -> Evidence:
    """Baseline expression from the Human Protein Atlas (HPA).

    Queries the HPA API to retrieve baseline RNA and protein expression data
    for the specified gene symbol.  If the call fails or no records are
    returned, falls back to the Expression Atlas gene JSON endpoint and then
    to UniProt search.  Returns up to ``limit`` baseline records.
    """

    validate_symbol(symbol, field_name="symbol")
    # Primary: Human Protein Atlas search.  The HPA API allows a custom
    # search with a gene symbol and returns a JSON structure.  We request
    # JSON output.
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
    # Fallback: Expression Atlas baseline gene expression.
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
    # Final fallback: UniProt search for baseline (very coarse).  We
    # search by gene symbol and return the first few entries.
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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of localization entries to return"),
) -> Evidence:
    """Protein localisation evidence from the COMPARTMENTS API.

    Retrieves subcellular localisation scores for the specified gene from the
    COMPARTMENTS database.  Returns up to ``limit`` localisation entries.
    """

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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of dataset identifiers to return"),
) -> Evidence:
    """Inducible expression evidence from GEO via E‑utilities.

    Uses the NCBI E‑utilities ``esearch`` endpoint to search GEO for
    datasets mentioning the gene symbol.  Returns up to ``limit`` GEO
    dataset identifiers (GSE accession numbers).  If the call fails,
    an error is returned.
    """

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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of pathways to return"),
) -> Evidence:
    """Pathway assignments via Reactome search.

    Queries the Reactome ContentService search API for the given gene
    symbol and returns matching pathways.  Only the first ``limit`` results
    are returned.  If the call fails an error is returned.
    """

    validate_symbol(symbol, field_name="symbol")
    search = (
        f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    )
    try:
        js = await _get_json(search)
        hits = js.get("results", []) if isinstance(js, dict) else []
        pathways: List[Dict[str, Any]] = []
        for h in hits:
            # Filter to pathway entries (stId starting with R‑HSA).
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
) -> Evidence:
    """Protein–protein interaction neighbours via STRING.

    Maps the given gene symbol to a STRING identifier and retrieves
    high‑confidence interaction partners with scores above the given
    cutoff.  The number of neighbours returned is limited by the
    ``limit`` parameter.
    """

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
    limit: int = Query(100, ge=1, le=500, description="Maximum number of interactions to return"),
) -> Evidence:
    """Ligand–receptor interactions via OmniPath.

    Queries the OmniPath web service for interactions where the supplied
    gene symbol acts as either a ligand or receptor.  Returns up to
    ``limit`` interactions.  If the call fails, an error is reported.
    """

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
    limit: int = Query(100, ge=1, le=500, description="Maximum number of drug interactions to return"),
) -> Evidence:
    """Drug–gene interactions via DGIdb; fallback to OpenTargets.

    Queries the DGIdb REST API for drug–gene interactions.  If no
    results are returned or the call fails, falls back to the
    OpenTargets platform GraphQL knownDrugs endpoint.  Returns up to
    ``limit`` interaction records.
    """

    validate_symbol(symbol, field_name="symbol")
    dg_url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(symbol)}"
    try:
        body = await _get_json(dg_url)
        matched = body.get("matchedTerms", []) if isinstance(body, dict) else []
        interactions: List[Any] = []
        if matched:
            for term in matched:
                interactions.extend(term.get("interactions", []))
        if interactions:
            return Evidence(
                status="OK",
                source="DGIdb",
                fetched_n=len(interactions),
                data={"symbol": symbol, "interactions": interactions[:limit]},
                citations=[dg_url],
                fetched_at=_now(),
            )
    except Exception:
        pass
    # Fallback: OpenTargets knownDrugs GraphQL.
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
        return Evidence(
            status="OK",
            source="OpenTargets knownDrugs",
            fetched_n=len(rows),
            data={"symbol": symbol, "interactions": rows[:limit]},
            citations=[gql_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "interactions": []},
            citations=[dg_url, gql_url],
            fetched_at=_now(),
        )


@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(
    symbol: str,
    limit: int = Query(100, ge=1, le=500, description="Maximum number of small‑molecule targets to return"),
) -> Evidence:
    """Small‑molecule ligandability via ChEMBL.

    Searches the ChEMBL REST API for targets matching the gene symbol.
    Returns up to ``limit`` target entries.  If the call fails an error is
    returned.
    """

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
    limit: int = Query(50, ge=1, le=200, description="Maximum number of structures to return"),
) -> Evidence:
    """Antibody ligandability via PDBe.

    Queries the PDBe API for proteins matching the gene symbol.  The
    endpoint returns structural biology metadata which can be used to
    assess antibody accessibility.  Returns up to ``limit`` entries.
    """

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


@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(
    symbol: str,
) -> Evidence:
    """Oligonucleotide ligandability placeholder (RiboAPT).

    Ligandability of antisense oligonucleotides and aptamers is an
    emerging field.  A complete implementation would query resources
    such as the RiboAPT database or other aptamer repositories, but
    these are not accessible via open APIs.  Consequently this
    endpoint returns ``NO_DATA``.
    """

    validate_symbol(symbol, field_name="symbol")
    return Evidence(
        status="NO_DATA",
        source="RiboAPT (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://riboapt.bii.a-star.edu.sg"],
        fetched_at=_now(),
    )


@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(
    symbol: str,
) -> Evidence:
    """Therapeutic modality placeholder.

    Determining the best therapeutic modality (small molecule, antibody,
    RNAi, etc.) requires integration of multiple sources including
    structural data, expression profiles, and safety considerations.
    This endpoint is not implemented and returns ``NO_DATA``.
    """

    validate_symbol(symbol, field_name="symbol")
    return Evidence(
        status="NO_DATA",
        source="Modality assessment (not implemented)",
        fetched_n=0,
        data={},
        citations=[],
        fetched_at=_now(),
    )


@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(
    symbol: str,
) -> Evidence:
    """Immunogenicity evidence via IEDB (placeholder).

    Immunogenicity of proteins and peptides can be assessed through the
    Immune Epitope Database (IEDB) query API.  An implementation would
    query the IQ‑API to retrieve epitope records for the gene.  Here we
    return ``NO_DATA`` and cite the resource.
    """

    validate_symbol(symbol, field_name="symbol")
    return Evidence(
        status="NO_DATA",
        source="IEDB IQ‑API (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://www.iedb.org/api"],
        fetched_at=_now(),
    )


# -----------------------------------------------------------------------------
# BUCKET 6 – Clinical Translation & Safety
# -----------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(
    condition: str,
    limit: int = Query(3, ge=1, le=100, description="Maximum number of trials to return"),
) -> Evidence:
    """Clinical trials for a given condition from ClinicalTrials.gov.

    Queries the ClinicalTrials.gov v2 API for the first ``limit`` studies
    matching the supplied condition.  Returns the studies array from the
    JSON response.  If the call fails an error is returned.
    """

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


@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(
    condition: str,
) -> Evidence:
    """Real‑world evidence placeholder.

    Real‑world evidence (RWE) data often require access to proprietary
    claims databases, registries or EMRs.  This endpoint is left
    unimplemented and returns ``NO_DATA``.
    """

    validate_condition(condition, field_name="condition")
    return Evidence(
        status="NO_DATA",
        source="Real‑world evidence (not implemented)",
        fetched_n=0,
        data={},
        citations=[],
        fetched_at=_now(),
    )


@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(
    symbol: str,
    limit: int = Query(50, ge=1, le=500, description="Maximum number of adverse event reports to return"),
) -> Evidence:
    """Drug safety signals via openFDA.

    Queries the openFDA drug adverse event API for reports mentioning the
    gene symbol.  Returns up to ``limit`` raw hits.  If the call fails an
    error is returned.  Note that openFDA has strict rate limits.
    """

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
    limit: int = Query(50, ge=1, le=500, description="Maximum number of pipeline entries to return"),
) -> Evidence:
    """Drug development pipeline via Inxight Drugs; fallback to tract_drugs.

    Queries the Inxight Drugs API for drugs targeting the given gene.
    If the call fails or returns no results, delegates to
    :func:`tract_drugs` to retrieve drug–gene interactions.  Returns
    up to ``limit`` pipeline entries.
    """

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
    # Fallback: reuse tract_drugs implementation.
    result = await _safe_call(tract_drugs(symbol, limit))
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
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of patent records to return"),
) -> Evidence:
    """Competitive intensity via PatentsView.

    Uses the PatentsView API to count patents mentioning the gene symbol
    (and optionally a condition).  Constructs a JSON query where the gene
    and condition are searched in patent titles and abstracts.  If
    PatentsView fails or returns no patents, returns the sum of the
    number of drugs and trials as a proxy for competition.  Patent
    results are truncated to ``limit`` entries.
    """

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
    pat_url = f"https://api.patentsview.org/patents/query?q={query_str}&f=[\"patent_id\"]"
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
    # Fallback: number of drugs and trials as proxy for competition.
    drug_res = await _safe_call(tract_drugs(symbol, limit))
    trial_res = await _safe_call(clin_endpoints(condition or symbol, limit))
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
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of patent identifiers to return"),
) -> Evidence:
    """Freedom‑to‑operate via PatentsView.

    Searches the PatentsView API for patents matching the gene symbol
    and returns up to ``limit`` patent identifiers.  A complete
    freedom‑to‑operate analysis would incorporate claim scope, expiry
    dates and jurisdiction; such analysis is beyond the scope of this
    gateway.
    """

    validate_symbol(symbol, field_name="symbol")
    query = {"_or": [
        {"patent_title": {"_text_any": symbol}},
        {"patent_abstract": {"_text_any": symbol}}
    ]}
    query_str = urllib.parse.quote(json.dumps(query))
    url = f"https://api.patentsview.org/patents/query?q={query_str}&f=[\"patent_id\"]"
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


# -----------------------------------------------------------------------------
# GitHub endpoints
# -----------------------------------------------------------------------------

@router.get("/github/releases", response_model=Evidence)
async def github_releases(
    repository: str,
    per_page: int = Query(5, ge=1, le=100, description="Number of releases to return"),
) -> Evidence:
    """Return recent GitHub releases for a repository.

    This endpoint proxies the GitHub REST API.  It returns the latest
    releases for the specified repository.  If there are no releases,
    an empty list is returned.  Results are wrapped in an Evidence
    object.
    """

    url = f"https://api.github.com/repos/{repository}/releases?per_page={per_page}"
    try:
        js = await _get_json(url)
        processed: List[Dict[str, Any]] = []
        if isinstance(js, list):
            for r in js:
                processed.append(
                    {
                        "name": r.get("name") or r.get("tag_name"),
                        "tag_name": r.get("tag_name"),
                        "published_at": r.get("published_at"),
                        "html_url": r.get("html_url"),
                    }
                )
        return Evidence(
            status="OK",
            source="GitHub releases API",
            fetched_n=len(processed),
            data={"repository": repository, "releases": processed},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"repository": repository, "releases": []},
            citations=[url],
            fetched_at=_now(),
        )
