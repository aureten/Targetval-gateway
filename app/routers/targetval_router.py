from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.utils.validation import validate_symbol, validate_condition

router = APIRouter()

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float

API_KEY = os.getenv("API_KEY")

def _require_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

def _now() -> float:
    return time.time()

DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=4.0)

async def _get_json(url: str, tries: int = 3) -> Any:
    err: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        for _ in range(tries):
            try:
                r = await client.get(url)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                err = e
                await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {err}")

async def _post_json(url: str, payload: Dict[str, Any], tries: int = 3) -> Any:
    err: Optional[Exception] = None
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

# --------------------------------------------------------------------------
# Genetics Modules
# --------------------------------------------------------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, efo: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    dis_url = f"https://www.disgenet.org/api/gda/gene/{urllib.parse.quote(gene)}?format=json"
    try:
        js = await _get_json(dis_url)
        records = js if isinstance(js, list) else []
        return Evidence(
            status="OK",
            source="DisGeNET gene-disease associations",
            fetched_n=len(records),
            data={"gene": gene, "efo": efo, "results": records},
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
            data={"gene": gene, "efo": efo, "results": hits},
            citations=[gwas_url],
            fetched_at=_now(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(gene: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    clinvar_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&term={urllib.parse.quote(gene)}%5Bgene%5D&retmode=json"
    )
    try:
        js = await _get_json(clinvar_url)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(
            status="OK",
            source="ClinVar E-utilities",
            fetched_n=len(ids),
            data={"gene": gene, "variants": ids},
            citations=[clinvar_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "variants": []},
            citations=[clinvar_url],
            fetched_at=_now(),
        )

@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(gene: str, efo: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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
            data={"gene": gene, "diseases": associations},
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
async def genetics_mr(gene: str, efo: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Approximate Mendelian randomisation by returning GWAS Catalog associations for the gene.
    This is a proxy and does not perform causal inference on summary statistics.
    """
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    base = "https://www.ebi.ac.uk/gwas/rest/api/associations"
    params = f"?gene_name={urllib.parse.quote(gene)}&size=200"
    url = f"{base}{params}"
    try:
        js = await _get_json(url)
        associations: List[Dict[str, Any]] = []
        if isinstance(js, dict):
            associations = js.get("_embedded", {}).get("associations", [])
        return Evidence(
            status="OK",
            source="GWAS Catalog associations",
            fetched_n=len(associations),
            data={"gene": gene, "associations": associations[:100]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"gene": gene, "associations": []},
            citations=[url],
            fetched_at=_now(),
        )

@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(gene: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(gene)}&page_size=50"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="RNAcentral REST",
            fetched_n=len(results),
            data={"gene": gene, "lncRNAs": results},
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
async def genetics_mirna(gene: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Placeholder for miRNA–gene interactions.  Returns NO_DATA because TarBase/miRTarBase
    require authentication and no public API is available.
    """
    _require_key(x_api_key)
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
async def genetics_sqtl(gene: str, efo: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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
            data={"gene": gene, "sqtls": results[:100]},
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
async def genetics_epigenetics(gene: str, efo: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    search_url = (
        "https://www.encodeproject.org/search/?"
        f"searchTerm={urllib.parse.quote(gene)}&format=json&limit=50&type=Experiment"
    )
    try:
        js = await _get_json(search_url)
        hits = js.get("@graph", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="ENCODE Search API",
            fetched_n=len(hits),
            data={"gene": gene, "experiments": hits},
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

# --------------------------------------------------------------------------
# Association & Perturbation Modules
# --------------------------------------------------------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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
            data={"condition": condition, "genes": genes[:100]},
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
async def assoc_bulk_prot(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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
            data={"condition": condition, "proteins": records[:100]},
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
async def assoc_sc(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Placeholder for single-cell expression evidence.  The cellxgene-census
    API is not integrated, so this returns NO_DATA.
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    return Evidence(
        status="NO_DATA",
        source="cellxgene-census (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://cellxgene.cziscience.com"],
        fetched_at=_now(),
    )

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Placeholder for CRISPR perturbation evidence.  BioGRID ORCS requires
    keys and complex queries, so this returns NO_DATA.
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    return Evidence(
        status="NO_DATA",
        source="BioGRID ORCS (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://orcs.thebiogrid.org"],
        fetched_at=_now(),
    )

# --------------------------------------------------------------------------
# Expression Modules
# --------------------------------------------------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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
                data={"symbol": symbol, "baseline": records[:100]},
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
                        "tissue": d.get("organismPart") or d.get("tissue"),
                        "level": d.get("expressionLevel") or d.get("level"),
                        "experiment": exp.get("experimentAccession"),
                    }
                )
        return Evidence(
            status="OK",
            source="Expression Atlas baseline",
            fetched_n=len(results),
            data={"symbol": symbol, "baseline": results[:100]},
            citations=[atlas_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={"symbol": symbol, "baseline": []},
            citations=[atlas_url],
            fetched_at=_now(),
        )

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = "https://compartments.jensenlab.org/Service?gene_names={}&format=json".format(
        urllib.parse.quote(symbol)
    )
    try:
        js = await _get_json(url)
        locs = js.get(symbol, []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="COMPARTMENTS API",
            fetched_n=len(locs),
            data={"symbol": symbol, "localization": locs[:50]},
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
async def expr_inducibility(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term={urllib.parse.quote(symbol)}%5Bgene%5D&retmode=json"
    )
    try:
        js = await _get_json(url)
        ids = js.get("esearchresult", {}).get("idlist", [])
        return Evidence(
            status="OK",
            source="GEO E-utilities",
            fetched_n=len(ids),
            data={"symbol": symbol, "datasets": ids},
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

# --------------------------------------------------------------------------
# Mechanistic Modules
# --------------------------------------------------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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
                    {"name": h.get("name"), "stId": h.get("stId"), "score": h.get("score")}
                )
        return Evidence(
            status="OK",
            source="Reactome ContentService",
            fetched_n=len(pathways),
            data={"symbol": symbol, "pathways": pathways[:50]},
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
    cutoff: float = 0.9,
    limit: int = 50,
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
async def mech_ligrec(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Return ligand–receptor interactions from OmniPath.  Filters interactions where
    the supplied gene is either source or target.
    """
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
            data={"symbol": symbol, "interactions": filtered[:100]},
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

# --------------------------------------------------------------------------
# Tractability & Modality Modules
# --------------------------------------------------------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Find drug–gene interactions.  Uses DGIdb and falls back to OpenTargets.
    """
    _require_key(x_api_key)
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
                data={"symbol": symbol, "interactions": interactions},
                citations=[dg_url],
                fetched_at=_now(),
            )
    except Exception:
        pass
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
            data={"symbol": symbol, "interactions": rows},
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
async def tract_ligandability_sm(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Retrieve ligandability data for small-molecule drugs via ChEMBL.
    """
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
            data={"symbol": symbol, "targets": targets[:100]},
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
async def tract_ligandability_ab(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Retrieve structural information for potential antibody targets via PDBe.
    """
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
            data={"symbol": symbol, "structures": entries[:50]},
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
async def tract_ligandability_oligo(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Placeholder for oligonucleotide ligandability.  No public RiboAPT API available.
    """
    _require_key(x_api_key)
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
async def tract_modality(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Simple rule-based modality classifier.  Certain cytokines and surface markers are flagged
    for antibody therapy; non-coding RNAs and mitochondrial genes are flagged as 'Other';
    everything else defaults to small-molecule.
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    s = symbol.upper()
    if any(s.startswith(p) for p in ("IL", "CD", "TNF", "IFN", "TGF", "FGF", "VEGF", "EGF", "CSF")):
        mode = "Antibody"
    elif any(s.startswith(p) for p in ("MIR", "LET", "LINC", "SNOR", "SNORA", "SNORD", "RNU", "RNA", "MT-")):
        mode = "Other"
    else:
        mode = "Small molecule"
    return Evidence(
        status="OK",
        source="Rule-based modality",
        fetched_n=1,
        data={"symbol": symbol, "modality": mode},
        citations=[],
        fetched_at=_now(),
    )

@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Placeholder for immunogenicity evidence (IEDB IQ-API not implemented).
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    return Evidence(
        status="NO_DATA",
        source="IEDB IQ-API (not implemented)",
        fetched_n=0,
        data={},
        citations=["https://www.iedb.org/api"],
        fetched_at=_now(),
    )

# --------------------------------------------------------------------------
# Clinical Modules
# --------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Fetch clinical trials (first three studies) for a given condition from ClinicalTrials.gov v2.
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    base = "https://clinicaltrials.gov/api/v2/studies"
    q = f"{base}?query.cond={urllib.parse.quote(condition)}&pageSize=3"
    try:
        js = await _get_json(q)
        studies = js.get("studies", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="ClinicalTrials.gov v2",
            fetched_n=len(studies),
            data={"condition": condition, "studies": studies},
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
async def clin_rwe(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Placeholder for real-world evidence.  Returns NO_DATA because such data are often proprietary.
    """
    _require_key(x_api_key)
    validate_condition(condition, field_name="condition")
    return Evidence(
        status="NO_DATA",
        source="Real-world evidence (not implemented)",
        fetched_n=0,
        data={},
        citations=[],
        fetched_at=_now(),
    )

@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Query FDA Adverse Event Reporting (openFDA FAERS) for a target's drug reports.
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    url = f"https://api.fda.gov/drug/event.json?search=patient.drug.openfda.generic_name:{urllib.parse.quote(symbol)}&limit=50"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="openFDA FAERS",
            fetched_n=len(results),
            data={"symbol": symbol, "reports": results},
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
async def clin_pipeline(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Query Inxight Drugs for pipeline entries.  Falls back to tractability data.
    """
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
                data={"symbol": symbol, "pipeline": items},
                citations=[inx_url],
                fetched_at=_now(),
            )
    except Exception:
        pass
    # Fallback: reuse tract_drugs to get known drug interactions
    result = await _safe_call(tract_drugs(symbol))
    return Evidence(
        status=result.status,
        source=result.source,
        fetched_n=result.fetched_n,
        data={"symbol": symbol, "pipeline": result.data.get("interactions", [])},
        citations=result.citations,
        fetched_at=result.fetched_at,
    )

# --------------------------------------------------------------------------
# Competition & IP Modules
# --------------------------------------------------------------------------

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(
    symbol: str,
    condition: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
) -> Evidence:
    """
    Count competition intensity via patents.  Fallback counts drugs plus trials.
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    cond = condition if condition else ""
    query = {"_and": [{"_or": [{"patent_title": {"_text_any": symbol}}, {"patent_abstract": {"_text_any": symbol}}]}]}
    if cond:
        query["_and"].append({"_or": [{"patent_title": {"_text_any": cond}}, {"patent_abstract": {"_text_any": cond}}]})
    query_str = urllib.parse.quote(json.dumps(query))
    pat_url = f"https://api.patentsview.org/patents/query?q={query_str}&f=[\"patent_id\"]"
    try:
        js = await _get_json(pat_url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="PatentsView query",
            fetched_n=len(patents),
            data={"symbol": symbol, "condition": condition, "patents": patents},
            citations=[pat_url],
            fetched_at=_now(),
        )
    except Exception:
        pass
    drug_res = await _safe_call(tract_drugs(symbol))
    trial_res = await _safe_call(clin_endpoints(condition or symbol))
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
async def comp_freedom(symbol: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
    """
    Simple freedom-to-operate check via PatentsView.
    """
    _require_key(x_api_key)
    validate_symbol(symbol, field_name="symbol")
    query = {"_or": [{"patent_title": {"_text_any": symbol}}, {"patent_abstract": {"_text_any": symbol}}]}
    query_str = urllib.parse.quote(json.dumps(query))
    url = f"https://api.patentsview.org/patents/query?q={query_str}&f=[\"patent_id\"]"
    try:
        js = await _get_json(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="PatentsView FTO query",
            fetched_n=len(patents),
            data={"symbol": symbol, "patents": patents},
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

# --------------------------------------------------------------------------
# Module/Bucket Mapping
# --------------------------------------------------------------------------

MODULE_BUCKET_MAP: Dict[str, str] = {
    "genetics_l2g": "Human Genetics & Causality",
    "genetics_rare": "Human Genetics & Causality",
    "genetics_mendelian": "Human Genetics & Causality",
    "genetics_mr": "Human Genetics & Causality",
    "genetics_lncrna": "Human Genetics & Causality",
    "genetics_mirna": "Human Genetics & Causality",
    "genetics_sqtl": "Human Genetics & Causality",
    "genetics_epigenetics": "Human Genetics & Causality",
    "assoc_bulk_rna": "Disease Association & Perturbation",
    "assoc_bulk_prot": "Disease Association & Perturbation",
    "assoc_sc": "Disease Association & Perturbation",
    "assoc_perturb": "Disease Association & Perturbation",
    "expression_baseline": "Expression, Specificity & Localization",
    "expr_localization": "Expression, Specificity & Localization",
    "expr_inducibility": "Expression, Specificity & Localization",
    "mech_pathways": "Mechanistic Wiring & Networks",
    "mech_ppi": "Mechanistic Wiring & Networks",
    "mech_ligrec": "Mechanistic Wiring & Networks",
    "tract_drugs": "Tractability & Modality",
    "tract_ligandability_sm": "Tractability & Modality",
    "tract_ligandability_ab": "Tractability & Modality",
    "tract_ligandability_oligo": "Tractability & Modality",
    "tract_modality": "Tractability & Modality",
    "tract_immunogenicity": "Tractability & Modality",
    "clin_endpoints": "Clinical Translation & Safety",
    "clin_rwe": "Clinical Translation & Safety",
    "clin_safety": "Clinical Translation & Safety",
    "clin_pipeline": "Clinical Translation & Safety",
    "comp_intensity": "Competition & IP",
    "comp_freedom": "Competition & IP",
}
