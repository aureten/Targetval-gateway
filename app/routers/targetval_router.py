# app/routers/targetval_router.py
from __future__ import annotations
import os, time, asyncio, urllib.parse, base64, json
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
import httpx

router = APIRouter()

# ---------- Common model & helpers ----------
class Evidence(BaseModel):
    status: str                 # "OK" | "NO_DATA" | "ERROR"
    source: str                 # upstream system(s)
    fetched_n: int              # number of rows/objects
    data: Dict[str, Any]        # normalized payload
    citations: List[str]        # exact URLs or endpoint roots
    fetched_at: float           # epoch seconds

API_KEY = os.getenv("API_KEY")

def _require_key(x_api_key: Optional[str]):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server missing API_KEY env")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")

def _now() -> float:
    return time.time()

async def _get_json(client: httpx.AsyncClient, url: str, tries: int = 3) -> Any:
    err: Optional[Exception] = None
    for _ in range(tries):
        try:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"Upstream failed: {err}")

async def _post_json(client: httpx.AsyncClient, url: str, payload: Dict[str, Any], tries: int = 3) -> Any:
    err: Optional[Exception] = None
    for _ in range(tries):
        try:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            await asyncio.sleep(0.8)
    raise HTTPException(status_code=502, detail=f"Upstream failed: {err}")

# ---------- Utility & health ----------
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
def checklist():
    # Minimal stub—this endpoint will be fed later by each module writing a ledger row.
    return {"ok": True, "note": "Add per-module ledgers in next sprint."}

# ====================================================================================
# BUCKET 1 — Human Genetics & Causality (8)
# ====================================================================================

# 1. Locus2Gene colocalization — Open Targets Genetics (GraphQL)
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
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _post_json(client, gql, payload)
    coloc = body.get("data", {}).get("colocalisationByGeneAndDisease", []) or []
    return Evidence(status="OK", source="OpenTargets Genetics GraphQL", fetched_n=len(coloc),
                    data={"gene": gene, "efo": efo, "results": coloc}, citations=[gql], fetched_at=_now())

# 2. Rare variation & constraint — gnomAD GraphQL (LOEUF-like)
@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    # gnomAD public GraphQL endpoint
    gql = "https://gnomad.broadinstitute.org/api"
    q = {
        "query": """
        query Rare($symbol:String!){
          gene(gene_symbol:$symbol, reference_genome:GRCh38){
            gene_id gene_symbol
            constraint{
              lof_z n_lof expected_lof pLI
            }
          }
        }""",
        "variables": {"symbol": symbol}
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _post_json(client, gql, q)
    g = body.get("data", {}).get("gene")
    return Evidence(status="OK" if g else "NO_DATA", source="gnomAD GraphQL",
                    fetched_n=1 if g else 0, data={"symbol": symbol, "constraint": g or {}},
                    citations=[gql], fetched_at=_now())

# 3. Mendelian overlap — HPO / Monarch (simple)
@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    # Monarch API gene–disease associations (public)
    base = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(symbol)}/diseases"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        try:
            body = await _get_json(client, base)
            items = body.get("associations", []) if isinstance(body, dict) else []
            return Evidence(status="OK", source="Monarch", fetched_n=len(items),
                            data={"symbol": symbol, "associations": items[:100]},
                            citations=[base], fetched_at=_now())
        except Exception:
            return Evidence(status="NO_DATA", source="Monarch", fetched_n=0,
                            data={"symbol": symbol}, citations=[base], fetched_at=_now())

# 4. Causal inference (MR) — IEU OpenGWAS (stub + citation)
@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(exposure_id: str, outcome_id: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    # IEU OpenGWAS has APIs but MR runs typically require server-side compute/auth.
    cite = ["https://gwas.mrcieu.ac.uk/"]
    return Evidence(status="NO_DATA", source="IEU OpenGWAS (MR)", fetched_n=0,
                    data={"exposure_id": exposure_id, "outcome_id": outcome_id, "note":"MR compute not run in this gateway"},
                    citations=cite, fetched_at=_now())

# 5. Long non‑coding RNAs & circRNAs — (stub + citations)
@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="NONCODE/LncBook/circBase",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["http://www.noncode.org/", "https://bigd.big.ac.cn/lncbook/","http://www.circbase.org/"],
                    fetched_at=_now())

