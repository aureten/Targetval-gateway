from __future__ import annotations
import os
import time
import asyncio
import urllib.parse
import json
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
import httpx

# Import parameter validation helpers
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

def _require_key(x_api_key: Optional[str]):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server missing API_KEY env")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")

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

# ------------------------------------------------------------------------------
# Utility endpoints
# ------------------------------------------------------------------------------

@router.get("/health")
def health():
    return {"ok": True, "time": _now()}

@router.get("/status")
def status():
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

@router.get("/checklist")
def checklist_stub():
    return {"ok": True, "note": "Ledger optionalâskipped in this build."}

# ------------------------------------------------------------------------------
# BUCKET 1 â Human Genetics & Causality
# ------------------------------------------------------------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, efo: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Locus2Gene colocalisation: try Open Targets Genetics GraphQL; fallback to GWAS Catalog REST.
    """
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    gql = "https://genetics.opentargets.org/graphql"
    payload = {
        "query": """
        query Q($geneId:String!, $efoId:String!){
          target(ensemblId:$geneId){ id approvedSymbol }
          disease(efoId:$efoId){ id name }
          colocalisationByGeneAndDisease(geneId:$geneId, efoId:$efoId){
            studyId phenotypeId geneId diseaseId yProbaModel yProbaCc hasColoc hasColocConsensus
          }
        }""",
        "variables": {"geneId": gene, "efoId": efo}
    }
    try:
        body = await _post_json(gql, payload)
        coloc = body.get("data", {}).get("colocalisationByGeneAndDisease", []) or []
        return Evidence(
            status="OK",
            source="OpenTargets Genetics GraphQL",
            fetched_n=len(coloc),
            data={"gene": gene, "efo": efo, "results": coloc},
            citations=[gql],
            fetched_at=_now(),
        )
    except Exception:
        pass

    # fallback: GWAS Catalog gene associations filtered by EFO
    gwas_url = f"https://www.ebi.ac.uk/gwas/rest/api/associations?geneName={urllib.parse.quote(gene)}"
    try:
        js = await _get_json(gwas_url)
        hits: List[Dict[str, Any]] = []
        if isinstance(js, dict):
            hits = js.get("_embedded", {}).get("associations", [])
        elif isinstance(js, list):
            hits = js
        filtered: List[Dict[str, Any]] = []
        for assoc in hits:
            traits = assoc.get("efoTraits") or []
            for trait in traits:
                trait_id = trait.get("shortForm") if isinstance(trait, dict) else trait
                if trait_id and trait_id.upper() == efo.upper():
                    filtered.append(assoc)
                    break
        return Evidence(
            status="OK" if filtered else "NO_DATA",
            source="GWAS Catalog REST (fallback)",
            fetched_n=len(filtered),
            data={"gene": gene, "efo": efo, "associations": filtered[:100]},
            citations=[gwas_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"OpenTargets & GWAS Catalog failed: {e}",
            fetched_n=0,
            data={"gene": gene, "efo": efo},
            citations=[gql, gwas_url],
            fetched_at=_now(),
        )

@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Rare variation & constraint: try gnomAD GraphQL; fallback to PanelApp.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    gql = "https://gnomad.broadinstitute.org/api"
    q = {
        "query": """
        query Rare($symbol:String!){
          gene(gene_symbol:$symbol, reference_genome:GRCh38){
            gene_id gene_symbol
            constraint{ lof_z n_lof expected_lof pLI }
          }
        }""",
        "variables": {"symbol": symbol}
    }
    try:
        body = await _post_json(gql, q)
        g = (body.get("data", {}) or {}).get("gene")
        return Evidence(
            status="OK" if g else "NO_DATA",
            source="gnomAD GraphQL",
            fetched_n=1 if g else 0,
            data={"symbol": symbol, "constraint": g or {}},
            citations=[gql],
            fetched_at=_now(),
        )
    except Exception:
        pass

    panelapp_url = f"https://panelapp.genomicsengland.co.uk/api/v1/genes/{urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(panelapp_url)
        panels: List[Dict[str, Any]] = []
        if isinstance(js, dict) and "results" in js:
            panels = js["results"]
        elif isinstance(js, list):
            panels = js
        return Evidence(
            status="OK" if panels else "NO_DATA",
            source="PanelApp API (fallback)",
            fetched_n=len(panels),
            data={"symbol": symbol, "panels": panels[:50]},
            citations=[panelapp_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"gnomAD + PanelApp failed: {e}",
            fetched_n=0,
            data={"symbol": symbol},
            citations=[gql, panelapp_url],
            fetched_at=_now(),
        )

@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(symbol_or_entrez: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Mendelian overlap & phenotype match: try Monarch API; fallback to HPO JAX API.
    """
    _require_key(x_api_key)
    validate_symbol(symbol_or_entrez, field_name="symbol_or_entrez")
    identifier = symbol_or_entrez.strip()
    monarch_url = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(identifier)}/diseases"
    try:
        body = await _get_json(monarch_url)
        items = body.get("associations", []) if isinstance(body, dict) else []
        return Evidence(
            status="OK",
            source="Monarch",
            fetched_n=len(items),
            data={"gene": identifier, "associations": items[:100]},
            citations=[monarch_url],
            fetched_at=_now(),
        )
    except Exception:
        pass

    hpo_url = f"https://hpo.jax.org/api/hpo/gene/{urllib.parse.quote(identifier)}/disease"
    try:
        js = await _get_json(hpo_url)
        diseases = js.get("diseaseAssociations", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK" if diseases else "NO_DATA",
            source="HPO JAX API (fallback)",
            fetched_n=len(diseases),
            data={"gene": identifier, "diseases": diseases[:100]},
            citations=[hpo_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"Monarch + HPO failed: {e}",
            fetched_n=0,
            data={"gene": identifier},
            citations=[monarch_url, hpo_url],
            fetched_at=_now(),
        )

@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(exposure_id: str, outcome_id: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Mendelian randomisation: this endpoint is currently a stub.
    """
    _require_key(x_api_key)
    validate_symbol(exposure_id, field_name="exposure_id")
    validate_symbol(outcome_id, field_name="outcome_id")
    return Evidence(
        status="NO_DATA",
        source="IEU OpenGWAS (MR compute not executed here)",
        fetched_n=0,
        data={"exposure_id": exposure_id, "outcome_id": outcome_id},
        citations=["https://gwas.mrcieu.ac.uk/"],
        fetched_at=_now(),
    )

@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    lncRNA data: not implemented; returns a placeholder response.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    return Evidence(
        status="NO_DATA",
        source="NONCODE/LncBook/circBase",
        fetched_n=0,
        data={"symbol": symbol},
        citations=[
            "http://www.noncode.org/",
            "https://bigd.big.ac.cn/lncbook/",
            "http://www.circbase.org/",
        ],
        fetched_at=_now(),
    )

@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    miRNA data: not implemented; returns a placeholder response.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    return Evidence(
        status="NO_DATA",
        source="miRTarBase/TargetScan",
        fetched_n=0,
        data={"symbol": symbol},
        citations=[
            "https://mirtarbase.cuhk.edu.cn/",
            "https://www.targetscan.org/",
        ],
        fetched_at=_now(),
    )

@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Splicing QTL data from the eQTL Catalogue.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    body = await _get_json(url)
    results = body if isinstance(body, list) else []
    return Evidence(
        status="OK",
        source="eQTL Catalogue",
        fetched_n=len(results),
        data={"symbol": symbol, "results": results[:100]},
        citations=[url],
        fetched_at=_now(),
    )

@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Epigenetics: query ENCODE; fallback to Cistrome DB.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    q = urllib.parse.quote(f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}")
    url = f"https://www.encodeproject.org/{q}&format=json"
    try:
        body = await _get_json(url)
        hits = body.get("@graph", []) if isinstance(body, dict) else []
        return Evidence(
            status="OK",
            source="ENCODE",
            fetched_n=len(hits),
            data={"symbol": symbol, "experiments": hits[:50]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        pass

    cistrome_url = f"http://cistrome.org/api/data?gene_name={urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(cistrome_url)
        exp = js.get("data", []) if isinstance(js, dict) else js
        return Evidence(
            status="OK" if exp else "NO_DATA",
            source="Cistrome DB (fallback)",
            fetched_n=len(exp),
            data={"symbol": symbol, "experiments": exp[:50]},
            citations=[cistrome_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"ENCODE + Cistrome failed: {e}",
            fetched_n=0,
            data={"symbol": symbol},
            citations=[url, cistrome_url],
            fetched_at=_now(),
        )

# ------------------------------------------------------------------------------
# BUCKET 2 â Disease Association & Perturbation
# ------------------------------------------------------------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_condition(condition)
    url = f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    js = await _get_json(url)
    exps = js.get("experiments", []) if isinstance(js, dict) else []
    return Evidence(
        status="OK",
        source="Expression Atlas",
        fetched_n=len(exps),
        data={"condition": condition, "experiments": exps[:50]},
        citations=[url],
        fetched_at=_now(),
    )

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_condition(condition)
    url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    js = await _get_json(url)
    projects = js if isinstance(js, list) else []
    return Evidence(
        status="OK",
        source="PRIDE",
        fetched_n=len(projects),
        data={"condition": condition, "projects": projects[:50]},
        citations=[url],
        fetched_at=_now(),
    )

@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_condition(condition)
    return Evidence(
        status="NO_DATA",
        source="cellxgene/HCA portals",
        fetched_n=0,
        data={"condition": condition},
        citations=[
            "https://cellxgene.cziscience.com/",
            "https://data.humancellatlas.org/",
        ],
        fetched_at=_now(),
    )

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    return Evidence(
        status="NO_DATA",
        source="BioGRID-ORCS/DepMap (UI-first)",
        fetched_n=0,
        data={"symbol": symbol},
        citations=[
            "https://orcs.thebiogrid.org/",
            "https://depmap.org/portal/",
        ],
        fetched_at=_now(),
    )

# ------------------------------------------------------------------------------
# BUCKET 3 â Expression, Specificity & Localization
# ------------------------------------------------------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Baseline expression & tissue specificity: try Expression Atlas; fallback to UniProt tissueâspecific comments.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    base = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    try:
        body = await _get_json(base)
        experiments = body.get("experiments", []) if isinstance(body, dict) else []
        rows: List[Dict[str, Any]] = []
        for exp in experiments:
            for d in exp.get("data", []):
                rows.append({
                    "experimentAccession": exp.get("experimentAccession"),
                    "tissue": d.get("organismPart") or d.get("tissue"),
                    "value": (
                        d.get("expressions", [{}])[0].get("value")
                        if d.get("expressions")
                        else None
                    ),
                })
        return Evidence(
            status="OK",
            source="Expression Atlas",
            fetched_n=len(rows),
            data={"symbol": symbol, "baseline": rows[:200]},
            citations=[base],
            fetched_at=_now(),
        )
    except Exception:
        pass

    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    uni_url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,genes,cc_tissue_specificity&format=json&size=1"
    try:
        body = await _get_json(uni_url)
        entries = body.get("results", []) if isinstance(body, dict) else []
        baseline: List[Dict[str, Any]] = []
        if entries:
            entry = entries[0]
            ts = entry.get("comments", []) or []
            for c in ts:
                if c.get("commentType") == "TISSUE SPECIFICITY":
                    baseline.append({
                        "tissue": "; ".join(c.get("tissue", [])),
                        "value": None
                    })
        return Evidence(
            status="OK" if baseline else "NO_DATA",
            source="UniProt REST (fallback)",
            fetched_n=len(baseline),
            data={"symbol": symbol, "baseline": baseline},
            citations=[uni_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"Expression Atlas + UniProt failed: {e}",
            fetched_n=0,
            data={"symbol": symbol},
            citations=[base, uni_url],
            fetched_at=_now(),
        )

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    body = await _get_json(url)
    entries = body.get("results", []) if isinstance(body, dict) else []
    return Evidence(
        status="OK" if entries else "NO_DATA",
        source="UniProt REST",
        fetched_n=len(entries),
        data={"symbol": symbol, "entries": entries},
        citations=[url],
        fetched_at=_now(),
    )

@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(symbol: str, stimulus: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    if stimulus is not None:
        validate_condition(stimulus)
    return Evidence(
        status="NO_DATA",
        source="GEO time-courses (download-first)",
        fetched_n=0,
        data={"symbol": symbol, "stimulus": stimulus},
        citations=["https://www.ncbi.nlm.nih.gov/geo/"],
        fetched_at=_now(),
    )

# ------------------------------------------------------------------------------
# BUCKET 4 â Mechanistic Wiring & Networks
# ------------------------------------------------------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    search = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    s = await _get_json(search)
    hits = s.get("results", []) if isinstance(s, dict) else []
    pathways = [h for h in hits if (h.get("stId", "").startswith("R-HSA") or "Pathway" in h.get("type", ""))]
    return Evidence(
        status="OK",
        source="Reactome ContentService",
        fetched_n=len(pathways),
        data={"symbol": symbol, "pathways": pathways[:100]},
        citations=[search],
        fetched_at=_now(),
    )

@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(symbol: str, cutoff: float = 0.9, limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    if cutoff <= 0 or cutoff > 1:
        raise HTTPException(status_code=422, detail="cutoff must be in the range (0,1].")
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000.")
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    ids = await _get_json(map_url)
    if not ids:
        return Evidence(
            status="OK",
            source="STRING",
            fetched_n=0,
            data={"symbol": symbol, "edges": []},
            citations=[map_url],
            fetched_at=_now(),
        )
    sid = ids[0].get("stringId")
    net_url = f"https://string-db.org/api/json/network?identifiers={sid}&species=9606"
    net = await _get_json(net_url)
    edges: List[Dict[str, Any]] = []
    for e in net if isinstance(net, list) else []:
        score = e.get("score") or e.get("combined_score")
        if score and float(score) >= cutoff:
            edges.append({
                "A": e.get("preferredName_A"),
                "B": e.get("preferredName_B"),
                "score": float(score),
            })
    return Evidence(
        status="OK",
        source="STRING REST",
        fetched_n=len(edges),
        data={"symbol": symbol, "edges": edges[:limit]},
        citations=[map_url, net_url],
        fetched_at=_now(),
    )

@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    body = await _get_json(url)
    rows = body if isinstance(body, list) else []
    return Evidence(
        status="OK",
        source="OmniPath",
        fetched_n=len(rows),
        data={"symbol": symbol, "ligrec": rows[:100]},
        citations=[url],
        fetched_at=_now(),
    )

# ------------------------------------------------------------------------------
# BUCKET 5 â Tractability & Modality
# ------------------------------------------------------------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Druggability / pipeline: try OpenTargets knownDrugs; fallback to DGIdb interactions.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    q = {
        "query": """
        query Q($sym:String!){
          target(approvedSymbol:$sym){
            id approvedSymbol
            knownDrugs{ rows{ drugType drug{ id name } disease{ id name } phase } count }
          }
        }""",
        "variables": {"sym": symbol}
    }
    try:
        body = await _post_json(gql, q)
        target = (body.get("data", {}) or {}).get("target") or {}
        kd = target.get("knownDrugs") or {}
        rows = kd.get("rows", []) or []
        return Evidence(
            status="OK",
            source="OpenTargets Platform GraphQL",
            fetched_n=len(rows),
            data={"symbol": symbol, "knownDrugs": rows[:200], "count": kd.get("count")},
            citations=[gql],
            fetched_at=_now(),
        )
    except Exception:
        pass

    dgidb_url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(symbol)}"
    try:
        js = await _get_json(dgidb_url)
        interactions = js.get("matchedTerms", []) if isinstance(js, dict) else []
        rows: List[Dict[str, Any]] = []
        for term in interactions:
            rows.extend(term.get("interactions", []))
        return Evidence(
            status="OK" if rows else "NO_DATA",
            source="DGIdb API (fallback)",
            fetched_n=len(rows),
            data={"symbol": symbol, "interactions": rows[:200]},
            citations=[dgidb_url],
            fetched_at=_now(),
        )
    except Exception as e:
        return Evidence(
            status="ERROR",
            source=f"OpenTargets + DGIdb failed: {e}",
            fetched_n=0,
            data={"symbol": symbol},
            citations=[gql, dgidb_url],
            fetched_at=_now(),
        )

@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    url = "https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps({
        "query": {"type":"terminal","service":"text","parameters":{"value":symbol}},
        "request_options":{"return_all_hits":True},
        "return_type":"entry"
    }))
    body = await _get_json(url)
    hits = body.get("result_set", []) if isinstance(body, dict) else []
    return Evidence(
        status="OK",
        source="RCSB PDB (search)",
        fetched_n=len(hits),
        data={"symbol": symbol, "pdb_hits": hits[:100]},
        citations=["https://www.rcsb.org/"],
        fetched_at=_now(),
    )

@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(symbol)
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    body = await _get_json(url)
    entries = body.get("results", []) if isinstance(body, dict) else []
    return Evidence(
        status="OK" if entries else "NO_DATA",
        source="UniProt REST",
        fetched_n=len(entries),
        data={"symbol": symbol, "entries": entries},
        citations=[url],
        fetched_at=_now(),
    )

@router.get("/tract/ligandability-oligo", response_model=Evidence, deprecated=True)
async def tract_ligandability_oligo(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Oligonucleotide ligandability: not yet implemented (experimental).
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    raise HTTPException(
        status_code=501,
        detail="Endpoint 'tract/ligandability-oligo' is not implemented (experimental).",
    )

@router.get("/tract/modality", response_model=Evidence, deprecated=True)
async def tract_modality(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Modalities: not yet implemented (experimental).
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    raise HTTPException(
        status_code=501,
        detail="Endpoint 'tract/modality' is not implemented (experimental).",
    )

@router.get("/tract/immunogenicity", response_model=Evidence, deprecated=True)
async def tract_immunogenicity(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Immunogenicity: not yet implemented (experimental).
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    raise HTTPException(
        status_code=501,
        detail="Endpoint 'tract/immunogenicity' is not implemented (experimental).",
    )

# ------------------------------------------------------------------------------
# BUCKET 6 â Clinical Translation & Safety
# ------------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_condition(condition)
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&fields=protocolSection.outcomesModule"
    js = await _get_json(url)
    studies = js.get("studies", []) if isinstance(js, dict) else []
    return Evidence(
        status="OK",
        source="ClinicalTrials.gov v2",
        fetched_n=len(studies),
        data={"condition": condition, "studies": studies[:50]},
        citations=[url],
        fetched_at=_now(),
    )

@router.get("/clin/rwe", response_model=Evidence, deprecated=True)
async def clin_rwe(condition: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Real-world evidence (RWE): not yet implemented due to access controls.
    """
    _require_key(x_api_key)
    validate_condition(condition)
    raise HTTPException(
        status_code=501,
        detail="Endpoint 'clin/rwe' is not implemented due to access-controlled data (experimental).",
    )

@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(drug: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    validate_symbol(drug, field_name="drug")
    url = f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug)}&count=patient.reaction.reactionmeddrapt.exact"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(
            status="OK",
            source="openFDA FAERS",
            fetched_n=len(results),
            data={"drug": drug, "reactions": results[:100]},
            citations=[url],
            fetched_at=_now(),
        )
    except Exception:
        return Evidence(
            status="NO_DATA",
            source="openFDA FAERS",
            fetched_n=0,
            data={"drug": drug},
            citations=[url],
            fetched_at=_now(),
        )

