"""
TargetVal Gateway — FastAPI entrypoint (Render-safe, router in app/routers/)

What this file does
- Imports the TargetVal router from app/routers/targetval_router.py
- Mounts it at /v1 (all 64 modules live there)
- Adds convenience wrapper endpoints:
    GET  /modules                       → list module keys (64)
    GET  /module/{name}                 → run one module (query params)
    POST /module                        → run one module (JSON body)
    POST /aggregate                     → run many modules (sequential/parallel)
    POST /domain/{domain_id}/run        → run all modules in a domain (1..6)
    GET  /domains                       → list domains with their module keys
    GET  /healthz, /livez, /readyz      → health/meta
- Uses internal ASGI calls (httpx.AsyncClient(app=app)) to hit /v1/* routes
- Respects router runtime knobs via environment variables (set before import)

To force live fetches on every call: set CACHE_TTL_SECONDS=0 in your env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Body, Query, Path as FPath
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------
# Router runtime knobs — set defaults BEFORE importing the router (env may override)
# ------------------------------------------------------------------------------
_env_defaults = {
    # Router HTTP behavior
    "OUTBOUND_TIMEOUT_S": "12.0",
    "REQUEST_BUDGET_S": "25.0",
    "OUTBOUND_TRIES": "2",
    "BACKOFF_BASE_S": "0.35",
    "OUTBOUND_MAX_CONCURRENCY": "8",
    "OUTBOUND_USER_AGENT": "TargetVal/2.0 (+https://github.com/aureten/Targetval-gateway)",
    # Cache (set CACHE_TTL_SECONDS=0 in Render to always fetch live)
    "CACHE_TTL_SECONDS": "86400",
}
for _k, _v in _env_defaults.items():
    os.environ.setdefault(_k, _v)

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("targetval.main")

# ------------------------------------------------------------------------------
# Import router(s) from your repo layout
# ------------------------------------------------------------------------------
try:
    from app.routers import targetval_router as _router_mod
    tv_router = _router_mod.router
    MODULES = getattr(_router_mod, "MODULES", None)   # list of Module models with .name and .route
    ROUTER_DOMAINS_META = getattr(_router_mod, "DOMAINS_META", None)
    ROUTER_DOMAIN_MODULES = getattr(_router_mod, "DOMAIN_MODULES", None)
    log.info("Loaded targetval router from app.routers.targetval_router")
except Exception as e:
    log.error(f"Failed to import app.routers.targetval_router: {e}")
    raise

# Try to optionally include insight router if present (safe no-op otherwise)
try:
    from app.routers import insight_router as _insight_router  # type: ignore
    HAS_INSIGHT = True
    log.info("Loaded optional insight router from app.routers.insight_router")
except Exception:
    _insight_router = None
    HAS_INSIGHT = False

# ------------------------------------------------------------------------------
# App settings
# ------------------------------------------------------------------------------
APP_TITLE = os.getenv("APP_TITLE", "TargetVal Gateway (Actions Surface) — Full")
APP_VERSION = os.getenv("APP_VERSION", "2025.10")
ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    docs_url=DOCS_URL,
    openapi_url=OPENAPI_URL,
    root_path=ROOT_PATH,
)

# CORS (permissive defaults; restrict in prod via env if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the main router under /v1
app.include_router(tv_router, prefix="/v1")

# Mount optional insight router (if present)
if HAS_INSIGHT and getattr(_insight_router, "router", None) is not None:
    app.include_router(_insight_router.router, prefix="/v1")

# ------------------------------------------------------------------------------
# Pydantic models for wrapper endpoints (kept minimal & stable)
# ------------------------------------------------------------------------------

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float
    debug: Optional[Dict[str, Any]] = None

class ModuleRunRequest(BaseModel):
    module_key: str = Field(..., description="One of the 64 keys from /modules or /domains")
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    uniprot_id: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    tissue: Optional[str] = None
    cell_type: Optional[str] = None
    species: Optional[str] = None
    limit: Optional[int] = Field(100, ge=1, le=500)
    offset: Optional[int] = Field(0, ge=0)
    strict: Optional[bool] = False
    extra: Optional[Dict[str, Any]] = None

class AggregateRequest(BaseModel):
    modules: Optional[List[str]] = None
    domain: Optional[str] = Field(None, description="Accepts 'D1'..'D6' or '1'..'6'.")
    primary_only: bool = True
    order: str = Field("sequential", pattern="^(sequential|parallel)$")
    continue_on_error: bool = True
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    uniprot_id: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    tissue: Optional[str] = None
    cell_type: Optional[str] = None
    species: Optional[str] = None
    cutoff: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None

class DomainRunRequest(BaseModel):
    primary_only: bool = True
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    uniprot_id: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    tissue: Optional[str] = None
    cell_type: Optional[str] = None
    species: Optional[str] = None
    cutoff: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None

# ------------------------------------------------------------------------------
# Domain metadata fallback (used if router didn't export DOMAINS/DOMAIN_MODULES)
# ------------------------------------------------------------------------------

DOMAINS_META: Dict[int, Dict[str, str]] = ROUTER_DOMAINS_META or {
    1: {"name": "Genetic causality & human validation"},
    2: {"name": "Functional & mechanistic validation"},
    3: {"name": "Expression, selectivity & cell-state context"},
    4: {"name": "Druggability & modality tractability"},
    5: {"name": "Therapeutic index & safety translation"},
    6: {"name": "Clinical & translational evidence"},
}

def _domain_modules() -> Dict[str, List[str]]:
    if ROUTER_DOMAIN_MODULES:
        D = {str(k): v for k, v in ROUTER_DOMAIN_MODULES.items()}
        D.update({f"D{k}": v for k, v in ROUTER_DOMAIN_MODULES.items()})
        return D
    # Fallback: if router didn’t publish mapping, use a minimal placeholder
    return {str(i): [] for i in range(1, 7)} | {f"D{i}": [] for i in range(1, 7)}

# ------------------------------------------------------------------------------
# Helpers to call the mounted /v1 routes internally (no external network hop)
# ------------------------------------------------------------------------------

def _module_map() -> Dict[str, str]:
    """
    Returns {module_key -> route_path} using router.MODULES if available.
    Example: {"expr-baseline": "/expr/baseline", ...}
    """
    m: Dict[str, str] = {}
    try:
        for rec in (MODULES or []):
            name = getattr(rec, "name", None) or getattr(rec, "key", None)
            route = getattr(rec, "route", None)
            if name and route:
                m[str(name)] = str(route)
    except Exception:
        pass
    return m

async def _self_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    if not path.startswith("/"):
        path = "/" + path
    async with httpx.AsyncClient(app=app, base_url="http://router.internal") as client:
        r = await client.get("/v1" + path, params={k: v for k, v in (params or {}).items() if v is not None})
        r.raise_for_status()
        return r.json()

# ------------------------------------------------------------------------------
# Health / meta
# ------------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}

@app.get("/livez")
async def livez():
    return {
        "ok": True,
        "router": "app.routers.targetval_router",
        "insight": HAS_INSIGHT,
        "import_ok": True,
        "version": APP_VERSION,
    }

@app.get("/readyz")
async def readyz():
    return {
        "ok": True,
        "env": {
            "CACHE_TTL_SECONDS": os.getenv("CACHE_TTL_SECONDS"),
            "OUTBOUND_MAX_CONCURRENCY": os.getenv("OUTBOUND_MAX_CONCURRENCY"),
            "REQUEST_BUDGET_S": os.getenv("REQUEST_BUDGET_S"),
        },
    }

# ------------------------------------------------------------------------------
# Registry / orchestration wrappers
# ------------------------------------------------------------------------------

@app.get("/modules")
async def list_modules() -> List[str]:
    mm = _module_map()
    if mm:
        return sorted(mm.keys())
    # Fall back to domain union if MODULES wasn’t exported (shouldn’t happen)
    seen: Dict[str, int] = {}
    for _, mods in _domain_modules().items():
        for k in (mods or []):
            seen[k] = 1
    return sorted(seen.keys())

@app.get("/domains")
async def list_domains():
    doms = []
    mapping = _domain_modules()
    for i in range(1, 7):
        key = str(i)
        doms.append({
            "id": i,
            "name": DOMAINS_META.get(i, {}).get("name", f"Domain {i}"),
            "modules": mapping.get(key, []),
        })
    return {"ok": True, "domains": doms}

@app.get("/module/{name}", response_model=Evidence)
async def run_module_get(
    name: str = FPath(..., description="Module key from /modules"),
    gene: Optional[str] = None,
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    uniprot_id: Optional[str] = None,
    efo: Optional[str] = None,
    condition: Optional[str] = None,
    tissue: Optional[str] = None,
    cell_type: Optional[str] = None,
    species: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    strict: bool = False,
):
    mm = _module_map()
    if name not in mm:
        raise HTTPException(status_code=404, detail=f"Unknown module: {name}")
    params = dict(
        symbol=symbol or gene,
        ensembl_id=ensembl_id,
        uniprot_id=uniprot_id,
        efo=efo,
        condition=condition,
        tissue=tissue,
        cell_type=cell_type,
        species=species,
        limit=limit,
        offset=offset,
        strict=strict,
    )
    data = await _self_get(mm[name], params)
    # Trust router’s Evidence envelope; just return it
    return data  # type: ignore[return-value]

@app.post("/module", response_model=Evidence)
async def run_module_post(body: ModuleRunRequest) -> Any:
    mm = _module_map()
    key = body.module_key
    if key not in mm:
        raise HTTPException(status_code=404, detail=f"Unknown module: {key}")
    params = dict(
        symbol=body.symbol or body.gene,
        ensembl_id=body.ensembl_id,
        uniprot_id=body.uniprot_id,
        efo=body.efo,
        condition=body.condition,
        tissue=body.tissue,
        cell_type=body.cell_type,
        species=body.species,
        limit=body.limit,
        offset=body.offset,
        strict=body.strict,
    )
    data = await _self_get(mm[key], params)
    return data  # type: ignore[return-value]

@app.post("/aggregate")
async def aggregate_modules(req: AggregateRequest):
    mm = _module_map()
    dmap = _domain_modules()

    # Resolve module list
    modules: List[str] = []
    if req.modules:
        modules = [m for m in req.modules if m in mm]
    elif req.domain:
        key = req.domain.strip().upper()
        modules = [m for m in dmap.get(key, []) if m in mm]
    else:
        modules = sorted(mm.keys())

    if not modules:
        raise HTTPException(status_code=400, detail="No modules resolved from request")

    params = dict(
        symbol=req.symbol or req.gene,
        ensembl_id=req.ensembl_id,
        uniprot_id=req.uniprot_id,
        efo=req.efo,
        condition=req.condition,
        tissue=req.tissue,
        cell_type=req.cell_type,
        species=req.species,
        limit=req.limit,
        offset=req.offset,
    )

    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    async def _run_one(k: str):
        try:
            res = await _self_get(mm[k], params)
            results[k] = res
        except Exception as e:
            if req.continue_on_error:
                errors[k] = str(e)
            else:
                raise

    if req.order == "parallel":
        # Respect the router’s own concurrency by not flooding it; cap at 8 by default
        sem = asyncio.Semaphore(int(os.getenv("AGGREGATE_MAX_CONCURRENCY", "8")))
        async def _guarded(k: str):
            async with sem:
                await _run_one(k)
        await asyncio.gather(*[_guarded(k) for k in modules])
    else:
        for k in modules:
            await _run_one(k)

    return {
        "ok": True,
        "requested": {"modules": modules, "domain": req.domain},
        "results": results,
        "errors": errors,
        "n_ok": sum(1 for _ in results),
        "n_err": sum(1 for _ in errors),
    }

@app.post("/domain/{domain_id}/run")
async def run_domain(
    domain_id: int = FPath(..., ge=1, le=6),
    body: DomainRunRequest = Body(default_factory=DomainRunRequest),
):
    dmap = _domain_modules()
    mods = dmap.get(str(domain_id), [])
    if body.primary_only:
        # dmap only keeps primary in this layout; left for interface symmetry
        pass
    req = AggregateRequest(
        modules=mods,
        domain=str(domain_id),
        primary_only=body.primary_only,
        order="sequential",
        continue_on_error=True,
        limit=body.limit,
        offset=body.offset,
        gene=body.gene,
        symbol=body.symbol,
        ensembl_id=body.ensembl_id,
        uniprot_id=body.uniprot_id,
        efo=body.efo,
        condition=body.condition,
        tissue=body.tissue,
        cell_type=body.cell_type,
        species=body.species,
        cutoff=body.cutoff,
        extra=body.extra,
    )
    return await aggregate_modules(req)

# Root convenience
@app.get("/", include_in_schema=False)
async def root():
    return {"ok": True, "service": APP_TITLE, "docs": DOCS_URL, "api": "/v1"}

# Local dev entrypoint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