# 6. microRNAs — TargetScan/miRTarBase (stub + citations)
@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="miRTarBase/TargetScan",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["https://mirtarbase.cuhk.edu.cn/","https://www.targetscan.org/"], fetched_at=_now())

# 7. Splicing QTLs — eQTL Catalogue (API)
@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    # eQTL Catalogue search by gene symbol (metadata-level)
    base = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, base)
    # The API returns genes and tissues/quant types—summarize counts
    results = body if isinstance(body, list) else []
    return Evidence(status="OK", source="eQTL Catalogue", fetched_n=len(results),
                    data={"symbol": symbol, "results": results[:100]},
                    citations=[base], fetched_at=_now())

# 8. Epigenetics — ENCODE (API, metadata-level)
@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    # ENCODE search for ChIP‑seq experiments linked to the gene symbol
    q = urllib.parse.quote(f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}")
    url = f"https://www.encodeproject.org/{q}&format=json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, url)
    hits = body.get("@graph", []) if isinstance(body, dict) else []
    return Evidence(status="OK", source="ENCODE", fetched_n=len(hits),
                    data={"symbol": symbol, "experiments": hits[:50]},
                    citations=[url], fetched_at=_now())

# ====================================================================================
# BUCKET 2 — Disease Association & Perturbation (4)
# ====================================================================================

# 9. Bulk RNA‑seq — Expression Atlas (metadata)
@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        js = await _get_json(client, url)
    exps = js.get("experiments", []) if isinstance(js, dict) else []
    return Evidence(status="OK", source="Expression Atlas", fetched_n=len(exps),
                    data={"condition": condition, "experiments": exps[:50]},
                    citations=[url], fetched_at=_now())

# 10. Bulk proteomics — PRIDE (metadata)
@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        js = await _get_json(client, url)
    projects = js if isinstance(js, list) else []
    return Evidence(status="OK", source="PRIDE", fetched_n=len(projects),
                    data{"condition": condition, "projects": projects[:50]},
                    citations=[url], fetched_at=_now())

# 11. Single‑cell & spatial — cellxgene (stub + citation)
@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="cellxgene/HCA (browser‑first)",
                    fetched_n=0, data={"condition": condition},
                    citations=["https://cellxgene.cziscience.com/", "https://data.humancellatlas.org/"],
                    fetched_at=_now())

# 12. Perturbation screens — DepMap ORCS (public summary)
@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="BioGRID‑ORCS/DepMap (UI‑first)",
                    fetched_n=0, data={"symbol": symbol},
                    citations=["https://orcs.thebiogrid.org/","https://depmap.org/portal/"], fetched_at=_now())

# ====================================================================================
# BUCKET 3 — Expression, Specificity & Localization (3)
# ====================================================================================

# 13. Baseline tissue & cell specificity — Expression Atlas (real)
@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    base = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, base)
    experiments = body.get("experiments", []) if isinstance(body, dict) else []
    rows = []
    for exp in experiments:
        for d in exp.get("data", []):
            rows.append({
                "experimentAccession": exp.get("experimentAccession"),
                "tissue": d.get("organismPart") or d.get("tissue"),
                "value": (d.get("expressions", [{}])[0].get("value") if d.get("expressions") else None)
            })
    return Evidence(status="OK", source="Expression Atlas", fetched_n=len(rows),
                    data{"symbol": symbol, "baseline": rows[:200]}, citations=[base], fetched_at=_now())

# 14. Protein localization & compartments — UniProt (API)
@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, url)
    entries = body.get("results", []) if isinstance(body, dict) else []
    return Evidence(status="OK" if entries else "NO_DATA", source="UniProt REST",
                    fetched_n=len(entries), data={"symbol": symbol, "entries": entries},
                    citations=[url], fetched_at=_now())

