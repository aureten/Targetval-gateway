# app/clients/sources.py
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import httpx
from fastapi import Request

# ------------------------------------------------------------------------------------
# Model
# ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    name: str
    base_url: str
    ping_path: str = "/"
    auth_scheme: Optional[str] = None          # e.g., "Bearer" or "X-API-Key"
    auth_env: Optional[str] = None             # e.g., "OPENTARGETS_API_KEY"
    default_headers: Dict[str, str] = field(default_factory=dict)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _json_env(name: str) -> Dict[str, str]:
    raw = os.getenv(name)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return {str(k): str(v) for k, v in val.items()} if isinstance(val, dict) else {}
    except Exception:
        return {}


def _join_url(base: str, path: str) -> str:
    if not base:
        return path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not base.endswith("/") and not path.startswith("/"):
        return f"{base}/{path}"
    return f"{base}{path}"


def _make_headers(src: Source) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"targetval-gateway/{_env('GIT_SHA', 'dev')}",
        **src.default_headers,
    }
    tok = _env(src.auth_env) if src.auth_env else None
    if tok and src.auth_scheme:
        s = src.auth_scheme.lower()
        if s == "bearer":
            headers["Authorization"] = f"Bearer {tok}"
        elif s in ("x-api-key", "x_api_key"):
            headers["X-API-Key"] = tok
        else:
            headers[src.auth_scheme] = tok  # treat as raw header key
    return headers


# ------------------------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------------------------
SOURCES: Dict[str, Source] = {
    # Keep base URLs configurable via ENV; leave empty defaults harmless.
    "opentargets": Source(
        name="opentargets",
        base_url=_env("OPENTARGETS_BASE_URL", _env("OT_BASE_URL", "")),
        ping_path=_env("OPENTARGETS_PING_PATH", "/"),
        auth_scheme=_env("OPENTARGETS_AUTH_SCHEME", ""),
        auth_env=_env("OPENTARGETS_API_KEY_ENV", "OPENTARGETS_API_KEY"),
        default_headers=_json_env("OPENTARGETS_EXTRA_HEADERS"),
    ),
    "ensembl": Source(
        name="ensembl",
        base_url=_env("ENSEMBL_BASE_URL", ""),
        ping_path=_env("ENSEMBL_PING_PATH", "/info/ping"),
        auth_scheme=_env("ENSEMBL_AUTH_SCHEME", ""),
        auth_env=_env("ENSEMBL_API_KEY_ENV", "ENSEMBL_API_KEY"),
        default_headers=_json_env("ENSEMBL_EXTRA_HEADERS"),
    ),
    "uniprot": Source(
        name="uniprot",
        base_url=_env("UNIPROT_BASE_URL", ""),
        ping_path=_env("UNIPROT_PING_PATH", "/"),
        auth_scheme=_env("UNIPROT_AUTH_SCHEME", ""),
        auth_env=_env("UNIPROT_API_KEY_ENV", "UNIPROT_API_KEY"),
        default_headers=_json_env("UNIPROT_EXTRA_HEADERS"),
    ),
    "chembl": Source(
        name="chembl",
        base_url=_env("CHEMBL_BASE_URL", ""),
        ping_path=_env("CHEMBL_PING_PATH", "/"),
        auth_scheme=_env("CHEMBL_AUTH_SCHEME", ""),
        auth_env=_env("CHEMBL_API_KEY_ENV", "CHEMBL_API_KEY"),
        default_headers=_json_env("CHEMBL_EXTRA_HEADERS"),
    ),
    "gwas": Source(
        name="gwas",
        base_url=_env("GWAS_BASE_URL", ""),
        ping_path=_env("GWAS_PING_PATH", "/"),
        auth_scheme=_env("GWAS_AUTH_SCHEME", ""),
        auth_env=_env("GWAS_API_KEY_ENV", "GWAS_API_KEY"),
        default_headers=_json_env("GWAS_EXTRA_HEADERS"),
    ),
    "ols": Source(
        name="ols",
        base_url=_env("OLS_BASE_URL", ""),
        ping_path=_env("OLS_PING_PATH", "/"),
        auth_scheme=_env("OLS_AUTH_SCHEME", ""),
        auth_env=_env("OLS_API_KEY_ENV", "OLS_API_KEY"),
        default_headers=_json_env("OLS_EXTRA_HEADERS"),
    ),
    # Add more as needed…
}

# Back-compat aliases some routers may import
SOURCE_MAP = SOURCES
REGISTRY = SOURCES


def register_source(src: Source) -> None:
    SOURCES[src.name] = src


def get_source(name: str) -> Source:
    return SOURCES[name]


