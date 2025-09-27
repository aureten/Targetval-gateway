from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, Optional, Iterable

from fastapi import HTTPException

try:
    import httpx  # async-first
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

try:
    import requests  # sync fallback
except Exception:  # pragma: no cover
    requests = None  # type: ignore

REQUEST_TIMEOUT_SECONDS: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
USER_AGENT: str = os.getenv("OUTBOUND_USER_AGENT", "TargetVal/1.3 (+https://github.com/aureten/Targetval-gateway)")
ALLOW_UPSTREAM_DEGRADED: bool = os.getenv("ALLOW_UPSTREAM_DEGRADED", "true").lower() in {"1","true","yes","on"}

# Default timeout for httpx
DEFAULT_TIMEOUT = None
if httpx is not None:
    DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=4.0)

def _ua_headers(extra: Optional[Dict[str,str]]=None) -> Dict[str,str]:
    base = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra:
        base.update(extra)
    return base

def require_or_degrade(required_env_keys: Iterable[str]) -> Optional[Dict[str, Any]]:
    """
    Gate for upstream creds. If any required env var is missing:
      - return a placeholder dict when ALLOW_UPSTREAM_DEGRADED=true
      - else raise HTTP 424
    Callers can early-return this placeholder to signal degraded mode.
    """
    missing = [k for k in (required_env_keys or []) if not os.getenv(k)]
    if not missing:
        return None
    if ALLOW_UPSTREAM_DEGRADED:
        return {
            "_degraded": True,
            "_warning": f"Missing upstream credentials for: {', '.join(missing)}",
        }
    raise HTTPException(status_code=424, detail={"error": "Upstream credentials missing", "missing": missing})

# ------------------------- Async HTTP (preferred) -----------------------------

async def aget_json(url: str, *, params: Optional[Dict[str, Any]]=None,
                    headers: Optional[Dict[str,str]]=None, tries: int = 3) -> Dict[str, Any] | list:
    if httpx is None:
        # Fall back via thread to sync get_json (rare)
        import functools
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(get_json, url, params=params, headers=headers, tries=tries))

    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=_ua_headers(headers)) as client:
        for _ in range(max(1, tries)):
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # narrow if needed
                last_err = e
                await asyncio.sleep(0.8)
    raise RuntimeError(f"GET failed for {url}: {last_err}")

async def apost_json(url: str, payload: Dict[str, Any], *, headers: Optional[Dict[str,str]]=None, tries: int = 3) -> Dict[str, Any] | list:
    if httpx is None:
        import functools
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(post_json, url, payload, headers=headers, tries=tries))

    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=_ua_headers(headers)) as client:
        for _ in range(max(1, tries)):
            try:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.8)
    raise RuntimeError(f"POST failed for {url}: {last_err}")

# --------------------------- Sync HTTP (fallback) -----------------------------

def _sync_client_get(url: str, *, params: Optional[Dict[str, Any]], headers: Optional[Dict[str,str]]):
    if httpx is not None:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, headers=_ua_headers(headers)) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    if requests is not None:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS, headers=_ua_headers(headers))
        r.raise_for_status()
        return r.json()
    raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")

def _sync_client_post(url: str, *, payload: Dict[str, Any], headers: Optional[Dict[str,str]]):
    if httpx is not None:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, headers=_ua_headers(headers)) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            return r.json()
    if requests is not None:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS, headers=_ua_headers(headers))
        r.raise_for_status()
        return r.json()
    raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")

def get_json(url: str, *, params: Optional[Dict[str, Any]]=None,
             headers: Optional[Dict[str,str]]=None, tries: int = 3) -> Dict[str, Any] | list:
    last_err: Optional[Exception] = None
    for _ in range(max(1, tries)):
        try:
            return _sync_client_get(url, params=params, headers=headers)
        except Exception as e:
            last_err = e
            import time as _t; _t.sleep(0.8)
    raise RuntimeError(f"GET failed for {url}: {last_err}")

def post_json(url: str, payload: Dict[str, Any], *, headers: Optional[Dict[str,str]]=None, tries: int = 3) -> Dict[str, Any] | list:
    last_err: Optional[Exception] = None
    for _ in range(max(1, tries)):
        try:
            return _sync_client_post(url, payload=payload, headers=headers)
        except Exception as e:
            last_err = e
            import time as _t; _t.sleep(0.8)
    raise RuntimeError(f"POST failed for {url}: {last_err}")