# 15. Inducibility & temporal regulation — GEO (stub)
@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(symbol: str, stimulus: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="GEO time-courses (download‑first)",
                    fetched_n=0, data={"symbol": symbol, "stimulus": stimulus},
                    citations=["https://www.ncbi.nlm.nih.gov/geo/"], fetched_at=_now())

# ====================================================================================
# BUCKET 4 — Mechanistic Wiring & Networks (3)
# ====================================================================================

# 16. Pathway membership & directionality — Reactome (API)
@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    search = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        s = await _get_json(client, search)
    hits = s.get("results", []) if isinstance(s, dict) else []
    pathways = [h for h in hits if (h.get("stId","").startswith("R-HSA") or "Pathway" in h.get("type",""))]
    return Evidence(status="OK", source="Reactome ContentService", fetched_n=len(pathways),
                    data={"symbol": symbol, "pathways": pathways[:100]}, citations=[search], fetched_at=_now())

# 17. Protein–protein interactions — STRING (API)
@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(symbol: str, cutoff: float = 0.9, limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    net_tpl = "https://string-db.org/api/json/network?identifiers={id}&species=9606"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        ids = await _get_json(client, map_url)
        if not ids:
            return Evidence(status="OK", source="STRING", fetched_n=0, data{"symbol": symbol, "edges": []}, citations=[map_url], fetched_at=_now())
        sid = ids[0].get("stringId")
        net = await _get_json(client, net_tpl.format(id=sid))
    edges = []
    for e in net:
        score = e.get("score") or e.get("combined_score")
        if score and float(score) >= cutoff:
            edges.append({"A": e.get("preferredName_A"), "B": e.get("preferredName_B"), "score": float(score)})
    return Evidence(status="OK", source="STRING REST", fetched_n=len(edges),
                    data{"symbol": symbol, "edges": edges[:limit]},
                    citations=[map_url, net_tpl.format(id=sid)], fetched_at=_now())

# 18. Intercellular ligand–receptor — OmniPath LR db (API)
@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, url)
    rows = body if isinstance(body, list) else []
    return Evidence(status="OK", source="OmniPath", fetched_n=len(rows),
                    data{"symbol": symbol, "ligrec": rows[:100]},
                    citations=[url], fetched_at=_now())

# ====================================================================================
# BUCKET 5 — Tractability & Modality (6)
# ====================================================================================

# 19. Druggability class & precedent — Open Targets Platform GraphQL (known drugs)
@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    q = {
        "query": """
        query Q($sym:String!){
          target(approvedSymbol:$sym){
            id approvedSymbol
            knownDrugs{ rows{ drugType drug{ id name } disease{ id name } phase } }
          }
        }""",
        "variables": {"sym": symbol}
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _post_json(client, gql, q)
    target = body.get("data",{}).get("target")
    rows = (target or {}).get("knownDrugs",{}).get("rows",[]) if target else []
    return Evidence(status="OK", source="OpenTargets Platform GraphQL",
                    fetched_n=len(rows), data{"symbol": symbol, "knownDrugs": rows[:200]},
                    citations=[gql], fetched_at=_now())

# 20. Small‑molecule ligandability — PDB (metadata; real), FTMap/P2Rank (stub)
@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps({
        "query": {"type":"terminal","service":"text","parameters":{"value":symbol}},
        "request_options":{"return_all_hits":True},"return_type":"entry"
    }))
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, url)
    hits = body.get("result_set", []) if isinstance(body, dict) else []
    return Evidence(status="OK", source="RCSB PDB (search)", fetched_n=len(hits),
                    data{"symbol": symbol, "pdb_hits": hits[:100]},
                    citations=["https://www.rcsb.org/"], fetched_at=_now())

# 21. Antibody epitope ligandability — UniProt topology (API)
@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, url)
    entries = body.get("results", []) if isinstance(body, dict) else []
    return Evidence(status="OK" if entries else "NO_DATA", source="UniProt REST",
                    fetched_n=len(entries), data{"symbol": symbol, "entries": entries},
                    citations=[url], fetched_at=_now())

