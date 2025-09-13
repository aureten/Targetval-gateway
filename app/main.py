import os
import time
import urllib.parse
import asyncio
from typing import Optional, List, Dict

from fastapi import FastAPI, APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel, BaseSettings
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    api_key: str = os.getenv("API_KEY")
    ctgov_base: str = "https://clinicaltrials.gov/api/v2/studies"
    opentargets_gql: str = "https://platform.opentargets.org/api/v4/graphql"
    gnomad_gql: str = "https://gnomad.broadinstitute.org/api/v2/graphql"
    expression_base: str = "https://www.ebi.ac.uk/gxa/genes"
    string_map_url: str = "https://string-db.org/api/json/get_string_ids"
    string_network_url: str = "https://string-db.org/api/json/network"
    reactome_search: str = "https://reactome.org/ContentService/search/query"
    reactome_detail_tpl: str = "https://reactome.org/ContentService/data/pathways/low/diagram/{stableId}"

settings = Settings()

# ---------------------------------------------------------------------------
# FastAPI app and models
# ---------------------------------------------------------------------------

app = FastAPI(title="TARGETVAL Gateway", version="0.2.0")
router_v1 = APIRouter()

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: List[str]
    fetched_at: float

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    """
    Dependency to validate the X-API-Key header. Raises 401/500 when missing
    or incorrect.
    """
    if not settings.api_key:
        raise HTTPException(status_code=500, detail="Server missing API key")
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")

async def _get_json(client: httpx.AsyncClient, url: str, max_tries: int = 3):
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

def _now() -> float:
    return time.time()

# ---------------------------------------------------------------------------
# Public health endpoint
# ---------------------------------------------------------------------------

@app.get("/v1/health")
def health() -> Dict[str, object]:
    """Simple health check."""
    return {"ok": True, "time": time.time()}

# ---------------------------------------------------------------------------
# Version 1 endpoints (require API key)
# ---------------------------------------------------------------------------

# 0) ClinicalTrials.gov
@router_v1.get("/clinical/ctgov", response_model=Evidence, dependencies=[Depends(verify_api_key)])
async def ctgov(condition: str):
    base = settings.ctgov_base
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
                    fetched_at=_now(),
                )
            except Exception:
                await asyncio.sleep(wait)
    raise HTTPException(status_code=502, detail="Failed to fetch studies from ClinicalTrials.gov")

# 1) Open Targets Genetics: L2G
@router_v1.get("/genetics/l2g", response_model=Evidence, dependencies=[Depends(verify_api_key)])
async def genetics_l2g(gene: str, efo: str):
    query = {
        "query": """
        query Q($geneId:String!, $efoId:String!){
          target(ensemblId:$geneId){ id approvedSymbol }
          disease(efoId:$efoId){ id name }
          colocalisationByGeneAndDisease(geneId:$geneId, efoId:$efoId){
            studyId phenotypeId geneId diseaseId yProbaModel yProbaCc
            hasColoc hasColocConsensus
          }
        }
        """,
        "variables": {"geneId": gene, "efoId": efo},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        try:
            r = await client.post(settings.opentargets_gql, json=query, follow_redirects=True)
            r.raise_for_status()
            body = r.json()
            coloc = body.get("data", {}).get("colocalisationByGeneAndDisease", []) or []
            return Evidence(
                status="OK",
                source="OpenTargets Genetics GraphQL",
                fetched_n=len(coloc),
                data={"gene": gene, "efo": efo, "results": coloc},
                citations=[settings.opentargets_gql],
                fetched_at=_now(),
            )
        except httpx.HTTPStatusError as e:
            # Pass through upstream status codes for better debugging
            raise HTTPException(status_code=e.response.status_code,
                                detail=f"OpenTargets Genetics error: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"OpenTargets Genetics failed: {e}")

# 2) Expression Atlas: baseline
@router_v1.get("/expression/baseline", response_model=Evidence, dependencies=[Depends(verify_api_key)])
async def expression_baseline(symbol: str):
    base = f"{settings.expression_base}/{symbol}.json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, base)
        results = []
        experiments = body.get("experiments", []) if isinstance(body, dict) else []
        for exp in experiments:
            for d in exp.get("data", []):
                results.append({
                    "tissue": d.get("organismPart") or d.get("tissue") or "NA",
                    "level": d.get("expressions", [{}])[0].get("value")
                })
        return Evidence(
            status="OK",
            source="Expression Atlas (baseline)",
            fetched_n=len(results),
            data={"symbol": symbol, "baseline": results[:100]},
            citations=[base],
            fetched_at=_now(),
        )

# 3) STRING: PPI neighbours
@router_v1.get("/ppi/string", response_model=Evidence, dependencies=[Depends(verify_api_key)])
async def ppi_string(symbol: str, cutoff: float = 0.9, limit: int = 50):
    map_url = f"{settings.string_map_url}?identifiers={symbol}&species=9606"
    network_url_tpl = f"{settings.string_network_url}?identifiers={{id}}&species=9606"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        ids = await _get_json(client, map_url)
        if not ids:
            return Evidence(status="OK", source="STRING", fetched_n=0,
                            data={"symbol": symbol, "neighbors": []},
                            citations=[map_url], fetched_at=_now())
        string_id = ids[0].get("stringId")
        net = await _get_json(client, network_url_tpl.format(id=string_id))
        neighbors = []
        for edge in net:
            score = edge.get("score") or edge.get("combined_score")
            if score and float(score) >= cutoff:
                neighbors.append({
                    "preferredName_A": edge.get("preferredName_A"),
                    "preferredName_B": edge.get("preferredName_B"),
                    "score": float(score),
                })
        neighbors = neighbors[:limit]
        return Evidence(
            status="OK",
            source="STRING REST",
            fetched_n=len(neighbors),
            data={"symbol": symbol, "neighbors": neighbors},
            citations=[map_url, network_url_tpl.format(id=string_id)],
            fetched_at=_now(),
        )

# 4) Reactome pathways
@router_v1.get("/pathways/reactome", response_model=Evidence, dependencies=[Depends(verify_api_key)])
async def pathways_reactome(symbol: str):
    search = f"{settings.reactome_search}?query={symbol}&species=Homo%20sapiens"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        s = await _get_json(client, search)
        hits = s.get("results", []) if isinstance(s, dict) else []
        pathways = []
        for h in hits:
            if "Pathway" in h.get("species", "") or h.get("stId", "").startswith("R-HSA"):
                pathways.append({
                    "name": h.get("name"),
                    "stId": h.get("stId"),
                    "score": h.get("score")
                })
        return Evidence(
            status="OK",
            source="Reactome ContentService",
            fetched_n=len(pathways),
            data={"symbol": symbol, "pathways": pathways[:50]},
            citations=[search],
            fetched_at=_now(),
        )

# 5) Immunogenicity (labels stub)
@router_v1.get("/immunogenicity/labels", response_model=Evidence, dependencies=[Depends(verify_api_key)])
async def immunogenicity_labels(symbol: str):
    ada_table = {
        "ADALIMUMAB": {"ada_incidence": "highly variable across indications (10–30%+)",
                       "notes": "Human mAb; concomitant MTX reduces ADA"},
        "INFLIXIMAB": {"ada_incidence": "common without concomitant immunosuppression",
                       "notes": "Chimeric mAb; infusion reactions correlate with ADA"},
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

# ---------------------------------------------------------------------------
# Register the router with a /v1 prefix
# ---------------------------------------------------------------------------

app.include_router(router_v1, prefix="/v1")
