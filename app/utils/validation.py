
"""
TargetVal validation & resolution utilities
------------------------------------------

Safe to import in Render (no network at import time). Includes:

- Lightweight *validators* used by the router:
    validate_symbol(value, field_name="gene")
    validate_condition(value, field_name="efo")

  These perform syntax/presence checks only (no network). They should be
  called early in request handling to fail fast on obviously bad inputs.

- Async *resolvers* that can be used by endpoints which need canonical IDs:
    resolve_gene(value, http=None, timeout=15.0) -> ResolvedGene
    resolve_condition(value, http=None, timeout=15.0) -> ResolvedCondition

  Resolvers optionally take an httpx.AsyncClient. If None, they create a
  short-lived client for that call. They are robust against upstream hiccups
  and return best-effort mappings (symbol/EFO labels are preserved even if
  ID lookups fail).

- Utilities:
    get_http(request)                 -> return shared httpx client from app.state.http
    normalize_gene_symbol(value)      -> uppercase & strip
    is_ensembl_gene_id(value)         -> bool
    normalize_efo_id(value)           -> 'EFO_000XXXX' (HP_/MONDO_ supported, padded)
    validate_limit(limit, lo, hi)     -> clamps/raises
    http_error(status, msg, rid=None) -> FastAPI HTTPException factory

No heavy work at import time. Tiny in-memory TTL caches avoid thundering herd.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

try:
    import httpx  # optional; resolvers work without if you don't call them
except Exception:  # pragma: no cover - optional dep
    httpx = None  # type: ignore

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------

DEFAULT_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15.0"))
DEFAULT_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
DEFAULT_BACKOFF = float(os.getenv("HTTP_BACKOFF", "0.25"))  # seconds base

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
    Accepts symbols (GPR75), Ensembl (ENSG...), or UniProt accessions.
    Does *not* hit the network.
    """
    v = _ensure_nonempty(value, field_name)
    # Be permissive: allow most simple tokens; stricter checks if prefixed IDs
    if _ENSEMBL_GENE_RE.match(v) or _UNIPROT_RE.match(v) or _GENE_RE.match(v):
        return v
    raise HTTPException(status_code=422, detail=f"Invalid {field_name} '{v}'")


def validate_condition(value: Optional[str], field_name: str = "efo") -> str:
    """Accepts EFO/HP/MONDO IDs (EFO_0001073 / EFO:0001073) or free text labels.
    Does *not* hit the network. Resolution to IDs is handled by `resolve_condition`.
    """
    v = _ensure_nonempty(value, field_name)
    # Always allow free text; ID-like strings are normalized later
    return v

# ----------------------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------------------

def normalize_gene_symbol(symbol: str) -> str:
    return str(symbol).strip().upper()

def is_ensembl_gene_id(value: str) -> bool:
    return bool(_ENSEMBL_GENE_RE.match(str(value).strip()))

