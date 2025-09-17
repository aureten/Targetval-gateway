import os
import asyncio
import time
import urllib.parse
from typing import Dict, List, Optional, Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.routers import targetval_router as tv

API_KEY = os.getenv("API_KEY")

app = FastAPI(title="TARGETVAL Gateway", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tv.router, prefix="/v1")

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: List[str]
    fetched_at: float

def require_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

@app.get("/")
def root() -> Dict[str, Any]:
    return {"service": "targetval-gateway", "ok": True, "time": time.time()}

@app.get("/v1/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": time.time()}

@app.get("/clinical/ctgov", response_model=Evidence)
async def ctgov(condition: str, x_api_key: Optional[str] = Header(default=None)) -> Evidence:
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

async def _safe_call(coro) -> tv.Evidence:
    try:
        return await coro
    except HTTPException as e:
        return tv.Evidence(
            status="ERROR",
            source=str(e.detail),
            fetched_n=0,
            data={},
            citations=[],
            fetched_at=time.time(),
        )
    except Exception as e:
        return tv.Evidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={},
            citations=[],
            fetched_at=time.time(),
        )

MODULE_BUCKET_MAP: Dict[str, str] = tv.MODULE_BUCKET_MAP

@app.get("/v1/targetval")
async def targetval(
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    condition: Optional[str] = None,
    efo_id: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    require_key(x_api_key)
    gene = ensembl_id or symbol
    efo = efo_id or condition
    if gene is None or efo is None:
        raise HTTPException(
            status_code=400,
            detail="Must supply gene (symbol or ensembl_id) and condition (efo_id or condition).",
        )
    tasks: Dict[str, asyncio.Future] = {
        "genetics_l2g": _safe_call(tv.genetics_l2g(gene, efo, x_api_key)),
        "genetics_rare": _safe_call(tv.genetics_rare(gene, x_api_key)),
        "genetics_mendelian": _safe_call(tv.genetics_mendelian(gene, efo, x_api_key)),
        "genetics_mr": _safe_call(tv.genetics_mr(gene, efo, x_api_key)),
        "genetics_lncrna": _safe_call(tv.genetics_lncrna(gene, x_api_key)),
        "genetics_mirna": _safe_call(tv.genetics_mirna(gene, x_api_key)),
        "genetics_sqtl": _safe_call(tv.genetics_sqtl(gene, efo, x_api_key)),
        "genetics_epigenetics": _safe_call(tv.genetics_epigenetics(gene, efo, x_api_key)),
        "assoc_bulk_rna": _safe_call(tv.assoc_bulk_rna(condition or efo, x_api_key)),
        "assoc_bulk_prot": _safe_call(tv.assoc_bulk_prot(condition or efo, x_api_key)),
        "assoc_sc": _safe_call(tv.assoc_sc(condition or efo, x_api_key)),
        "assoc_perturb": _safe_call(tv.assoc_perturb(condition or efo, x_api_key)),
        "expression_baseline": _safe_call(tv.expression_baseline(symbol or gene, x_api_key)),
        "expr_localization": _safe_call(tv.expr_localization(symbol or gene, x_api_key)),
        "expr_inducibility": _safe_call(tv.expr_inducibility(symbol or gene, x_api_key)),
        "mech_pathways": _safe_call(tv.mech_pathways(symbol or gene, x_api_key)),
        "mech_ppi": _safe_call(tv.mech_ppi(symbol or gene, 0.9, 50, x_api_key)),
        "mech_ligrec": _safe_call(tv.mech_ligrec(symbol or gene, x_api_key)),
        "tract_drugs": _safe_call(tv.tract_drugs(symbol or gene, x_api_key)),
        "tract_ligandability_sm": _safe_call(tv.tract_ligandability_sm(symbol or gene, x_api_key)),
        "tract_ligandability_ab": _safe_call(tv.tract_ligandability_ab(symbol or gene, x_api_key)),
        "tract_ligandability_oligo": _safe_call(tv.tract_ligandability_oligo(symbol or gene, x_api_key)),
        "tract_modality": _safe_call(tv.tract_modality(symbol or gene, x_api_key)),
        "tract_immunogenicity": _safe_call(tv.tract_immunogenicity(symbol or gene, x_api_key)),
        "clin_endpoints": _safe_call(tv.clin_endpoints(condition or efo, x_api_key)),
        "clin_rwe": _safe_call(tv.clin_rwe(condition or efo, x_api_key)),
        "clin_safety": _safe_call(tv.clin_safety(symbol or gene, x_api_key)),
        "clin_pipeline": _safe_call(tv.clin_pipeline(symbol or gene, x_api_key)),
        "comp_intensity": _safe_call(tv.comp_intensity(symbol or gene, condition or efo, x_api_key)),
        "comp_freedom": _safe_call(tv.comp_freedom(symbol or gene, x_api_key)),
    }
    results = await asyncio.gather(*tasks.values())
    evidence_list: List[Dict[str, Any]] = []
    for name, ev in zip(tasks.keys(), results):
        evidence_list.append(
            {
                "module": name,
                "bucket": MODULE_BUCKET_MAP.get(name, "Unknown"),
                "status": ev.status,
                "fetched_n": ev.fetched_n,
                "data": ev.data,
                "citations": ev.citations,
                "fetched_at": ev.fetched_at,
            }
        )
    return {
        "target": {"symbol": symbol, "ensembl_id": ensembl_id},
        "context": {"condition": condition, "efo_id": efo_id},
        "evidence": evidence_list,
    }
