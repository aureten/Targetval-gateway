"""
Routes implementing the TARGETVAL gateway (revised).

This module exposes a suite of REST endpoints grouped by functional
buckets (e.g., Human Genetics & Causality, Disease Association &
Perturbation, Expression & Localization, Mechanistic Wiring,
Tractability & Modality, Clinical Translation & Safety, Competition & IP).

Major improvements:
* Caching of outbound GETs (default 24h)
* Concurrency throttling across outbound requests
* Most endpoints accept an optional `limit` for payload control

This build removes API-key requirements for public operation and
includes `/github/releases` as a convenience proxy to GitHub REST.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.utils.validation import validate_symbol, validate_condition

# -----------------------------------------------------------------------------
# Router
# -----------------------------------------------------------------------------

router = APIRouter()

# -----------------------------------------------------------------------------
# Evidence model
# -----------------------------------------------------------------------------

class Evidence(BaseModel):
    """Standard container for endpoint responses."""
    status: str                # "OK" | "ERROR" | "NO_DATA"
    source: str                # description of upstream/fallback
    fetched_n: int             # number of records (before slicing by `limit`)
    data: Dict[str, Any]       # module-specific payload
    citations: List[str]       # URLs used as sources
    fetched_at: float          # UNIX timestamp

# -----------------------------------------------------------------------------
# Caching & concurrency
# -----------------------------------------------------------------------------

CACHE: Dict[str, Dict[str, Any]] = {}           # url -> {"data":..., "timestamp":...}
CACHE_TTL: int = 24 * 60 * 60                   # 24h
DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=4.0)
MAX_CONCURRENT_REQUESTS: int = 5
_sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

def _now() -> float:
    return time.time()

async def _get_json(url: str, tries: int = 3) -> Any:
    """GET JSON with cache + retries + concurrency guard."""
    hit = CACHE.get(url)
    if hit and (_now() - hit.get("timestamp", 0) < CACHE_TTL):
        return hit["data"]

    err: Optional[Exception] = None
    async with _sem:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for _ in range(tries):
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    js = r.json()
                    CACHE[url] = {"data": js, "timestamp": _now()}
                    return js
                except Exception as e:
                    err = e
                    await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {err}")

async def _post_json(url: str, payload: Dict[str, Any], tries: int = 3) -> Any:
    """POST JSON with retries + concurrency guard (no caching)."""
    err: Optional[Exception] = None
    async with _sem:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for _ in range(tries):
                try:
                    r = await client.post(url, json=payload)
                    r.raise_for_status()
                    return r.json()
                except Exception as e:
                    err = e
                    await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"POST failed for {url}: {err}")

async def _safe_call(coro):
    """Wrap any coroutine and convert errors into Evidence(status='ERROR')."""
    try:
        return await coro
    except HTTPException as e:
        return Evidence(status="ERROR", source=str(e.detail), fetched_n=0, data={}, citations=[], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0, data={}, citations=[], fetched_at=_now())

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
        "ok": True,
        "modules": [
            "genetics.l2g","genetics.rare","genetics.mendelian","genetics.mr",
            "genetics.lncrna","genetics.mirna","genetics.sqtl","genetics.epigenetics",
            "assoc.bulk_rna","assoc.bulk_prot","assoc.sc","assoc.perturb",
            "expr.baseline","expr.localization","expr.inducibility",
            "mech.pathways","mech.ppi","mech.ligrec",
            "tract.drugs","tract.ligandability.sm","tract.ligandability.ab",
            "tract.ligandability.oligo","tract.modality","tract.immunogenicity",
            "clin.endpoints","clin.rwe","clin.safety","clin.pipeline",
            "comp.intensity","comp.freedom","github.releases"
        ],
    }

# -----------------------------------------------------------------------------
# BUCKET 1 – Human Genetics & Causality
# -----------------------------------------------------------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(
    gene: str,
    # Accept BOTH param names; normalize internally:
    efo: Optional[str] = Query(None, description="EFO disease id"),
    efo_id: Optional[str] = Query(None, alias="efo_id", description="EFO disease id (alias of `efo`)"),
    limit: int = Query(50, ge=1, le=200, description="Max associations to return"),
) -> Evidence:
    """
    Gene–disease associations primary: DisGeNET; fallback: GWAS Catalog.
    Accepts `efo` or `efo_id` (either works).
    """
    efo = efo or efo_id
    if not efo:
        raise HTTPException(status_code=422, detail="Missing EFO id (use `efo` or `efo_id`).")

    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")

    # Primary: DisGeNET
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

    # Fallback: GWAS Catalog
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
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "efo": efo, "results": []},
            citations=[gwas_url],
            fetched_at=_now(),
        )

@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(
    gene: str,
    limit: int = Query(50, ge=1, le=200, description="Max variants to return"),
) -> Evidence:
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
            source="ClinVar E-utilities",
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
            variants { variantId genome { ac an } }
          }
        }""",
        "variables": {"symbol": gene},
    }
    try:
        body = await _post_json(gql_url, query)
        variants = (body.get("data", {}).get("gene", {}).get("variants", []) or [])
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
    limit: int = Query(50, ge=1, le=200, description="Max diseases to return"),
) -> Evidence:
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
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"gene": gene, "diseases": []}, citations=[url], fetched_at=_now())