def iter_sources() -> Iterable[Source]:
    return SOURCES.values()


# ------------------------------------------------------------------------------------
# HTTP + retries/backoff
# ------------------------------------------------------------------------------------
_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))
_BACKOFF = float(os.getenv("HTTP_BACKOFF", "0.25"))  # seconds


async def _request_with_retries(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    json_body: Optional[Any] = None,
    retries: int = _RETRIES,
    backoff: float = _BACKOFF,
) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = await http.request(method, url, headers=headers, params=params, json=json_body)
            if r.status_code >= 500:
                # retry 5xx
                last_exc = httpx.HTTPStatusError(f"server error {r.status_code}", request=r.request, response=r)
                raise last_exc
            return r
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.HTTPStatusError) as e:
            last_exc = e
            if attempt >= retries:
                break
            await asyncio.sleep(backoff * (2**attempt))
    assert last_exc is not None
    raise last_exc


async def fetch_source_json(
    request: Request,
    source_name: str,
    path: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    json_body: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    GET (default) or POST (if json_body provided) against a named source, returning JSON.
    Raises httpx.HTTPStatusError for non-2xx so your route can map to NO_DATA vs ERROR.
    """
    src = get_source(source_name)
    http: httpx.AsyncClient = request.app.state.http
    url = _join_url(src.base_url, path)

    hdrs = _make_headers(src)
    if headers:
        hdrs.update(headers)

    method = "POST" if json_body is not None else "GET"
    r = await _request_with_retries(http, method, url, headers=hdrs, params=params, json_body=json_body)

    if r.status_code >= 400:
        # Normalize error raising so global handler can format a consistent envelope
        text = None
        try:
            text = r.text
        except Exception:
            pass
        raise httpx.HTTPStatusError(
            f"{source_name} {method} {url} -> {r.status_code}",
            request=r.request,
            response=r,
        ) from None

    try:
        return r.json()
    except Exception:
        return {"text": r.text}


# ------------------------------------------------------------------------------------
# Health pings
# ------------------------------------------------------------------------------------
async def ping_source(request: Request, src: Source, *, timeout: float = 3.0) -> Dict[str, Any]:
    http: httpx.AsyncClient = request.app.state.http
    url = _join_url(src.base_url, src.ping_path or "/")
    try:
        r = await http.get(url, headers=_make_headers(src), timeout=timeout)
        return {"ok": 200 <= r.status_code < 300, "status_code": r.status_code, "url": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "url": url}


async def ping_all(request: Request, *, per_source_timeout: float = 3.0) -> Dict[str, Dict[str, Any]]:
    tasks = [ping_source(request, src, timeout=per_source_timeout) for src in iter_sources() if src.base_url]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    out: Dict[str, Dict[str, Any]] = {}
    i = 0
    for src in iter_sources():
        if not src.base_url:
            out[src.name] = {"ok": False, "error": "base_url not configured"}
        else:
            out[src.name] = results[i]
            i += 1
    return out


# ------------------------------------------------------------------------------------
# Convenience: Open Targets GraphQL (if your modules use it)
# ------------------------------------------------------------------------------------
def _ot_graphql_endpoint() -> Tuple[str, Dict[str, str]]:
    """
    Picks an OT GraphQL endpoint from env. Two common patterns:
    - OPENTARGETS_GRAPHQL_URL  (preferred)
    - OPENTARGETS_BASE_URL + '/api/v4/graphql' (fallback guess)
    """
    url = _env("OPENTARGETS_GRAPHQL_URL")
    if not url:
        base = _env("OPENTARGETS_BASE_URL") or _env("OT_BASE_URL")
        if base:
            url = _join_url(base, "/api/v4/graphql")
    headers = _make_headers(SOURCES["opentargets"]) if "opentargets" in SOURCES else {}
    headers.setdefault("Content-Type", "application/json")
    return url, headers


async def opentargets_graphql(
    request: Request,
    query: str,
    variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Minimal GQL POST wrapper for Open Targets.
    """
    url, headers = _ot_graphql_endpoint()
    if not url:
        raise RuntimeError("Open Targets GraphQL endpoint is not configured (set OPENTARGETS_GRAPHQL_URL or OPENTARGETS_BASE_URL).")

    http: httpx.AsyncClient = request.app.state.http
    payload = {"query": query, "variables": variables or {}}
    r = await _request_with_retries(http, "POST", url, headers=headers, json_body=payload)
    data = r.json()
    if "errors" in data:
        raise httpx.HTTPStatusError(f"GraphQL error(s): {data['errors']}", request=r.request, response=r)
    return data.get("data", data)
