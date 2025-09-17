"""
Revised entry point for the TARGETVAL gateway.

This version introduces several improvements over the original implementation:

* **Concurrency control** – A semaphore limits the number of concurrent
  outbound requests.  This helps avoid saturating public APIs and
  reduces the likelihood of transient failures.  Adjust
  ``MAX_CONCURRENT_REQUESTS`` as necessary for your deployment.

* **Module selection** – Clients can specify which modules to execute via
  a comma‑separated ``modules`` query parameter.  Omitting the
  parameter will run all available modules.  Unknown module names are
  ignored.

* **Consistent aggregation** – The response now includes the list of
  requested modules along with the gene and condition identifiers used
  for the query.  Results are returned as before, with the addition of
  concurrency handling.

The remainder of the file mirrors the original behaviour: it exposes
health and ClinicalTrials.gov proxy endpoints, imports the module
coroutines from :mod:`app.routers.targetval_router` and collates
results from those coroutines into a single JSON structure.
"""

import asyncio
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

# Import module functions from the router.  These endpoints wrap
# external APIs and return :class:`app.routers.targetval_router.Evidence`.
from app.routers.targetval_router import (
    Evidence as RouterEvidence,
    assoc_bulk_prot,
    assoc_bulk_rna,
    assoc_perturb,
    assoc_sc,
    clin_endpoints,
    clin_pipeline,
    clin_rwe,
    clin_safety,
    comp_freedom,
    comp_intensity,
    expression_baseline,
    expr_inducibility,
    expr_localization,
    genetics_epigenetics,
    genetics_l2g,
    genetics_lncrna,
    genetics_mendelian,
    genetics_mirna,
    genetics_mr,
    genetics_rare,
    genetics_sqtl,
    mech_ligrec,
    mech_pathways,
    mech_ppi,
    tract_drugs,
    tract_immunogenicity,
    tract_ligandability_ab,
    tract_ligandability_oligo,
    tract_ligandability_sm,
    tract_modality,
)

API_KEY = os.getenv("API_KEY")

# FastAPI application instance.  Note that the version number has
# incremented to reflect breaking changes.
app = FastAPI(title="TARGETVAL Gateway", version="0.3.0")


class Evidence(BaseModel):
    """Response model mirroring :class:`app.routers.targetval_router.Evidence`.

    The entrypoint uses this model for its ClinicalTrials.gov proxy but
    otherwise delegates to the router for evidence structures.
    """

    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float


def require_key(x_api_key: Optional[str]) -> None:
    """API key check.  Currently a no‑op to support public access.

    To enable API key enforcement, replace the body of this function
    with a comparison against an environment variable and raise
    ``HTTPException`` on mismatch.
    """

    return


@app.get("/v1/health")
def health() -> Dict[str, float]:
    """Liveness endpoint returning current time in seconds since the epoch."""
    return {"ok": True, "time": time.time()}


