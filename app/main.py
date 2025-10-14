
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sys
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple, Iterable

from fastapi import FastAPI, HTTPException, Query, Request, Path, Body
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Optional middlewares (Render-friendly)
try:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore
except Exception:
    ProxyHeadersMiddleware = None  # type: ignore

try:
    from starlette.middleware.trustedhost import TrustedHostMiddleware  # type: ignore
except Exception:
    TrustedHostMiddleware = None  # type: ignore


# -----------------------------------------------------------------------------
# App config
# -----------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "TargetVal Gateway")
APP_VERSION = os.getenv("APP_VERSION", "2025.10-actions")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}

ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
REDOC_URL = os.getenv("REDOC_URL", "/redoc")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
ALLOW_METHODS = os.getenv("ALLOW_METHODS", "*")
ALLOW_HEADERS = os.getenv("ALLOW_HEADERS", "*")
TRUSTED_HOSTS = os.getenv("TRUSTED_HOSTS", "*")

# Aggregation behavior defaults (can be overridden per request)
DEFAULT_ORDER = os.getenv("AGG_DEFAULT_ORDER", "sequential").lower()  # "sequential" | "parallel"
DEFAULT_CONCURRENCY = int(os.getenv("AGG_CONCURRENCY", "8"))
DEFAULT_CONTINUE = os.getenv("AGG_CONTINUE_ON_ERROR", "true").lower() in {"1","true","yes"}

# YAML registry path (module keys/paths and domain mapping 1..6)
REGISTRY_PATH = os.getenv("TARGETVAL_MODULE_CONFIG", "app/routers/targetval_modules.yaml")


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class AggregateRequest(BaseModel):
    # Query-like fields
    gene: Optional[str] = None
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    limit: Optional[int] = 50

    # Which modules to run
    modules: Optional[List[str]] = None
    domain: Optional[str | int] = None
    primary_only: bool = True

    # Execution behavior
    order: str = Field(default=DEFAULT_ORDER, pattern="^(sequential|parallel)$")
    continue_on_error: bool = DEFAULT_CONTINUE

    # misc passthroughs used by specific modules
    species: Optional[int] = None
    cutoff: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None


class ModuleRunRequest(BaseModel):
    module_key: str
    gene: Optional[str] = None
    symbol: Optional[str] = None
    ensembl: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    limit: Optional[int] = 50
    extra: Optional[Dict[str, Any]] = None


# -----------------------------------------------------------------------------
# Import helpers
# -----------------------------------------------------------------------------
def _import_router_module():
    attempts = []
    def _try(mod: str):
        try:
            m = __import__(mod, fromlist=["router"])
            if getattr(m, "router", None) is None:
                raise ImportError(f"module {mod} has no attribute 'router'")
            return m, mod, None
        except Exception:
            return None, None, traceback.format_exc()

    for mod in ("app.routers.targetval_router",
                "routers.targetval_router",
                "targetval_router",
                "router"):
        module, where, err = _try(mod)
        if module is not None:
            return module, where, None
        attempts.append(f"=== Attempt {mod} failed ===\n{err}")
    return None, None, "\n".join(attempts)


# -----------------------------------------------------------------------------
# Middleware
# -----------------------------------------------------------------------------
class RequestIDMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, header_name: str = "X-Request-ID"):
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(self.header_name) or str(uuid.uuid4())
        request.state.request_id = rid
        start = time.perf_counter()
        response = await call_next(request)
        response.headers.setdefault(self.header_name, rid)
        try:
            duration_ms = (time.perf_counter() - start) * 1000.0
            logging.getLogger("uvicorn.access").info(
                "%s %s -> %s (%.1f ms) rid=%s",
                request.method, request.url.path, response.status_code, duration_ms, rid,
            )
        except Exception:
            pass
        return response


# -----------------------------------------------------------------------------
# Registry utils (read YAML once and build maps)
# -----------------------------------------------------------------------------
def _safe_load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError("PyYAML is required to load module registry") from e
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}

