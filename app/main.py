
"""
TargetVal Gateway — FastAPI entrypoint
------------------------------------------------------------
Production-safe main.py aligned with app/routers/targetval_router.py.
- Imports the APIRouter exported as `router` (with robust fallbacks).
- Adds CORS, GZip, and ProxyHeaders middleware (Render / other PaaS).
- Uniform health checks and a friendly root that redirects to /docs.
- Minimal logging + request-id propagation.
- Introspection endpoint to list registered routes for quick sanity.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

# ----------------------------------------------------------------------------------
# Configuration via environment variables (safe defaults for Render & local dev)
# ----------------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "TargetVal Gateway")
APP_VERSION = os.getenv("APP_VERSION", "2025.09")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}

# If you deploy behind a reverse proxy on a subpath, set ROOT_PATH (e.g. "/api")
ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
REDOC_URL = os.getenv("REDOC_URL", "/redoc")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

# CORS
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
ALLOW_METHODS = os.getenv("ALLOW_METHODS", "*")
ALLOW_HEADERS = os.getenv("ALLOW_HEADERS", "*")
TRUSTED_HOSTS = os.getenv("TRUSTED_HOSTS", "*")

# ----------------------------------------------------------------------------------
# Helper: import the TargetVal router robustly (works with several repo layouts)
# Expected file: app/routers/targetval_router.py defining `router = APIRouter()`
# Fallbacks: routers/targetval_router.py OR router.py at project root.
# ----------------------------------------------------------------------------------
def _import_targetval_router():
    err_chain: List[str] = []

    # Preferred canonical path
    try:
        from app.routers.targetval_router import router as targetval_router  # type: ignore
        return targetval_router, "app.routers.targetval_router:router"
    except Exception as e:  # noqa: BLE001
        err_chain.append(f"app.routers.targetval_router: {e!r}")

    # Alt: module with symbol `router`
    try:
        from routers.targetval_router import router as targetval_router  # type: ignore
        return targetval_router, "routers.targetval_router:router"
    except Exception as e:  # noqa: BLE001
        err_chain.append(f"routers.targetval_router: {e!r}")

    # Alt: plain module name with exported `router`
    try:
        from targetval_router import router as targetval_router  # type: ignore
        return targetval_router, "targetval_router:router"
    except Exception as e:  # noqa: BLE001
        err_chain.append(f"targetval_router: {e!r}")

    # Last-resort fallback: a top-level router.py (the file you attached)
    try:
        from router import router as targetval_router  # type: ignore
        return targetval_router, "router:router"
    except Exception as e:  # noqa: BLE001
        err_chain.append(f"router: {e!r}")

    raise ImportError(
        "Could not import the TargetVal APIRouter. Tried: "
        + " | ".join(err_chain)
    )

# ----------------------------------------------------------------------------------
# Middleware: Request ID (preserve or generate) to stitch logs across services
# ----------------------------------------------------------------------------------
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
        # Basic latency log (avoid noisy bodies/headers)
        try:
            duration_ms = (time.perf_counter() - start) * 1000.0
            logging.getLogger("uvicorn.access").info(
                "%s %s -> %s (%.1f ms) rid=%s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                rid,
            )
        except Exception:
            pass
        return response

# ----------------------------------------------------------------------------------
# App factory (allows reuse in tests)
# ----------------------------------------------------------------------------------
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
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # compress JSON > 1KB
    app.add_middleware(RequestIDMiddleware)

    # X-Forwarded-* handling for Render/other proxies
    app.add_middleware(ProxyHeadersMiddleware)

    # Trusted hosts (optional hardening)
    if TRUSTED_HOSTS.strip() != "*":
        hosts = [h.strip() for h in TRUSTED_HOSTS.split(",") if h.strip()]
        if hosts:
            app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    # CORS (wide-open by default to keep it public-friendly)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS != "*" else ["*"],
        allow_credentials=True,
        allow_methods=[m.strip() for m in ALLOW_METHODS.split(",")] if ALLOW_METHODS != "*" else ["*"],
        allow_headers=[h.strip() for h in ALLOW_HEADERS.split(",")] if ALLOW_HEADERS != "*" else ["*"],
    )

    # Routers
    router, where = _import_targetval_router()
    app.include_router(router)  # all the genetics/association/biology/.../synth endpoints
    app.state.router_location = where

    # ----------------------------------------------------------------------------------
    # Housekeeping & health
    # ----------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        # Friendly redirect straight into Swagger UI
        return RedirectResponse(DOCS_URL)

    @app.get("/healthz", tags=["_meta"])
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "app": APP_NAME, "version": APP_VERSION}

    @app.get("/livez", tags=["_meta"])
    async def livez() -> Dict[str, Any]:
        # If import succeeded, we consider the process live
        return {"ok": True, "router": app.state.router_location}

    @app.get("/readyz", tags=["_meta"])
    async def readyz() -> Dict[str, Any]:
        # Basic readiness: we know the router was imported; nothing else is blocking start
        return {"ok": True, "root_path": ROOT_PATH, "docs": DOCS_URL}

    @app.get("/meta/routes", tags=["_meta"])
    async def list_routes() -> Dict[str, Any]:
        # Quick introspection to confirm your synthesis routes are wired in
        routes = []
        for r in app.router.routes:
            try:
                routes.append({"path": r.path, "name": getattr(r, "name", None), "methods": sorted(getattr(r, "methods", []))})
            except Exception:
                pass
        routes.sort(key=lambda x: x["path"])
        return {"count": len(routes), "routes": routes, "router": app.state.router_location}

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # Keep it concise but useful; don't leak stack traces in prod
        logging.exception("Unhandled error: %r", exc)
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=500,
            content={"status": "ERROR", "detail": "internal error", "request_id": rid},
        )

    return app

# expose ASGI app for uvicorn/gunicorn
app = create_app()

# Local dev convenience: `python -m app.main` will run a server.
if __name__ == "__main__":
    try:
        import uvicorn  # type: ignore
    except Exception:  # pragma: no cover - uvicorn may not be installed
        print("uvicorn is not installed. For local dev: pip install uvicorn[standard]")
        sys.exit(1)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
