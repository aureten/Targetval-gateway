# main.py
"""
TargetVal Gateway — main application entrypoint (revised)

Goal: expose *all* capabilities defined in the router (buckets, modules, synth, etc.)
without introducing any restrictions. This file only wires and configures the app.

Key properties:
- Sets the router's env-based knobs BEFORE importing the router module (so defaults apply).
- Adds permissive CORS by default (ALLOWED_ORIGINS="*"; override via env for prod).
- Includes helpful meta endpoints: /, /healthz, /livez, /readyz, /_debug/import, /meta/routes.
- Does NOT impose any request-size or rate limits that could block router functions.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import List, Tuple

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

APP_NAME = os.getenv("APP_NAME", "targetval-gateway")
APP_VERSION = os.getenv("APP_VERSION", "best-of")


def _configure_env() -> None:
    """
    Configure environment defaults the router depends on.
    These MUST be set before importing the router module, because the router reads them at import time.
    """
    defaults = {
        # Router runtime knobs (see router code)
        "CACHE_TTL_SECONDS": "86400",  # 24h cache
        "OUTBOUND_TIMEOUT_S": "12.0",
        "REQUEST_BUDGET_S": "25.0",
        "OUTBOUND_TRIES": "2",
        "BACKOFF_BASE_S": "0.6",
        "MAX_CONCURRENT_REQUESTS": "8",
        "OUTBOUND_USER_AGENT": "TargetVal/2.0 (+https://github.com/aureten/Targetval-gateway)",
        # App-level knobs
        "ALLOWED_ORIGINS": "*",  # comma-separated list or "*" (recommended to scope in prod)
        # Optional: set TRUSTED_HOSTS to a comma-separated list for TrustedHostMiddleware (empty => disabled)
        "TRUSTED_HOSTS": "",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)


def _import_router() -> Tuple[str, APIRouter]:
    """
    Import the APIRouter from common locations.
    Returns (qualified_module_name, router).
    Raises RuntimeError if not found.
    """
    candidates: List[Tuple[str, str]] = [
        ("app.routers.targetval_router", "router"),
        ("app.routers.router", "router"),
        ("routers.targetval_router", "router"),
        ("routers.router", "router"),
        ("targetval_router", "router"),
        ("router", "router"),
    ]
    errors: List[str] = []
    for mod_name, attr in candidates:
        try:
            module = importlib.import_module(mod_name)
            r = getattr(module, attr, None)
            if isinstance(r, APIRouter):
                return mod_name, r
            errors.append(f"{mod_name}.{attr}: attribute not APIRouter (got {type(r).__name__})")
        except Exception as e:
            errors.append(f"{mod_name}.{attr}: {e}")
    raise RuntimeError("Unable to import router. Tried:\n" + "\n".join(errors))


# --------------- App factory ---------------

def create_app() -> FastAPI:
    _configure_env()

    app = FastAPI(
        title="TargetVal Gateway",
        version=APP_VERSION,
        description="Public-only gateway that exposes genetics, association, biology, tractability, clinical, and readiness modules via APIRouter.",
        default_response_class=JSONResponse,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Middleware — permissive & safe; tune via env for production
    allow_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
    allowed = [o.strip() for o in allow_origins_env.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )
    app.add_middleware(GZipMiddleware, minimum_size=500)
    # Respect X-Forwarded-* headers when running behind a proxy / load balancer
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    trusted_hosts = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "").split(",") if h.strip()]
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    # Meta endpoints (do not collide with router's internal /health + /status)
    @app.get("/", tags=["meta"])
    async def root():
        return {
            "service": APP_NAME,
            "version": APP_VERSION,
            "docs": "/docs",
            "redoc": "/redoc",
            "openapi": "/openapi.json",
            "meta": ["/healthz", "/livez", "/readyz", "/_debug/import", "/meta/routes"],
        }

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"ok": True}

    @app.get("/livez", tags=["meta"])
    async def livez():
        return {"live": True}

    @app.get("/readyz", tags=["meta"])
    async def readyz():
        # If the router import succeeds, we consider the app "ready".
        return {"ready": True}

    # Try to import & include the router
    mod_name, router = _import_router()
    app.include_router(router)

    @app.get("/_debug/import", tags=["meta"])
    async def debug_import():
        return {"imported_router": mod_name, "routes_added": True}

    @app.get("/meta/routes", tags=["meta"])
    async def meta_routes():
        out = []
        for r in app.router.routes:
            if isinstance(r, Route):
                methods = sorted(list(getattr(r, "methods", set()) or []))
                out.append({
                    "path": r.path,
                    "name": r.name,
                    "methods": methods,
                })
        # Sort for stability
        out.sort(key=lambda x: (x["path"], ",".join(x["methods"])))
        return {
            "count": len(out),
            "routes": out,
        }

    return app


app = create_app()

if __name__ == "__main__":
    # Optional: run with uvicorn when executed directly
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=bool(os.getenv("RELOAD", "")))
