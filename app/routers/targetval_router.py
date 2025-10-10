
# -*- coding: utf-8 -*-
"""
router_v9.py
============
**Best-and-loaded** merge:
- 55 **decorated** module endpoints.
- Live adapters with **defaults** for key public sources (Europe PMC, PatentsView, CTGv2, Crossref, Unpaywall, OpenTargets GraphQL).
- Literature scan + triggers (job queue), audit log.
- Full synthesis: Steiner-like extraction, communities, RWR (optional), orthogonal-link detection.
- Declarative normalizers via env; built-in EPuMC mentions; PatentsView builder wired for OOTB results.

Run: `uvicorn router_v9:app --reload`
"""

from __future__ import annotations

import asyncio, json, os, time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Body
from pydantic import BaseModel, Field
from cachetools import TTLCache

# Optional scientific libs
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
# Defaults to enable **live** calls without env
# -----------------------------------------------------------------------------
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
}

# -----------------------------------------------------------------------------
# Async infra: rate limit, retry, cache, concurrency
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

async def fetch_with_retry(client: httpx.AsyncClient, method: str, url: str, *, headers: Optional[Dict[str,str]] = None,
                           params: Optional[Dict[str,Any]] = None, json_body: Optional[Dict[str,Any]] = None,
                           data: Optional[Dict[str,Any]] = None, timeout: float = 30.0, retries: int = 2,
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
                    await asyncio.sleep(delays[attempt]); continue
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(delays[attempt]); continue
            raise
    if last_exc: raise last_exc

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

_CACHE = TTLCache(maxsize=int(os.getenv("CACHE_SIZE", "15000")), ttl=int(os.getenv("CACHE_TTL_SECONDS", "900")))
def _cache_key(source: str, url: str, params: Optional[Dict[str,Any]], body: Optional[Dict[str,Any]]):
    return json.dumps({"src":source,"url":url,"params":params or {}, "body": body or {}}, sort_keys=True)

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class Evidence(BaseModel):
    status: str = "OK"               # "OK" | "NO_DATA" | "ERROR"
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
# Adapters
# -----------------------------------------------------------------------------
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
            raise RuntimeError(f"Base URL env var {self.cfg.base_env} not set and no default configured for '{self.cfg.name}'")
        return base
    def _headers(self) -> Dict[str,str]:
        headers: Dict[str,str] = {}
        if self.cfg.headers_env and os.getenv(self.cfg.headers_env):
            try: headers.update(json.loads(os.getenv(self.cfg.headers_env, "{}")))
            except Exception: pass
        if self.cfg.token_env and os.getenv(self.cfg.token_env):
            headers.setdefault("Authorization", f"Bearer {os.getenv(self.cfg.token_env)}")
        return headers
    async def request(self, path: str, method: str = "GET", params: Optional[Dict[str,Any]] = None, json_body: Optional[Dict[str,Any]] = None) -> httpx.Response:
        raise NotImplementedError

class RestAdapter(BaseAdapter):
    async def request(self, path: str, method: str = "GET", params: Optional[Dict[str,Any]] = None, json_body: Optional[Dict[str,Any]] = None) -> httpx.Response:
        base = self._base()
        if not path or path == "/":
            path = DEFAULT_PATHS.get(self.cfg.name, "/")
        url = f"{base}{path}"
        headers = self._headers()
        host = httpx.URL(url).host
        rate = get_rate_limiter(host)
        async with _SEM:
            async with httpx.AsyncClient() as client:
                ck = _cache_key(self.cfg.name, url, params, json_body)
                if ck in _CACHE: return _CACHE[ck]
                resp = await fetch_with_retry(client, method, url, headers=headers, params=params, json_body=json_body, ratelimiter=rate, timeout=self.cfg.default_timeout)
                _CACHE[ck] = resp
                return resp

class GraphQLAdapter(BaseAdapter):
    async def request(self, path: str = "/graphql", method: str = "POST", params: Optional[Dict[str,Any]] = None, json_body: Optional[Dict[str,Any]] = None) -> httpx.Response:
        base = self._base()
        if "platform.opentargets.org" in base:
            path = "/api/v4/graphql"
        url = f"{base}{path}"
        headers = self._headers()
        headers.setdefault("Content-Type", "application/json")
        host = httpx.URL(url).host
        rate = get_rate_limiter(host)
        if not json_body:
            json_body = {"query":"query { __typename }","variables":{}}
        async with _SEM:
            async with httpx.AsyncClient() as client:
                ck = _cache_key(self.cfg.name, url, params, json_body)
                if ck in _CACHE: return _CACHE[ck]
                resp = await fetch_with_retry(client, "POST", url, headers=headers, json_body=json_body, ratelimiter=rate, timeout=self.cfg.default_timeout)
                _CACHE[ck] = resp
                return resp

# Source catalog (subset defaulted; others require env)
LIVE_SOURCE_CATALOG: Dict[str, SourceConfig] = {
    # Defaults provided
    "Europe PMC API": SourceConfig("Europe PMC API", "SRC_EPMC_BASE", "rest"),
    "Crossref": SourceConfig("Crossref", "SRC_CROSSREF_BASE", "rest"),
    "Unpaywall": SourceConfig("Unpaywall", "SRC_UNPAYWALL_BASE", "rest"),
    "PatentsView API": SourceConfig("PatentsView API", "SRC_PATENTSVIEW_BASE", "rest"),
    "ClinicalTrials.gov v2 API": SourceConfig("ClinicalTrials.gov v2 API", "SRC_CTGV2_BASE", "rest"),
    "OpenTargets GraphQL (L2G)": SourceConfig("OpenTargets GraphQL (L2G)", "SRC_OPENTARGETS_BASE", "graphql"),
    # Many more (require env): add as you configure
    "UniProtKB API": SourceConfig("UniProtKB API", "SRC_UNIPROT_BASE", "rest"),
    "GTEx API": SourceConfig("GTEx API", "SRC_GTEX_BASE", "rest"),
    "EBI Expression Atlas API": SourceConfig("EBI Expression Atlas API", "SRC_EBI_EXPR_ATLAS_BASE", "rest"),
    "HCA Azul APIs": SourceConfig("HCA Azul APIs", "SRC_HCA_AZUL_BASE", "rest"),
    "Single-Cell Expression Atlas API": SourceConfig("Single-Cell Expression Atlas API", "SRC_SCXA_BASE", "rest"),
    "CELLxGENE Discover API": SourceConfig("CELLxGENE Discover API", "SRC_CELLXGENE_BASE", "rest"),
    "HuBMAP Search API": SourceConfig("HuBMAP Search API", "SRC_HUBMAP_BASE", "rest"),
    "ProteomicsDB API": SourceConfig("ProteomicsDB API", "SRC_PROTEOMICSDb_BASE", "rest"),
    "PRIDE Archive API": SourceConfig("PRIDE Archive API", "SRC_PRIDE_BASE", "rest"),
    "PDC (CPTAC) GraphQL": SourceConfig("PDC (CPTAC) GraphQL", "SRC_PDC_BASE", "graphql"),
    "MetaboLights API": SourceConfig("MetaboLights API", "SRC_METABOLIGHTS_BASE", "rest"),
    "Metabolomics Workbench API": SourceConfig("Metabolomics Workbench API", "SRC_METABO_WORKBENCH_BASE", "rest"),
    "GWAS Catalog REST API": SourceConfig("GWAS Catalog REST API", "SRC_GWAS_CATALOG_BASE", "rest"),
    "IEU OpenGWAS API": SourceConfig("IEU OpenGWAS API", "SRC_OPENGWAS_BASE", "rest"),
    "PhenoScanner v2 API": SourceConfig("PhenoScanner v2 API", "SRC_PHENOSCANNER_BASE", "rest"),
    "ClinVar via NCBI E-utilities": SourceConfig("ClinVar via NCBI E-utilities", "SRC_NCBI_EUTILS_BASE", "rest"),
    "MyVariant.info": SourceConfig("MyVariant.info", "SRC_MYVARIANT_BASE", "rest"),
    "Ensembl VEP REST": SourceConfig("Ensembl VEP REST", "SRC_ENSEMBL_VEP_BASE", "rest"),
    "GTEx sQTL API": SourceConfig("GTEx sQTL API", "SRC_GTEX_SQTL_BASE", "rest"),
    "ENCODE REST API": SourceConfig("ENCODE REST API", "SRC_ENCODE_BASE", "rest"),
    "UCSC Genome Browser track APIs": SourceConfig("UCSC Genome Browser track APIs", "SRC_UCSC_BASE", "rest"),
    "4D Nucleome API": SourceConfig("4D Nucleome API", "SRC_4DN_BASE", "rest"),
    "DepMap API": SourceConfig("DepMap API", "SRC_DEPMAP_BASE", "rest"),
    "BioGRID ORCS REST": SourceConfig("BioGRID ORCS REST", "SRC_BIOGRID_ORCS_BASE", "rest"),
    "MaveDB API": SourceConfig("MaveDB API", "SRC_MAVEDB_BASE", "rest"),
    "RNAcentral API": SourceConfig("RNAcentral API", "SRC_RNACENTRAL_BASE", "rest"),
    "gnomAD GraphQL API": SourceConfig("gnomAD GraphQL API", "SRC_GNOMAD_BASE", "graphql"),
    "AlphaFold DB API": SourceConfig("AlphaFold DB API", "SRC_ALPHAFOLD_BASE", "rest"),
    "PDBe API": SourceConfig("PDBe API", "SRC_PDBE_BASE", "rest"),
    "PDBe-KB API": SourceConfig("PDBe-KB API", "SRC_PDBEKB_BASE", "rest"),
    "STRING API": SourceConfig("STRING API", "SRC_STRING_BASE", "rest"),
    "IntAct via PSICQUIC": SourceConfig("IntAct via PSICQUIC", "SRC_PSICQUIC_BASE", "rest"),
    "OmniPath API": SourceConfig("OmniPath API", "SRC_OMNIPATH_BASE", "rest"),
    "Reactome Content/Analysis APIs": SourceConfig("Reactome Content/Analysis APIs", "SRC_REACTOME_BASE", "rest"),
    "SIGNOR API": SourceConfig("SIGNOR API", "SRC_SIGNOR_BASE", "rest"),
    "QuickGO API": SourceConfig("QuickGO API", "SRC_QUICKGO_BASE", "rest"),
    "IUPHAR/Guide to Pharmacology API": SourceConfig("IUPHAR/Guide to Pharmacology API", "SRC_GTOP_BASE", "rest"),
    "ChEMBL API": SourceConfig("ChEMBL API", "SRC_CHEMBL_BASE", "rest"),
    "DGIdb GraphQL": SourceConfig("DGIdb GraphQL", "SRC_DGIDB_BASE", "graphql"),
    "DrugCentral API": SourceConfig("DrugCentral API", "SRC_DRUGCENTRAL_BASE", "rest"),
    "BindingDB API": SourceConfig("BindingDB API", "SRC_BINDINGDB_BASE", "rest"),
    "PubChem PUG-REST": SourceConfig("PubChem PUG-REST", "SRC_PUBCHEM_BASE", "rest"),
    "STITCH API": SourceConfig("STITCH API", "SRC_STITCH_BASE", "rest"),
    "GlyGen API": SourceConfig("GlyGen API", "SRC_GLYGEN_BASE", "rest"),
    "Pharos GraphQL": SourceConfig("Pharos GraphQL", "SRC_PHAROS_BASE", "graphql"),
    "IEDB IQ-API": SourceConfig("IEDB IQ-API", "SRC_IEDB_IQ_BASE", "rest"),
    "IEDB Tools API": SourceConfig("IEDB Tools API", "SRC_IEDB_TOOLS_BASE", "rest"),
    "IPD-IMGT/HLA API": SourceConfig("IPD-IMGT/HLA API", "SRC_IMGT_BASE", "rest"),
    "PharmGKB API": SourceConfig("PharmGKB API", "SRC_PHARMGKB_BASE", "rest"),
    "HPO/Monarch APIs": SourceConfig("HPO/Monarch APIs", "SRC_MONARCH_BASE", "rest"),
    "Inxight Drugs API": SourceConfig("Inxight Drugs API", "SRC_INXIGHT_BASE", "rest"),
    "openFDA FAERS API": SourceConfig("openFDA FAERS API", "SRC_OPENFDA_BASE", "rest"),
    "CTDbase API": SourceConfig("CTDbase API", "SRC_CTD_BASE", "rest"),
    "IMPC API": SourceConfig("IMPC API", "SRC_IMPC_BASE", "rest"),
    "PatentsView API": SourceConfig("PatentsView API", "SRC_PATENTSVIEW_BASE", "rest"),
    "OLS API": SourceConfig("OLS API", "SRC_OLS_BASE", "rest"),
}

def get_adapter(source_name: str):
    cfg = LIVE_SOURCE_CATALOG.get(source_name)
    if not cfg: raise RuntimeError(f"Unknown source '{source_name}'")
    return GraphQLAdapter(cfg) if cfg.kind == "graphql" else RestAdapter(cfg)

# -----------------------------------------------------------------------------
# Module registry (domains + sources)
# -----------------------------------------------------------------------------
@dataclass
class ModuleConfig:
    id: int
    key: str
    path: str
    domains: Dict[str, List[int]]
    live_sources: List[str]

def _m(i,k,p,prim,sec,sources):
    return ModuleConfig(id=i,key=k,path=p,domains={"primary":prim,"secondary":sec},live_sources=sources)

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

FAMILY_BY_MODULE: Dict[str, str] = {
    "genetics-l2g":"causal_genetics","genetics-coloc":"causal_genetics","genetics-mr":"causal_genetics",
    "genetics-consortia-summary":"causal_genetics","genetics-annotation":"causal_genetics",
    "genetics-regulatory":"regulatory_3d","genetics-3d-maps":"regulatory_3d","genetics-chromatin-contacts":"regulatory_3d",
    "genetics-sqtl":"causal_genetics","genetics-pqtl":"causal_genetics","genetics-functional":"causal_genetics",
    "genetics-rare":"causal_genetics","genetics-mendelian":"causal_genetics","genetics-pathogenicity-priors":"causal_genetics",
    "genetics-intolerance":"causal_genetics","genetics-mavedb":"causal_genetics","genetics-phewas-human-knockout":"causal_genetics",
    "expr-baseline":"expression_cell","assoc-sc":"expression_cell","assoc-spatial":"expression_cell","sc-hubmap":"expression_cell","expr-inducibility":"expression_cell",
    "assoc-bulk-rna":"expression_cell","assoc-proteomics":"proteo_metabo","assoc-metabolomics":"proteo_metabo","assoc-perturb":"proteo_metabo",
    "mech-pathways":"mechanisms","biology-causal-pathways":"mechanisms","mech-ppi":"mechanisms","mech-ligrec":"mechanisms","mech-structure":"tractability",
    "tract-ligandability-sm":"tractability","tract-ligandability-ab":"tractability","tract-ligandability-oligo":"tractability","tract-surfaceome":"tractability","tract-modality":"tractability","tract-drugs":"tractability",
    "clin-safety":"safety","clin-rwe":"safety","clin-on-target-ae-prior":"safety","immuno-hla-coverage":"safety","function-dependency":"safety","tract-immunogenicity":"safety","tract-mhc-binding":"safety","tract-iedb-epitopes":"safety",
    "clin-endpoints":"clinical","clin-feasibility":"clinical","clin-pipeline":"clinical","clin-biomarker-fit":"clinical",
    "comp-intensity":"clinical","comp-freedom":"clinical",
}

# -----------------------------------------------------------------------------
# Normalizers (env-driven) + built-ins
# -----------------------------------------------------------------------------
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
    pred = tmpl.get("predicate","related_to")
    fam = FAMILY_BY_MODULE.get(module_key)
    for row in arr:
        subj = EntityRef(type=tmpl.get("subject",{}).get("type","entity"),
                         id=_get_path(row, tmpl.get("subject",{}).get("id_path","")),
                         label=_get_path(row, tmpl.get("subject",{}).get("label_path","")))
        obj = EntityRef(type=tmpl.get("object",{}).get("type","entity"),
                        id=_get_path(row, tmpl.get("object",{}).get("id_path","")),
                        label=_get_path(row, tmpl.get("object",{}).get("label_path","")))
        ctx = {}
        for k,v in (tmpl.get("context") or {}).items():
            if k.endswith("_path"): continue
            ctx[k]=v
        for k,v in (tmpl.get("context") or {}).items():
            if k.endswith("_path"):
                ctx[k.replace("_path","")] = _get_path(row, v)
        edges.append(Edge(subject=subj, predicate=pred, object=obj, provenance={"source": source_name}, family=fam, context=ctx))
    return edges

def epmc_mentions_normalizer(payload: Dict[str, Any], module_key: str) -> List[Edge]:
    hits = (payload.get("resultList",{}) or {}).get("result", [])
    edges: List[Edge] = []
    for h in hits[:100]:
        pmid = h.get("pmid") or h.get("id")
        subj = EntityRef(type="paper", id=f"PMID:{pmid}", label=h.get("title"))
        obj = EntityRef(type="topic", label=h.get("journalTitle"))
        edges.append(Edge(subject=subj, predicate="mentions", object=obj, provenance={"source":"Europe PMC","pmid":pmid}, family="literature_claims"))
    return edges

# -----------------------------------------------------------------------------
# Builders and executor
# -----------------------------------------------------------------------------
async def _build_request(module: ModuleConfig, src: str, params: Dict[str, Any]) -> Dict[str, Any]:
    cfg = LIVE_SOURCE_CATALOG[src]
    # Special handling: PatentsView query for comp-* modules
    if src == "PatentsView API" and module.key.startswith("comp-"):
        symbol = params.get("target") or params.get("disease") or params.get("symbol") or "cancer"
        q = {"_text_any": {"patent_title": symbol}}
        flds = ["patent_number","patent_title","patent_date"]
        return {"source": src, "path": DEFAULT_PATHS.get("PatentsView API","/patents/query"), "method":"GET",
                 "params": {"q": json.dumps(q), "f": json.dumps(flds), "o": json.dumps({"per_page": params.get("limit",25)})}, "json_body": None}
    # GraphQL sources expect env query payload
    if cfg.kind == "graphql":
        env_key = os.getenv(f"QUERY_TEMPLATE_{module.key.upper().replace('-', '_').replace('/', '_')}_{src.upper().replace(' ', '_').replace('-', '_').replace('/', '_')}")
        payload = None
        if env_key:
            try:
                payload = json.loads(env_key)
            except Exception:
                pass
        if not payload:
            payload = {"query":"query { __typename }","variables":{}}
        variables = payload.get("variables", {})
        variables.update({k:v for k,v in params.items() if v is not None})
        return {"source": src, "path": "/graphql", "method": "POST", "params": None, "json_body": {"query": payload["query"], "variables": variables}}
    # REST: forward params; path from default or env
    path = os.getenv(f"SRC_{src.upper().replace(' ','_').replace('-','_').replace('/','_')}_PATH", DEFAULT_PATHS.get(src, "/"))
    method = os.getenv(f"SRC_{src.upper().replace(' ','_').replace('-','_').replace('/','_')}_METHOD", "GET")
    fwd = {k:v for k,v in params.items() if v is not None}
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
            except Exception: data = {"status_code": resp.status_code, "text": resp.text[:2000]}
            # Normalization
            edges = normalize_with_template(module.key, req["source"], data)
            if not edges and req["source"] == "Europe PMC API":
                edges = epmc_mentions_normalizer(data, module.key)
            if not edges and req["source"] == "PatentsView API":
                patents = (data.get("patents") or []) if isinstance(data, dict) else []
                symbol = params.get("target") or params.get("disease") or "topic"
                for p in patents[: params.get("limit",25)]:
                    subj = EntityRef(type="patent", id=str(p.get("patent_number")), label=p.get("patent_title"))
                    obj = EntityRef(type="topic", label=symbol)
                    edges.append(Edge(subject=subj, predicate="mentions", object=obj, provenance={"source":"PatentsView API"}, family="clinical"))
            ev = Evidence(status="OK" if resp.status_code < 400 else "ERROR", source=req["source"], fetched_n=len(edges), data=data, citations=[f"{adapter._base()}{req['path']}"])
            fam = FAMILY_BY_MODULE.get(module.key)
            for e in edges: e.family = e.family or fam
            return ModuleResult(module_key=module.key, source=req["source"], family=fam, evidence=ev, edges=edges)
        except Exception as e:
            ev = Evidence(status="ERROR", source=req["source"], fetched_n=0, data={"error": str(e)}, citations=[])
            return ModuleResult(module_key=module.key, source=req["source"], family=FAMILY_BY_MODULE.get(module.key), evidence=ev, edges=[])
    results = await asyncio.gather(*[_one(r) for r in reqs])
    errors = [r.evidence.data.get("error") for r in results if r.evidence.status == "ERROR"]
    return ModuleResponse(module=module.key, params=params, results=results, errors=errors)

# Routing helper
MODULE_MAP = {m.key:m for m in MODULES}
async def route_module(module_key: str, params: Dict[str,Any]) -> ModuleResponse:
    mod = MODULE_MAP.get(module_key)
    if not mod: raise HTTPException(status_code=404, detail=f"Unknown module '{module_key}'")
    return await execute_module(mod, params)

# -----------------------------------------------------------------------------
# Literature layer + triggers + job queue + audit
# -----------------------------------------------------------------------------
class LitScanRequest(BaseModel):
    query: str
    entity_hints: Dict[str,str] = Field(default_factory=dict)
    include_preprints: bool = True
    limit: int = 50

class LitScanResponse(BaseModel):
    claims: List[LiteratureClaim]
    triggered_modules: List[str] = Field(default_factory=list)

async def _epmc_search(query: str, limit: int, include_preprints: bool) -> Dict[str,Any]:
    adapter = get_adapter("Europe PMC API")
    path = DEFAULT_PATHS["Europe PMC API"]
    params = {"query": query, "pageSize": min(limit, 1000)}
    if not include_preprints: params["source"] = "MED"
    resp = await adapter.request(path=path, method="GET", params=params)
    try: return resp.json()
    except Exception: return {}

def _score_claim(hit: Dict[str,Any], include_preprints: bool) -> float:
    types = (hit.get("pubTypeList",{}) or {}).get("pubType", []) or []
    base = 0.6
    if "Randomized Controlled Trial" in types or "Clinical Trial" in types: base += 0.2
    if "PREPRINT" in types and not include_preprints: base -= 0.3
    try:
        year = int(hit.get("pubYear") or 0)
        if year >= 2022: base += 0.1
    except Exception: pass
    return float(max(0.0, min(1.0, base)))

async def literature_scan_and_extract(req: LitScanRequest) -> LitScanResponse:
    data = await _epmc_search(req.query, req.limit, req.include_preprints)
    hits = (data.get("resultList",{}) or {}).get("result", [])
    claims: List[LiteratureClaim] = []
    for h in hits[: req.limit]:
        pmid = h.get("pmid") or h.get("id")
        claims.append(LiteratureClaim(
            subject=EntityRef(type="paper", id=f"PMID:{pmid}", label=h.get("title")),
            predicate="mentions",
            object=EntityRef(type="topic", label=req.query),
            context={"journal": h.get("journalTitle")},
            provenance={"pmid": pmid, "source": "Europe PMC"},
            scores={"quality": _score_claim(h, req.include_preprints)},
            timestamps={"pubYear": h.get("pubYear")},
        ))
    trig = []
    ql = req.query.lower()
    if "mendelian randomization" in ql: trig += ["genetics-mr","genetics-l2g","genetics-coloc"]
    if "colocalization" in ql: trig += ["genetics-coloc","genetics-regulatory"]
    if "adverse event" in ql: trig += ["clin-safety","clin-rwe","clin-on-target-ae-prior"]
    return LitScanResponse(claims=claims, triggered_modules=sorted(set(trig)))

class Job(BaseModel):
    job_id: str
    kind: str
    payload: Dict[str,Any]
    created_at: float

JOB_QUEUE: asyncio.Queue = asyncio.Queue()
AUDIT_LOG: List[Dict[str,Any]] = []

def audit(event: str, detail: Dict[str,Any]):
    AUDIT_LOG.append({"ts": time.time(), "event": event, "detail": detail})
    if len(AUDIT_LOG) > 2000: AUDIT_LOG.pop(0)

async def enqueue_job(kind: str, payload: Dict[str,Any]) -> str:
    jid = f"{kind}-{int(time.time()*1000)}"
    await JOB_QUEUE.put(Job(job_id=jid, kind=kind, payload=payload, created_at=time.time()))
    audit("enqueue", {"job_id": jid, "kind": kind})
    return jid

async def process_one_job() -> Optional[Dict[str,Any]]:
    try:
        job: Job = await asyncio.wait_for(JOB_QUEUE.get(), timeout=0.1)
    except asyncio.TimeoutError:
        return None
    if job.kind == "rerun_modules":
        modules = job.payload.get("modules", [])
        params = job.payload.get("params", {})
        tasks = [route_module(m, params) for m in modules if m in MODULE_MAP]
        await asyncio.gather(*tasks)
        audit("rerun_done", {"job_id": job.job_id, "modules": modules})
        return {"job_id": job.job_id, "status": "ok", "modules": modules}
    audit("job_unknown", {"job_id": job.job_id, "kind": job.kind})
    return {"job_id": job.job_id, "status": "unknown"}

# -----------------------------------------------------------------------------
# Synthesis (graph math; qualitative outputs)
# -----------------------------------------------------------------------------
def _ensure_nx():
    if nx is None:
        raise HTTPException(status_code=500, detail="networkx not available")

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
    try:
        comms = nx.algorithms.community.asyn_lpa_communities(UG, weight=None)
        labels = {}; cid=0
        for c in comms:
            for n in c: labels[n]=cid
            cid+=1
        return labels
    except Exception:
        return {n:0 for n in UG.nodes()}

def steiner_like(G: "nx.MultiDiGraph", terminals: List[Tuple[str,str]], max_nodes: int = 60):
    UG = G.to_undirected()
    H = nx.Graph()
    for i in range(len(terminals)):
        for j in range(i+1, len(terminals)):
            s, t = terminals[i], terminals[j]
            try:
                p = nx.shortest_path(UG, s, t)
                for u,v in zip(p[:-1], p[1:]):
                    data = G.get_edge_data(u, v) or G.get_edge_data(v, u) or {}
                    dd = list(data.values())[0] if isinstance(data, dict) and data else {}
                    H.add_edge(u, v, **dd)
            except Exception:
                continue
    if H.number_of_nodes() > max_nodes:
        edges_sorted = sorted(H.edges(data=True), key=lambda x: x[2].get("weight", 0.0), reverse=True)
        H2 = nx.Graph()
        for u,v,d in edges_sorted[:max_nodes]: H2.add_edge(u,v,**d)
        H = H2
    return list(H.edges(data=True))

def detect_orthogonal_links(G: "nx.MultiDiGraph") -> List[Edge]:
    UG = G.to_undirected()
    comm = communities(G)
    out: List[Edge] = []
    for u,v,d in G.edges(data=True):
        cu, cv = comm.get(u,0), comm.get(v,0)
        if cu != cv or (G.nodes[u].get("family") != G.nodes[v].get("family")):
            out.append(Edge(subject=EntityRef(type=u[0], id=u[1]), predicate=d.get("predicate","related_to"), object=EntityRef(type=v[0], id=v[1]), provenance=d.get("provenance",{}), context=d.get("context",{}), family=d.get("family")))
        if len(out) >= 10: break
    return out

def build_insight_cards(all_edges: List[Edge], terminals: List[Tuple[str,str]], title: str) -> List[InsightCard]:
    G = edges_to_graph(all_edges)
    steiner_edges = steiner_like(G, terminals, max_nodes=60)
    path_edges: List[Edge] = []
    for u,v,d in steiner_edges:
        path_edges.append(Edge(subject=EntityRef(type=u[0], id=u[1]), predicate=d.get("predicate","related_to"), object=EntityRef(type=v[0], id=v[1]), provenance=d.get("provenance",{}), context=d.get("context",{}), family=d.get("family"), weight=d.get("weight",0.1)))
    orth = detect_orthogonal_links(G)
    return [InsightCard(title=title, path_edges=path_edges, orthogonal_links=orth, notes="Minimal explanation subgraph; numbers kept internal.")]

# Domain playbooks
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

class CrossDomainRequest(BaseModel):
    target: Optional[str] = None
    disease: Optional[str] = None
    include_domains: Optional[List[int]] = None
    limit_per_module: int = 40

class CrossDomainResponse(BaseModel):
    storylines: List[Storyline]
    used_modules: List[str]

# -----------------------------------------------------------------------------
# FastAPI router
# -----------------------------------------------------------------------------
router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True, "version": "v9", "decorated_endpoints": 55}

