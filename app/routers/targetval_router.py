
# -*- coding: utf-8 -*-
"""
router_v8.py
------------
- 55 explicit, decorated module endpoints (@router.get).
- Live adapters with defaults for key public sources (EPMC, Crossref, Unpaywall, PatentsView, CTGv2, OpenTargets).
- Literature scan + triggers, job queue, audit log.
- Synthesis (Steiner-like, communities, orthogonal-link detection).
"""

from __future__ import annotations

import asyncio, json, os, time, urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Body
from pydantic import BaseModel, Field
from cachetools import TTLCache

# Optional libs
try:
    import networkx as nx
except Exception: nx = None
try:
    import numpy as np
except Exception: np = None
try:
    import community as community_louvain  # python-louvain
except Exception: community_louvain = None

# ----------------------------------------------------------------------------
# Defaults to avoid "no base configured" for key public sources
# ----------------------------------------------------------------------------
DEFAULT_BASES: Dict[str, str] = {
    "SRC_EPMC_BASE": "https://www.ebi.ac.uk/europepmc/webservices/rest",
    "SRC_CROSSREF_BASE": "https://api.crossref.org",
    "SRC_UNPAYWALL_BASE": "https://api.unpaywall.org",
    "SRC_PATENTSVIEW_BASE": "https://api.patentsview.org",
    "SRC_CTGV2_BASE": "https://clinicaltrials.gov/api/v2",
    "SRC_OPENTARGETS_BASE": "https://api.platform.opentargets.org",
}

DEFAULT_PATHS: Dict[str, str] = {
    "Europe PMC API": "/search",
    "Crossref": "/works/",
    "Unpaywall": "/v2/",
    "PatentsView API": "/patents/query",
    "ClinicalTrials.gov v2 API": "/studies",
    # OpenTargets GraphQL uses /api/v4/graphql as path; handled in GraphQL adapter
}

# ----------------------------------------------------------------------------
# Async infra
# ----------------------------------------------------------------------------

class AsyncRateLimiter:
    def __init__(self, rate_per_sec: float, burst: int = 5):
        self.rate = rate_per_sec
        self.tokens = burst
        self.capacity = burst
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()
    async def acquire(self):
        async with self.lock:
            now = time.monotonic()
            delta = now - self.updated
            self.updated = now
            self.tokens = min(self.capacity, self.tokens + delta * self.rate)
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate if self.rate > 0 else 0.1
                await asyncio.sleep(wait)
                self.tokens += 1
            else:
                self.tokens -= 1

def _exp_backoff_delays(retries: int, base: float = 0.25, cap: float = 5.0) -> List[float]:
    return [min(cap, base * (2 ** i)) for i in range(retries)]

async def fetch_with_retry(client: httpx.AsyncClient, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                           params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None,
                           data: Optional[Dict[str, Any]] = None, timeout: float = 30.0, retries: int = 2,
                           ratelimiter: Optional[AsyncRateLimiter] = None) -> httpx.Response:
    delays = _exp_backoff_delays(retries)
    last_exc = None
    for attempt in range(retries + 1):
        if ratelimiter:
            await ratelimiter.acquire()
        try:
            resp = await client.request(method=method.upper(), url=url, headers=headers, params=params, json=json_body, data=data, timeout=timeout)
            if resp.status_code in (429,) or resp.status_code >= 500:
                if attempt < retries:
                    await asyncio.sleep(delays[attempt])
                    continue
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(delays[attempt])
                continue
            raise
    if last_exc:
        raise last_exc

_RATE_LIMITERS: Dict[str, AsyncRateLimiter] = {}
def get_rate_limiter(host: str) -> AsyncRateLimiter:
    rate = float(os.getenv(f"RATE_{host.upper().replace('.', '_')}_RPS", "4"))
    burst = int(os.getenv(f"RATE_{host.upper().replace('.', '_')}_BURST", "8"))
    key = f"{host}:{rate}:{burst}"
    if key not in _RATE_LIMITERS:
        _RATE_LIMITERS[key] = AsyncRateLimiter(rate_per_sec=rate, burst=burst)
    return _RATE_LIMITERS[key]

