"""
Entry point for the TARGETVAL gateway.

This FastAPI application exposes high‑level endpoints that aggregate
evidence across the module functions defined in
:mod:`app.routers.targetval_router`.  It mirrors the original
structure of the gateway while updating the data sources used by
individual modules to more reliable public APIs.  A convenience
endpoint ``/v1/targetval`` orchestrates concurrent calls across the
modules and collates the results into a single response.
"""

import asyncio
import os
import time
import urllib.parse
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# Import module functions from the router.  These endpoints wrap
# external APIs and return :class:`app.routers.targetval_router.Evidence`
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


API_KEY = os.getenv("API_KEY")

app = FastAPI(title="TARGETVAL Gateway", version="0.2.0")


class Evidence(BaseModel):
    """Mirror of :class:`app.routers.targetval_router.Evidence` for responses."""

    status: str
    source: str
    fetched_n: int
    data: dict
    citations: List[str]
    fetched_at: float


def require_key(x_api_key: Optional[str]) -> None:
    """API key check.  Disabled for public operation."""
    return


@app.get("/v1/health")
def health() -> Dict[str, float]:
    """Health endpoint returning current time."""
    return {"ok": True, "time": time.time()}


@app.get("/clinical/ctgov", response_model=Evidence)
async def ctgov(
    condition: str, x_api_key: Optional[str] = Header(default=None)
) -> Evidence:
    """Proxy to ClinicalTrials.gov for a given condition.

    Returns the first three studies retrieved from the v2 API.  Retries
    the request a few times on failure before raising an error.
    """
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


async def safe_call(coro) -> RouterEvidence:
    """Wrap a module coroutine call to return an Evidence object on error."""
    try:
        return await coro
    except HTTPException as e:
        return RouterEvidence(
            status="ERROR",
            source=str(e.detail),
            fetched_n=0,
            data={},
            citations=[],
            fetched_at=time.time(),
        )
    except Exception as e:
        return RouterEvidence(
            status="ERROR",
            source=str(e),
            fetched_n=0,
            data={},
            citations=[],
            fetched_at=time.time(),
        )


# Mapping from module name to bucket for aggregated responses.
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


@app.get("/v1/targetval")
async def targetval(
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    condition: Optional[str] = None,
    efo_id: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Aggregate evidence across all modules.

    Clients provide a gene identifier (symbol or Ensembl ID) and a
    condition (condition name or EFO ID).  The aggregator dispatches
    asynchronous calls to each module defined in the router and
    collates the results into a list.  Errors within any module are
    captured and reported as part of the result.
    """
    require_key(x_api_key)
    gene = ensembl_id or symbol
    efo = efo_id or condition
    if gene is None or efo is None:
        raise HTTPException(
            status_code=400,
            detail="Must supply gene (symbol or ensembl_id) and condition (efo_id or condition).",
        )
    # Dispatch module calls concurrently.  Each entry is a coroutine
    # wrapped with safe_call to ensure errors are captured.
    tasks: Dict[str, asyncio.Future] = {
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
    # Assemble response evidence list.
    evidence_list: List[Dict[str, Any]] = []
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