@router.get("/modules")
async def list_modules():
    return [{"id": m.id, "key": m.key, "path": m.path, "domains": m.domains, "sources": m.live_sources} for m in MODULES]

@router.get("/config")
async def get_config():
    src_status = {name: bool(os.getenv(cfg.base_env) or DEFAULT_BASES.get(cfg.base_env)) for name, cfg in LIVE_SOURCE_CATALOG.items()}
    return {"sources_configured": src_status, "domain_playbooks": DOMAIN_PLAYBOOKS}

@router.post("/literature/scan", response_model=LitScanResponse)
async def literature_scan(req: LitScanRequest) -> LitScanResponse:
    res = await literature_scan_and_extract(req)
    audit("literature_scan", {"query": req.query, "claims": len(res.claims), "triggered": res.triggered_modules})
    return res

class LitTriggerRequest(LitScanRequest):
    run_triggers: bool = True
class LitTriggerResponse(BaseModel):
    claims: List[LiteratureClaim]
    triggered_modules: List[str]
    jobs_enqueued: List[str]

@router.post("/literature/scan-and-trigger", response_model=LitTriggerResponse)
async def literature_scan_and_trigger(req: LitTriggerRequest) -> LitTriggerResponse:
    base = await literature_scan_and_extract(req)
    jobs = []
    if req.run_triggers and base.triggered_modules:
        jid = await enqueue_job("rerun_modules", {"modules": base.triggered_modules, "params": req.entity_hints})
        jobs.append(jid)
    audit("literature_trigger", {"query": req.query, "modules": base.triggered_modules})
    return LitTriggerResponse(claims=base.claims, triggered_modules=base.triggered_modules, jobs_enqueued=jobs)

