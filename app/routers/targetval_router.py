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

router = APIRouter()

# =============================================================================
# Common model & helpers
# =============================================================================

class Evidence(BaseModel):
    status: str                 # "OK" | "NO_DATA" | "ERROR"
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
    """
    Helper to catch exceptions from module calls and return an ERROR Evidence instead of raising.
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

# =============================================================================
# Utility endpoints
# =============================================================================

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
            "B7: /comp/intensity, /comp/freedom"
        ]
    }

@router.get("/checklist")
def checklist_stub():
    return {"ok": True, "note": "Ledger optional—skipped in this build."}

# =============================================================================
# BUCKET 1 — Human Genetics & Causality (8)
# =============================================================================

# 1) Locus2Gene — Open Targets Genetics GraphQL
@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, efo: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
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
    body = await _post_json(gql, payload)
    coloc = body.get("data", {}).get("colocalisationByGeneAndDisease", []) or []
    return Evidence(status="OK", source="OpenTargets Genetics GraphQL", fetched_n=len(coloc),
                    data={"gene": gene, "efo": efo, "results": coloc},
                    citations=[gql], fetched_at=_now())

# 2) Rare variation & constraint — gnomAD GraphQL
@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
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
    body = await _post_json(gql, q)
    g = (body.get("data", {}) or {}).get("gene")
    return Evidence(status="OK" if g else "NO_DATA", source="gnomAD GraphQL",
                    fetched_n=1 if g else 0, data={"symbol": symbol, "constraint": g or {}},
                    citations=[gql], fetched_at=_now())

# 3) Mendelian overlap — Monarch
@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(symbol_or_entrez: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(symbol_or_entrez)}/diseases"
    try:
        body = await _get_json(url)
        items = body.get("associations", []) if isinstance(body, dict) else []
        return Evidence(status="OK", source="Monarch", fetched_n=len(items),
                        data={"gene": symbol_or_entrez, "associations": items[:100]},
                        citations=[url], fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="Monarch", fetched_n=0,
                        data={"gene": symbol_or_entrez}, citations=[url], fetched_at=_now())

# 4) Causal inference (MR) — stub with citation
@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(exposure_id: str, outcome_id: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="IEU OpenGWAS (MR compute not executed here)",
                    fetched_n=0, data={"exposure_id": exposure_id, "outcome_id": outcome_id},
                    citations=["https://gwas.mrcieu.ac.uk/"], fetched_at=_now())

# 5) lncRNA/circRNA — stub with citations
@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="NONCODE/LncBook/circBase",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["http://www.noncode.org/","https://bigd.big.ac.cn/lncbook/","http://www.circbase.org/"],
                    fetched_at=_now())

# 6) microRNAs — stub with citations
@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="miRTarBase/TargetScan",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["https://mirtarbase.cuhk.edu.cn/","https://www.targetscan.org/"],
                    fetched_at=_now())

# 7) Splicing/eQTL — eQTL Catalogue
@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    body = await _get_json(url)
    results = body if isinstance(body, list) else []
    return Evidence(status="OK", source="eQTL Catalogue", fetched_n=len(results),
                    data={"symbol": symbol, "results": results[:100]},
                    citations=[url], fetched_at=_now())

# 8) Epigenetics — ENCODE metadata
@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    q = urllib.parse.quote(f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}")
    url = f"https://www.encodeproject.org/{q}&format=json"
    body = await _get_json(url)
    hits = body.get("@graph", []) if isinstance(body, dict) else []
    return Evidence(status="OK", source="ENCODE", fetched_n=len(hits),
                    data={"symbol": symbol, "experiments": hits[:50]},
                    citations=[url], fetched_at=_now())

# =============================================================================
# BUCKET 2 — Disease Association & Perturbation (4)
# =============================================================================

# 9) Bulk RNA-seq — Expression Atlas experiments by condition
@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    js = await _get_json(url)
    exps = js.get("experiments", []) if isinstance(js, dict) else []
    return Evidence(status="OK", source="Expression Atlas", fetched_n=len(exps),
                    data={"condition": condition, "experiments": exps[:50]},
                    citations=[url], fetched_at=_now())

# 10) Bulk proteomics — PRIDE
@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    js = await _get_json(url)
    projects = js if isinstance(js, list) else []
    return Evidence(status="OK", source="PRIDE", fetched_n=len(projects),
                    data={"condition": condition, "projects": projects[:50]},
                    citations=[url], fetched_at=_now())

# 11) Single-cell & spatial — stub
@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="cellxgene/HCA portals",
                    fetched_n=0, data={"condition": condition},
                    citations=["https://cellxgene.cziscience.com/","https://data.humancellatlas.org/"],
                    fetched_at=_now())

# 12) Perturbation screens — stub
@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="BioGRID-ORCS/DepMap (UI-first)",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["https://orcs.thebiogrid.org/","https://depmap.org/portal/"],
                    fetched_at=_now())

# =============================================================================
# BUCKET 3 — Expression, Specificity & Localization (3)
# =============================================================================

# 13) Baseline expression — Expression Atlas
@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    base = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    body = await _get_json(base)
    experiments = body.get("experiments", []) if isinstance(body, dict) else []
    rows: List[Dict[str, Any]] = []
    for exp in experiments:
        for d in exp.get("data", []):
            rows.append({
                "experimentAccession": exp.get("experimentAccession"),
                "tissue": d.get("organismPart") or d.get("tissue"),
                "value": (d.get("expressions", [{}])[0].get("value") if d.get("expressions") else None)
            })
    return Evidence(status="OK", source="Expression Atlas", fetched_n=len(rows),
                    data={"symbol": symbol, "baseline": rows[:200]},
                    citations=[base], fetched_at=_now())

# 14) Protein localization — UniProt
@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    body = await _get_json(url)
    entries = body.get("results", []) if isinstance(body, dict) else []
    return Evidence(status="OK" if entries else "NO_DATA", source="UniProt REST",
                    fetched_n=len(entries), data={"symbol": symbol, "entries": entries},
                    citations=[url], fetched_at=_now())

# 15) Inducibility / temporal regulation — stub
@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(symbol: str, stimulus: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="GEO time-courses (download-first)",
                    fetched_n=0, data={"symbol": symbol, "stimulus": stimulus},
                    citations=["https://www.ncbi.nlm.nih.gov/geo/"], fetched_at=_now())

# =============================================================================
# BUCKET 4 — Mechanistic Wiring & Networks (3)
# =============================================================================

# 16) Pathways — Reactome
@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    search = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    s = await _get_json(search)
    hits = s.get("results", []) if isinstance(s, dict) else []
    pathways = [h for h in hits if (h.get("stId","").startswith("R-HSA") or "Pathway" in h.get("type",""))]
    return Evidence(status="OK", source="Reactome ContentService", fetched_n=len(pathways),
                    data={"symbol": symbol, "pathways": pathways[:100]},
                    citations=[search], fetched_at=_now())

# 17) PPI — STRING
@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(symbol: str, cutoff: float = 0.9, limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    ids = await _get_json(map_url)
    if not ids:
        return Evidence(status="OK", source="STRING", fetched_n=0,
                        data={"symbol": symbol, "edges": []},
                        citations=[map_url], fetched_at=_now())
    sid = ids[0].get("stringId")
    net_url = f"https://string-db.org/api/json/network?identifiers={sid}&species=9606"
    net = await _get_json(net_url)
    edges: List[Dict[str, Any]] = []
    for e in net if isinstance(net, list) else []:
        score = e.get("score") or e.get("combined_score")
        if score and float(score) >= cutoff:
            edges.append({"A": e.get("preferredName_A"), "B": e.get("preferredName_B"), "score": float(score)})
    return Evidence(status="OK", source="STRING REST", fetched_n=len(edges),
                    data={"symbol": symbol, "edges": edges[:limit]},
                    citations=[map_url, net_url], fetched_at=_now())

# 18) Ligand–receptor — OmniPath
@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    body = await _get_json(url)
    rows = body if isinstance(body, list) else []
    return Evidence(status="OK", source="OmniPath", fetched_n=len(rows),
                    data={"symbol": symbol, "ligrec": rows[:100]},
                    citations=[url], fetched_at=_now())

# =============================================================================
# BUCKET 5 — Tractability & Modality (6)
# =============================================================================

# 19) Druggability/pipeline — Open Targets Platform GraphQL knownDrugs
@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
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
    body = await _post_json(gql, q)
    target = (body.get("data", {}) or {}).get("target") or {}
    kd = (target.get("knownDrugs") or {})
    rows = kd.get("rows", []) or []
    return Evidence(status="OK", source="OpenTargets Platform GraphQL",
                    fetched_n=len(rows), data={"symbol": symbol, "knownDrugs": rows[:200], "count": kd.get("count")},
                    citations=[gql], fetched_at=_now())

# 20) Small-molecule ligandability — PDB search
@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = "https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps({
        "query": {"type":"terminal","service":"text","parameters":{"value":symbol}},
        "request_options":{"return_all_hits":True},"return_type":"entry"
    }))
    body = await _get_json(url)
    hits = body.get("result_set", []) if isinstance(body, dict) else []
    return Evidence(status="OK", source="RCSB PDB (search)", fetched_n=len(hits),
                    data={"symbol": symbol, "pdb_hits": hits[:100]},
                    citations=["https://www.rcsb.org/"], fetched_at=_now())

# 21) Antibody ligandability — UniProt topology
@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    body = await _get_json(url)
    entries = body.get("results", []) if isinstance(body, dict) else []
    return Evidence(status="OK" if entries else "NO_DATA", source="UniProt REST",
                    fetched_n=len(entries), data={"symbol": symbol, "entries": entries},
                    citations=[url], fetched_at=_now())

# 22) Oligonucleotide ligandability — stub
@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="RNAcentral/TargetScan design step",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["https://rnacentral.org/","https://www.targetscan.org/"], fetched_at=_now())

# 23) Modality feasibility — stub
@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="UniProt + SURFY/CSPA rules (later)",
                    fetched_n=0, data={"symbol": symbol}, citations=["https://rest.uniprot.org/"], fetched_at=_now())

# 24) Immunogenicity — stub
@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="IEDB (wire later)",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["https://www.iedb.org/"], fetched_at=_now())

# =============================================================================
# BUCKET 6 — Clinical Translation & Safety (4)
# =============================================================================

# 25) Clinical endpoints — ClinicalTrials.gov v2
@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&fields=protocolSection.outcomesModule"
    js = await _get_json(url)
    studies = js.get("studies", []) if isinstance(js, dict) else []
    return Evidence(status="OK", source="ClinicalTrials.gov v2",
                    fetched_n=len(studies), data={"condition": condition, "studies": studies[:50]},
                    citations=[url], fetched_at=_now())

# 26) RWE — stub
@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="Sentinel/N3C/SEER (access-controlled)",
                    fetched_n=0, data={"condition": condition},
                    citations=["https://www.sentinelinitiative.org/","https://covid.cd2h.org/N3C","https://seer.cancer.gov/"],
                    fetched_at=_now())

# 27) Safety / class AEs — openFDA FAERS
@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(drug: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug)}&count=patient.reaction.reactionmeddrapt.exact"
    try:
        js = await _get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return Evidence(status="OK", source="openFDA FAERS",
                        fetched_n=len(results), data={"drug": drug, "reactions": results[:100]},
                        citations=[url], fetched_at=_now())
    except Exception:
        return Evidence(status="NO_DATA", source="openFDA FAERS", fetched_n=0,
                        data={"drug": drug}, citations=[url], fetched_at=_now())

# 28) Clinical evidence / pipeline — alias to knownDrugs
@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    return await tract_drugs(symbol, x_api_key)

# =============================================================================
# BUCKET 7 — Competition & IP (2)
# =============================================================================

# 29) Competition intensity — OT knownDrugs count + CT.gov total
@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(symbol: str, condition: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    q = {"query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }",
         "variables": {"sym": symbol}}
    ct = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition or '')}&countTotal=true"
    ot = await _post_json(gql, q)
    trials = await _get_json(ct) if condition else {"totalStudies": None}
    count_kd = (((ot.get("data",{}) or {}).get("target") or {}).get("knownDrugs") or {}).get("count")
    total_trials = trials.get("totalStudies")
    return Evidence(status="OK", source="OpenTargets Platform + ClinicalTrials.gov",
                    fetched_n=1, data={"symbol": symbol, "knownDrugsCount": count_kd,
                                       "trialCount": total_trials, "condition": condition},
                    citations=[gql, ct], fetched_at=_now())

# 30) Freedom-to-operate — stub with citations
@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(query: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="Lens/Espacenet",
                    fetched_n=0, data={"query": query},
                    citations=["https://www.lens.org/","https://worldwide.espacenet.com/"],
                    fetched_at=_now())

# =============================================================================
# Minimal multi-module fan-out: /report/run
# =============================================================================

@router.get("/report/run")
async def report_run(gene: str, efo: str, symbol: str, condition: str,
                     x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)

    # fan-out to representative modules, wrapping each call in _safe_call
    l2g_task      = _safe_call(genetics_l2g(gene, efo, x_api_key))
    expr_task     = _safe_call(expr_baseline(symbol, x_api_key))
    ppi_task      = _safe_call(mech_ppi(symbol, 0.9, 50, x_api_key))
    path_task     = _safe_call(mech_pathways(symbol, x_api_key))
    clin_task     = _safe_call(clin_endpoints(condition, x_api_key))
    kd_task       = _safe_call(tract_drugs(symbol, x_api_key))

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