# 22. Oligonucleotide ligandability — RNAcentral (stub + citation)
@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="RNAcentral/TargetScan (design step required)",
                    fetched_n=0, data{"symbol": symbol},
                    citations=["https://rnacentral.org/","https://www.targetscan.org/"], fetched_at=_now())

# 23. Modality feasibility — UniProt + surface catalogs (stub)
@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="UniProt + SURFY/CSPA (design rules)",
                    fetched_n=0, data{"symbol": symbol},
                    citations=["https://rest.uniprot.org/"], fetched_at=_now())

# 24. Immunogenicity — IEDB labels (simple API) + stub
@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="IEDB (API available); labels summary",
                    fetched_n=0, data{"symbol": symbol},
                    citations["https://www.iedb.org/"], fetched_at=_now())

# ====================================================================================
# BUCKET 6 — Clinical Translation & Safety (4)
# ====================================================================================

# 25. Clinical endpoints / biomarkers — ClinicalTrials.gov search fields (real)
@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&fields=protocolSection.outcomesModule"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        js = await _get_json(client, url)
    studies = js.get("studies", []) if isinstance(js, dict) else []
    return Evidence(status="OK", source="ClinicalTrials.gov v2",
                    fetched_n=len(studies), data{"condition": condition, "studies": studies[:50]},
                    citations[url], fetched_at=_now())

# 26. Real‑world evidence — (stub + citations)
@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="FDA Sentinel/N3C/SEER (access‑controlled)",
                    fetched_n=0, data{"condition": condition},
                    citations["https://www.sentinelinitiative.org/","https://covid.cd2h.org/N3C","https://seer.cancer.gov/"],
                    fetched_at=_now())

# 27. On‑target safety, essentiality & class AEs — openFDA FAERS (simple)
@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(drug: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    url = f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug)}&count=patient.reaction.reactionmeddrapt.exact"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=6.0)) as client:
        try:
            js = await _get_json(client, url)
            results = js.get("results", []) if isinstance(js, dict) else []
            return Evidence(status="OK", source="openFDA FAERS",
                            fetched_n=len(results), data{"drug": drug, "reactions": results[:100]},
                            citations[url], fetched_at=_now())
        except Exception:
            return Evidence(status="NO_DATA", source="openFDA FAERS", fetched_n=0,
                            data{"drug": drug}, citations[url], fetched_at=_now())

# 28. Clinical evidence & pipeline signal — Open Targets Platform (reuse knownDrugs)
@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    # reuse /tract/drugs for pipeline summary; alias
    return await tract_drugs(symbol, x_api_key)

# ====================================================================================
# BUCKET 7 — Competition & IP (qualitative) (2)
# ====================================================================================

# 29. Competition intensity — counts from Open Targets & ClinicalTrials.gov
@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(symbol: str, condition: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    q = {"query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }",
         "variables": {"sym": symbol}}
    ct = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition or '')}&countTotal=true"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        ot = await _post_json(client, gql, q)
        trials = await _get_json(client, ct) if condition else {"totalStudies": None}
    count_kd = (((ot.get("data",{}) or {}).get("target") or {}).get("knownDrugs") or {}).get("count")
    total_trials = trials.get("totalStudies")
    return Evidence(status="OK", source="OpenTargets Platform + ClinicalTrials.gov",
                    fetched_n=1, data{"symbol": symbol, "knownDrugsCount": count_kd, "trialCount": total_trials, "condition": condition},
                    citations[gql, ct], fetched_at=_now())

