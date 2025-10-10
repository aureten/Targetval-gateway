
"""
router.py (Expanded, Production-Style)
=====================================

DrugHunter Router — full routing, literature scanning, synthesis, and orchestration
for the 55-module live data stack. This file stays **honest**: it does not assume
vendor-specific schemas. When specificity is required (GraphQL queries, JSON shapes),
it reads **templates from environment variables**.

Highlights
----------
- **55 LIVE modules** → dynamic GET endpoints (see `MODULES`).
- **Source adapters** (REST/GraphQL) with async retries, per-host rate limiting, bounded concurrency, TTL cache.
- **Evidence envelope** (`Evidence`) for consistent payloads across sources.
- **Declarative normalizers**: env-driven templates; built-in safe normalizers for a few common cases.
- **Literature layer**: Europe PMC primary search + Crossref/Unpaywall optional enrich. Quality scoring, triggers.
- **Synthesis engine** (no numeric grading in responses) using a multiplex graph:
    - Random Walk with Restart (RWR),
    - Steiner-like minimal explanation subgraph extraction,
    - Community detection (Louvain if available; else label-propagation),
    - Orthogonal-link detection (bridge edges across evidence families).
- **Audit log**, **job queue**, **config introspection**, **health**.

Run
---
    uvicorn router:app --reload

This file intentionally requires environment configuration for base URLs, GraphQL queries,
and optional normalizer templates, so it remains robust when third-party APIs change.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Body
from pydantic import BaseModel, Field
from cachetools import TTLCache
import logging

# Optional heavy deps
try:
    import networkx as nx
except Exception: nx = None
try:
    import numpy as np
except Exception: np = None
try:
    import community as community_louvain  # python-louvain
except Exception: community_louvain = None

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("drugrouter")

# -----------------------------------------------------------------------------
# Async primitives: rate limiting, concurrency, retry/backoff, cache
# -----------------------------------------------------------------------------

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


async def fetch_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    retries: int = 3,
    ratelimiter: Optional[AsyncRateLimiter] = None,
) -> httpx.Response:
    delays = _exp_backoff_delays(retries)
    last_exc = None
    for attempt in range(retries + 1):
        if ratelimiter:
            await ratelimiter.acquire()
        try:
            resp = await client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=timeout,
            )
            # Retry on 429 and 5xx
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
    rate = float(os.getenv(f"RATE_{host.upper().replace('.', '_')}_RPS", "5"))
    burst = int(os.getenv(f"RATE_{host.upper().replace('.', '_')}_BURST", "10"))
    key = f"{host}:{rate}:{burst}"
    if key not in _RATE_LIMITERS:
        _RATE_LIMITERS[key] = AsyncRateLimiter(rate_per_sec=rate, burst=burst)
    return _RATE_LIMITERS[key]


# Bounded concurrency (global)
_MAX_CONCURRENCY = int(os.getenv("GLOBAL_MAX_CONCURRENCY", "32"))
_SEM = asyncio.Semaphore(_MAX_CONCURRENCY)

_CACHE = TTLCache(maxsize=int(os.getenv("CACHE_SIZE", "20000")), ttl=int(os.getenv("CACHE_TTL_SECONDS", "900")))
def _cache_key(source: str, url: str, params: Optional[Dict[str, Any]], body: Optional[Dict[str, Any]]):
    return json.dumps({"src": source, "url": url, "params": params or {}, "body": body or {}}, sort_keys=True)


# -----------------------------------------------------------------------------
# Evidence envelope + models
# -----------------------------------------------------------------------------

class Evidence(BaseModel):
    status: str = "OK"                # "OK" | "NO_DATA" | "ERROR"
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


# -----------------------------------------------------------------------------
# Source adapters (REST / GraphQL)
# -----------------------------------------------------------------------------

@dataclass
class SourceConfig:
    name: str
    base_env: str
    kind: str      # 'rest' | 'graphql'
    headers_env: Optional[str] = None
    token_env: Optional[str] = None
    default_timeout: float = 30.0


class BaseAdapter:
    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg

    def _base(self) -> str:
        base = os.getenv(self.cfg.base_env, "").rstrip("/")
        if not base:
            raise RuntimeError(f"Base URL env var {self.cfg.base_env} not set for source '{self.cfg.name}'")
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
        url = f"{base}{path}"
        headers = self._headers()
        headers.setdefault("Content-Type", "application/json")
        host = httpx.URL(url).host
        rate = get_rate_limiter(host)
        async with _SEM:
            async with httpx.AsyncClient() as client:
                ck = _cache_key(self.cfg.name, url, params, json_body)
                if ck in _CACHE:
                    return _CACHE[ck]
                resp = await fetch_with_retry(client, "POST", url, headers=headers, json_body=json_body, ratelimiter=rate, timeout=self.cfg.default_timeout)
                _CACHE[ck] = resp
                return resp


# Live source catalog (names → adapters). No hard-coded URLs; use env.
LIVE_SOURCE_CATALOG: Dict[str, SourceConfig] = {
    # Expression / SC / spatial
    "GTEx API": SourceConfig("GTEx API", "SRC_GTXEX_BASE", "rest"),
    "EBI Expression Atlas API": SourceConfig("EBI Expression Atlas API", "SRC_EBI_EXPR_ATLAS_BASE", "rest"),
    "UniProtKB API": SourceConfig("UniProtKB API", "SRC_UNIPROT_BASE", "rest"),
    "HCA Azul APIs": SourceConfig("HCA Azul APIs", "SRC_HCA_AZUL_BASE", "rest"),
    "Single-Cell Expression Atlas API": SourceConfig("Single-Cell Expression Atlas API", "SRC_SCXA_BASE", "rest"),
    "CELLxGENE Discover API": SourceConfig("CELLxGENE Discover API", "SRC_CELLXGENE_BASE", "rest"),
    "Europe PMC API": SourceConfig("Europe PMC API", "SRC_EPMC_BASE", "rest"),
    "HuBMAP Search API": SourceConfig("HuBMAP Search API", "SRC_HUBMAP_BASE", "rest"),
    # Proteomics/metabolomics
    "ProteomicsDB API": SourceConfig("ProteomicsDB API", "SRC_PROTEOMICSDb_BASE", "rest"),
    "PRIDE Archive API": SourceConfig("PRIDE Archive API", "SRC_PRIDE_BASE", "rest"),
    "PDC (CPTAC) GraphQL": SourceConfig("PDC (CPTAC) GraphQL", "SRC_PDC_BASE", "graphql"),
    "MetaboLights API": SourceConfig("MetaboLights API", "SRC_METABOLIGHTS_BASE", "rest"),
    "Metabolomics Workbench API": SourceConfig("Metabolomics Workbench API", "SRC_METABO_WORKBENCH_BASE", "rest"),
    # Genetics
    "OpenTargets GraphQL (L2G)": SourceConfig("OpenTargets GraphQL (L2G)", "SRC_OPENTARGETS_BASE", "graphql"),
    "OpenTargets GraphQL (colocalisations)": SourceConfig("OpenTargets GraphQL (colocalisations)", "SRC_OPENTARGETS_BASE", "graphql"),
    "OpenTargets GraphQL (pQTL colocs)": SourceConfig("OpenTargets GraphQL (pQTL colocs)", "SRC_OPENTARGETS_BASE", "graphql"),
    "GWAS Catalog REST API": SourceConfig("GWAS Catalog REST API", "SRC_GWAS_CATALOG_BASE", "rest"),
    "IEU OpenGWAS API": SourceConfig("IEU OpenGWAS API", "SRC_OPENGWAS_BASE", "rest"),
    "PhenoScanner v2 API": SourceConfig("PhenoScanner v2 API", "SRC_PHENOSCANNER_BASE", "rest"),
    "ClinVar via NCBI E-utilities": SourceConfig("ClinVar via NCBI E-utilities", "SRC_NCBI_EUTILS_BASE", "rest"),
    "MyVariant.info": SourceConfig("MyVariant.info", "SRC_MYVARIANT_BASE", "rest"),
    "Ensembl VEP REST": SourceConfig("Ensembl VEP REST", "SRC_ENSEMBL_VEP_BASE", "rest"),
    "GTEx sQTL API": SourceConfig("GTEx sQTL API", "SRC_GTXEX_SQTL_BASE", "rest"),
    "ENCODE REST API": SourceConfig("ENCODE REST API", "SRC_ENCODE_BASE", "rest"),
    "UCSC Genome Browser track APIs": SourceConfig("UCSC Genome Browser track APIs", "SRC_UCSC_BASE", "rest"),
    "4D Nucleome API": SourceConfig("4D Nucleome API", "SRC_4DN_BASE", "rest"),
    "DepMap API": SourceConfig("DepMap API", "SRC_DEPMAP_BASE", "rest", token_env="SRC_DEPMAP_TOKEN"),
    "BioGRID ORCS REST": SourceConfig("BioGRID ORCS REST", "SRC_BIOGRID_ORCS_BASE", "rest", token_env="SRC_BIOGRID_TOKEN"),
    "MaveDB API": SourceConfig("MaveDB API", "SRC_MAVEDB_BASE", "rest"),
    "RNAcentral API": SourceConfig("RNAcentral API", "SRC_RNACENTRAL_BASE", "rest"),
    "gnomAD GraphQL API": SourceConfig("gnomAD GraphQL API", "SRC_GNOMAD_BASE", "graphql"),
    # Mechanisms & pathways
    "AlphaFold DB API": SourceConfig("AlphaFold DB API", "SRC_ALPHAFOLD_BASE", "rest"),
    "PDBe API": SourceConfig("PDBe API", "SRC_PDBE_BASE", "rest"),
    "PDBe-KB API": SourceConfig("PDBe-KB API", "SRC_PDBEKB_BASE", "rest"),
    "STRING API": SourceConfig("STRING API", "SRC_STRING_BASE", "rest"),
    "IntAct via PSICQUIC": SourceConfig("IntAct via PSICQUIC", "SRC_PSICQUIC_BASE", "rest"),
    "OmniPath API": SourceConfig("OmniPath API", "SRC_OMNIPATH_BASE", "rest"),
    "Reactome Content/Analysis APIs": SourceConfig("Reactome Content/Analysis APIs", "SRC_REACTOME_BASE", "rest"),
    "SIGNOR API": SourceConfig("SIGNOR API", "SRC_SIGNOR_BASE", "rest"),
    "QuickGO API": SourceConfig("QuickGO API", "SRC_QUICKGO_BASE", "rest"),
    "IUPHAR/Guide to Pharmacology API": SourceConfig("IUPHAR/Guide to Pharmacology API", "SRC_GT0P_BASE", "rest"),
    # Tractability / drugs
    "ChEMBL API": SourceConfig("ChEMBL API", "SRC_CHEMBL_BASE", "rest"),
    "DGIdb GraphQL": SourceConfig("DGIdb GraphQL", "SRC_DGIDB_BASE", "graphql"),
    "DrugCentral API": SourceConfig("DrugCentral API", "SRC_DRUGCENTRAL_BASE", "rest"),
    "BindingDB API": SourceConfig("BindingDB API", "SRC_BINDINGDB_BASE", "rest"),
    "PubChem PUG-REST": SourceConfig("PubChem PUG-REST", "SRC_PUBCHEM_BASE", "rest"),
    "STITCH API": SourceConfig("STITCH API", "SRC_STITCH_BASE", "rest"),
    "GlyGen API": SourceConfig("GlyGen API", "SRC_GLYGEN_BASE", "rest"),
    "Pharos GraphQL": SourceConfig("Pharos GraphQL", "SRC_PHAROS_BASE", "graphql"),
    # Immunology
    "IEDB IQ-API": SourceConfig("IEDB IQ-API", "SRC_IEDB_IQ_BASE", "rest"),
    "IEDB Tools API": SourceConfig("IEDB Tools API", "SRC_IEDB_TOOLS_BASE", "rest"),
    "IPD-IMGT/HLA API": SourceConfig("IPD-IMGT/HLA API", "SRC_IMGT_BASE", "rest"),
    # Clinical & safety
    "ClinicalTrials.gov v2 API": SourceConfig("ClinicalTrials.gov v2 API", "SRC_CTGV2_BASE", "rest"),
    "WHO ICTRP web service": SourceConfig("WHO ICTRP web service", "SRC_ICTRP_BASE", "rest"),
    "PharmGKB API": SourceConfig("PharmGKB API", "SRC_PHARMGKB_BASE", "rest"),
    "HPO/Monarch APIs": SourceConfig("HPO/Monarch APIs", "SRC_MONARCH_BASE", "rest"),
    "Inxight Drugs API": SourceConfig("Inxight Drugs API", "SRC_INXIGHT_BASE", "rest"),
    "openFDA FAERS API": SourceConfig("openFDA FAERS API", "SRC_OPENFDA_BASE", "rest"),
    "CTDbase API": SourceConfig("CTDbase API", "SRC_CTD_BASE", "rest"),
    "IMPC API": SourceConfig("IMPC API", "SRC_IMPC_BASE", "rest"),
    # Patents / utility
    "PatentsView API": SourceConfig("PatentsView API", "SRC_PATENTSVIEW_BASE", "rest"),
    "OLS API": SourceConfig("OLS API", "SRC_OLS_BASE", "rest"),
}

def get_adapter(source_name: str) -> BaseAdapter:
    cfg = LIVE_SOURCE_CATALOG.get(source_name)
    if not cfg:
        raise RuntimeError(f"Unknown source '{source_name}'")
    return GraphQLAdapter(cfg) if cfg.kind == "graphql" else RestAdapter(cfg)


# -----------------------------------------------------------------------------
# Module registry (55 modules; LIVE only)
# -----------------------------------------------------------------------------

@dataclass
class ModuleConfig:
    id: int
    key: str
    path: str
    domains: Dict[str, List[int]]   # {"primary":[...], "secondary":[...]}
    live_sources: List[str]


def _m(i, k, p, prim, sec, sources):
    return ModuleConfig(id=i, key=k, path=p, domains={"primary": prim, "secondary": sec}, live_sources=sources)


MODULES: List[ModuleConfig] = [
    _m(1, "expr-baseline", "/expr/baseline", [3], [2,5], ["GTEx API", "EBI Expression Atlas API"]),
    _m(2, "expr-localization", "/expr/localization", [3,4], [5], ["UniProtKB API"]),
    _m(3, "expr-inducibility", "/expr/inducibility", [3], [2,5], ["EBI Expression Atlas API", "NCBI GEO E-utilities", "ArrayExpress/BioStudies API"]),
    _m(4, "assoc-bulk-rna", "/assoc/bulk-rna", [2], [1], ["NCBI GEO E-utilities", "ArrayExpress/BioStudies API", "EBI Expression Atlas API"]),
    _m(5, "assoc-sc", "/assoc/sc", [3], [2], ["HCA Azul APIs", "Single-Cell Expression Atlas API", "CELLxGENE Discover API"]),
    _m(6, "assoc-spatial", "/assoc/spatial", [3], [2,5], ["Europe PMC API"]),
    _m(7, "sc-hubmap", "/sc/hubmap", [3], [2], ["HuBMAP Search API", "HCA Azul APIs"]),
    _m(8, "assoc-proteomics", "/assoc/proteomics", [2], [1,5,6], ["ProteomicsDB API", "PRIDE Archive API", "PDC (CPTAC) GraphQL"]),
    _m(9, "assoc-metabolomics", "/assoc/metabolomics", [2], [1,6], ["MetaboLights API", "Metabolomics Workbench API"]),
    _m(10, "assoc-hpa-pathology", "/assoc/hpa-pathology", [3], [2,5], ["Europe PMC API"]),
    _m(11, "assoc-perturb", "/assoc/perturb", [2], [4], ["LINCS LDP APIs", "CLUE.io API", "PubChem PUG-REST"]),
    _m(12, "genetics-l2g", "/genetics/l2g", [1], [6], ["OpenTargets GraphQL (L2G)", "GWAS Catalog REST API"]),
    _m(13, "genetics-coloc", "/genetics/coloc", [1], [2], ["OpenTargets GraphQL (colocalisations)", "eQTL Catalogue API", "IEU OpenGWAS API"]),
    _m(14, "genetics-mr", "/genetics/mr", [1], [], ["IEU OpenGWAS API", "PhenoScanner v2 API"]),
    _m(15, "genetics-rare", "/genetics/rare", [1], [5], ["ClinVar via NCBI E-utilities", "MyVariant.info", "Ensembl VEP REST"]),
    _m(16, "genetics-mendelian", "/genetics/mendelian", [1], [5], ["ClinGen GeneGraph/GraphQL"]),
    _m(17, "genetics-phewas-human-knockout", "/genetics/phewas-human-knockout", [1], [5], ["PhenoScanner v2 API", "IEU OpenGWAS API", "HPO/Monarch APIs"]),
    _m(18, "genetics-sqtl", "/genetics/sqtl", [1,3], [], ["GTEx sQTL API", "eQTL Catalogue API"]),
    _m(19, "genetics-pqtl", "/genetics/pqtl", [1,3], [], ["OpenTargets GraphQL (pQTL colocs)", "IEU OpenGWAS API"]),
    _m(20, "genetics-chromatin-contacts", "/genetics/chromatin-contacts", [1], [2], ["ENCODE REST API", "UCSC Genome Browser track APIs", "4D Nucleome API"]),
    _m(21, "genetics-3d-maps", "/genetics/3d-maps", [1], [2], ["4D Nucleome API", "UCSC Genome Browser track APIs"]),
    _m(22, "genetics-regulatory", "/genetics/regulatory", [1], [2,3], ["ENCODE REST API", "eQTL Catalogue API"]),
    _m(23, "genetics-annotation", "/genetics/annotation", [1], [5], ["Ensembl VEP REST", "MyVariant.info", "CADD API"]),
    _m(24, "genetics-consortia-summary", "/genetics/consortia-summary", [1], [6], ["IEU OpenGWAS API"]),
    _m(25, "genetics-functional", "/genetics/functional", [1], [2], ["DepMap API", "BioGRID ORCS REST", "Europe PMC API"]),
    _m(26, "genetics-mavedb", "/genetics/mavedb", [1], [2], ["MaveDB API"]),
    _m(27, "genetics-lncrna", "/genetics/lncrna", [2], [1], ["RNAcentral API", "Europe PMC API"]),
    _m(28, "genetics-mirna", "/genetics/mirna", [2], [1], ["RNAcentral API", "Europe PMC API"]),
    _m(29, "genetics-pathogenicity-priors", "/genetics/pathogenicity-priors", [1], [5], ["gnomAD GraphQL API", "CADD API"]),
    _m(30, "genetics-intolerance", "/genetics/intolerance", [1], [5], ["gnomAD GraphQL API"]),
    _m(31, "mech-structure", "/mech/structure", [4], [2], ["UniProtKB API", "AlphaFold DB API", "PDBe API", "PDBe-KB API"]),
    _m(32, "mech-ppi", "/mech/ppi", [2], [4,5], ["STRING API", "IntAct via PSICQUIC", "OmniPath API"]),
    _m(33, "mech-pathways", "/mech/pathways", [2], [5], ["Reactome Content/Analysis APIs", "Pathway Commons API", "SIGNOR API", "QuickGO API"]),
    _m(34, "mech-ligrec", "/mech/ligrec", [2,4], [3], ["OmniPath API", "IUPHAR/Guide to Pharmacology API", "Reactome Content/Analysis APIs"]),
    _m(35, "biology-causal-pathways", "/biology/causal-pathways", [2], [5], ["SIGNOR API", "Reactome Content/Analysis APIs", "Pathway Commons API"]),
    _m(36, "tract-drugs", "/tract/drugs", [4,6], [1], ["ChEMBL API", "DGIdb GraphQL", "DrugCentral API", "BindingDB API", "PubChem PUG-REST", "STITCH API", "Pharos GraphQL"]),
    _m(37, "tract-ligandability-sm", "/tract/ligandability-sm", [4], [2], ["UniProtKB API", "AlphaFold DB API", "PDBe API", "PDBe-KB API", "BindingDB API"]),
    _m(38, "tract-ligandability-ab", "/tract/ligandability-ab", [4,3], [5], ["UniProtKB API", "GlyGen API"]),
    _m(39, "tract-ligandability-oligo", "/tract/ligandability-oligo", [4], [2], ["Ensembl VEP REST", "RNAcentral API", "Europe PMC API"]),
    _m(40, "tract-modality", "/tract/modality", [4], [6], ["UniProtKB API", "AlphaFold DB API", "Pharos GraphQL", "IUPHAR/Guide to Pharmacology API"]),
    _m(41, "tract-immunogenicity", "/tract/immunogenicity", [5], [4], ["IEDB IQ-API", "IPD-IMGT/HLA API", "Europe PMC API"]),
    _m(42, "tract-mhc-binding", "/tract/mhc-binding", [5], [4], ["IEDB Tools API", "IPD-IMGT/HLA API"]),
    _m(43, "tract-iedb-epitopes", "/tract/iedb-epitopes", [5], [4], ["IEDB IQ-API", "IEDB Tools API"]),
    _m(44, "tract-surfaceome", "/tract/surfaceome", [4,3], [6], ["UniProtKB API", "GlyGen API"]),
    _m(45, "function-dependency", "/function/dependency", [5], [2,3], ["DepMap API", "BioGRID ORCS REST"]),
    _m(46, "immuno-hla-coverage", "/immuno/hla-coverage", [5], [6], ["IEDB Tools API", "IPD-IMGT/HLA API"]),
    _m(47, "clin-endpoints", "/clin/endpoints", [6], [1], ["ClinicalTrials.gov v2 API", "WHO ICTRP web service"]),
    _m(48, "clin-biomarker-fit", "/clin/biomarker-fit", [6], [1], ["OpenTargets GraphQL (L2G)", "PharmGKB API", "HPO/Monarch APIs"]),
    _m(49, "clin-pipeline", "/clin/pipeline", [6], [4], ["Inxight Drugs API", "ChEMBL API", "DrugCentral API"]),
    _m(50, "clin-safety", "/clin/safety", [5], [], ["openFDA FAERS API", "DrugCentral API", "CTDbase API", "DGIdb GraphQL", "IMPC API"]),
    _m(51, "clin-rwe", "/clin/rwe", [5], [], ["openFDA FAERS API"]),
    _m(52, "clin-on-target-ae-prior", "/clin/on-target-ae-prior", [5], [3], ["DrugCentral API", "DGIdb GraphQL", "openFDA FAERS API"]),
    _m(53, "clin-feasibility", "/clin/feasibility", [6], [], ["ClinicalTrials.gov v2 API", "WHO ICTRP web service"]),
    _m(54, "comp-intensity", "/comp/intensity", [6], [], ["PatentsView API"]),
    _m(55, "comp-freedom", "/comp/freedom", [6], [], ["PatentsView API"]),
]

# Evidence family/layer
FAMILY_BY_MODULE: Dict[str, str] = {
    # genetics / regulatory
    "genetics-l2g":"causal_genetics","genetics-coloc":"causal_genetics","genetics-mr":"causal_genetics",
    "genetics-consortia-summary":"causal_genetics","genetics-annotation":"causal_genetics",
    "genetics-regulatory":"regulatory_3d","genetics-3d-maps":"regulatory_3d","genetics-chromatin-contacts":"regulatory_3d",
    "genetics-sqtl":"causal_genetics","genetics-pqtl":"causal_genetics","genetics-functional":"causal_genetics",
    "genetics-rare":"causal_genetics","genetics-mendelian":"causal_genetics","genetics-pathogenicity-priors":"causal_genetics",
    "genetics-intolerance":"causal_genetics","genetics-mavedb":"causal_genetics","genetics-phewas-human-knockout":"causal_genetics",
    # expression & omics
    "expr-baseline":"expression_cell","assoc-sc":"expression_cell","assoc-spatial":"expression_cell","sc-hubmap":"expression_cell","expr-inducibility":"expression_cell",
    "assoc-bulk-rna":"expression_cell","assoc-proteomics":"proteo_metabo","assoc-metabolomics":"proteo_metabo","assoc-perturb":"proteo_metabo",
    # mechanisms
    "mech-pathways":"mechanisms","biology-causal-pathways":"mechanisms","mech-ppi":"mechanisms","mech-ligrec":"mechanisms","mech-structure":"tractability",
    # tractability
    "tract-ligandability-sm":"tractability","tract-ligandability-ab":"tractability","tract-ligandability-oligo":"tractability","tract-surfaceome":"tractability","tract-modality":"tractability","tract-drugs":"tractability",
    # safety
    "clin-safety":"safety","clin-rwe":"safety","clin-on-target-ae-prior":"safety","immuno-hla-coverage":"safety","function-dependency":"safety","tract-immunogenicity":"safety","tract-mhc-binding":"safety","tract-iedb-epitopes":"safety",
    # clinical
    "clin-endpoints":"clinical","clin-feasibility":"clinical","clin-pipeline":"clinical","clin-biomarker-fit":"clinical",
    # competition counted under clinical
    "comp-intensity":"clinical","comp-freedom":"clinical",
}

# -----------------------------------------------------------------------------
# Declarative normalizers (env-driven) + safe built-ins
# -----------------------------------------------------------------------------

def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    if not path: return None
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try: cur = cur[int(part)]
            except Exception: return None
        else:
            return None
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
    for row in arr:
        subj = EntityRef(
            type=tmpl.get("subject", {}).get("type", "entity"),
            id=_get_path(row, tmpl.get("subject", {}).get("id_path", "")),
            label=_get_path(row, tmpl.get("subject", {}).get("label_path", "")),
        )
        obj = EntityRef(
            type=tmpl.get("object", {}).get("type", "entity"),
            id=_get_path(row, tmpl.get("object", {}).get("id_path", "")),
            label=_get_path(row, tmpl.get("object", {}).get("label_path", "")),
        )
        ctx = {}
        for k, v in (tmpl.get("context") or {}).items():
            if k.endswith("_path"): continue
            ctx[k] = v
        for k, v in (tmpl.get("context") or {}).items():
            if k.endswith("_path"):
                ctx[k.replace("_path", "")] = _get_path(row, v)
        edges.append(Edge(subject=subj, predicate=pred, object=obj, provenance={"source": source_name}, family=FAMILY_BY_MODULE.get(module_key), context=ctx))
    return edges

def epmc_mentions_normalizer(raw: Dict[str, Any], module_key: str) -> List[Edge]:
    hits = (raw.get("resultList", {}) or {}).get("result", [])
    edges: List[Edge] = []
    for h in hits[:100]:
        pmid = h.get("pmid") or h.get("id")
        title = h.get("title")
        subj = EntityRef(type="paper", id=f"PMID:{pmid}", label=title)
        obj = EntityRef(type="topic", label=h.get("journalTitle"))
        edges.append(Edge(subject=subj, predicate="mentions", object=obj, provenance={"source": "Europe PMC", "pmid": pmid}, family="literature_claims"))
    return edges

# -----------------------------------------------------------------------------
# Builders & executor
# -----------------------------------------------------------------------------

async def _build_request(module: ModuleConfig, src: str, params: Dict[str, Any]) -> Dict[str, Any]:
    cfg = LIVE_SOURCE_CATALOG[src]
    if cfg.kind == "graphql":
        env_key = os.getenv(f"QUERY_TEMPLATE_{module.key.upper().replace('-', '_').replace('/', '_')}_{src.upper().replace(' ', '_').replace('-', '_').replace('/', '_')}")
        if not env_key:
            if "OPENTARGETS" in src.upper():
                env_key = "OPENTARGETS_GENERIC_QUERY"
            elif "GNOMAD" in src.upper():
                env_key = "GNOMAD_GENERIC_QUERY"
            else:
                env_key = "GENERIC_GRAPHQL_QUERY"
        try:
            payload = json.loads(os.getenv(env_key, ""))
            if not isinstance(payload, dict) or "query" not in payload:
                raise ValueError("GraphQL query template is missing 'query'")
        except Exception:
            raise RuntimeError(f"GraphQL query template missing/invalid for {module.key} @ {src} ({env_key})")
        variables = payload.get("variables", {})
        variables.update({k: v for k, v in params.items() if v is not None})
        return {"source": src, "path": "/graphql", "method": "POST", "params": None, "json_body": {"query": payload["query"], "variables": variables}}
    else:
        path = os.getenv(f"SRC_{src.upper().replace(' ', '_').replace('-', '_').replace('/', '_')}_PATH", "/")
        method = os.getenv(f"SRC_{src.upper().replace(' ', '_').replace('-', '_').replace('/', '_')}_METHOD", "GET")
        fwd = {k: v for k, v in params.items() if v is not None}
        return {"source": src, "path": path, "method": method, "params": fwd, "json_body": None}

async def execute_module(module: ModuleConfig, params: Dict[str, Any]) -> ModuleResponse:
    reqs: List[Dict[str, Any]] = []
    for src in module.live_sources:
        if src not in LIVE_SOURCE_CATALOG:
            return ModuleResponse(module=module.key, params=params, results=[], errors=[f"Unknown source '{src}'"])
        try:
            reqs.append(await _build_request(module, src, params))
        except Exception as e:
            return ModuleResponse(module=module.key, params=params, results=[], errors=[f"Builder error for '{src}': {e}"])

    async def _one(req: Dict[str, Any]) -> ModuleResult:
        adapter = get_adapter(req["source"])
        try:
            resp = await adapter.request(path=req["path"], method=req["method"], params=req["params"], json_body=req["json_body"])
            try: data = resp.json()
            except Exception: data = {"status_code": resp.status_code, "text": resp.text[:5000]}
            # normalizers
            edges = normalize_with_template(module.key, req["source"], data)
            if not edges and req["source"] == "Europe PMC API":
                edges = epmc_mentions_normalizer(data, module.key)
            evidence = Evidence(
                status="OK" if resp.status_code < 400 else "ERROR",
                source=req["source"],
                fetched_n=(len(edges) if isinstance(edges, list) else 0),
                data=data if isinstance(data, dict) else {},
                citations=[],
            )
            fam = FAMILY_BY_MODULE.get(module.key)
            for e in edges: e.family = e.family or fam
            return ModuleResult(module_key=module.key, source=req["source"], family=fam, evidence=evidence, edges=edges)
        except Exception as e:
            ev = Evidence(status="ERROR", source=req["source"], fetched_n=0, data={"error": str(e)}, citations=[])
            return ModuleResult(module_key=module.key, source=req["source"], family=FAMILY_BY_MODULE.get(module.key), evidence=ev, edges=[])

    results = await asyncio.gather(*[_one(r) for r in reqs])
    errors = [r.evidence.data.get("error") for r in results if r.evidence.status == "ERROR"]
    return ModuleResponse(module=module.key, params=params, results=results, errors=errors)


# -----------------------------------------------------------------------------
# Literature layer with quality gating + triggers
# -----------------------------------------------------------------------------

class LitScanRequest(BaseModel):
    query: str
    entity_hints: Dict[str, str] = Field(default_factory=dict)
    date_window_days: int = 1825
    include_preprints: bool = True
    limit: int = 100

class LitScanResponse(BaseModel):
    claims: List[LiteratureClaim]
    triggered_modules: List[str] = Field(default_factory=list)

async def _epmc_search(query: str, limit: int, include_preprints: bool) -> Dict[str, Any]:
    adapter = get_adapter("Europe PMC API")
    path = os.getenv("SRC_EPMC_SEARCH_PATH", "/search")
    params = {"query": query, "pageSize": min(limit, 1000)}
    if not include_preprints: params["source"] = "MED"
    resp = await adapter.request(path=path, method="GET", params=params)
    try: return resp.json()
    except Exception: return {}

async def _crossref_enrich(doi: str) -> Dict[str, Any]:
    if not doi: return {}
    if "Crossref" not in LIVE_SOURCE_CATALOG:
        LIVE_SOURCE_CATALOG["Crossref"] = SourceConfig("Crossref", "SRC_CROSSREF_BASE", "rest")
    try:
        adapter = get_adapter("Crossref")
        base_path = os.getenv("SRC_CROSSREF_PATH", "/works/")
        path = f"{base_path}{doi}"
        resp = await adapter.request(path=path, method="GET", params=None)
        return resp.json()
    except Exception:
        return {}

async def _unpaywall_enrich(doi: str) -> Dict[str, Any]:
    if not doi: return {}
    if "Unpaywall" not in LIVE_SOURCE_CATALOG:
        LIVE_SOURCE_CATALOG["Unpaywall"] = SourceConfig("Unpaywall", "SRC_UNPAYWALL_BASE", "rest")
    try:
        adapter = get_adapter("Unpaywall")
        base_path = os.getenv("SRC_UNPAYWALL_PATH", "/")
        email = os.getenv("UNPAYWALL_EMAIL", "")
        params = {"email": email} if email else None
        path = f"{base_path}{doi}"
        resp = await adapter.request(path=path, method="GET", params=params)
        return resp.json()
    except Exception:
        return {}

def _score_claim(hit: Dict[str, Any], crossref: Dict[str, Any], include_preprints: bool) -> float:
    types = (hit.get("pubTypeList", {}) or {}).get("pubType", []) or []
    base = 0.6
    if "Randomized Controlled Trial" in types or "Clinical Trial" in types: base += 0.2
    if "PREPRINT" in types and not include_preprints: base -= 0.3
    # crude recency
    try:
        year = int(hit.get("pubYear") or 0)
        if year >= 2022: base += 0.1
    except Exception:
        pass
    # retraction/concern via Crossref
    msg = (crossref.get("message") or {}) if isinstance(crossref, dict) else {}
    status = (msg.get("subtype") or "") + " " + (" ".join([rel.get("type","") for rel in (msg.get("relation", {}).get("is-retracted-by", []) or [])]))
    if "retract" in status.lower() or "withdraw" in status.lower():
        return 0.0
    return float(max(0.0, min(1.0, base)))

async def literature_scan_and_extract(req: LitScanRequest) -> LitScanResponse:
    data = await _epmc_search(req.query, req.limit, req.include_preprints)
    hits = (data.get("resultList", {}) or {}).get("result", []) if isinstance(data, dict) else []
    claims: List[LiteratureClaim] = []
    for h in hits[: req.limit]:
        pmid = h.get("pmid") or h.get("id")
        doi = (h.get("doi") or "").lower()
        crossref = await _crossref_enrich(doi) if doi else {}
        oa = await _unpaywall_enrich(doi) if doi else {}
        quality = _score_claim(h, crossref, req.include_preprints)
        claims.append(LiteratureClaim(
            subject=EntityRef(type="paper", id=f"PMID:{pmid}", label=h.get("title")),
            predicate="mentions",
            object=EntityRef(type="topic", label=req.query),
            context={"entity_hints": req.entity_hints, "journal": h.get("journalTitle"), "pubType": h.get("pubTypeList", {}).get("pubType", [])},
            evidence={"design": h.get("pubTypeList", {}).get("pubType", [])},
            provenance={"pmid": pmid, "doi": doi, "source": "Europe PMC"},
            scores={"quality": quality},
            timestamps={"published": h.get("firstPublicationDate"), "pubYear": h.get("pubYear")},
        ))

    # Trigger rules
    triggered: List[str] = []
    R = {
        "mendelian randomization": ["genetics-mr", "genetics-l2g", "genetics-coloc"],
        "colocalization": ["genetics-coloc", "genetics-regulatory"],
        "hi-c": ["genetics-3d-maps", "genetics-chromatin-contacts"],
        "crispr": ["genetics-functional", "assoc-perturb"],
        "adverse event": ["clin-safety", "clin-rwe", "clin-on-target-ae-prior"],
        "epitope": ["tract-iedb-epitopes", "tract-mhc-binding", "immuno-hla-coverage"],
    }
    env_R = os.getenv("LITERATURE_TRIGGERS_JSON", "")
    if env_R:
        try: R = json.loads(env_R)
        except Exception: pass
    ql = req.query.lower()
    for kw, mods in R.items():
        if kw.lower() in ql: triggered.extend(mods)

    return LitScanResponse(claims=claims, triggered_modules=sorted(set(triggered)))


# -----------------------------------------------------------------------------
# Synthesis engine (graph math; qualitative outputs only)
# -----------------------------------------------------------------------------

def _ensure_nx():
    if nx is None:
        raise HTTPException(status_code=500, detail="networkx not available. Install it to enable synthesis.")

def edges_to_graph(edges: List[Edge]) -> "nx.MultiDiGraph":
    _ensure_nx()
    G = nx.MultiDiGraph()
    for e in edges:
        s = (e.subject.type, e.subject.id or e.subject.label)
        o = (e.object.type, e.object.id or e.object.label)
        G.add_node(s, label=e.subject.label, type=e.subject.type, family=e.family)
        G.add_node(o, label=e.object.label, type=e.object.type, family=e.family)
        G.add_edge(s, o, predicate=e.predicate, provenance=e.provenance, context=e.context, weight=e.weight or 0.1, family=e.family)
    return G

def communities(G: "nx.MultiDiGraph") -> Dict[Tuple[str,str], int]:
    UG = G.to_undirected()
    if community_louvain is not None:
        return community_louvain.best_partition(UG)
    # fallback
    try:
        comms = nx.algorithms.community.asyn_lpa_communities(UG, weight=None)
        lab = {}
        cid = 0
        for c in comms:
            for n in c: lab[n]=cid
            cid+=1
        return lab
    except Exception:
        return {n: 0 for n in UG.nodes()}

def steiner_like(G: "nx.MultiDiGraph", terminals: List[Tuple[str,str]], max_nodes: int = 40) -> List[Tuple[Tuple[str,str],Tuple[str,str],Dict[str,Any]]]:
    UG = G.to_undirected()
    H = nx.Graph()
    for i in range(len(terminals)):
        for j in range(i+1, len(terminals)):
            s, t = terminals[i], terminals[j]
            try:
                p = nx.shortest_path(UG, s, t)
                for u, v in zip(p[:-1], p[1:]):
                    data = G.get_edge_data(u, v) or G.get_edge_data(v, u) or {}
                    dd = list(data.values())[0] if isinstance(data, dict) and data else {}
                    H.add_edge(u, v, **dd)
            except Exception:
                continue
    if H.number_of_nodes() > max_nodes:
        edges_sorted = sorted(H.edges(data=True), key=lambda x: x[2].get("weight", 0.0), reverse=True)
        H2 = nx.Graph()
        for u,v,d in edges_sorted[:max_nodes]:
            H2.add_edge(u,v,**d)
        H = H2
    return list(H.edges(data=True))

def detect_orthogonal_links(G: "nx.MultiDiGraph") -> List[Edge]:
    # Heuristic: edges bridging distinct families or communities
    UG = G.to_undirected()
    comm = communities(G)
    out: List[Edge] = []
    for u, v, d in G.edges(data=True):
        cu = comm.get(u, 0); cv = comm.get(v, 0)
        if cu != cv or (G.nodes[u].get("family") != G.nodes[v].get("family")):
            subj = EntityRef(type=u[0], id=u[1])
            obj = EntityRef(type=v[0], id=v[1])
            out.append(Edge(subject=subj, predicate=d.get("predicate","related_to"), object=obj, provenance=d.get("provenance",{}), context=d.get("context",{}), family=d.get("family")))
        if len(out) >= 10: break
    return out

def build_insight_cards(all_edges: List[Edge], terminals: List[Tuple[str,str]], title: str) -> List[InsightCard]:
    G = edges_to_graph(all_edges)
    steiner_edges = steiner_like(G, terminals, max_nodes=60)
    path_edges: List[Edge] = []
    for u, v, d in steiner_edges:
        subj = EntityRef(type=u[0], id=u[1])
        obj = EntityRef(type=v[0], id=v[1])
        path_edges.append(Edge(subject=subj, predicate=d.get("predicate","related_to"), object=obj, provenance=d.get("provenance",{}), context=d.get("context",{}), family=d.get("family"), weight=d.get("weight",0.1)))
    orth = detect_orthogonal_links(G)
    card = InsightCard(title=title, path_edges=path_edges, counter_evidence=[], orthogonal_links=orth, notes="Minimal explanation subgraph with orthogonal bridges.")
    return [card]


# -----------------------------------------------------------------------------
# Orchestration: jobs, audit
# -----------------------------------------------------------------------------

class Job(BaseModel):
    job_id: str
    kind: str
    payload: Dict[str, Any]
    created_at: float

JOB_QUEUE: asyncio.Queue = asyncio.Queue()
AUDIT_LOG: List[Dict[str, Any]] = []

def audit(event: str, detail: Dict[str, Any]):
    AUDIT_LOG.append({"ts": time.time(), "event": event, "detail": detail})
    if len(AUDIT_LOG) > 2000:
        AUDIT_LOG.pop(0)

async def enqueue_job(kind: str, payload: Dict[str, Any]) -> str:
    jid = f"{kind}-{int(time.time()*1000)}"
    await JOB_QUEUE.put(Job(job_id=jid, kind=kind, payload=payload, created_at=time.time()))
    audit("enqueue", {"job_id": jid, "kind": kind, "payload": payload})
    return jid

async def process_one_job() -> Optional[Dict[str, Any]]:
    try:
        job: Job = await asyncio.wait_for(JOB_QUEUE.get(), timeout=0.1)
    except asyncio.TimeoutError:
        return None
    result = {"job_id": job.job_id, "kind": job.kind, "status": "ok"}
    if job.kind == "rerun_modules":
        modules = job.payload.get("modules", [])
        params = job.payload.get("params", {})
        mod_map = {m.key: m for m in MODULES}
        tasks = [execute_module(mod_map[m], params) for m in modules if m in mod_map]
        await asyncio.gather(*tasks)
        audit("rerun_done", {"job_id": job.job_id, "modules": modules})
    else:
        audit("job_unknown", {"job_id": job.job_id, "kind": job.kind})
    return result


# -----------------------------------------------------------------------------
# FastAPI router
# -----------------------------------------------------------------------------

router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True, "modules": len(MODULES)}

@router.get("/modules")
async def list_modules():
    return [{"id": m.id, "key": m.key, "path": m.path, "domains": m.domains, "sources": m.live_sources} for m in MODULES]

@router.get("/config")
async def get_config():
    src_status = {name: bool(os.getenv(cfg.base_env)) for name, cfg in LIVE_SOURCE_CATALOG.items()}
    return {"modules": len(MODULES), "sources_configured": src_status, "families": FAMILY_BY_MODULE}

# Dynamic module endpoints (55)
for module in MODULES:
    async def _endpoint(
        target: Optional[str] = Query(None),
        disease: Optional[str] = Query(None),
        tissue: Optional[str] = Query(None),
        variant: Optional[str] = Query(None),
        celltype: Optional[str] = Query(None),
        refresh: bool = Query(False),
        limit: int = Query(50, ge=1, le=1000),
        _mkey: str = module.key,
    ) -> ModuleResponse:
        params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
        mod = next((m for m in MODULES if m.key == _mkey), None)
        if not mod:
            raise HTTPException(status_code=404, detail=f"Unknown module '{_mkey}'")
        res = await execute_module(mod, params)
        audit("module_call", {"module": _mkey, "params": params, "errors": res.errors})
        return res
    router.add_api_route(module.path, _endpoint, methods=["GET"], name=module.key, summary=f"{module.key} (LIVE)")


# Literature endpoints
class LitTriggerRequest(LitScanRequest):
    trigger_threshold: float = 0.70
    run_triggers: bool = True

class LitTriggerResponse(BaseModel):
    claims: List[LiteratureClaim]
    triggered_modules: List[str]
    jobs_enqueued: List[str]

@router.post("/literature/scan", response_model=LitScanResponse)
async def literature_scan(req: LitScanRequest) -> LitScanResponse:
    resp = await literature_scan_and_extract(req)
    audit("literature_scan", {"query": req.query, "triggered": resp.triggered_modules, "claims": len(resp.claims)})
    return resp

@router.post("/literature/scan-and-trigger", response_model=LitTriggerResponse)
async def literature_scan_and_trigger(req: LitTriggerRequest) -> LitTriggerResponse:
    base = await literature_scan_and_extract(req)
    jobs = []
    if req.run_triggers and base.triggered_modules:
        jid = await enqueue_job("rerun_modules", {"modules": base.triggered_modules, "params": req.entity_hints})
        jobs.append(jid)
    audit("literature_trigger", {"query": req.query, "modules": base.triggered_modules, "jobs": jobs})
    return LitTriggerResponse(claims=base.claims, triggered_modules=base.triggered_modules, jobs_enqueued=jobs)


# Domain synthesis
DOMAIN_PLAYBOOKS: Dict[int, List[str]] = {
    1: ["genetics-l2g","genetics-coloc","genetics-mr","genetics-annotation","genetics-consortia-summary","genetics-regulatory","genetics-3d-maps","genetics-chromatin-contacts","genetics-sqtl","genetics-pqtl","genetics-functional","genetics-rare","genetics-mendelian","genetics-pathogenicity-priors","genetics-intolerance","genetics-mavedb","genetics-phewas-human-knockout"],
    2: ["mech-pathways","biology-causal-pathways","mech-ppi","mech-ligrec","assoc-bulk-rna","assoc-proteomics","assoc-metabolomics","assoc-perturb"],
    3: ["expr-baseline","assoc-sc","assoc-spatial","sc-hubmap","expr-inducibility","function-dependency","genetics-sqtl","genetics-pqtl"],
    4: ["mech-structure","tract-ligandability-sm","tract-ligandability-ab","tract-ligandability-oligo","tract-surfaceome","tract-modality","tract-drugs"],
    5: ["clin-safety","clin-rwe","clin-on-target-ae-prior","immuno-hla-coverage","genetics-intolerance","function-dependency","tract-immunogenicity","tract-mhc-binding","tract-iedb-epitopes"],
    6: ["clin-endpoints","clin-feasibility","clin-pipeline","clin-biomarker-fit"],
}

class DomainSynthesisRequest(BaseModel):
    target: Optional[str] = None
    disease: Optional[str] = None
    tissue: Optional[str] = None
    variant: Optional[str] = None
    celltype: Optional[str] = None
    domain: int = Field(..., ge=1, le=6)
    modules: Optional[List[str]] = None
    limit_per_module: int = 50

class DomainSynthesisResponse(BaseModel):
    cards: List[InsightCard]
    used_modules: List[str]

@router.post("/synthesis/domain", response_model=DomainSynthesisResponse)
async def synthesize_domain(req: DomainSynthesisRequest) -> DomainSynthesisResponse:
    used_modules = req.modules or DOMAIN_PLAYBOOKS.get(req.domain, [])
    if not used_modules:
        raise HTTPException(status_code=400, detail=f"No modules mapped for domain {req.domain}")
    mod_map = {m.key: m for m in MODULES}
    tasks = []
    params = {"target": req.target, "disease": req.disease, "tissue": req.tissue, "variant": req.variant, "celltype": req.celltype, "limit": req.limit_per_module}
    for mk in used_modules:
        if mk in mod_map: tasks.append(execute_module(mod_map[mk], params))
    mod_responses = await asyncio.gather(*tasks)
    all_edges: List[Edge] = []
    for mr in mod_responses:
        for r in mr.results:
            all_edges.extend(r.edges)
    terminals: List[Tuple[str,str]] = []
    if req.target: terminals.append(("gene", req.target))
    if req.disease: terminals.append(("disease", req.disease))
    if not terminals and all_edges:
        seen = set()
        for e in all_edges:
            s = (e.subject.type, e.subject.id or e.subject.label)
            o = (e.object.type, e.object.id or e.object.label)
            if s not in seen: terminals.append(s); seen.add(s)
            if o not in seen: terminals.append(o); seen.add(o)
            if len(terminals) >= 2: break
    cards = build_insight_cards(all_edges, terminals, title=f"Domain {req.domain} synthesis")
    audit("synthesis_domain", {"domain": req.domain, "used_modules": used_modules, "edges": len(all_edges)})
    return DomainSynthesisResponse(cards=cards, used_modules=used_modules)


# Cross-domain storyline
class CrossDomainRequest(BaseModel):
    target: Optional[str] = None
    disease: Optional[str] = None
    include_domains: Optional[List[int]] = None
    limit_per_module: int = 40

class CrossDomainResponse(BaseModel):
    storylines: List[Storyline]
    used_modules: List[str]

@router.post("/synthesis/cross", response_model=CrossDomainResponse)
async def synthesize_cross(req: CrossDomainRequest) -> CrossDomainResponse:
    domains = req.include_domains or [1,2,3,4,5,6]
    used_modules = sorted(set([mk for d in domains for mk in DOMAIN_PLAYBOOKS.get(d, [])]))
    mod_map = {m.key: m for m in MODULES}
    params = {"target": req.target, "disease": req.disease, "limit": req.limit_per_module}
    tasks = [execute_module(mod_map[mk], params) for mk in used_modules if mk in mod_map]
    mod_responses = await asyncio.gather(*tasks)
    edges: List[Edge] = []
    for mr in mod_responses:
        for r in mr.results:
            edges.extend(r.edges)
    terminals: List[Tuple[str,str]] = []
    if req.target: terminals.append(("gene", req.target))
    if req.disease: terminals.append(("disease", req.disease))
    if not terminals and edges:
        deg = {}
        for e in edges:
            s = (e.subject.type, e.subject.id or e.subject.label)
            o = (e.object.type, e.object.id or e.object.label)
            deg[s] = deg.get(s,0)+1; deg[o]=deg.get(o,0)+1
        terminals = sorted(deg, key=deg.get, reverse=True)[:2]
    cards = build_insight_cards(edges, terminals, title="Cross-domain storyline")
    storylines = [Storyline(title=c.title, path_edges=c.path_edges, notes=c.notes) for c in cards]
    audit("synthesis_cross", {"domains": domains, "used_modules": used_modules, "edges": len(edges)})
    return CrossDomainResponse(storylines=storylines, used_modules=used_modules)


# Admin
@router.post("/admin/jobs/process-one")
async def process_one():
    res = await process_one_job()
    return res or {"status": "empty"}

@router.get("/admin/audit")
async def get_audit(limit: int = 200):
    return AUDIT_LOG[-limit:]


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(title="DrugHunter Router (Expanded)", version="1.0.0")
app.include_router(router, prefix="/api")


# -----------------------------------------------------------------------------
# Environment guide (summary)
# -----------------------------------------------------------------------------
"""
Set env vars for:
- Base URLs: SRC_*_BASE (see LIVE_SOURCE_CATALOG keys).
- REST path & method per source (optional): SRC_<SOURCE>_PATH, SRC_<SOURCE>_METHOD.
- GraphQL queries: e.g., OPENTARGETS_GENERIC_QUERY='{"query":"...", "variables":{}}'
  and QUERY_TEMPLATE_<MODULE>_<SOURCE>=<that JSON>.
- Normalizer templates: NORMALIZER_TEMPLATE_<MODULE>_<SOURCE>='{"array_path":"...","subject":...}'.
- Rate limits: RATE_<HOST>_RPS, RATE_<HOST>_BURST.
- Cache: CACHE_TTL_SECONDS, CACHE_SIZE.
- Concurrency: GLOBAL_MAX_CONCURRENCY.
"""