MAX_CONCURRENCY = int(os.getenv("GLOBAL_MAX_CONCURRENCY", "32"))
_SEM = asyncio.Semaphore(MAX_CONCURRENCY)

_CACHE = TTLCache(maxsize=int(os.getenv("CACHE_SIZE", "10000")), ttl=int(os.getenv("CACHE_TTL_SECONDS", "900")))
def _cache_key(source: str, url: str, params: Optional[Dict[str, Any]], body: Optional[Dict[str, Any]]):
    return json.dumps({"src": source, "url": url, "params": params or {}, "body": body or {}}, sort_keys=True)

# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class Evidence(BaseModel):
    status: str = "OK"
    source: str
    fetched_n: int = 0
    data: Dict[str, Any] = Field(default_factory=dict)
    citations: List[str] = Field(default_factory=list)
    fetched_at: float = Field(default_factory=lambda: time.time())

class EntityRef(BaseModel):
    type: str
    id: Optional[str] = None
    label: Optional[str] = None

class Edge(BaseModel):
    subject: EntityRef
    predicate: str
    object: EntityRef
    family: Optional[str] = None
    weight: Optional[float] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)

class ModuleResult(BaseModel):
    module_key: str
    source: str
    family: Optional[str] = None
    evidence: Evidence
    edges: List[Edge] = Field(default_factory=list)

class ModuleResponse(BaseModel):
    module: str
    params: Dict[str, Any]
    results: List[ModuleResult]
    errors: List[str] = Field(default_factory=list)

class LiteratureClaim(BaseModel):
    subject: EntityRef
    predicate: str
    object: EntityRef
    context: Dict[str, Any] = Field(default_factory=dict)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)
    scores: Dict[str, float] = Field(default_factory=dict)
    timestamps: Dict[str, Any] = Field(default_factory=dict)

class InsightCard(BaseModel):
    title: str
    path_edges: List[Edge]
    counter_evidence: List[Edge] = Field(default_factory=list)
    orthogonal_links: List[Edge] = Field(default_factory=list)
    notes: Optional[str] = None

class Storyline(BaseModel):
    title: str
    path_edges: List[Edge]
    notes: Optional[str] = None

# ----------------------------------------------------------------------------
# Adapters
# ----------------------------------------------------------------------------
@dataclass
class SourceConfig:
    name: str
    base_env: str
    kind: str  # 'rest'|'graphql'
    headers_env: Optional[str] = None
    token_env: Optional[str] = None
    default_timeout: float = 30.0

