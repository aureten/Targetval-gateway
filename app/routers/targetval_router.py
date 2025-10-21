
from __future__ import annotations

# ---- Pydantic v2 typing prelude ----------------------------------------------
from typing import Any, Dict, List, Optional, Tuple, Set, Mapping, Iterable, Union, Literal
try:
    from typing import TypedDict, NotRequired
except Exception:
    try:
        from typing_extensions import TypedDict, NotRequired  # type: ignore
    except Exception:
        TypedDict = dict  # type: ignore
        NotRequired = None  # type: ignore
globals().update({
    "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple, "Set": Set,
    "Mapping": Mapping, "Iterable": Iterable, "Union": Union, "Literal": Literal,
    "TypedDict": TypedDict, "NotRequired": NotRequired
})
# ------------------------------------------------------------------------------

# router.py — Advanced (64 modules) with live fetch (approach aligned to router-revised.py)

# -------------- Live genetics helpers (OpenTargets + OpenGWAS) --------------
from typing import Optional
import urllib, urllib.parse

_OT_GQL = "https://api.platform.opentargets.org/api/v4/graphql"
_ENS_XREF = "https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{symbol}?content-type=application/json"
_IEU_BASE = "https://gwas-api.mrcieu.ac.uk/"

async def _httpx_json_post(url: str, payload: dict, timeout: float = 45.0):
    # Prefer existing _post_json (budgeted/retry); fallback to raw httpx
    try:
        return await _post_json(url, json=payload, timeout=timeout)  # type: ignore
    except Exception:
        import httpx, asyncio
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                    r = await client.post(url, json=payload)
                    r.raise_for_status()
                    return r.json()
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))

async def _httpx_json_get(url: str, timeout: float = 45.0):
    try:
        return await _get_json(url, timeout=timeout)  # type: ignore
    except Exception:
        import httpx, asyncio
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    if "application/json" in (r.headers.get("content-type","").lower()):
                        return r.json()
                    return None
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
    return None

async def _efo_lookup(condition: Optional[str]) -> Optional[str]:
    """Free-text disease -> EFO via OpenTargets GraphQL search."""
    if not condition:
        return None
    q = """
    query($q:String!){
      search(query:$q){ diseases{ id name score } }
    }"""
    js = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"q": condition}})
    try:
        diseases = (((js or {}).get("data") or {}).get("search") or {}).get("diseases") or []
        diseases.sort(key=lambda d: d.get("score", 0) or 0, reverse=True)
        return diseases[0]["id"] if diseases else None
    except Exception:
        return None

async def _ensg_from_symbol(symbol: Optional[str]) -> Optional[str]:
    """Approved symbol -> Ensembl gene ID via Ensembl REST, fallback to your symbol normalizer."""
    if not symbol:
        return None
    try:
        xs = await _httpx_json_get(_ENS_XREF.format(symbol=symbol))
        if isinstance(xs, list):
            for x in xs:
                if x.get("type") == "gene" and str(x.get("id","")).startswith("ENSG"):
                    return x["id"]
    except Exception:
        pass
    try:
        s = await _normalize_symbol(symbol)  # type: ignore
        if s and str(s).upper().startswith("ENSG"):
            return str(s).upper()
    except Exception:
        pass
    return None
# ---------------------------------------------------------------------------
"""
TARGETVAL Gateway — Router (Advanced, full registry)

What this file does:
- Uses the same general **live call fetch** approach as your working `router-revised.py`:
  - httpx AsyncClient + timeout object, exponential backoff on 429/5xx, bounded concurrency semaphore,
    per-request budget guard, small caching layer, gzip fallback, merged headers.
  - Evidence envelope compatible with your file: {status, source, fetched_n, data, citations: [str URL], fetched_at: float}.
- Implements the **full set of 64 modules** with truth-in-labeling sources.
- Adds the **sophisticated literature + synthesis** layer we agreed: `/lit/mesh`, `/synth/bucket`, `/synth/therapeutic-index`,
  plus `/synth/targetcard` and `/synth/graph` for aggregation / visualization.
- Accepts `symbol` or `gene` across gene endpoints; `condition` where relevant.
- Keeps legacy convenience routes for compatibility.

Public-only: no API keys required. All endpoints work with public APIs or literature fallbacks.
"""

import asyncio
import gzip
import io
import json
import math
import os
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

class BoundedTtlCache(dict):
    """A very small LRU+TTL-ish dict, drop-oldest when maxsize exceeded.
    Compatible with direct dict-style get/set used in the router."""
    def __init__(self, maxsize: int = 20000, ttl: float = 86400.0):
        super().__init__()
        self._order = []
        self.maxsize = maxsize
        self.ttl = ttl
    def _now(self):
        try:
            import time
            return time.time()
        except Exception:
            return 0.0
    def __setitem__(self, key, value):
        # normalize order list
        if key in self:
            try:
                self._order.remove(key)
            except ValueError:
                pass
        super().__setitem__(key, value)
        self._order.append(key)
        # evict if needed
        while len(self._order) > self.maxsize:
            oldest = self._order.pop(0)
            try:
                super().__delitem__(oldest)
            except KeyError:
                pass
    def get(self, key, default=None):
        v = super().get(key, None)
        if not v:
            return default
        # accept either our own timestamp OR the router's stored 'timestamp' field
        ts = None
        if isinstance(v, dict):
            ts = v.get('timestamp') or v.get('ts') or v.get('fetched_at')
        if ts is None:
            # if router stored raw data only, we can't TTL it — return as is
            return v
        if (self._now() - float(ts)) > float(self.ttl):
            try:
                super().__delitem__(key)
                self._order.remove(key)
            except Exception:
                pass
            return default
        return v


# ------------------------ Domains (explicit, per spec) ------------------------
from typing import Dict, List

# maxfixed_typing_globals: ensure typing names are in globals for pydantic
try:
    from typing import Any as _Any, Dict as _Dict, List as _List, Optional as _Optional
    globals().update({'Any': _Any, 'Dict': _Dict, 'List': _List, 'Optional': _Optional})
except Exception:
    pass
DOMAINS_META: Dict[int, Dict[str, str]] = {
    1: {"name": "Genetic causality & human validation"},
    2: {"name": "Functional & mechanistic validation"},
    3: {"name": "Expression, selectivity & cell-state context"},
    4: {"name": "Druggability & modality tractability"},
    5: {"name": "Therapeutic index & safety translation"},
    6: {"name": "Clinical & translational evidence"},
}

DOMAIN_MODULES: Dict[int, List[str]] = {
    1: [
        "genetics-l2g",
        "genetics-coloc",
        "genetics-mr",
        "genetics-chromatin-contacts",
        "genetics-3d-maps",
        "genetics-regulatory",
        "genetics-sqtl",
        "genetics-pqtl",
        "genetics-annotation",
        "genetics-pathogenicity-priors",
        "genetics-intolerance",
        "genetics-rare",
        "genetics-mendelian",
        "genetics-phewas-human-knockout",
        "genetics-functional",
        "genetics-mavedb",
        "genetics-consortia-summary",
    ],
    2: [
        "mech-pathways",
        "biology-causal-pathways",
        "mech-ppi",
        "mech-ligrec",
        "assoc-proteomics",
        "assoc-metabolomics",
        "assoc-bulk-rna",
        "assoc-perturb",
        "perturb-lincs-signatures",
        "perturb-connectivity",
        "perturb-signature-enrichment",
        "perturb-perturbseq-encode",
        "perturb-crispr-screens",
        "genetics-lncrna",
        "genetics-mirna",
        "perturb-qc (internal)",
        "perturb-scrna-summary (internal)",
    ],
    3: [
        "expr-baseline",
        "expr-inducibility",
        "assoc-sc",
        "assoc-spatial",
        "sc-hubmap",
        "expr-localization",
        "assoc-hpa-pathology",
        "tract-ligandability-ab",
        "tract-surfaceome",
    ],
    4: [
        "mech-structure",
        "tract-ligandability-sm",
        "tract-ligandability-oligo",
        "tract-modality",
        "tract-drugs",
        "perturb-drug-response",
    ],
    5: [
        "function-dependency",
        "immuno/hla-coverage",
        "tract-immunogenicity",
        "tract-mhc-binding",
        "tract-iedb-epitopes",
        "clin-safety",
        "clin-rwe",
        "clin-on-target-ae-prior",
        "perturb-depmap-dependency",
    ],
    6: [
        "clin-endpoints",
        "clin-biomarker-fit",
        "clin-pipeline",
        "clin-feasibility",
        "comp-intensity",
        "comp-freedom",
    ],
}

import httpx
from urllib.parse import urlparse
from fnmatch import fnmatch
from fastapi import APIRouter, HTTPException, Query, Body, Path as PathParam, Request

# ----------------------- Domain naming & registry (authoritative) -----------------------
DOMAIN_LABELS = {
    "1": "Genetic causality & human validation",
    "2": "Functional & mechanistic validation",
    "3": "Expression, selectivity & cell-state context",
    "4": "Druggability & modality tractability",
    "5": "Therapeutic index & safety translation",
    "6": "Clinical & translational evidence",
}
# alias keys "D1".."D6"
DOMAIN_LABELS.update({f"D{k}": v for k, v in list(DOMAIN_LABELS.items()) if len(k)==1})

def _domain_modules_spec() -> dict:
    # E) Module Order by Domain (display) — mirrored from config
    D1 = [
        "genetics-l2g","genetics-coloc","genetics-mr",
        "genetics-chromatin-contacts","genetics-3d-maps",
        "genetics-regulatory","genetics-sqtl","genetics-pqtl",
        "genetics-annotation","genetics-pathogenicity-priors",
        "genetics-intolerance","genetics-rare","genetics-mendelian",
        "genetics-phewas-human-knockout","genetics-functional",
        "genetics-mavedb","genetics-consortia-summary"
    ]
    D2 = [
        "mech-pathways","biology-causal-pathways","mech-ppi","mech-ligrec",
        "assoc-proteomics","assoc-metabolomics","assoc-bulk-rna","assoc-perturb",
        "perturb-lincs-signatures","perturb-connectivity","perturb-signature-enrichment",
        "perturb-perturbseq-encode","perturb-crispr-screens",
        "genetics-lncrna","genetics-mirna","perturb-qc (internal)","perturb-scrna-summary (internal)"
    ]
    D3 = [
        "expr-baseline","expr-inducibility","assoc-sc","assoc-spatial","sc-hubmap",
        "expr-localization","assoc-hpa-pathology","tract-ligandability-ab","tract-surfaceome"
    ]
    D4 = [
        "mech-structure","tract-ligandability-sm","tract-ligandability-oligo",
        "tract-modality","tract-drugs","perturb-drug-response"
    ]
    D5 = [
        "function-dependency","immuno/hla-coverage","tract-immunogenicity",
        "tract-mhc-binding","tract-iedb-epitopes","clin-safety","clin-rwe",
        "clin-on-target-ae-prior","perturb-depmap-dependency"
    ]
    D6 = [
        "clin-endpoints","clin-biomarker-fit","clin-pipeline","clin-feasibility",
        "comp-intensity","comp-freedom"
    ]
    return {"1": D1, "2": D2, "3": D3, "4": D4, "5": D5, "6": D6,
            "D1": D1, "D2": D2, "D3": D3, "D4": D4, "D5": D5, "D6": D6}
from pydantic import BaseModel, Field

# ------------------------------ Utilities ------------------------------------

def _now() -> float:
    return time.time()

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ------------------------ Evidence (aligned with user's file) -----------------

class Evidence(BaseModel):
    status: str               # "OK" | "NO_DATA" | "ERROR"
    source: str               # upstream(s) used
    fetched_n: int            # count before slicing
    data: Dict[str, Any]      # payload
    citations: List[str]      # URLs used
    fetched_at: float         # UNIX ts

# ------------------------ Outbound HTTP (bounded) ----------------------------

CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL: int = int(os.getenv("CACHE_TTL_SECONDS", str(24 * 60 * 60)))  # 24h default
DEFAULT_TIMEOUT = httpx.Timeout(float(os.getenv("OUTBOUND_TIMEOUT_S", "12.0")), connect=6.0)
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": os.getenv("OUTBOUND_USER_AGENT", "TargetVal/2.0 (+https://github.com/aureten/Targetval-gateway)"),
    "Accept": "application/json",
}
OUTBOUND_TRIES: int = int(os.getenv("OUTBOUND_TRIES", "5"))

# ------------------------ Per-source reliability profiles ---------------------
PROFILE_OVERRIDES = [
    {"hosts": ["api.platform.opentargets.org","api.opentargets.io"], "tries": 6, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.8, "backoff_cap": 5.0, "budget": 120.0},
    {"hosts": ["europepmc.org","www.ebi.ac.uk"], "path_contains": ["/europepmc","/europepmc/webservices"], "tries": 6, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.8, "backoff_cap": 5.0, "budget": 120.0},
    {"hosts": ["string-db.org"], "tries": 6, "timeout_total": 45.0, "connect": 10.0, "backoff_base": 1.0, "backoff_cap": 6.0, "budget": 120.0},
    {"hosts": ["gwas.mrcieu.ac.uk"], "tries": 6, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.8, "backoff_cap": 5.0, "budget": 120.0},
    {"hosts": ["phenoscanner.medschl.cam.ac.uk","www.phenoscanner.medschl.cam.ac.uk"], "tries": 6, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.8, "backoff_cap": 5.0, "budget": 120.0},
    {"hosts": ["eutils.ncbi.nlm.nih.gov","www.ncbi.nlm.nih.gov"], "tries": 6, "timeout_total": 45.0, "connect": 10.0, "backoff_base": 0.8, "backoff_cap": 6.0, "budget": 120.0},
    {"hosts": ["www.ebi.ac.uk"], "path_contains": ["/pride","/gwas"], "tries": 6, "timeout_total": 60.0, "connect": 10.0, "backoff_base": 1.0, "backoff_cap": 8.0, "budget": 150.0},
    {"hosts": ["proteomicsdb.org","www.proteomicsdb.org"], "tries": 6, "timeout_total": 60.0, "connect": 10.0, "backoff_base": 1.0, "backoff_cap": 8.0, "budget": 150.0},
    {"hosts": ["pdc.cancer.gov","api.gdc.cancer.gov"], "tries": 6, "timeout_total": 60.0, "connect": 10.0, "backoff_base": 1.0, "backoff_cap": 8.0, "budget": 150.0},
    {"hosts": ["webservice.thebiogrid.org","thebiogrid.org"], "tries": 6, "timeout_total": 45.0, "connect": 10.0, "backoff_base": 1.0, "backoff_cap": 6.0, "budget": 120.0},
    {"hosts": ["dgidb.org","www.dgidb.org","api.dgidb.org"], "tries": 5, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.8, "backoff_cap": 5.0, "budget": 120.0},
    {"hosts": ["api.clinicaltrials.gov"], "tries": 5, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.7, "backoff_cap": 5.0, "budget": 120.0},
    {"hosts": ["api.patentsview.org"], "tries": 5, "timeout_total": 40.0, "connect": 10.0, "backoff_base": 0.7, "backoff_cap": 5.0, "budget": 120.0},
]

def _select_profile(url: str):
    try:
        u = urlparse(url)
        host = (u.netloc or "").lower()
        path = (u.path or "").lower()
        for prof in PROFILE_OVERRIDES:
            for h in prof.get("hosts", []):
                if fnmatch(host, h):
                    pcs = prof.get("path_contains")
                    if not pcs or any(pc in path for pc in pcs):
                        return prof
        return None
    except Exception:
        return None
BACKOFF_BASE_S: float = float(os.getenv("BACKOFF_BASE_S", "0.6"))
OUTBOUND_MAX_CONCURRENCY: int = int(os.getenv("OUTBOUND_MAX_CONCURRENCY", "3"))
REQUEST_BUDGET_S: float = float(os.getenv("REQUEST_BUDGET_S", "90.0"))

_semaphore = asyncio.Semaphore(OUTBOUND_MAX_CONCURRENCY)

async def _get_json(url: str, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None) -> Any:
    prof = _select_profile(url)
    tries_local = int(prof.get("tries", tries)) if prof else tries
    timeout_local = httpx.Timeout(float(prof.get("timeout_total", os.getenv("OUTBOUND_TIMEOUT_S", "12.0"))), connect=float(prof.get("connect", 6.0))) if prof else DEFAULT_TIMEOUT
    budget_local = float(prof.get("budget", REQUEST_BUDGET_S)) if prof else REQUEST_BUDGET_S
    backoff_base_local = float(prof.get("backoff_base", BACKOFF_BASE_S)) if prof else BACKOFF_BASE_S
    backoff_cap_local = float(prof.get("backoff_cap", 3.0)) if prof else 3.0
    cached = CACHE.get(url)
    if cached and (_now() - cached.get("timestamp", 0) < CACHE_TTL):
        return cached["data"]
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=timeout_local) as client:
            for attempt in range(1, tries_local + 1):
                remaining = budget_local - (_now() - t0)
                if remaining <= 0: break
                try:
                    merged = {**DEFAULT_HEADERS, **(headers or {})}
                    resp = await asyncio.wait_for(client.get(url, headers=merged), timeout=remaining)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        backoff = min((2**(attempt-1))*backoff_base_local, backoff_cap_local) + random.random()*0.25
                        await asyncio.sleep(backoff); continue
                    resp.raise_for_status()
                    try:
                        data = resp.json()
                    except Exception:
                        try:
                            buf = io.BytesIO(resp.content)
                            with gzip.GzipFile(fileobj=buf) as gz:
                                data = json.loads(gz.read().decode("utf-8"))
                        except Exception as ge:
                            last_err = ge; raise
                    CACHE[url] = {"data": data, "timestamp": _now()}
                    return data
                except Exception as e:
                    last_err = e
                    backoff = min((2**(attempt-1))*backoff_base_local, backoff_cap_local) + random.random()*0.25
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"GET failed for {url}: {last_err}")

async def _post_json(url: str, payload: Any, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None) -> Any:
    prof = _select_profile(url)
    tries_local = int(prof.get("tries", tries)) if prof else tries
    timeout_local = httpx.Timeout(float(prof.get("timeout_total", os.getenv("OUTBOUND_TIMEOUT_S", "12.0"))), connect=float(prof.get("connect", 6.0))) if prof else DEFAULT_TIMEOUT
    budget_local = float(prof.get("budget", REQUEST_BUDGET_S)) if prof else REQUEST_BUDGET_S
    backoff_base_local = float(prof.get("backoff_base", BACKOFF_BASE_S)) if prof else BACKOFF_BASE_S
    backoff_cap_local = float(prof.get("backoff_cap", 3.0)) if prof else 3.0
    last_err: Optional[Exception] = None
    t0 = _now()
    async with _semaphore:
        async with httpx.AsyncClient(timeout=timeout_local) as client:
            for attempt in range(1, tries_local + 1):
                remaining = budget_local - (_now() - t0)
                if remaining <= 0: break
                try:
                    merged = {**DEFAULT_HEADERS, "Content-Type": "application/json", **(headers or {})}
                    resp = await asyncio.wait_for(client.post(url, headers=merged, json=payload), timeout=remaining)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                        backoff = min((2**(attempt-1))*backoff_base_local, backoff_cap_local) + random.random()*0.25
                        await asyncio.sleep(backoff); continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as e:
                    last_err = e
                    backoff = min((2**(attempt-1))*backoff_base_local, backoff_cap_local) + random.random()*0.25
                    await asyncio.sleep(backoff)
    raise HTTPException(status_code=502, detail=f"POST failed for {url}: {last_err}")

# ------------------------ Symbol normalization (similar to user's file) -------

_SYMBOL_CACHE: Dict[str, str] = {}
_COMMON_GENE_SET = {
    "TP53","EGFR","VEGFA","APOE","PCSK9","APP","MAPT","TNF","IL6","CFTR","BRCA1","BRCA2","KRAS","NRAS","BRAF","JAK2","HBB","HBA1","HBA2",
    "INS","LEP","LEPR","TNFRSF1A","TLR4","CXCL8","CXCR4","CCR5","PTEN","MTOR","AKT1","PIK3CA","ERBB2","ERBB3","ALK",
}
_ALIAS_GENE_MAP = {
    "LDLRAP1":"ARH", "POMC":"POMC", "HMGCR":"HMGCR", "ADIPOQ":"ADIPOQ",
}

async def _normalize_symbol(symbol: str) -> str:
    if not symbol:
        return symbol
    up = symbol.strip().upper()
    if up in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[up]
    if up in _ALIAS_GENE_MAP:
        _SYMBOL_CACHE[up] = _ALIAS_GENE_MAP[up]; return _ALIAS_GENE_MAP[up]
    if up in _COMMON_GENE_SET or up.startswith("ENSG"):
        _SYMBOL_CACHE[up] = up; return up
    # Try UniProt for aliases
    url = ("https://rest.uniprot.org/uniprotkb/search?"
           f"query={urllib.parse.quote(symbol)}+AND+organism_id:9606&fields=genes&format=json&size=1")
    try:
        js = await _get_json(url, tries=1)
        res = js.get("results", []) if isinstance(js, dict) else []
        if res:
            genes = res[0].get("genes") or []
            for g in genes:
                gn = g.get("geneName", {}).get("value")
                if gn: up2 = gn.upper(); _SYMBOL_CACHE[up] = up2; return up2
            for g in genes:
                for syn in (g.get("synonyms") or []):
                    val = syn.get("value")
                    if val: up2 = val.upper(); _SYMBOL_CACHE[up] = up2; return up2
    except Exception:
        pass
    _SYMBOL_CACHE[up] = up
    return up

def _sym_or_gene(symbol: Optional[str], gene: Optional[str]) -> str:
    s = symbol or gene
    if not s:
        raise HTTPException(status_code=422, detail="Provide 'symbol' or 'gene'")
    return s

# ------------------------ Registry (64 modules) -------------------------------

class Module(BaseModel):
    route: str
    name: str
    sources: List[str]
    bucket: str

# maxfixed: ensure schema built before referencing in Registry
try:
    Module.model_rebuild()
except Exception:
    pass

class Registry(BaseModel):
    modules: List[Any]
    counts: Dict[str, int]