@app.get("/clinical/ctgov", response_model=Evidence)
async def ctgov(
    condition: str, x_api_key: Optional[str] = Header(default=None)
) -> Evidence:
    """Proxy to ClinicalTrials.gov for a given condition.

    Returns the first three studies retrieved from the v2 API.  The
    request is retried up to three times with increasing delays on
    failure.  Errors propagate to the caller with a 500 status code.
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


# -----------------------------------------------------------------------------
# Concurrency support
# -----------------------------------------------------------------------------

# Limit the number of concurrent outbound requests across all modules.  The
# semaphore is shared globally within this module.  Increase the value to
# improve throughput or decrease it if remote services impose strict rate
# limits.
MAX_CONCURRENT_REQUESTS = 5
_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


async def safe_call(coro) -> RouterEvidence:
    """Wrap a module coroutine call to return an Evidence object on error.

    This helper mirrors the behaviour of the router's ``safe_call`` but
    catches exceptions and converts them into ``RouterEvidence``.  It
    should be used when awaiting any module coroutine to ensure the
    overall request does not fail if one source errors.
    """

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


async def call_with_semaphore(coro) -> RouterEvidence:
    """Acquire the concurrency semaphore before invoking a coroutine.

    This ensures that no more than ``MAX_CONCURRENT_REQUESTS`` coroutines
    are concurrently performing outbound I/O.  The semaphore is
    released when the coroutine completes.
    """

    async with _semaphore:
        return await safe_call(coro)


# -----------------------------------------------------------------------------
# Module definitions
# -----------------------------------------------------------------------------

# Mapping from module names to the coroutine functions imported from the
# router.  This is used to dynamically dispatch calls based on a
# comma‑separated ``modules`` query parameter.
MODULE_MAP: Dict[str, Any] = {
    "genetics_l2g": genetics_l2g,
    "genetics_rare": genetics_rare,
    "genetics_mendelian": genetics_mendelian,
    "genetics_mr": genetics_mr,
    "genetics_lncrna": genetics_lncrna,
    "genetics_mirna": genetics_mirna,
    "genetics_sqtl": genetics_sqtl,
    "genetics_epigenetics": genetics_epigenetics,
    "assoc_bulk_rna": assoc_bulk_rna,
    "assoc_bulk_prot": assoc_bulk_prot,
    "assoc_sc": assoc_sc,
    "assoc_perturb": assoc_perturb,
    "expression_baseline": expression_baseline,
    "expr_localization": expr_localization,
    "expr_inducibility": expr_inducibility,
    "mech_pathways": mech_pathways,
    "mech_ppi": mech_ppi,
    "mech_ligrec": mech_ligrec,
    "tract_drugs": tract_drugs,
    "tract_ligandability_sm": tract_ligandability_sm,
    "tract_ligandability_ab": tract_ligandability_ab,
    "tract_ligandability_oligo": tract_ligandability_oligo,
    "tract_modality": tract_modality,
    "tract_immunogenicity": tract_immunogenicity,
    "clin_endpoints": clin_endpoints,
    "clin_rwe": clin_rwe,
    "clin_safety": clin_safety,
    "clin_pipeline": clin_pipeline,
    "comp_intensity": comp_intensity,
    "comp_freedom": comp_freedom,
}


# Mapping from module names to high‑level buckets for aggregated responses.
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


def _parse_module_list(modules: Optional[str]) -> List[str]:
    """Parse a comma‑separated list of modules into a list of known names.

    Unknown names are silently ignored.  If ``modules`` is empty or
    ``None``, all available modules are returned.
    """

    if not modules:
        return list(MODULE_MAP.keys())
    return [m for m in modules.split(",") if m in MODULE_MAP]


@app.get("/v1/targetval")
async def targetval(
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    condition: Optional[str] = None,
    efo_id: Optional[str] = None,
    modules: Optional[str] = Query(
        None,
        description="Comma‑separated list of module names to execute.  If omitted, all modules are called.",
    ),
    x_api_key: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Aggregate evidence across selected modules.

    Clients must provide a gene identifier (``symbol`` or ``ensembl_id``)
    and a condition (``condition`` or ``efo_id``).  The aggregator
    normalises these values and dispatches asynchronous calls to each
    selected module defined in :mod:`app.routers.targetval_router`.
    Results from each module are collated into a list.  Errors within
    any module are captured and reported as part of the result.

    The optional ``modules`` parameter allows callers to specify which
    modules to run; unknown names are ignored.  This can reduce
    latency and external API usage when only a subset of evidence is
    required.
    """

    require_key(x_api_key)
    gene = ensembl_id or symbol
    efo = efo_id or condition
    if gene is None or efo is None:
        raise HTTPException(
            status_code=400,
            detail="Must supply gene (symbol or ensembl_id) and condition (efo_id or condition).",
        )

    # Determine which modules to invoke based on the ``modules`` query parameter.
    selected_modules = _parse_module_list(modules)

    # Build tasks for each selected module.  We inspect the module name to
    # determine the appropriate argument signature.  This introspection
    # mirrors the original aggregator logic but could be improved by
    # introspecting the function signature directly (see ``inspect`` module).
    tasks: Dict[str, asyncio.Task] = {}
    for name in selected_modules:
        func = MODULE_MAP[name]
        if name.startswith("genetics"):
            # Modules that require gene and EFO identifiers
            if name in (
                "genetics_l2g",
                "genetics_mendelian",
                "genetics_mr",
                "genetics_sqtl",
                "genetics_epigenetics",
            ):
                coro = func(gene, efo, x_api_key)  # type: ignore
            else:
                coro = func(gene, x_api_key)  # type: ignore
        elif name.startswith("assoc"):
            # Association modules operate on the condition only
            coro = func(efo, x_api_key)  # type: ignore
        elif name.startswith("expr") or name.startswith("mech") or name.startswith("tract"):
            # Expression, mechanistic and tractability modules operate on the gene symbol
            coro = func(gene, x_api_key)  # type: ignore
        elif name.startswith("clin"):
            if name in ("clin_safety", "clin_pipeline"):
                coro = func(gene, x_api_key)  # type: ignore
            else:
                coro = func(efo, x_api_key)  # type: ignore
        elif name.startswith("comp"):
            if name == "comp_intensity":
                coro = func(gene, efo, x_api_key)  # type: ignore
            else:
                coro = func(gene, x_api_key)  # type: ignore
        else:
            # Default: no arguments
            coro = func()  # type: ignore
        tasks[name] = asyncio.create_task(call_with_semaphore(coro))

    # Await all module tasks concurrently.
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
        "symbol": gene,
        "condition": efo,
        "modules": selected_modules,
        "evidence": evidence_list,
    }