@router.post("/synthesis/domain", response_model=DomainSynthesisResponse)
async def synthesize_domain(req: DomainSynthesisRequest) -> DomainSynthesisResponse:
    used_modules = req.modules or DOMAIN_PLAYBOOKS.get(req.domain, [])
    if not used_modules: raise HTTPException(status_code=400, detail=f"No modules mapped for domain {req.domain}")
    params = {"target": req.target, "disease": req.disease, "tissue": req.tissue, "variant": req.variant, "celltype": req.celltype, "limit": req.limit_per_module}
    tasks = [route_module(mk, params) for mk in used_modules if mk in MODULE_MAP]
    mod_resps = await asyncio.gather(*tasks)
    edges: List[Edge] = []
    for mr in mod_resps:
        for r in mr.results: edges.extend(r.edges)
    terminals: List[Tuple[str,str]] = []
    if req.target: terminals.append(("gene", req.target))
    if req.disease: terminals.append(("disease", req.disease))
    if not terminals and edges:
        seen=set()
        for e in edges:
            s=(e.subject.type, e.subject.id or e.subject.label); o=(e.object.type, e.object.id or e.object.label)
            if s not in seen: terminals.append(s); seen.add(s)
            if o not in seen: terminals.append(o); seen.add(o)
            if len(terminals)>=2: break
    cards = build_insight_cards(edges, terminals, title=f"Domain {req.domain} synthesis")
    audit("synthesis_domain", {"domain": req.domain, "edges": len(edges)})
    return DomainSynthesisResponse(cards=cards, used_modules=used_modules)