@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(gene: str, efo: str) -> Evidence:
    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    return Evidence(status="NO_DATA", source="OpenGWAS API (MR not implemented)",
                    fetched_n=0, data={}, citations=["https://gwas-api.opencagedata.com"], fetched_at=_now())

@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(
    gene: str,
    limit: int = Query(50, ge=1, le=200, description="Max lncRNAs to return"),
) -> Evidence:
    validate_symbol(gene, field_name="gene")
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(gene)}&page_size={limit}"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="RNAcentral REST", fetched_n=len(results),
                        data={"gene": gene, "lncRNAs": results[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"gene": gene, "lncRNAs": []}, citations=[url], fetched_at=_now())

@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(gene: str) -> Evidence:
    validate_symbol(gene, field_name="gene")
    return Evidence(status="NO_DATA", source="TarBase/miRTarBase (not implemented)",
                    fetched_n=0, data={}, citations=["https://diana.e-ce.uth.gr/tarbase"], fetched_at=_now())

@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(
    gene: str,
    efo: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200, description="Max sQTLs to return"),
) -> Evidence:
    validate_symbol(gene, field_name="gene")
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(gene)}/sqtls"
    try:
        js = await _get_json(url)
        results = js.get("sqtls", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="eQTL Catalogue", fetched_n=len(results),
                        data={"gene": gene, "sqtls": results[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"gene": gene, "sqtls": []}, citations=[url], fetched_at=_now())

@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(
    gene: str,
    efo: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200, description="Max experiments to return"),
) -> Evidence:
    validate_symbol(gene, field_name="gene")
    search_url = ("https://www.encodeproject.org/search/?"
                  f"searchTerm={urllib.parse.quote(gene)}&format=json&limit={limit}&type=Experiment")
    try:
        js = await _get_json(search_url)
        hits = js.get("@graph", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="ENCODE Search API", fetched_n=len(hits),
                        data={"gene": gene, "experiments": hits[:limit]},
                        citations=[search_url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"gene": gene, "experiments": []}, citations=[search_url], fetched_at=_now())

