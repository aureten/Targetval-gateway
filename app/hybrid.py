# app/hybrid.py
# One-file hybrid layer: mode routing (auto|live|snapshot), provenance, fallbacks, simple TTL cache.

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from typing import Any, Callable, Dict, Optional, Literal, Tuple

from fastapi import Header, Query
from pydantic import BaseModel, Field

SourceMode = Literal["auto", "live", "snapshot"]

# ---------- Minimal registry (defaults you can extend per module) ----------
# You can override these in-code where you wrap each function.
REGISTRY: Dict[str, Dict[str, Any]] = {
    # module_name: {default: "live|snapshot|auto", ttl_sec: int}
    "mech_ppi":          {"default": "live",     "ttl_sec": 7 * 24 * 3600},
    "mech_pathways":     {"default": "live",     "ttl_sec": 7 * 24 * 3600},
    "tract_drugs":       {"default": "auto",     "ttl_sec": 14 * 24 * 3600},
    "genetics_sqtl":     {"default": "snapshot", "ttl_sec": 180 * 24 * 3600},
    "genetics_mr":       {"default": "snapshot", "ttl_sec": 90 * 24 * 3600},
    "expr_inducibility": {"default": "snapshot", "ttl_sec": 365 * 24 * 3600},
    "tract_modality":    {"default": "snapshot", "ttl_sec": 90 * 24 * 3600},
    "clin_endpoints":    {"default": "live",     "ttl_sec": 3 * 24 * 3600},
    # add others as needed; absent keys fall back to defaults provided at wrap-time
}

# ---------- Provenance & Evidence envelopes ----------
class Provenance(BaseModel):
    mode: SourceMode
    source: str
    source_version: Optional[str] = None
    snapshot_id: Optional[str] = None
    retrieved_at: Optional[str] = None
    router_reason: Optional[str] = None


class Diagnostics(BaseModel):
    latency_ms: Optional[int] = None
    cache: Optional[str] = None
    fallbacks: list[dict] = Field(default_factory=list)


class Evidence(BaseModel):
    module: str
    status: Literal["OK", "NO_DATA", "UPSTREAM_ERROR", "CLIENT_ERROR", "API_CHANGED", "ERROR"]
    fetched_n: int
    data: Any
    provenance: Provenance
    diagnostics: Diagnostics = Diagnostics()


def _now_iso() -> str:
    # RFC3339-lite
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_payload(
    module: str,
    payload: Any,
    mode: SourceMode,
    source: str,
    src_version: Optional[str],
    snapshot_id: Optional[str],
    latency_ms: Optional[int],
    cache: Optional[str],
    router_reason: str,
    fallbacks: list[dict],
) -> Evidence:
    # Try to read status/fetched_n if upstream already returns an envelope; otherwise infer.
    status = "OK"
    data = payload
    fetched_n = 0
    if isinstance(payload, dict) and "data" in payload and "status" in payload:
        status = payload.get("status", "OK")
        data = payload.get("data")
        fetched_n = payload.get("fetched_n", (len(data) if hasattr(data, "__len__") else 1))
    else:
        # raw list or dict
        fetched_n = (len(payload) if hasattr(payload, "__len__") and not isinstance(payload, (str, bytes)) else 1)

    prov = Provenance(
        mode=mode,
        source=source,
        source_version=src_version,
        snapshot_id=snapshot_id,
        retrieved_at=_now_iso(),
        router_reason=router_reason,
    )
    diag = Diagnostics(latency_ms=latency_ms, cache=cache, fallbacks=fallbacks)
    return Evidence(
        module=module,
        status=status,
        fetched_n=fetched_n,
        data=data,
        provenance=prov,
        diagnostics=diag,
    )


# ---------- Tiny per-process TTL cache (keeps changes minimal) ----------
class _TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    def _ts(self) -> float:
        return time.time()

    def _key(self, module: str, mode: str, params: dict) -> str:
        # stable hash by sorting keys
        s = json.dumps({"m": module, "mode": mode, "p": params}, sort_keys=True, default=str)
        return hashlib.sha1(s.encode("utf-8")).hexdigest()

    async def get(self, module: str, mode: str, params: dict) -> Optional[Any]:
        k = self._key(module, mode, params)
        async with self._lock:
            item = self._store.get(k)
            if not item:
                return None
            exp, val = item
            if self._ts() > exp:
                # expired
                self._store.pop(k, None)
                return None
            return val

    async def set(self, module: str, mode: str, params: dict, ttl_sec: int, value: Any) -> None:
        k = self._key(module, mode, params)
        async with self._lock:
            self._store[k] = (self._ts() + ttl_sec, value)


CACHE = _TTLCache()


# ---------- Mode resolution as a FastAPI dependency ----------
async def get_source_mode(
    mode: Optional[SourceMode] = Query(default=None),
    x_source_mode: Optional[SourceMode] = Header(default=None),
) -> SourceMode:
    # prefer explicit query over header; default to auto
    return (mode or x_source_mode or "auto")  # type: ignore[return-value]