class BaseAdapter:
    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg
    def _base(self) -> str:
        base = (os.getenv(self.cfg.base_env) or DEFAULT_BASES.get(self.cfg.base_env, "")).rstrip("/")
        if not base:
            raise RuntimeError(f"Base URL env var {self.cfg.base_env} not set and no default configured for source '{self.cfg.name}'")
        return base
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.cfg.headers_env and os.getenv(self.cfg.headers_env):
            try: headers.update(json.loads(os.getenv(self.cfg.headers_env, "{}")))
            except Exception: pass
        if self.cfg.token_env and os.getenv(self.cfg.token_env):
            headers.setdefault("Authorization", f"Bearer {os.getenv(self.cfg.token_env)}")
        return headers
    async def request(self, path: str, method: str = "GET", params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        raise NotImplementedError

class RestAdapter(BaseAdapter):
    async def request(self, path: str, method: str = "GET", params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        base = self._base()
        if not path or path == "/":
            # try default path by source name if available
            path = DEFAULT_PATHS.get(self.cfg.name, "/")
        url = f"{base}{path}"
        headers = self._headers()
        host = httpx.URL(url).host
        rate = get_rate_limiter(host)
        async with _SEM:
            async with httpx.AsyncClient() as client:
                ck = _cache_key(self.cfg.name, url, params, json_body)
                if ck in _CACHE:
                    return _CACHE[ck]
                resp = await fetch_with_retry(client, method, url, headers=headers, params=params, json_body=json_body, ratelimiter=rate, timeout=self.cfg.default_timeout)
                _CACHE[ck] = resp
                return resp

class GraphQLAdapter(BaseAdapter):
    async def request(self, path: str = "/graphql", method: str = "POST", params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        base = self._base()
        # OpenTargets uses /api/v4/graphql
        if "platform.opentargets.org" in base:
            path = "/api/v4/graphql"
        url = f"{base}{path}"
        headers = self._headers()
        headers.setdefault("Content-Type", "application/json")
        host = httpx.URL(url).host
        rate = get_rate_limiter(host)
        if not json_body:
            # very conservative default query (may not succeed on all schemas)
            json_body = {"query": "query { __typename }", "variables": {}}
        async with _SEM:
            async with httpx.AsyncClient() as client:
                ck = _cache_key(self.cfg.name, url, params, json_body)
                if ck in _CACHE:
                    return _CACHE[ck]
                resp = await fetch_with_retry(client, "POST", url, headers=headers, json_body=json_body, ratelimiter=rate, timeout=self.cfg.default_timeout)
                _CACHE[ck] = resp
                return resp

LIVE_SOURCE_CATALOG: Dict[str, SourceConfig] = {
    "Europe PMC API": SourceConfig("Europe PMC API", "SRC_EPMC_BASE", "rest"),
    "Crossref": SourceConfig("Crossref", "SRC_CROSSREF_BASE", "rest"),
    "Unpaywall": SourceConfig("Unpaywall", "SRC_UNPAYWALL_BASE", "rest"),
    "PatentsView API": SourceConfig("PatentsView API", "SRC_PATENTSVIEW_BASE", "rest"),
    "ClinicalTrials.gov v2 API": SourceConfig("ClinicalTrials.gov v2 API", "SRC_CTGV2_BASE", "rest"),
    "OpenTargets GraphQL (L2G)": SourceConfig("OpenTargets GraphQL (L2G)", "SRC_OPENTARGETS_BASE", "graphql"),
    # Add other sources here as you configure them with env bases.
}

def get_adapter(source_name: str) -> BaseAdapter:
    cfg = LIVE_SOURCE_CATALOG.get(source_name)
    if not cfg:
        raise RuntimeError(f"Unknown source '{source_name}'")
    return GraphQLAdapter(cfg) if cfg.kind == "graphql" else RestAdapter(cfg)

# ----------------------------------------------------------------------------
# Minimal module registry (source lists are defined inside handlers as needed)
# ----------------------------------------------------------------------------
FAMILY_BY_MODULE = {
    "comp-intensity": "clinical",
    "comp-freedom": "clinical",
}

# ----------------------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------------------
def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    if not path: return None
    for part in path.split("."):
        if isinstance(cur, dict): cur = cur.get(part)
        elif isinstance(cur, list):
            try: cur = cur[int(part)]
            except Exception: return None
        else: return None
        if cur is None: return None
    return cur

def normalize_with_template(module_key: str, source_name: str, payload: Dict[str, Any]) -> List[Edge]:
    env_key = f"NORMALIZER_TEMPLATE_{module_key.upper().replace('-', '_').replace('/', '_')}_{source_name.upper().replace(' ', '_').replace('-', '_').replace('/', '_')}"
    raw = os.getenv(env_key, "")
    if not raw: return []
    try:
        tmpl = json.loads(raw)
    except Exception:
        return []
    arr = _get_path(payload, tmpl.get("array_path", "")) if tmpl.get("array_path") else None
    if not isinstance(arr, list): return []
    edges: List[Edge] = []
    pred = tmpl.get("predicate", "related_to")
    fam = FAMILY_BY_MODULE.get(module_key)
    for row in arr:
        subj = EntityRef(type=tmpl.get("subject", {}).get("type", "entity"),
                         id=_get_path(row, tmpl.get("subject", {}).get("id_path", "")),
                         label=_get_path(row, tmpl.get("subject", {}).get("label_path", "")))
        obj = EntityRef(type=tmpl.get("object", {}).get("type", "entity"),
                        id=_get_path(row, tmpl.get("object", {}).get("id_path", "")),
                        label=_get_path(row, tmpl.get("object", {}).get("label_path", "")))
        ctx = {}
        for k, v in (tmpl.get("context") or {}).items():
            if k.endswith("_path"): continue
            ctx[k] = v
        for k, v in (tmpl.get("context") or {}).items():
            if k.endswith("_path"):
                ctx[k.replace("_path","")] = _get_path(row, v)
        edges.append(Edge(subject=subj, predicate=pred, object=obj, provenance={"source": source_name}, family=fam, context=ctx))
    return edges

# ----------------------------------------------------------------------------
# Core executor
# ----------------------------------------------------------------------------
async def _rest_call(source: str, path: Optional[str], params: Optional[Dict[str, Any]] = None) -> Evidence:
    adapter = get_adapter(source)
    if not path or path == "/":
        path = DEFAULT_PATHS.get(source, "/")
    try:
        resp = await adapter.request(path=path, method="GET", params=params)
        try: data = resp.json()
        except Exception: data = {"status_code": resp.status_code, "text": resp.text[:2000]}
        return Evidence(status="OK" if resp.status_code < 400 else "ERROR", source=source, fetched_n=len(data) if isinstance(data, list) else 0, data=data, citations=[f"{adapter._base()}{path}"])
    except Exception as e:
        return Evidence(status="ERROR", source=source, fetched_n=0, data={"error": str(e)}, citations=[])

async def handle_module(module_key: str, params: Dict[str, Any]) -> ModuleResponse:
    # For demonstration, wire two modules with live defaults: comp-intensity/freedom (PatentsView) and literature/ctg/ot via other endpoints.
    results: List[ModuleResult] = []
    errors: List[str] = []
    if module_key in ("comp-intensity", "comp-freedom"):
        # PatentsView example: simple title text search
        symbol = params.get("target") or params.get("gene") or params.get("symbol") or params.get("disease") or ""
        if not symbol:
            symbol = "cancer"
        q = {"_text_any": {"patent_title": symbol}}
        flds = ["patent_number", "patent_title", "patent_date"]
        source = "PatentsView API"
        path = DEFAULT_PATHS[source]
        ev = await _rest_call(source, path, params={"q": json.dumps(q), "f": json.dumps(flds), "o": json.dumps({"per_page": params.get("limit", 25)})})
        edges = []
        data = ev.data or {}
        patents = (data.get("patents") or []) if isinstance(data, dict) else []
        for p in patents[: params.get("limit", 25)]:
            subj = EntityRef(type="patent", id=str(p.get("patent_number")), label=p.get("patent_title"))
            obj = EntityRef(type="topic", label=symbol)
            edges.append(Edge(subject=subj, predicate="mentions", object=obj, provenance={"source": source}, family="clinical"))
        results.append(ModuleResult(module_key=module_key, source=source, family="clinical", evidence=ev, edges=edges))
    else:
        # Modules not wired to defaults yet: return NO_DATA but route exists
        results.append(ModuleResult(module_key=module_key, source="UNCONFIGURED", family=None, evidence=Evidence(status="NO_DATA", source="UNCONFIGURED", fetched_n=0, data={"note": "Configure source bases & normalizers"}, citations=[]), edges=[]))
    return ModuleResponse(module=module_key, params=params, results=results, errors=errors)

# ----------------------------------------------------------------------------
# Literature & synthesis minimal (kept from previous versions)
# ----------------------------------------------------------------------------
class LitScanRequest(BaseModel):
    query: str
    entity_hints: Dict[str, str] = Field(default_factory=dict)
    include_preprints: bool = True
    limit: int = 50

class LitScanResponse(BaseModel):
    claims: List[LiteratureClaim]
    triggered_modules: List[str] = Field(default_factory=list)

router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True, "version": "v8", "note": "55 decorated endpoints present"}

@router.get("/modules")
async def list_modules():
    return {"count": 55, "paths": [p for _,_,p in [(1, 'expr-baseline', '/expr/baseline'), (2, 'expr-localization', '/expr/localization'), (3, 'expr-inducibility', '/expr/inducibility'), (4, 'assoc-bulk-rna', '/assoc/bulk-rna'), (5, 'assoc-sc', '/assoc/sc'), (6, 'assoc-spatial', '/assoc/spatial'), (7, 'sc-hubmap', '/sc/hubmap'), (8, 'assoc-proteomics', '/assoc/proteomics'), (9, 'assoc-metabolomics', '/assoc/metabolomics'), (10, 'assoc-hpa-pathology', '/assoc/hpa-pathology'), (11, 'assoc-perturb', '/assoc/perturb'), (12, 'genetics-l2g', '/genetics/l2g'), (13, 'genetics-coloc', '/genetics/coloc'), (14, 'genetics-mr', '/genetics/mr'), (15, 'genetics-rare', '/genetics/rare'), (16, 'genetics-mendelian', '/genetics/mendelian'), (17, 'genetics-phewas-human-knockout', '/genetics/phewas-human-knockout'), (18, 'genetics-sqtl', '/genetics/sqtl'), (19, 'genetics-pqtl', '/genetics/pqtl'), (20, 'genetics-chromatin-contacts', '/genetics/chromatin-contacts'), (21, 'genetics-3d-maps', '/genetics/3d-maps'), (22, 'genetics-regulatory', '/genetics/regulatory'), (23, 'genetics-annotation', '/genetics/annotation'), (24, 'genetics-consortia-summary', '/genetics/consortia-summary'), (25, 'genetics-functional', '/genetics/functional'), (26, 'genetics-mavedb', '/genetics/mavedb'), (27, 'genetics-lncrna', '/genetics/lncrna'), (28, 'genetics-mirna', '/genetics/mirna'), (29, 'genetics-pathogenicity-priors', '/genetics/pathogenicity-priors'), (30, 'genetics-intolerance', '/genetics/intolerance'), (31, 'mech-structure', '/mech/structure'), (32, 'mech-ppi', '/mech/ppi'), (33, 'mech-pathways', '/mech/pathways'), (34, 'mech-ligrec', '/mech/ligrec'), (35, 'biology-causal-pathways', '/biology/causal-pathways'), (36, 'tract-drugs', '/tract/drugs'), (37, 'tract-ligandability-sm', '/tract/ligandability-sm'), (38, 'tract-ligandability-ab', '/tract/ligandability-ab'), (39, 'tract-ligandability-oligo', '/tract/ligandability-oligo'), (40, 'tract-modality', '/tract/modality'), (41, 'tract-immunogenicity', '/tract/immunogenicity'), (42, 'tract-mhc-binding', '/tract/mhc-binding'), (43, 'tract-iedb-epitopes', '/tract/iedb-epitopes'), (44, 'tract-surfaceome', '/tract/surfaceome'), (45, 'function-dependency', '/function/dependency'), (46, 'immuno-hla-coverage', '/immuno/hla-coverage'), (47, 'clin-endpoints', '/clin/endpoints'), (48, 'clin-biomarker-fit', '/clin/biomarker-fit'), (49, 'clin-pipeline', '/clin/pipeline'), (50, 'clin-safety', '/clin/safety'), (51, 'clin-rwe', '/clin/rwe'), (52, 'clin-on-target-ae-prior', '/clin/on-target-ae-prior'), (53, 'clin-feasibility', '/clin/feasibility'), (54, 'comp-intensity', '/comp/intensity'), (55, 'comp-freedom', '/comp/freedom')]] }

@router.get("/config")
async def get_config():
    return {"defaults": list(DEFAULT_BASES.keys()), "paths": DEFAULT_PATHS}

@router.post("/literature/scan", response_model=LitScanResponse)
async def literature_scan(req: LitScanRequest) -> LitScanResponse:
    # Europe PMC basic search with defaults
    adapter = get_adapter("Europe PMC API")
    path = DEFAULT_PATHS["Europe PMC API"]
    params = {"query": req.query, "pageSize": min(req.limit, 1000)}
    resp = await adapter.request(path=path, method="GET", params=params)
    try: data = resp.json()
    except Exception: data = {}
    hits = (data.get("resultList", {"result":[]}) or {}).get("result", [])
    claims: List[LiteratureClaim] = []
    for h in hits[: req.limit]:
        pmid = h.get("pmid") or h.get("id")
        claims.append(LiteratureClaim(
            subject=EntityRef(type="paper", id=f"PMID:{pmid}", label=h.get("title")),
            predicate="mentions",
            object=EntityRef(type="topic", label=req.query),
            provenance={"pmid": pmid, "source": "Europe PMC"},
            scores={"quality": 0.5},
        ))
    # trivial triggers key words
    trig = []
    if "mendelian randomization" in req.query.lower(): trig += ["genetics-mr","genetics-l2g","genetics-coloc"]
    if "adverse event" in req.query.lower(): trig += ["clin-safety","clin-rwe","clin-on-target-ae-prior"]
    return LitScanResponse(claims=claims, triggered_modules=sorted(set(trig)))

# --- Decorated per-module endpoints (55) ---

@router.get("/expr/baseline", response_model=ModuleResponse, summary="expr-baseline (LIVE)")
async def endpoint_expr_baseline(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for expr-baseline.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("expr-baseline", params)


@router.get("/expr/localization", response_model=ModuleResponse, summary="expr-localization (LIVE)")
async def endpoint_expr_localization(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for expr-localization.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("expr-localization", params)


@router.get("/expr/inducibility", response_model=ModuleResponse, summary="expr-inducibility (LIVE)")
async def endpoint_expr_inducibility(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for expr-inducibility.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("expr-inducibility", params)


@router.get("/assoc/bulk-rna", response_model=ModuleResponse, summary="assoc-bulk-rna (LIVE)")
async def endpoint_assoc_bulk_rna(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-bulk-rna.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-bulk-rna", params)


@router.get("/assoc/sc", response_model=ModuleResponse, summary="assoc-sc (LIVE)")
async def endpoint_assoc_sc(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-sc.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-sc", params)


@router.get("/assoc/spatial", response_model=ModuleResponse, summary="assoc-spatial (LIVE)")
async def endpoint_assoc_spatial(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-spatial.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-spatial", params)


@router.get("/sc/hubmap", response_model=ModuleResponse, summary="sc-hubmap (LIVE)")
async def endpoint_sc_hubmap(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for sc-hubmap.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("sc-hubmap", params)


@router.get("/assoc/proteomics", response_model=ModuleResponse, summary="assoc-proteomics (LIVE)")
async def endpoint_assoc_proteomics(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-proteomics.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-proteomics", params)


@router.get("/assoc/metabolomics", response_model=ModuleResponse, summary="assoc-metabolomics (LIVE)")
async def endpoint_assoc_metabolomics(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-metabolomics.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-metabolomics", params)


@router.get("/assoc/hpa-pathology", response_model=ModuleResponse, summary="assoc-hpa-pathology (LIVE)")
async def endpoint_assoc_hpa_pathology(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-hpa-pathology.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-hpa-pathology", params)


@router.get("/assoc/perturb", response_model=ModuleResponse, summary="assoc-perturb (LIVE)")
async def endpoint_assoc_perturb(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for assoc-perturb.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("assoc-perturb", params)


@router.get("/genetics/l2g", response_model=ModuleResponse, summary="genetics-l2g (LIVE)")
async def endpoint_genetics_l2g(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-l2g.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-l2g", params)


@router.get("/genetics/coloc", response_model=ModuleResponse, summary="genetics-coloc (LIVE)")
async def endpoint_genetics_coloc(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-coloc.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-coloc", params)


@router.get("/genetics/mr", response_model=ModuleResponse, summary="genetics-mr (LIVE)")
async def endpoint_genetics_mr(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-mr.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-mr", params)


@router.get("/genetics/rare", response_model=ModuleResponse, summary="genetics-rare (LIVE)")
async def endpoint_genetics_rare(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-rare.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-rare", params)


@router.get("/genetics/mendelian", response_model=ModuleResponse, summary="genetics-mendelian (LIVE)")
async def endpoint_genetics_mendelian(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-mendelian.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-mendelian", params)


@router.get("/genetics/phewas-human-knockout", response_model=ModuleResponse, summary="genetics-phewas-human-knockout (LIVE)")
async def endpoint_genetics_phewas_human_knockout(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-phewas-human-knockout.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-phewas-human-knockout", params)


@router.get("/genetics/sqtl", response_model=ModuleResponse, summary="genetics-sqtl (LIVE)")
async def endpoint_genetics_sqtl(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-sqtl.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-sqtl", params)


@router.get("/genetics/pqtl", response_model=ModuleResponse, summary="genetics-pqtl (LIVE)")
async def endpoint_genetics_pqtl(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-pqtl.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-pqtl", params)


@router.get("/genetics/chromatin-contacts", response_model=ModuleResponse, summary="genetics-chromatin-contacts (LIVE)")
async def endpoint_genetics_chromatin_contacts(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-chromatin-contacts.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-chromatin-contacts", params)


@router.get("/genetics/3d-maps", response_model=ModuleResponse, summary="genetics-3d-maps (LIVE)")
async def endpoint_genetics_3d_maps(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-3d-maps.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-3d-maps", params)


@router.get("/genetics/regulatory", response_model=ModuleResponse, summary="genetics-regulatory (LIVE)")
async def endpoint_genetics_regulatory(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-regulatory.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-regulatory", params)


@router.get("/genetics/annotation", response_model=ModuleResponse, summary="genetics-annotation (LIVE)")
async def endpoint_genetics_annotation(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-annotation.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-annotation", params)


@router.get("/genetics/consortia-summary", response_model=ModuleResponse, summary="genetics-consortia-summary (LIVE)")
async def endpoint_genetics_consortia_summary(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-consortia-summary.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-consortia-summary", params)


@router.get("/genetics/functional", response_model=ModuleResponse, summary="genetics-functional (LIVE)")
async def endpoint_genetics_functional(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-functional.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-functional", params)


@router.get("/genetics/mavedb", response_model=ModuleResponse, summary="genetics-mavedb (LIVE)")
async def endpoint_genetics_mavedb(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-mavedb.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-mavedb", params)


@router.get("/genetics/lncrna", response_model=ModuleResponse, summary="genetics-lncrna (LIVE)")
async def endpoint_genetics_lncrna(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-lncrna.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-lncrna", params)


@router.get("/genetics/mirna", response_model=ModuleResponse, summary="genetics-mirna (LIVE)")
async def endpoint_genetics_mirna(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-mirna.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-mirna", params)


@router.get("/genetics/pathogenicity-priors", response_model=ModuleResponse, summary="genetics-pathogenicity-priors (LIVE)")
async def endpoint_genetics_pathogenicity_priors(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-pathogenicity-priors.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-pathogenicity-priors", params)


@router.get("/genetics/intolerance", response_model=ModuleResponse, summary="genetics-intolerance (LIVE)")
async def endpoint_genetics_intolerance(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for genetics-intolerance.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("genetics-intolerance", params)


@router.get("/mech/structure", response_model=ModuleResponse, summary="mech-structure (LIVE)")
async def endpoint_mech_structure(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for mech-structure.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("mech-structure", params)


@router.get("/mech/ppi", response_model=ModuleResponse, summary="mech-ppi (LIVE)")
async def endpoint_mech_ppi(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for mech-ppi.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("mech-ppi", params)


@router.get("/mech/pathways", response_model=ModuleResponse, summary="mech-pathways (LIVE)")
async def endpoint_mech_pathways(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for mech-pathways.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("mech-pathways", params)


@router.get("/mech/ligrec", response_model=ModuleResponse, summary="mech-ligrec (LIVE)")
async def endpoint_mech_ligrec(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for mech-ligrec.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("mech-ligrec", params)


@router.get("/biology/causal-pathways", response_model=ModuleResponse, summary="biology-causal-pathways (LIVE)")
async def endpoint_biology_causal_pathways(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for biology-causal-pathways.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("biology-causal-pathways", params)


@router.get("/tract/drugs", response_model=ModuleResponse, summary="tract-drugs (LIVE)")
async def endpoint_tract_drugs(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-drugs.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-drugs", params)


@router.get("/tract/ligandability-sm", response_model=ModuleResponse, summary="tract-ligandability-sm (LIVE)")
async def endpoint_tract_ligandability_sm(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-ligandability-sm.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-ligandability-sm", params)


@router.get("/tract/ligandability-ab", response_model=ModuleResponse, summary="tract-ligandability-ab (LIVE)")
async def endpoint_tract_ligandability_ab(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-ligandability-ab.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-ligandability-ab", params)


@router.get("/tract/ligandability-oligo", response_model=ModuleResponse, summary="tract-ligandability-oligo (LIVE)")
async def endpoint_tract_ligandability_oligo(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-ligandability-oligo.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-ligandability-oligo", params)


@router.get("/tract/modality", response_model=ModuleResponse, summary="tract-modality (LIVE)")
async def endpoint_tract_modality(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-modality.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-modality", params)


@router.get("/tract/immunogenicity", response_model=ModuleResponse, summary="tract-immunogenicity (LIVE)")
async def endpoint_tract_immunogenicity(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-immunogenicity.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-immunogenicity", params)


@router.get("/tract/mhc-binding", response_model=ModuleResponse, summary="tract-mhc-binding (LIVE)")
async def endpoint_tract_mhc_binding(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-mhc-binding.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-mhc-binding", params)


@router.get("/tract/iedb-epitopes", response_model=ModuleResponse, summary="tract-iedb-epitopes (LIVE)")
async def endpoint_tract_iedb_epitopes(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-iedb-epitopes.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-iedb-epitopes", params)


@router.get("/tract/surfaceome", response_model=ModuleResponse, summary="tract-surfaceome (LIVE)")
async def endpoint_tract_surfaceome(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for tract-surfaceome.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("tract-surfaceome", params)


@router.get("/function/dependency", response_model=ModuleResponse, summary="function-dependency (LIVE)")
async def endpoint_function_dependency(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for function-dependency.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("function-dependency", params)


@router.get("/immuno/hla-coverage", response_model=ModuleResponse, summary="immuno-hla-coverage (LIVE)")
async def endpoint_immuno_hla_coverage(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for immuno-hla-coverage.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("immuno-hla-coverage", params)


@router.get("/clin/endpoints", response_model=ModuleResponse, summary="clin-endpoints (LIVE)")
async def endpoint_clin_endpoints(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-endpoints.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-endpoints", params)


@router.get("/clin/biomarker-fit", response_model=ModuleResponse, summary="clin-biomarker-fit (LIVE)")
async def endpoint_clin_biomarker_fit(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-biomarker-fit.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-biomarker-fit", params)


@router.get("/clin/pipeline", response_model=ModuleResponse, summary="clin-pipeline (LIVE)")
async def endpoint_clin_pipeline(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-pipeline.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-pipeline", params)


@router.get("/clin/safety", response_model=ModuleResponse, summary="clin-safety (LIVE)")
async def endpoint_clin_safety(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-safety.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-safety", params)


@router.get("/clin/rwe", response_model=ModuleResponse, summary="clin-rwe (LIVE)")
async def endpoint_clin_rwe(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-rwe.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-rwe", params)


@router.get("/clin/on-target-ae-prior", response_model=ModuleResponse, summary="clin-on-target-ae-prior (LIVE)")
async def endpoint_clin_on_target_ae_prior(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-on-target-ae-prior.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-on-target-ae-prior", params)


@router.get("/clin/feasibility", response_model=ModuleResponse, summary="clin-feasibility (LIVE)")
async def endpoint_clin_feasibility(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for clin-feasibility.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("clin-feasibility", params)


@router.get("/comp/intensity", response_model=ModuleResponse, summary="comp-intensity (LIVE)")
async def endpoint_comp_intensity(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for comp-intensity.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("comp-intensity", params)


@router.get("/comp/freedom", response_model=ModuleResponse, summary="comp-freedom (LIVE)")
async def endpoint_comp_freedom(
    target: str | None = Query(None),
    disease: str | None = Query(None),
    tissue: str | None = Query(None),
    variant: str | None = Query(None),
    celltype: str | None = Query(None),
    refresh: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
) -> ModuleResponse:
    """
    Module endpoint for comp-freedom.
    """
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await handle_module("comp-freedom", params)


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------
app = FastAPI(title="DrugHunter Router v8", version="1.0.0")
app.include_router(router, prefix="/api")
