
import os, time, urllib.parse, asyncio
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import httpx

API_KEY = os.getenv("API_KEY")

app = FastAPI(title="TARGETVAL Gateway", version="0.1.0")

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: list[str]
    fetched_at: float

def require_key(x_api_key: str | None):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server missing API key")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")

@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}

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
# --- ADD BELOW your existing imports ---
from typing import Optional, List, Dict

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

# ---------- 1) Open Targets Genetics: L2G ----------
@app.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, efo: str, x_api_key: Optional[str] = Header(default=None)):
    require_key(x_api_key)
    # Open Targets Genetics GraphQL endpoint works best with POST, but we’ll
    # use the public REST "study-locus2gene" proxy for a quick summary where possible.
    # If not available, we return a stub with a citation to the GraphQL endpoint.
    base_gql = "https://genetics.opentargets.org/graphql"
    # Minimal payload (returned in data for transparency)
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
        "variables": {"geneId": gene, "efoId": efo}
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        try:
            r = await client.post(base_gql, json=query)
            r.raise_for_status()
            body = r.json()
            coloc = body.get("data", {}).get("colocalisationByGeneAndDisease", []) or []
            return Evidence(
                status="OK",
                source="OpenTargets Genetics GraphQL",
                fetched_n=len(coloc),
                data={"gene": gene, "efo": efo, "results": coloc},
                citations=[base_gql],
                fetched_at=_now(),
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"OpenTargets Genetics failed: {e}")

# ---------- 2) Expression Atlas: baseline ----------
@app.get("/expression/baseline", response_model=Evidence)
async def expression_baseline(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    require_key(x_api_key)
    # Expression Atlas JSON baseline endpoint (public) uses gene symbols.
    # It returns tissues and TPM-ish measures depending on dataset.
    base = f"https://www.ebi.ac.uk/gxa/genes/{symbol}.json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        body = await _get_json(client, base)
        # Summarize into (tissue, level) pairs if present
        results = []
        try:
            experiments = body.get("experiments", [])
            for exp in experiments:
                for d in exp.get("data", []):
                    results.append({
                        "tissue": d.get("organismPart") or d.get("tissue") or "NA",
                        "level": d.get("expressions", [{}])[0].get("value")
                    })
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
async def ppi_string(symbol: str, cutoff: float = 0.9, limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    require_key(x_api_key)
    # STRING alias to protein mapping (human)
    # 9606 = Homo sapiens
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={symbol}&species=9606"
    network_url_tpl = "https://string-db.org/api/json/network?identifiers={id}&species=9606"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        ids = await _get_json(client, map_url)
        if not ids:
            return Evidence(status="OK", source="STRING", fetched_n=0,
                            data={"symbol": symbol, "neighbors": []},
                            citations=[map_url], fetched_at=_now())
        string_id = ids[0].get("stringId")
        net = await _get_json(client, network_url_tpl.format(id=string_id))
        # filter by combined_score
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

# ---------- 4) Reactome pathways ----------
@app.get("/pathways/reactome", response_model=Evidence)
async def pathways_reactome(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    require_key(x_api_key)
    # Reactome: map UniProt or gene symbol to pathways (using ContentService)
    #  Human default
    search = f"https://reactome.org/ContentService/search/query?query={symbol}&species=Homo%20sapiens"
    details_tpl = "https://reactome.org/ContentService/data/pathways/low/diagram/{stableId}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=4.0)) as client:
        s = await _get_json(client, search)
        hits = s.get("results", []) if isinstance(s, dict) else []
        # Keep pathway-like hits
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

# ---------- 5) Immunogenicity (labels stub) ----------
@app.get("/immunogenicity/labels", response_model=Evidence)
async def immunogenicity_labels(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    require_key(x_api_key)
    # Stub: returns known high-level ADA mentions for monoclonals by simple key match
    # (We can replace with openFDA + EMA EPAR parsing later.)
    ada_table = {
        "ADALIMUMAB": {"ada_incidence": "highly variable across indications (10–30%+)", "notes": "Human mAb; concomitant MTX reduces ADA"},
        "INFLIXIMAB": {"ada_incidence": "common without concomitant immunosuppression", "notes": "Chimeric mAb; infusion reactions correlate with ADA"},
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
from app.routers.targetval_router import router as tv_router
app.include_router(tv_router, prefix="/v1")