@router.post("/synthesis/cross", response_model=CrossDomainResponse)
async def synthesize_cross(req: CrossDomainRequest) -> CrossDomainResponse:
    domains = req.include_domains or [1,2,3,4,5,6]
    used_modules = sorted(set([mk for d in domains for mk in DOMAIN_PLAYBOOKS.get(d,[])]))
    params = {"target": req.target, "disease": req.disease, "limit": req.limit_per_module}
    tasks = [route_module(mk, params) for mk in used_modules if mk in MODULE_MAP]
    mod_resps = await asyncio.gather(*tasks)
    edges: List[Edge] = []
    for mr in mod_resps:
        for r in mr.results: edges.extend(r.edges)
    terminals: List[Tuple[str,str]] = []
    if req.target: terminals.append(("gene", req.target))
    if req.disease: terminals.append(("disease", req.disease))
    if not terminals and edges:
        deg = {}
        for e in edges:
            s=(e.subject.type, e.subject.id or e.subject.label); o=(e.object.type, e.object.id or e.object.label)
            deg[s]=deg.get(s,0)+1; deg[o]=deg.get(o,0)+1
        terminals = sorted(deg, key=deg.get, reverse=True)[:2]
    cards = build_insight_cards(edges, terminals, title="Cross-domain storyline")
    storylines = [Storyline(title=c.title, path_edges=c.path_edges, notes=c.notes) for c in cards]
    audit("synthesis_cross", {"domains": domains, "edges": len(edges)})
    return CrossDomainResponse(storylines=storylines, used_modules=used_modules)