# 30. Freedom‑to‑operate (IP landscape) — (stub + citations)
@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(query: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    return Evidence(status="NO_DATA", source="The Lens / Espacenet (API keys or scraping)",
                    fetched_n=0, data{"query": query},
                    citations["https://www.lens.org/","https://worldwide.espacenet.com/"], fetched_at=_now())
from app.clients.sources import (
    ot_genetics_l2g, expression_atlas_gene, string_map_and_network,
    reactome_search, ctgov_studies_outcomes, ot_platform_known_drugs
)
from app.utils.evidence import Evidence

@router.get("/report/run")
async def report_run(gene: str, efo: str, symbol: str, condition: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)

    # fan out to 5 high-value modules (you can add more later)
    tasks = [
        ot_genetics_l2g(gene, efo),                         # genetics/l2g
        expression_atlas_gene(symbol),                      # expr/baseline
        string_map_and_network(symbol),                     # mech/ppi
        reactome_search(symbol),                            # mech/pathways
        ctgov_studies_outcomes(condition),                  # clin/endpoints
    ]
    kd_task = ot_platform_known_drugs(symbol)              # tract/drugs

    res_l2g, res_expr, res_ppi, res_path, res_ct = await asyncio.gather(*tasks)
    res_kd = await kd_task

    # normalize into Evidence records (and ledger entries)
    out: Dict[str, Evidence] = {}

    # L2G
    l2g = Evidence(status="OK", source="OpenTargets Genetics", fetched_n=len(res_l2g["coloc"]),
                   data={"gene": gene, "efo": efo, "results": res_l2g["coloc"]},
                   citations=res_l2g["citations"], fetched_at=time.time())
    await ledger.add("genetics/l2g", l2g.status, l2g.fetched_n, l2g.source); out["genetics_l2g"] = l2g

    # Expression baseline (summarize quickly)
    expr_rows = []
    for exp in (res_expr["raw"].get("experiments", []) if isinstance(res_expr.get("raw"), dict) else []):
        for d in exp.get("data", []):
            expr_rows.append({
                "experimentAccession": exp.get("experimentAccession"),
                "tissue": d.get("organismPart") or d.get("tissue"),
                "value": (d.get("expressions", [{}])[0].get("value") if d.get("expressions") else None)
            })
    expr = Evidence(status="OK", source="Expression Atlas", fetched_n=len(expr_rows),
                    data={"symbol": symbol, "baseline": expr_rows[:200]},
                    citations=res_expr["citations"], fetched_at=time.time())
    await ledger.add("expr/baseline", expr.status, expr.fetched_n, expr.source); out["expr_baseline"] = expr

    # PPI
    edges = res_ppi["edges"]
    ppi = Evidence(status="OK", source="STRING REST", fetched_n=len(edges),
                   data={"symbol": symbol, "edges": edges[:100]},
                   citations=res_ppi["citations"], fetched_at=time.time())
    await ledger.add("mech/ppi", ppi.status, ppi.fetched_n, ppi.source); out["mech_ppi"] = ppi

    # Pathways
    hits = [h for h in res_path["results"] if (h.get("stId","").startswith("R-HSA") or "Pathway" in h.get("type",""))]
    path = Evidence(status="OK", source="Reactome ContentService", fetched_n=len(hits),
                    data={"symbol": symbol, "pathways": hits[:100]},
                    citations=res_path["citations"], fetched_at=time.time())
    await ledger.add("mech/pathways", path.status, path.fetched_n, path.source); out["mech_pathways"] = path

    # Clinical endpoints
    studies = res_ct["studies"]
    clin = Evidence(status="OK", source="ClinicalTrials.gov v2", fetched_n=len(studies),
                    data={"condition": condition, "studies": studies[:50]},
                    citations=res_ct["citations"], fetched_at=time.time())
    await ledger.add("clin/endpoints", clin.status, clin.fetched_n, clin.source); out["clin_endpoints"] = clin

    # Known drugs / pipeline
    kd_rows = res_kd["knownDrugs"]
    kd = Evidence(status="OK", source="OpenTargets Platform GraphQL", fetched_n=len(kd_rows),
                  data={"symbol": symbol, "knownDrugs": kd_rows[:200], "count": res_kd.get("count")},
                  citations=res_kd["citations"], fetched_at=time.time())
    await ledger.add("tract/drugs", kd.status, kd.fetched_n, kd.source); out["tract_drugs"] = kd

    # Tiny roll-up so you can plug into TargetVal scorecard later
    summary = {k: {"status": v.status, "n": v.fetched_n} for k, v in out.items()}
    return {"summary": summary, "modules": {k: v.dict() for k,v in out.items()}}