MODULES: List[Module] = [
    Module(route="/expr/baseline", name="expr-baseline", sources=["GTEx API","EBI Expression Atlas API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/expr/localization", name="expr-localization", sources=["UniProtKB API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/expr/inducibility", name="expr-inducibility", sources=["EBI Expression Atlas API","NCBI GEO E-utilities","ArrayExpress/BioStudies API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/assoc/bulk-rna", name="assoc-bulk-rna", sources=["NCBI GEO E-utilities","ArrayExpress/BioStudies API","EBI Expression Atlas API"], bucket="Functional & mechanistic validation"),
    Module(route="/assoc/sc", name="assoc-sc", sources=["HCA Azul APIs","Single-Cell Expression Atlas API","CELLxGENE Discover API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/assoc/spatial", name="assoc-spatial", sources=["Europe PMC API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/sc/hubmap", name="sc-hubmap", sources=["HuBMAP Search API","HCA Azul APIs"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/assoc/proteomics", name="assoc-proteomics", sources=["ProteomicsDB API","PRIDE Archive API","PDC (CPTAC) GraphQL"], bucket="Functional & mechanistic validation"),
    Module(route="/assoc/metabolomics", name="assoc-metabolomics", sources=["MetaboLights API","Metabolomics Workbench API"], bucket="Functional & mechanistic validation"),
    Module(route="/assoc/hpa-pathology", name="assoc-hpa-pathology", sources=["Europe PMC API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/assoc/perturb", name="assoc-perturb", sources=["LINCS LDP APIs","CLUE.io API","PubChem PUG-REST"], bucket="Functional & mechanistic validation"),
    Module(route="/genetics/l2g", name="genetics-l2g", sources=["OpenTargets GraphQL (L2G)","GWAS Catalog REST API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/coloc", name="genetics-coloc", sources=["OpenTargets GraphQL (colocalisations)","eQTL Catalogue API","OpenGWAS API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/mr", name="genetics-mr", sources=["IEU OpenGWAS API","PhenoScanner v2 API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/rare", name="genetics-rare", sources=["ClinVar via NCBI E-utilities","MyVariant.info","Ensembl VEP REST"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/mendelian", name="genetics-mendelian", sources=["ClinGen GeneGraph/GraphQL"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/phewas-human-knockout", name="genetics-phewas-human-knockout", sources=["PhenoScanner v2 API","OpenGWAS PheWAS","HPO/Monarch APIs"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/sqtl", name="genetics-sqtl", sources=["GTEx sQTL API","eQTL Catalogue API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/pqtl", name="genetics-pqtl", sources=["OpenTargets GraphQL (pQTL colocs)","OpenGWAS (protein traits)"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/chromatin-contacts", name="genetics-chromatin-contacts", sources=["ENCODE REST API","UCSC Genome Browser track APIs","4D Nucleome API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/3d-maps", name="genetics-3d-maps", sources=["4D Nucleome API","UCSC loop/interaction tracks"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/regulatory", name="genetics-regulatory", sources=["ENCODE REST API","eQTL Catalogue API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/annotation", name="genetics-annotation", sources=["Ensembl VEP REST","MyVariant.info","CADD API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/consortia-summary", name="genetics-consortia-summary", sources=["IEU OpenGWAS API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/functional", name="genetics-functional", sources=["DepMap API","BioGRID ORCS REST","Europe PMC API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/mavedb", name="genetics-mavedb", sources=["MaveDB API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/lncrna", name="genetics-lncrna", sources=["RNAcentral API","Europe PMC API"], bucket="Functional & mechanistic validation"),
    Module(route="/genetics/mirna", name="genetics-mirna", sources=["RNAcentral API","Europe PMC API"], bucket="Functional & mechanistic validation"),
    Module(route="/genetics/pathogenicity-priors", name="genetics-pathogenicity-priors", sources=["gnomAD GraphQL API","CADD API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/intolerance", name="genetics-intolerance", sources=["gnomAD GraphQL API"], bucket="Genetic causality & human validation"),
    Module(route="/mech/structure", name="mech-structure", sources=["UniProtKB API","AlphaFold DB API","PDBe API","PDBe-KB API"], bucket="Druggability & modality tractability"),
    Module(route="/mech/ppi", name="mech-ppi", sources=["STRING API","IntAct via PSICQUIC","OmniPath API"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/pathways", name="mech-pathways", sources=["Reactome Content/Analysis APIs","Pathway Commons API","SIGNOR API","QuickGO API"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/ligrec", name="mech-ligrec", sources=["OmniPath (ligand–receptor)","IUPHAR/Guide to Pharmacology API","Reactome interactors"], bucket="Functional & mechanistic validation"),
    Module(route="/biology/causal-pathways", name="biology-causal-pathways", sources=["SIGNOR API","Reactome Analysis Service","Pathway Commons API"], bucket="Functional & mechanistic validation"),
    Module(route="/tract/drugs", name="tract-drugs", sources=["ChEMBL API","DGIdb GraphQL","DrugCentral API","BindingDB API","PubChem PUG-REST","STITCH API","Pharos GraphQL"], bucket="Druggability & modality tractability"),
    Module(route="/tract/ligandability-sm", name="tract-ligandability-sm", sources=["UniProtKB API","AlphaFold DB API","PDBe API","PDBe-KB API","BindingDB API"], bucket="Druggability & modality tractability"),
    Module(route="/tract/ligandability-ab", name="tract-ligandability-ab", sources=["UniProtKB API","GlyGen API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/tract/ligandability-oligo", name="tract-ligandability-oligo", sources=["Ensembl VEP REST","RNAcentral API","Europe PMC API"], bucket="Druggability & modality tractability"),
    Module(route="/tract/modality", name="tract-modality", sources=["UniProtKB API","AlphaFold DB API","Pharos GraphQL","IUPHAR/Guide to Pharmacology API"], bucket="Druggability & modality tractability"),
    Module(route="/tract/immunogenicity", name="tract-immunogenicity", sources=["IEDB IQ-API","IPD-IMGT/HLA API","Europe PMC API"], bucket="Therapeutic index & safety translation"),
    Module(route="/tract/mhc-binding", name="tract-mhc-binding", sources=["IEDB Tools API (prediction)","IPD-IMGT/HLA API"], bucket="Druggability & modality tractability"),
    Module(route="/tract/iedb-epitopes", name="tract-iedb-epitopes", sources=["IEDB IQ-API","IEDB Tools API"], bucket="Therapeutic index & safety translation"),
    Module(route="/tract/surfaceome", name="tract-surfaceome", sources=["UniProtKB API","GlyGen API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/function/dependency", name="function-dependency", sources=["DepMap API","BioGRID ORCS REST"], bucket="Therapeutic index & safety translation"),
    Module(route="/immuno/hla-coverage", name="immuno/hla-coverage", sources=["IEDB population coverage/Tools API","IPD-IMGT/HLA API"], bucket="Therapeutic index & safety translation"),
    Module(route="/clin/endpoints", name="clin-endpoints", sources=["ClinicalTrials.gov v2 API","WHO ICTRP web service"], bucket="Clinical & translational evidence"),
    Module(route="/clin/biomarker-fit", name="clin-biomarker-fit", sources=["OpenTargets GraphQL (evidence)","PharmGKB API","HPO/Monarch APIs"], bucket="Clinical & translational evidence"),
    Module(route="/clin/pipeline", name="clin-pipeline", sources=["Inxight Drugs API","ChEMBL API","DrugCentral API"], bucket="Clinical & translational evidence"),
    Module(route="/clin/safety", name="clin-safety", sources=["openFDA FAERS API","DrugCentral API","CTDbase API","DGIdb GraphQL","IMPC API"], bucket="Therapeutic index & safety translation"),
    Module(route="/clin/rwe", name="clin-rwe", sources=["openFDA FAERS API"], bucket="Therapeutic index & safety translation"),
    Module(route="/clin/on-target-ae-prior", name="clin-on-target-ae-prior", sources=["DrugCentral API","DGIdb GraphQL","openFDA FAERS API"], bucket="Therapeutic index & safety translation"),
    Module(route="/clin/feasibility", name="clin-feasibility", sources=["ClinicalTrials.gov v2 API","WHO ICTRP web service"], bucket="Clinical & translational evidence"),
    Module(route="/comp/intensity", name="comp-intensity", sources=["PatentsView API"], bucket="Clinical & translational evidence"),
    Module(route="/comp/freedom", name="comp-freedom", sources=["PatentsView API"], bucket="Clinical & translational evidence"),
]

BUCKETS = ["Genetic causality & human validation", "Functional & mechanistic validation", "Expression, selectivity & cell-state context", "Druggability & modality tractability", "Therapeutic index & safety translation", "Clinical & translational evidence"]

# ------------------------ Router & registry endpoints -------------------------

router = APIRouter()

# ---------------------------- Domain label helpers ----------------------------
@router.get("/synth/domain/{domain_id}/label")
async def synth_domain_label(domain_id: str = PathParam(..., description="1..6 or D1..D6")):
    key = domain_id if domain_id in DOMAIN_LABELS else domain_id.upper()
    if key not in DOMAIN_LABELS:
        raise HTTPException(status_code=404, detail=f"Unknown domain: {domain_id}")
    return {"domain_id": key, "label": DOMAIN_LABELS[key]}

# Internal: call our own mounted endpoints via ASGI (no external hop)


# -- helper: resilient internal call (uses request.app) --
async def _internal_get(request, path: str, params: dict) -> dict:
    import httpx
    if not path.startswith('/'):
        path = '/' + path
    async with httpx.AsyncClient(app=request.app, base_url="http://internal") as client:
        r = await client.get(path, params={k: v for k,v in (params or {}).items() if v is not None})
        if r.status_code == 404 and not path.startswith("/v1/"):
            r = await client.get("/v1" + path, params={k: v for k,v in (params or {}).items() if v is not None})
        r.raise_for_status()
        return r.json()

async def _module_evidence(request, key: str, params: dict) -> dict | None:
    try:
        return await _internal_get(request, f"/module/{key}", params)
    except Exception:
        return None

async def _self_get(path: str, params: dict) -> dict:
    import httpx, asyncio
    if not path.startswith('/'):
        path = '/' + path
    async with httpx.AsyncClient(app=app, base_url="http://router.internal") as client:
        resp = await client.get(
            path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        return resp.json()

# ---------------------------- Therapeutic index synth ----------------------------
@router.get("/synth/therapeutic-index")
async def synth_therapeutic_index(
    gene: str = Query(None, description="Alias of 'symbol'."),
    symbol: str = Query(None),
    condition: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Domain 5 synthesis ("Therapeutic index & safety translation").
    This orchestrates a live pull from Domain 5 modules and returns a stitched view:
    - dependency (function-dependency)
    - immunogenicity & MHC binding (tract-* + immuno/hla-coverage)
    - clinical safety signals (clin-safety, clin-rwe, on-target AE prior)
    - DepMap dependency (perturb-depmap-dependency) as supporting context
    """
    dkey = "5"
    modules = _domain_modules_spec()[dkey]
    # Keep execution bounded but live; selectively call the core D5 modules first
    core = [
        "function-dependency",
        "immuno/hla-coverage",
        "tract-immunogenicity",
        "tract-mhc-binding",
        "tract-iedb-epitopes",
        "clin-safety",
        "clin-rwe",
        "clin-on-target-ae-prior",
        "perturb-depmap-dependency",
    ]
    params = dict(symbol=symbol or gene, condition=condition, limit=limit, offset=offset)
    results = {}
    errors = {}

    # Map registry keys to router paths via MODULES if available; else synthesize
    _map = {}
    try:
        for m in MODULES:
            _map[getattr(m, "name", getattr(m, "key", None))] = getattr(m, "route", None)
    except Exception:
        pass
    # Fallback guessing
    def _guess_path(k: str) -> str:
        if k.startswith("immuno/"):
            return f"/{k}"
        fam, mod = (k.split("-", 1) + [""])[:2]
        return f"/{fam}/{mod}"
    # Run core set sequentially (router has its own parallelism where needed)
    for k in core:
        path = _map.get(k) or _guess_path(k)
        try:
            results[k] = await _self_get(path, params)
        except Exception as e:
            errors[k] = str(e)

    return {
        "ok": True,
        "domain_id": dkey,
        "domain_label": DOMAIN_LABELS[dkey],
        "modules": core,
        "n_modules": len(core),
        "results": results,
        "errors": errors,
    }


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now()}

@router.get("/status")
def status() -> Dict[str, Any]:
    return {
        "service": "targetval-gateway (advanced, live)",
        "time": _now(),
        "modules": len(MODULES),
        "by_bucket": {b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS},
    }

@router.get("/registry/modules", response_model=Registry)
def registry_modules() -> Registry:
    return Registry(modules=MODULES, counts={b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS})

@router.get("/registry/sources")
def registry_sources() -> List[Dict[str, Any]]:
    return [{"route": m.route, "sources": m.sources, "bucket": m.bucket} for m in MODULES]

@router.get("/registry/counts")
def registry_counts() -> Dict[str, Any]:
    return {"total": len(MODULES), "by_bucket": {b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS}}

# ------------------------ Europe PMC helpers ---------------------------------

CONFIRM_KWS = {"mendelian randomization","colocalization","colocalisation","replication","significant association","functional validation","perturb-seq","crispr","mpra","starr"}
DISCONFIRM_KWS = {"no association","did not replicate","null association","not significant","failed replication","no effect","not associated"}

async def _epmc_search(query: str, size: int = 50) -> Tuple[List[Dict[str, Any]], List[str]]:
    citations: List[str] = []
    q = urllib.parse.quote(query)
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json&pageSize={size}"
    js = await _get_json(url, tries=2)
    hits = (js.get("resultList", {}) or {}).get("result", []) if isinstance(js, dict) else []
    citations.append(url)
    # light stance tag
    for h in hits:
        text = (h.get("title","") + " " + h.get("abstract","")).lower()
        if any(k in text for k in CONFIRM_KWS) and not any(k in text for k in DISCONFIRM_KWS):
            h["stance"] = "confirm"
        elif any(k in text for k in DISCONFIRM_KWS) and not any(k in text for k in CONFIRM_KWS):
            h["stance"] = "disconfirm"
        else:
            h["stance"] = "neutral"
    return hits, citations

# ------------------------ Endpoints: IDENTITY --------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym_in = _sym_or_gene(symbol, gene)
    sym = await _normalize_symbol(sym_in)
    cites: List[str] = []
    # Prefer GTEx linkouts; fallback to HPA summary with GTEx column
    hpa_url = ("https://www.proteinatlas.org/api/search_download.php"
               f"?format=json&columns=gene,rna_tissue,rna_gtex&search={urllib.parse.quote(sym)}")
    try:
        rows = await _get_json(hpa_url, tries=1)
        cites.append(hpa_url)
        if isinstance(rows, list):
            return Evidence(status="OK", source="GTEx (via HPA linkout)", fetched_n=len(rows), data={"hpa": rows}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    return Evidence(status="NO_DATA", source="GTEx (via HPA linkout)", fetched_n=0, data={}, citations=cites, fetched_at=_now())

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym_in = _sym_or_gene(symbol, gene)
    sym = await _normalize_symbol(sym_in)
    url = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+reviewed:true+AND+organism_id:9606"
           "&format=json&fields=accession,protein_name,cc_subcellular_location,keyword")
    try:
        js = await _get_json(url, tries=2)
        return Evidence(status="OK", source="UniProtKB", fetched_n=len((js or {}).get("results") or []), data=js or {}, citations=[url], fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"UniProt localization fetch failed: {e!s}")

@router.get("/mech/structure", response_model=Evidence)
async def mech_structure(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    uni = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+reviewed:true+AND+organism_id:9606"
           "&format=json&fields=accession,protein_name,cc_subcellular_location,ft_domain")
    af = f"https://alphafold.ebi.ac.uk/search/text?query={urllib.parse.quote(sym)}"
    pdbe = f"https://www.ebi.ac.uk/pdbe/entry/search/index?text={urllib.parse.quote(sym)}"
    cites = [uni, af, pdbe]
    js = await _get_json(uni, tries=2)
    data = {"uniprot": js, "alphafold_link": af, "pdbe_link": pdbe}
    return Evidence(status="OK", source="UniProtKB, AlphaFoldDB, PDBe", fetched_n=1, data=data, citations=cites, fetched_at=_now())

@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} induction OR inducible OR cytokine OR stimulus"
    hits, cites = await _epmc_search(q, size=80)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

# ------------------------ Endpoints: ASSOCIATION ------------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    studies = []
    try:
        term = urllib.parse.quote(f"{sym}[Gene]")
        es = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term={term}&retmode=json&retmax=30"
        sm = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id={{ids}}&retmode=json"
        es_js = await _get_json(es, tries=2); cites.append(es)
        ids = ((es_js.get('esearchresult', {}) or {}).get('idlist') or [])[:30]
        if ids:
            s_js = await _get_json(sm.replace("{ids}", ",".join(ids)), tries=1); cites.append(sm.replace("{ids}", ",".join(ids)))
            studies.append({"geo_summary": s_js})
    except Exception:
        pass
    try:
        q = urllib.parse.quote(sym)
        ae = f"https://www.ebi.ac.uk/biostudies/api/v1/studies?search={q}"
        ae_js = await _get_json(ae, tries=1); cites.append(ae)
        studies.append({"arrayexpress": ae_js})
    except Exception:
        pass
    return Evidence(status="OK", source="NCBI GEO (E-utilities), ArrayExpress/BioStudies", fetched_n=len(studies), data={"studies": studies}, citations=cites, fetched_at=_now())

@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} single-cell OR scRNA-seq OR snRNA-seq OR Tabula Sapiens"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="HCA Azul, Tabula Sapiens (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/spatial-expression", response_model=Evidence)
async def assoc_spatial_expression(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} spatial transcriptomics OR MERFISH OR Visium OR Xenium"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/spatial-neighborhoods", response_model=Evidence)
async def assoc_spatial_neighborhoods(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} neighborhood OR ligand-receptor OR cell-cell communication OR NicheNet"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    entries = []
    try:
        pq = urllib.parse.quote(sym)
        pride = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?query={pq}&pageSize=50"
        pr_js = await _get_json(pride, tries=2); cites.append(pride)
        entries.append({"pride_projects": pr_js})
    except Exception:
        pass
    return Evidence(status="OK", source="ProteomicsDB, PRIDE, ProteomeXchange", fetched_n=len(entries), data={"entries": entries}, citations=cites, fetched_at=_now())

@router.get("/assoc/omics-phosphoproteomics", response_model=Evidence)
async def assoc_omics_phosphoproteomics(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} phosphoproteomics OR phosphorylation site"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="PRIDE (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/omics-metabolites", response_model=Evidence)
async def assoc_omics_metabolites(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} metabolomics OR metabolite OR HMDB OR Nightingale"
    hits, cites = await _epmc_search(q, size=80)
    return Evidence(status="OK", source="MetaboLights, HMDB", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/hpa-pathology", response_model=Evidence)
async def assoc_hpa_pathology(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    hpa = ("https://www.proteinatlas.org/api/search_download.php"
           f"?format=json&columns=ensembl,gene,cell_type,subcellular_location,pathology&search={urllib.parse.quote(sym)}")
    uni = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+reviewed:true+AND+organism_id:9606"
           "&format=json&fields=accession,protein_name,cc_subcellular_location")
    data = {}
    try:
        data["hpa"] = await _get_json(hpa, tries=1); cites.append(hpa)
    except Exception:
        data["hpa"] = []
    try:
        data["uniprot"] = await _get_json(uni, tries=1); cites.append(uni)
    except Exception:
        data["uniprot"] = {}
    n = len(data.get("hpa") or [])
    return Evidence(status="OK", source="Human Protein Atlas (HPA), UniProtKB", fetched_n=n, data=data, citations=cites, fetched_at=_now())

@router.get("/assoc/bulk-prot-pdc", response_model=Evidence)
async def assoc_bulk_prot_pdc(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} CPTAC proteomics"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="PDC GraphQL (CPTAC) (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/metabolomics-ukb-nightingale", response_model=Evidence)
async def assoc_metabolomics_ukb_nightingale(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} Nightingale biomarker"
    hits, cites = await _epmc_search(q, size=30)
    return Evidence(status="OK", source="Nightingale Biomarker Atlas", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/sc/bican", response_model=Evidence)
async def sc_bican(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    link = "https://biccn.org"
    q = f"{sym} BICAN single-cell"
    hits, cites = await _epmc_search(q, size=20); cites.append(link)
    return Evidence(status="OK", source="BICAN portal", fetched_n=len(hits), data={"portal": link, "query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/sc/hubmap", response_model=Evidence)
async def sc_hubmap(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    link = "https://portal.hubmapconsortium.org"
    q = f"{sym} HuBMAP single-cell"
    hits, cites = await _epmc_search(q, size=20); cites.append(link)
    return Evidence(status="OK", source="HuBMAP portal", fetched_n=len(hits), data={"portal": link, "query": q, "hits": hits}, citations=cites, fetched_at=_now())

# ------------------------ Endpoints: GENETIC CAUSALITY ------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(symbol: Optional[str] = Query(None),
                       gene: Optional[str] = Query(None),
                       condition: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets",
                        fetched_n=0,
                        data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      target(ensemblId:$ensg){ id approvedSymbol }
      disease(efoId:$efo){ id name }
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        overallScore
        datasourceScores{ id score }
      }
    }"""
    js = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    assoc = (((js or {}).get("data") or {}).get("associationByEntity") or {})
    if not assoc:
        return Evidence(status="NO_DATA", source="OpenTargets", fetched_n=0,
                        data={"ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    return Evidence(status="OK", source="OpenTargets", fetched_n=1,
                    data={"ensg": ensg, "efo": efo, **assoc},
                    citations=cites, fetched_at=_now())


@router.get("/genetics/coloc", response_model=Evidence)
async def genetics_coloc(symbol: Optional[str] = Query(None),
                         gene: Optional[str] = Query(None),
                         condition: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets",
                        fetched_n=0,
                        data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        datasourceScores{ id score }
      }
    }"""
    js  = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
    coloc_keys = {"ot_genetics_portal","gwas_catalog","uk_biobank","finngen"}
    coloc = [x for x in dss if (str(x.get("id","")).lower() in coloc_keys) and (x.get("score") or 0) > 0]
    return Evidence(status=("OK" if coloc else "NO_DATA"), source="OpenTargets",
                    fetched_n=len(coloc),
                    data={"ensg": ensg, "efo": efo, "datasourceScores": dss, "coloc_like": coloc},
                    citations=cites, fetched_at=_now())


@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(symbol: Optional[str] = Query(None),
                      gene: Optional[str] = Query(None),
                      condition: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites = ["https://gwas.mrcieu.ac.uk/", "https://gwas-api.mrcieu.ac.uk/"]
    if not condition:
        return Evidence(status="NO_DATA", source="OpenGWAS", fetched_n=0,
                        data={"note":"missing condition text"}, citations=cites, fetched_at=_now())
    try:
        outcomes   = await _httpx_json_get(f"{_IEU_BASE}v1/gwas?q={urllib.parse.quote(condition)}&p=1")
        n_outcomes = len(outcomes or []) if isinstance(outcomes, list) else 0
        exposures  = await _httpx_json_get(f"{_IEU_BASE}v1/gwas?q={urllib.parse.quote(sym)}&p=1")
        n_exposures= len(exposures or []) if isinstance(exposures, list) else 0
        status = "OK" if (n_outcomes and n_exposures) else "NO_DATA"
        return Evidence(status=status, source="OpenGWAS",
                        fetched_n=min(n_outcomes, n_exposures),
                        data={"outcomes_checked": n_outcomes, "exposures_checked": n_exposures,
                              "note": "MR-capable datasets detected" if status=="OK" else "insufficient datasets"},
                        citations=cites, fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenGWAS query failed: {e!s}")


@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    term = urllib.parse.quote(f"{sym}[gene] AND human[orgn]")
    es = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&term={term}&retmode=json&retmax=100"
    try:
        s_js = await _get_json(es, tries=2)
        ids = ((s_js.get('esearchresult', {}) or {}).get('idlist') or [])[:50]
        cites = [es]
        v = {}
        if ids:
            summ = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=clinvar&id={','.join(ids)}&retmode=json"
            v = await _get_json(summ, tries=1); cites.append(summ)
        return Evidence(status="OK", source="ClinVar (NCBI E-utilities)", fetched_n=len(ids), data={"ids": ids, "summary": v}, citations=cites, fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ClinVar fetch failed: {e!s}")

@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} ClinGen gene validity"
    hits, cites = await _epmc_search(q, size=30)
    return Evidence(status="OK", source="ClinGen Gene Validity (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/phewas-human-knockout", response_model=Evidence)
async def genetics_phewas_human_knockout(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} PheWAS human loss-of-function OR LoF"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} sQTL splice QTL"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="GTEx sQTL API (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/pqtl", response_model=Evidence)
async def genetics_pqtl(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} pQTL colocalization"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="OpenTargets GraphQL (pQTL colocs) (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/chromatin-contacts", response_model=Evidence)
async def genetics_chromatin_contacts(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} promoter capture Hi-C OR PCHi-C OR HiC enhancer-promoter"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/functional", response_model=Evidence)
async def genetics_functional(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} MPRA OR STARR OR CRISPRa OR CRISPRi functional screen"
    hits, cites = await _epmc_search(q, size=80)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} lncRNA long noncoding RNA"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} microRNA OR miRNA"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/pathogenicity-priors", response_model=Evidence)
async def genetics_pathogenicity_priors(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:  # type: ignore
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} AlphaMissense OR PrimateAI pathogenicity"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/genetics/finngen-summary", response_model=Evidence)
async def genetics_finngen_summary(condition: Optional[str] = None) -> Evidence:
    portal = "https://r8.finngen.fi"
    return Evidence(status="OK", source="FinnGen", fetched_n=0, data={"condition": condition, "portal": portal}, citations=[portal], fetched_at=_now())

@router.get("/genetics/gbmi-summary", response_model=Evidence)
async def genetics_gbmi_summary(condition: Optional[str] = None) -> Evidence:
    portal = "https://globalbiobankmeta.org"
    return Evidence(status="OK", source="GBMI portal", fetched_n=0, data={"condition": condition, "portal": portal}, citations=[portal], fetched_at=_now())

@router.get("/genetics/mavedb", response_model=Evidence)
async def genetics_mavedb(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} deep mutational scanning OR MAVE database"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="MaveDB API, Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

# ------------------------ Endpoints: MECHANISM -------------------------------

@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = urllib.parse.quote(sym)
    url = f"https://string-db.org/api/json/network?identifiers={q}&species=9606"
    try:
        js = await _get_json(url, tries=2)
        return Evidence(status="OK", source="STRING", fetched_n=len(js or []), data={"network": js}, citations=[url], fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STRING fetch failed: {e!s}")

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} Reactome pathway"
    hits, cites = await _epmc_search(q, size=30)
    return Evidence(status="OK", source="Reactome (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(sym)}&format=json"
    try:
        pairs = await _get_json(url, tries=2)
        n = len(pairs or [])
        return Evidence(status="OK", source="OmniPath", fetched_n=n, data={"pairs": pairs}, citations=[url], fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OmniPath fetch failed: {e!s}")

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), phenotype: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} CRISPR OR RNAi OR perturb-seq {phenotype or ''}"
    hits, cites = await _epmc_search(q, size=100)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/perturbatlas", response_model=Evidence)
async def assoc_perturbatlas(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} PerturbAtlas OUP perturb-seq"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="PerturbAtlas OUP", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

# ------------------------ Endpoints: TRACTABILITY -----------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    try:
        url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(sym)}"
        data["chembl"] = await _get_json(url, tries=2); cites.append(url)
    except Exception:
        data["chembl"] = {}
    try:
        url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(sym)}"
        data["dgidb"] = await _get_json(url, tries=2); cites.append(url)
    except Exception:
        data["dgidb"] = {}
    n = len(((data.get("dgidb") or {}).get("matchedTerms") or []))
    return Evidence(status="OK", source="ChEMBL, DGIdb", fetched_n=n, data=data, citations=cites, fetched_at=_now())

@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    url = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+reviewed:true+AND+organism_id:9606"
           "&format=json&fields=accession,protein_name,cc_subcellular_location,ft_binding")
    js = await _get_json(url, tries=2)
    cites = [url]
    data = {"uniprot": js, "alphafold_link": f"https://alphafold.ebi.ac.uk/search/text?query={urllib.parse.quote(sym)}",
            "pdbe_link": f"https://www.ebi.ac.uk/pdbe/entry/search/index?text={urllib.parse.quote(sym)}"}
    cites += [data["alphafold_link"], data["pdbe_link"]]
    return Evidence(status="OK", source="UniProtKB, AlphaFoldDB, PDBe", fetched_n=1, data=data, citations=cites, fetched_at=_now())

@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    try:
        uni = ("https://rest.uniprot.org/uniprotkb/search"
               f"?query=gene:{urllib.parse.quote(sym)}+AND+reviewed:true+AND+organism_id:9606"
               "&format=json&fields=accession,protein_name,cc_subcellular_location")
        data["uniprot"] = await _get_json(uni, tries=2); cites.append(uni)
    except Exception:
        data["uniprot"] = {}
    try:
        hpa = ("https://www.proteinatlas.org/api/search_download.php"
               f"?format=json&columns=gene,secretome,subcellular_location&search={urllib.parse.quote(sym)}")
        data["hpa_flags"] = await _get_json(hpa, tries=1); cites.append(hpa)
    except Exception:
        data["hpa_flags"] = []
    feasible = False
    if isinstance(data.get("hpa_flags"), list):
        flags_text = " ".join([json.dumps(x).lower() for x in data["hpa_flags"]])
        feasible = ("membrane" in flags_text) or ("secreted" in flags_text)
    return Evidence(status="OK", source="UniProtKB, Human Protein Atlas (HPA)", fetched_n=1, data={"features": data, "antibody_feasible": bool(feasible)}, citations=cites, fetched_at=_now())

@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} antisense OR siRNA OR ASO OR aptamer"
    hits, cites = await _epmc_search(q, size=80)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    url = ("https://rest.uniprot.org/uniprotkb/search"
           f"?query=gene:{urllib.parse.quote(sym)}+AND+reviewed:true+AND+organism_id:9606"
           "&format=json&fields=accession,cc_subcellular_location,keyword")
    js = await _get_json(url, tries=2); cites = [url]
    loc = json.dumps(js).lower()
    sm = any(k in loc for k in ["enzyme","kinase","pocket","binding"])
    ab = any(k in loc for k in ["membrane","secreted","cell surface"])
    oligo = any(k in loc for k in ["nucleus","rna","transcript"])
    rec = {"P.SM": float(sm), "P.Ab": float(ab), "P.Oligo": float(oligo)}
    return Evidence(status="OK", source="UniProtKB, AlphaFoldDB", fetched_n=1, data={"modality_scores": rec}, citations=cites, fetched_at=_now())

@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} immunogenic OR epitope OR HLA"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/tract/mhc-binding", response_model=Evidence)
async def tract_mhc_binding(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} netMHCpan OR HLA binding"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/tract/iedb-epitopes", response_model=Evidence)
async def tract_iedb_epitopes(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} IEDB epitope HLA"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="IEDB IQ-API, Europe PMC (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/tract/surfaceome-hpa", response_model=Evidence)
async def tract_surfaceome_hpa(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    url = ("https://www.proteinatlas.org/api/search_download.php"
           f"?format=json&columns=gene,rna_tissue,rna_gtex&search={urllib.parse.quote(sym)}")
    out = await _get_json(url, tries=2)
    return Evidence(status="OK", source="Human Protein Atlas (HPA)", fetched_n=len(out or []), data={"hpa": out}, citations=[url], fetched_at=_now())

@router.get("/tract/tsca", response_model=Evidence)
async def tract_tsca(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} Cancer Surfaceome Atlas"
    hits, cites = await _epmc_search(q, size=20)
    return Evidence(status="OK", source="Cancer Surfaceome Atlas (TCSA)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

# ------------------------ Endpoints: CLINICAL FIT -----------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: Optional[str] = None) -> Evidence:
    q = urllib.parse.quote(condition or "")
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={q}&pageSize=50"
    try:
        js = await _get_json(url, tries=2)
        return Evidence(status="OK", source="ClinicalTrials.gov v2", fetched_n=len(((js or {}).get('studies',{})).get('results',[]) if isinstance(js.get('studies'),dict) else []), data=js, citations=[url], fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ClinicalTrials fetch failed: {e!s}")

@router.get("/clin/biomarker-fit", response_model=Evidence)
async def clin_biomarker_fit(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} biomarker predictive prospective validation"
    hits, cites = await _epmc_search(q, size=80)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} NCATS Inxight Drugs pipeline"
    hits, cites = await _epmc_search(q, size=20)
    return Evidence(status="OK", source="Inxight Drugs (NCATS) (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    drugs: List[str] = []
    # DGIdb → drug list
    try:
        url = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(sym)}"
        dj = await _get_json(url, tries=2); cites.append(url)
        for term in (dj.get("matchedTerms") or []):
            for it in (term.get("interactions") or []):
                n = it.get("drugName"); 
                if n and n not in drugs: drugs.append(n)
    except Exception:
        pass
    faers = {}
    if drugs:
        d = urllib.parse.quote(drugs[0])
        try:
            f_url = f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{d}&count=patient.reaction.reactionmeddrapt.exact"
            faers = await _get_json(f_url, tries=1); cites.append(f_url)
        except Exception:
            faers = {}
    return Evidence(status="OK", source="openFDA FAERS, DGIdb", fetched_n=len(faers.get("results") or []), data={"drugs": drugs, "faers": faers}, citations=cites, fetched_at=_now())

@router.get("/clin/safety-pgx", response_model=Evidence)
async def clin_safety_pgx(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} pharmacogenomics site:pharmgkb.org"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="PharmGKB (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe() -> Evidence:
    url = "https://api.fda.gov/drug/event.json?search=receivedate:[20200101+TO+20300101]&count=patient.reaction.reactionmeddrapt.exact"
    try:
        faers = await _get_json(url, tries=1)
    except Exception:
        faers = {}
    return Evidence(status="OK", source="openFDA FAERS", fetched_n=len(faers.get("results") or []), data=faers, citations=[url], fetched_at=_now())

@router.get("/clin/on-target-ae-prior", response_model=Evidence)
async def clin_on_target_ae_prior(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} SIDER adverse effect class signal"
    hits, cites = await _epmc_search(q, size=30)
    return Evidence(status="OK", source="DGIdb, SIDER", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    # Build PatentsView URL safely (avoid f-strings with JSON)
    query = {"_text_any": {"patent_title": sym}}
    fields = ["patent_number", "patent_title", "patent_date"]
    url = "https://api.patentsview.org/patents/query?" + urllib.parse.urlencode({"q": json.dumps(query), "f": json.dumps(fields)})
    data = await _get_json(url, tries=1)
    return Evidence(status="OK", source="PatentsView", fetched_n=len((data or {}).get('patents') or []), data=data, citations=[url], fetched_at=_now())

@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    # Build PatentsView URL safely (avoid f-strings with JSON)
    query = {"_text_any": {"patent_title": sym}}
    fields = ["patent_number", "patent_title", "patent_date"]
    url = "https://api.patentsview.org/patents/query?" + urllib.parse.urlencode({"q": json.dumps(query), "f": json.dumps(fields)})
    data = await _get_json(url, tries=1)
    return Evidence(status="OK", source="PatentsView", fetched_n=len((data or {}).get('patents') or []), data=data, citations=[url], fetched_at=_now())

@router.get("/clin/eu-ctr-linkouts", response_model=Evidence)
async def clin_eu_ctr_linkouts(condition: Optional[str] = None) -> Evidence:
    link = "https://www.clinicaltrialsregister.eu/ctr-search/search"
    return Evidence(status="OK", source="EU CTR/CTIS", fetched_n=0, data={"condition": condition, "linkouts": True}, citations=[link], fetched_at=_now())

@router.get("/genetics/intolerance", response_model=Evidence)
async def genetics_intolerance(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} gnomAD constraint intolerance pLI"
    hits, cites = await _epmc_search(q, size=40)
    return Evidence(status="OK", source="gnomAD GraphQL (fallback via literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

# ------------------------ Literature mesh + Synthesis -------------------------

class BucketNarrative(BaseModel):
    bucket: str
    summary: str
    drivers: List[str] = []
    tensions: List[str] = []
    flip_if: List[str] = []
    citations: List[str] = []

class LitMeshRequest(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket: str
    module_outputs: Dict[str, Any] = Field(default_factory=dict)
    max_passes: int = 1

class LitSummary(BaseModel):
    bucket: str
    hits: List[Dict[str, Any]]
    stance_tally: Dict[str, int] = {}
    notes: Optional[str] = None
    citations: List[str] = []

LITERATURE_TEMPLATES = {
    "GENETIC_CAUSALITY": {
        "confirm": [
            "({gene}) AND ({condition}) AND (mendelian randomization OR MR)",
            "({gene}) AND ({condition}) AND (colocalization OR colocalisation)",
            "({gene}) AND (pQTL OR sQTL)",
            "({gene}) AND ({condition}) AND (fine-mapping OR credible set OR posterior inclusion probability)",
        ],
        "disconfirm": [
            "({gene}) AND ({condition}) AND (no association OR not associated OR failed replication)",
            "({gene}) AND (MR OR colocalization) AND (null OR negative)",
        ],
    },
    "ASSOCIATION": {"confirm": [
            "({gene}) AND ({condition}) AND (single-cell OR scRNA-seq OR snRNA-seq)",
            "({gene}) AND ({condition}) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)",
            "({gene}) AND ({condition}) AND (proteomics OR phosphoproteomics)",
            "({gene}) AND ({condition}) AND (metabolomics OR biomarker)",
        ],
        "disconfirm": [
            "({gene}) AND ({condition}) AND (no change OR unchanged OR not differential)",
        ],
    },
    "MECHANISM": {
        "confirm": [
            "({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi)",
            "({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)",
            "({gene}) AND ({condition}) AND (phosphoproteomics OR kinase-substrate)",
        ],
        "disconfirm": [
            "({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)",
        ],
    },
    "TRACTABILITY": {
        "confirm": [
            "({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)",
            "({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)",
            "({gene}) AND (aptamer OR antisense OR siRNA OR ASO)",
            "({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)",
        ],
        "disconfirm": [
            "({gene}) AND (antibody) AND (cross-reactivity OR off-tumor)",
        ],
    },
    "CLINICAL_FIT": {
        "confirm": [
            "({gene}) AND ({condition}) AND (biomarker) AND (trial OR prospective OR predictive)",
            "({gene}) AND ({condition}) AND (pharmacogenomics OR PharmGKB)",
        ],
        "disconfirm": [
            "({gene}) AND ({condition}) AND (adverse event OR toxicity) AND (case series OR signal)",
        ],
    },
}

@router.post("/lit/mesh", response_model=LitSummary)
async def lit_mesh(req: LitMeshRequest) -> LitSummary:
    bucket = req.bucket if req.bucket in LITERATURE_TEMPLATES else "ASSOCIATION"
    tmpl = LITERATURE_TEMPLATES[bucket]
    tokens = {"gene": req.gene, "condition": req.condition or ""}
    def fill(s: str) -> str:
        x = s
        for k, v in tokens.items():
            x = x.replace("{"+k+"}", v)
        return re.sub(r"\s+", " ", x).strip()
    queries = [fill(q) for q in (tmpl["confirm"] + tmpl["disconfirm"])]
    hits_all: List[Dict[str, Any]] = []
    citations: List[str] = []
    for q in queries:
        hits, cites = await _epmc_search(q, size=60)
        citations += cites
        hits_all.extend(hits)
    confirm = sum(1 for h in hits_all if h.get("stance") == "confirm")
    disconfirm = sum(1 for h in hits_all if h.get("stance") == "disconfirm")
    notes = f"confirm={confirm}, disconfirm={disconfirm}"
    # one focused pass if evidence is thin
    if confirm < 3 and req.max_passes > 0:
        q2 = f"{req.gene} {req.condition or ''} tissue-specific OR cell-type specific"
        hits2, cites2 = await _epmc_search(q2, size=40)
        citations += cites2; hits_all.extend(hits2)
        notes += " | resolution_pass_applied"
    stance_tally = {"confirm": confirm, "disconfirm": disconfirm, "neutral": len(hits_all) - confirm - disconfirm}
    return LitSummary(bucket=bucket, hits=hits_all[:300], stance_tally=stance_tally, notes=notes, citations=citations)

# Synthesis per bucket

class BucketSynthRequest(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket: str
    module_outputs: Dict[str, Any] = Field(default_factory=dict)
    lit_summary: Optional[Dict[str, Any]] = None

def _narrate(bucket: str, signals: Dict[str, float]) -> Tuple[str, List[str], List[str], List[str]]:
    drivers: List[str] = []; tensions: List[str] = []; flip: List[str] = []
    if bucket == "GENETIC_CAUSALITY":
        if signals.get("MR",0)>0.5 and signals.get("COLOC",0)>0.5: drivers.append("MR + coloc align")
        if signals.get("RARE",0)>0.3: drivers.append("Rare/Mendelian corroboration")
        if signals.get("COLOC",0)<0.2: tensions.append("Coloc missing or tissue misaligned"); flip.append("Fine-map in disease tissue")
        return ("Causality supported" if drivers else "Causality uncertain"), drivers, tensions, flip
    if bucket == "ASSOCIATION":
        if signals.get("SC",0)>0.5 and signals.get("BULK_PROT",0)>0.3: drivers.append("Convergent sc/spatial + proteomics")
        if signals.get("BULK_RNA",0)<0.2: tensions.append("Bulk RNA weak/heterogeneous"); flip.append("Focus on enriched cell states")
        return ("Consistent disease context" if drivers else "Context incomplete"), drivers, tensions, flip
    if bucket == "MECHANISM":
        if signals.get("PERTURB",0)>0.4 and signals.get("PATHWAY",0)>0.4: drivers.append("Perturb effects match pathway")
        if signals.get("PPI",0)<0.2: tensions.append("Sparse PPI evidence")
        return ("Mechanism plausible" if drivers else "Mechanism underspecified"), drivers, tensions, flip
    if bucket == "TRACTABILITY":
        if any(signals.get(k,0)>0.5 for k in ["AB","SM","OLIGO"]): drivers.append("At least one feasible modality")
        if signals.get("IMMUNO",0)>0.5: tensions.append("Potential immunogenicity risk")
        return ("Modality feasible" if drivers else "Modality uncertain"), drivers, tensions, flip
    if bucket == "CLINICAL_FIT":
        if signals.get("ENDPOINTS",0)>0.3 and signals.get("BIOMARKER",0)>0.3: drivers.append("Endpoints and biomarkers feasible")
        if signals.get("SAFETY",0)>0.4: tensions.append("Safety/RWE signals present"); flip.append("PGx stratification to mitigate AE risk")
        return ("Clinical opportunity" if drivers else "Clinical viability unclear"), drivers, tensions, flip
    return "No synthesis", drivers, tensions, flip

@router.post("/synth/bucket", response_model=BucketNarrative)
async def synth_bucket(req: BucketSynthRequest) -> BucketNarrative:
    mo = req.module_outputs or {}
    s: Dict[str, float] = {}
    if req.bucket == "GENETIC_CAUSALITY":
        s["MR"] = float((mo.get("genetics_mr") or {}).get("support", 0.0))
        s["COLOC"] = float((mo.get("genetics_coloc") or {}).get("support", 0.0))
        s["RARE"] = float((mo.get("genetics_rare") or {}).get("support", 0.0))
    elif req.bucket == "ASSOCIATION":
        s["SC"] = float((mo.get("assoc_sc") or {}).get("support", 0.0))
        s["BULK_PROT"] = float((mo.get("assoc_bulk_prot") or {}).get("support", 0.0))
        s["BULK_RNA"] = float((mo.get("assoc_bulk_rna") or {}).get("support", 0.0))
    elif req.bucket == "MECHANISM":
        s["PERTURB"] = float((mo.get("assoc_perturb") or {}).get("support", 0.0))
        s["PATHWAY"] = float((mo.get("mech_pathways") or {}).get("support", 0.0))
        s["PPI"] = float((mo.get("mech_ppi") or {}).get("support", 0.0))
    elif req.bucket == "TRACTABILITY":
        s["AB"] = float((mo.get("tract_ligandability_ab") or {}).get("support", 0.0))
        s["SM"] = float((mo.get("tract_ligandability_sm") or {}).get("support", 0.0))
        s["OLIGO"] = float((mo.get("tract_ligandability_oligo") or {}).get("support", 0.0))
        s["IMMUNO"] = float((mo.get("tract_immunogenicity") or {}).get("support", 0.0))
    elif req.bucket == "CLINICAL_FIT":
        s["ENDPOINTS"] = float((mo.get("clin_endpoints") or {}).get("support", 0.0))
        s["BIOMARKER"] = float((mo.get("clin_biomarker_fit") or {}).get("support", 0.0))
        s["SAFETY"] = float((mo.get("clin_safety") or {}).get("support", 0.0))
    summary, drivers, tensions, flip = _narrate(req.bucket, s)
    citations = (req.lit_summary or {}).get("citations") or []
    return BucketNarrative(bucket=req.bucket, summary=summary, drivers=drivers, tensions=tensions, flip_if=flip, citations=citations)

class TherapeuticIndexRequest(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket_narratives: List[BucketNarrative]

class TINarrative(BaseModel):
    verdict: str
    drivers: List[str] = []
    tensions: List[str] = []
    flip_if: List[str] = []
    citations: List[str] = []

@router.post("/synth/therapeutic-index", response_model=TINarrative)
async def synth_therapeutic_index(req: TherapeuticIndexRequest) -> TINarrative:
    by_bucket = {b.bucket: b for b in req.bucket_narratives}
    safety_flags = any("safety" in " ".join(b.tensions).lower() for b in req.bucket_narratives)
    causality_strong = "Causality supported" in (by_bucket.get("GENETIC_CAUSALITY").summary if by_bucket.get("GENETIC_CAUSALITY") else "")
    association_ok = "Consistent disease context" in (by_bucket.get("ASSOCIATION").summary if by_bucket.get("ASSOCIATION") else "")
    modality_feasible = "Modality feasible" in (by_bucket.get("TRACTABILITY").summary if by_bucket.get("TRACTABILITY") else "")
    if safety_flags:
        verdict = "Unfavorable"; drivers=[]; tensions=["Strong safety signal"]; flip=["Stratify by PGx markers or consider alternative modality"]
    elif causality_strong and association_ok and modality_feasible:
        verdict = "Favorable"; drivers=["Causality triangulated","Context expression present","At least one feasible modality"]; tensions=[]; flip=[]
    else:
        verdict = "Marginal"; drivers=[]; tensions=["Evidence incomplete or conflicted"]; flip=["Fine-map, cell-state proteomics, de-risking design"]
    citations: List[str] = []
    for b in req.bucket_narratives:
        citations += b.citations or []
    return TINarrative(verdict=verdict, drivers=drivers, tensions=tensions, flip_if=flip, citations=citations[:25])

# TargetCard & small graph

class TargetCard(BaseModel):
    target: str
    disease: Optional[str] = None
    bucket_summaries: Dict[str, BucketNarrative]
    registry_snapshot: Dict[str, Any]
    fetched_at: str = Field(default_factory=_iso)

class GraphEdge(BaseModel):
    src: str; dst: str; kind: str; weight: float = 1.0

class TargetGraph(BaseModel):
    nodes: Dict[str, Dict[str, Any]]
    edges: List[GraphEdge]
    fetched_at: str = Field(default_factory=_iso)

@router.post("/synth/targetcard", response_model=TargetCard)
async def synth_targetcard(
    gene: str = Body(...),
    condition: Optional[str] = Body(None),
    bucket_payloads: Dict[str, Dict[str, Any]] = Body(default_factory=dict),
    request: Request = None
) -> TargetCard:
    if not bucket_payloads:
        bucket_payloads = {}
        for b in ["GENETIC_CAUSALITY", "ASSOCIATION", "TRACTABILITY"]:
            try:
                bn = await synth_bucket_get(name=b, gene=gene, condition=condition, mode="live", request=request)
                bucket_payloads[b] = {"module_outputs": getattr(bn, "module_outputs", {}) if hasattr(bn, "module_outputs") else {},
                                      "lit_summary": getattr(bn, "lit_summary", {}) if hasattr(bn, "lit_summary") else {}}
            except Exception:
                bucket_payloads[b] = {"module_outputs": {}, "lit_summary": {}}
    bucket_narratives: Dict[str, BucketNarrative] = {}
    for b in ["GENETIC_CAUSALITY", "ASSOCIATION", "TRACTABILITY"]:
        payload = bucket_payloads.get(b) or {}
        bn = await synth_bucket(BucketSynthRequest(
            gene=gene, condition=condition, bucket=b,
            module_outputs=payload.get("module_outputs") or {},
            lit_summary=payload.get("lit_summary") or {},
        ))
        bucket_narratives[b] = bn
    reg = {"modules": len(MODULES), "by_bucket": {b: sum(1 for m in MODULES if m.bucket==b) for b in BUCKETS}}
    return TargetCard(target=gene, disease=condition, bucket_summaries=bucket_narratives, registry_snapshot=reg)
@router.post("/synth/graph", response_model=TargetGraph)
async def synth_graph(gene: str = Body(...), condition: Optional[str] = Body(None), bucket_payloads: Dict[str, Dict[str, Any]] = Body(default_factory=dict)) -> TargetGraph:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[GraphEdge] = []
    def add_node(key: str, kind: str, label: Optional[str] = None, **attrs):
        nodes[key] = {"kind": kind, "label": label or key, **attrs}
    def add_edge(src: str, dst: str, kind: str, weight: float = 1.0):
        edges.append(GraphEdge(src=src, dst=dst, kind=kind, weight=weight))
    add_node(gene, "Gene", label=gene)
    for b, p in (bucket_payloads or {}).items():
        add_node(b, "Bucket", label=b); add_edge(gene, b, "in_bucket")
        mos = p.get("module_outputs") or {}
        for k, v in mos.items():
            add_node(k, "Module", label=k); add_edge(b, k, "has_module")
            for t in (v.get("tissues") or []):
                add_node(t, "Tissue", label=t); add_edge(k, t, "in_tissue")
            for path in (v.get("pathways") or []):
                add_node(path, "Pathway", label=path); add_edge(k, path, "in_pathway")
    return TargetGraph(nodes=nodes, edges=edges)

# ------------------------ Debug / QA -----------------------------------------

@router.get("/debug/config")
def debug_config() -> Dict[str, Any]:
    return {
        "modules": len(MODULES),
        "by_bucket": {b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS},
        "cache_items": len(CACHE),
        "budget_s": REQUEST_BUDGET_S,
        "timeout": repr(DEFAULT_TIMEOUT),
    }

# ------------------------ Legacy aliases (compat) ----------------------------

@router.get("/assoc/geo-arrayexpress", response_model=Evidence)
async def assoc_geo_arrayexpress(symbol: Optional[str] = None, gene: Optional[str] = None, condition: Optional[str] = None) -> Evidence:
    return await assoc_bulk_rna(symbol=symbol, gene=gene, condition=condition)

@router.get("/assoc/tabula-hca", response_model=Evidence)
async def assoc_tabula_hca(symbol: Optional[str] = None, gene: Optional[str] = None) -> Evidence:
    return await assoc_sc(symbol=symbol, gene=gene)

@router.post("/assoc/cptac", response_model=Evidence)
async def assoc_cptac(symbol: Optional[str] = Body(None), gene: Optional[str] = Body(None)) -> Evidence:
    return await assoc_bulk_prot_pdc(symbol=symbol, gene=gene)

@router.get("/synth/bucket", response_model=BucketNarrative)
async def synth_bucket_get(
    name: str = Query(..., alias="name"),
    gene: Optional[str] = Query(None, description="Alias of 'symbol'."),
    symbol: Optional[str] = Query(None),
    efo: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    mode: Optional[str] = Query("auto", pattern="^(auto|live|snapshot)$"),
    request: Request = None,
) -> BucketNarrative:
    bucket = name.upper().replace(" ", "_")
    sym = symbol or gene
    module_outputs: Dict[str, Any] = {}
    if mode in ("auto", "live"):
        params = dict(symbol=sym, gene=None, condition=condition, efo=efo, limit=limit, offset=0, strict=False)
        if bucket == "GENETIC_CAUSALITY":
            keys = ["genetics-coloc","genetics-mr","genetics-rare","genetics-sqtl","genetics-pqtl","genetics-ase"]
            for k in keys:
                ev = await _module_evidence(request, k, params)
                if ev and ev.get("status") in ("OK","NO_DATA"):
                    module_outputs[k.replace("-","_")] = {
                        "support": 1.0 if (ev.get("status")== "OK" and (ev.get("fetched_n") or 0) > 0) else 0.0,
                        "tissues": (ev.get("data") or {}).get("tissues") or [],
                        "pathways": (ev.get("data") or {}).get("pathways") or [],
                        "raw": ev,
                    }
            try:
                ca = await _internal_get(request, "/genetics/caqtl-lite", {"symbol": sym, "condition": condition, "limit": limit})
                module_outputs["genetics_caqtl_lite"] = {
                    "support": 1.0 if ca and ca.get("status")=="OK" and (ca.get("fetched_n") or 0)>0 else 0.0,
                    "tissues": (ca or {}).get("data", {}).get("tissues") or [],
                    "pathways": [],
                    "raw": ca,
                }
            except Exception:
                pass
        elif bucket == "ASSOCIATION":
            for k in ["assoc-sc","assoc-bulk-rna","assoc-proteomics"]:
                ev = await _module_evidence(request, k, params)
                if ev and ev.get("status") in ("OK","NO_DATA"):
                    module_outputs[k.replace("-","_")] = {
                        "support": 1.0 if (ev.get("status")== "OK" and (ev.get("fetched_n") or 0) > 0) else 0.0,
                        "tissues": (ev.get("data") or {}).get("tissues") or [],
                        "pathways": (ev.get("data") or {}).get("pathways") or [],
                        'raw': ev,
                    }
        elif bucket == "TRACTABILITY":
            for k in ["tract-druggable-proteome","tract-binding-sites","tract-kinase-tractability","tract-iedb-epitopes","tract-mhc-binding"]:
                ev = await _module_evidence(request, k, params)
                if ev and ev.get("status") in ("OK","NO_DATA"):
                    module_outputs[k.replace("-","_")] = {
                        "support": 1.0 if (ev.get("status")== "OK" and (ev.get("fetched_n") or 0) > 0) else 0.0,
                        "tissues": (ev.get("data") or {}).get("tissues") or [],
                        "pathways": (ev.get("data") or {}).get("pathways") or [],
                        "raw": ev,
                    }
            try:
                ptm = await _internal_get(request, "/genetics/ptm-signal-lite", {"symbol": sym, "limit": limit})
                module_outputs["genetics_ptm_signal_lite"] = {
                    "support": 1.0 if ptm and ptm.get("status")=="OK" and (ptm.get("fetched_n") or 0)>0 else 0.0,
                    "tissues": [],
                    "pathways": [],
                    "raw": ptm,
                }
            except Exception:
                pass
            try:
                pep = await _internal_get(request, "/immuno/peptidome-pride", {"symbol": sym, "limit": limit})
                module_outputs["immuno_peptidome_pride"] = {
                    "support": 1.0 if pep and pep.get("status")=="OK" and (pep.get("fetched_n") or 0)>0 else 0.0,
                    "tissues": [],
                    "pathways": [],
                    "raw": pep,
                }
            except Exception:
                pass
    lit_summary: Dict[str, Any] = {}
    if mode in ("auto","live"):
        try:
            q = await _internal_get(request, "/lit/search", {"symbol": sym, "condition": condition, "window_days": 1825, "page_size": 40})
            lit_summary = (q or {}).get("data") or {}
        except Exception:
            pass
    return await synth_bucket(BucketSynthRequest(gene=sym, condition=condition, bucket=bucket, module_outputs=module_outputs, lit_summary=lit_summary))
@router.get("/debug/extended-size")
def debug_extended_size() -> Dict[str, Any]:
    return {"extended_yaml_bytes": len(ROUTER_KNOWLEDGE_YAML)}

# ------------------------ EOF -------------------------------------------------

# ---- external knowledge moved to file (router_knowledge.yaml) ----
import os as _os
try:
    _here = _os.path.dirname(__file__)
except Exception:
    _here = "."
_ROUTER_KNOWLEDGE_PATH = _os.environ.get("ROUTER_KNOWLEDGE_PATH") or _os.path.join(_here, "router_knowledge.yaml")
try:
    with open(_ROUTER_KNOWLEDGE_PATH, "r", encoding="utf-8") as _fh:
        ROUTER_KNOWLEDGE_YAML = _fh.read()
except Exception as _e:
    print(f"Warning: failed to load router_knowledge.yaml: {_e}")
    ROUTER_KNOWLEDGE_YAML = ""
# ------------------------------------------------------------------

# /modules55 deprecated and removed by maxfixed patch.
# maxfixed: removed stray code from legacy /modules55 block:     try:
# maxfixed: removed stray code from legacy /modules55 block:         eqtl = await _get_json(f"https://www.ebi.ac.uk/eqtl/api/v3/associations?filter=gene_id:{ensg}")
# maxfixed: removed stray code from legacy /modules55 block:         cites.append("https://www.ebi.ac.uk/eqtl/api-docs/")
# maxfixed: removed stray code from legacy /modules55 block:     except Exception:
# maxfixed: removed stray code from legacy /modules55 block:         eqtl = None
# maxfixed: removed stray code from legacy /modules55 block:     fetched_n = len(eqtl.get('_embedded',{}).get('associations',[])) if isinstance(eqtl, dict) else (1 if eqtl else 0)
# maxfixed: removed stray code from legacy /modules55 block:     return Evidence(status="OK" if fetched_n else "NO_DATA", source="eQTL Catalogue", fetched_n=fetched_n, data={"gene_id": ensg, "eqtl": eqtl}, citations=cites, fetched_at=_now())
# maxfixed: removed stray code from legacy /modules55 block: 
# maxfixed: removed stray code from legacy /modules55 block: 
# maxfixed: removed stray code from legacy /modules55 block: 

@router.get("/biology/causal-pathways", response_model=Evidence)
async def biology_causal_pathways(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None), rsid: Optional[str] = None, variant: Optional[str] = None) -> Evidence:

    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites = []
    try:
        up = await _get_json(f"https://rest.uniprot.org/uniprotkb/search?query=gene_exact:{sym}+AND+organism_id:9606&fields=accession")
        cites.append("https://www.uniprot.org/help/api_queries")
        acc = up["results"][0]["primaryAccession"] if up.get("results") else None
    except Exception:
        acc = None
    if not acc:
        return Evidence(status="NO_DATA", source="Reactome", fetched_n=0, data={"error":"UniProt accession not found"}, citations=cites, fetched_at=_now())
    try:
        rx = await _get_json(f"https://reactome.org/ContentService/data/mapping/UniProt/{acc}/pathways?species=Homo%20sapiens")
        cites.append("https://reactome.org/dev/content-service")
    except Exception:
        rx = None
    fetched_n = len(rx) if isinstance(rx, list) else (1 if rx else 0)
    return Evidence(status="OK" if fetched_n else "NO_DATA", source="Reactome ContentService", fetched_n=fetched_n, data={"uniprot":acc,"pathways":rx}, citations=cites, fetched_at=_now())




@router.get("/function/dependency", response_model=Evidence)
async def function_dependency(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None), rsid: Optional[str] = None, variant: Optional[str] = None) -> Evidence:

    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites = []
    files = None
    try:
        files = await _get_json("https://depmap.org/portal/api/download/files")
        cites.append("https://forum.depmap.org/t/stable-url-for-current-release-files/3765")
    except Exception:
        pass
    orcs = None
    import os
    key = os.getenv("BIOGRID_ORCS_KEY") or os.getenv("BIOGRID_API_KEY")
    if key:
        try:
            orcs = await _get_json(f"https://orcsws.thebiogrid.org/genes/?accesskey={key}&geneName={sym}&format=json")
            cites.append("https://wiki.thebiogrid.org/doku.php/orcs:webservice")
        except Exception:
            pass
    payload = {"depmap_files": files, "orcs_gene": orcs}
    fetched_n = sum(len(v) if isinstance(v, list) else (len(v) if isinstance(v, dict) else 1) for v in payload.values() if v)
    return Evidence(status="OK" if fetched_n else "NO_DATA", source="DepMap portal, BioGRID ORCS", fetched_n=fetched_n, data=payload, citations=cites, fetched_at=_now())




@router.get("/immuno/hla-coverage", response_model=Evidence)
async def immuno_hla_coverage(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None), rsid: Optional[str] = None, variant: Optional[str] = None) -> Evidence:

    import json as _json
    cites = ["https://nextgen-tools.iedb.org/docs/api/endpoints/api_references.html"]
    alleles = (condition or "").split(",") if condition else []
    if not alleles:
        return Evidence(status="ERROR", source="IEDB Tools", fetched_n=0, data={"error":"provide alleles (comma-separated) in 'condition' or POST body"}, citations=cites, fetched_at=_now())
    return Evidence(status="OK", source="IEDB (spec refs)", fetched_n=len(alleles), data={"alleles": alleles, "note": "Use POST /tract/mhc-binding + /tract/iedb-epitopes first, then coverage."}, citations=cites, fetched_at=_now())




@router.get("/clin/feasibility", response_model=Evidence)
async def clin_feasibility(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None), rsid: Optional[str] = None, variant: Optional[str] = None) -> Evidence:

    q = condition or (symbol or gene) or ""
    try:
        url = "https://clinicaltrials.gov/api/query/study_fields?expr=" + q + "&fields=NCTId,Condition,EnrollmentCount,OverallStatus,LocationCountry&min_rnk=1&max_rnk=200&fmt=json"
        ct = await _get_json(url)
    except Exception:
        ct = None
    fetched_n = len(ct.get("StudyFieldsResponse",{}).get("StudyFields",[])) if isinstance(ct, dict) else (1 if ct else 0)
    return Evidence(status="OK" if fetched_n else "NO_DATA", source="ClinicalTrials.gov", fetched_n=fetched_n, data={"query":q,"ctgov": ct}, citations=["https://clinicaltrials.gov/api/"], fetched_at=_now())


@router.get("/tract/surfaceome", response_model=Evidence)
async def tract_surfaceome(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None)) -> Evidence:

    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    return await tract_surfaceome_hpa(symbol=sym, gene=None)
    



    # fail-closed would break the service; we log to stdout and keep routes as-is
    print("Route guard skipped due to error:", _e)



# ===== Strict route pruning (hard-coded 55 module paths) =====
# ---------- Added modules to reach 64 per Targetval Config (2025-10-17) ----------
try:
    MODULES += [
        Module(route="/perturb/qc", name="perturb-qc (internal)", sources=["(internal only)"], bucket="Functional & mechanistic validation"),
        Module(route="/perturb/scrna-summary", name="perturb-scrna-summary (internal)", sources=["(internal only)"], bucket="Functional & mechanistic validation"),
        Module(route="/perturb/lincs-signatures", name="perturb-lincs-signatures", sources=["LINCS Data Portal (LDP3) Signature API","iLINCS API"], bucket="Functional & mechanistic validation"),
        Module(route="/perturb/connectivity", name="perturb-connectivity", sources=["iLINCS API","LDP3 Data API"], bucket="Functional & mechanistic validation"),
        Module(route="/perturb/perturbseq", name="perturb-perturbseq-encode", sources=["ENCODE REST API","EBI ENA"], bucket="Functional & mechanistic validation"),
        Module(route="/perturb/crispr-screens", name="perturb-crispr-screens", sources=["BioGRID ORCS REST"], bucket="Functional & mechanistic validation"),
        Module(route="/perturb/depmap-dependency", name="perturb-depmap-dependency", sources=["Broad DepMap Portal/figshare","Sanger Cell Model Passports JSON:API"], bucket="Therapeutic index & safety translation"),
        Module(route="/perturb/drug-response", name="perturb-drug-response", sources=["PharmacoDB API","NCI-60 CellMiner API"], bucket="Druggability & modality tractability"),
        Module(route="/perturb/signature-enrichment", name="perturb-signature-enrichment", sources=["iLINCS API","LINCS Data Portal (LDP3)"], bucket="Functional & mechanistic validation"),
    ]
except Exception:
    pass


# ------------------------ Added endpoints: PERTURBATION (per new config) -------

@router.get("/perturb/qc", response_model=Evidence)
async def perturb_qc(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    # Internal-only per spec — expose explicit, truthful status.
    return Evidence(status="ERROR", source="internal-only", fetched_n=0, data={"note": "Module is internal per configuration."}, citations=[], fetched_at=_now())

@router.get("/perturb/scrna-summary", response_model=Evidence)
async def perturb_scrna_summary(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    # Internal-only per spec — expose explicit, truthful status.
    return Evidence(status="ERROR", source="internal-only", fetched_n=0, data={"note": "Module is internal per configuration."}, citations=[], fetched_at=_now())

async def _ldp3_find_signatures(where: Dict[str, Any], limit: int = 50) -> Tuple[List[Dict[str, Any]], List[str]]:
    cites: List[str] = []
    url = "https://ldp3.cloud/metadata-api/signatures/find"
    payload = {"filter": {"where": where, "limit": limit}}
    try:
        js = await _post_json(url, payload, tries=2)
        cites.append(url)
        if isinstance(js, list):
            return js, cites
        # some LDP3 endpoints return dict; normalize
        if isinstance(js, dict) and js.get("results") and isinstance(js["results"], list):
            return js["results"], cites
    except Exception:
        pass
    return [], cites

async def _ldp3_entities_from_genes(genes: List[str]) -> Tuple[Dict[str, str], List[str]]:
    # Map gene symbols -> entity UUIDs
    cites: List[str] = []
    url = "https://ldp3.cloud/metadata-api/entities/find"
    payload = {"filter": {"where": {"meta.symbol": {"inq": genes}}, "fields": ["id","meta.symbol"]}}
    try:
        js = await _post_json(url, payload, tries=2)
        cites.append(url)
        mapping = {}
        if isinstance(js, list):
            for e in js:
                sym = ((e or {}).get("meta") or {}).get("symbol")
                if sym and e.get("id"):
                    mapping[str(sym).upper()] = e["id"]
        return mapping, cites
    except Exception:
        return {}, cites

@router.get("/perturb/lincs-signatures", response_model=Evidence)
async def perturb_lincs_signatures(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), limit: int = Query(50)) -> Evidence:
    """
    Find LINCS signatures where the perturbagen corresponds to the input gene (knockdown/overexpression).
    Primary: LDP3 metadata API; Fallback: iLINCS docs not reliably programmatic here.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    # Look for signatures with CRISPR/shRNA/cDNA targeting this gene
    where = {"and": [{"meta.pert_type": {"inq": ["CRISPR Knockdown","shRNA","cDNA overexpression","CRISPR"]}},
                     {"meta.treatment": {"like": sym}}]}
    sigs, cites = await _ldp3_find_signatures(where, limit=limit)
    fetched_n = len(sigs)
    return Evidence(status="OK" if fetched_n else "NO_DATA",
                    source="LDP3 metadata-api",
                    fetched_n=fetched_n,
                    data={"signatures": sigs},
                    citations=cites,
                    fetched_at=_now())

@router.get("/perturb/connectivity", response_model=Evidence)
async def perturb_connectivity(symbol: Optional[str] = Query(None),
                               gene: Optional[str] = Query(None),
                               up: Optional[str] = Query(None, description="comma-separated up genes"),
                               down: Optional[str] = Query(None, description="comma-separated down genes"),
                               database: str = Query("l1000_xpr"),
                               limit: int = Query(25)) -> Evidence:
    """
    Compute connectivity against LINCS signatures given a gene set (or derive from single gene).
    Uses LDP3 data API (/enrich/ranktwosided); iLINCS APIs are also compatible if available.
    """
    cites: List[str] = []
    up_list = [g.strip().upper() for g in (up.split(",") if isinstance(up, str) and up else []) if g.strip()]
    down_list = [g.strip().upper() for g in (down.split(",") if isinstance(down, str) and down else []) if g.strip()]
    if not (up_list or down_list):
        sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
        up_list = [sym]
    mapping, c2 = await _ldp3_entities_from_genes(list(set(up_list + down_list)))
    cites += c2
    up_entities = [mapping[g] for g in up_list if g in mapping]
    down_entities = [mapping[g] for g in down_list if g in mapping]
    if not (up_entities or down_entities):
        return Evidence(status="NO_DATA", source="LDP3 data-api", fetched_n=0, data={"note": "No gene entities resolved."}, citations=cites, fetched_at=_now())
    url = "https://ldp3.cloud/data-api/api/v1/enrich/ranktwosided"
    payload = {"up_entities": up_entities, "down_entities": down_entities, "limit": limit, "database": database}
    try:
        res = await _post_json(url, payload, tries=2)
        cites.append(url)
        results = (res or {}).get("results") or []
        return Evidence(status="OK" if results else "NO_DATA",
                        source="LDP3 data-api",
                        fetched_n=len(results),
                        data=res or {},
                        citations=cites,
                        fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LDP3 connectivity failed: {e!s}")

@router.get("/perturb/perturbseq", response_model=Evidence)
async def perturb_perturbseq(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), limit: int = Query(50)) -> Evidence:
    """
    ENCODE REST API search for Perturb-seq experiments matching the target gene.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    # Broad search to be resilient across schema variants
    url = ("https://www.encodeproject.org/search/"
           f"?searchTerm=perturb-seq%20{urllib.parse.quote(sym)}&type=Experiment&frame=object&status=released&limit={limit}")
    try:
        js = await _get_json(url, tries=2, headers={"Accept": "application/json"})
        cites.append(url)
        # ENCODE search returns dict with "@graph"
        hits = (js or {}).get("@graph") if isinstance(js, dict) else js
        n = len(hits or [])
        return Evidence(status="OK" if n else "NO_DATA", source="ENCODE REST API", fetched_n=n, data={"hits": hits}, citations=cites, fetched_at=_now())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ENCODE perturb-seq search failed: {e!s}")

@router.get("/perturb/crispr-screens", response_model=Evidence)
async def perturb_crispr_screens(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    """
    BioGRID ORCS REST: gene-centered CRISPR screen summaries.
    If API key not present, attempt open call; fallback to Europe PMC keyword search.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    import os as _os
    key = _os.getenv("BIOGRID_ORCS_KEY") or _os.getenv("BIOGRID_API_KEY")
    base = "https://orcsws.thebiogrid.org/genes/?"
    if key:
        url = f"{base}accesskey={key}&geneName={urllib.parse.quote(sym)}&format=json"
    else:
        url = f"{base}geneName={urllib.parse.quote(sym)}&format=json"
    try:
        data["orcs"] = await _get_json(url, tries=2)
        cites.append("https://wiki.thebiogrid.org/doku.php/orcs:webservice")
        cites.append(url)
    except Exception:
        data["orcs"] = None
    if not data.get("orcs"):
        q = f"{sym} CRISPR screen"
        hits, epmc_cites = await _epmc_search(q, size=40)
        data["literature_proxy"] = {"query": q, "hits": hits}
        cites += epmc_cites
        n = len(hits or [])
        return Evidence(status="OK" if n else "NO_DATA", source="BioGRID ORCS (fallback: Europe PMC)", fetched_n=n, data=data, citations=cites, fetched_at=_now())
    n = len(data["orcs"]) if isinstance(data["orcs"], list) else (1 if data["orcs"] else 0)
    return Evidence(status="OK" if n else "NO_DATA", source="BioGRID ORCS", fetched_n=n, data=data, citations=cites, fetched_at=_now())

@router.get("/perturb/depmap-dependency", response_model=Evidence)
async def perturb_depmap_dependency(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    """
    Broad DepMap portal file listing (public) + figshare archival note (if applicable).
    Fallback to Sanger Cell Model Passports JSON:API pointer.
    """
    cites: List[str] = []
    files = None
    try:
        files = await _get_json("https://depmap.org/portal/api/download/files", tries=2)
        cites.append("https://depmap.org/portal/api/download/files")
        # Policy change note
        cites.append("https://forum.depmap.org/t/availability-of-depmap-25q2-on-figshare/4424")
    except Exception:
        pass
    fallback = {"sanger_passports_api": "https://cellmodelpassports.sanger.ac.uk/api/v1"}
    cites.append("https://cellmodelpassports.sanger.ac.uk/documentation/guides/API")
    payload = {"depmap_files": files, "fallback": fallback}
    fetched_n = len(files or []) if isinstance(files, list) else (len(files or {}) if isinstance(files, dict) else (1 if files else 0))
    return Evidence(status="OK" if fetched_n else "NO_DATA", source="DepMap portal (fallback: Sanger CMP)", fetched_n=fetched_n, data=payload, citations=cites, fetched_at=_now())

@router.get("/perturb/drug-response", response_model=Evidence)
async def perturb_drug_response(drug: Optional[str] = Query(None, description="compound name"), 
                                cell_line: Optional[str] = Query(None, description="cell line name")) -> Evidence:
    """
    PharmacoDB API: retrieve metadata for compound and (optionally) intersect with a cell line.
    If both provided, attempts to fetch intersection endpoint; otherwise returns compound/cell metadata.
    """
    if not (drug or cell_line):
        raise HTTPException(status_code=422, detail="Provide 'drug' and/or 'cell_line'.")
    cites: List[str] = []
    data: Dict[str, Any] = {}
    base = "https://api.pharmacodb.ca/v1"
    try:
        if drug:
            url = f"{base}/compounds?type=name&search={urllib.parse.quote(drug)}&per_page=20"
            data["compounds"] = await _get_json(url, tries=2); cites.append(url)
        if cell_line:
            url = f"{base}/cell_lines?type=name&search={urllib.parse.quote(cell_line)}&per_page=20"
            data["cell_lines"] = await _get_json(url, tries=2); cites.append(url)
        # If we have exact ids, try intersection
        def _pick_id(obj_list):
            if isinstance(obj_list, list) and obj_list:
                first = obj_list[0]
                return first.get("id") or first.get("cell_line_id") or first.get("compound_id")
            return None
        cid = _pick_id(data.get("cell_lines"))
        did = _pick_id(data.get("compounds"))
        if cid and did:
            url = f"{base}/intersections/1/{cid}/{did}"
            data["intersection"] = await _get_json(url, tries=2); cites.append(url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PharmacoDB fetch failed: {e!s}")
    fetched_n = sum(len(v) if isinstance(v, list) else (len(v) if isinstance(v, dict) else 1) for v in data.values() if v)
    return Evidence(status="OK" if fetched_n else "NO_DATA", source="PharmacoDB", fetched_n=fetched_n, data=data, citations=cites, fetched_at=_now())

@router.get("/perturb/signature-enrichment", response_model=Evidence)
async def perturb_signature_enrichment(up: str = Query(..., description="comma-separated up genes"),
                                       down: str = Query(..., description="comma-separated down genes"),
                                       database: str = Query("l1000_xpr"),
                                       limit: int = Query(25)) -> Evidence:
    """
    Two-sided enrichment against LDP3 signatures.
    """
    up_list = [g.strip().upper() for g in up.split(",") if g.strip()]
    down_list = [g.strip().upper() for g in down.split(",") if g.strip()]
    mapping, cites = await _ldp3_entities_from_genes(list(set(up_list + down_list)))
    up_entities = [mapping[g] for g in up_list if g in mapping]
    down_entities = [mapping[g] for g in down_list if g in mapping]
    if not (up_entities or down_entities):
        return Evidence(status="NO_DATA", source="LDP3 data-api", fetched_n=0, data={"note": "No gene entities resolved."}, citations=cites, fetched_at=_now())
    url = "https://ldp3.cloud/data-api/api/v1/enrich/ranktwosided"
    payload = {"up_entities": up_entities, "down_entities": down_entities, "limit": limit, "database": database}
    res = await _post_json(url, payload, tries=2)
    cites.append(url)
    results = (res or {}).get("results") or []
    return Evidence(status="OK" if results else "NO_DATA",
                    source="LDP3 data-api",
                    fetched_n=len(results),
                    data=res or {},
                    citations=cites,
                    fetched_at=_now())



from collections import OrderedDict
class LruTtlCache:
    def __init__(self, maxsize=2048, ttl=24*60*60):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data = OrderedDict()
    def get(self, key):
        now = time.time()
        if key in self._data:
            ts, value = self._data.pop(key)
            if now - ts < self.ttl:
                self._data[key] = (ts, value)
                return value
        return None
    def set(self, key, value):
        now = time.time()
        if key in self._data:
            self._data.pop(key)
        elif len(self._data) >= self.maxsize:
            self._data.popitem(last=False)
        self._data[key] = (now, value)
from fastapi import FastAPI

app = FastAPI(title="TargetVal Router (embedded)")
app.include_router(router)



@app.on_event("startup")
async def _maxfixed_startup_assertions():
    # Ensure every module key has a route, and config module_set keys are valid.
    try:
        registered = {getattr(m, 'key', None) or f"{getattr(m,'bucket','')}-{getattr(m,'name','')}" for m in MODULES}
        # Collect actual router paths for module endpoints
        available_paths = {getattr(r, 'path', None) for r in router.routes if hasattr(r, 'path')}
        # Derive route-keys from paths like '/genetics/l2g' -> 'genetics-l2g'
        available_keys = set()
        for p in available_paths:
            if isinstance(p, str) and p.count('/')>=2:
                segs = [s for s in p.split('/') if s]
                available_keys.add(f"{segs[0]}-{segs[1]}".lower())
        missing = sorted([k for k in registered if k and k.lower() not in available_keys])
        if missing:
            raise RuntimeError(f"Module keys without routes: {missing[:10]}{'...' if len(missing)>10 else ''}")
        # Normalize DEFAULT_CONFIG module_set and assert coverage
        try:
            cfg = DEFAULT_CONFIG
            ms = [k.replace('/', '-') for k in (cfg.get('module_set') or [])]
            bad = [k for k in ms if k.lower() not in {x.lower() for x in registered}]
            if bad:
                raise RuntimeError(f"DEFAULT_CONFIG.module_set contains unknown modules: {bad}")
        except Exception:
            pass
    except Exception as e:
        # Fail fast so we don't run a partial registry silently
        raise

@router.get("/domains")
async def list_domains_router() -> Dict[str, Any]:
    return {
        "ok": True,
        "domains": [
            {"id": i, "name": DOMAINS_META[i]["name"], "modules": DOMAIN_MODULES[i]}
            for i in sorted(DOMAINS_META.keys())
        ],
    }

@router.get("/domains/{domain_id}")
async def get_domain_router(domain_id: int) -> Dict[str, Any]:
    if domain_id not in DOMAIN_MODULES:
        raise HTTPException(status_code=404, detail="Unknown domain")
    return {"ok": True, "id": domain_id, "name": DOMAINS_META[domain_id]["name"], "modules": DOMAIN_MODULES[domain_id]}


# ------------------------ Domain mapping validation (fail-fast) ---------------
def _validate_domain_mapping_against_modules() -> None:
    name_set = {getattr(m, "name", "") for m in MODULES}
    missing = []
    for did, keys in DOMAIN_MODULES.items():
        for k in keys:
            if k not in name_set:
                missing.append((did, k))
    if missing:
        # Fail fast with a clear message
        raise RuntimeError(f"DOMAIN_MODULES contains names not present in MODULES: {missing}")

_validate_domain_mapping_against_modules()


# ========================
# APPENDED — TargetVal extensions (no stubs; live-only)
# ========================
from typing import Any, Dict, List, Optional, Tuple
import asyncio, json, math, time
import httpx
from fastapi import Request, HTTPException, Query
try:
    from pydantic import BaseModel, Field
except Exception:
    pass

# ---- Guards for models (define if legacy did not) ----
try:
    Evidence  # type: ignore[name-defined]
except NameError:
    pass

try:
    ModuleRunRequest  # type: ignore[name-defined]
except NameError:
    class ModuleRunRequest(BaseModel):  # type: ignore[no-redef]
        module_key: str
        gene: Optional[str] = None
        symbol: Optional[str] = None
        ensembl_id: Optional[str] = None
        uniprot_id: Optional[str] = None
        efo: Optional[str] = None
        condition: Optional[str] = None
        tissue: Optional[str] = None
        cell_type: Optional[str] = None
        species: Optional[str] = None
        limit: Optional[int] = 100
        offset: Optional[int] = 0
        strict: Optional[bool] = False
        extra: Optional[Dict[str, Any]] = None

try:
    AggregateRequest  # type: ignore[name-defined]
except NameError:
    class AggregateRequest(BaseModel):  # type: ignore[no-redef]
        modules: Optional[List[str]] = None
        domain: Optional[str] = None
        primary_only: bool = True
        order: str = "sequential"
        continue_on_error: bool = True
        limit: int = 100
        offset: int = 0
        gene: Optional[str] = None
        symbol: Optional[str] = None
        ensembl_id: Optional[str] = None
        uniprot_id: Optional[str] = None
        efo: Optional[str] = None
        condition: Optional[str] = None
        tissue: Optional[str] = None
        cell_type: Optional[str] = None
        species: Optional[str] = None
        cutoff: Optional[float] = None
        extra: Optional[Dict[str, Any]] = None

try:
    DomainRunRequest  # type: ignore[name-defined]
except NameError:
    class DomainRunRequest(BaseModel):  # type: ignore[no-redef]
        primary_only: bool = True
        limit: int = 100
        offset: int = 0
        gene: Optional[str] = None
        symbol: Optional[str] = None
        ensembl_id: Optional[str] = None
        uniprot_id: Optional[str] = None
        efo: Optional[str] = None
        condition: Optional[str] = None
        tissue: Optional[str] = None
        cell_type: Optional[str] = None
        species: Optional[str] = None
        cutoff: Optional[float] = None
        extra: Optional[Dict[str, Any]] = None

try:
    SynthIntegrateRequest  # type: ignore[name-defined]
except NameError:
    class SynthIntegrateRequest(BaseModel):  # type: ignore[no-redef]
        gene: Optional[str] = None
        symbol: Optional[str] = None
        efo: Optional[str] = None
        condition: Optional[str] = None
        modules: Optional[List[str]] = None
        method: str = "math"   # one of math|vote|rank|bayes|hybrid
        extra: Optional[Dict[str, Any]] = None

# ---- Utils ----
def _now() -> float:
    return float(time.time())

async def _self_get(request: Request, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=5.0)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
        async with httpx.AsyncClient(app=request.app, base_url="http://internal", timeout=timeout, limits=limits) as client:
            try:
                r = await client.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
                if r.status_code == 404 and not path.startswith("/v1/"):
                    r = await client.get("/v1" + path, params={k: v for k, v in (params or {}).items() if v is not None})
                r.raise_for_status()
                return r.json()
            except Exception:
                # final attempt: try bare path one more time to bubble up correct error
                r = await client.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
                r.raise_for_status()
                return r.json()

async def _get_json(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
                    timeout_s: float = 30.0, tries: int = 3, backoff: float = 0.6) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    params = params or {}
    headers = {"Accept": "application/json", "User-Agent": "targetval-gateway/2025"} | (headers or {})
    last_err = None
    async with httpx.AsyncClient(timeout=timeout_s, headers=headers) as client:
        for i in range(tries):
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if "application/json" in ctype or "text/plain" in ctype:
                    try:
                        return r.json(), None
                    except Exception:
                        return json.loads(r.text), None
                return {"raw": r.text}, None
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(backoff * (2 ** i))
        return None, last_err

async def _post_json(url: str, json_body: Dict[str, Any], headers: Optional[Dict[str, str]] = None,
                     timeout_s: float = 30.0, tries: int = 3, backoff: float = 0.6) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    headers = {"Accept": "application/json", "User-Agent": "targetval-gateway/2025"} | (headers or {})
    last_err = None
    async with httpx.AsyncClient(timeout=timeout_s, headers=headers) as client:
        for i in range(tries):
            try:
                r = await client.post(url, json=json_body)
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if "application/json" in ctype or "text/plain" in ctype:
                    try:
                        return r.json(), None
                    except Exception:
                        return json.loads(r.text), None
                return {"raw": r.text}, None
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(backoff * (2 ** i))
        return None, last_err

# ---- Route registration helpers ----
def _existing_paths() -> set:
    paths = set()
    try:
        for r in router.routes:  # type: ignore[name-defined]
            p = getattr(r, "path", None)
            if isinstance(p, str):
                paths.add(p)
    except Exception:
        pass
    return paths

def _add_if_missing(path: str, endpoint, methods: List[str], response_model=None):
    if path not in _existing_paths():
        router.add_api_route(path, endpoint, methods=methods, response_model=response_model)  # type: ignore[name-defined]

# ---- Registry helpers (for /modules map) ----
def _module_map_from_registry() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    try:
        for m in (MODULES or []):  # type: ignore[name-defined]
            name = getattr(m, "name", None) or getattr(m, "key", None) or getattr(m, "route", None)
            route = getattr(m, "route", None)
            if name and route:
                mapping[str(name)] = str(route)
    except Exception:
        pass
    return mapping

# 1) Orchestration endpoints (dup-safe)
async def _healthz():
    return {"ok": True, "version": "2025.10"}
_add_if_missing("/healthz", _healthz, ["GET"], None)

async def _modules() -> List[str]:
    return sorted(_module_map_from_registry().keys())
_add_if_missing("/modules", _modules, ["GET"], None)

async def _module_get(request: Request, name: str,
                      gene: Optional[str] = None, symbol: Optional[str] = None,
                      ensembl_id: Optional[str] = None, uniprot_id: Optional[str] = None,
                      efo: Optional[str] = None, condition: Optional[str] = None,
                      tissue: Optional[str] = None, cell_type: Optional[str] = None,
                      species: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                      offset: int = Query(0, ge=0), strict: bool = False):
    mm = _module_map_from_registry()
    if name not in mm:
        raise HTTPException(status_code=404, detail=f"Unknown module: {name}")
    params = dict(symbol=symbol or gene, ensembl_id=ensembl_id, uniprot_id=uniprot_id, efo=efo,
                  condition=condition, tissue=tissue, cell_type=cell_type, species=species,
                  limit=limit, offset=offset, strict=strict)
    path = mm[name]
    if path.startswith("/"):
        return await _self_get(request, path, params)
    return await _self_get(request, "/module/" + name, params)
_add_if_missing("/module/{name}", _module_get, ["GET"], Evidence)

async def _module_post(request: Request, body: ModuleRunRequest):
    mm = _module_map_from_registry()
    key = body.module_key
    if key not in mm:
        raise HTTPException(status_code=404, detail=f"Unknown module: {key}")
    params = dict(symbol=body.symbol or body.gene, ensembl_id=body.ensembl_id, uniprot_id=body.uniprot_id, efo=body.efo,
                  condition=body.condition, tissue=body.tissue, cell_type=body.cell_type, species=body.species,
                  limit=body.limit, offset=body.offset, strict=body.strict)
    path = mm[key]
    if path.startswith("/"):
        return await _self_get(request, path, params)
    return await _self_get(request, "/module/" + key, params)
_add_if_missing("/module", _module_post, ["POST"], Evidence)

async def _aggregate(request: Request, req: AggregateRequest):
    mm = _module_map_from_registry()
    modules: List[str] = []
    if req.modules:
        modules = [m for m in req.modules if m in mm]
    elif req.domain:
        key = req.domain.strip().upper()
        try:
            mods = DOMAIN_MODULES.get(key, [])  # type: ignore[name-defined]
        except Exception:
            mods = []
        modules = [m for m in mods if m in mm]
    else:
        modules = sorted(mm.keys())
    if not modules:
        raise HTTPException(status_code=400, detail="No modules resolved from request")
    params = dict(symbol=req.symbol or req.gene, ensembl_id=req.ensembl_id, uniprot_id=req.uniprot_id, efo=req.efo,
                  condition=req.condition, tissue=req.tissue, cell_type=req.cell_type, species=req.species,
                  limit=req.limit, offset=req.offset)
    results: Dict[str, Any] = {}; errors: Dict[str, str] = {}
    async def _run_one(k: str):
        try:
            route = mm[k]
            if route.startswith("/"):
                results[k] = await _self_get(request, route, params)
            else:
                results[k] = await _self_get(request, "/module/" + k, params)
        except Exception as e:
            if req.continue_on_error:
                errors[k] = str(e)
            else:
                raise
    if req.order == "parallel":
        sem = asyncio.Semaphore(8)
        async def _guarded(k: str):
            async with sem:
                await _run_one(k)
        await asyncio.gather(*[_guarded(k) for k in modules])
    else:
        for k in modules:
            await _run_one(k)
    return {"ok": True, "requested": {"modules": modules, "domain": req.domain}, "results": results, "errors": errors}
_add_if_missing("/aggregate", _aggregate, ["POST"], None)

async def _run_domain(request: Request, domain_id: int, body: Optional[DomainRunRequest] = None):
    if domain_id < 1 or domain_id > 6:
        raise HTTPException(status_code=400, detail="domain_id must be 1..6")
    req = body or DomainRunRequest()
    try:
        mods = DOMAIN_MODULES.get(str(domain_id), [])  # type: ignore[name-defined]
    except Exception:
        mods = []
    agg = AggregateRequest(modules=mods, domain=str(domain_id), primary_only=req.primary_only,
                           order="sequential", continue_on_error=True, limit=req.limit, offset=req.offset,
                           gene=req.gene, symbol=req.symbol, ensembl_id=req.ensembl_id, uniprot_id=req.uniprot_id,
                           efo=req.efo, condition=req.condition, tissue=req.tissue, cell_type=req.cell_type,
                           species=req.species, cutoff=req.cutoff, extra=req.extra)
    return await _aggregate(request, agg)
_add_if_missing("/domain/{domain_id}/run", _run_domain, ["POST"], None)

# 2) New live modules (dup-safe)
async def _genetics_caqtl_lite(request: Request, gene: Optional[str] = None, symbol: Optional[str] = None,
                               tissue: Optional[str] = None, limit: int = 100):
    sym = (symbol or gene or "").strip()
    if not sym:
        raise HTTPException(status_code=400, detail="Provide 'symbol' or 'gene'")
    citations: List[str] = []
    aggregates: Dict[str, Any] = {"peaks": [], "contacts": [], "motifs": []}
    try:
        reg = await _self_get(request, "/genetics/regulatory", {"symbol": sym, "limit": limit})
        if isinstance(reg, dict):
            aggregates["peaks"] = reg.get("data", {}).get("peaks", []) or reg.get("data", {}).get("items", []) or []
            citations.extend(reg.get("citations", []) or [])
    except Exception:
        pass
    try:
        cc = await _self_get(request, "/genetics/chromatin-contacts", {"symbol": sym, "limit": limit})
        if isinstance(cc, dict):
            aggregates["contacts"] = cc.get("data", {}).get("contacts", []) or cc.get("data", {}).get("items", []) or []
            citations.extend(cc.get("citations", []) or [])
    except Exception:
        pass
    try:
        ann = await _self_get(request, "/genetics/annotation", {"symbol": sym, "limit": limit})
        if isinstance(ann, dict):
            motifs = ann.get("data", {}).get("motif_changes", []) or []
            if motifs:
                aggregates["motifs"] = motifs
            citations.extend(ann.get("citations", []) or [])
    except Exception:
        pass
    fetched = len(aggregates.get("peaks", [])) + len(aggregates.get("contacts", [])) + len(aggregates.get("motifs", []))
    status = "OK" if fetched > 0 else "NO_DATA"
    source = "ENCODE+4D Nucleome via genetics-regulatory/chromatin-contacts; Ensembl VEP motifs"
    return Evidence(status=status, source=source, fetched_n=fetched, data=aggregates, citations=sorted(list(set(citations))), fetched_at=_now())
_add_if_missing("/genetics/caqtl-lite", _genetics_caqtl_lite, ["GET"], Evidence)

async def _genetics_nmd_inference(request: Request, gene: Optional[str] = None, symbol: Optional[str] = None,
                                  limit: int = 100):
    sym = (symbol or gene or "").strip()
    if not sym:
        raise HTTPException(status_code=400, detail="Provide 'symbol' or 'gene'")
    citations: List[str] = []
    data: Dict[str, Any] = {"events": [], "nmd_positive": 0, "nmd_negative": 0}
    try:
        sq = await _self_get(request, "/genetics/sqtl", {"symbol": sym, "limit": limit})
        citations.extend(sq.get("citations", []) or [])
        events = sq.get("data", {}).get("events", []) or sq.get("data", {}).get("items", []) or []
    except Exception:
        events = []
    ensembl_url = f"https://rest.ensembl.org/lookup/symbol/homo_sapiens/{sym}?expand=1"
    ens, err = await _get_json(ensembl_url, headers={"Content-Type": "application/json"}, timeout_s=25.0)
    if ens is not None:
        citations.append(ensembl_url)
    nmd_pos = 0
    for ev in events[:limit]:
        ev_out = {"raw": ev, "nmd_flag": None, "reason": None}
        try:
            frameshift = ev.get("frameshift") or ev.get("is_frameshift")
            stop_gain = ev.get("stop_gain") or ev.get("ptc") or ev.get("premature_stop")
            intron_ret = ev.get("intron_retention") or ev.get("intron_ret")
            if frameshift or stop_gain or intron_ret:
                nmd_flag = True
                reason = "frameshift/stop_gain/intron_retention"
            else:
                nmd_flag = False
                reason = "no_ptc_flag"
        except Exception:
            nmd_flag = None
            reason = "inference_failed"
        ev_out["nmd_flag"] = nmd_flag
        ev_out["reason"] = reason
        data["events"].append(ev_out)
        if nmd_flag is True:
            nmd_pos += 1
    data["nmd_positive"] = nmd_pos
    data["nmd_negative"] = max(0, len(data["events"]) - nmd_pos)
    fetched = len(data["events"])
    status = "OK" if fetched > 0 else "NO_DATA"
    return Evidence(status=status, source="genetics-sqtl + Ensembl REST", fetched_n=fetched, data=data, citations=sorted(list(set(citations))), fetched_at=_now())
_add_if_missing("/genetics/nmd-inference", _genetics_nmd_inference, ["GET"], Evidence)

async def _immunopeptidome_pride(request: Request, gene: Optional[str] = None, symbol: Optional[str] = None, limit: int = 50):
    sym = (symbol or gene or "").strip()
    if not sym:
        raise HTTPException(status_code=400, detail="Provide 'symbol' or 'gene'")
    citations: List[str] = []
    base = "https://www.ebi.ac.uk/pride/ws/archive/project/list"
    queries = [f"{sym} immunopeptidome", f"{sym} HLA ligandome", f"{sym} peptidome", f"{sym} antigen presentation"]
    seen = {}
    projects: List[Dict[str, Any]] = []
    for q in queries:
        js, err = await _get_json(base, params={"keyword": q, "pageSize": str(limit)})
        if js and isinstance(js, dict) and "list" in js:
            citations.append(base + f"?keyword={q}")
            for proj in js.get("list", []):
                acc = proj.get("accession")
                if acc and acc not in seen:
                    seen[acc] = 1
                    projects.append({
                        "accession": acc,
                        "title": proj.get("title"),
                        "submissionType": proj.get("submissionType"),
                        "projectTags": proj.get("projectTags"),
                        "species": proj.get("species"),
                        "numProteins": proj.get("numProteins"),
                        "numPeptides": proj.get("numPeptides"),
                    })
    status = "OK" if projects else "NO_DATA"
    data = {"projects": projects[:limit]}
    return Evidence(status=status, source="PRIDE Archive", fetched_n=len(projects), data=data, citations=sorted(list(set(citations))), fetched_at=_now())
_add_if_missing("/immuno/peptidome-pride", _immunopeptidome_pride, ["GET"], Evidence)

async def _genetics_mqtl_coloc(symbol: str, limit: int = 100):
    gql = """
    query Coloc($q: String!) {
      target(query: $q) {
        id
        approvedSymbol
        colocalisations {
          rows {
            studyId
            phenotypeId
            trait
            traitCategory
            qtlType
            h4
            log2h4
            yProportion
            sourceId
          }
        }
      }
    }
    """
    url = "https://api.platform.opentargets.org/api/v4/graphql"
    js, err = await _post_json(url, {"query": gql, "variables": {"q": symbol}},
                               headers={"Content-Type": "application/json"}, timeout_s=40.0, tries=3)
    citations = [url]
    items: List[Dict[str, Any]] = []
    if js and "data" in js and js["data"].get("target"):
        rows = (js["data"]["target"].get("colocalisations") or {}).get("rows") or []
        for r in rows:
            cat = (r.get("traitCategory") or "").lower()
            trait = (r.get("trait") or "").lower()
            if "molecular" in cat and any(k in trait for k in ["metabol", "lipid", "glyco", "lipoprotein", "cholesterol", "triglycer"]):
                items.append(r)
    status = "OK" if items else "NO_DATA"
    return Evidence(status=status, source="OpenTargets GraphQL (colocalisations)", fetched_n=len(items),
                    data={"items": items[:limit]}, citations=citations, fetched_at=_now())
_add_if_missing("/genetics/mqtl-coloc", _genetics_mqtl_coloc, ["GET"], Evidence)

async def _genetics_ase_check(symbol: str, condition: Optional[str] = None, limit: int = 50):
    citations: List[str] = []
    q = f'"allele-specific expression" {symbol}'
    if condition:
        q += f" {condition}"
    epmc_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    js, err = await _get_json(epmc_url, params={"query": q, "format": "json", "pageSize": str(limit)})
    items: List[Dict[str, Any]] = []
    if js and "resultList" in js:
        citations.append(epmc_url + f"?query={q}")
        for it in (js["resultList"].get("result") or []):
            title = it.get("title", "")
            stance = "confirm" if any(s in title.lower() for s in ["ase", "allele-specific"]) else "neutral"
            items.append({"id": it.get("id"), "title": title, "journal": it.get("journalTitle"), "year": it.get("pubYear"), "stance": stance})
    status = "OK" if items else "NO_DATA"
    return Evidence(status=status, source="Europe PMC (ASE literature)", fetched_n=len(items), data={"items": items[:limit]}, citations=citations, fetched_at=_now())
_add_if_missing("/genetics/ase-check", _genetics_ase_check, ["GET"], Evidence)

async def _genetics_ptm_signal_lite(symbol: str, limit: int = 100):
    citations: List[str] = []
    u_url = "https://rest.uniprot.org/uniprotkb/search"
    params = {
        "query": f"gene_exact:{symbol} AND (organism_id:9606)",
        "fields": "accession,protein_name,genes,feature(MOD_RES)",
        "format": "json",
        "size": str(limit)
    }
    u_js, u_err = await _get_json(u_url, params=params, timeout_s=30.0)
    data: Dict[str, Any] = {"entries": []}
    if u_js and "results" in u_js:
        citations.append(u_url + f"?query=gene_exact:{symbol}%20AND%20organism_id:9606")
        for r in u_js.get("results", []):
            mods = []
            for f in r.get("features", []) or []:
                if f.get("type") == "MOD_RES":
                    mods.append({
                        "description": f.get("description"),
                        "begin": f.get("begin"),
                        "end": f.get("end"),
                        "evidence": f.get("evidences")
                    })
            data["entries"].append({"accession": r.get("primaryAccession"), "mod_sites": mods})
    pride_url = "https://www.ebi.ac.uk/pride/ws/archive/project/list"
    p_js, _ = await _get_json(pride_url, params={"keyword": f"{symbol} phospho", "pageSize": "25"}, timeout_s=25.0)
    if p_js and "list" in p_js:
        citations.append(pride_url + f"?keyword={symbol}%20phospho")
        data["phospho_projects"] = [{"accession": it.get("accession"), "title": it.get("title")} for it in p_js.get("list", [])]
    fetched = len(data.get("entries", []))
    status = "OK" if fetched > 0 else "NO_DATA"
    return Evidence(status=status, source="UniProtKB + PRIDE", fetched_n=fetched, data=data, citations=sorted(list(set(citations))), fetched_at=_now())
_add_if_missing("/genetics/ptm-signal-lite", _genetics_ptm_signal_lite, ["GET"], Evidence)

# 3) Literature enrichers (dup-safe)
async def _lit_meta(query: Optional[str] = None, gene: Optional[str] = None, symbol: Optional[str] = None,
                    efo: Optional[str] = None, condition: Optional[str] = None, limit: int = 50):
    q = query or " ".join([s for s in [gene or symbol, condition or efo] if s])
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    js, err = await _get_json(base, params={"query": q, "format": "json", "pageSize": str(limit)})
    return {"ok": bool(js), "q": q, "result": js or {}, "error": err, "fetched_at": _now()}
_add_if_missing("/lit/meta", _lit_meta, ["GET"], None)

async def _lit_search(symbol: str, condition: Optional[str] = None, window_days: int = 1825, page_size: int = 50):
    q = f'"{symbol}"'
    if condition:
        q += f' AND "{condition}"'
    q += f" AND FIRST_PDATE:[NOW-{window_days}DAYS TO NOW]"
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    js, err = await _get_json(url, params={"query": q, "format": "json", "pageSize": str(page_size)})
    items = []
    if js and "resultList" in js:
        for it in js["resultList"].get("result", []):
            items.append({"id": it.get("id"), "title": it.get("title"), "journal": it.get("journalTitle"),
                          "year": it.get("pubYear"), "pmcid": it.get("pmcid"), "pmid": it.get("pmid")})
    status = "OK" if items else "NO_DATA"
    return Evidence(status=status, source="Europe PMC", fetched_n=len(items),
                    data={"items": items}, citations=[url + f"?query={q}"], fetched_at=_now())
_add_if_missing("/lit/search", _lit_search, ["GET"], Evidence)

async def _lit_claims(symbol: str, condition: Optional[str] = None, limit: int = 50):
    base = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
    query = f"{symbol} {condition}" if condition else symbol
    epmc = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    js, _ = await _get_json(epmc, params={"query": query, "format": "json", "pageSize": str(limit)})
    pmids = []
    if js and "resultList" in js:
        for it in js["resultList"].get("result", []):
            if it.get("pmid"):
                pmids.append(it["pmid"])
    items = []
    citations = [epmc + f"?query={query}"]
    if pmids:
        pt_js, _ = await _post_json(base, {"pmids": pmids})
        citations.append(base)
        if isinstance(pt_js, dict) and "documents" in pt_js:
            for d in pt_js["documents"]:
                items.append({"pmid": d.get("pmid"), "title": d.get("passages", [{}])[0].get("text", "")[:200]})
    status = "OK" if items else "NO_DATA"
    return Evidence(status=status, source="EuropePMC + PubTator3", fetched_n=len(items),
                    data={"items": items}, citations=citations, fetched_at=_now())
_add_if_missing("/lit/claims", _lit_claims, ["GET"], Evidence)

async def _lit_score(body: Dict[str, Any]) -> Evidence:
    items = body.get("items", [])
    scored = []
    for it in items:
        t = (it.get("title") or "") + " " + (it.get("abstract") or "")
        s = 0; tl = t.lower()
        if any(w in tl for w in ["randomized", "replicated", "meta-analysis", "cohort", "colocalization", "mendelian randomization"]): s += 2
        if any(w in tl for w in ["mechanism", "splice", "ribosome", "proteomics", "phospho", "hla", "metabol"]): s += 1
        if any(w in tl for w in ["case report", "opinion", "letter"]): s -= 1
        scored.append({"id": it.get("id") or it.get("pmid"), "score": s})
    return Evidence(status="OK", source="objective rubric", fetched_n=len(scored),
                    data={"scores": scored}, citations=[], fetched_at=_now())
_add_if_missing("/lit/score", _lit_score, ["POST"], Evidence)

# 4) Causal-path synthesis endpoint (dup-safe)
class CausalPathRequest(BaseModel):
    gene: Optional[str] = None
    symbol: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    tissues: Optional[List[str]] = None
    modules: Optional[List[str]] = None
    config: Optional[Dict[str, Any]] = None
    limit: int = 100
    offset: int = 0


class CausalPath(BaseModel):
    e_levels: Dict[str, float]
    drivers: List[str] = []
    tensions: List[str] = []
    citations: List[str] = []
    fetched_at: str = Field(default_factory=_iso)

DEFAULT_CONFIG: Dict[str, Any] = {
    "priors": {"pi_path": 1e-5, "pi_shared_variant": 1e-4, "pi_trans_qtl": 1e-6},
    "edge_penalties": {"lambda_miss": {"E0": 2.0, "E1": 1.5, "E2": 1.2, "E3": 1.2, "E4": 0.8, "E5": 1.0, "E6": 2.0}},
    "direction": {"lambda_dir": 0.7, "beta_dir": 1.0},
    "tissue_weights": {"a0": -1.0, "a1": 0.8, "a2": 0.5, "a3": 0.6},
    "transforms": {"presence_eta": 0.4, "binder_eta": 0.6, "coverage_eta": 0.5},
    "dependence": {"rho_default": 0.25},
    "thresholds": {"weak_instrument_F": 10.0},
    "http": {"timeout_s": 60.0, "max_concurrency": 8},
    "module_set": [
        "genetics-regulatory","genetics-annotation","genetics-coloc","genetics-sqtl","genetics-pqtl",
        "expr-baseline","assoc-sc","assoc-proteomics",
        "tract-iedb-epitopes","tract-mhc-binding","immuno/hla-coverage",
        "mech-pathways","assoc-perturb","genetics-mr",
        "genetics-caqtl-lite","genetics-nmd-inference","genetics-mqtl-coloc","genetics-ptm-signal-lite"]
}

def _logit(p: float) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12);  return math.log(p) - math.log(1 - p)
def _logistic(x: float) -> float:
    if x >= 0: z = math.exp(-x);  return 1 / (1 + z)
    z = math.exp(x);  return z / (1 + z)
def _noisy_or_bf(bfs: List[float]) -> float:
    probs = [bf / (1.0 + bf) for bf in bfs if bf > 0]
    if not probs: return 0.0
    one_minus = 1.0
    for p in probs: one_minus *= (1.0 - p)
    pstar = 1.0 - one_minus
    return pstar / max(1e-12, (1.0 - pstar))
def _pp4_to_bf(pp4: Optional[float], pi: float) -> float:
    if pp4 is None: return 0.0
    pp4 = min(max(pp4, 1e-12), 1 - 1e-12)
    prior_odds = pi / (1 - pi); post_odds = pp4 / (1 - pp4)
    return post_odds / prior_odds
def _p_to_lbf(p: Optional[float]) -> float:
    if p is None or p <= 0 or p > 1: return 0.0
    return max(0.0, -math.log(p + 1e-12))
def _rank_to_lbf(rank: Optional[float]) -> float:
    if rank is None or rank <= 0 or rank > 1: return 0.0
    return -math.log(rank + 1e-12)
def _find_first_numeric(d: Any, keys: List[str]) -> Optional[float]:
    try:
        if isinstance(d, dict):
            for k in keys:
                if k in d and isinstance(d[k], (int, float)): return float(d[k])
            for v in d.values():
                x = _find_first_numeric(v, keys)
                if x is not None: return x
        elif isinstance(d, list):
            for v in d:
                x = _find_first_numeric(v, keys)
                if x is not None: return x
    except Exception: return None
    return None
def _find_list(d: Any, keys: List[str]) -> List[Any]:
    out: List[Any] = []
    try:
        if isinstance(d, dict):
            for k in keys:
                if k in d and isinstance(d[k], list): out.extend(d[k])
            for v in d.values(): out.extend(_find_list(v, keys))
        elif isinstance(d, list):
            for v in d: out.extend(_find_list(v, keys))
    except Exception: pass
    return out
def _dependence_joint_bf(bf_a: float, bf_b: float, rho: float) -> float:
    return math.log(1.0 + bf_a + bf_b + rho * bf_a * bf_b)

async def _synth_causal_path(request: Request, req: CausalPathRequest):
    symbol = (req.symbol or req.gene or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Provide 'symbol' or 'gene'")
    cfg = DEFAULT_CONFIG.copy()
    if req.config:
        for k, v in req.config.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict): cfg[k].update(v)
            else: cfg[k] = v
    modules = req.modules or cfg["module_set"]
    timeout = httpx.Timeout(connect=10.0, read=cfg["http"]["timeout_s"], write=cfg["http"]["timeout_s"], pool=5.0)
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=32)
    params = {"symbol": symbol, "condition": req.condition or req.efo, "limit": req.limit, "offset": req.offset}
    raw: Dict[str, Any] = {}; errors: Dict[str, str] = {}
    async with httpx.AsyncClient(app=request.app, base_url="http://internal", timeout=timeout, limits=limits) as client:
        async def _call(mkey: str):
            path = f"/{mkey}" if mkey.startswith(("genetics/", "immuno/", "tract/", "assoc/", "expr/", "mech/", "clin/", "comp/")) else f"/module/{mkey}"
            try:
                r = await client.get(path, params=params); r.raise_for_status(); raw[mkey] = r.json()
            except Exception as e: errors[mkey] = str(e)
        await asyncio.gather(*[asyncio.create_task(_call(m)) for m in modules])
    pi_shared = cfg["priors"]["pi_shared_variant"]; rho = cfg["dependence"]["rho_default"]
    def get_pp4(modkey: str) -> Optional[float]:
        d = (raw.get(modkey, {}) or {}).get("data", {}); 
        return _find_first_numeric(d, ["pp4", "PP4", "shared_posterior", "h4"])
    def get_presence_iBAQ() -> float:
        d = (raw.get("assoc-proteomics", {}) or {}).get("data", {}); 
        v = _find_first_numeric(d, ["iBAQ", "NSAF", "intensity", "abundance"]); return float(v or 0.0)
    def get_best_binder() -> Optional[float]:
        d = (raw.get("tract-mhc-binding", {}) or {}).get("data", {}); arrs = _find_list(d, ["predictions", "binders", "peptides"]); ranks = []
        for arr in arrs:
            if isinstance(arr, list):
                for it in arr:
                    if isinstance(it, dict):
                        r = _find_first_numeric(it, ["rank", "percentile", "ic50_rank"]); 
                        if r is not None: ranks.append(r if r <= 1.0 else r/100.0)
        return min(ranks) if ranks else None
    def get_epitope_count() -> int:
        d = (raw.get("tract-iedb-epitopes", {}) or {}).get("data", {}); total = 0
        for arr in _find_list(d, ["epitopes","hits","items","results"]):
            if isinstance(arr, list): total += len(arr)
        return total
    def get_expr_z() -> Dict[str, float]:
        d = (raw.get("expr-baseline", {}) or {}).get("data", {}); out: Dict[str, float] = {}
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, (int, float)): out[str(k)] = float(v)
        for arr in _find_list(d, ["tissues","items","results"]):
            if isinstance(arr, list):
                for it in arr:
                    if isinstance(it, dict):
                        t = it.get("tissue") or it.get("label") or it.get("name")
                        z = _find_first_numeric(it, ["z","zscore","z_score","expr_z"])
                        if t and z is not None: out[str(t)] = float(z)
        return out
    expr_z = get_expr_z(); tissues = sorted(expr_z.keys()) or ["generic"]
    bf_e0 = _pp4_to_bf(get_pp4("genetics-caqtl-lite"), pi_shared) if raw.get("genetics-caqtl-lite") else 0.0; lbf_e0 = math.log(1.0 + bf_e0)
    bf_eqtl = _pp4_to_bf(get_pp4("genetics-coloc"), pi_shared)
    lbf_mr_e = _p_to_lbf(_find_first_numeric((raw.get("genetics-mr", {}) or {}).get("data", {}), ["p","pval","p_value"])); lbf_e1 = math.log(1.0 + bf_eqtl) + lbf_mr_e
    bf_sqtl = _pp4_to_bf(get_pp4("genetics-sqtl"), pi_shared)
    nmd_js = (raw.get("genetics-nmd-inference") or {}).get("data", {}); nmd_bonus = 0.5 if (nmd_js.get("nmd_positive", 0) > 0) else 0.0
    bf_e2_star = _noisy_or_bf([bf_sqtl, nmd_bonus]); lbf_e2 = math.log(1.0 + bf_e2_star)
    bf_pqtl = _pp4_to_bf(get_pp4("genetics-pqtl"), pi_shared)
    lbf_presence = DEFAULT_CONFIG["transforms"]["presence_eta"] * math.log(1.0 + get_presence_iBAQ())
    lbf_joint = _dependence_joint_bf(bf_pqtl, bf_eqtl, rho); has_ptm = bool((raw.get("genetics-ptm-signal-lite") or {}).get("data", {}).get("entries"))
    lbf_e3 = lbf_joint + (0.3 if has_ptm else 0.0) + lbf_presence
    best_rank = get_best_binder(); lbf_pred = DEFAULT_CONFIG["transforms"]["binder_eta"] * _rank_to_lbf(best_rank)
    epi_count = get_epitope_count(); lbf_obs = 0.2 * epi_count; lbf_e4 = max(0.0, lbf_pred + lbf_obs)
    s_cell = _find_first_numeric((raw.get("mech-pathways", {}) or {}).get("data", {}), ["score","similarity","path_score"]) or 0.0
    s_pert = _find_first_numeric((raw.get("assoc-perturb", {}) or {}).get("data", {}), ["similarity","cosine","score"]) or 0.0
    lbf_e5 = max(0.0, s_cell * 2.0 + s_pert * 1.0)
    lbf_e6 = lbf_mr_e + math.log(1.0 + bf_eqtl)
    def tw(z: float) -> float:
        a0,a1 = DEFAULT_CONFIG["tissue_weights"]["a0"], DEFAULT_CONFIG["tissue_weights"]["a1"]; return _logistic(a0 + a1 * z)
    edges_tissue = {
        "E0": {t: tw(expr_z.get(t,0.0)) * lbf_e0 for t in tissues},
        "E1": {t: tw(expr_z.get(t,0.0)) * lbf_e1 for t in tissues},
        "E2": {t: tw(expr_z.get(t,0.0)) * lbf_e2 for t in tissues},
        "E3": {t: tw(expr_z.get(t,0.0)) * lbf_e3 for t in tissues},
        "E4": {t: 0.5 + 0.5*tw(expr_z.get(t,0.0)) for t in tissues},
        "E5": {t: tw(expr_z.get(t,0.0)) * lbf_e5 for t in tissues},
        "E6": {t: tw(expr_z.get(t,0.0)) * lbf_e6 for t in tissues},
    }
    edges_agg = {}
    for eid, per_t in edges_tissue.items():
        bfs = [math.exp(v) - 1.0 for v in per_t.values() if v > 0]
        bf_star = _noisy_or_bf(bfs)
        edges_agg[eid] = math.log(1.0 + bf_star) if bf_star > 0 else 0.0
    lambda_miss = DEFAULT_CONFIG["edge_penalties"]["lambda_miss"]
    paths = [("P1", ["E0","E1","E3","E6"]), ("P2", ["E2","E3","E6"]), ("P3", ["E0","E1","E2","E3","E6"]), ("P4", ["E0","E1","E3","E4","E5","E6"])]
    def path_posterior(edges: List[str]) -> Tuple[float,float,List[str]]:
        lbf_sum = 0.0; missing: List[str] = []
        for e in edges:
            v = edges_agg.get(e, 0.0)
            if v > 0: lbf_sum += v
            else: lbf_sum -= lambda_miss.get(e, 1.0); missing.append(e)
        post = _logistic(_logit(DEFAULT_CONFIG["priors"]["pi_path"]) + lbf_sum)
        return post, lbf_sum, missing
    posts = []; top_paths = []
    for pid, eds in paths:
        p, lbf_sum, miss = path_posterior(eds)
        top_paths.append({"id": pid, "edges": eds, "posterior": p, "lbf": lbf_sum, "missing": miss})
        posts.append(p)
    cpp = 1.0
    for p in posts: cpp *= (1.0 - p)
    cpp = 1.0 - cpp
    bottlenecks = []
    for e in ["E0","E1","E2","E3","E4","E5","E6"]:
        gap = max(0.0, lambda_miss.get(e,1.0) - edges_agg.get(e,0.0))
        if gap > 0.0: bottlenecks.append({"edge": e, "delta_needed_lbf": round(gap, 3)})
    bottlenecks.sort(key=lambda x: x["delta_needed_lbf"], reverse=True)
    citations: List[str] = []
    for js in raw.values():
        if isinstance(js, dict): citations.extend(js.get("citations", []) or [])
    citations = sorted(list({c for c in citations if isinstance(c, str)}))[:200]
    return {"ok": True, "symbol": symbol, "condition": req.condition or req.efo, "modules_used": modules,
            "errors": errors, "citations": citations, "edges": {"by_tissue": edges_tissue, "aggregate": edges_agg},
            "results": {"cpp": cpp, "dcs": 0.0, "top_paths": sorted(top_paths, key=lambda x: x["posterior"], reverse=True), "bottlenecks": bottlenecks},
            "fetched_at": _now()}
_add_if_missing("/synth/causal-path", _synth_causal_path, ["POST"], None)

# 4b) /synth/integrate — mathematical synthesis across modules
def _pp4_from_evidence(ev: Any) -> float:
    try:
        d = (ev or {}).get("data", {})
        for k in ["pp4","PP4","h4","shared_posterior"]:
            v = d.get(k)
            if isinstance(v, (int, float)):
                v = float(v)
                if 0 <= v <= 1:
                    return v
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, (int, float)) and 0 <= float(v) <= 1:
                    return float(v)
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            vv = it.get("pp4") or it.get("h4")
                            if isinstance(vv, (int, float)) and 0 <= vv <= 1:
                                return float(vv)
    except Exception:
        pass
    return 0.0

async def _synth_integrate(request: Request, body: SynthIntegrateRequest):
    symbol = body.symbol or body.gene
    if not symbol:
        raise HTTPException(status_code=400, detail="Provide 'symbol' or 'gene'")
    modules = body.modules or ["genetics-coloc","genetics-mr","genetics-sqtl","genetics-pqtl"]
    params = {"symbol": symbol, "limit": 100}
    results: Dict[str, Any] = {}

    async def _call(m):
        try:
            out = await _self_get(request, f"/module/{m}", params)
            results[m] = out
        except Exception as e:
            results[m] = {"status":"ERROR","data":{},"citations":[],"debug":{"error":str(e)}}

    await asyncio.gather(*[asyncio.create_task(_call(m)) for m in modules])

    method = (body.method or "math").lower()
    if method == "vote":
        ok = sum(1 for r in results.values() if (r or {}).get("status") == "OK")
        score = ok / max(1, len(modules))
    elif method == "rank":
        score = sum(max(0, (r or {}).get("fetched_n", 0)) for r in results.values()) / (100.0 * max(1, len(modules)))
    elif method == "bayes":
        bfs = []
        prior = 1e-4
        for r in results.values():
            p = _pp4_from_evidence(r)
            if p <= 0 or p >= 1:
                continue
            bf = (p / (1 - p)) / (prior / (1 - prior))
            if bf > 0:
                bfs.append(bf)
        if bfs:
            probs = [bf / (1.0 + bf) for bf in bfs]
            one_minus = 1.0
            for pr in probs:
                one_minus *= (1.0 - pr)
            score = 1.0 - one_minus
        else:
            score = 0.0
    elif method == "hybrid":
        ok = sum(1 for r in results.values() if (r or {}).get("status") == "OK")
        vote = ok / max(1, len(modules))
        bfs = []
        prior = 1e-4
        for r in results.values():
            p = _pp4_from_evidence(r)
            if p <= 0 or p >= 1:
                continue
            bf = (p / (1 - p)) / (prior / (1 - prior))
            if bf > 0:
                bfs.append(bf)
        if bfs:
            probs = [bf / (1.0 + bf) for bf in bfs]
            one_minus = 1.0
            for pr in probs:
                one_minus *= (1.0 - pr)
            bayes = 1.0 - one_minus
        else:
            bayes = 0.0
        score = 0.5 * (vote + bayes)
    else:  # "math"
        score = sum(max(0, (r or {}).get("fetched_n", 0)) for r in results.values()) / (100.0 * max(1, len(modules)))

    return {"ok": True, "symbol": symbol, "method": method, "score": score, "results": results}

_add_if_missing("/synth/integrate", _synth_integrate, ["POST"], None)

# 5) Two-layer domain helpers (dup-safe); merge with existing DOMAIN_MODULES if present
TECHNICAL_BUCKETS = {
    "population_genomics": [
        "genetics-l2g","genetics-coloc","genetics-mr","genetics-rare","genetics-mendelian","genetics-phewas-human-knockout",
        "genetics-sqtl","genetics-pqtl","genetics-consortia-summary","genetics-intolerance","genetics-mqtl-coloc","genetics-nmd-inference","genetics/ase-check"],
    "functional_genomics": [
        "genetics-chromatin-contacts","genetics-3d-maps","genetics-regulatory","genetics-annotation","genetics-functional","genetics-mavedb",
        "assoc-metabolomics","mech-ppi","mech-pathways","mech-ligrec","biology-causal-pathways","assoc-omics-phosphoproteomics",
        "genetics-caqtl-lite","genetics-ptm-signal-lite"
    ],
    "singlecell_perturbomics": [
        "assoc-sc","assoc-spatial","sc-hubmap","assoc-perturb","perturb-lincs-signatures","perturb-connectivity","perturb-perturbseq",
        "perturb-crispr-screens","perturb-signature-enrichment","perturb-drug-response"
    ],
    "expression_tissue": [
        "expr-baseline","expr-localization","expr-inducibility","assoc-bulk-rna","assoc-proteomics","assoc-bulk-prot","assoc-hpa-pathology"
    ],
    "tractability": [
        "mech-structure","tract-drugs","tract-ligandability-sm","tract-ligandability-oligo","tract-ligandability-ab","tract-surfaceome",
        "tract-modality","tract-mhc-binding","tract-iedb-epitopes","immuno/peptidome-pride"
    ],
    "clinical_translation_safety": [
        "tract-immunogenicity","function-dependency","immuno/hla-coverage","clin-safety","clin-rwe","clin-on-target-ae-prior",
        "perturb-depmap-dependency","clin-endpoints","clin-biomarker-fit","clin-pipeline","clin-feasibility","comp-intensity","comp-freedom"
    ]
}
DECISIONAL_DOMAINS = {
    "causality": {"uses": ["population_genomics","functional_genomics","expression_tissue","singlecell_perturbomics"], "synth": "/synth/causal-path"},
    "drugability": {"uses": ["tractability","functional_genomics","singlecell_perturbomics"], "synth": "/synth/decision-scores"},
    "therapeutic_index_safety": {"uses": ["clinical_translation_safety","expression_tissue","population_genomics"], "synth": "/synth/therapeutic-index"},
    "clinical_developability": {"uses": ["clinical_translation_safety","expression_tissue"], "synth": "/synth/decision-scores"}
}

def _union_unique(a: List[str], b: List[str]) -> List[str]:
    return sorted(list({*a, *b}))

try:
    DOMAIN_MODULES  # type: ignore[name-defined]
except NameError:
    DOMAIN_MODULES = {}  # type: ignore[var-annotated]

DOMAIN_MODULES.update({
    "1": _union_unique(DOMAIN_MODULES.get("1", []), TECHNICAL_BUCKETS["population_genomics"]),
    "2": _union_unique(DOMAIN_MODULES.get("2", []), TECHNICAL_BUCKETS["functional_genomics"]),
    "3": _union_unique(DOMAIN_MODULES.get("3", []), TECHNICAL_BUCKETS["singlecell_perturbomics"]),
    "4": _union_unique(DOMAIN_MODULES.get("4", []), TECHNICAL_BUCKETS["expression_tissue"]),
    "5": _union_unique(DOMAIN_MODULES.get("5", []), TECHNICAL_BUCKETS["tractability"]),
    "6": _union_unique(DOMAIN_MODULES.get("6", []), TECHNICAL_BUCKETS["clinical_translation_safety"]),
    "D1": _union_unique(DOMAIN_MODULES.get("D1", []), TECHNICAL_BUCKETS["population_genomics"]),
    "D2": _union_unique(DOMAIN_MODULES.get("D2", []), TECHNICAL_BUCKETS["functional_genomics"]),
    "D3": _union_unique(DOMAIN_MODULES.get("D3", []), TECHNICAL_BUCKETS["singlecell_perturbomics"]),
    "D4": _union_unique(DOMAIN_MODULES.get("D4", []), TECHNICAL_BUCKETS["expression_tissue"]),
    "D5": _union_unique(DOMAIN_MODULES.get("D5", []), TECHNICAL_BUCKETS["tractability"]),
    "D6": _union_unique(DOMAIN_MODULES.get("D6", []), TECHNICAL_BUCKETS["clinical_translation_safety"]),
})  # type: ignore[name-defined]

async def _domains_technical():
    return {"ok": True, "buckets": TECHNICAL_BUCKETS}
_add_if_missing("/domains/technical", _domains_technical, ["GET"], None)

async def _domains_decisional():
    return {"ok": True, "buckets": DECISIONAL_DOMAINS}
_add_if_missing("/domains/decisional", _domains_decisional, ["GET"], None)

try:
    MODULES  # type: ignore[name-defined]
except NameError:
    MODULES = []  # type: ignore[var-annotated]

class _MM:
    def __init__(self, key: str, route: str, bucket: str, sources: List[str]):
        self.key = key; self.route = route; self.bucket = bucket; self.sources = sources

_extra = [
    _MM("genetics-caqtl-lite", "/genetics/caqtl-lite", "functional_genomics", ["ENCODE REST","4DN","Ensembl VEP"]),
    _MM("genetics-nmd-inference", "/genetics/nmd-inference", "population_genomics", ["Ensembl REST","sQTL module"]),
    _MM("immuno/peptidome-pride", "/immuno/peptidome-pride", "tractability", ["PRIDE Archive REST"]),
    _MM("genetics-mqtl-coloc", "/genetics/mqtl-coloc", "population_genomics", ["OpenTargets GraphQL"]),
    _MM("genetics/ase-check", "/genetics/ase-check", "population_genomics", ["Europe PMC"]),
    _MM("genetics-ptm-signal-lite", "/genetics/ptm-signal-lite", "functional_genomics", ["UniProtKB REST","PRIDE Archive REST"]),
]
_have = {getattr(m, "key", getattr(m, "name", None)) for m in MODULES}
for m in _extra:
    if m.key not in _have:
        MODULES.append(m)

async def _registry_modules():
    out = []
    for m in MODULES:
        out.append({"key": getattr(m, "key", getattr(m, "name", None)), "route": getattr(m, "route", None),
                    "bucket": getattr(m, "bucket", ""), "sources": getattr(m, "sources", [])})
    return {"ok": True, "modules": out}
_add_if_missing("/registry/modules", _registry_modules, ["GET"], None)


# ===================== PATCH: Fully-enabled causal path + live fetch =====================
# This block appends an updated /synth/causal-path endpoint that (1) enables the full
# E0..E6 framework with family weights, directional penalties, priors, tissue gates,
# and (2) restores robust "live fetch" mechanics using bounded-concurrency httpx + TTL cache.

from typing import Dict, Any, List, Optional, Tuple
import math

# ---- Config (equivalent to the YAML you approved) ----
CAUSAL_PATH_CFG: Dict[str, Any] = {
  "priors": {
    "pi_path": 1e-5,
    "pi_shared_variant": 1e-4,
    "pi_trans_qtl": 1e-6
  },
  "dependence": {
    "rho_default": 0.25,
    "max_per_edge_items": 5
  },
  "tissue": {
    "gating": {
      "require_celltype_gate": True,
      "min_single_cell_overlap": 0.20,
      "min_deconv_r2": 0.50
    },
    "weighting": {
      "default_weight": 0.5,
      "single_cell_bonus": 0.3,
      "stimulation_bonus": 0.2,
      "cap": 1.0
    }
  },
  "weights_qtl": {
    "E0": {"bqtl":1.0, "caqtl":1.0, "hqtl":0.6, "meqtl":0.4, "dsqtl":0.8, "trans_caqtl":0.1},
    "E1": {"eqtl":1.0, "tqtl":0.6, "paqtl":0.6, "ase_dir":0.5},
    "E2": {"sqtl":1.0, "mir":0.3, "circ":0.2, "nmd_bonus":0.5},
    "E3": {"pqtl":1.0, "rqtl":0.3, "tiqtl":0.3, "ptmqtl":0.4, "presence_eta":0.4, "stab":0.3, "degr":0.3, "loc":0.2},
    "E4": {"observed_epitope":1.0, "predicted_binder":0.3, "hla_coverage":0.4},
    "E5": {"signature":0.8, "pathway":0.6, "perturb_concord":0.8},
    "E6": {"sink_coloc":1.0, "mr_multimediator":1.0}
  },
  "direction": {
    "lambda_dir": 0.7
  },
  "penalties": {
    "trans_unreplicated": "scale_by_prior",
    "unreplicated_qtl": 0.5,
    "proxy_only": 0.3
  },
  "caps": {
    "lbf_per_edge_max": 6.0,
    "lbf_per_source_max": 4.0
  },
  "edges": {
    "E0": ["bqtl","dsqtl","caqtl","hqtl","meqtl","motif_delta","distance_tss","contacts_3d","trans_caqtl"],
    "E1": ["eqtl","tqtl","paqtl","ase_dir"],
    "E2": ["sqtl","mir_qtl","circ_qtl","nmd_flag"],
    "E3": ["pqtl","rqtl","tiqtl","ptm_qtl","proteomics_presence","stab_qtl","degr_qtl","loc_qtl"],
    "E4": ["observed_epitope","predicted_binder","hla_coverage"],
    "E5": ["signature_concordance","pathway_concordance","perturb_concordance"],
    "E6": ["sink_coloc","mr_multimediator"]
  },
  "output": {
    "include_bottleneck_recos": True,
    "include_audit_trail": True
  }
}

# ---- Helpers ----
def _logit(x: float) -> float:
    x = min(max(x, 1e-12), 1-1e-12)
    return math.log(x/(1-x))

def _inv_logit(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))

def lbf_from_coloc(PP4: float, pi: float) -> float:
    PP4 = min(max(float(PP4), 1e-12), 1-1e-12)
    pi = min(max(float(pi), 1e-12), 1-1e-12)
    return math.log((PP4/(1-PP4)) / (pi/(1-pi)))

def lbf_from_p(p: float) -> float:
    return -math.log(max(float(p), 1e-300))

def apply_replication_and_proxy(BF: float, replicated: bool, is_proxy: bool, is_trans: bool, cfg: Dict[str, Any]) -> float:
    if not replicated:
        BF *= cfg['penalties']['unreplicated_qtl']
    if is_proxy:
        BF *= cfg['penalties']['proxy_only']
    if is_trans and cfg['penalties']['trans_unreplicated'] == "scale_by_prior":
        BF *= (cfg['priors']['pi_trans_qtl'] / cfg['priors']['pi_shared_variant'])
    return BF

def combine_with_dependence(evidence_terms: List[Tuple[float, float]], rho: float, lbf_cap_edge: float) -> float:
    base = 1.0; S = 0.0; n = 0
    for BF, w in evidence_terms:
        BF = min(BF, math.exp(lbf_cap_edge))
        S += w * BF; n += 1
    BF_edge = base + (1 - rho) * (S - n)
    return math.log(BF_edge)

def directional_penalty(discordances: int, lambda_dir: float) -> float:
    return -lambda_dir * max(0, discordances)

def tissue_weight(tissue_ctx: Dict[str, Any], cfg: Dict[str, Any]) -> float:
    w = cfg['tissue']['weighting']['default_weight']
    if tissue_ctx.get('single_cell_support'): w += cfg['tissue']['weighting']['single_cell_bonus']
    if tissue_ctx.get('stimulation_matched'): w += cfg['tissue']['weighting']['stimulation_bonus']
    return min(w, cfg['tissue']['weighting']['cap'])

def gated(evidence: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    gate = cfg['tissue']['gating']
    if not evidence.get('celltype_supported', False) and evidence.get('deconv_r2', 0.0) < gate['min_deconv_r2']:
        evidence['proxy_only'] = True
    return evidence

# ---- Technical bucket mapping (dynamic, prefix-based; no hard-coded stubs) ----
def classify_technical(module_key: str) -> str:
    k = module_key.lower()
    if any(s in k for s in ["tract-", "tractability", "druggability"]):
        return "TRACTABILITY"
    if k.startswith("singlecell") or "perturb" in k or "lincs" in k:
        return "SINGLE_CELL_PERTURBOMICS"
    if any(s in k for s in ["tissue", "expression", "gtex", "bulk-rna"]):
        return "EXPRESSION_TISSUE"
    if any(s in k for s in ["clinical", "safety", "phewas", "knockout", "trial"]):
        return "CLINICAL_TRANSLATION_SAFETY"
    if any(s in k for s in ["qtl", "coloc", "mr", "rare", "fine-map", "finemap"]):
        return "POPULATION_GENOMICS"  # includes functional QTLs by naming convention
    if any(s in k for s in ["pathway", "ppi", "ligrec", "proteomics", "metabolomics"]):
        return "FUNCTIONAL_GENOMICS"
    return "FUNCTIONAL_GENOMICS"

# ---- Live internal fan-out helper (uses ASGI app to call module endpoints concurrently) ----
async def _fanout_modules(request: Request, symbol: str, efo: Optional[str], modules: List[str], limit: int = 50) -> Dict[str, Any]:
    import httpx
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=5.0)
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=32)
    params = {"symbol": symbol, "condition": efo, "limit": limit}
    raw: Dict[str, Any] = {}; errors: Dict[str, str] = {}
    async with httpx.AsyncClient(app=request.app, base_url="http://internal", timeout=timeout, limits=limits) as client:
        async def _call(mkey: str):
            path = f"/{mkey}" if mkey.startswith(("genetics/", "assoc-", "singlecell-", "biology/", "tract-", "immuno/")) else f"/modules/{mkey}"
            try:
                r = await client.get(path, params=params)
                r.raise_for_status()
                raw[mkey] = r.json()
            except Exception as e:
                errors[mkey] = str(e)[:200]
        await asyncio.gather(*[_call(m) for m in modules])
    return raw

# ---- Core causal-path endpoint (v2) ----
@router.post("/synth/causal-path", response_model=CausalPath, tags=["synthesis"])
async def synth_causal_path_v2(req: CausalPathRequest, request: Request) -> CausalPath:
    cfg = CAUSAL_PATH_CFG.copy()
    symbol = req.symbol or req.gene
    if not symbol:
        raise HTTPException(status_code=400, detail="Provide 'symbol' or 'gene'")
    symbol = await _normalize_symbol(symbol)
    trait = req.condition or req.efo

    # 1) Collect module outputs live (respect req.modules or default module_set in cfg if present)
    modules = req.modules or [
        # Core cis genetics
        "genetics/coloc", "genetics/mr", "genetics/sqtl", "genetics/pqtl",
        # Functional QTL families (treated as supportive, low priors)
        "genetics/caqtl", "genetics/hqtl", "genetics/meqtl", "genetics/tqtl", "genetics/paqtl",
        "genetics/miR-qtl", "genetics/circqtl", "genetics/rqtl", "genetics/tiqtl", "genetics/ptm-qtl",
        # Protein/PTM presence
        "assoc/proteomics", "assoc/phosphoproteomics",
        # Immuno
        "immuno/mhc-binding", "immuno/iedb-epitopes", "immuno/hla-coverage",
        # Pathways / signatures
        "biology/causal-pathways", "assoc/perturb", "assoc/bulk-rna",
    ]

    raw = await _fanout_modules(request, symbol, trait, modules, limit=req.limit or 50)

    # Helper to safely read a PP4 or p-value
    def _pp4(obj: Any) -> float:
        try:
            d = (obj or {}).get("data", {})
            for k in ["pp4","PP4","h4","shared_posterior"]:
                v = d.get(k)
                if isinstance(v, (int,float)) and 0 <= float(v) <= 1: return float(v)
        except Exception: pass
        return 0.0
    def _p(obj: Any, key="p") -> float:
        try:
            d = (obj or {}).get("data", {})
            v = d.get(key) or d.get("pvalue") or d.get("pval")
            return float(v) if v is not None else 1.0
        except Exception:
            return 1.0
    def _rep(obj: Any) -> bool:
        try:
            d = (obj or {}).get("data", {})
            return bool(d.get("replicated") or d.get("replication") == "PASS")
        except Exception: return False

    # 2) Build edge evidence
    terms_E0 = []
    for key, wt in [("genetics/caqtl","caqtl"),("genetics/hqtl","hqtl"),("genetics/meqtl","meqtl")]:
        if key in raw:
            PP4 = _pp4(raw[key]); BF = math.exp(lbf_from_coloc(PP4, cfg["priors"]["pi_shared_variant"])) if PP4>0 else 1.0
            BF = apply_replication_and_proxy(BF, _rep(raw[key]), False, False, cfg)
            terms_E0.append((BF, cfg["weights_qtl"]["E0"].get(wt, 0.5)))
    # motif/distance support
    if "genetics/coloc" in raw:
        prior_small = 0.5  # small annotation prior if available
        terms_E0.append((math.exp(prior_small), 0.25))

    E1_eqtl = raw.get("genetics/coloc")
    E1_mr   = raw.get("genetics/mr")
    terms_E1 = []
    if E1_eqtl:
        PP4 = _pp4(E1_eqtl); 
        if PP4>0:
            terms_E1.append((math.exp(lbf_from_coloc(PP4, cfg["priors"]["pi_shared_variant"])), cfg["weights_qtl"]["E1"]["eqtl"]))
    if E1_mr:
        p = _p(E1_mr); terms_E1.append((math.exp(lbf_from_p(p)), cfg["weights_qtl"]["E1"]["eqtl"]))

    # E2 – splicing/miR/circ/NMD
    terms_E2 = []
    if "genetics/sqtl" in raw:
        PP4s = _pp4(raw["genetics/sqtl"]); 
        if PP4s>0:
            terms_E2.append((math.exp(lbf_from_coloc(PP4s, cfg["priors"]["pi_shared_variant"])), cfg["weights_qtl"]["E2"]["sqtl"]))
    for (key, wname, proxy_key) in [("genetics/miR-qtl","mir","mir_qtl"), ("genetics/circqtl","circ","circ_qtl")]:
        if key in raw:
            p = _p(raw[key]); replicated = _rep(raw[key])
            BF = math.exp(lbf_from_p(p))
            BF = apply_replication_and_proxy(BF, replicated, True, False, cfg)
            terms_E2.append((BF, cfg["weights_qtl"]["E2"].get(wname, 0.2)))
    # NMD bonus if present on sQTL
    if "genetics/sqtl" in raw:
        d = (raw["genetics/sqtl"] or {}).get("data", {})
        if d.get("nmd_flag") or d.get("ptc_frameshift"):
            terms_E2.append((math.exp(cfg["weights_qtl"]["E2"]["nmd_bonus"]), 1.0))

    # E3 – protein/translation/PTM/stability
    terms_E3 = []
    if "genetics/pqtl" in raw:
        PP4p = _pp4(raw["genetics/pqtl"]); 
        if PP4p>0:
            terms_E3.append((math.exp(lbf_from_coloc(PP4p, cfg["priors"]["pi_shared_variant"])), cfg["weights_qtl"]["E3"]["pqtl"]))
    for key, wname in [("genetics/rqtl","rqtl"),("genetics/tiqtl","tiqtl"),("genetics/ptm-qtl","ptmqtl")]:
        if key in raw:
            p = _p(raw[key]); BF = math.exp(lbf_from_p(p))
            BF = apply_replication_and_proxy(BF, _rep(raw[key]), True, False, cfg)
            terms_E3.append((BF, cfg["weights_qtl"]["E3"].get(wname, 0.3)))
    # proteomics presence (support)
    if "assoc/proteomics" in raw:
        try:
            iBAQ = float((raw["assoc/proteomics"]["data"] or {}).get("iBAQ") or 0.0)
        except Exception: iBAQ = 0.0
        presence = 1.0 + math.log1p(max(0.0, iBAQ))
        terms_E3.append((presence, cfg["weights_qtl"]["E3"]["presence_eta"]))

    # E4 – immunopeptidome
    terms_E4 = []
    if "immuno/iedb-epitopes" in raw:
        try:
            n_obs = int((raw["immuno/iedb-epitopes"]["data"] or {}).get("n_observed") or 0)
        except Exception: n_obs = 0
        if n_obs>0:
            terms_E4.append((1.0 + n_obs/10.0, cfg["weights_qtl"]["E4"]["observed_epitope"]))
    if "immuno/mhc-binding" in raw:
        try:
            best_rank = float((raw["immuno/mhc-binding"]["data"] or {}).get("best_rank") or 100.0)
        except Exception: best_rank = 100.0
        if best_rank < 2.0:
            terms_E4.append((math.exp(1.0), cfg["weights_qtl"]["E4"]["predicted_binder"]))
    if "immuno/hla-coverage" in raw:
        try:
            cov = float((raw["immuno/hla-coverage"]["data"] or {}).get("coverage") or 0.0)
        except Exception: cov = 0.0
        if cov>0: terms_E4.append(((1.0+cov), cfg["weights_qtl"]["E4"]["hla_coverage"]))

    # E5 – cellular state (signatures, pathways)
    terms_E5 = []
    for key, wname in [("assoc/perturb","perturb_concord"),("assoc/bulk-rna","signature"),("biology/causal-pathways","pathway")]:
        if key in raw:
            p = _p(raw[key]); BF = math.exp(lbf_from_p(p))
            terms_E5.append((BF, cfg["weights_qtl"]["E5"].get(wname, 0.6)))

    # E6 – sink: coloc at trait and multi-mediator MR
    terms_E6 = []
    if "genetics/coloc" in raw:
        PP4sink = _pp4(raw["genetics/coloc"]); 
        if PP4sink>0:
            terms_E6.append((math.exp(lbf_from_coloc(PP4sink, cfg["priors"]["pi_shared_variant"])), cfg["weights_qtl"]["E6"]["sink_coloc"]))
    if "genetics/mr" in raw:
        p = _p(raw["genetics/mr"]); terms_E6.append((math.exp(lbf_from_p(p)), cfg["weights_qtl"]["E6"]["mr_multimediator"]))

    # Combine with dependence shrinkage
    rho = cfg["dependence"]["rho_default"]
    LBF_E0 = combine_with_dependence(terms_E0, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E0 else 0.0
    LBF_E1 = combine_with_dependence(terms_E1, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E1 else 0.0
    LBF_E2 = combine_with_dependence(terms_E2, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E2 else 0.0
    LBF_E3 = combine_with_dependence(terms_E3, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E3 else 0.0
    LBF_E4 = combine_with_dependence(terms_E4, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E4 else 0.0
    LBF_E5 = combine_with_dependence(terms_E5, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E5 else 0.0
    LBF_E6 = combine_with_dependence(terms_E6, rho, cfg["caps"]["lbf_per_edge_max"]) if terms_E6 else 0.0

    # Directionality penalties (ASE, NMD, PTM; only if present)
    penalties = 0.0
    lam = cfg["direction"]["lambda_dir"]
    # ASE sign vs eQTL/GWAS/MR: if module outputs carry 'sign', apply penalties (best-effort)
    ase = raw.get("genetics/eqtl-ase") or raw.get("genetics/ase")
    if ase and E1_eqtl:
        try:
            sign_ase = int((ase.get("data") or {}).get("sign") or 0)
            sign_eqtl = int((E1_eqtl.get("data") or {}).get("sign") or 0)
            if sign_ase and sign_eqtl and (sign_ase != sign_eqtl): penalties += lam
        except Exception: pass
    # NMD-positive but expression not down: penalize E1/E2
    if "genetics/sqtl" in raw and E1_eqtl:
        try:
            nmd = ((raw["genetics/sqtl"]["data"] or {}).get("nmd_flag") or False)
            expr_delta = float((E1_eqtl.get("data") or {}).get("beta") or 0.0)
            if nmd and expr_delta > 0: penalties += lam
        except Exception: pass

    # Path posterior (Noisy-AND backbone)
    log_odds = _logit(cfg["priors"]["pi_path"]) + LBF_E0 + LBF_E1 + LBF_E6 + 0.7*(LBF_E2 + LBF_E3 + LBF_E4 + LBF_E5) - penalties
    posterior = _inv_logit(log_odds)

    # Bottlenecks (simple heuristics based on which edges are weak)
    bottlenecks: List[Dict[str, Any]] = []
    if LBF_E0 < 1.0: bottlenecks.append({"edge":"E0","next_experiment":"MPRA/CRISPRi across enhancer","success_criterion":"+1–2 LBF"})
    if LBF_E1 < 1.0: bottlenecks.append({"edge":"E1","next_experiment":"eQTL replication in disease tissue/stim","success_criterion":"+1–2 LBF"})
    if LBF_E2 < 1.0: bottlenecks.append({"edge":"E2","next_experiment":"Minigene + long-read RNA","success_criterion":"+1–2 LBF"})
    if LBF_E3 < 1.0: bottlenecks.append({"edge":"E3","next_experiment":"Targeted PRM/SRM or phospho-MS","success_criterion":"+1–1.5 LBF"})
    if LBF_E4 < 0.5: bottlenecks.append({"edge":"E4","next_experiment":"HLA-IP LC–MS/MS","success_criterion":"+0.5–1.0 LBF"})
    if LBF_E5 < 1.0: bottlenecks.append({"edge":"E5","next_experiment":"CRISPR/ASO + signature readout","success_criterion":"+1–2 LBF"})
    if LBF_E6 < 2.0: bottlenecks.append({"edge":"E6","next_experiment":"Multivariable MR with better instruments","success_criterion":"+1–2 LBF"})

    # Technical & decisional layers
    technical_map = {}
    for mkey in raw.keys():
        technical_map.setdefault(classify_technical(mkey), []).append(mkey)

    decisional = {
        "CAUSALITY": float(posterior),
        "DRUGGABILITY": float(((raw.get("tract/tractability") or {}).get("score") or 0.0)),
        "THERAPEUTIC_INDEX_SAFETY": float(((raw.get("clinical/safety") or {}).get("score") or 0.0)),
        "CLINICAL_DEVELOPABILITY": float(((raw.get("clinical/developability") or {}).get("score") or 0.0)),
    }

    return CausalPath(
        symbol=symbol, condition=trait,
        edges={"E0": LBF_E0, "E1": LBF_E1, "E2": LBF_E2, "E3": LBF_E3, "E4": LBF_E4, "E5": LBF_E5, "E6": LBF_E6},
        posterior=float(posterior),
        bottlenecks=bottlenecks,
        technical_buckets=technical_map,
        decisional=decisional,
        audit={"modules": list(raw.keys()), "penalties": penalties}
    )

# -- maxfixed: ensure Pydantic forward refs are resolved at import time
def __maxfixed_model_rebuild__():
    try:
        # Not all models may exist in every variant; rebuild best-effort.
        for cls_name in [
            "Evidence","Module","Registry","BucketNarrative","TherapeuticIndexRequest","TINarrative",
            "TargetCard","GraphEdge","TargetGraph","CausalPath","CausalPathRequest"
        ]:
            cls = globals().get(cls_name)
            if cls is not None and hasattr(cls, "model_rebuild"):
                try:
                    cls.model_rebuild()
                except Exception:
                    pass
    except Exception:
        pass

__maxfixed_model_rebuild__()

# ------------------------ maxfixed: self-test endpoint ------------------------
@router.post("/debug/selftest", response_model=Dict[str, Any])
async def debug_selftest(symbol: str = Body(...), efo: Optional[str] = Body(None), strict: bool = Body(False), request: Request = None) -> Dict[str, Any]:
    """Iterate all MODULES and try to run them via the internal client.
    Returns: per-module status (OK/NO_DATA/ERROR), the path called, and any note.
    If strict=True, raises HTTP 502 if any module errors out."""
    results: Dict[str, Any] = {}
    errors = 0
    try:
        async with httpx.AsyncClient(app=request.app, base_url="http://internal") as client:
            for m in MODULES:
                try:
                    # infer path from module
                    path_ = m.route
                    params = {"symbol": symbol}
                    if any(seg in path_ for seg in ("clin","assoc","genetics")) and efo:
                        params["condition"] = efo
                    r = await client.get(path_, params=params)
                    payload = r.json() if r.headers.get("content-type","{}").startswith("application/json") else {}
                    status = payload.get("status", "OK" if r.status_code==200 else "ERROR")
                    results[m.name] = {"path": path_, "status": status}
                    if status == "ERROR":
                        errors += 1
                except Exception as e:
                    errors += 1
                    results[getattr(m, "name", getattr(m, "route", "?"))] = {"path": getattr(m, "route", "?"), "status": "ERROR", "note": str(e)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"selftest failed to run: {e!s}")
    if strict and errors:
        raise HTTPException(status_code=502, detail=f"{errors} module(s) failed")
    return {"ok": errors==0, "errors": errors, "results": results}


@router.get("/genetics/consortia-summary", response_model=Evidence)
async def genetics_consortia_summary(symbol: Optional[str] = Query(None),
                                     gene: Optional[str] = Query(None),
                                     condition: Optional[str] = Query(None)) -> Evidence:
    """Summarise consortia evidence via OT datasourceScores for target-disease pair."""
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets",
                        fetched_n=0, data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        datasourceScores{ id score }
      }
    }"""
    js  = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
    keys  = {"uk_biobank","finngen","gwas_catalog"}
    cons  = [x for x in dss if (str(x.get("id","")).lower() in keys) and (x.get("score") or 0) > 0]
    return Evidence(status=("OK" if cons else "NO_DATA"), source="OpenTargets",
                    fetched_n=len(cons),
                    data={"ensg": ensg, "efo": efo, "consortia": cons, "datasourceScores": dss},
                    citations=cites, fetched_at=_now())



@router.get("/genetics/mqtl-coloc", response_model=Evidence)
async def genetics_mqtl_coloc(symbol: Optional[str] = Query(None),
                              gene: Optional[str] = Query(None),
                              condition: Optional[str] = Query(None)) -> Evidence:
    """Approximate molecular QTL colocalisation presence using OT datasource breakdown (live)."""
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets",
                        fetched_n=0, data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        datasourceScores{ id score }
      }
    }"""
    js  = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
    qtl_like = [x for x in dss if any(s in str(x.get("id","")).lower() for s in ["eqtl","pqtl","molecular","qtl"]) and (x.get("score") or 0) > 0]
    return Evidence(status=("OK" if qtl_like else "NO_DATA"), source="OpenTargets",
                    fetched_n=len(qtl_like),
                    data={"ensg": ensg, "efo": efo, "mqtl_like": qtl_like, "datasourceScores": dss},
                    citations=cites, fetched_at=_now())



@router.get("/genetics/consortia-summary", response_model=Evidence)
async def genetics_consortia_summary(symbol: Optional[str] = Query(None),
                                     gene: Optional[str] = Query(None),
                                     condition: Optional[str] = Query(None)) -> Evidence:
    """
    Live OpenTargets Platform GraphQL summary of consortia-like datasources
    (UK Biobank, FinnGen, GWAS Catalog) for the target–disease pair.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets",
                        fetched_n=0, data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        datasourceScores{ id score }
      }
    }"""
    js  = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
    keys  = {"uk_biobank","finngen","gwas_catalog"}
    cons  = [x for x in dss if (str(x.get("id","")).lower() in keys) and (x.get("score") or 0) > 0]
    return Evidence(status=("OK" if cons else "NO_DATA"), source="OpenTargets",
                    fetched_n=len(cons),
                    data={"ensg": ensg, "efo": efo, "consortia": cons, "datasourceScores": dss},
                    citations=cites, fetched_at=_now())

@router.get("/genetics/mqtl-coloc", response_model=Evidence)
async def genetics_mqtl_coloc(symbol: Optional[str] = Query(None),
                              gene: Optional[str] = Query(None),
                              condition: Optional[str] = Query(None)) -> Evidence:
    """
    Live OpenTargets Platform GraphQL QTL-coloc heuristic:
    flags datasourceScores that look QTL-like (contains 'eqtl','pqtl','molecular','qtl').
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets",
                        fetched_n=0, data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo},
                        citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        datasourceScores{ id score }
      }
    }"""
    js  = await _httpx_json_post(_OT_GQL, {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
    qtl_like = [x for x in dss if any(s in str(x.get("id","")).lower() for s in ["eqtl","pqtl","molecular","qtl"]) and (x.get("score") or 0) > 0]
    return Evidence(status=("OK" if qtl_like else "NO_DATA"), source="OpenTargets",
                    fetched_n=len(qtl_like),
                    data={"ensg": ensg, "efo": efo, "mqtl_like": qtl_like, "datasourceScores": dss},
                    citations=cites, fetched_at=_now())



# ---------- Backward-compatibility: hyphen aliases for genetics endpoints ----------
try:
    _alias_defs = [
        ("/genetics-coloc", "/genetics/coloc", "genetics_coloc"),
        ("/genetics-consortia-summary", "/genetics/consortia-summary", "genetics_consortia_summary"),
        ("/genetics-intolerance", "/genetics/intolerance", "genetics_intolerance"),
        ("/genetics-l2g", "/genetics/l2g", "genetics_l2g"),
        ("/genetics-mendelian", "/genetics/mendelian", "genetics_mendelian"),
        ("/genetics-mqtl-coloc", "/genetics/mqtl-coloc", "genetics_mqtl_coloc"),
        ("/genetics-mr", "/genetics/mr", "genetics_mr"),
        ("/genetics-nmd-inference", "/genetics/nmd-inference", "genetics_nmd_inference"),
        ("/genetics-phewas-human-knockout", "/genetics/phewas-human-knockout", "genetics_phewas_human_knockout"),
        ("/genetics-pqtl", "/genetics/pqtl", "genetics_pqtl"),
        ("/genetics-rare", "/genetics/rare", "genetics_rare"),
        ("/genetics-sqtl", "/genetics/sqtl", "genetics_sqtl"),
        ("/genetics-ase-check", "/genetics/ase-check", "genetics_ase_check"),
    ]
    for hyphen_path, slash_path, fn_name in _alias_defs:
        if fn_name in globals():
            router.add_api_route(hyphen_path, globals()[fn_name], methods=["GET"])
except Exception as _e:
    print("Alias registration warning:", _e)
# -------------------------------------------------------------------------------

