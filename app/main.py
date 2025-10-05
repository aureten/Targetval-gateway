# app/main.py
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse

# ------------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s [%(filename)s:%(lineno)d] %(message)s",
)
logger = logging.getLogger("targetval.main")


# ------------------------------------------------------------------------------------
# App + OpenAPI metadata
# ------------------------------------------------------------------------------------
TAGS_METADATA = [
    {"name": "meta", "description": "Service metadata and health"},
    {"name": "evidence", "description": "Evidence and aggregation endpoints"},
    {"name": "modules", "description": "Module-level endpoints (per upstream)"},
]

app = FastAPI(
    title="TargetVal Gateway",
    version=os.getenv("GIT_SHA", "dev"),
    description="Biologic target validation API gateway",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    contact={"name": "TargetVal"},
    license_info={"name": "MIT"},
    terms_of_service="https://example.org/terms",
    openapi_tags=TAGS_METADATA,
)


# ------------------------------------------------------------------------------------
# Security: API key middleware (optional but recommended)
# ------------------------------------------------------------------------------------
try:
    from app.middleware.api_key import APIKeyMiddleware  # type: ignore
    app.add_middleware(APIKeyMiddleware)
    logger.info("APIKeyMiddleware enabled.")
except Exception as e:  # noqa: BLE001
    logger.warning("APIKeyMiddleware not enabled: %s", e)


# ------------------------------------------------------------------------------------
# Middleware: CORS, GZip, Request-ID + timing
# ------------------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Enable gzip for JSON responses
app.add_middleware(GZipMiddleware, minimum_size=1024)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Adds X-Request-ID (if absent) and measures request duration.
    Exposes request.state.rid and request.state.t0.
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.rid = rid
        request.state.t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:  # noqa: BLE001
            # Let the global exception handlers convert it to JSON
            raise e
        finally:
            # Attach tracing headers even on exception (in the handler below)
            pass

        # Duration header
        dt_ms = (time.perf_counter() - request.state.t0) * 1000.0
        response.headers["X-Request-ID"] = rid
        response.headers["X-Response-Time-ms"] = f"{dt_ms:.1f}"
        return response


app.add_middleware(RequestContextMiddleware)


# ------------------------------------------------------------------------------------
# Shared HTTP client lifecycle (in app.state.http)
# ------------------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default


HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 15.0)
HTTP_MAX_CONNECTIONS = _env_int("HTTP_MAX_CONNECTIONS", 200)
HTTP_MAX_KEEPALIVE = _env_int("HTTP_MAX_KEEPALIVE", 50)
HTTP_FOLLOW_REDIRECTS = os.getenv("HTTP_FOLLOW_REDIRECTS", "true").lower() == "true"


@app.on_event("startup")
async def _startup() -> None:
    limits = httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_MAX_KEEPALIVE)
    app.state.http = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        limits=limits,
        follow_redirects=HTTP_FOLLOW_REDIRECTS,
        headers={"User-Agent": f"targetval-gateway/{app.version}"},
    )
    logger.info(
        "Async HTTP client initialized (timeout=%ss, max_conn=%s, keepalive=%s).",
        HTTP_TIMEOUT,
        HTTP_MAX_CONNECTIONS,
        HTTP_MAX_KEEPALIVE,
    )

    # Optional: build/attach router module map if your router exposes a builder.
    try:
        from app.routers.targetval_router import build_module_map  # type: ignore
        app.state.module_map = build_module_map()
        logger.info("Module map built with %s entries.", len(app.state.module_map))
    except Exception:
        app.state.module_map = {}
        logger.info("No module map builder found; continuing without it.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    http: Optional[httpx.AsyncClient] = getattr(app.state, "http", None)
    if http is not None:
        await http.aclose()
        logger.info("Async HTTP client closed.")


# ------------------------------------------------------------------------------------
# Exception handlers -> consistent JSON envelope
# ------------------------------------------------------------------------------------
def _error_envelope(
    request: Request,
    *,
    status_code: int,
    error: str,
    detail: Any = None,
) -> JSONResponse:
    rid = getattr(request.state, "rid", None) or uuid.uuid4().hex
    dt_ms: Optional[float] = None
    if hasattr(request.state, "t0"):
        dt_ms = (time.perf_counter() - request.state.t0) * 1000.0

    payload: Dict[str, Any] = {
        "status": "ERROR",
        "error": error,
        "detail": detail,
        "request_id": rid,
        "elapsed_ms": round(dt_ms, 1) if dt_ms is not None else None,
    }
    return JSONResponse(payload, status_code=status_code, headers={"X-Request-ID": rid})


@app.exception_handler(httpx.RequestError)
async def _handle_httpx_request_error(request: Request, exc: httpx.RequestError):
    logger.exception("Upstream request error: %s", exc)
    return _error_envelope(request, status_code=502, error="UPSTREAM_REQUEST_ERROR", detail=str(exc))


@app.exception_handler(httpx.HTTPStatusError)
async def _handle_httpx_status_error(request: Request, exc: httpx.HTTPStatusError):
    logger.exception("Upstream status error: %s", exc)
    detail = {
        "url": str(exc.request.url) if exc.request else None,
        "status_code": exc.response.status_code if exc.response else None,
        "text": exc.response.text[:500] if exc.response is not None else None,
    }
    return _error_envelope(request, status_code=502, error="UPSTREAM_STATUS_ERROR", detail=detail)


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return _error_envelope(request, status_code=500, error="UNHANDLED_EXCEPTION", detail=str(exc))


# ------------------------------------------------------------------------------------
# OpenAPI security: annotate X-API-Key as a header-based apiKey scheme
# ------------------------------------------------------------------------------------
def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = app.original_openapi()
    # Add an API key security scheme if not present
    components = schema.setdefault("components", {})
    sec_schemes = components.setdefault("securitySchemes", {})
    sec_schemes["ApiKeyAuth"] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    # Make it global default
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


# Patch in our customizer
app.original_openapi = app.openapi  # type: ignore[attr-defined]
app.openapi = _custom_openapi  # type: ignore[assignment]


# ------------------------------------------------------------------------------------
# Routers
# ------------------------------------------------------------------------------------
try:
    from app.routers.targetval_router import router as targetval_router  # type: ignore
    app.include_router(targetval_router)
    logger.info("targetval_router mounted.")
except Exception as e:  # noqa: BLE001
    logger.warning("targetval_router not mounted: %s", e)


# ------------------------------------------------------------------------------------
# Utility endpoints
# ------------------------------------------------------------------------------------
@app.get("/", tags=["meta"])
async def root() -> Dict[str, Any]:
    return {
        "name": "TargetVal Gateway",
        "version": app.version,
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/__diag/http", tags=["meta"])
async def diag_http(request: Request) -> Dict[str, Any]:
    http: httpx.AsyncClient = request.app.state.http
    r = await http.get("https://example.org/")
    return {"ok": r.status_code == 200, "status_code": r.status_code}


@app.get("/healthz", tags=["meta"])
async def healthz(request: Request) -> Dict[str, Any]:
    # Import here to avoid circular import on startup
    from app.clients.sources import ping_all  # type: ignore

    summary = await ping_all(request, per_source_timeout=_env_float("HEALTH_PING_TIMEOUT", 3.0))
    status = "UP" if summary and all(s.get("ok", False) for s in summary.values()) else "DEGRADED"
    return {"status": status, "version": app.version, "upstreams": summary}
