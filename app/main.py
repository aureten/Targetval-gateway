"""
TargetVal Gateway — FastAPI entrypoint (Render-safe)

This file:
- Uses Uvicorn's ProxyHeadersMiddleware (NOT Starlette's) to avoid the
  "ModuleNotFoundError: starlette.middleware.proxy_headers" crash on Render.
- Wires your upgraded APIRouter from `router.py` (or `app/routers/router.py`).
- Adds CORS, GZip and simple health/version endpoints.
- Provides a shared httpx.AsyncClient at app.state.http (if httpx is installed).
- Keeps imports defensive so a missing optional dependency won't crash boot.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
from typing import List
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, RedirectResponse

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("targetval.main")

APP_NAME = os.getenv("APP_NAME", "TargetVal Gateway")
API_PREFIX = os.getenv("API_PREFIX", "")  # e.g., "/api"
ALLOWED_ORIGINS = [o for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o] or ["*"]
FORWARDED_ALLOW_IPS = os.getenv("FORWARDED_ALLOW_IPS", "*")

def _parse_csv(value: str):
    v = value.strip()
    if v == "*":
        return "*"
    return [p.strip() for p in v.split(",") if p.strip()]

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Optional shared HTTP client; available in routers via request.app.state.http."""
    app.state.http = None
    client = None
    try:
        import httpx  # optional
        timeout = float(os.getenv("HTTPX_TIMEOUT", "20"))
        limits = httpx.Limits(
            max_keepalive_connections=int(os.getenv("HTTPX_MAX_KEEPALIVE", "20")),
            max_connections=int(os.getenv("HTTPX_MAX_CONNECTIONS", "100")),
        )
        client = httpx.AsyncClient(timeout=timeout, limits=limits, headers={"User-Agent": "targetval-gateway"})
        app.state.http = client
        log.info("Shared httpx.AsyncClient initialized (timeout=%s).", timeout)
    except Exception as e:
        log.info("httpx not available; continuing without shared client: %r", e)
    try:
        yield
    finally:
        if client is not None:
            await client.aclose()
            log.info("Shared httpx.AsyncClient closed.")

def _try_add_proxy_middleware(app: FastAPI) -> None:
    """Enable proxy header handling when running behind Render (supports multiple uvicorn versions)."""
    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # <- key change
        kwargs = {}
        params = inspect.signature(ProxyHeadersMiddleware.__init__).parameters
        if "trusted_hosts" in params:
            kwargs["trusted_hosts"] = _parse_csv(FORWARDED_ALLOW_IPS)
        elif "trusted_downstream" in params:  # older uvicorns
            kwargs["trusted_downstream"] = _parse_csv(FORWARDED_ALLOW_IPS)
        app.add_middleware(ProxyHeadersMiddleware, **kwargs)
        log.info("ProxyHeadersMiddleware enabled via uvicorn with %s", kwargs or "defaults")
    except Exception as e:
        log.warning(
            "ProxyHeadersMiddleware not enabled (%s). If you're behind a proxy,"
            " you can also pass --proxy-headers and --forwarded-allow-ips to uvicorn.",
            repr(e),
        )

def _discover_router():
    """
    Import an APIRouter named `router` from one of these (in order):
    - env APP_ROUTER_MODULE
    - app.routers.router
    - app.router
    - routers.router
    - router
    """
    candidates: List[str] = []
    env_mod = os.getenv("APP_ROUTER_MODULE")
    if env_mod:
        candidates.append(env_mod)
    candidates.extend(["app.routers.router", "app.router", "routers.router", "router"])

    last_err: Exception | None = None
    for mod in candidates:
        try:
            module = importlib.import_module(mod)
            r = getattr(module, "router", None)
            if r is None:
                log.debug("Module %s imported but no attribute `router`.", mod)
                continue
            log.info("Using router from %s", mod)
            return r
        except Exception as e:
            last_err = e
            log.debug("Router import failed from %s: %r", mod, e)
    if last_err:
        log.warning("No router module could be imported; last error: %r", last_err)
    return None

def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=os.getenv("APP_VERSION", os.getenv("RENDER_GIT_COMMIT", "dev")),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    _try_add_proxy_middleware(app)

    # Health & meta
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/docs", status_code=307)

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "app": APP_NAME,
            "version": os.getenv("RENDER_GIT_COMMIT") or "dev",
        })

    @app.get("/routes", tags=["meta"])
    async def routes() -> JSONResponse:
        data = []
        for r in app.router.routes:
            methods = sorted(getattr(r, "methods", []))
            data.append({
                "path": getattr(r, "path", getattr(r, "path_format", "")),
                "name": getattr(r, "name", ""),
                "methods": methods,
            })
        return JSONResponse({"routes": data})

    # Plug in the business router
    r = _discover_router()
    if r is not None:
        app.include_router(r, prefix=API_PREFIX)
    else:
        log.error("No APIRouter found. Set APP_ROUTER_MODULE or place router in a default path.")

    return app

# Export ASGI application for uvicorn/gunicorn
app = create_app()

if __name__ == "__main__":
    try:
        import uvicorn
    except Exception:
        print("uvicorn is not installed. For local dev: pip install 'uvicorn[standard]'")
        sys.exit(1)
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
