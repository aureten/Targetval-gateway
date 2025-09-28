# app/main.py — public gateway, passthrough extras, proxy-aware, request-id
import os
import inspect
import asyncio
import uuid
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.routers import targetval_router
from app.routers.targetval_router import Evidence  # for isinstance checks

# -----------------------------------------------------------------------------
# FastAPI app & CORS
# -----------------------------------------------------------------------------
app = FastAPI(title="TARGETVAL Gateway", version="1.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Honor X-Forwarded-* from Render/ingress
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


# Request ID middleware for traceability
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or os.getenv("REQUEST_ID_PREFIX", "") + str(uuid.uuid4())
        resp = await call_next(request)
        resp.headers["X-Request-ID"] = rid
        return resp


app.add_middleware(RequestIDMiddleware)

# Mount all module endpoints from router
app.include_router(targetval_router.router)
app.include_router(insight_router.router)  # NEW


# -----------------------------------------------------------------------------
# Modules registry (string → function) for programmatic calls
# -----------------------------------------------------------------------------
MODULE_MAP: Dict[str, Any] = {
    # Bucket 1 – Human Genetics & Causality
    "genetics_l2g": targetval_router.genetics_l2g,
    "genetics_rare": targetval_router.genetics_rare,
    "genetics_mendelian": targetval_router.genetics_mendelian,
    "genetics_mr": targetval_router.genetics_mr,
    "genetics_lncrna": targetval_router.genetics_lncrna,
    "genetics_mirna": targetval_router.genetics_mirna,
    "genetics_sqtl": targetval_router.genetics_sqtl,
    "genetics_epigenetics": targetval_router.genetics_epigenetics,
    # Bucket 2 – Disease Association & Perturbation
    "assoc_bulk_rna": targetval_router.assoc_bulk_rna,
    "assoc_bulk_prot": targetval_router.assoc_bulk_prot,
    "assoc_sc": targetval_router.assoc_sc,
    "assoc_perturb": targetval_router.assoc_perturb,
    # Bucket 3 – Expression, Specificity & Localization
    "expr_baseline": targetval_router.expression_baseline,
    "expr_localization": targetval_router.expr_localization,
    "expr_inducibility": targetval_router.expr_inducibility,
    # Bucket 4 – Mechanistic Wiring & Networks
    "mech_pathways": targetval_router.mech_pathways,
    "mech_ppi": targetval_router.mech_ppi,
    "mech_ligrec": targetval_router.mech_ligrec,
    # Bucket 5 – Tractability & Modality
    "tract_drugs": targetval_router.tract_drugs,
    "tract_ligandability_sm": targetval_router.tract_ligandability_sm,
    "tract_ligandability_ab": targetval_router.tract_ligandability_ab,
    "tract_ligandability_oligo": targetval_router.tract_ligandability_oligo,
    "tract_modality": targetval_router.tract_modality,
    "tract_immunogenicity": targetval_router.tract_immunogenicity,
    # Bucket 6 – Clinical Translation & Safety
    "clin_endpoints": targetval_router.clin_endpoints,
    "clin_rwe": targetval_router.clin_rwe,
    "clin_safety": targetval_router.clin_safety,
    "clin_pipeline": targetval_router.clin_pipeline,
    "clin_biomarker_fit": targetval_router.clin_biomarker_fit,
    # Bucket 7 – Competition & IP
    "comp_intensity": targetval_router.comp_intensity,
    "comp_freedom": targetval_router.comp_freedom,
}

# -----------------------------------------------------------------------------
# Models & helpers for aggregate execution
# -----------------------------------------------------------------------------
class AggregateRequest(BaseModel):
    gene: Optional[str] = None
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None  # NEW: allow direct Ensembl submission
    efo: Optional[str] = None
    condition: Optional[str] = None
    modules: Optional[List[str]] = None
    limit: Optional[int] = 50
    # Common passthroughs used by specific modules
    species: Optional[int] = None
    cutoff: Optional[float] = None
    # catch-all for forward-compat extras (silently ignored by funcs that don't accept them)
    extra: Optional[Dict[str, Any]] = None


# crude heuristic to tell if a string "looks like" an HGNC symbol (not an Ensembl/NCBI/curie)
SYMBOLISH = re.compile(r"^[A-Za-z0-9-]+$")


def _looks_like_symbol(s: str) -> bool:
    if not s:
        return False
    up = s.upper()
    if up.startswith("ENSG") or ":" in up or "_" in up:
        return False
    return bool(SYMBOLISH.match(up))


def _bind_kwargs(func: Any, provided: Dict[str, Any]) -> Dict[str, Any]:
    """Return only the kwargs that the target function actually accepts."""
    sig = inspect.signature(func)
    allowed = set(sig.parameters.keys())
    return {k: v for k, v in provided.items() if k in allowed and v is not None}


async def _run_module(
    name: str,
    func: Any,
    gene: Optional[str],
    symbol: Optional[str],
    ensembl_id: Optional[str],
    efo: Optional[str],
    condition: Optional[str],
    limit: Optional[int],
    extra: Optional[Dict[str, Any]],
):
    """Invoke a single module function safely and return (name, result_dict)."""
    # Safer fallback: only use gene as symbol if it looks like an HGNC symbol
    symbol_effective = symbol if symbol else (gene if _looks_like_symbol(gene or "") else None)

    kwargs_all: Dict[str, Any] = {
        "gene": gene,
        "symbol": symbol_effective,
        "efo": efo,
        "condition": condition,
        "disease": condition,  # some functions use 'disease' as the param name
        "limit": limit,
        # NEW passthrough for modules that accept it (e.g., genetics_l2g, genetics_mendelian)
        "ensembl": ensembl_id,
    }
    if extra:
        kwargs_all.update(extra)

    kwargs = _bind_kwargs(func, kwargs_all)

    try:
        result = func(**kwargs)
        if asyncio.iscoroutine(result):
            result = await result

        # Coerce Evidence Pydantic model (v1 or v2) to plain dict
        if isinstance(result, Evidence):
            if hasattr(result, "dict"):
                return name, result.dict()
            if hasattr(result, "model_dump"):
                return name, result.model_dump()
        if isinstance(result, dict):
            return name, result

        # Unexpected type; wrap
        return name, {
            "status": "ERROR",
            "source": "Unexpected return type",
            "fetched_n": 0,
            "data": {},
            "citations": [],
            "fetched_at": 0.0,
        }
    except HTTPException as he:
        return name, {
            "status": "ERROR",
            "source": f"HTTP {he.status_code}: {he.detail}",
            "fetched_n": 0,
            "data": {},
            "citations": [],
            "fetched_at": 0.0,
        }
    except Exception as e:
        return name, {
            "status": "ERROR",
            "source": str(e),
            "fetched_n": 0,
            "data": {},
            "citations": [],
            "fetched_at": 0.0,
        }


# -----------------------------------------------------------------------------
# Convenience service endpoints (public)
# -----------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "modules": len(MODULE_MAP)}


@app.get("/modules")
async def list_modules():
    return sorted(MODULE_MAP.keys())


@app.get("/module/{name}")
async def run_single_module(
    request: Request,
    name: str,
    gene: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    ensembl: Optional[str] = Query(default=None, description="Optional Ensembl gene ID"),
    efo: Optional[str] = Query(default=None),
    condition: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
):
    func = MODULE_MAP.get(name)
    if not func:
        raise HTTPException(status_code=404, detail=f"Unknown module: {name}")

    # Pass through any extra query params (species, cutoff, tissue, cell_type, etc.)
    known = {"gene", "symbol", "ensembl", "efo", "condition", "limit"}
    extras: Dict[str, Any] = {k: v for k, v in request.query_params.items() if k not in known}

    _, res = await _run_module(
        name=name,
        func=func,
        gene=gene,
        symbol=symbol,
        ensembl_id=ensembl,
        efo=efo,
        condition=condition,
        limit=limit,
        extra=extras or None,
    )
    return res


# Aggregate fan-out with optional concurrency cap
AGG_LIMIT = int(os.getenv("AGG_CONCURRENCY", "8"))


@app.post("/aggregate")
async def aggregate(body: AggregateRequest):
    modules = body.modules or list(MODULE_MAP.keys())
    unknown = [m for m in modules if m not in MODULE_MAP]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown modules: {', '.join(unknown)}")

    # Build a shared extras dict from common passthroughs + explicit extra
    extras: Dict[str, Any] = {}
    if body.species is not None:
        extras["species"] = body.species
    if body.cutoff is not None:
        extras["cutoff"] = body.cutoff
    if body.extra:
        extras.update(body.extra)

    sem = asyncio.Semaphore(AGG_LIMIT)

    async def _guarded(mname: str, mfunc: Any):
        async with sem:
            return await _run_module(
                name=mname,
                func=mfunc,
                gene=body.gene,
                symbol=body.symbol,
                ensembl_id=body.ensembl_id,
                efo=body.efo,
                condition=body.condition,
                limit=body.limit or 50,
                extra=extras or None,
            )

    results = await asyncio.gather(*[_guarded(m, MODULE_MAP[m]) for m in modules])

    # Provide a slightly smarter echo of the query (mirroring symbol fallback)
    sym = body.symbol if body.symbol else (
        body.gene if (body.gene and re.match(r"^[A-Za-z0-9-]+$", body.gene)
                      and not body.gene.upper().startswith("ENSG")
                      and ":" not in body.gene and "_" not in body.gene) else None
    )

    return {
        "query": {
            "gene": body.gene,
            "symbol": sym,
            "ensembl_id": body.ensembl_id,
            "efo": body.efo,
            "condition": body.condition,
            "limit": body.limit or 50,
            "modules": modules,
            "species": body.species,
            "cutoff": body.cutoff,
        },
        "results": {name: res for name, res in results},
    }