# -----------------------------------------------------------------------------
# BUCKET 2 – Disease Association & Perturbation
# -----------------------------------------------------------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(
    condition: str,
    limit: int = Query(50, ge=1, le=200, description="Max genes to return"),
) -> Evidence:
    validate_condition(condition, field_name="condition")
    url = ("https://gtexportal.org/api/v2/association/genesByTissue"
           f"?tissueSiteDetail={urllib.parse.quote(condition)}")
    try:
        js = await _get_json(url)
        genes = js.get("genes", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="GTEx genesByTissue", fetched_n=len(genes),
                        data={"condition": condition, "genes": genes[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"condition": condition, "genes": []}, citations=[url], fetched_at=_now())

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(
    condition: str,
    limit: int = Query(50, ge=1, le=200, description="Max proteins to return"),
) -> Evidence:
    validate_condition(condition, field_name="condition")
    url = ("https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search"
           f"?search={urllib.parse.quote(condition)}")
    try:
        js = await _get_json(url)
        records: List[Any] = []
        if isinstance(js, dict):
            records = js.get("items", js.get("proteins", js.get("results", [])))
        elif isinstance(js, list):
            records = js
        return Evidence(status="OK", source="ProteomicsDB proteins search", fetched_n=len(records),
                        data={"condition": condition, "proteins": records[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"condition": condition, "proteins": []}, citations=[url], fetched_at=_now())

@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(condition: str) -> Evidence:
    validate_condition(condition, field_name="condition")
    return Evidence(status="NO_DATA", source="cellxgene-census (not implemented)",
                    fetched_n=0, data={}, citations=["https://cellxgene.cziscience.com"], fetched_at=_now())

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(condition: str) -> Evidence:
    validate_condition(condition, field_name="condition")
    return Evidence(status="NO_DATA", source="BioGRID ORCS (not implemented)",
                    fetched_n=0, data={}, citations=["https://orcs.thebiogrid.org"], fetched_at=_now())

# -----------------------------------------------------------------------------
# BUCKET 3 – Expression, Specificity & Localization
# -----------------------------------------------------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(
    symbol: str,
    limit: int = Query(50, ge=1, le=500, description="Max baseline entries to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    hpa_url = ("https://www.proteinatlas.org/api/search_download.php"
               f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(symbol)}")
    try:
        js = await _get_json(hpa_url)
        records = js if isinstance(js, list) else []
        if records:
            return Evidence(status="OK", source="Human Protein Atlas search_download", fetched_n=len(records),
                            data={"symbol": symbol, "baseline": records[:limit]},
                            citations=[hpa_url], fetched_at=_now())
    except Exception:
        pass

    atlas_url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    try:
        body = await _get_json(atlas_url)
        results: List[Dict[str, Any]] = []
        experiments = body.get("experiments", []) if isinstance(body, dict) else []
        for exp in experiments:
            for d in exp.get("data", []):
                results.append({"tissue": d.get("organismPart") or d.get("tissue") or "NA",
                                "level": d.get("expressions", [{}])[0].get("value")})
        if results:
            return Evidence(status="OK", source="Expression Atlas (baseline)", fetched_n=len(results),
                            data={"symbol": symbol, "baseline": results[:limit]},
                            citations=[atlas_url], fetched_at=_now())
    except Exception:
        pass

    uniprot_url = f"https://rest.uniprot.org/uniprotkb/search?query={urllib.parse.quote(symbol)}&format=json&size={limit}"
    try:
        body = await _get_json(uniprot_url)
        entries = body.get("results", []) if isinstance(body, dict) else []
        return Evidence(status="OK", source="UniProt search", fetched_n=len(entries),
                        data={"symbol": symbol, "baseline": entries[:limit]},
                        citations=[uniprot_url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "baseline": []},
                        citations=[hpa_url, atlas_url, uniprot_url], fetched_at=_now())

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(
    symbol: str,
    limit: int = Query(50, ge=1, le=200, description="Max localization entries to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    url = ("https://compartments.jensenlab.org/Service"
           f"?gene_names={urllib.parse.quote(symbol)}&format=json")
    try:
        js = await _get_json(url)
        locs = js.get(symbol, []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="COMPARTMENTS API", fetched_n=len(locs),
                        data={"symbol": symbol, "localization": locs[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "localization": []}, citations=[url], fetched_at=_now())

@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(
    symbol: str,
    limit: int = Query(50, ge=1, le=200, description="Max GEO dataset IDs to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term={urllib.parse.quote(symbol)}%5Bgene%5D"
           "&retmode=json")
    try:
        js = await _get_json(url)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(status="OK", source="GEO E-utilities", fetched_n=len(ids),
                        data={"symbol": symbol, "datasets": ids[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "datasets": []}, citations=[url], fetched_at=_now())

# -----------------------------------------------------------------------------
# BUCKET 4 – Mechanistic Wiring & Networks
# -----------------------------------------------------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(
    symbol: str,
    limit: int = Query(50, ge=1, le=200, description="Max pathways to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    search = (f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens")
    try:
        js = await _get_json(search)
        hits = js.get("results", []) if isinstance(js, dict) else []
        pathways: List[Dict[str, Any]] = []
        for h in hits:
            if "Pathway" in h.get("species", "") or (h.get("stId", "") or "").startswith("R-HSA"):
                pathways.append({"name": h.get("name"), "stId": h.get("stId"), "score": h.get("score")})
        return Evidence(status="OK", source="Reactome ContentService", fetched_n=len(pathways),
                        data={"symbol": symbol, "pathways": pathways[:limit]},
                        citations=[search], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "pathways": []}, citations=[search], fetched_at=_now())

@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(
    symbol: str,
    cutoff: float = Query(0.9, ge=0.0, le=1.0, description="Min interaction score"),
    limit: int = Query(50, ge=1, le=200, description="Max neighbours to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    map_url = ("https://string-db.org/api/json/get_string_ids"
               f"?identifiers={urllib.parse.quote(symbol)}&species=9606")
    net_tpl = "https://string-db.org/api/json/network?identifiers={id}&species=9606"
    try:
        ids = await _get_json(map_url)
        if not ids:
            return Evidence(status="OK", source="STRING", fetched_n=0,
                            data={"symbol": symbol, "neighbors": []},
                            citations=[map_url], fetched_at=_now())
        sid = ids[0].get("stringId")
        net = await _get_json(net_tpl.format(id=sid))
        neighbors: List[Dict[str, Any]] = []
        for e in net:
            score = e.get("score") or e.get("combined_score")
            if score and float(score) >= cutoff:
                neighbors.append({"preferredName_A": e.get("preferredName_A"),
                                  "preferredName_B": e.get("preferredName_B"),
                                  "score": float(score)})
        neighbors = neighbors[:limit]
        return Evidence(status="OK", source="STRING REST", fetched_n=len(neighbors),
                        data={"symbol": symbol, "neighbors": neighbors},
                        citations=[map_url, net_tpl.format(id=sid)], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "neighbors": []}, citations=[map_url], fetched_at=_now())

@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(
    symbol: str,
    limit: int = Query(100, ge=1, le=500, description="Max interactions to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    url = "https://omnipathdb.org/interactions?format=json&genes={gene}&substrate_only=false"
    try:
        js = await _get_json(url.format(gene=urllib.parse.quote(symbol)))
        interactions = js if isinstance(js, list) else []
        filtered: List[Dict[str, Any]] = [i for i in interactions if symbol in (i.get("source",""), i.get("target",""))]
        return Evidence(status="OK", source="OmniPath interactions", fetched_n=len(filtered),
                        data={"symbol": symbol, "interactions": filtered[:limit]},
                        citations=[url.format(gene=urllib.parse.quote(symbol))], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "interactions": []},
                        citations=[url.format(gene=urllib.parse.quote(symbol))], fetched_at=_now())

# -----------------------------------------------------------------------------
# BUCKET 5 – Tractability & Modality
# -----------------------------------------------------------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(
    symbol: str,
    limit: int = Query(100, ge=1, le=500, description="Max interactions to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    dg_url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(symbol)}"
    try:
        body = await _get_json(dg_url)
        matched = body.get("matchedTerms", []) if isinstance(body, dict) else []
        interactions: List[Any] = []
        for term in matched or []:
            interactions.extend(term.get("interactions", []))
        if interactions:
            return Evidence(status="OK", source="DGIdb", fetched_n=len(interactions),
                            data={"symbol": symbol, "interactions": interactions[:limit]},
                            citations=[dg_url], fetched_at=_now())
    except Exception:
        pass

    gql_url = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
        query ($symbol: String!) {
          target(approvedSymbol: $symbol) {
            knownDrugs { rows { drugId drugName mechanismOfAction } }
          }
        }""",
        "variables": {"symbol": symbol},
    }
    try:
        res = await _post_json(gql_url, query)
        rows = (res.get("data", {}).get("target", {}).get("knownDrugs", {}).get("rows", []))
        return Evidence(status="OK", source="OpenTargets knownDrugs", fetched_n=len(rows),
                        data={"symbol": symbol, "interactions": rows[:limit]},
                        citations=[gql_url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "interactions": []},
                        citations=[dg_url, gql_url], fetched_at=_now())

@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(
    symbol: str,
    limit: int = Query(100, ge=1, le=500, description="Max targets to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(symbol)}&format=json"
    try:
        js = await _get_json(url)
        targets = js.get("targets", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="ChEMBL target search", fetched_n=len(targets),
                        data={"symbol": symbol, "targets": targets[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "targets": []}, citations=[url], fetched_at=_now())

@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(
    symbol: str,
    limit: int = Query(50, ge=1, le=200, description="Max structures to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(url)
        entries: List[Any] = []
        if isinstance(js, dict):
            for _, vals in js.items():
                if isinstance(vals, list):
                    entries.extend(vals)
        return Evidence(status="OK", source="PDBe proteins API", fetched_n=len(entries),
                        data={"symbol": symbol, "structures": entries[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "structures": []}, citations=[url], fetched_at=_now())

@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(symbol: str) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    return Evidence(status="NO_DATA", source="RiboAPT (not implemented)",
                    fetched_n=0, data={}, citations=["https://riboapt.bii.a-star.edu.sg"], fetched_at=_now())

@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(symbol: str) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    return Evidence(status="NO_DATA", source="Modality assessment (not implemented)",
                    fetched_n=0, data={}, citations=[], fetched_at=_now())

@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(symbol: str) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    return Evidence(status="NO_DATA", source="IEDB IQ-API (not implemented)",
                    fetched_n=0, data={}, citations=["https://www.iedb.org/api"], fetched_at=_now())

# -----------------------------------------------------------------------------
# BUCKET 6 – Clinical Translation & Safety
# -----------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(
    condition: str,
    limit: int = Query(3, ge=1, le=100, description="Max trials to return"),
) -> Evidence:
    validate_condition(condition, field_name="condition")
    base = "https://clinicaltrials.gov/api/v2/studies"
    q = f"{base}?query.cond={urllib.parse.quote(condition)}&pageSize={limit}"
    try:
        js = await _get_json(q)
        studies = js.get("studies", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="ClinicalTrials.gov v2", fetched_n=len(studies),
                        data={"condition": condition, "studies": studies[:limit]},
                        citations=[q], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"condition": condition, "studies": []}, citations=[q], fetched_at=_now())

@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(condition: str) -> Evidence:
    validate_condition(condition, field_name="condition")
    return Evidence(status="NO_DATA", source="Real-world evidence (not implemented)",
                    fetched_n=0, data={}, citations=[], fetched_at=_now())

@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(
    symbol: str,
    limit: int = Query(50, ge=1, le=500, description="Max AE reports to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    url = (f"https://api.fda.gov/drug/event.json?search=patient.drug.openfda.generic_name:"
           f"{urllib.parse.quote(symbol)}&limit={limit}")
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="openFDA FAERS", fetched_n=len(results),
                        data={"symbol": symbol, "reports": results[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "reports": []}, citations=[url], fetched_at=_now())

@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(
    symbol: str,
    limit: int = Query(50, ge=1, le=500, description="Max pipeline entries to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    inx_url = f"https://drugs.ncats.io/api/v1/drugs?name={urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(inx_url)
        items = js.get("content", []) if isinstance(js, dict) else []
        if items:
            return Evidence(status="OK", source="Inxight Drugs", fetched_n=len(items),
                            data={"symbol": symbol, "pipeline": items[:limit]},
                            citations=[inx_url], fetched_at=_now())
    except Exception:
        pass
    result = await _safe_call(tract_drugs(symbol, limit))
    return Evidence(status=result.status, source=result.source, fetched_n=result.fetched_n,
                    data={"symbol": symbol, "pipeline": result.data.get("interactions", [])[:limit]},
                    citations=result.citations, fetched_at=result.fetched_at)

# -----------------------------------------------------------------------------
# BUCKET 7 – Competition & IP
# -----------------------------------------------------------------------------

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(
    symbol: str,
    condition: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000, description="Max patent records to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    cond = condition or ""
    query = {"_and": [
        {"_or": [{"patent_title": {"_text_any": symbol}},
                 {"patent_abstract": {"_text_any": symbol}}]}
    ]}
    if cond:
        query["_and"].append({"_or": [{"patent_title": {"_text_any": cond}},
                                      {"patent_abstract": {"_text_any": cond}}]})
    query_str = urllib.parse.quote(json.dumps(query))
    pat_url = f"https://api.patentsview.org/patents/query?q={query_str}&f=[\"patent_id\"]"
    try:
        js = await _get_json(pat_url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="PatentsView query", fetched_n=len(patents),
                        data={"symbol": symbol, "condition": condition, "patents": patents[:limit]},
                        citations=[pat_url], fetched_at=_now())
    except Exception:
        pass
    drug_res = await _safe_call(tract_drugs(symbol, limit))
    trial_res = await _safe_call(clin_endpoints(condition or symbol, limit))
    count = (drug_res.fetched_n if drug_res else 0) + (trial_res.fetched_n if trial_res else 0)
    return Evidence(status="OK", source="Drugs+Trials fallback", fetched_n=count,
                    data={"symbol": symbol, "condition": condition,
                          "drugs_n": drug_res.fetched_n if drug_res else 0,
                          "trials_n": trial_res.fetched_n if trial_res else 0},
                    citations=(drug_res.citations if drug_res else []) + (trial_res.citations if trial_res else []),
                    fetched_at=_now())

@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(
    symbol: str,
    limit: int = Query(100, ge=1, le=1000, description="Max patent IDs to return"),
) -> Evidence:
    validate_symbol(symbol, field_name="symbol")
    query = {"_or": [{"patent_title": {"_text_any": symbol}},
                     {"patent_abstract": {"_text_any": symbol}}]}
    query_str = urllib.parse.quote(json.dumps(query))
    url = f"https://api.patentsview.org/patents/query?q={query_str}&f=[\"patent_id\"]"
    try:
        js = await _get_json(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="PatentsView FTO query", fetched_n=len(patents),
                        data={"symbol": symbol, "patents": patents[:limit]},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"symbol": symbol, "patents": []}, citations=[url], fetched_at=_now())

# -----------------------------------------------------------------------------
# GitHub convenience
# -----------------------------------------------------------------------------

@router.get("/github/releases", response_model=Evidence)
async def github_releases(
    repository: str,
    per_page: int = Query(5, ge=1, le=100, description="Number of releases to return"),
) -> Evidence:
    url = f"https://api.github.com/repos/{repository}/releases?per_page={per_page}"
    try:
        js = await _get_json(url)
        processed: List[Dict[str, Any]] = []
        if isinstance(js, list):
            for r in js:
                processed.append({
                    "name": r.get("name") or r.get("tag_name"),
                    "tag_name": r.get("tag_name"),
                    "published_at": r.get("published_at"),
                    "html_url": r.get("html_url"),
                })
        return Evidence(status="OK", source="GitHub releases API", fetched_n=len(processed),
                        data={"repository": repository, "releases": processed},
                        citations=[url], fetched_at=_now())
    except Exception as e:
        return Evidence(status="ERROR", source=str(e), fetched_n=0,
                        data={"repository": repository, "releases": []}, citations=[url], fetched_at=_now())