@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Pipeline: delegate to tract_drugs for known-drug pipeline information.
    """
    return await tract_drugs(symbol, x_api_key)

# ------------------------------------------------------------------------------
# BUCKET 7 â Competition & IP
# ------------------------------------------------------------------------------

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(symbol: str, condition: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)):
    """
    Competition intensity: try OpenTargets + ClinicalTrials.gov; fallback to counts from tract_drugs and clin_endpoints.
    """
    _require_key(x_api_key)
    validate_symbol(symbol)
    if condition is not None:
        validate_condition(condition)
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    q = {"query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }", "variables": {"sym": symbol}}
    ct = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition or '')}&countTotal=true"
    try:
        ot = await _post_json(gql, q)
        trials = await _get_json(ct) if condition else {"totalStudies": None}
        count_kd = (((ot.get("data",{}) or {}).get("target") or {}).get("knownDrugs") or {}).get("count")
        total_trials = trials.get("totalStudies")
        return Evidence(
            status="OK",
            source="OpenTargets Platform + ClinicalTrials.gov",
            fetched_n=1,
            data={"symbol": symbol, "knownDrugsCount": count_kd, "trialCount": total_trials, "condition": condition},
            citations=[gql, ct],
            fetched_at=_now(),
        )
    except Exception:
        pass

    # fallback: use existing endpoints for counts
    kd_ev = await _safe_call(tract_drugs(symbol, x_api_key))
    clin_ev = await _safe_call(clin_endpoints(condition, x_api_key)) if condition else None
    kd_count = kd_ev.data.get("count") if kd_ev.status == "OK" else None
    trial_count = clin_ev.fetched_n if clin_ev and clin_ev.status == "OK" else None
    return Evidence(
        status="OK" if (kd_count is not None or trial_count is not None) else "NO_DATA",
        source="Fallback (tract_drugs + clin_endpoints)",
        fetched_n=1,
        data={"symbol": symbol, "knownDrugsCount": kd_count, "trialCount": trial_count, "condition": condition},
        citations=["fallback"],
        fetched_at=_now(),
    )

@router.get("/comp/freedom", response_model=Evidence, deprecated=True)
async def comp_freedom(query: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Freedom-to-operate (FTO): not yet implemented; patent data requires licensing.
    """
    _require_key(x_api_key)
    validate_condition(query)
    raise HTTPException(
        status_code=501,
        detail="Endpoint 'comp/freedom' is not implemented (experimental); patent data requires licensing.",
    )

