"""
Entry point for the TARGETVAL gateway.

- Mounts the targetval_router at /v1 (so /v1/status, /v1/genetics/l2g, etc. are live)
- Serves a plugin manifest at /.well-known/ai-plugin.json
- Enables permissive CORS for browser-based clients (e.g., ChatGPT)
- Provides GitHub live-data endpoints
- Aggregator /v1/targetval concurrently calls router module functions

NOTE: All API-key enforcement removed for public operation.
"""

from __future__ import annotations

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

# Mount the router that implements /status, /genetics/*, /expr/*, /mech/*, /tract/*, /clin/*, /comp/*, /github/releases
from app.routers import targetval_router

# Import router functions so the aggregator can call them directly
from app.routers.targetval_router import (
    Evidence as RouterEvidence,
    genetics_l2g,
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
    expression_baseline,
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

# --------------------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------------------

app = FastAPI(title="TARGETVAL Gateway", version="0.3.1")

# Enable CORS (permissive; tighten if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount ALL routes from targetval_router at /v1
app.include_router(targetval_router.router, prefix="/v1")

# --------------------------------------------------------------------------------------
# Plugin manifest for ChatGPT (adjust URLs to your repo/artifacts)
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
# Local Evidence model (used only for the simple ctgov proxy below)
# --------------------------------------------------------------------------------------

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: List[str]
    fetched_at: float

# --------------------------------------------------------------------------------------
# Optional root ping (useful to verify which app is running)
# --------------------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "targetval-gateway", "time": time.time()}

# --------------------------------------------------------------------------------------
# Simple ClinicalTrials.gov proxy (kept for convenience; not part of /v1 router)
# --------------------------------------------------------------------------------------

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
# GitHub live-data endpoints (under /v1 already exist in router for releases; we keep
# commits/issues here under /v1/github/* so you can hit them directly)
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
        data = r.json()
        return RouterEvidence(status="OK", source=url, fetched_n=len(data), data={"commits": data}, citations=[url], fetched_at=time.time())

@app.get("/v1/github/issues")
async def github_issues(owner: str, repo: str):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_gh_headers(), params={"state": "open"})
        r.raise_for_status()
        data = r.json()
        return RouterEvidence(status="OK", source=url, fetched_n=len(data), data={"issues": data}, citations=[url], fetched_at=time.time())

@app.get("/v1/github/releases")
async def github_releases(owner: str, repo: str):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_gh_headers())
        r.raise_for_status()
        data = r.json()
        return RouterEvidence(status="OK", source=url, fetched_n=len(data), data={"releases": data}, citations=[url], fetched_at=time.time())

# --------------------------------------------------------------------------------------
# Module → bucket map for aggregator output
# --------------------------------------------------------------------------------------

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
    "github_commits": "GitHub Live Data",
    "github_issues": "GitHub Live Data",
    "github_releases": "GitHub Live Data",
}

# --------------------------------------------------------------------------------------
# Aggregator: /v1/targetval
# --------------------------------------------------------------------------------------

@app.get("/v1/targetval")
async def targetval(
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    condition: Optional[str] = None,
    efo_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Aggregate evidence across all modules and GitHub endpoints.

    Requires a gene (symbol or Ensembl) and a condition (name or EFO ID).
    """
    gene = ensembl_id or symbol
    efo  = efo_id or condition
    if not gene or not efo:
        raise HTTPException(status_code=400, detail="Must supply gene (symbol or ensembl_id) and condition (efo_id or condition).")

    # Build task map (use explicit kwargs to avoid param mismatches)
    tasks: Dict[str, asyncio.Future] = {
        "genetics_l2g":            safe_call(genetics_l2g(gene=gene, efo=efo)),
        "genetics_rare":           safe_call(genetics_rare(gene=gene)),
        "genetics_mendelian":      safe_call(genetics_mendelian(gene=gene, efo=efo)),
        "genetics_mr":             safe_call(genetics_mr(gene=gene, efo=efo)),
        "genetics_lncrna":         safe_call(genetics_lncrna(gene=gene)),
        "genetics_mirna":          safe_call(genetics_mirna(gene=gene)),
        "genetics_sqtl":           safe_call(genetics_sqtl(gene=gene, efo=efo)),
        "genetics_epigenetics":    safe_call(genetics_epigenetics(gene=gene, efo=efo)),
        "assoc_bulk_rna":          safe_call(assoc_bulk_rna(condition=condition or "")),
        "assoc_bulk_prot":         safe_call(assoc_bulk_prot(condition=condition or "")),
        "assoc_sc":                safe_call(assoc_sc(condition=condition or "")),
        "assoc_perturb":           safe_call(assoc_perturb(condition=condition or "")),
        "expression_baseline":     safe_call(expression_baseline(symbol=gene)),
        "expr_localization":       safe_call(expr_localization(symbol=gene)),
        "expr_inducibility":       safe_call(expr_inducibility(symbol=gene)),
        "mech_pathways":           safe_call(mech_pathways(symbol=gene)),
        "mech_ppi":                safe_call(mech_ppi(symbol=gene, cutoff=0.9, limit=50)),
        "mech_ligrec":             safe_call(mech_ligrec(symbol=gene)),
        "tract_drugs":             safe_call(tract_drugs(symbol=gene)),
        "tract_ligandability_sm":  safe_call(tract_ligandability_sm(symbol=gene)),
        "tract_ligandability_ab":  safe_call(tract_ligandability_ab(symbol=gene)),
        "tract_ligandability_oligo": safe_call(tract_ligandability_oligo(symbol=gene)),
        "tract_modality":          safe_call(tract_modality(symbol=gene)),
        "tract_immunogenicity":    safe_call(tract_immunogenicity(symbol=gene)),
        "clin_endpoints":          safe_call(clin_endpoints(condition=condition or "")),
        "clin_rwe":                safe_call(clin_rwe(condition=condition or "")),
        "clin_safety":             safe_call(clin_safety(symbol=gene)),
        "clin_pipeline":           safe_call(clin_pipeline(symbol=gene)),
        "comp_intensity":          safe_call(comp_intensity(symbol=gene, condition=condition)),
        "comp_freedom":            safe_call(comp_freedom(symbol=gene)),
        # GitHub — hard-code your repo for now (or make params)
        "github_commits":          safe_call(github_commits(owner="aureten", repo="Targetval-gateway")),
        "github_issues":           safe_call(github_issues(owner="aureten", repo="Targetval-gateway")),
        "github_releases":         safe_call(github_releases(owner="aureten", repo="Targetval-gateway")),
    }

    results = await asyncio.gather(*tasks.values())
    evidence_list: List[Dict[str, Any]] = []
    for name, evidence in zip(tasks.keys(), results):
        evidence_list.append({
            "module": name,
            "bucket": MODULE_BUCKET_MAP.get(name, "Unknown"),
            "status": evidence.status,
            "fetched_n": evidence.fetched_n,
            "data": evidence.data,
            "citations": evidence.citations,
            "fetched_at": evidence.fetched_at,
        })

    return {
        "target": {"symbol": symbol, "ensembl_id": ensembl_id},
        "context": {"condition": condition, "efo_id": efo_id},
        "evidence": evidence_list,
    }