def _build_key_to_path_and_domains(registry: Dict[str, Any]) -> Tuple[Dict[str,str], Dict[str, List[int]], List[str]]:
    key_to_path: Dict[str, str] = {}
    key_to_domains: Dict[str, List[int]] = {}
    all_keys: List[str] = []
    for m in registry.get("modules", []):
        k = m.get("key")
        p = m.get("path")
        if not k or not p:
            continue
        all_keys.append(k)
        key_to_path[k] = p
        # domains.primary is a list of ints (IDs 1..6) in the YAML
        doms = m.get("domains", {}) or {}
        primary = doms.get("primary", []) or []
        # normalise to int list
        primary_ids = [int(d) for d in primary if str(d).isdigit()]
        key_to_domains[k] = primary_ids
    return key_to_path, key_to_domains, all_keys


# -----------------------------------------------------------------------------
# App factory
# -----------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        debug=DEBUG,
        root_path=ROOT_PATH,
        docs_url=DOCS_URL,
        redoc_url=REDOC_URL,
        openapi_url=OPENAPI_URL,
    )

    # Middlewares
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestIDMiddleware)
    if ProxyHeadersMiddleware:
        app.add_middleware(ProxyHeadersMiddleware)
    if TRUSTED_HOSTS.strip() != "*" and TrustedHostMiddleware:
        hosts = [h.strip() for h in TRUSTED_HOSTS.split(",") if h.strip()]
        if hosts:
            app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if ALLOW_ORIGINS == "*" else [o.strip() for o in ALLOW_ORIGINS.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"] if ALLOW_METHODS == "*" else [m.strip() for m in ALLOW_METHODS.split(",") if m.strip()],
        allow_headers=["*"] if ALLOW_HEADERS == "*" else [h.strip() for h in ALLOW_HEADERS.split(",") if h.strip()],
    )

    # Import router and include under /v1
    router_module, where, import_error = _import_router_module()
    app.state.router_location = where
    app.state.router_import_error = import_error
    if router_module:
        app.include_router(router_module.router, prefix="/v1")

    # Detect presence of optional synthesis v2 routes
    has_v2 = False
    try:
        if router_module and hasattr(router_module, "router"):
            for r in router_module.router.routes:
                p = getattr(r, "path", "")
                if p in ("/synth/integrate", "/lit/meta") or str(p).startswith("/synth/bucket"):
                    has_v2 = True
                    break
    except Exception:
        has_v2 = False
    app.state.has_v2 = has_v2

    # Load registry and build maps
    try:
        reg = _safe_load_yaml(REGISTRY_PATH)
    except Exception as e:
        logging.exception("Failed to load registry %s: %r", REGISTRY_PATH, e)
        reg = {"modules": []}
    key_to_path, key_to_domains, all_keys = _build_key_to_path_and_domains(reg)
    app.state.key_to_path = key_to_path
    app.state.key_to_domains = key_to_domains
    app.state.module_keys = all_keys

    # Build function map from included router by matching paths -> endpoint function
    path_to_endpoint: Dict[str, Any] = {}
    if router_module and hasattr(router_module, "router"):
        for r in router_module.router.routes:
            try:
                path = getattr(r, "path", "") or ""
                endpoint = getattr(r, "endpoint", None)
                if path and endpoint:
                    path_to_endpoint[path] = endpoint
            except Exception:
                continue
    app.state.path_to_endpoint = path_to_endpoint

    # Compute a module_key -> callable map based on registry path
    def _resolve_callable_for_key(module_key: str) -> Optional[Any]:
        # registry path (without /v1 prefix)
        p = key_to_path.get(module_key)
        if not p:
            return None
        # when included under /v1, effective path is /v1 + p
        f = path_to_endpoint.get(p) or path_to_endpoint.get("/v1" + p) or None
        return f

    app.state.resolve_callable_for_key = _resolve_callable_for_key

    # ---------------- Meta/debug endpoints ----------------
    @app.get("/livez", tags=["_meta"])
    async def livez():
        return {
            "ok": True,
            "router": getattr(app.state, "router_location", None),
            "import_ok": getattr(app.state, "router_import_error", None) is None,
            "synthesis_v2": getattr(app.state, "has_v2", False),
        }

    @app.get("/readyz", tags=["_meta"])
    async def readyz():
        if getattr(app.state, "router_import_error", None) is not None:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "reason": "router import failed",
                    "details": "See /_debug/import",
                    "synthesis_v2": getattr(app.state, "has_v2", False),
                },
            )
        return {"ok": True, "root_path": ROOT_PATH, "docs": DOCS_URL, "synthesis_v2": getattr(app.state, "has_v2", False)}

    @app.get("/", tags=["_meta"])
    async def about():
        has_v2_local = getattr(app.state, "has_v2", False)
        tip = {
            "message": "TargetVal Gateway â Synthesis v2 available" if has_v2_local else "TargetVal Gateway",
            "try": ["/v1/aggregate", "/v1/modules", "/docs"]
        }
        return tip

    @app.get("/_debug/import", tags=["_meta"])
    async def debug_import():
        return {
            "attempted": getattr(app.state, "router_location", None),
            "error": getattr(app.state, "router_import_error", None),
            "sys_path": sys.path,
        }

    @app.get("/meta/routes", tags=["_meta"])
    async def list_routes() -> Dict[str, Any]:
        routes: List[Dict[str, Any]] = []
        for r in app.router.routes:
            try:
                routes.append({
                    "path": getattr(r, "path", None),
                    "name": getattr(r, "name", None),
                    "methods": sorted(list(getattr(r, "methods", []) or [])),
                })
            except Exception:
                pass
        routes.sort(key=lambda x: (x["path"] or ""))
        return {
            "count": len(routes),
            "routes": routes,
            "router": getattr(app.state, "router_location", None),
            "import_ok": getattr(app.state, "router_import_error", None) is None,
        }

    # ---------------- Utilities ----------------
    SYMBOLISH = re.compile(r"^[A-Za-z0-9-]+$")

    def _looks_like_symbol(s: Optional[str]) -> bool:
        if not s:
            return False
        up = s.upper()
        if up.startswith("ENSG") or ":" in up or "_" in up:
            return False
        return bool(SYMBOLISH.match(up))

    def _bind_kwargs(func: Any, provided: Dict[str, Any]) -> Dict[str, Any]:
        sig = inspect.signature(func)
        allowed = set(sig.parameters.keys())
        return {k: v for k, v in provided.items() if k in allowed and v is not None}

    async def _run_callable(name: str, func: Any, kwargs_all: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        kwargs = _bind_kwargs(func, kwargs_all)
        try:
            result = func(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            if hasattr(result, "dict") and callable(result.dict):
                return name, result.dict()
            if hasattr(result, "model_dump") and callable(result.model_dump):
                return name, result.model_dump()
            if isinstance(result, dict):
                return name, result
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

    def _build_shared_kwargs(body_like: Dict[str, Any]) -> Dict[str, Any]:
        gene = body_like.get("gene")
        symbol = body_like.get("symbol")
        ensembl_id = body_like.get("ensembl_id") or body_like.get("ensembl")
        efo = body_like.get("efo")
        condition = body_like.get("condition")
        limit = body_like.get("limit", 50)
        # Safe fallback gene->symbol
        symbol_effective = symbol if symbol else (gene if _looks_like_symbol(gene) else None)

        kwargs_all: Dict[str, Any] = {
            "gene": gene,
            "symbol": symbol_effective,
            "efo": efo,
            "condition": condition,
            "disease": condition,
            "limit": limit,
            # pass both names so handlers can pick what they accept
            "ensembl": ensembl_id,
            "ensembl_id": ensembl_id,
        }
        # passthroughs
        for k in ("species", "cutoff"):
            if k in body_like and body_like.get(k) is not None:
                kwargs_all[k] = body_like[k]
        extra = body_like.get("extra") or {}
        if isinstance(extra, dict):
            kwargs_all.update(extra)
        return kwargs_all

    def _filter_modules_for_domain(all_keys: Iterable[str], key_to_domains: Dict[str, List[int]], domain: int, primary_only: bool = True) -> List[str]:
        out = []
        for k in all_keys:
            doms = key_to_domains.get(k, [])
            if primary_only:
                if domain in doms:
                    out.append(k)
            else:
                if domain in doms:
                    out.append(k)
        return out

    # ---------------- Public endpoints under /v1 ----------------
    @app.get("/v1/healthz")
    async def healthz():
        return {"ok": True, "modules": len(app.state.module_keys), "version": APP_VERSION}

    @app.get("/v1/modules")
    async def list_modules():
        return sorted(app.state.module_keys)

    @app.post("/v1/module")
    async def run_single_module_post(body: ModuleRunRequest):
        func = app.state.resolve_callable_for_key(body.module_key)
        if not func:
            raise HTTPException(status_code=404, detail=f"Unknown module: {body.module_key}")
        kwargs_all = _build_shared_kwargs(body.model_dump())
        _, res = await _run_callable(name=body.module_key, func=func, kwargs_all=kwargs_all)
        return res

    @app.get("/v1/module/{name}")
    async def run_single_module_get(
        request: Request,
        name: str,
        gene: Optional[str] = Query(default=None),
        symbol: Optional[str] = Query(default=None),
        ensembl: Optional[str] = Query(default=None, description="Optional Ensembl gene ID"),
        efo: Optional[str] = Query(default=None),
        condition: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=1000),
    ):
        func = app.state.resolve_callable_for_key(name)
        if not func:
            raise HTTPException(status_code=404, detail=f"Unknown module: {name}")
        # Pass through extra query params
        known = {"gene", "symbol", "ensembl", "efo", "condition", "limit"}
        extras: Dict[str, Any] = {k: v for k, v in request.query_params.items() if k not in known}

        kwargs_all = _build_shared_kwargs({
            "gene": gene, "symbol": symbol, "ensembl": ensembl, "efo": efo, "condition": condition,
            "limit": limit, "extra": extras,
        })
        _, res = await _run_callable(name=name, func=func, kwargs_all=kwargs_all)
        return res

    async def _aggregate_impl(mod_keys: List[str], body: Dict[str, Any], order: str, continue_on_error: bool) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        kwargs_all = _build_shared_kwargs(body)

        if order == "parallel":
            sem = asyncio.Semaphore(DEFAULT_CONCURRENCY)
            async def _guarded(mk: str, fn: Any):
                async with sem:
                    _, res = await _run_callable(mk, fn, kwargs_all)
                    return mk, res
            tasks = []
            for mk in mod_keys:
                fn = app.state.resolve_callable_for_key(mk)
                if fn:
                    tasks.append(_guarded(mk, fn))
            if tasks:
                pairs = await asyncio.gather(*tasks)
                results = {k: v for (k, v) in pairs}
            return results

        # sequential
        for mk in mod_keys:
            fn = app.state.resolve_callable_for_key(mk)
            if not fn:
                results[mk] = {"status": "ERROR", "source": "Unknown module", "data": {}}
                if not continue_on_error:
                    break
                continue
            _, res = await _run_callable(mk, fn, kwargs_all)
            results[mk] = res
            if not continue_on_error and (not isinstance(res, dict) or res.get("status") == "ERROR"):
                break
        return results

    @app.post("/v1/aggregate")
    async def aggregate(body: AggregateRequest):
        # Choose modules
        module_keys = body.modules or list(app.state.module_keys)
        # Domain filter if provided
        domain = body.domain
        domain_id: Optional[int] = None
        if domain is not None:
            if isinstance(domain, int):
                domain_id = int(domain)
            else:
                ds = str(domain).strip().upper().lstrip("D")
                if ds.isdigit():
                    domain_id = int(ds)
            if domain_id not in {1,2,3,4,5,6}:
                raise HTTPException(status_code=400, detail="domain must be 1..6 or 'D1'..'D6'")
            module_keys = _filter_modules_for_domain(module_keys, app.state.key_to_domains, domain_id, primary_only=body.primary_only)

        # Stable order by registry order
        key_order = {k: i for i, k in enumerate(app.state.module_keys)}
        module_keys = sorted(module_keys, key=lambda k: key_order.get(k, 1_000_000))

        results = await _aggregate_impl(module_keys, body.model_dump(), body.order, body.continue_on_error)

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
                "modules": module_keys,
                "domain": domain_id,
                "order": body.order,
            },
            "results": results,
        }

    @app.post("/v1/domain/{domain_id}/run")
    async def run_domain(
        domain_id: int = Path(ge=1, le=6),
        body: Dict[str, Any] | None = Body(default=None)
    ):
        payload = body or {}
        primary_only = bool(payload.get("primary_only", True))
        module_keys = _filter_modules_for_domain(app.state.module_keys, app.state.key_to_domains, domain_id, primary_only=primary_only)
        if not module_keys:
            raise HTTPException(status_code=404, detail=f"No modules registered for domain {domain_id}")
        payload.setdefault("order", "sequential")
        payload.setdefault("continue_on_error", True)
        results = await _aggregate_impl(module_keys, payload, "sequential", True)
        return {
            "query": {"domain": domain_id, "modules": module_keys, "order": "sequential"},
            "results": results,
        }

    # ---------------- Serve Actions-friendly OpenAPI from the API itself ----------------
    ACTIONS_OPENAPI = {
        "openapi": "3.0.3",
        "info": {"title": "TargetVal Gateway (Actions Surface)", "version": "2025.10.4",
                 "description": "Compact surface for ChatGPT Actions; dynamic access to 55 modules via keys; literature; synthesis; domain sequential runner."},
        "servers": [{"url": "https://targetval-gateway.onrender.com/v1"}],
        "paths": {
            "/healthz": {"get": {"operationId": "healthzV1", "summary": "Health check",
                                 "responses": {"200": {"description": "OK",
                                                       "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/modules": {"get": {"operationId": "listModulesV1", "summary": "List canonical module keys (55) from YAML",
                                 "responses": {"200": {"description": "OK",
                                                       "content": {"application/json": {"schema": {"type": "array", "items": {"type": "string"}}}}}}}},
            "/module": {"post": {"operationId": "runModuleV1", "summary": "Run a single module by key",
                                 "requestBody": {"required": True,
                                                 "content": {"application/json": {"schema": {"type": "object",
                                                                                             "properties": {
                                                                                                 "module_key": {"type": "string"},
                                                                                                 "gene": {"type": "string"},
                                                                                                 "symbol": {"type": "string"},
                                                                                                 "ensembl": {"type": "string"},
                                                                                                 "efo": {"type": "string"},
                                                                                                 "condition": {"type": "string"},
                                                                                                 "limit": {"type": "integer", "default": 50},
                                                                                                 "extra": {"type": "object", "additionalProperties": True}},
                                                                                             "required": ["module_key"]},
                                                                      "example": {"module_key": "genetics-l2g", "symbol": "IL6", "efo": "EFO_0003767", "limit": 50}}}},
                                 "responses": {"200": {"description": "OK",
                                                       "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/module/{name}": {"get": {"operationId": "runModuleByNameV1", "summary": "Run a single module by key (GET)",
                                       "parameters": [{"in": "path", "name": "name", "required": True, "schema": {"type": "string"}},
                                                      {"in": "query", "name": "gene", "schema": {"type": "string"}},
                                                      {"in": "query", "name": "symbol", "schema": {"type": "string"}},
                                                      {"in": "query", "name": "ensembl", "schema": {"type": "string"}},
                                                      {"in": "query", "name": "efo", "schema": {"type": "string"}},
                                                      {"in": "query", "name": "condition", "schema": {"type": "string"}},
                                                      {"in": "query", "name": "limit", "schema": {"type": "integer", "default": 50}}],
                                       "responses": {"200": {"description": "OK",
                                                             "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/aggregate": {"post": {"operationId": "aggregateModulesV1",
                                    "summary": "Run many modules; sequential or parallel; optional domain filter",
                                    "requestBody": {"required": True,
                                                    "content": {"application/json": {"schema": {"type": "object",
                                                                                                "properties": {
                                                                                                    "modules": {"type": "array", "items": {"type": "string"}},
                                                                                                    "domain": {"type": "string", "description": "D1..D6 or 1..6"},
                                                                                                    "primary_only": {"type": "boolean", "default": True},
                                                                                                    "order": {"type": "string", "enum": ["sequential", "parallel"], "default": "sequential"},
                                                                                                    "continue_on_error": {"type": "boolean", "default": True},
                                                                                                    "limit": {"type": "integer", "default": 50},
                                                                                                    "gene": {"type": "string"},
                                                                                                    "symbol": {"type": "string"},
                                                                                                    "ensembl_id": {"type": "string"},
                                                                                                    "efo": {"type": "string"},
                                                                                                    "condition": {"type": "string"},
                                                                                                    "species": {"type": "integer"},
                                                                                                    "cutoff": {"type": "number"},
                                                                                                    "extra": {"type": "object", "additionalProperties": True}},
                                                                                               },
                                                                     "example": {"modules": ["genetics-l2g", "genetics-coloc", "expr-baseline"],
                                                                                 "symbol": "IL6", "efo": "EFO_0003767", "order": "sequential",
                                                                                 "continue_on_error": True, "limit": 50}}}},
                                    "responses": {"200": {"description": "OK",
                                                          "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/domain/{domain_id}/run": {"post": {"operationId": "runDomainV1",
                                                 "summary": "Run all modules of a domain sequentially; continue on errors",
                                                 "parameters": [{"in": "path", "name": "domain_id", "required": True,
                                                                 "schema": {"type": "integer", "minimum": 1, "maximum": 6}}],
                                                 "requestBody": {"required": False,
                                                                 "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True},
                                                                                                  "example": {"symbol": "IL6", "condition": "ulcerative colitis", "limit": 50}}}},
                                                 "responses": {"200": {"description": "OK",
                                                                       "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/lit/meta": {"get": {"operationId": "litMetaV1", "summary": "Literature meta/search",
                                  "parameters": [{"in": "query", "name": "query", "schema": {"type": "string"}},
                                                 {"in": "query", "name": "gene", "schema": {"type": "string"}},
                                                 {"in": "query", "name": "symbol", "schema": {"type": "string"}},
                                                 {"in": "query", "name": "efo", "schema": {"type": "string"}},
                                                 {"in": "query", "name": "condition", "schema": {"type": "string"}},
                                                 {"in": "query", "name": "limit", "schema": {"type": "integer", "default": 50}}],
                                  "responses": {"200": {"description": "OK",
                                                        "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/synth/integrate": {"post": {"operationId": "synthIntegrateV1", "summary": "Synthesis across modules",
                                          "requestBody": {"required": True,
                                                          "content": {"application/json": {"schema": {"type": "object",
                                                                                                      "properties": {"gene": {"type": "string"},
                                                                                                                     "symbol": {"type": "string"},
                                                                                                                     "efo": {"type": "string"},
                                                                                                                     "condition": {"type": "string"},
                                                                                                                     "modules": {"type": "array", "items": {"type": "string"}},
                                                                                                                     "method": {"type": "string", "enum": ["math", "vote", "rank", "bayes", "hybrid"], "default": "math"},
                                                                                                                     "extra": {"type": "object", "additionalProperties": True}},
                                                                                                     },
                                                                                     "example": {"symbol": "IL6", "condition": "ulcerative colitis",
                                                                                                 "modules": ["genetics-l2g", "expr-baseline", "mech-ppi"], "method": "math"}}}},
                                          "responses": {"200": {"description": "OK",
                                                                "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
            "/synth/bucket": {"get": {"operationId": "synthBucketV1", "summary": "Math synthesis per bucket",
                                      "parameters": [{"in": "query", "name": "name", "required": True, "schema": {"type": "string"}},
                                                     {"in": "query", "name": "gene", "schema": {"type": "string"}},
                                                     {"in": "query", "name": "symbol", "schema": {"type": "string"}},
                                                     {"in": "query", "name": "efo", "schema": {"type": "string"}},
                                                     {"in": "query", "name": "condition", "schema": {"type": "string"}},
                                                     {"in": "query", "name": "limit", "schema": {"type": "integer", "default": 50}},
                                                     {"in": "query", "name": "mode", "schema": {"type": "string", "enum": ["auto", "live", "snapshot"]}}],
                                      "responses": {"200": {"description": "OK",
                                                            "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}}
        },
        "components": {}
    }

    @app.get("/v1/actions-openapi.json", include_in_schema=False)
    def serve_actions_openapi():
        return JSONResponse(ACTIONS_OPENAPI)

    return app


app = create_app()

if __name__ == "__main__":
    try:
        import uvicorn  # dev only
    except Exception:
        print("uvicorn is not installed. For local dev: pip install uvicorn[standard]")
        sys.exit(1)
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
