
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException, Path as PathParam, Query
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
# Import router (prefer monolithic full router; fallback to legacy router)
# ------------------------------------------------------------------------------
try:
    from app.routers import targetval_router_full as _router_mod  # monolithic router
    log.info("Loaded router: app.routers.targetval_router_full")
except Exception:
    from app.routers import targetval_router as _router_mod        # legacy router (supported)
    log.info("Loaded router: app.routers.targetval_router")

tv_router = _router_mod.router
MODULES = getattr(_router_mod, "MODULES", None)
ROUTER_DOMAINS_META = getattr(_router_mod, "DOMAINS_META", None)
ROUTER_DOMAIN_MODULES = getattr(_router_mod, "DOMAIN_MODULES", None)

# ------------------------------------------------------------------------------
# App metadata / env
# ------------------------------------------------------------------------------
APP_TITLE = os.getenv("APP_TITLE", "TargetVal Gateway (Actions Surface) â Full")
APP_VERSION = os.getenv("APP_VERSION", "2025.10")
ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

# "compat" wrappers mirror the classic /modules,/module,/aggregate,/domain endpoints at app-level
# They internally call the mounted router and exist to preserve long-lived clients.
WRAPPERS_ENABLED = os.getenv("WRAPPERS_ENABLED", "1").strip() not in ("0", "false", "False")

# ------------------------------------------------------------------------------
# Add missing endpoints **onto the router itself** so they appear under /v1/*
# The router has prefix="/targetval", so these become:
#   GET  /v1/targetval/modules
#   GET  /v1/targetval/module/{name}
# These are used by the compat wrappers via internal self-calls.
# ------------------------------------------------------------------------------

class EvidenceLegacy(BaseModel):
    status: str                # "OK" | "ERROR" | "NO_DATA"
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str] = []
    fetched_at: float
    debug: Optional[Dict[str, Any]] = None

def _env_to_legacy_evidence(module_key: str, env: Any) -> EvidenceLegacy:
    # Map router's EvidenceEnvelope -> legacy Evidence
    try:
        status_enum = getattr(env, "status", None)
        status = str(status_enum.value if hasattr(status_enum, "value") else status_enum or "OK")
        if status not in {"OK", "NO_DATA"}:
            status = "ERROR" if status not in {"OK", "NO_DATA"} else status
    except Exception:
        status = "OK"
    prov = getattr(env, "provenance", None)
    sources = getattr(prov, "sources", None) or []
    records = getattr(env, "records", None) or []
    src = sources[0] if sources else (MODULES.get(module_key).primary.get("url") if isinstance(MODULES, dict) and module_key in MODULES else module_key)
    out = EvidenceLegacy(
        status=status,
        source=str(src),
        fetched_n=len(records) if isinstance(records, list) else (1 if records else 0),
        data={
            "records": records,
            "context": getattr(env, "context", None).model_dump() if hasattr(getattr(env, "context", None), "model_dump") else getattr(env, "context", None),
        },
        citations=[str(s) for s in sources],
        fetched_at=time.time(),
        debug={
            "module": module_key,
            "edge_count": len(getattr(env, "edges", []) or []),
            "warnings": getattr(prov, "warnings", []) if prov is not None else [],
            "error": getattr(env, "error", None),
        },
    )
    return out

@tv_router.get("/modules", tags=["Modules"])
async def _router_list_modules() -> List[str]:
    if isinstance(MODULES, dict) and MODULES:
        return sorted(list(MODULES.keys()))
    return []