@router.post("/admin/jobs/process-one")
async def process_one():
    res = await process_one_job()
    return res or {"status": "empty"}

@router.get("/admin/audit")
async def get_audit(limit: int = 200):
    return AUDIT_LOG[-limit:]

# ------------- 55 decorated module endpoints (explicit) ----------------------

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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("expr-baseline", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("expr-localization", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("expr-inducibility", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-bulk-rna", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-sc", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-spatial", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("sc-hubmap", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-proteomics", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-metabolomics", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-hpa-pathology", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("assoc-perturb", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-l2g", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-coloc", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-mr", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-rare", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-mendelian", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-phewas-human-knockout", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-sqtl", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-pqtl", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-chromatin-contacts", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-3d-maps", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-regulatory", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-annotation", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-consortia-summary", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-functional", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-mavedb", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-lncrna", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-mirna", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-pathogenicity-priors", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("genetics-intolerance", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("mech-structure", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("mech-ppi", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("mech-pathways", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("mech-ligrec", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("biology-causal-pathways", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-drugs", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-ligandability-sm", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-ligandability-ab", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-ligandability-oligo", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-modality", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-immunogenicity", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-mhc-binding", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-iedb-epitopes", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("tract-surfaceome", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("function-dependency", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("immuno-hla-coverage", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-endpoints", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-biomarker-fit", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-pipeline", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-safety", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-rwe", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-on-target-ae-prior", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("clin-feasibility", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("comp-intensity", params)


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
    params = {"target": target, "disease": disease, "tissue": tissue, "variant": variant, "celltype": celltype, "limit": limit, "refresh": refresh}
    return await route_module("comp-freedom", params)


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(title="DrugHunter Router v9", version="1.0.0")
app.include_router(router, prefix="/api")
