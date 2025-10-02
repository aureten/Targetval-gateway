from __future__ import annotations

import logging
import os
import sys
import time
import uuid
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.responses import JSONResponse, RedirectResponse

APP_NAME = os.getenv("APP_NAME", "TargetVal Gateway")
APP_VERSION = os.getenv("APP_VERSION", "2025.09")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}

ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
REDOC_URL = os.getenv("REDOC_URL", "/redoc")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
ALLOW_METHODS = os.getenv("ALLOW_METHODS", "*")
ALLOW_HEADERS = os.getenv("ALLOW_HEADERS", "*")
TRUSTED_HOSTS = os.getenv("TRUSTED_HOSTS", "*")


def _import_targetval_router():
    attempts: List[str] = []
    # Each block tries an import and on failure collects the exception traceback
    def _try(mod: str, attr: str = "router"):
        try:
            module = __import__(mod, fromlist=[attr])
            return getattr(module, attr), f"{mod}:{attr}", None
        except Exception:
            return None, None, traceback.format_exc()

    for mod in (
        "app.routers.targetval_router",
        "routers.targetval_router",
        "targetval_router",
        "router",
    ):
        router, where, err = _try(mod)
        if router is not None:
            return router, where, None
        attempts.append(f"=== Attempt {mod} failed ===\n{err}")
    return None, None, "\n".join(attempts)


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

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(ProxyHeadersMiddleware)
    if TRUSTED_HOSTS.strip() != "*":
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

    router, where, import_error = _import_targetval_router()
    app.state.router_location = where
    app.state.router_import_error = import_error

    if router is not None:
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(DOCS_URL)

    @app.get("/healthz", tags=["_meta"])
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "app": APP_NAME, "version": APP_VERSION}

    @app.get("/livez", tags=["_meta"])
    async def livez() -> Dict[str, Any]:
        return {"ok": True, "router": app.state.router_location, "import_ok": app.state.router_import_error is None}

    @app.get("/readyz", tags=["_meta"])
    async def readyz():
        if app.state.router_import_error is not None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "reason": "router import failed", "details": "See /_debug/import"},
            )
        return {"ok": True, "root_path": ROOT_PATH, "docs": DOCS_URL}

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
                routes.append({"path": r.path, "name": getattr(r, "name", None),
                               "methods": sorted(getattr(r, "methods", []))})
            except Exception:
                pass
        routes.sort(key=lambda x: x["path"])
        return {"count": len(routes), "routes": routes, "router": app.state.router_location,
                "import_ok": app.state.router_import_error is None}

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
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
