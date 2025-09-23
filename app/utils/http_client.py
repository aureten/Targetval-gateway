from __future__ import annotations
import os
from typing import Optional, Dict, Any
from fastapi import HTTPException

try:
    import httpx as _http
except Exception:  # pragma: no cover
    try:
        import requests as _http  # type: ignore
    except Exception:  # pragma: no cover
        _http = None

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
ALLOW_UPSTREAM_DEGRADED = os.getenv("ALLOW_UPSTREAM_DEGRADED", "true").lower() in {"1","true","yes","on"}


def get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    required_env_keys: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """
    Lightweight HTTP GET with timeouts and a 'degraded' mode when strict upstream
    API credentials are not provided.

    If `required_env_keys` is set and any are missing, behavior depends on
    ALLOW_UPSTREAM_DEGRADED (default true):
      - true  -> return a placeholder shape with `_warning` and `_degraded` flags
      - false -> raise HTTP 424 Failed Dependency
    """
    missing = [k for k in (required_env_keys or []) if not os.getenv(k)]
    if missing:
        if ALLOW_UPSTREAM_DEGRADED:
            return {
                "_degraded": True,
                "_warning": f"Missing upstream credentials for: {', '.join(missing)}",
                "_source": url,
                "data": None,
            }
        raise HTTPException(status_code=424, detail={"error": "Upstream credentials missing", "missing": missing})

    if _http is None:
        raise HTTPException(status_code=500, detail="No HTTP client available. Install 'httpx' or 'requests'.")

    if hasattr(_http, "Client"):
        with _http.Client(timeout=REQUEST_TIMEOUT) as client:
            r = client.get(url, params=params, headers=headers)
            r.raise_for_status()
            return r.json()
    r = _http.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)  # type: ignore
    r.raise_for_status()
    return r.json()
