
"""
TargetVal validation & resolution utilities
==========================================

Design goals
------------
- Safe to import in Render or any ASGI context (NO network at import time).
- Tiny, dependency-light, and testable.
- Public API kept stable: validate_symbol, validate_condition, resolve_gene,
  resolve_condition, normalize_* helpers, validate_limit, http_error, get_http,
  ResolvedGene, ResolvedCondition.

What this module does
---------------------
- *Validators* (no network):
    validate_symbol(value, field_name="gene")
    validate_condition(value, field_name="efo")

- *Resolvers* (optional network, when called):
    resolve_gene(value, http=None, timeout=15.0) -> ResolvedGene
    resolve_condition(value, http=None, timeout=15.0) -> ResolvedCondition

  Resolvers accept an optional httpx.AsyncClient and create/close their own
  short-lived client when not provided. They tolerate upstream hiccups and
  return best-effort results with warnings rather than raising.

- *Utilities*:
    get_http(request)                 -> shared httpx.AsyncClient from app.state.http (if any)
    normalize_gene_symbol(value)      -> uppercase & strip
    is_ensembl_gene_id(value)         -> bool
    normalize_efo_id(value)           -> 'EFO_000XXXX' (HP_/MONDO_ also padded)
    validate_limit(limit, lo, hi)     -> raises on out-of-bounds
    http_error(status, msg, rid=None) -> FastAPI HTTPException factory

Environment toggles
-------------------
HTTP_TIMEOUT      (default: 15.0 seconds)
HTTP_RETRIES      (default: 3)
HTTP_BACKOFF      (default: 0.25 seconds base)
GENE_CACHE_TTL_S  (default: 43200 = 12h)
EFO_CACHE_TTL_S   (default: 43200 = 12h)
CACHE_MAXSIZE     (default: 8192)
HTTP_USER_AGENT   (default: "targetval-validation")

No secrets/keys are referenced here.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

try:  # optional dependency; only needed if you call the resolvers
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

DEFAULT_TIMEOUT = _float_env("HTTP_TIMEOUT", 15.0)
DEFAULT_RETRIES = _int_env("HTTP_RETRIES", 3)
DEFAULT_BACKOFF = _float_env("HTTP_BACKOFF", 0.25)
HTTP_UA = os.getenv("HTTP_USER_AGENT", "targetval-validation")

GENE_CACHE_TTL_S = _int_env("GENE_CACHE_TTL_S", 12 * 3600)
EFO_CACHE_TTL_S  = _int_env("EFO_CACHE_TTL_S",  12 * 3600)
CACHE_MAXSIZE    = _int_env("CACHE_MAXSIZE",    8192)

# ----------------------------------------------------------------------------
# Basic validators (no network)
# ----------------------------------------------------------------------------

_GENE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_ENSEMBL_GENE_RE = re.compile(r"^ENSG\d{11}(?:\.\d+)?$", re.IGNORECASE)
_UNIPROT_RE = re.compile(r"^[A-NR-Z][0-9][A-Z0-9]{3}[0-9](?:-[0-9]+)?$", re.IGNORECASE)  # coarse

_EFO_RE = re.compile(r"^(EFO|HP|MONDO)[:_]?\d{3,9}$", re.IGNORECASE)

def _ensure_nonempty(value: Optional[str], field: str) -> str:
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing '{field}'")
    v = str(value).strip()
    if not v:
        raise HTTPException(status_code=422, detail=f"Empty '{field}'")
    return v

def validate_symbol(value: Optional[str], field_name: str = "gene") -> str:
    """Permissive gene validator.
    Accepts symbols (e.g., GPR75), Ensembl (ENSG...), or UniProt accessions.
    Does *not* hit the network.
    """
    v = _ensure_nonempty(value, field_name)
    if _ENSEMBL_GENE_RE.match(v) or _UNIPROT_RE.match(v) or _GENE_RE.match(v):
        return v
    raise HTTPException(status_code=422, detail=f"Invalid {field_name} '{v}'")

def validate_condition(value: Optional[str], field_name: str = "efo") -> str:
    """Accepts EFO/HP/MONDO IDs (EFO_0001073 / EFO:0001073) or free text labels.
    Does *not* hit the network. Resolution to IDs is handled by `resolve_condition`.
    """
    v = _ensure_nonempty(value, field_name)
    return v  # free text allowed; normalization handled later

# ----------------------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------------------

def normalize_gene_symbol(symbol: str) -> str:
    return str(symbol).strip().upper()

def is_ensembl_gene_id(value: str) -> bool:
    return bool(_ENSEMBL_GENE_RE.match(str(value).strip()))

def normalize_efo_id(value: str) -> str:
    """Normalize EFO/HP/MONDO IDs into PREFIX_000XXXX (7+ digits padded)."""
    v = str(value).strip().upper().replace(":", "_")
    if _EFO_RE.match(v):
        if "_" in v:
            prefix, num = v.split("_", 1)
        else:
            prefix, num = v[:3], v[3:]
        # strip leading zeros just to re-pad deterministically
        num = re.sub(r"^0+", "", num)
        try:
            padded = f"{int(num):07d}"
            return f"{prefix}_{padded}"
        except Exception:
            return f"{prefix}_{num}"
    return v

# ----------------------------------------------------------------------------
# Resolved entities
# ----------------------------------------------------------------------------

@dataclass
class ResolvedGene:
    query: str
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    uniprot_id: Optional[str] = None
    species: str = "Homo sapiens"
    warning: Optional[str] = None

@dataclass
class ResolvedCondition:
    query: str
    efo_id: Optional[str] = None
    label: Optional[str] = None
    iri: Optional[str] = None
    warning: Optional[str] = None

# ----------------------------------------------------------------------------
# Tiny TTL caches (module-local, no external deps)
# ----------------------------------------------------------------------------

class _TTLCache:
    def __init__(self, ttl_seconds: float = 6 * 3600, maxsize: int = 4096):
        self.ttl = float(ttl_seconds)
        self.maxsize = int(maxsize)
        self._data: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        item = self._data.get(key)
        if not item:
            return None
        expires, val = item
        if expires < now:
            self._data.pop(key, None)
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self.maxsize:
            # cheap eviction: drop ~1/16 oldest by timestamp
            try:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[: max(1, self.maxsize // 16)]
                for k, _ in oldest:
                    self._data.pop(k, None)
            except Exception:
                self._data.clear()
        self._data[key] = (time.time() + self.ttl, value)

_gene_cache = _TTLCache(ttl_seconds=GENE_CACHE_TTL_S, maxsize=CACHE_MAXSIZE)
_efo_cache = _TTLCache(ttl_seconds=EFO_CACHE_TTL_S,  maxsize=CACHE_MAXSIZE)

# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

def get_http(request) -> Optional["httpx.AsyncClient"]:
    """Return shared AsyncClient from `app.state.http` if available."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "http", None)

