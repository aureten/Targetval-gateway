"""
Entry point for the TARGETVAL gateway.

- Mounts the targetval_router at /v1 (so /v1/status, /v1/genetics/l2g, etc. are live)
- Serves a plugin manifest at /.well-known/ai-plugin.json
- Enables permissive CORS
- Provides GitHub live-data endpoints
- Aggregator /v1/targetval concurrently calls router module functions
"""

import asyncio
import os
import time
import urllib.parse
from typing import Dict, List, Optional, Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Mount router with all module endpoints
from app.routers import targetval_router
from app.routers.targetval_router import Evidence as RouterEvidence

# --------------------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------------------

app = FastAPI(title="TARGETVAL Gateway", version="0.3.2")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount ALL routes from targetval_router at /v1
app.include_router(targetval_router.router, prefix="/v1")

# --------------------------------------------------------------------------------------
# Plugin manifest for ChatGPT (adjust URLs to your repo)
# --------------------------------------------------------------------------------------

PLUGIN_MANIFEST: Dict[str, Any] = {
    "schema_version": "v1",
    "name_for_human": "TargetVal Gateway",
    "name_for_model": "targetval_gateway",
    "description_for_human": (
        "Fetches target validation evidence across genetics, expression, "
        "pathways, tractability, clinical, IP, and GitHub live data modules."
    ),
    "description_for_model": (
        "Use this plugin to query the TargetVal Gateway for live evidence on "
        "human genes and diseases. Provide a gene symbol or Ensembl ID, and "
        "a disease name or EFO ID, to receive a structured list of evidence "
        "objects across genetics, expression, clinical, and GitHub modules."
    ),
    "auth": {"type": "none"},
    "api": {
        "type": "openapi",
        "url": "https://raw.githubusercontent.com/aureten/Targetval-gateway/main/openapi.json",
    },
    "logo_url": "https://raw.githubusercontent.com/aureten/Targetval-gateway/main/logo.png",
    "contact_email": "your-email@example.com",
    "legal_info_url": "https://raw.githubusercontent.com/aureten/Targetval-gateway/main/legal.html",
}

@app.get("/.well-known/ai-plugin.json", include_in_schema=False)
def serve_ai_plugin() -> JSONResponse:
    return JSONResponse(PLUGIN_MANIFEST)

# --------------------------------------------------------------------------------------
# Optional root ping
# --------------------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "targetval-gateway", "time": time.time()}

# --------------------------------------------------------------------------------------
# Simple ClinicalTrials.gov proxy (convenience)
# --------------------------------------------------------------------------------------

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: List[str]
    fetched_at: float

@app.get("/clinical/ctgov", response_model=Evidence)
async def ctgov(condition: str) -> Evidence:
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

# --------------------------------------------------------------------------------------
# Helper for aggregator
# --------------------------------------------------------------------------------------

async def safe_call(coro) -> RouterEvidence:
    try:
        return await coro
    except HTTPException as e:
        return RouterEvidence(status="ERROR", source=str(e.detail), fetched_n=0, data={}, citations=[], fetched_at=time.time())
    except Exception as e:
        return RouterEvidence(status="ERROR", source=str(e), fetched_n=0, data={}, citations=[], fetched_at=time.time())

# --------------------------------------------------------------------------------------
# GitHub live-data endpoints
# --------------------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

def _gh_headers() -> Dict[str, str]:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

@app.get("/v1/github/commits")
async def github_commits(owner: str, repo: str):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_gh_headers())
        r.raise_for_status()
        return RouterEvidence(status="OK", source=url, fetched_n=len(r.json()), data={"commits": r.json()}, citations=[url], fetched_at=time.time())

@app.get("/v1/github/issues")
async def github_issues(owner: str, repo: str):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_gh_headers(), params={"state": "open"})
        r.raise_for_status()
        return RouterEvidence(status="OK", source=url, fetched_n=len(r.json()), data={"issues": r.json()}, citations=[url], fetched_at=time.time())

@app.get("/v1/github/releases")
async def github_releases(owner: str, repo: str):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_gh_headers())
        r.raise_for_status()
        return RouterEvidence(status="OK", source=url, fetched_n=len(r.json()), data={"releases": r.json()}, citations=[url], fetched_at=time.time())