# ---------- Helpers to call sync/async functions uniformly ----------
async def _maybe_await(func: Callable, **kwargs):
    if inspect.iscoroutinefunction(func):
        return await func(**kwargs)
    return func(**kwargs)


# ---------- The core wrapper factory ----------
def hybridize(
    module: str,
    live_fn: Callable[..., Any],
    snapshot_fn: Optional[Callable[..., Any]] = None,
    *,
    default: Optional[SourceMode] = None,  # overrides REGISTRY default
    ttl_sec: Optional[int] = None,         # overrides REGISTRY ttl
    source_name_live: str = "LIVE",
    source_name_snapshot: str = "SNAPSHOT",
) -> Callable[..., Any]:
    """
    Wrap a 'live' callable with hybrid behavior:
      - choose mode: live | snapshot | auto
      - TTL cache per (module,mode,params)
      - fallback live->snapshot on error
      - attach provenance + diagnostics

    The wrapped function accepts original params + an optional 'mode' kwarg.
    Return type: Evidence (module-wrapped).
    """

    reg = REGISTRY.get(module, {})
    _default = default or reg.get("default", "auto")
    _ttl = ttl_sec or reg.get("ttl_sec", 24 * 3600)

    async def _wrapped(*, mode: Optional[SourceMode] = None, **params) -> Evidence:
        chosen: SourceMode = (mode or _default)  # type: ignore[assignment]
        reason = "user_forced" if mode else "policy_default"

        # --------- Cache probe ----------
        # If caller asked for 'auto', try to reuse any existing resolved cache (live first, then snapshot).
        if chosen == "auto":
            cached = await CACHE.get(module, "live", params) or await CACHE.get(module, "snapshot", params)
        else:
            cached = await CACHE.get(module, chosen, params)

        if cached is not None:
            ev: Evidence = cached
            ev.diagnostics.cache = "hit"
            return ev

        t0 = time.time()
        fallbacks: list[dict] = []
        effective: SourceMode = chosen
        src_version: Optional[str] = None
        snapshot_id: Optional[str] = None

        try:
            if chosen == "snapshot":
                if not snapshot_fn:
                    raise RuntimeError("snapshot_unavailable")
                payload = await _maybe_await(snapshot_fn, **params)
                source = source_name_snapshot
                snapshot_id = getattr(snapshot_fn, "__snapshot_id__", None)

            elif chosen == "live":
                payload = await _maybe_await(live_fn, **params)
                source = source_name_live

            else:
                # auto: try live first, fallback to snapshot on error (if provided)
                try:
                    payload = await _maybe_await(live_fn, **params)
                    source = source_name_live
                    effective = "live"  # type: ignore[assignment]
                    reason = "auto_live_ok"
                except Exception as e_live:
                    if not snapshot_fn:
                        raise
                    fallbacks.append(
                        {"module": module, "from": "live", "to": "snapshot", "reason": e_live.__class__.__name__}
                    )
                    payload = await _maybe_await(snapshot_fn, **params)
                    source = source_name_snapshot
                    effective = "snapshot"  # type: ignore[assignment]
                    reason = "auto_fallback_snapshot"
                    snapshot_id = getattr(snapshot_fn, "__snapshot_id__", None)

            latency_ms = int((time.time() - t0) * 1000)
            ev = _normalize_payload(
                module=module,
                payload=payload,
                mode=effective,
                source=source,
                src_version=src_version,
                snapshot_id=snapshot_id,
                latency_ms=latency_ms,
                cache="miss",
                router_reason=reason,
                fallbacks=fallbacks,
            )
            # Cache under the **effective** mode so future 'auto' requests can reuse it.
            await CACHE.set(module, effective, params, _ttl, ev)
            return ev

        except Exception as e:
            latency_ms = int((time.time() - t0) * 1000)
            ev = Evidence(
                module=module,
                status="UPSTREAM_ERROR",
                fetched_n=0,
                data=[],
                provenance=Provenance(
                    mode=(mode or _default),  # type: ignore[arg-type]
                    source="ERROR",
                    router_reason=f"exception:{e.__class__.__name__}",
                ),
                diagnostics=Diagnostics(latency_ms=latency_ms, cache="miss", fallbacks=fallbacks),
            )
            return ev

    # Nice: keep repr informative
    _wrapped.__name__ = f"hybrid_{module}"
    return _wrapped


# ---------- Aggregate helper (optional use) ----------
async def run_aggregate(
    modules: list[str],
    wrappers: Dict[str, Callable[..., Any]],
    common_params: Dict[str, Any],
    mode: Optional[SourceMode] = None,
) -> Dict[str, Evidence]:
    out: Dict[str, Evidence] = {}
    # Simple sequential; flip to asyncio.gather if your wrapped callables are async and independent.
    for m in modules:
        w = wrappers.get(m)
        if not w:
            out[m] = Evidence(
                module=m,
                status="CLIENT_ERROR",
                fetched_n=0,
                data=[],
                provenance=Provenance(mode=(mode or "auto"), source="N/A", router_reason="module_not_wrapped"),
                diagnostics=Diagnostics(),
            )
            continue
        ev = await w(mode=mode, **common_params)
        out[m] = ev
    return out
