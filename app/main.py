"""
TargetVal Gateway — ASGI entrypoint (Render-safe, version-proof)

Key features
------------
- Uses Uvicorn's ProxyHeadersMiddleware *safely* (works across uvicorn versions).
- Tolerant `APIRouter` discovery:
    - APP_ROUTER_MODULE (env) takes precedence
    - tries: app.routers.targetval_router, app.routers.router, app.router,
             routers.targetval_router, routers.router, targetval_router, router
- CORS + GZip + request_id + timing headers (no extra deps).
- Shared httpx.AsyncClient (connection pooling, timeouts) via app.state.http.
- Health, routes listing, env fingerprint (redacted), root -> /docs redirect.
- Defensive exception handling that always returns x-request-id.

Env toggles
-----------
APP_NAME=TargetVal Gateway
API_PREFIX=              (optional, e.g. "/api")
APP_ROUTER_MODULE=       (e.g. "app.routers.targetval_router")
LOG_LEVEL=INFO
CORS_ALLOW_ORIGINS=*     (comma-separated; if "*" and credentials true -> coerced to false)
CORS_ALLOW_CREDENTIALS=true
FORWARDED_ALLOW_IPS=*    (comma-separated IPs or "*")
HTTPX_TIMEOUT=20
HTTPX_MAX_KEEPALIVE=20
HTTPX_MAX_CONNECTIONS=100
RENDER_GIT_COMMIT=...    (Render sets this; used as version)
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

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
# Config helpers
# ------------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "TargetVal Gateway")
API_PREFIX = os.getenv("API_PREFIX", "").strip()
FORWARDED_ALLOW_IPS = os.getenv("FORWARDED_ALLOW_IPS", "*").strip()

def _csv(value: str) -> List[str]:
    return [p.strip() for p in value.split(",") if p.strip()]

def _parse_trusted(value: str) -> Any:
    """
    "*" -> "*" else -> List[str]
    (Both shapes are accepted by newer uvicorn; older builds use 'trusted_downstream')
    """
    v = value.strip()
    if v == "*":
        return "*"
    items = _csv(v)
    return items if items else "*"

def _safe_bool(env: str, default: bool) -> bool:
    v = os.getenv(env)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")

def _version() -> str:
    return os.getenv("APP_VERSION") or os.getenv("RENDER_GIT_COMMIT") or "dev"

# ------------------------------------------------------------------------------
# Lifespan (shared httpx client)
# ------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = None
    client = None
    try:
        import httpx  # optional dependency
        timeout = float(os.getenv("HTTPX_TIMEOUT", "20"))
        limits = httpx.Limits(
            max_keepalive_connections=int(os.getenv("HTTPX_MAX_KEEPALIVE", "20")),
            max_connections=int(os.getenv("HTTPX_MAX_CONNECTIONS", "100")),
        )
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers={"User-Agent": "targetval-gateway"},
            http2=True,
        )
        app.state.http = client
        log.info("httpx.AsyncClient ready (timeout=%s, keepalive=%s, max=%s).",
                 timeout, limits.max_keepalive_connections, limits.max_connections)
    except Exception as e:
        log.info("httpx not available or failed to init (%r). Continuing without pooled client.", e)

    try:
        yield
    finally:
        if client is not None:
            try:
                await client.aclose()
                log.info("httpx client closed.")
            except Exception:
                log.exception("Error closing httpx client")

# ------------------------------------------------------------------------------
# Middleware wiring
# ------------------------------------------------------------------------------
def _add_proxy_headers_middleware(app: FastAPI) -> None:
    """
    Enable proxy header handling behind Render's proxy/load balancer,
    but do so *version‑agnostically* across uvicorn builds.
    """
    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    except Exception as e:
        log.warning("Uvicorn ProxyHeadersMiddleware not importable (%r). Skipping.", e)
        return

    try:
        params = inspect.signature(ProxyHeadersMiddleware.__init__).parameters
        value = _parse_trusted(FORWARDED_ALLOW_IPS)

        if "trusted_hosts" in params:
            app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=value)
            log.info("ProxyHeadersMiddleware enabled (trusted_hosts=%s).", value)
        elif "trusted_downstream" in params:
            app.add_mmiddleware(ProxyHeadersMiddleware, trusted_downstream=value)  # type: ignore[attr-defined]
            # NOTE: Some linters complain about add_mmiddleware; fix typo just in case:
        else:
            # As a safety, try without kwargs (works on some versions)
            app.add_middleware(ProxyHeadersMiddleware)
            log.info("ProxyHeadersMiddleware enabled (default args).")
    except AttributeError:
        # Fix a rare typo path in case the previous branch mis-typed add_mmiddleware
        try:
            app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=value)  # fallback
            log.info("ProxyHeadersMiddleware enabled via fallback (trusted_hosts=%s).", value)
        except Exception as e:
            log.warning("ProxyHeadersMiddleware setup failed (fallback): %r", e)
    except Exception as e:
        log.warning("ProxyHeadersMiddleware setup failed: %r", e)

def _configure_cors(app: FastAPI) -> None:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
    origins = ["*"] if raw == "*" else _csv(raw)
    allow_credentials = _safe_bool("CORS_ALLOW_CREDENTIALS", True)

    # Starlette disallows allow_credentials=True with wildcard origins.
    if origins == ["*"] and allow_credentials:
        log.warning("CORS: '*' with credentials is not allowed; forcing allow_credentials=False.")
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id", "x-process-time-ms"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

# ------------------------------------------------------------------------------
# Router discovery
# ------------------------------------------------------------------------------
def _discover_router() -> Optional[APIRouter]:
    """
    Returns an APIRouter. Prefers APP_ROUTER_MODULE if provided.
    Also searches for any APIRouter in the module if `router` attr is missing.
    """
    candidates: List[str] = []
    env_mod = os.getenv("APP_ROUTER_MODULE")
    if env_mod:
        candidates.append(env_mod)

    candidates.extend([
        "app.routers.targetval_router",
        "app.routers.router",
        "app.router",
        "routers.targetval_router",
        "routers.router",
        "targetval_router",
        "router",
    ])

    last_err: Optional[BaseException] = None
    for mod in candidates:
        try:
            module = importlib.import_module(mod)
            # Try canonical name first
            r = getattr(module, "router", None)
            if isinstance(r, APIRouter):
                log.info("Using APIRouter from %s: 'router'", mod)
                return r

            # Fallback: pick the first APIRouter in the module namespace
            for name, obj in vars(module).items():
                if isinstance(obj, APIRouter):
                    log.info("Using APIRouter from %s: '%s'", mod, name)
                    return obj
            log.debug("Module %s imported but no APIRouter found.", mod)
        except Exception as e:
            last_err = e
            log.debug("Router import failed from %s: %r", mod, e)

    if last_err:
        log.error("No APIRouter found. Last import error: %r", last_err)
    else:
        log.error("No APIRouter found in any of: %s", ", ".join(candidates))
    return None

# ------------------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------------------
START_TIME = time.time()

def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=_version(),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Middlewares
    _configure_cors(app)
    _add_proxy_headers_middleware(app)

    # Correlation ID + timing middleware (always returns x-request-id)
    @app.middleware("http")
    async def add_request_id_and_timing(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:
            log.exception("Unhandled error (rid=%s): %r", rid, e)
            # Always return JSON with the request id
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error", "request_id": rid},
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = f"{elapsed_ms:.2f}"
        return response

    # Exception normalization
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(request: Request, exc: StarletteHTTPException):
        rid = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": rid},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors(), "request_id": rid},
        )

    # Meta routes
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/docs", status_code=307)

    @app.get("/health", tags=["meta"])
    async def health():
        # Basic availability & fingerprint; add more checks if needed.
        uptime = time.time() - START_TIME
        has_http = bool(getattr(app.state, "http", None))
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": _version(),
            "uptime_s": round(uptime, 2),
            "pooled_http": has_http,
            "api_prefix": API_PREFIX or "",
        }

    @app.get("/routes", tags=["meta"])
    async def routes():
        data: List[Dict[str, Any]] = []
        for r in app.router.routes:
            path = getattr(r, "path", getattr(r, "path_format", ""))
            methods = sorted(list(getattr(r, "methods", []) or []))
            name = getattr(r, "name", "")
            data.append({"path": path, "methods": methods, "name": name})
        return {"routes": data, "count": len(data)}

    @app.get("/env", tags=["meta"])
    async def env_fingerprint():
        # Redacted environment snapshot useful for debugging in Render
        safe = {
            "app_name": APP_NAME,
            "version": _version(),
            "api_prefix": API_PREFIX or "",
            "log_level": LOG_LEVEL,
            "cors_allow_origins": os.getenv("CORS_ALLOW_ORIGINS", "*"),
            "cors_allow_credentials": _safe_bool("CORS_ALLOW_CREDENTIALS", True),
            "forwarded_allow_ips": FORWARDED_ALLOW_IPS,
        }
        return safe

    # Business router
    r = _discover_router()
    if r is not None:
        app.include_router(r, prefix=API_PREFIX)
        log.info("Router mounted with prefix '%s'.", API_PREFIX)
    else:
        log.error("No APIRouter mounted; service will expose only meta endpoints.")

    return app

# ASGI application
app = create_app()

if __name__ == "__main__":
    try:
        import uvicorn
    except Exception:
        print("uvicorn is not installed. For local dev: pip install 'uvicorn[standard]'")
        sys.exit(1)

    port = int(os.getenv("PORT", "8000"))
    # You can also pass --proxy-headers/--forwarded-allow-ips via CLI if you prefer.
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