@tv_router.get("/module/{name}", response_model=EvidenceLegacy, tags=["Modules"])
async def _router_run_module_get(
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
    strict: bool = False,  # accepted for parity; passed through in ctx
) -> EvidenceLegacy:
    if not (isinstance(MODULES, dict) and name in MODULES):
        raise HTTPException(status_code=404, detail=f"Unknown module: {name}")
    ctx = {
        "hgnc": symbol or gene,
        "ensg": ensembl_id,
        "uniprot": uniprot_id,
        "efo": efo,
        "trait_label": condition,
        "tissue": tissue,
        "cell_type": cell_type,
        "species": species,
        "limit": limit,
        "offset": offset,
        "strict": strict,
    }
    env = await _router_mod.run_module(name, ctx)  # returns EvidenceEnvelope
    return _env_to_legacy_evidence(name, env)

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
    allow_origins=[o for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount router under /v1 (matches OpenAPI servers and keeps public surface stable)
app.include_router(tv_router, prefix="/v1")

# ------------------------------------------------------------------------------
# Internal ASGI self-call helpers
# ------------------------------------------------------------------------------
async def _self_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not path.startswith("/"):
        path = "/" + path
    async with httpx.AsyncClient(app=app, base_url="http://internal") as client:
        candidate = path if path.startswith("/v1/") else ("/v1" + path)
        r = await client.get(candidate, params={k: v for k, v in (params or {}).items() if v is not None})
        if r.status_code == 404 and not path.startswith("/v1/"):
            r = await client.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        r.raise_for_status()
        return r.json()

async def _self_post(path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
    if not path.startswith("/"):
        path = "/" + path
    async with httpx.AsyncClient(app=app, base_url="http://internal") as client:
        candidate = path if path.startswith("/v1/") else ("/v1" + path)
        r = await client.post(candidate, json=json_body)
        if r.status_code == 404 and not path.startswith("/v1/"):
            r = await client.post(path, json=json_body)
        r.raise_for_status()
        return r.json()

# ------------------------------------------------------------------------------
# Health
# ------------------------------------------------------------------------------
@app.get("/healthz")
@app.get("/v1/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}

@app.get("/livez")
@app.get("/v1/livez")
async def livez():
    return {"ok": True, "router": _router_mod.__name__, "import_ok": True}

@app.get("/readyz")
@app.get("/v1/readyz")
async def readyz():
    return {
        "ok": True,
        "env": {
            "CACHE_TTL_SECONDS": os.getenv("CACHE_TTL_SECONDS"),
            "REQUEST_BUDGET_S": os.getenv("REQUEST_BUDGET_S"),
        },
    }

# ------------------------------------------------------------------------------
# Optional "compat" wrappers (toggle with WRAPPERS_ENABLED=0 to disable)
# These mirror your longstanding orchestrators so clients can hit /v1/* or root/* interchangeably.
# ------------------------------------------------------------------------------
if WRAPPERS_ENABLED:
    class Evidence(BaseModel):
        status: str
        source: str
        fetched_n: int
        data: Dict[str, Any]
        citations: List[str]
        fetched_at: float
        debug: Optional[Dict[str, Any]] = None

    class ModuleRunRequest(BaseModel):
        module_key: str
        gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
        symbol: Optional[str] = None
        ensembl_id: Optional[str] = None
        uniprot_id: Optional[str] = None
        efo: Optional[str] = None
        condition: Optional[str] = None
        tissue: Optional[str] = None
        cell_type: Optional[str] = None
        species: Optional[str] = None
        limit: int = Field(100, ge=1, le=500)
        offset: int = Field(0, ge=0)
        strict: bool = False
        extra: Dict[str, Any] = {}

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
        extra: Dict[str, Any] = {}

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

    # -------- helpers (wrappers) --------
    def _module_map() -> Dict[str, str]:
        # Map module key -> router path we just added above
        mapping: Dict[str, str] = {}
        try:
            if isinstance(MODULES, dict):
                for key in MODULES.keys():
                    mapping[str(key)] = f"/targetval/module/{key}"
        except Exception:
            pass
        return mapping

    def _domain_modules() -> Dict[str, List[str]]:
        if ROUTER_DOMAIN_MODULES:
            D = {str(k): v for k, v in ROUTER_DOMAIN_MODULES.items()}
            D.update({f"D{k}": v for k, v in ROUTER_DOMAIN_MODULES.items()})
            return D
        return {str(i): [] for i in range(1, 7)} | {f"D{i}": [] for i in range(1, 7)}

    # -------- discovery --------
    @app.get("/modules")
    @app.get("/v1/modules")
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
    @app.get("/v1/domains")
    async def list_domains():
        dmap = _domain_modules()
        out = []
        for i in range(1, 6 + 1):
            out.append({
                "id": i,
                "name": (ROUTER_DOMAINS_META or {}).get(i, {}).get("name", f"Domain {i}"),
                "modules": dmap.get(str(i), [])
            })
        return {"ok": True, "domains": out}

    # -------- single module --------
    @app.get("/module/{name}", response_model=Evidence)
    @app.get("/v1/module/{name}", response_model=Evidence)
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
    ) -> Any:
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
    @app.post("/v1/module", response_model=Evidence)
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

    # -------- aggregate --------
    @app.post("/aggregate")
    @app.post("/v1/aggregate")
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

    # -------- domain run --------
    @app.post("/domain/{domain_id}/run")
    @app.post("/v1/domain/{domain_id}/run")
    async def run_domain(
        domain_id: int = PathParam(ge=1, le=6, description="Domain id 1..6"),  # MUST be Path, not Query
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

# ------------------------------------------------------------------------------
# Safety shims for /targetval/run to avoid misconfiguration
# ------------------------------------------------------------------------------
class RunRequestShim(BaseModel):
    gene_symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    uniprot_id: Optional[str] = None
    trait_efo: Optional[str] = None
    trait_label: Optional[str] = None
    rsid: Optional[str] = None
    region: Optional[str] = None
    run_questions: Optional[List[int]] = None
    strict_human: bool = True

@app.post("/targetval/run", include_in_schema=False)
async def proxy_targetval_run_post(body: RunRequestShim):
    return await _self_post("/targetval/run", body.model_dict() if hasattr(body, "model_dict") else body.dict())

@app.get("/targetval/run", include_in_schema=False)
async def proxy_targetval_run_get(
    gene_symbol: Optional[str] = None,
    trait_label: Optional[str] = None,
    trait_efo: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    uniprot_id: Optional[str] = None,
    rsid: Optional[str] = None,
    region: Optional[str] = None,
):
    body = {
        "gene_symbol": gene_symbol,
        "trait_label": trait_label,
        "trait_efo": trait_efo,
        "ensembl_id": ensembl_id,
        "uniprot_id": uniprot_id,
        "rsid": rsid,
        "region": region,
    }
    return await _self_post("/targetval/run", body)

@app.get("/v1/targetval/run", include_in_schema=False)
async def proxy_targetval_run_get_v1(
    gene_symbol: Optional[str] = None,
    trait_label: Optional[str] = None,
    trait_efo: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    uniprot_id: Optional[str] = None,
    rsid: Optional[str] = None,
    region: Optional[str] = None,
):
    body = {
        "gene_symbol": gene_symbol,
        "trait_label": trait_label,
        "trait_efo": trait_efo,
        "ensembl_id": ensembl_id,
        "uniprot_id": uniprot_id,
        "rsid": rsid,
        "region": region,
    }
    return await _self_post("/v1/targetval/run", body)

# ------------------------------------------------------------------------------
# Root
# ------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {"ok": True, "service": APP_TITLE, "docs": "/docs", "api": "/v1"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
