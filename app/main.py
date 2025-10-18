# app/main.py
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
# Import your router implementation
# ------------------------------------------------------------------------------
try:
    from app.routers import targetval_router as _router_mod
except Exception as e:
    # Fail fast — without the primary router, there is no API.
    log.error(f"Failed to import app.routers.targetval_router: {e}")
    raise

tv_router = _router_mod.router
MODULES = getattr(_router_mod, "MODULES", None)
ROUTER_DOMAINS_META = getattr(_router_mod, "DOMAINS_META", None)
ROUTER_DOMAIN_MODULES = getattr(_router_mod, "DOMAIN_MODULES", None)

# ------------------------------------------------------------------------------
# App metadata / env
# ------------------------------------------------------------------------------
APP_TITLE = os.getenv("APP_TITLE", "TargetVal Gateway (Actions Surface) — Full")
APP_VERSION = os.getenv("APP_VERSION", "2025.10")
ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

# ------------------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------------------
app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    docs_url=DOCS_URL,
    openapi_url=OPENAPI_URL,
    root_path=ROOT_PATH,
)

# CORS (default permissive; tighten in prod with CORS_ALLOW_ORIGINS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the full router under /v1 (this carries all 64 modules)
app.include_router(tv_router, prefix="/v1")

# ------------------------------------------------------------------------------
# Schemas for wrapper endpoints
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
    module_key: str = Field(..., description="One of the 64 keys from /modules")
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
# Domain metadata fallbacks (if router didn't export them)
# ------------------------------------------------------------------------------
DOMAINS_META = ROUTER_DOMAINS_META or {
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
    # Fallback to empty lists if not exported by router
    return {str(i): [] for i in range(1, 7)} | {f"D{i}": [] for i in range(1, 7)}

# ------------------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------------------
def _module_map() -> Dict[str, str]:
    """
    Build {module_key -> route_path} from router.MODULES
    """
    mapping: Dict[str, str] = {}
    try:
        for m in (MODULES or []):
            name = getattr(m, "name", None) or getattr(m, "key", None)
            route = getattr(m, "route", None)
            if name and route:
                mapping[str(name)] = str(route)
    except Exception:
        pass
    return mapping

async def _self_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not path.startswith("/"):
        path = "/" + path
    # ASGI-internal call; no outbound network hop
    async with httpx.AsyncClient(app=app, base_url="http://internal") as client:
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
    return {"ok": True, "router": "app.routers.targetval_router", "import_ok": True}

@app.get("/readyz")
async def readyz():
    return {
        "ok": True,
        "env": {
            "CACHE_TTL_SECONDS": os.getenv("CACHE_TTL_SECONDS"),
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
    # Fallback (union of domain lists)
    seen: Dict[str, int] = {}
    for _, mods in _domain_modules().items():
        for k in (mods or []):
            seen[k] = 1
    return sorted(seen.keys())

@app.get("/domains")
async def list_domains():
    dmap = _domain_modules()
    out = []
    for i in range(1, 7):
        out.append({"id": i, "name": DOMAINS_META.get(i, {}).get("name", f"Domain {i}"), "modules": dmap.get(str(i), [])})
    return {"ok": True, "domains": out}

@app.get("/module/{name}", response_model=Evidence)
async def run_module_get(
    name: str,
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
    return await _self_get(mm[name], params)  # type: ignore[return-value]

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
    return await _self_get(mm[key], params)  # type: ignore[return-value]

@app.post("/aggregate")
async def aggregate_modules(req: AggregateRequest):
    mm = _module_map()
    dmap = _domain_modules()

    # Resolve modules
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
            results[k] = await _self_get(mm[k], params)
        except Exception as e:
            if req.continue_on_error:
                errors[k] = str(e)
            else:
                raise

    if req.order == "parallel":
        sem = asyncio.Semaphore(int(os.getenv("AGGREGATE_MAX_CONCURRENCY", "8")))
        async def _guarded(k: str):
            async with sem:
                await _run_one(k)
        await asyncio.gather(*[_guarded(k) for k in modules])
    else:
        for k in modules:
            await _run_one(k)

    return {"ok": True, "requested": {"modules": modules, "domain": req.domain}, "results": results, "errors": errors}

@app.post("/domain/{domain_id}/run")
async def run_domain(
    domain_id: int = Path(..., ge=1, le=6, description="Domain id 1..6"),  # <- MUST be Path, not Query
    body: Optional[DomainRunRequest] = Body(None),
):
    req = body or DomainRunRequest()
    dmap = _domain_modules()
    mods = dmap.get(str(domain_id), [])
    agg = AggregateRequest(
        modules=mods,
        domain=str(domain_id),
        primary_only=req.primary_only,
        order="sequential",
        continue_on_error=True,
        limit=req.limit,
        offset=req.offset,
        gene=req.gene,
        symbol=req.symbol,
        ensembl_id=req.ensembl_id,
        uniprot_id=req.uniprot_id,
        efo=req.efo,
        condition=req.condition,
        tissue=req.tissue,
        cell_type=req.cell_type,
        species=req.species,
        cutoff=req.cutoff,
        extra=req.extra,
    )
    return await aggregate_modules(agg)

@app.get("/", include_in_schema=False)
async def root():
    return {"ok": True, "service": APP_TITLE, "docs": "/docs", "api": "/v1"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
