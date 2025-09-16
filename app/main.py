import os
import time
import urllib.parse
import asyncio
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import httpx
from typing import Optional, List, Dict

# Import other module functions from the targetval router
from app.routers.targetval_router import (
    genetics_rare,
    genetics_mendelian,
    genetics_mr,
    genetics_lncrna,
    genetics_mirna,
    genetics_sqtl,
    genetics_epigenetics,
    assoc_bulk_rna,
    assoc_bulk_prot,
    assoc_sc,
    assoc_perturb,
    expr_localization,
    expr_inducibility,
    mech_pathways,
    mech_ppi,
    mech_ligrec,
    tract_drugs,
    tract_ligandability_sm,
    tract_ligandability_ab,
    tract_ligandability_oligo,
    tract_modality,
    tract_immunogenicity,
    clin_endpoints,
    clin_rwe,
    clin_safety,
    clin_pipeline,
    comp_intensity,
    comp_freedom,
)

API_KEY = os.getenv("API_KEY")

app = FastAPI(title="TARGETVAL Gateway", version="0.1.1")


class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: List[str]
    fetched_at: float


def require_key(x_api_key: str | None):
    """API key enforcement disabled; all requests are allowed."""
    return


# ---------- Health endpoint (PUBLIC) ----------
@app.get("/v1/health")
def health():
    return {"ok": True, "time": time.time()}


# ---------- 0) ClinicalTrials.gov ----------
@app.get("/clinical/ctgov", response_model=Evidence)
async def ctgov(condition: str, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    base = "https://clinicaltrials.gov/api/v2/studies"
    q = f"{base}?query.cond={urllib.parse.quote(condition)}&pageSize=3"
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=3.0)) as client:
        for wait in (0.5, 1.0, 2.0):
            try:
                r = await client.get(q)
                r.raise_for_status()
                studies = r.json().get("studies", [])
                return Evidence(
                    status="OK",
                    source="ClinicalTrials.gov v2",
                    fetched_n=len(studies),
                    data={"studies": studies},
                    citations=[q],
                    fetched_at=time.time(),
                )
            except Exception:
                await asyncio.sleep(wait)
    raise HTTPException(status_code=500, detail="Failed to fetch studies")


# ---------- Helpers ----------
async def _get_json(client, url, max_tries=3):
    last = None
    for _ in range(max_tries):
        try:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            await asyncio.sleep(0.8)
    raise last


def _now():
    return time.time()


# ---------- Safe call wrapper ----------
async def safe_call(coro):
    """Call a module coroutine and return an Evidence object, catching exceptions."""
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


# ---------- Bucket map ----------
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


# ---------- 1) Open Targets Genetics: L2G ----------
@app.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(
    gene: str, efo: str, x_api_key: Optional[str] = Header(default=None)
):
    require_key(x_api_key)
    base_gql = "https://genetics.opentargets.org/graphql"
    query = {
        "query": """
        query Q($geneId:String!, $efoId:String!){
          target(ensemblId:$geneId){ id approvedSymbol }
          disease(efoId:$efoId){ id name }
          colocalisationByGeneAndDisease(geneId:$geneId, efoId:$efoId){
            studyId phenotypeId geneId diseaseId yProbaModel yProbaCc
            hasColoc hasColocConsensus
          }
        }""",
        "variables": {"geneId": gene, "efoId": efo},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        try:
            r = await client.post(base_gql, json=query)
            r.raise_for_status()
            body = r.json()
            coloc = (
                body.get("data", {}).get("colocalisationByGeneAndDisease", []) or []
            )
            return Evidence(
                status="OK",
                source="OpenTargets Genetics GraphQL",
                fetched_n=len(coloc),
                data={"gene": gene, "efo": efo, "results": coloc},
                citations=[base_gql],
                fetched_at=_now(),
            )
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"OpenTargets Genetics failed: {e}"
            )


# ---------- 2) Expression Atlas: baseline ----------
@app.get("/expression/baseline", response_model=Evidence)
async def expression_baseline(
    symbol: str, x_api_key: Optional[str] = Header(default=None)
):
    require_key(x_api_key)
    base = f"https://www.ebi.ac.uk/gxa/genes/{symbol}.json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, base)
        results: List[Dict] = []
        try:
            experiments = body.get("experiments", [])
            for exp in experiments:
                for d in exp.get("data", []):
                    results.append(
                        {
                            "tissue": d.get("organismPart")
                            or d.get("tissue")
                            or "NA",
                            "level": d.get("expressions", [{}])[0].get("value"),
                        }
                    )
        except Exception:
            results = []
        return Evidence(
            status="OK",
            source="Expression Atlas (baseline)",
            fetched_n=len(results),
            data={"symbol": symbol, "baseline": results[:100]},
            citations=[base],
            fetched_at=_now(),
        )


# ---------- 3) STRING: PPI neighbors ----------
@app.get("/ppi/string", response_model=Evidence)
async def ppi_string(
    symbol: str,
    cutoff: float = 0.9,
    limit: int = 50,
    x_api_key: Optional[str] = Header(default=None),
):
    require_key(x_api_key)
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={symbol}&species=9606"
    network_url_tpl = (
        "https://string-db.org/api/json/network?identifiers={id}&species=9606"
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        ids = await _get_json(client, map_url)
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
        net = await _get_json(client, network_url_tpl.format(id=string_id))
        neighbors: List[Dict] = []
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


# ---------- 4) Reactome pathways ----------
@app.get("/pathways/reactome", response_model=Evidence)
async def pathways_reactome(
    symbol: str, x_api_key: Optional[str] = Header(default=None)
):
    require_key(x_api_key)
    search = f"https://reactome.org/ContentService/search/query?query={symbol}&species=Homo%20sapiens"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        s = await _get_json(client, search)
        hits = s.get("results", []) if isinstance(s, dict) else []
        pathways: List[Dict] = []
        for h in hits:
            if "Pathway" in h.get("species", "") or h.get("stId", "").startswith(
                "R-HSA"
            ):
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
            data={"symbol": symbol, "pathways": pathways[:50]},
            citations=[search],
            fetched_at=_now(),
        )