# ------------------------------------------------------------------------------
# Minimal multi-module fan-out: /report/run
# ------------------------------------------------------------------------------

@router.get("/report/run")
async def report_run(gene: str, efo: str, symbol: str, condition: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Run a set of module calls in parallel and return a summary plus the full data.
    """
    _require_key(x_api_key)
    validate_symbol(gene, field_name="gene")
    validate_symbol(efo, field_name="efo")
    validate_symbol(symbol)
    validate_condition(condition)
    l2g_task  = _safe_call(genetics_l2g(gene, efo, x_api_key))
    expr_task = _safe_call(expr_baseline(symbol, x_api_key))
    ppi_task  = _safe_call(mech_ppi(symbol, 0.9, 50, x_api_key))
    path_task = _safe_call(mech_pathways(symbol, x_api_key))
    clin_task = _safe_call(clin_endpoints(condition, x_api_key))
    kd_task   = _safe_call(tract_drugs(symbol, x_api_key))

    l2g, expr, ppi, pathw, clin, kd = await asyncio.gather(
        l2g_task, expr_task, ppi_task, path_task, clin_task, kd_task
    )

    summary = {
        "genetics_l2g": {"status": l2g.status, "n": l2g.fetched_n},
        "expr_baseline": {"status": expr.status, "n": expr.fetched_n},
        "mech_ppi": {"status": ppi.status, "n": ppi.fetched_n},
        "mech_pathways": {"status": pathw.status, "n": pathw.fetched_n},
        "clin_endpoints": {"status": clin.status, "n": clin.fetched_n},
        "tract_drugs": {"status": kd.status, "n": kd.fetched_n},
    }
    return {
        "summary": summary,
        "modules": {
            "genetics_l2g": l2g.dict(),
            "expr_baseline": expr.dict(),
            "mech_ppi": ppi.dict(),
            "mech_pathways": pathw.dict(),
            "clin_endpoints": clin.dict(),
            "tract_drugs": kd.dict(),
        }
    }

# ------------------------------------------------------------------------------
# Alias routes to match diagnostic paths (mapping to existing handlers)
# ------------------------------------------------------------------------------

# genetics aliases
router.add_api_route("/genetics/splicing", genetics_sqtl, response_model=Evidence)
router.add_api_route("/genetics/sqtl", genetics_sqtl, response_model=Evidence)

# expression aliases
router.add_api_route("/expression/baseline", expr_baseline, response_model=Evidence)
router.add_api_route("/expression/localization", expr_localization, response_model=Evidence)
router.add_api_route("/expression/inducibility", expr_inducibility, response_model=Evidence)

# association aliases
router.add_api_route("/assoc/rna_bulk", assoc_bulk_rna, response_model=Evidence)
router.add_api_route("/assoc/proteomics", assoc_bulk_prot, response_model=Evidence)
router.add_api_route("/assoc/sc_spatial", assoc_sc, response_model=Evidence)

# network aliases
router.add_api_route("/pathways/reactome", mech_pathways, response_model=Evidence)
router.add_api_route("/ppi/string", mech_ppi, response_model=Evidence)
router.add_api_route("/networks/ligand_receptor", mech_ligrec, response_model=Evidence)

# tractability aliases
router.add_api_route("/tractability/druggability", tract_drugs, response_model=Evidence)
router.add_api_route("/tractability/ligandability_sm", tract_ligandability_sm, response_model=Evidence)
router.add_api_route("/tractability/ligandability_ab", tract_ligandability_ab, response_model=Evidence)
router.add_api_route("/tractability/ligandability_oligo", tract_ligandability_oligo, response_model=Evidence)
router.add_api_route("/tractability/modality_feasibility", tract_modality, response_model=Evidence)

# clinical aliases
router.add_api_route("/clinical/endpoints", clin_endpoints, response_model=Evidence)
router.add_api_route("/rwe/summary", clin_rwe, response_model=Evidence)
router.add_api_route("/safety/on_target", clin_safety, response_model=Evidence)

# competition/IP aliases
router.add_api_route("/competition/intensity", comp_intensity, response_model=Evidence)
router.add_api_route("/ip/landscape", comp_freedom, response_model=Evidence)
