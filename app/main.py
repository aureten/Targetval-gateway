
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
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Use uvicorn's ProxyHeaders (available on Render) and fall back gracefully.
try:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore
except Exception:  # pragma: no cover
    ProxyHeadersMiddleware = None  # type: ignore

try:
    from starlette.middleware.trustedhost import TrustedHostMiddleware  # type: ignore
except Exception:  # pragma: no cover
    TrustedHostMiddleware = None  # type: ignore


# -----------------------------------------------------------------------------
# App config (env-tunable; safe defaults for Render)
# -----------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "TargetVal Gateway")
APP_VERSION = os.getenv("APP_VERSION", "2025.10-adapted")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}

ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
REDOC_URL = os.getenv("REDOC_URL", "/redoc")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
ALLOW_METHODS = os.getenv("ALLOW_METHODS", "*")
ALLOW_HEADERS = os.getenv("ALLOW_HEADERS", "*")
TRUSTED_HOSTS = os.getenv("TRUSTED_HOSTS", "*")

HAS_INSIGHT = False  # set True if you include an insight router elsewhere


# -----------------------------------------------------------------------------
# Import helpers
# -----------------------------------------------------------------------------
def _import_router_module():
    """
    Try a few common import paths and return (module, where, error_text).
    We import the *module* and later access module.router to avoid brittle names.
    """
    attempts: List[str] = []
    def _try(mod: str):
        try:
            m = __import__(mod, fromlist=["router"])
            if getattr(m, "router", None) is None:
                raise ImportError(f"module {mod} has no attribute 'router'")
            return m, mod, None
        except Exception:
            return None, None, traceback.format_exc()

    for mod in (
        "app.routers.targetval_router",
        "routers.targetval_router",
        "targetval_router",
        "router",
    ):
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
# FastAPI app factory
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
        allow_origins=[o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS != "*" else ["*"],
        allow_credentials=True,
        allow_methods=[m.strip() for m in ALLOW_METHODS.split(",")] if ALLOW_METHODS != "*" else ["*"],
        allow_headers=[h.strip() for h in ALLOW_HEADERS.split(",")] if ALLOW_HEADERS != "*" else ["*"],
    )

    # Import targetval_router module and include its router
    router_module, where, import_error = _import_router_module()
    app.state.router_location = where
    app.state.router_import_error = import_error
    if router_module:
        app.include_router(router_module.router)

# Detect presence of synthesis v2 endpoints
has_v2 = False
try:
    if router_module and hasattr(router_module, "router"):
        for r in router_module.router.routes:
            p = getattr(r, "path", "")
            if p in ("/synth/integrate", "/lit/meta") or p.startswith("/synth/bucket"):
                has_v2 = True
                break
except Exception:
    has_v2 = False
app.state.has_v2 = has_v2


    # ---------------- Meta/debug endpoints ----------------
    @app.get("/livez", tags=["_meta"])
    async def livez():
        return {"ok": True, "router": app.state.router_location, "import_ok": app.state.router_import_error is None, "synthesis_v2": app.state.has_v2}

    @app.get("/readyz", tags=["_meta"])
    async def readyz():
        if app.state.router_import_error is not None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "reason": "router import failed", "details": "See /_debug/import", "synthesis_v2": app.state.has_v2},
            )
        return {"ok": True, "root_path": ROOT_PATH, "docs": DOCS_URL, "synthesis_v2": app.state.has_v2}

    

@app.get("/", tags=["_meta"])
async def about():
    tip = {
        "message": "TARGETVAL Gateway â Synthesis v2 available" if getattr(app.state, "has_v2", False) else "TARGETVAL Gateway",
        "try": [
            "/synth/bucket?name=genetics&gene=IL6&condition=ulcerative%20colitis&mode=math",
            "/synth/bucket?name=association&gene=IL6&mode=hybrid",
            "/synth/integrate?gene=IL6&condition=ulcerative%20colitis",
            "/lit/meta?symbol=IL6&condition=ulcerative%20colitis"
        ] if getattr(app.state, "has_v2", False) else ["/docs", "/redoc"]
    }
    return tip