# ---------- 5) Immunogenicity (labels stub) ----------
@app.get("/immunogenicity/labels", response_model=Evidence)
async def immunogenicity_labels(
    symbol: str, x_api_key: Optional[str] = Header(default=None)
):
    require_key(x_api_key)
    ada_table = {
        "ADALIMUMAB": {
            "ada_incidence": "highly variable across indications (10–30%+)",
            "notes": "Human mAb; concomitant MTX reduces ADA",
        },
        "INFLIXIMAB": {
            "ada_incidence": "common without concomitant immunosuppression",
            "notes": "Chimeric mAb; infusion reactions correlate with ADA",
        },
    }
    item = ada_table.get(symbol.upper())
    return Evidence(
        status="OK",
        source="Label summaries (stub)",
        fetched_n=1 if item else 0,
        data={"symbol": symbol, "label": item},
        citations=[],
        fetched_at=_now(),
    )


# ---------- Aggregated endpoint: /v1/targetval ----------
@app.get("/v1/targetval")
async def targetval(
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    condition: Optional[str] = None,
    efo_id: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Aggregate evidence across all modules.
    Provide either symbol or ensembl_id (gene identifier), and either condition or efo_id (disease identifier).
    """
    require_key(x_api_key)
    gene = ensembl_id or symbol
    efo = efo_id or condition
    if gene is None or efo is None:
        raise HTTPException(
            status_code=400,
            detail="Must supply gene (symbol or ensembl_id) and condition (efo_id or condition).",
        )

    # Dispatch module calls concurrently
    tasks = {
        "genetics_l2g": safe_call(genetics_l2g(gene, efo, x_api_key)),
        "genetics_rare": safe_call(genetics_rare(gene, x_api_key)),
        "genetics_mendelian": safe_call(genetics_mendelian(gene, efo, x_api_key)),
        "genetics_mr": safe_call(genetics_mr(gene, efo, x_api_key)),
        "genetics_lncrna": safe_call(genetics_lncrna(gene, x_api_key)),
        "genetics_mirna": safe_call(genetics_mirna(gene, x_api_key)),
        "genetics_sqtl": safe_call(genetics_sqtl(gene, efo, x_api_key)),
        "genetics_epigenetics": safe_call(genetics_epigenetics(gene, efo, x_api_key)),
        "assoc_bulk_rna": safe_call(assoc_bulk_rna(condition, x_api_key)),
        "assoc_bulk_prot": safe_call(assoc_bulk_prot(condition, x_api_key)),
        "assoc_sc": safe_call(assoc_sc(condition, x_api_key)),
        "assoc_perturb": safe_call(assoc_perturb(condition, x_api_key)),
        "expression_baseline": safe_call(expression_baseline(symbol, x_api_key)),
        "expr_localization": safe_call(expr_localization(symbol, x_api_key)),
        "expr_inducibility": safe_call(expr_inducibility(symbol, x_api_key)),
        "mech_pathways": safe_call(mech_pathways(symbol, x_api_key)),
        "mech_ppi": safe_call(mech_ppi(symbol, 0.9, 50, x_api_key)),
        "mech_ligrec": safe_call(mech_ligrec(symbol, x_api_key)),
        "tract_drugs": safe_call(tract_drugs(symbol, x_api_key)),
        "tract_ligandability_sm": safe_call(tract_ligandability_sm(symbol, x_api_key)),
        "tract_ligandability_ab": safe_call(tract_ligandability_ab(symbol, x_api_key)),
        "tract_ligandability_oligo": safe_call(tract_ligandability_oligo(symbol, x_api_key)),
        "tract_modality": safe_call(tract_modality(symbol, x_api_key)),
        "tract_immunogenicity": safe_call(tract_immunogenicity(symbol, x_api_key)),
        "clin_endpoints": safe_call(clin_endpoints(condition, x_api_key)),
        "clin_rwe": safe_call(clin_rwe(condition, x_api_key)),
        "clin_safety": safe_call(clin_safety(symbol, x_api_key)),
        "clin_pipeline": safe_call(clin_pipeline(symbol, x_api_key)),
        "comp_intensity": safe_call(comp_intensity(symbol, condition, x_api_key)),
        "comp_freedom": safe_call(comp_freedom(symbol, x_api_key)),
    }

    results = await asyncio.gather(*tasks.values())

    # Assemble response
    evidence_list: List[Dict] = []
    for name, evidence in zip(tasks.keys(), results):
        evidence_list.append(
            {
                "module": name,
                "bucket": MODULE_BUCKET_MAP.get(name, "Unknown"),
                "status": evidence.status,
                "fetched_n": evidence.fetched_n,
                "data": evidence.data,
                "citations": evidence.citations,
                "fetched_at": evidence.fetched_at,
            }
        )

    return {
        "target": {"symbol": symbol, "ensembl_id": ensembl_id},
        "context": {"condition": condition, "efo_id": efo_id},
        "evidence": evidence_list,
    }