def normalize_efo_id(value: str) -> str:
    v = str(value).strip().upper().replace(":", "_")
    if _EFO_RE.match(v):
        # Zero-pad if needed (keep existing zeros if present)
        if "_" in v:
            prefix, num = v.split("_", 1)
        else:
            # e.g., EFO0001073 -> split prefix and digits
            prefix = v[:3]
            num = v[3:]
        num = re.sub(r"^0*", "", num)  # remove leading zeros to compute width
        # Pad to at least 7 digits (common across EFO/HP/MONDO IDs)
        padded = f"{int(num):07d}"
        return f"{prefix}_{padded}"
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
    species: Optional[str] = "Homo sapiens"
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
            # cheap eviction: drop 1/16 oldest-ish by timestamp
            try:
                for k, _ in list(sorted(self._data.items(), key=lambda x: x[1][0]))[: max(1, self.maxsize // 16)]:
                    self._data.pop(k, None)
            except Exception:
                self._data.clear()
        self._data[key] = (time.time() + self.ttl, value)

_gene_cache = _TTLCache(ttl_seconds=12 * 3600, maxsize=8192)
_efo_cache = _TTLCache(ttl_seconds=12 * 3600, maxsize=8192)

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
        headers={"User-Agent": "targetval-validation", "Accept": "application/json"},
        http2=True,
    )

async def _sleep_backoff(attempt: int, base: float) -> None:
    await asyncio.sleep(base * (attempt + 1))

async def _get_json(http: "httpx.AsyncClient", url: str, timeout: float, retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF) -> Optional[Any]:
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            r = await http.get(url, timeout=timeout)
            if r.status_code == 200:
                # be tolerant of wrong content-types
                try:
                    return r.json()
                except Exception:
                    return None
            # 429 handling
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra is not None else backoff * (attempt + 1)
                except Exception:
                    delay = backoff * (attempt + 1)
                await asyncio.sleep(delay)
                continue
            # brief backoff for 5xx
            if 500 <= r.status_code < 600:
                await _sleep_backoff(attempt, backoff)
                continue
            # non-retryable
            return None
        except Exception as e:
            last_exc = e
            await _sleep_backoff(attempt, backoff)
    # give up
    return None

# ----------------------------------------------------------------------------
# Gene resolution (Ensembl first, UniProt fallback)
# ----------------------------------------------------------------------------

async def _resolve_gene_via_ensembl(symbol: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedGene]:
    # Try Ensembl xrefs/symbol
    url = f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{symbol}?content-type=application/json"
    js = await _get_json(http, url, timeout=timeout)
    if not js:
        # Newer Ensembl endpoints (lookup/symbol)
        url2 = f"https://rest.ensembl.org/lookup/symbol/homo_sapiens/{symbol}?content-type=application/json"
        js2 = await _get_json(http, url2, timeout=timeout)
        if not js2:
            return None
        # Single object
        ensg = js2.get("id")
        if ensg:
            return ResolvedGene(query=symbol, symbol=symbol, ensembl_id=ensg)
        return None

    # xrefs returns a list of records; pick the first gene
    for rec in js:
        if str(rec.get("type", "")).lower() == "gene":
            ensg = rec.get("id") or rec.get("primary_id")
            if ensg:
                return ResolvedGene(query=symbol, symbol=symbol, ensembl_id=ensg)
    # fallback: pick any id
    if isinstance(js, list) and js:
        ensg = js[0].get("id") or js[0].get("primary_id")
        if ensg:
            return ResolvedGene(query=symbol, symbol=symbol, ensembl_id=ensg)
    return None

async def _resolve_gene_via_uniprot(symbol: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedGene]:
    # Search UniProt for primary gene
    query = f"gene_exact:{symbol} AND organism_id:9606"
    url = f"https://rest.uniprot.org/uniprotkb/search?query={httpx.QueryParams({'query': query, 'fields': 'accession,gene_primary', 'size': 1}) if httpx else 'query=' + query}&fields=accession,gene_primary&size=1"
    js = await _get_json(http, url, timeout=timeout)
    # UniProt REST returns {'results': [...]}
    try:
        results = (js or {}).get("results") or []
        if results:
            acc = results[0].get("primaryAccession") or results[0].get("uniProtkbId")
            return ResolvedGene(query=symbol, symbol=symbol, uniprot_id=acc)
    except Exception:
        pass
    return None

async def resolve_gene(value: str, http: Optional["httpx.AsyncClient"] = None, timeout: float = DEFAULT_TIMEOUT) -> ResolvedGene:
    """Resolve a gene symbol/ID to best-known identifiers.

    - If input is already an Ensembl ID: return it normalized.
    - Otherwise attempt Ensembl, then UniProt.
    - On failure, return a minimal object with the input symbol preserved.
    """
    v = _ensure_nonempty(value, "gene")
    v_norm = normalize_gene_symbol(v)

    # Ensembl ID passthrough
    if is_ensembl_gene_id(v_norm):
        ensg = v_norm.upper().split(".")[0]
        return ResolvedGene(query=v, symbol=None, ensembl_id=ensg)

    # Cache
    cached = _gene_cache.get(v_norm)
    if cached is not None:
        return cached

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
    if iri and "/EFO_" in iri:
        frag = iri.rsplit("/", 1)[-1]
        return normalize_efo_id(frag)
    return None

async def _resolve_efo_via_ols4(label: str, http: "httpx.AsyncClient", timeout: float) -> Optional[ResolvedCondition]:
    params = httpx.QueryParams({"q": label, "ontology": "efo", "rows": 1}) if httpx else f"q={label}&ontology=efo&rows=1"
    url = f"https://www.ebi.ac.uk/ols4/api/search?{params}"
    js = await _get_json(http, url, timeout=timeout)
    if not js:
        return None
    # OLS4 returns { "response": {"numFound":..., "docs":[...] } }
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
    params = httpx.QueryParams({"q": label, "ontology": "efo", "rows": 1}) if httpx else f"q={label}&ontology=efo&rows=1"
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
        return cached

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