@app.get("/_debug/import", tags=["_meta"])
    async def debug_import():
        return {
            "attempted": app.state.router_location,
            "error": app.state.router_import_error,
            "sys_path": sys.path,
        }

    @app.get("/meta/routes", tags=["_meta"])
    async def list_routes() -> Dict[str, Any]:
        routes = []
        for r in app.router.routes:
            try:
                routes.append({
                    "path": getattr(r, "path", None),
                    "name": getattr(r, "name", None),
                    "methods": sorted(getattr(r, "methods", []) or []),
                })
            except Exception:
                pass
        routes.sort(key=lambda x: (x["path"] or ""))
        return {"count": len(routes), "routes": routes, "router": app.state.router_location,
                "import_ok": app.state.router_import_error is None}

    # ---------------- Module aggregator wiring ----------------
    def _build_module_map() -> Dict[str, Any]:
        """
        Derive MODULE_MAP from the included APIRouter. This makes the app future-proof
        when new endpoints are added to router.py (no hand-maintained dicts).
        We keep a couple of human-friendly aliases stable.
        """
        mapping: Dict[str, Any] = {}
        prefixes = ("/genetics/", "/assoc/", "/expr/", "/mech/", "/tract/", "/clin/", "/comp/", "/lit/")
        if not router_module:
            return mapping
        for r in getattr(router_module, "router").routes:
            try:
                path = getattr(r, "path", "") or ""
                endpoint = getattr(r, "endpoint", None)
                name = getattr(endpoint, "__name__", None)
                if endpoint and name and any(path.startswith(p) for p in prefixes):
                    mapping[name] = endpoint
            except Exception:
                continue
        # Stable aliases users expect
        if "expression_baseline" in mapping and "expr_baseline" not in mapping:
            mapping["expr_baseline"] = mapping["expression_baseline"]
        if "genetics_cross_species" in mapping and "genetics_cross" not in mapping:
            mapping["genetics_cross"] = mapping["genetics_cross_species"]
        # Allow eqtl alias to hit sQTL/eQTL union
        if "genetics_sqtl" in mapping and "genetics_eqtl" not in mapping:
            mapping["genetics_eqtl"] = mapping["genetics_sqtl"]
        return mapping

    MODULE_MAP: Dict[str, Any] = _build_module_map()
    app.state.module_map = MODULE_MAP  # expose for debug

    class AggregateRequest(BaseModel):
        gene: Optional[str] = None
        symbol: Optional[str] = None
        ensembl_id: Optional[str] = None
        efo: Optional[str] = None
        condition: Optional[str] = None
        modules: Optional[List[str]] = None
        limit: Optional[int] = 50
        # Passthroughs used by specific modules
        species: Optional[int] = None
        cutoff: Optional[float] = None
        extra: Optional[Dict[str, Any]] = None  # arbitrary passthrough

    SYMBOLISH = re.compile(r"^[A-Za-z0-9-]+$")

    def _looks_like_symbol(s: Optional[str]) -> bool:
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
        symbol_effective = symbol if symbol else (gene if _looks_like_symbol(gene) else None)

        kwargs_all: Dict[str, Any] = {
            "gene": gene,
            "symbol": symbol_effective,
            "efo": efo,
            "condition": condition,
            "disease": condition,  # some functions use 'disease' as the param name
            "limit": limit,
            # passthrough for modules that accept it (e.g., genetics_l2g can take ensembl)
            "ensembl": ensembl_id,
        }
        if extra:
            kwargs_all.update(extra)

        kwargs = _bind_kwargs(func, kwargs_all)

        try:
            result = func(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result

            # Coerce to plain dict without relying on Pydantic class specifics
            if hasattr(result, "dict") and callable(result.dict):
                return name, result.dict()
            if hasattr(result, "model_dump") and callable(result.model_dump):  # pydantic v2
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
        except HTTPException as he:  # bubble up details but keep shape
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

    # ---------------- Public convenience endpoints ----------------
    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "modules": len(MODULE_MAP), "version": APP_VERSION, "has_insight": HAS_INSIGHT}

    @app.get("/")
    async def root():
        return {"service": "targetval-gateway", "docs": DOCS_URL, "version": app.version}

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
        out = {
            "request": {
                "gene": body.gene,
                "symbol": sym,
                "ensembl_id": body.ensembl_id,
                "efo": body.efo,
                "condition": body.condition,
                "limit": body.limit or 50,
                "modules": modules,
            },
            "results": {name: payload for (name, payload) in results},
        }
        return out

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logging.exception("Unhandled error: %r", exc)
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(status_code=500, content={"status": "ERROR", "detail": "internal error", "request_id": rid})

    return app


app = create_app()

if __name__ == "__main__":
    try:
        import uvicorn  # dev only
    except Exception:
        print("uvicorn is not installed. For local dev: pip install uvicorn[standard]")
        sys.exit(1)
    port = int(os.getenv("PORT", "8000"))
    # If your module path is different, replace "app.main:app"
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