async def _ensure_http(http: Optional["httpx.AsyncClient"], timeout: float) -> "httpx.AsyncClient":
    if http is not None:
        return http
    if httpx is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="httpx is not installed on the server")
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": HTTP_UA, "Accept": "application/json"},
        http2=True,
    )

async def _sleep_backoff(attempt: int, base: float) -> None:
    await asyncio.sleep(base * (attempt + 1))

async def _get_json(
    http: "httpx.AsyncClient",
    url: str,
    timeout: float,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> Optional[Any]:
    """GET a URL with tolerant JSON parsing, throttling/backoff, and limited retries."""
    for attempt in range(max(1, retries)):
        try:
            r = await http.get(url, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return None
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra is not None else backoff * (attempt + 1)
                except Exception:
                    delay = backoff * (attempt + 1)
                await asyncio.sleep(delay)
                continue
            if 500 <= r.status_code < 600:
                await _sleep_backoff(attempt, backoff)
                continue
            return None
        except Exception:
            await _sleep_backoff(attempt, backoff)
    return None

# ----------------------------------------------------------------------------
# Gene resolution (Ensembl first, UniProt fallback)
# ----------------------------------------------------------------------------

async def _resolve_gene_via_ensembl(symbol: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedGene]:
    # Try Ensembl xrefs/symbol (list)
    url = f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{symbol}?content-type=application/json"
    js = await _get_json(http, url, timeout=timeout)
    if not js:
        # Newer lookup endpoint (single object)
        url2 = f"https://rest.ensembl.org/lookup/symbol/homo_sapiens/{symbol}?content-type=application/json"
        js2 = await _get_json(http, url2, timeout=timeout)
        if not js2:
            return None
        ensg = js2.get("id")
        if ensg:
            return ResolvedGene(query=symbol, symbol=symbol, ensembl_id=str(ensg))
        return None

    # xrefs returns a list; take first gene
    try:
        for rec in js:
            if str(rec.get("type", "")).lower() == "gene":
                ensg = rec.get("id") or rec.get("primary_id")
                if ensg:
                    return ResolvedGene(query=symbol, symbol=symbol, ensembl_id=str(ensg))
        # fallback: take first record's id
        if isinstance(js, list) and js:
            ensg = js[0].get("id") or js[0].get("primary_id")
            if ensg:
                return ResolvedGene(query=symbol, symbol=symbol, ensembl_id=str(ensg))
    except Exception:
        pass
    return None

async def _resolve_gene_via_uniprot(symbol: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedGene]:
    # Search UniProt for primary gene in human (9606)
    if httpx is None:  # pragma: no cover
        return None
    query = f"gene_exact:{symbol} AND organism_id:9606"
    params = httpx.QueryParams({"query": query, "fields": "accession,gene_primary", "size": 1})
    url = f"https://rest.uniprot.org/uniprotkb/search?{params}"
    js = await _get_json(http, url, timeout=timeout)
    try:
        results = (js or {}).get("results") or []
        if results:
            acc = results[0].get("primaryAccession") or results[0].get("uniProtkbId")
            if acc:
                return ResolvedGene(query=symbol, symbol=symbol, uniprot_id=str(acc))
    except Exception:
        pass
    return None

async def resolve_gene(value: str, http: Optional["httpx.AsyncClient"] = None, timeout: float = DEFAULT_TIMEOUT) -> ResolvedGene:
    """Resolve a gene symbol/ID to best-known identifiers.

    - If input is already an Ensembl ID: return it normalized immediately.
    - Otherwise attempt Ensembl, then UniProt.
    - On failure, return a minimal object with the input symbol preserved and a warning.
    """
    v = _ensure_nonempty(value, "gene")
    v_norm = normalize_gene_symbol(v)

    # Ensembl ID passthrough
    if is_ensembl_gene_id(v_norm):
        ensg = v_norm.split(".")[0].upper()
        return ResolvedGene(query=v, symbol=None, ensembl_id=ensg)

    cached = _gene_cache.get(v_norm)
    if cached is not None:
        return cached  # type: ignore

    owns_client = False
    client: Optional["httpx.AsyncClient"] = http
    if client is None:
        client = await _ensure_http(None, timeout=timeout)
        owns_client = True

    try:
        rg = await _resolve_gene_via_ensembl(v_norm, client, timeout=timeout)
        if not rg:
            rg = await _resolve_gene_via_uniprot(v_norm, client, timeout=timeout)
        if not rg:
            rg = ResolvedGene(query=v, symbol=v_norm, warning="unresolved_symbol")
        _gene_cache.set(v_norm, rg)
        return rg
    finally:
        if owns_client and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

# ----------------------------------------------------------------------------
# Condition resolution (EFO via OLS4 -> OLS3 fallback)
# ----------------------------------------------------------------------------

def _extract_obo_id(doc: Dict[str, Any]) -> Optional[str]:
    # OLS docs may include 'obo_id' or derive it from 'iri'
    oid = doc.get("obo_id") or doc.get("oboId") or ""
    if oid:
        return normalize_efo_id(oid)
    iri = doc.get("iri") or doc.get("iri_lowercase") or ""
    if iri and "/EFO_" in iri.upper():
        frag = iri.rsplit("/", 1)[-1]
        return normalize_efo_id(frag)
    return None

async def _resolve_efo_via_ols4(label: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedCondition]:
    if httpx is None:  # pragma: no cover
        return None
    params = httpx.QueryParams({"q": label, "ontology": "efo", "rows": 1})
    url = f"https://www.ebi.ac.uk/ols4/api/search?{params}"
    js = await _get_json(http, url, timeout=timeout)
    if not js:
        return None
    try:
        docs = (js.get("response") or {}).get("docs") or []
        if not docs:
            return None
        doc = docs[0]
        oid = _extract_obo_id(doc)
        lab = doc.get("label") or label
        iri = doc.get("iri")
        if oid:
            return ResolvedCondition(query=label, efo_id=oid, label=lab, iri=iri)
    except Exception:
        return None
    return None

async def _resolve_efo_via_ols3(label: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedCondition]:
    if httpx is None:  # pragma: no cover
        return None
    params = httpx.QueryParams({"q": label, "ontology": "efo", "rows": 1})
    url = f"https://www.ebi.ac.uk/ols/api/search?{params}"
    js = await _get_json(http, url, timeout=timeout)
    if not js:
        return None
    try:
        docs = (js.get("response") or {}).get("docs") or []
        if not docs:
            return None
        doc = docs[0]
        oid = _extract_obo_id(doc)
        lab = doc.get("label") or label
        iri = doc.get("iri")
        if oid:
            return ResolvedCondition(query=label, efo_id=oid, label=lab, iri=iri)
    except Exception:
        return None
    return None

async def resolve_condition(value: str, http: Optional["httpx.AsyncClient"] = None, timeout: float = DEFAULT_TIMEOUT) -> ResolvedCondition:
    """Resolve a free-text condition or EFO-style string to a canonical EFO ID.

    - Accepts labels (e.g., "obesity") or IDs ("EFO_0001073" / "EFO:0001073").
    - Tries OLS4 first, falls back to OLS v3.
    - On failure, returns label-only result with a warning.
    """
    v = _ensure_nonempty(value, "condition")
    v_norm = v.strip()

    # Already an EFO/HP/MONDO-like ID?
    if _EFO_RE.match(v_norm):
        return ResolvedCondition(query=v, efo_id=normalize_efo_id(v_norm), label=None)

    cached = _efo_cache.get(v_norm.lower())
    if cached is not None:
        return cached  # type: ignore

    owns_client = False
    client: Optional["httpx.AsyncClient"] = http
    if client is None:
        client = await _ensure_http(None, timeout=timeout)
        owns_client = True

    try:
        rc = await _resolve_efo_via_ols4(v_norm, client, timeout=timeout)
        if not rc:
            rc = await _resolve_efo_via_ols3(v_norm, client, timeout=timeout)
        if not rc:
            rc = ResolvedCondition(query=v, efo_id=None, label=v_norm, warning="unresolved_condition")
        _efo_cache.set(v_norm.lower(), rc)
        return rc
    finally:
        if owns_client and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

# ----------------------------------------------------------------------------
# Misc helpers
# ----------------------------------------------------------------------------

def validate_limit(value: Optional[int], lo: int = 1, hi: int = 200, field: str = "limit") -> int:
    if value is None:
        return min(50, hi)  # sensible default
    try:
        v = int(value)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid {field} '{value}' (must be integer)")
    if v < lo or v > hi:
        raise HTTPException(status_code=422, detail=f"{field} must be between {lo} and {hi}")
    return v

def http_error(status: int, message: str, request_id: Optional[str] = None) -> HTTPException:
    payload: Dict[str, Any] = {"detail": message}
    if request_id:
        payload["request_id"] = request_id
    return HTTPException(status_code=status, detail=payload)

__all__ = [
    # Validators
    "validate_symbol",
    "validate_condition",
    # Normalizers
    "normalize_gene_symbol",
    "normalize_efo_id",
    "is_ensembl_gene_id",
    # Resolvers
    "resolve_gene",
    "resolve_condition",
    # Entities
    "ResolvedGene",
    "ResolvedCondition",
    # HTTP helpers
    "get_http",
    "http_error",
    # Misc
    "validate_limit",
]
