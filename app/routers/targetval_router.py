
"""
Targetval Gateway — Router (Full, Extended) for 22 Questions
Keyless, Live v2 — implements full module registry, HTTP discipline,
directive literature seek, event-driven iteration hooks, and math synthesis
per the Word configuration.

Usage:
    from targetval_router_full_v2 import router as targetval_router

Exposes:
    POST /targetval/run
        body: {
            gene_symbol?, ensembl_id?, uniprot_id?, trait_efo?, trait_label?,
            rsid?, region?, run_questions?[1..22], strict_human?=true
        }

This router executes Q1→Q22 serially, only calling the modules mapped to
each question before proceeding to the next. After each module burst it runs
Directive Literature Seek and applies math synthesis (internal, scoreless).
"""

from __future__ import annotations

# --- Pydantic-safe typing bootstrap (must be first) ---
import typing as _typing, builtins as _builtins
for _n in ("Any","Dict","List","Optional","Union","Tuple","Mapping","Sequence","Literal","Set","Type"):
    try:
        setattr(_builtins, _n, getattr(_typing, _n))
    except Exception:
        pass
# ------------------------------------------------------

import asyncio
import datetime as dt
import hashlib
import json
import re
import os
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple
from enum import Enum

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

# ============================================================================
# Router & Runtime Policy (per config)
# ============================================================================

router = APIRouter(prefix="/targetval", tags=["Targetval Gateway (Full 22Q)"])

HTTP_CONNECT_TIMEOUT = 6.0
HTTP_TOTAL_TIMEOUT = 12.0
HTTP_READ_TIMEOUT = 12.0
HTTP_WRITE_TIMEOUT = 12.0
HTTP_RETRY_ATTEMPTS = 5
HTTP_RETRY_BASE = 0.6
GLOBAL_CONCURRENCY = 24

# TTL classes (seconds)
TTL_FAST = 6 * 3600
TTL_MODERATE = 24 * 3600
TTL_SLOW = 7 * 24 * 3600

# ----------------------------------------------------------------------------
# Minimal in-memory TTL cache
# ----------------------------------------------------------------------------

class _TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        v = self._store.get(key)
        if not v:
            return None
        exp, data = v
        if dt.datetime.now().timestamp() > exp:
            self._store.pop(key, None)
            return None
        return data

    def set(self, key: str, data: Any, ttl_seconds: int) -> None:
        self._store[key] = (dt.datetime.now().timestamp() + ttl_seconds, data)

CACHE = _TTLCache()

# ----------------------------------------------------------------------------
# Allowlist (strict). Any non-listed host is denied at request time.
# ----------------------------------------------------------------------------

HOST_ALLOW = {
    # OpenTargets / OpenGWAS / GWAS Catalog
    "api.opentargets.io",
    "gwas-api.mrcieu.ac.uk",
    "www.ebi.ac.uk",
    "www.ebi.ac.uk",
    "rest.ensembl.org",
    # ENCODE / 4DN / UCSC
    "www.encodeproject.org",
    "data.4dnucleome.org",
    # STRING / Reactome / SIGNOR / OmniPath
    "string-db.org",
    "reactome.org",
    "signor.uniroma2.it",
    "www.omnipathdb.org",
    # UniProt / PDBe / AlphaFold
    "rest.uniprot.org",
    "alphafold.ebi.ac.uk",
    "www.ebi.ac.uk",
    # PRIDE / ProteomicsDB / PDC
    "www.ebi.ac.uk",
    "www.proteomicsdb.org",
    "pdc.cancer.gov",
    # Europe PMC / PubMed / Crossref / Unpaywall / OpenAlex / SemanticScholar / PubTator3
    "www.ebi.ac.uk",
    "eutils.ncbi.nlm.nih.gov",
    "api.crossref.org",
    "api.unpaywall.org",
    "api.openalex.org",
    "api.semanticscholar.org",
    "www.ncbi.nlm.nih.gov",
    # LINCS / iLINCS
    "lincsportal.ccs.miami.edu",
    "www.ilincs.org",
    # CellxGene / HCA / HuBMAP
    "api.cellxgene.cziscience.com",
    "azul.data.humancellatlas.org",
    "data.humancellatlas.org",
    "search.api.hubmapconsortium.org",
    "hubmapconsortium.org",
    # ArrayExpress / BioStudies / Expression Atlas
    "www.ebi.ac.uk",
    # IEDB / IPD-IMGT
    "www.iedb.org",
    "tools-api.iedb.org",
    "www.ebi.ac.uk",
    "www.ipd-imgt.org",
    # DrugCentral / ChEMBL / BindingDB / PubChem / STITCH / Pharos
    "drugcentral.org",
    "www.ebi.ac.uk",
    "www.bindingdb.org",
    "pubchem.ncbi.nlm.nih.gov",
    "stitch.embl.de",
    "pharos.nih.gov",
    # PharmacoDB / CellMiner
    "api.pharmacodb.net",
    "discover.nci.nih.gov",
    # FAERS
    "api.fda.gov",
    # ClinicalTrials.gov / WHO ICTRP
    "clinicaltrials.gov",
    "www.who.int",
    # HPO / Monarch
    "hpo.jax.org",
    "api.monarchinitiative.org",
    # PatentsView
    "api.patentsview.org",
    # RNAcentral
    "rnacentral.org",
}

# ----------------------------------------------------------------------------
# Host-specific overrides & circuit breaker (simple)
# ----------------------------------------------------------------------------
HOST_OVERRIDES: Dict[str, Dict[str, Any]] = {}
try:
    _env_ho = os.getenv("HOST_OVERRIDES_JSON")
    if _env_ho:
        HOST_OVERRIDES.update(json.loads(_env_ho))
except Exception:
    pass

_host_state: Dict[str, Dict[str, Any]] = {}
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_OPEN_SECONDS = float(os.getenv("CB_OPEN_SECONDS", "30"))

def _get_host_limits(host: str) -> Dict[str, Any]:
    limits = {
        "semaphore": 4,
        "connect_timeout": HTTP_CONNECT_TIMEOUT,
        "read_timeout": HTTP_READ_TIMEOUT,
        "write_timeout": HTTP_WRITE_TIMEOUT,
        "retry_attempts": HTTP_RETRY_ATTEMPTS,
    }
    ov = HOST_OVERRIDES.get(host) or {}
    limits.update({k: v for k, v in ov.items() if v is not None})
    return limits

def _now_ts() -> float:
    return dt.datetime.now().timestamp()

def _cb_is_open(host: str) -> bool:
    st = _host_state.get(host) or {}
    return st.get("open_until", 0) > _now_ts()

def _cb_on_success(host: str) -> None:
    st = _host_state.setdefault(host, {})
    st["fails"] = 0
    st["recent_429"] = max(0, int(st.get("recent_429", 0)) - 1)
    st["open_until"] = 0

def _cb_on_failure(host: str, was_429: bool=False) -> None:
    st = _host_state.setdefault(host, {})
    st["fails"] = int(st.get("fails", 0)) + 1
    if was_429:
        st["recent_429"] = int(st.get("recent_429", 0)) + 1
    if st["fails"] >= CB_FAILURE_THRESHOLD:
        st["open_until"] = _now_ts() + CB_OPEN_SECONDS

def _adaptive_retry_base(host: str) -> float:
    # allow per-host override of base backoff; otherwise adapt to recent 429s
    ov = HOST_OVERRIDES.get(host) or {}
    try:
        base = float(ov.get("retry_base", HTTP_RETRY_BASE))
    except Exception:
        base = HTTP_RETRY_BASE
    st = _host_state.get(host) or {}
    r429 = int(st.get("recent_429", 0))
    if r429 > 0:
        base = base * min(3.0, 1.0 + 0.5 * r429)
    return base
0 + 0.5 * r429)
    return base


_host_semaphores: Dict[str, asyncio.Semaphore] = {}
_global_semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY)

def _host_from_url(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0]

def _get_host_sem(url: str) -> asyncio.Semaphore:
    host = _host_from_url(url)
    # Derive allowlist lazily from module registry to avoid duplication/drift
    global HOST_ALLOW
    if not HOST_ALLOW:
        try:
            derived = set()
            for _m in MODULES.values():
                try:
                    if isinstance(_m.primary, dict):
                        u = _m.primary.get("url","")
                        if u:
                            derived.add(_host_from_url(u))
                    for fb in (_m.fallbacks or []):
                        u2 = fb.get("url","")
                        if u2:
                            derived.add(_host_from_url(u2))
                except Exception:
                    continue
            HOST_ALLOW = derived
        except Exception:
            pass
    if host not in HOST_ALLOW:
        raise HTTPException(status_code=400, detail=f"Host not allowlisted: {host}")
    limits = _get_host_limits(host)
    if host not in _host_semaphores:
        _host_semaphores[host] = asyncio.Semaphore(int(limits.get("semaphore", 4)))
    return _host_semaphores[host]

# ============================================================================
# Schemas (Pydantic v2 style)
# ============================================================================


class EvidenceStatus(str, Enum):
    OK = "OK"
    NO_DATA = "NO_DATA"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"

class Edge(BaseModel):

    kind: str
    src: str | None = None
    dst: str | None = None
    meta: Dict[str, Any] = {}

class Provenance(BaseModel):
    sources: List[str] = []
    query: Dict[str, Any] = {}
    accessed_at: str = Field(default_factory=lambda: dt.datetime.utcnow().isoformat())
    version: str = "v2"
    module_order: List[str] = []
    warnings: List[str] = []

class EnvelopeContext(BaseModel):
    gene_id: str | None = None
    target: str | None = None
    trait_id: str | None = None
    variant_id: str | None = None
    tissue: str | None = None
    cell_type: str | None = None
    assay: str | None = None
    qtl_type: str | None = None  # cis-QTL / trans-QTL
    qtl_stage: str | None = None  # E0..E6

class EvidenceEnvelope(BaseModel):
    module: str
    context: EnvelopeContext
    records: List[Dict[str, Any]] = []
    edges: List[Edge] = []
    provenance: Provenance = Field(default_factory=Provenance)
    notes: List[str] = []
    status: EvidenceStatus = EvidenceStatus.OK
    error: Optional[str] = None

class LitEvidence(BaseModel):
    query: str
    hits: List[Dict[str, Any]]
    tags: Dict[str, List[str]]
    errors: List[str] = []

class QuestionAnswer(BaseModel):
    question_id: int
    title: str
    envelopes: List[EvidenceEnvelope]
    literature: LitEvidence | None = None
    math_summary: Dict[str, Any] | None = None
    skeptic_view: List[str] = []
    killer_experiments: List[str] = []

class RunRequest(BaseModel):
    gene_symbol: str | None = None
    ensembl_id: str | None = None
    uniprot_id: str | None = None
    trait_efo: str | None = None
    trait_label: str | None = None
    rsid: str | None = None
    region: str | None = None
    run_questions: List[int] | None = None  # default 1..22
    strict_human: bool = True

class FinalRecommendation(BaseModel):
    decision: str
    why: List[str]
    modality_plan: Dict[str, Any]
    pd_plan: Dict[str, Any]
    risk_register: Dict[str, Any]
    mos_plan: Dict[str, Any]
    fastest_pom: Dict[str, Any]
    next_experiments: List[str]
    differentiation: Dict[str, Any] | None = None

class RunResponse(BaseModel):
    context: Dict[str, Any]
    questions: List[QuestionAnswer]
    final_recommendation: FinalRecommendation

# ============================================================================
# Identifier expansion & Query Planner
# ============================================================================

def _normalize_gene(req: RunRequest) -> Dict[str, str]:
    return {"hgnc": req.gene_symbol or "", "ensg": req.ensembl_id or "", "uniprot": req.uniprot_id or ""}

def _normalize_trait(req: RunRequest) -> Dict[str, str]:
    return {"efo": req.trait_efo or "", "trait_label": req.trait_label or ""}

def _normalize_variant(req: RunRequest) -> Dict[str, str]:
    return {"rsid": req.rsid or "", "region": req.region or ""}

# ============================================================================
# Module registry (full set per config)
# ============================================================================

@dataclass
class ModuleCfg:
    key: str
    family: str         # graphql | rest
    primary: Dict[str, Any]
    fallbacks: List[Dict[str, Any]]
    ttl: int = TTL_MODERATE
    qtl_stage: Optional[str] = None

# Endpoint bases
OPEN_TARGETS_GQL = "https://api.opentargets.io/v3/platform/graphql"
OPENGWAS_BASE = "https://gwas-api.mrcieu.ac.uk"
ENSEMBL_VEP = "https://rest.ensembl.org/vep/human/region"
ENCODE_BASE = "https://www.encodeproject.org"
FOURDN_BASE = "https://data.4dnucleome.org"
GTEX_BASE = "https://gtexportal.org/rest/v1"
STRING_BASE = "https://string-db.org/api"
REACTOME = "https://reactome.org/AnalysisService"
OMNIPATH = "https://www.omnipathdb.org/interactions"
SIGNOR = "https://signor.uniroma2.it/ws"
UNIPROT = "https://rest.uniprot.org"
ALPHAFOLD = "https://alphafold.ebi.ac.uk/api"
PDBe = "https://www.ebi.ac.uk/pdbe/api"
PRIDE = "https://www.ebi.ac.uk/pride/ws/archive"
PROTEOMICSDB = "https://www.proteomicsdb.org/proxy"
PDC = "https://pdc.cancer.gov/graphql"
ILINCS = "https://www.ilincs.org/api"
LINCS_LDP3 = "https://lincsportal.ccs.miami.edu/api"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
OPENALEX = "https://api.openalex.org/works"
SEM_SCH = "https://api.semanticscholar.org/graph/v1/paper"
PUBTATOR3 = "https://www.ncbi.nlm.nih.gov/research/pubtator3/publications/export/biocjson"
FAERS = "https://api.fda.gov/drug/event.json"
DRUGCENTRAL = "https://drugcentral.org/api"
CLINTRIALS = "https://clinicaltrials.gov/api/v2/studies"
ICTRP = "https://www.who.int/clinical-trials-registry-platform"
HPO = "https://hpo.jax.org/api/hpo"
MONARCH = "https://api.monarchinitiative.org/api"
PATENTSVIEW = "https://api.patentsview.org/patents"
GLYGEN = "https://glygen.org/api"
PHAROS = "https://pharos.nih.gov/api/graphql"
BINDINGDB = "https://www.bindingdb.org"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
STITCH = "https://stitch.embl.de"
RNCENTRAL = "https://rnacentral.org/api"

def _mod(key, family, primary, fallbacks, ttl=TTL_MODERATE, qtl_stage=None) -> ModuleCfg:
    return ModuleCfg(key=key, family=family, primary=primary, fallbacks=fallbacks, ttl=ttl, qtl_stage=qtl_stage)

# Selected GraphQL templates
GQL_TEMPLATES = {
    "genetics-l2g": """
query L2G($ensg: String!, $efo: String) {
  gene(ensemblId: $ensg) {
    id
    geneticAssociations(efos: [$efo]) {
      rows { id, score, studyId, efoId }
    }
  }
}""",
    "genetics-coloc": """
query Coloc($ensg: String!, $efo: String) {
  gene(ensemblId: $ensg) {
    id
    colocalisations(efos: [$efo]) {
      rows { locusId, efoId, pp4, tissue, qtlType }
    }
  }
}""",
    "genetics-pqtl": """
query PQTL($ensg: String!) {
  gene(ensemblId: $ensg) { id pqtl { rows { studyId, efoId, pp4 } } }
}""",
    "genetics-mqtl-coloc": """
query MQTL($ensg: String!, $efo: String) {
  gene(ensemblId: $ensg) {
    id
    metaboliteColoc(efos: [$efo]) { rows { metabolite, pp4, studyId } }
  }
}""",
}

MODULES: Dict[str, ModuleCfg] = {}

def _register_modules():
    M = MODULES
    # --- Genetics & QTL ---
    M["genetics-l2g"] = _mod("genetics-l2g","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-l2g"},[{"url":"https://www.ebi.ac.uk/gwas/rest/api"}],TTL_FAST,"E6")
    M["genetics-coloc"] = _mod("genetics-coloc","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-coloc"},[{"url":OPENGWAS_BASE}],TTL_FAST,"E1/E6")
    M["genetics-mr"] = _mod("genetics-mr","rest",{"url":f"{OPENGWAS_BASE}/mr","method":"GET"},[{"url":f"{OPENGWAS_BASE}/"}],TTL_FAST,"E1/E6")
    M["genetics-chromatin-contacts"] = _mod("genetics-chromatin-contacts","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[{"url":FOURDN_BASE}],TTL_MODERATE,"E0")
    M["genetics-3d-maps"] = _mod("genetics-3d-maps","rest",{"url":f"{FOURDN_BASE}/search/","method":"GET"},[{"url":f"{ENCODE_BASE}/search/"}],TTL_MODERATE,"E0")
    M["genetics-regulatory"] = _mod("genetics-regulatory","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[{"url":"https://www.ebi.ac.uk/eqtl/api"}],TTL_MODERATE,"E0")
    M["genetics-sqtl"] = _mod("genetics-sqtl","rest",{"url":f"{GTEX_BASE}/association/singleTissueEqtl","method":"GET"},[{"url":"https://www.ebi.ac.uk/eqtl/api"}],TTL_FAST,"E2")
    M["genetics-pqtl"] = _mod("genetics-pqtl","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-pqtl"},[{"url":OPENGWAS_BASE}],TTL_FAST,"E3")
    M["genetics-annotation"] = _mod("genetics-annotation","rest",{"url":ENSEMBL_VEP,"method":"POST"},[{"url":"https://cadd.gs.washington.edu/api"}],TTL_MODERATE,"E0/E2")
    M["genetics-pathogenicity-priors"] = _mod("genetics-pathogenicity-priors","rest",{"url":"https://gnomad.broadinstitute.org/api","method":"POST"},[{"url":"https://cadd.gs.washington.edu/api"}],TTL_SLOW,"E0")
    M["genetics-intolerance"] = _mod("genetics-intolerance","rest",{"url":"https://gnomad.broadinstitute.org/api","method":"POST"},[],TTL_SLOW,"E0")
    M["genetics-rare"] = _mod("genetics-rare","rest",{"url":f"{PUBMED_EUTILS}/esearch.fcgi","method":"GET"},[{"url":"https://myvariant.info/v1"}],TTL_SLOW,"E6")
    M["genetics-mendelian"] = _mod("genetics-mendelian","rest",{"url":"https://clinicalgenome.org/graphql","method":"POST"},[],TTL_SLOW,"E6")
    M["genetics-phewas-human-knockout"] = _mod("genetics-phewas-human-knockout","rest",{"url":f"{OPENGWAS_BASE}/phew","method":"GET"},[{"url":"https://www.ebi.ac.uk/gwas/rest/api"}],TTL_FAST,"E6")
    M["genetics-functional"] = _mod("genetics-functional","rest",{"url":"https://webservice.thebiogrid.org/ORCS","method":"GET"},[{"url":EPMC_SEARCH}],TTL_MODERATE,"E4")
    M["genetics-mavedb"] = _mod("genetics-mavedb","rest",{"url":"https://api.mavedb.org/graphql","method":"POST"},[],TTL_SLOW,"E4")
    M["genetics-consortia-summary"] = _mod("genetics-consortia-summary","rest",{"url":f"{OPENGWAS_BASE}/studies","method":"GET"},[],TTL_MODERATE,"E6")
    M["genetics-caqtl-lite"] = _mod("genetics-caqtl-lite","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[],TTL_MODERATE,"E0")
    M["genetics-nmd-inference"] = _mod("genetics-nmd-inference","rest",{"url":ENSEMBL_VEP,"method":"POST"},[],TTL_MODERATE,"E2")
    M["genetics-mqtl-coloc"] = _mod("genetics-mqtl-coloc","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-mqtl-coloc"},[{"url":OPENGWAS_BASE}],TTL_FAST,"E5")
    M["genetics-ase-check"] = _mod("genetics-ase-check","rest",{"url":EPMC_SEARCH,"method":"GET"},[],TTL_MODERATE,"E1")
    M["genetics-ptm-signal-lite"] = _mod("genetics-ptm-signal-lite","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":PRIDE}],TTL_MODERATE,"E3")
    M["genetics-lncrna"] = _mod("genetics-lncrna","rest",{"url":f"{RNCENTRAL}/v1/rna","method":"GET"},[{"url":EPMC_SEARCH}],TTL_MODERATE,"E0/E1/E2")
    M["genetics-mirna"] = _mod("genetics-mirna","rest",{"url":f"{RNCENTRAL}/v1/rna","method":"GET"},[{"url":EPMC_SEARCH}],TTL_MODERATE,"E2")

    # --- Mechanism & Perturbation ---
    M["mech-ppi"] = _mod("mech-ppi","rest",{"url":f"{STRING_BASE}/json/network","method":"GET"},[{"url":"https://www.ebi.ac.uk/Tools/webservices/psicquic"}],TTL_MODERATE,"E4")
    M["mech-pathways"] = _mod("mech-pathways","rest",{"url":f"{REACTOME}/token","method":"POST"},[{"url":"https://www.pathwaycommons.org/pc2"}],TTL_MODERATE,"E4/E5")
    M["mech-ligrec"] = _mod("mech-ligrec","rest",{"url":OMNIPATH,"method":"GET"},[{"url":"https://www.guidetopharmacology.org"}],TTL_MODERATE,"E4")
    M["biology-causal-pathways"] = _mod("biology-causal-pathways","rest",{"url":f"{REACTOME}/token","method":"POST"},[{"url":SIGNOR}],TTL_MODERATE,"E4/E5")
    M["mech-directed-signaling"] = _mod("mech-directed-signaling","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E4")
    M["mech-kinase-substrate"] = _mod("mech-kinase-substrate","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E3/E4")
    M["mech-tf-target"] = _mod("mech-tf-target","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E1/E4")
    M["mech-mirna-target"] = _mod("mech-mirna-target","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E2/E4")
    M["mech-complexes"] = _mod("mech-complexes","rest",{"url":"https://www.ebi.ac.uk/complexportal/api/complex","method":"GET"},[{"url":"https://www.omnipathdb.org/complexes"}],TTL_SLOW,"E4")

    M["assoc-perturb"] = _mod("assoc-perturb","rest",{"url":EPMC_SEARCH,"method":"GET"},[{"url":f"{LINCS_LDP3}/signatures"}],TTL_MODERATE,"E4/E5")
    M["perturb-crispr-screens"] = _mod("perturb-crispr-screens","rest",{"url":"https://webservice.thebiogrid.org/ORCS","method":"GET"},[{"url":"https://cellmodelpassports.sanger.ac.uk/api"}],TTL_MODERATE,"E4")
    M["perturb-lincs-signatures"] = _mod("perturb-lincs-signatures","rest",{"url":f"{LINCS_LDP3}/signatures","method":"GET"},[{"url":ILINCS}],TTL_MODERATE,"E4/E5")
    M["perturb-signature-enrichment"] = _mod("perturb-signature-enrichment","rest",{"url":f"{ILINCS}/SignatureMeta","method":"GET"},[{"url":f"{LINCS_LDP3}/signatures"}],TTL_MODERATE,"E4/E5")
    M["perturb-connectivity"] = _mod("perturb-connectivity","rest",{"url":f"{ILINCS}/Connectivity","method":"GET"},[{"url":f"{LINCS_LDP3}/signatures"}],TTL_MODERATE,"E4/E5")
    M["perturb-perturbseq-encode"] = _mod("perturb-perturbseq-encode","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[{"url":"https://www.ebi.ac.uk/ena/browser/api"}],TTL_MODERATE,"E4")

    # --- Expression & Context ---
    M["expr-baseline"] = _mod("expr-baseline","rest",{"url":f"{GTEX_BASE}/gene/expression","method":"GET"},[{"url":"https://www.ebi.ac.uk/gxa/api/genes"}],TTL_MODERATE,"E3")
    M["assoc-sc"] = _mod("assoc-sc","rest",{"url":"https://azul.data.humancellatlas.org/index/projects","method":"GET"},[{"url":"https://api.cellxgene.cziscience.com/dp/v1/discover/datasets"}],TTL_MODERATE,"E3")
    M["sc-hubmap"] = _mod("sc-hubmap","rest",{"url":"https://search.api.hubmapconsortium.org/portal/search","method":"GET"},[],TTL_MODERATE,"E3")
    M["assoc-spatial"] = _mod("assoc-spatial","rest",{"url":EPMC_SEARCH,"method":"GET"},[],TTL_MODERATE,"E3/E4")
    M["expr-localization"] = _mod("expr-localization","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[],TTL_MODERATE,"E3")
    M["assoc-bulk-prot"] = _mod("assoc-bulk-prot","rest",{"url":f"{PROTEOMICSDB}/v2/proteinexpression","method":"GET"},[{"url":PDC}],TTL_MODERATE,"E3")
    M["assoc-omics-phosphoproteomics"] = _mod("assoc-omics-phosphoproteomics","rest",{"url":f"{PRIDE}/project/list","method":"GET"},[],TTL_MODERATE,"E3")
    M["expr-inducibility"] = _mod("expr-inducibility","rest",{"url":"https://www.ebi.ac.uk/gxa/json/experiments","method":"GET"},[{"url":"https://www.ebi.ac.uk/biostudies/api/v1"}],TTL_MODERATE,"E3")
    M["assoc-bulk-rna"] = _mod("assoc-bulk-rna","rest",{"url":f"{PUBMED_EUTILS}/esearch.fcgi","method":"GET"},[{"url":"https://www.ebi.ac.uk/biostudies/api/v1"}],TTL_MODERATE,None)
    M["assoc-metabolomics"] = _mod("assoc-metabolomics","rest",{"url":"https://www.ebi.ac.uk/metabolights/ws/studies","method":"GET"},[{"url":"https://www.metabolomicsworkbench.org/rest"}],TTL_MODERATE,"E5")
    M["assoc-proteomics"] = _mod("assoc-proteomics","rest",{"url":f"{PROTEOMICSDB}/v2/proteins","method":"GET"},[{"url":PRIDE},{"url":PDC}],TTL_MODERATE,"E3")
    M["assoc-hpa-pathology"] = _mod("assoc-hpa-pathology","rest",{"url":EPMC_SEARCH,"method":"GET"},[],TTL_MODERATE,"E3")

    # --- Tractability & Modality ---
    M["mech-structure"] = _mod("mech-structure","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":ALPHAFOLD},{"url":PDBe}],TTL_SLOW,"E4")
    M["tract-ligandability-sm"] = _mod("tract-ligandability-sm","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":PDBe},{"url":BINDINGDB}],TTL_SLOW,None)
    M["tract-ligandability-ab"] = _mod("tract-ligandability-ab","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":GLYGEN}],TTL_SLOW,None)
    M["tract-surfaceome"] = _mod("tract-surfaceome","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":GLYGEN}],TTL_SLOW,None)
    M["tract-ligandability-oligo"] = _mod("tract-ligandability-oligo","rest",{"url":ENSEMBL_VEP,"method":"POST"},[{"url":"https://rnacentral.org/api"}],TTL_MODERATE,None)
    M["tract-modality"] = _mod("tract-modality","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":PHAROS}],TTL_SLOW,None)
    M["tract-drugs"] = _mod("tract-drugs","rest",{"url":f"{CHEMBL}/mechanism","method":"GET"},[{"url":DRUGCENTRAL},{"url":BINDINGDB},{"url":f"{PUBCHEM}/"}],TTL_SLOW,None)
    M["perturb-drug-response"] = _mod("perturb-drug-response","rest",{"url":"https://api.pharmacodb.net/v2/experiments","method":"GET"},[{"url":"https://discover.nci.nih.gov/cellminerapi"}],TTL_MODERATE,None)

    # --- Safety & Clinical ---
    M["tract-immunogenicity"] = _mod("tract-immunogenicity","rest",{"url":EPMC_SEARCH,"method":"GET"},[{"url":"https://tools-api.iedb.org/population"}],TTL_MODERATE,"E4")
    M["tract-mhc-binding"] = _mod("tract-mhc-binding","rest",{"url":"https://tools-api.iedb.org/mhci/consensus","method":"POST"},[{"url":"https://tools-api.iedb.org/mhcii/consensus"}],TTL_FAST,"E4")
    M["tract-iedb-epitopes"] = _mod("tract-iedb-epitopes","rest",{"url":"https://www.iedb.org/api","method":"GET"},[],TTL_SLOW,"E4")
    M["immuno/hla-coverage"] = _mod("immuno/hla-coverage","rest",{"url":"https://www.ebi.ac.uk/ipd/api","method":"GET"},[{"url":"https://tools-api.iedb.org/population"}],TTL_SLOW,"E4")
    M["function-dependency"] = _mod("function-dependency","rest",{"url":"https://webservice.thebiogrid.org/ORCS","method":"GET"},[{"url":"https://cellmodelpassports.sanger.ac.uk/api"}],TTL_MODERATE,"E4")
    M["perturb-depmap-dependency"] = _mod("perturb-depmap-dependency","rest",{"url":"https://cellmodelpassports.sanger.ac.uk/api","method":"GET"},[{"url":"https://webservice.thebiogrid.org/ORCS"}],TTL_MODERATE,"E4")
    M["clin-safety"] = _mod("clin-safety","rest",{"url":FAERS,"method":"GET"},[],TTL_FAST,"E6")
    M["clin-rwe"] = _mod("clin-rwe","rest",{"url":FAERS,"method":"GET"},[],TTL_FAST,"E6")
    M["clin-on-target-ae-prior"] = _mod("clin-on-target-ae-prior","rest",{"url":f"{DRUGCENTRAL}/adverseevents","method":"GET"},[{"url":FAERS}],TTL_SLOW,"E6")
    M["clin-endpoints"] = _mod("clin-endpoints","rest",{"url":CLINTRIALS,"method":"GET"},[{"url":ICTRP}],TTL_FAST,"E6")
    M["clin-biomarker-fit"] = _mod("clin-biomarker-fit","rest",{"url":HPO,"method":"GET"},[{"url":MONARCH}],TTL_MODERATE,"E6")
    M["clin-feasibility"] = _mod("clin-feasibility","rest",{"url":CLINTRIALS,"method":"GET"},[{"url":ICTRP}],TTL_FAST,"E6")
    M["clin-pipeline"] = _mod("clin-pipeline","rest",{"url":CLINTRIALS,"method":"GET"},[{"url":DRUGCENTRAL}],TTL_FAST,"E6")
    M["comp-intensity"] = _mod("comp-intensity","rest",{"url":PATENTSVIEW,"method":"GET"},[],TTL_MODERATE,None)
    M["comp-freedom"] = _mod("comp-freedom","rest",{"url":PATENTSVIEW,"method":"GET"},[],TTL_MODERATE,None)

_register_modules()

# ============================================================================
# HTTP helpers (retries/failover/caching)
# ============================================================================

async def _retry_sleep(attempt: int) -> None:
    await asyncio.sleep(HTTP_RETRY_BASE * (2 ** attempt))

def _cache_key(url: str, method: str, params: Dict[str, Any], body: Any) -> str:
    m = hashlib.sha256()
    m.update(url.encode())
    m.update(method.encode())
    m.update(json.dumps(params or {}, sort_keys=True).encode())
    if body is not None:
        if isinstance(body, (str, bytes)):
            m.update(body if isinstance(body, bytes) else body.encode())
        else:
            m.update(json.dumps(body, sort_keys=True).encode())
    return m.hexdigest()

async def _fetch(module: ModuleCfg, url: str, method: str = "GET", params: Dict[str, Any] | None = None, json_body: Any | None = None, ttl: Optional[int]=None) -> Dict[str, Any]:
    params = params or {}
    ttl = ttl or module.ttl
    ck = _cache_key(url, method, params, json_body)
    cached = CACHE.get(ck)
    if cached is not None:
        return cached

    host = _host_from_url(url)
    if _cb_is_open(host):
        raise HTTPException(status_code=503, detail=f"Circuit open for host: {host}")

    limits = _get_host_limits(host)
    sem = _get_host_sem(url)
    retry_attempts = int(limits.get("retry_attempts", HTTP_RETRY_ATTEMPTS))
    base = _adaptive_retry_base(host)

    last_rate_limited = False
    async with _global_semaphore, sem:
        attempt = 0
        while True:
            try:
                timeout = httpx.Timeout(
                    connect=float(limits.get("connect_timeout", HTTP_CONNECT_TIMEOUT)),
                    read=float(limits.get("read_timeout", HTTP_READ_TIMEOUT)),
                    write=float(limits.get("write_timeout", HTTP_WRITE_TIMEOUT)),
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    wants_stream = bool(module.primary.get("stream", False)) or str(params.get("stream","")).lower() in {"1","true","yes"}
                    if wants_stream and method.upper() == "GET":
                        async with client.stream("GET", url, params=params) as resp:
                            if resp.status_code == 429 and attempt < retry_attempts:
                                last_rate_limited = True
                                ra = resp.headers.get("Retry-After")
                                if ra and str(ra).isdigit():
                                    await asyncio.sleep(float(ra))
                                else:
                                    await asyncio.sleep(base * (2 ** attempt))
                                attempt += 1
                                continue
                            resp.raise_for_status()
                            items = []
                            async for line in resp.aiter_lines():
                                if not line:
                                    continue
                                try:
                                    items.append(json.loads(line))
                                except Exception:
                                    continue
                            data = {"ndjson": items}
                            cc = resp.headers.get("Cache-Control","").lower()
                            if "no-store" not in cc:
                                CACHE.set(ck, data, ttl_seconds=ttl)
                            _cb_on_success(host)
                            return data
                    else:
                        if method.upper() == "GET":
                            resp = await client.get(url, params=params)
                        elif method.upper() == "POST":
                            resp = await client.post(url, json=json_body, params=params)
                        else:
                            resp = await client.request(method, url, params=params, json=json_body)

                    if resp.status_code == 429 and attempt < retry_attempts:
                        last_rate_limited = True
                        ra = resp.headers.get("Retry-After")
                        if ra and str(ra).isdigit():
                            await asyncio.sleep(float(ra))
                        else:
                            await asyncio.sleep(base * (2 ** attempt))
                        attempt += 1
                        continue

                    resp.raise_for_status()
                    # Try JSON; if not JSON but 200, treat as text payload
                    data = None
                    try:
                        data = resp.json()
                    except Exception:
                        txt = resp.text
                        data = {"_text": txt}
                    cc = resp.headers.get("Cache-Control","").lower()
                    if "no-store" not in cc:
                        CACHE.set(ck, data, ttl_seconds=ttl)
                    _cb_on_success(host)
                    return data
            except Exception as e:
                was_429 = isinstance(e, httpx.HTTPStatusError) and getattr(e, "response", None) is not None and getattr(e.response, "status_code", None) == 429
                _cb_on_failure(host, was_429=was_429 or last_rate_limited)
                if attempt < retry_attempts:
                    await asyncio.sleep(base * (2 ** attempt))
                    attempt += 1
                    continue
                for fb in module.fallbacks:
                    try:
                        data = await _fetch(module, fb["url"], fb.get("method","GET"), {}, None, ttl)
                        return data
                    except Exception:
                        continue
                detail = f"{module.key} fetch error from {url}: {e}"
                if last_rate_limited:
                    raise HTTPException(status_code=429, detail=detail)
                raise HTTPException(status_code=502, detail=detail)



def build_params(module_key: str, ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
    """Return (params, json_body) shaped for the given module and context."""
    params: Dict[str, Any] = {}
    body: Any = None

    if module_key in {"genetics-l2g","genetics-coloc","genetics-pqtl","genetics-mqtl-coloc"}:
        # GraphQL handled elsewhere
        pass
    elif module_key == "genetics-mr":
        params = {"exposure": ctx.get("efo") or ctx.get("ensg") or ctx.get("hgnc"), "outcome": ctx.get("efo")}
    elif module_key in {"genetics-annotation","genetics-nmd-inference"}:
        v = ctx.get("rsid") or ctx.get("region")
        if v:
            body = {"variants": [v]}
    elif module_key == "genetics-sqtl":
        params = {"gencodeId": ctx.get("ensg"), "tissueSiteDetailId": ctx.get("tissue")}
    elif module_key in {"genetics-regulatory","genetics-chromatin-contacts","genetics-3d-maps"}:
        # ENCODE/4DN search
        params = {"searchTerm": ctx.get("hgnc") or ctx.get("ensg") or ctx.get("region"), "format":"json"}
    elif module_key == "genetics-rare":
        params = {"db":"clinvar","term": (ctx.get("hgnc") or ctx.get("ensg"))}
    elif module_key == "genetics-consortia-summary":
        params = {"q": ctx.get("efo") or ctx.get("trait_label"), "size": 20}
    elif module_key == "genetics-ase-check":
        q = f'("{ctx.get("hgnc") or ctx.get("ensg")}") AND (ASE OR allele-specific)'
        params = {"query": q, "format": "json", "pageSize": 25}
    elif module_key == "genetics-ptm-signal-lite":
        params = {"query": f'accession:{ctx.get("uniprot")} AND (annotation:(type:PTM))'}
    elif module_key in {"genetics-lncrna","genetics-mirna"}:
        params = {"ids": ctx.get("hgnc") or ctx.get("ensg") or ctx.get("uniprot")}
    elif module_key.startswith("mech-") and "omnipath" in MODULES[module_key].primary["url"]:
        params = {"genesymbols": ctx.get("hgnc"), "fields": "is_directed,is_stimulation"}
    elif module_key == "mech-ppi":
        params = {"identifiers": ctx.get("hgnc"), "species": 9606}
    elif module_key == "mech-pathways":
        body = {"interactors":"true"}  # token step; downstream calls omitted
    elif module_key in {"assoc-perturb","assoc-spatial","tract-immunogenicity"}:
        q = f'("{ctx.get("hgnc") or ctx.get("ensg")}") AND ("{ctx.get("trait_label") or ctx.get("efo")}")'
        params = {"query": q, "format": "json", "pageSize": 50}
    elif module_key == "perturb-crispr-screens":
        params = {"searchNames": ctx.get("hgnc"), "format":"json"}
    elif module_key == "perturb-lincs-signatures":
        params = {"q": ctx.get("hgnc")}
    elif module_key == "perturb-signature-enrichment":
        params = {"keyword": ctx.get("hgnc")}
    elif module_key == "perturb-connectivity":
        params = {"sig": ctx.get("hgnc")}
    elif module_key == "perturb-perturbseq-encode":
        params = {"searchTerm": ctx.get("hgnc") or ctx.get("ensg"), "type":"Annotation","format":"json"}
    elif module_key == "expr-baseline":
        params = {"geneId": ctx.get("ensg") or ctx.get("hgnc")}
    elif module_key in {"assoc-sc","sc-hubmap"}:
        params = {"q": ctx.get("hgnc") or ctx.get("ensg")}
    elif module_key == "expr-localization":
        params = {"query": ctx.get("uniprot") or ctx.get("hgnc"), "fields":"cc_subcellular_location"}
    elif module_key == "assoc-bulk-prot":
        params = {"accession": ctx.get("uniprot")}
    elif module_key == "assoc-omics-phosphoproteomics":
        params = {"q": ctx.get("uniprot") or ctx.get("hgnc"), "pageSize": 25}
    elif module_key == "expr-inducibility":
        params = {"geneQuery": ctx.get("ensg") or ctx.get("hgnc")}
    elif module_key == "assoc-bulk-rna":
        params = {"db":"gds","term": (ctx.get("hgnc") or ctx.get("ensg"))}
    elif module_key == "assoc-metabolomics":
        params = {"study_type":"DISEASE"}
    elif module_key == "assoc-proteomics":
        params = {"name": ctx.get("hgnc")}
    elif module_key.startswith("tract-") and module_key != "tract-ligandability-oligo":
        params = {"query": ctx.get("uniprot") or ctx.get("hgnc")}
    elif module_key == "tract-ligandability-oligo":
        v = ctx.get("rsid") or ctx.get("region")
        if v:
            body = {"variants": [v]}
    elif module_key == "tract-drugs":
        params = {"molecule_chembl_id__icontains": ctx.get("hgnc")}
    elif module_key == "perturb-drug-response":
        params = {"target": ctx.get("hgnc")}
    elif module_key in {"clin-safety","clin-rwe"}:
        params = {"search": ctx.get("hgnc") or ctx.get("trait_label"), "count":"10"}
    elif module_key == "clin-on-target-ae-prior":
        params = {"search": ctx.get("hgnc")}
    elif module_key in {"clin-endpoints","clin-feasibility","clin-pipeline"}:
        params = {"cond": ctx.get("trait_label") or ctx.get("efo"), "term": ctx.get("hgnc")}
    elif module_key in {"clin-biomarker-fit"}:
        params = {"q": ctx.get("trait_label") or ctx.get("efo")}
    elif module_key.startswith("comp-"):
        params = {"q": ctx.get("hgnc") or ctx.get("trait_label") or ctx.get("ensg")}
    return params, body

# ============================================================================
# Module runner
# ============================================================================

async def run_module(module_key: str, ctx: Dict[str, Any]) -> EvidenceEnvelope:
    if module_key not in MODULES:
        raise HTTPException(status_code=400, detail=f"Unknown module: {module_key}")
    mod = MODULES[module_key]
    prov = Provenance(sources=[], query=ctx, module_order=[module_key])
    env = EvidenceEnvelope(module=module_key, context=EnvelopeContext(
        gene_id=ctx.get("ensg") or ctx.get("uniprot") or ctx.get("hgnc"),
        target=ctx.get("hgnc"),
        trait_id=ctx.get("efo") or ctx.get("trait_label"),
        variant_id=ctx.get("rsid") or ctx.get("region"),
        qtl_stage=mod.qtl_stage
    ))
    try:
        if mod.family == "graphql":
            template_key = mod.primary.get("template")
            gql = GQL_TEMPLATES.get(template_key, "")
            variables = {"ensg": ctx.get("ensg") or "", "efo": ctx.get("efo") or None}
            data = await _fetch(mod, mod.primary["url"], method="POST", json_body={"query": gql, "variables": variables})
        else:
            params, body = build_params(module_key, ctx)
            data = await _fetch(mod, mod.primary["url"], method=mod.primary.get("method","GET"), params=params, json_body=body)

        env.records = [{"raw": data}]
        env.edges = [Edge(kind="module_result", src=module_key, dst=env.context.gene_id, meta={"module": module_key, "stage": mod.qtl_stage})]
        prov.sources.append(mod.primary["url"])
        env.provenance = prov
        env.status = EvidenceStatus.NO_DATA if _is_no_data(data) else EvidenceStatus.OK
        return env
    except HTTPException as e:
        # Attempt fallbacks
        for fb in mod.fallbacks:
            try:
                data = await _fetch(mod, fb["url"], method=fb.get("method","GET"), params={}, json_body=None)
                env.records = [{"raw": data}]
                env.edges = [Edge(kind="module_result", src=module_key, dst=env.context.gene_id, meta={"module": module_key, "fallback": True})]
                prov.sources.append(fb["url"])
                env.provenance = prov
                env.status = EvidenceStatus.NO_DATA if _is_no_data(data) else EvidenceStatus.OK
                return env
            except Exception:
                continue
        if hasattr(prov, "warnings"):
            prov.warnings.append(str(e))
        env.provenance = prov
        env.status = (EvidenceStatus.CIRCUIT_OPEN if (getattr(e, "status_code", None) == 503 and "Circuit open" in str(getattr(e, "detail", ""))) else (EvidenceStatus.RATE_LIMITED if getattr(e, "status_code", None) == 429 else EvidenceStatus.UPSTREAM_ERROR))
        env.error = str(e)
        env.records = []
        return env



# ============================================================================
# Router & Runtime Policy (per config)
# ============================================================================

router = APIRouter(prefix="/targetval", tags=["Targetval Gateway (Full 22Q)"])

HTTP_CONNECT_TIMEOUT = 6.0
HTTP_TOTAL_TIMEOUT = 12.0
HTTP_READ_TIMEOUT = 12.0
HTTP_WRITE_TIMEOUT = 12.0
HTTP_RETRY_ATTEMPTS = 5
HTTP_RETRY_BASE = 0.6
GLOBAL_CONCURRENCY = 24

# TTL classes (seconds)
TTL_FAST = 6 * 3600
TTL_MODERATE = 24 * 3600
TTL_SLOW = 7 * 24 * 3600

# ----------------------------------------------------------------------------
# Minimal in-memory TTL cache
# ----------------------------------------------------------------------------

class _TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        v = self._store.get(key)
        if not v:
            return None
        exp, data = v
        if dt.datetime.now().timestamp() > exp:
            self._store.pop(key, None)
            return None
        return data

    def set(self, key: str, data: Any, ttl_seconds: int) -> None:
        self._store[key] = (dt.datetime.now().timestamp() + ttl_seconds, data)

CACHE = _TTLCache()

# ----------------------------------------------------------------------------
# Allowlist (strict). Any non-listed host is denied at request time.
# ----------------------------------------------------------------------------

HOST_ALLOW = {
    # OpenTargets / OpenGWAS / GWAS Catalog
    "api.opentargets.io",
    "gwas-api.mrcieu.ac.uk",
    "www.ebi.ac.uk",
    "www.ebi.ac.uk",
    "rest.ensembl.org",
    # ENCODE / 4DN / UCSC
    "www.encodeproject.org",
    "data.4dnucleome.org",
    # STRING / Reactome / SIGNOR / OmniPath
    "string-db.org",
    "reactome.org",
    "signor.uniroma2.it",
    "www.omnipathdb.org",
    # UniProt / PDBe / AlphaFold
    "rest.uniprot.org",
    "alphafold.ebi.ac.uk",
    "www.ebi.ac.uk",
    # PRIDE / ProteomicsDB / PDC
    "www.ebi.ac.uk",
    "www.proteomicsdb.org",
    "pdc.cancer.gov",
    # Europe PMC / PubMed / Crossref / Unpaywall / OpenAlex / SemanticScholar / PubTator3
    "www.ebi.ac.uk",
    "eutils.ncbi.nlm.nih.gov",
    "api.crossref.org",
    "api.unpaywall.org",
    "api.openalex.org",
    "api.semanticscholar.org",
    "www.ncbi.nlm.nih.gov",
    # LINCS / iLINCS
    "lincsportal.ccs.miami.edu",
    "www.ilincs.org",
    # CellxGene / HCA / HuBMAP
    "api.cellxgene.cziscience.com",
    "azul.data.humancellatlas.org",
    "data.humancellatlas.org",
    "search.api.hubmapconsortium.org",
    "hubmapconsortium.org",
    # ArrayExpress / BioStudies / Expression Atlas
    "www.ebi.ac.uk",
    # IEDB / IPD-IMGT
    "www.iedb.org",
    "tools-api.iedb.org",
    "www.ebi.ac.uk",
    "www.ipd-imgt.org",
    # DrugCentral / ChEMBL / BindingDB / PubChem / STITCH / Pharos
    "drugcentral.org",
    "www.ebi.ac.uk",
    "www.bindingdb.org",
    "pubchem.ncbi.nlm.nih.gov",
    "stitch.embl.de",
    "pharos.nih.gov",
    # PharmacoDB / CellMiner
    "api.pharmacodb.net",
    "discover.nci.nih.gov",
    # FAERS
    "api.fda.gov",
    # ClinicalTrials.gov / WHO ICTRP
    "clinicaltrials.gov",
    "www.who.int",
    # HPO / Monarch
    "hpo.jax.org",
    "api.monarchinitiative.org",
    # PatentsView
    "api.patentsview.org",
    # RNAcentral
    "rnacentral.org",
}

# ----------------------------------------------------------------------------
# Host-specific overrides & circuit breaker (simple)
# ----------------------------------------------------------------------------
HOST_OVERRIDES: Dict[str, Dict[str, Any]] = {}
try:
    _env_ho = os.getenv("HOST_OVERRIDES_JSON")
    if _env_ho:
        HOST_OVERRIDES.update(json.loads(_env_ho))
except Exception:
    pass

_host_state: Dict[str, Dict[str, Any]] = {}
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_OPEN_SECONDS = float(os.getenv("CB_OPEN_SECONDS", "30"))

def _get_host_limits(host: str) -> Dict[str, Any]:
    limits = {
        "semaphore": 4,
        "connect_timeout": HTTP_CONNECT_TIMEOUT,
        "read_timeout": HTTP_READ_TIMEOUT,
        "write_timeout": HTTP_WRITE_TIMEOUT,
        "retry_attempts": HTTP_RETRY_ATTEMPTS,
    }
    ov = HOST_OVERRIDES.get(host) or {}
    limits.update({k: v for k, v in ov.items() if v is not None})
    return limits

def _now_ts() -> float:
    return dt.datetime.now().timestamp()

def _cb_is_open(host: str) -> bool:
    st = _host_state.get(host) or {}
    return st.get("open_until", 0) > _now_ts()

def _cb_on_success(host: str) -> None:
    st = _host_state.setdefault(host, {})
    st["fails"] = 0
    st["recent_429"] = max(0, int(st.get("recent_429", 0)) - 1)
    st["open_until"] = 0

def _cb_on_failure(host: str, was_429: bool=False) -> None:
    st = _host_state.setdefault(host, {})
    st["fails"] = int(st.get("fails", 0)) + 1
    if was_429:
        st["recent_429"] = int(st.get("recent_429", 0)) + 1
    if st["fails"] >= CB_FAILURE_THRESHOLD:
        st["open_until"] = _now_ts() + CB_OPEN_SECONDS

def _adaptive_retry_base(host: str) -> float:
    # allow per-host override of base backoff; otherwise adapt to recent 429s
    ov = HOST_OVERRIDES.get(host) or {}
    try:
        base = float(ov.get("retry_base", HTTP_RETRY_BASE))
    except Exception:
        base = HTTP_RETRY_BASE
    st = _host_state.get(host) or {}
    r429 = int(st.get("recent_429", 0))
    if r429 > 0:
        base = base * min(3.0, 1.0 + 0.5 * r429)
    return base
0 + 0.5 * r429)
    return base


_host_semaphores: Dict[str, asyncio.Semaphore] = {}
_global_semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY)

def _host_from_url(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0]

def _get_host_sem(url: str) -> asyncio.Semaphore:
    host = _host_from_url(url)
    # Derive allowlist lazily from module registry to avoid duplication/drift
    global HOST_ALLOW
    if not HOST_ALLOW:
        try:
            derived = set()
            for _m in MODULES.values():
                try:
                    if isinstance(_m.primary, dict):
                        u = _m.primary.get("url","")
                        if u:
                            derived.add(_host_from_url(u))
                    for fb in (_m.fallbacks or []):
                        u2 = fb.get("url","")
                        if u2:
                            derived.add(_host_from_url(u2))
                except Exception:
                    continue
            HOST_ALLOW = derived
        except Exception:
            pass
    if host not in HOST_ALLOW:
        raise HTTPException(status_code=400, detail=f"Host not allowlisted: {host}")
    limits = _get_host_limits(host)
    if host not in _host_semaphores:
        _host_semaphores[host] = asyncio.Semaphore(int(limits.get("semaphore", 4)))
    return _host_semaphores[host]

# ============================================================================
# Schemas (Pydantic v2 style)
# ============================================================================


class EvidenceStatus(str, Enum):
    OK = "OK"
    NO_DATA = "NO_DATA"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    RATE_LIMITED = "RATE_LIMITED"

class Edge(BaseModel):

    kind: str
    src: str | None = None
    dst: str | None = None
    meta: Dict[str, Any] = {}

class Provenance(BaseModel):
    sources: List[str] = []
    query: Dict[str, Any] = {}
    accessed_at: str = Field(default_factory=lambda: dt.datetime.utcnow().isoformat())
    version: str = "v2"
    module_order: List[str] = []
    warnings: List[str] = []

class EnvelopeContext(BaseModel):
    gene_id: str | None = None
    target: str | None = None
    trait_id: str | None = None
    variant_id: str | None = None
    tissue: str | None = None
    cell_type: str | None = None
    assay: str | None = None
    qtl_type: str | None = None  # cis-QTL / trans-QTL
    qtl_stage: str | None = None  # E0..E6

class EvidenceEnvelope(BaseModel):
    module: str
    context: EnvelopeContext
    records: List[Dict[str, Any]] = []
    edges: List[Edge] = []
    provenance: Provenance = Field(default_factory=Provenance)
    notes: List[str] = []
    status: EvidenceStatus = EvidenceStatus.OK
    error: Optional[str] = None

class LitEvidence(BaseModel):
    query: str
    hits: List[Dict[str, Any]]
    tags: Dict[str, List[str]]
    errors: List[str] = []

class QuestionAnswer(BaseModel):
    question_id: int
    title: str
    envelopes: List[EvidenceEnvelope]
    literature: LitEvidence | None = None
    math_summary: Dict[str, Any] | None = None
    skeptic_view: List[str] = []
    killer_experiments: List[str] = []

class RunRequest(BaseModel):
    gene_symbol: str | None = None
    ensembl_id: str | None = None
    uniprot_id: str | None = None
    trait_efo: str | None = None
    trait_label: str | None = None
    rsid: str | None = None
    region: str | None = None
    run_questions: List[int] | None = None  # default 1..22
    strict_human: bool = True

class FinalRecommendation(BaseModel):
    decision: str
    why: List[str]
    modality_plan: Dict[str, Any]
    pd_plan: Dict[str, Any]
    risk_register: Dict[str, Any]
    mos_plan: Dict[str, Any]
    fastest_pom: Dict[str, Any]
    next_experiments: List[str]
    differentiation: Dict[str, Any] | None = None

class RunResponse(BaseModel):
    context: Dict[str, Any]
    questions: List[QuestionAnswer]
    final_recommendation: FinalRecommendation

# ============================================================================
# Identifier expansion & Query Planner
# ============================================================================

def _normalize_gene(req: RunRequest) -> Dict[str, str]:
    return {"hgnc": req.gene_symbol or "", "ensg": req.ensembl_id or "", "uniprot": req.uniprot_id or ""}

def _normalize_trait(req: RunRequest) -> Dict[str, str]:
    return {"efo": req.trait_efo or "", "trait_label": req.trait_label or ""}

def _normalize_variant(req: RunRequest) -> Dict[str, str]:
    return {"rsid": req.rsid or "", "region": req.region or ""}

# ============================================================================
# Module registry (full set per config)
# ============================================================================

@dataclass
class ModuleCfg:
    key: str
    family: str         # graphql | rest
    primary: Dict[str, Any]
    fallbacks: List[Dict[str, Any]]
    ttl: int = TTL_MODERATE
    qtl_stage: Optional[str] = None

# Endpoint bases
OPEN_TARGETS_GQL = "https://api.opentargets.io/v3/platform/graphql"
OPENGWAS_BASE = "https://gwas-api.mrcieu.ac.uk"
ENSEMBL_VEP = "https://rest.ensembl.org/vep/human/region"
ENCODE_BASE = "https://www.encodeproject.org"
FOURDN_BASE = "https://data.4dnucleome.org"
GTEX_BASE = "https://gtexportal.org/rest/v1"
STRING_BASE = "https://string-db.org/api"
REACTOME = "https://reactome.org/AnalysisService"
OMNIPATH = "https://www.omnipathdb.org/interactions"
SIGNOR = "https://signor.uniroma2.it/ws"
UNIPROT = "https://rest.uniprot.org"
ALPHAFOLD = "https://alphafold.ebi.ac.uk/api"
PDBe = "https://www.ebi.ac.uk/pdbe/api"
PRIDE = "https://www.ebi.ac.uk/pride/ws/archive"
PROTEOMICSDB = "https://www.proteomicsdb.org/proxy"
PDC = "https://pdc.cancer.gov/graphql"
ILINCS = "https://www.ilincs.org/api"
LINCS_LDP3 = "https://lincsportal.ccs.miami.edu/api"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
OPENALEX = "https://api.openalex.org/works"
SEM_SCH = "https://api.semanticscholar.org/graph/v1/paper"
PUBTATOR3 = "https://www.ncbi.nlm.nih.gov/research/pubtator3/publications/export/biocjson"
FAERS = "https://api.fda.gov/drug/event.json"
DRUGCENTRAL = "https://drugcentral.org/api"
CLINTRIALS = "https://clinicaltrials.gov/api/v2/studies"
ICTRP = "https://www.who.int/clinical-trials-registry-platform"
HPO = "https://hpo.jax.org/api/hpo"
MONARCH = "https://api.monarchinitiative.org/api"
PATENTSVIEW = "https://api.patentsview.org/patents"
GLYGEN = "https://glygen.org/api"
PHAROS = "https://pharos.nih.gov/api/graphql"
BINDINGDB = "https://www.bindingdb.org"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
STITCH = "https://stitch.embl.de"
RNCENTRAL = "https://rnacentral.org/api"

def _mod(key, family, primary, fallbacks, ttl=TTL_MODERATE, qtl_stage=None) -> ModuleCfg:
    return ModuleCfg(key=key, family=family, primary=primary, fallbacks=fallbacks, ttl=ttl, qtl_stage=qtl_stage)

# Selected GraphQL templates
GQL_TEMPLATES = {
    "genetics-l2g": """
query L2G($ensg: String!, $efo: String) {
  gene(ensemblId: $ensg) {
    id
    geneticAssociations(efos: [$efo]) {
      rows { id, score, studyId, efoId }
    }
  }
}""",
    "genetics-coloc": """
query Coloc($ensg: String!, $efo: String) {
  gene(ensemblId: $ensg) {
    id
    colocalisations(efos: [$efo]) {
      rows { locusId, efoId, pp4, tissue, qtlType }
    }
  }
}""",
    "genetics-pqtl": """
query PQTL($ensg: String!) {
  gene(ensemblId: $ensg) { id pqtl { rows { studyId, efoId, pp4 } } }
}""",
    "genetics-mqtl-coloc": """
query MQTL($ensg: String!, $efo: String) {
  gene(ensemblId: $ensg) {
    id
    metaboliteColoc(efos: [$efo]) { rows { metabolite, pp4, studyId } }
  }
}""",
}

MODULES: Dict[str, ModuleCfg] = {}

def _register_modules():
    M = MODULES
    # --- Genetics & QTL ---
    M["genetics-l2g"] = _mod("genetics-l2g","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-l2g"},[{"url":"https://www.ebi.ac.uk/gwas/rest/api"}],TTL_FAST,"E6")
    M["genetics-coloc"] = _mod("genetics-coloc","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-coloc"},[{"url":OPENGWAS_BASE}],TTL_FAST,"E1/E6")
    M["genetics-mr"] = _mod("genetics-mr","rest",{"url":f"{OPENGWAS_BASE}/mr","method":"GET"},[{"url":f"{OPENGWAS_BASE}/"}],TTL_FAST,"E1/E6")
    M["genetics-chromatin-contacts"] = _mod("genetics-chromatin-contacts","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[{"url":FOURDN_BASE}],TTL_MODERATE,"E0")
    M["genetics-3d-maps"] = _mod("genetics-3d-maps","rest",{"url":f"{FOURDN_BASE}/search/","method":"GET"},[{"url":f"{ENCODE_BASE}/search/"}],TTL_MODERATE,"E0")
    M["genetics-regulatory"] = _mod("genetics-regulatory","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[{"url":"https://www.ebi.ac.uk/eqtl/api"}],TTL_MODERATE,"E0")
    M["genetics-sqtl"] = _mod("genetics-sqtl","rest",{"url":f"{GTEX_BASE}/association/singleTissueEqtl","method":"GET"},[{"url":"https://www.ebi.ac.uk/eqtl/api"}],TTL_FAST,"E2")
    M["genetics-pqtl"] = _mod("genetics-pqtl","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-pqtl"},[{"url":OPENGWAS_BASE}],TTL_FAST,"E3")
    M["genetics-annotation"] = _mod("genetics-annotation","rest",{"url":ENSEMBL_VEP,"method":"POST"},[{"url":"https://cadd.gs.washington.edu/api"}],TTL_MODERATE,"E0/E2")
    M["genetics-pathogenicity-priors"] = _mod("genetics-pathogenicity-priors","rest",{"url":"https://gnomad.broadinstitute.org/api","method":"POST"},[{"url":"https://cadd.gs.washington.edu/api"}],TTL_SLOW,"E0")
    M["genetics-intolerance"] = _mod("genetics-intolerance","rest",{"url":"https://gnomad.broadinstitute.org/api","method":"POST"},[],TTL_SLOW,"E0")
    M["genetics-rare"] = _mod("genetics-rare","rest",{"url":f"{PUBMED_EUTILS}/esearch.fcgi","method":"GET"},[{"url":"https://myvariant.info/v1"}],TTL_SLOW,"E6")
    M["genetics-mendelian"] = _mod("genetics-mendelian","rest",{"url":"https://clinicalgenome.org/graphql","method":"POST"},[],TTL_SLOW,"E6")
    M["genetics-phewas-human-knockout"] = _mod("genetics-phewas-human-knockout","rest",{"url":f"{OPENGWAS_BASE}/phew","method":"GET"},[{"url":"https://www.ebi.ac.uk/gwas/rest/api"}],TTL_FAST,"E6")
    M["genetics-functional"] = _mod("genetics-functional","rest",{"url":"https://webservice.thebiogrid.org/ORCS","method":"GET"},[{"url":EPMC_SEARCH}],TTL_MODERATE,"E4")
    M["genetics-mavedb"] = _mod("genetics-mavedb","rest",{"url":"https://api.mavedb.org/graphql","method":"POST"},[],TTL_SLOW,"E4")
    M["genetics-consortia-summary"] = _mod("genetics-consortia-summary","rest",{"url":f"{OPENGWAS_BASE}/studies","method":"GET"},[],TTL_MODERATE,"E6")
    M["genetics-caqtl-lite"] = _mod("genetics-caqtl-lite","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[],TTL_MODERATE,"E0")
    M["genetics-nmd-inference"] = _mod("genetics-nmd-inference","rest",{"url":ENSEMBL_VEP,"method":"POST"},[],TTL_MODERATE,"E2")
    M["genetics-mqtl-coloc"] = _mod("genetics-mqtl-coloc","graphql",{"url":OPEN_TARGETS_GQL,"template":"genetics-mqtl-coloc"},[{"url":OPENGWAS_BASE}],TTL_FAST,"E5")
    M["genetics-ase-check"] = _mod("genetics-ase-check","rest",{"url":EPMC_SEARCH,"method":"GET"},[],TTL_MODERATE,"E1")
    M["genetics-ptm-signal-lite"] = _mod("genetics-ptm-signal-lite","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":PRIDE}],TTL_MODERATE,"E3")
    M["genetics-lncrna"] = _mod("genetics-lncrna","rest",{"url":f"{RNCENTRAL}/v1/rna","method":"GET"},[{"url":EPMC_SEARCH}],TTL_MODERATE,"E0/E1/E2")
    M["genetics-mirna"] = _mod("genetics-mirna","rest",{"url":f"{RNCENTRAL}/v1/rna","method":"GET"},[{"url":EPMC_SEARCH}],TTL_MODERATE,"E2")

    # --- Mechanism & Perturbation ---
    M["mech-ppi"] = _mod("mech-ppi","rest",{"url":f"{STRING_BASE}/json/network","method":"GET"},[{"url":"https://www.ebi.ac.uk/Tools/webservices/psicquic"}],TTL_MODERATE,"E4")
    M["mech-pathways"] = _mod("mech-pathways","rest",{"url":f"{REACTOME}/token","method":"POST"},[{"url":"https://www.pathwaycommons.org/pc2"}],TTL_MODERATE,"E4/E5")
    M["mech-ligrec"] = _mod("mech-ligrec","rest",{"url":OMNIPATH,"method":"GET"},[{"url":"https://www.guidetopharmacology.org"}],TTL_MODERATE,"E4")
    M["biology-causal-pathways"] = _mod("biology-causal-pathways","rest",{"url":f"{REACTOME}/token","method":"POST"},[{"url":SIGNOR}],TTL_MODERATE,"E4/E5")
    M["mech-directed-signaling"] = _mod("mech-directed-signaling","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E4")
    M["mech-kinase-substrate"] = _mod("mech-kinase-substrate","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E3/E4")
    M["mech-tf-target"] = _mod("mech-tf-target","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E1/E4")
    M["mech-mirna-target"] = _mod("mech-mirna-target","rest",{"url":OMNIPATH,"method":"GET"},[],TTL_MODERATE,"E2/E4")
    M["mech-complexes"] = _mod("mech-complexes","rest",{"url":"https://www.ebi.ac.uk/complexportal/api/complex","method":"GET"},[{"url":"https://www.omnipathdb.org/complexes"}],TTL_SLOW,"E4")

    M["assoc-perturb"] = _mod("assoc-perturb","rest",{"url":EPMC_SEARCH,"method":"GET"},[{"url":f"{LINCS_LDP3}/signatures"}],TTL_MODERATE,"E4/E5")
    M["perturb-crispr-screens"] = _mod("perturb-crispr-screens","rest",{"url":"https://webservice.thebiogrid.org/ORCS","method":"GET"},[{"url":"https://cellmodelpassports.sanger.ac.uk/api"}],TTL_MODERATE,"E4")
    M["perturb-lincs-signatures"] = _mod("perturb-lincs-signatures","rest",{"url":f"{LINCS_LDP3}/signatures","method":"GET"},[{"url":ILINCS}],TTL_MODERATE,"E4/E5")
    M["perturb-signature-enrichment"] = _mod("perturb-signature-enrichment","rest",{"url":f"{ILINCS}/SignatureMeta","method":"GET"},[{"url":f"{LINCS_LDP3}/signatures"}],TTL_MODERATE,"E4/E5")
    M["perturb-connectivity"] = _mod("perturb-connectivity","rest",{"url":f"{ILINCS}/Connectivity","method":"GET"},[{"url":f"{LINCS_LDP3}/signatures"}],TTL_MODERATE,"E4/E5")
    M["perturb-perturbseq-encode"] = _mod("perturb-perturbseq-encode","rest",{"url":f"{ENCODE_BASE}/search/","method":"GET"},[{"url":"https://www.ebi.ac.uk/ena/browser/api"}],TTL_MODERATE,"E4")

    # --- Expression & Context ---
    M["expr-baseline"] = _mod("expr-baseline","rest",{"url":f"{GTEX_BASE}/gene/expression","method":"GET"},[{"url":"https://www.ebi.ac.uk/gxa/api/genes"}],TTL_MODERATE,"E3")
    M["assoc-sc"] = _mod("assoc-sc","rest",{"url":"https://azul.data.humancellatlas.org/index/projects","method":"GET"},[{"url":"https://api.cellxgene.cziscience.com/dp/v1/discover/datasets"}],TTL_MODERATE,"E3")
    M["sc-hubmap"] = _mod("sc-hubmap","rest",{"url":"https://search.api.hubmapconsortium.org/portal/search","method":"GET"},[],TTL_MODERATE,"E3")
    M["assoc-spatial"] = _mod("assoc-spatial","rest",{"url":EPMC_SEARCH,"method":"GET"},[],TTL_MODERATE,"E3/E4")
    M["expr-localization"] = _mod("expr-localization","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[],TTL_MODERATE,"E3")
    M["assoc-bulk-prot"] = _mod("assoc-bulk-prot","rest",{"url":f"{PROTEOMICSDB}/v2/proteinexpression","method":"GET"},[{"url":PDC}],TTL_MODERATE,"E3")
    M["assoc-omics-phosphoproteomics"] = _mod("assoc-omics-phosphoproteomics","rest",{"url":f"{PRIDE}/project/list","method":"GET"},[],TTL_MODERATE,"E3")
    M["expr-inducibility"] = _mod("expr-inducibility","rest",{"url":"https://www.ebi.ac.uk/gxa/json/experiments","method":"GET"},[{"url":"https://www.ebi.ac.uk/biostudies/api/v1"}],TTL_MODERATE,"E3")
    M["assoc-bulk-rna"] = _mod("assoc-bulk-rna","rest",{"url":f"{PUBMED_EUTILS}/esearch.fcgi","method":"GET"},[{"url":"https://www.ebi.ac.uk/biostudies/api/v1"}],TTL_MODERATE,None)
    M["assoc-metabolomics"] = _mod("assoc-metabolomics","rest",{"url":"https://www.ebi.ac.uk/metabolights/ws/studies","method":"GET"},[{"url":"https://www.metabolomicsworkbench.org/rest"}],TTL_MODERATE,"E5")
    M["assoc-proteomics"] = _mod("assoc-proteomics","rest",{"url":f"{PROTEOMICSDB}/v2/proteins","method":"GET"},[{"url":PRIDE},{"url":PDC}],TTL_MODERATE,"E3")
    M["assoc-hpa-pathology"] = _mod("assoc-hpa-pathology","rest",{"url":EPMC_SEARCH,"method":"GET"},[],TTL_MODERATE,"E3")

    # --- Tractability & Modality ---
    M["mech-structure"] = _mod("mech-structure","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":ALPHAFOLD},{"url":PDBe}],TTL_SLOW,"E4")
    M["tract-ligandability-sm"] = _mod("tract-ligandability-sm","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":PDBe},{"url":BINDINGDB}],TTL_SLOW,None)
    M["tract-ligandability-ab"] = _mod("tract-ligandability-ab","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":GLYGEN}],TTL_SLOW,None)
    M["tract-surfaceome"] = _mod("tract-surfaceome","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":GLYGEN}],TTL_SLOW,None)
    M["tract-ligandability-oligo"] = _mod("tract-ligandability-oligo","rest",{"url":ENSEMBL_VEP,"method":"POST"},[{"url":"https://rnacentral.org/api"}],TTL_MODERATE,None)
    M["tract-modality"] = _mod("tract-modality","rest",{"url":f"{UNIPROT}/uniprotkb/search","method":"GET"},[{"url":PHAROS}],TTL_SLOW,None)
    M["tract-drugs"] = _mod("tract-drugs","rest",{"url":f"{CHEMBL}/mechanism","method":"GET"},[{"url":DRUGCENTRAL},{"url":BINDINGDB},{"url":f"{PUBCHEM}/"}],TTL_SLOW,None)
    M["perturb-drug-response"] = _mod("perturb-drug-response","rest",{"url":"https://api.pharmacodb.net/v2/experiments","method":"GET"},[{"url":"https://discover.nci.nih.gov/cellminerapi"}],TTL_MODERATE,None)

    # --- Safety & Clinical ---
    M["tract-immunogenicity"] = _mod("tract-immunogenicity","rest",{"url":EPMC_SEARCH,"method":"GET"},[{"url":"https://tools-api.iedb.org/population"}],TTL_MODERATE,"E4")
    M["tract-mhc-binding"] = _mod("tract-mhc-binding","rest",{"url":"https://tools-api.iedb.org/mhci/consensus","method":"POST"},[{"url":"https://tools-api.iedb.org/mhcii/consensus"}],TTL_FAST,"E4")
    M["tract-iedb-epitopes"] = _mod("tract-iedb-epitopes","rest",{"url":"https://www.iedb.org/api","method":"GET"},[],TTL_SLOW,"E4")
    M["immuno/hla-coverage"] = _mod("immuno/hla-coverage","rest",{"url":"https://www.ebi.ac.uk/ipd/api","method":"GET"},[{"url":"https://tools-api.iedb.org/population"}],TTL_SLOW,"E4")
    M["function-dependency"] = _mod("function-dependency","rest",{"url":"https://webservice.thebiogrid.org/ORCS","method":"GET"},[{"url":"https://cellmodelpassports.sanger.ac.uk/api"}],TTL_MODERATE,"E4")
    M["perturb-depmap-dependency"] = _mod("perturb-depmap-dependency","rest",{"url":"https://cellmodelpassports.sanger.ac.uk/api","method":"GET"},[{"url":"https://webservice.thebiogrid.org/ORCS"}],TTL_MODERATE,"E4")
    M["clin-safety"] = _mod("clin-safety","rest",{"url":FAERS,"method":"GET"},[],TTL_FAST,"E6")
    M["clin-rwe"] = _mod("clin-rwe","rest",{"url":FAERS,"method":"GET"},[],TTL_FAST,"E6")
    M["clin-on-target-ae-prior"] = _mod("clin-on-target-ae-prior","rest",{"url":f"{DRUGCENTRAL}/adverseevents","method":"GET"},[{"url":FAERS}],TTL_SLOW,"E6")
    M["clin-endpoints"] = _mod("clin-endpoints","rest",{"url":CLINTRIALS,"method":"GET"},[{"url":ICTRP}],TTL_FAST,"E6")
    M["clin-biomarker-fit"] = _mod("clin-biomarker-fit","rest",{"url":HPO,"method":"GET"},[{"url":MONARCH}],TTL_MODERATE,"E6")
    M["clin-feasibility"] = _mod("clin-feasibility","rest",{"url":CLINTRIALS,"method":"GET"},[{"url":ICTRP}],TTL_FAST,"E6")
    M["clin-pipeline"] = _mod("clin-pipeline","rest",{"url":CLINTRIALS,"method":"GET"},[{"url":DRUGCENTRAL}],TTL_FAST,"E6")
    M["comp-intensity"] = _mod("comp-intensity","rest",{"url":PATENTSVIEW,"method":"GET"},[],TTL_MODERATE,None)
    M["comp-freedom"] = _mod("comp-freedom","rest",{"url":PATENTSVIEW,"method":"GET"},[],TTL_MODERATE,None)

_register_modules()

# ============================================================================
# HTTP helpers (retries/failover/caching)
# ============================================================================

async def _retry_sleep(attempt: int) -> None:
    await asyncio.sleep(HTTP_RETRY_BASE * (2 ** attempt))

def _cache_key(url: str, method: str, params: Dict[str, Any], body: Any) -> str:
    m = hashlib.sha256()
    m.update(url.encode())
    m.update(method.encode())
    m.update(json.dumps(params or {}, sort_keys=True).encode())
    if body is not None:
        if isinstance(body, (str, bytes)):
            m.update(body if isinstance(body, bytes) else body.encode())
        else:
            m.update(json.dumps(body, sort_keys=True).encode())
    return m.hexdigest()

async def _fetch(module: ModuleCfg, url: str, method: str = "GET", params: Dict[str, Any] | None = None, json_body: Any | None = None, ttl: Optional[int]=None) -> Dict[str, Any]:
    params = params or {}
    ttl = ttl or module.ttl
    ck = _cache_key(url, method, params, json_body)
    cached = CACHE.get(ck)
    if cached is not None:
        return cached

    host = _host_from_url(url)
    if _cb_is_open(host):
        raise HTTPException(status_code=503, detail=f"Circuit open for host: {host}")

    limits = _get_host_limits(host)
    sem = _get_host_sem(url)
    retry_attempts = int(limits.get("retry_attempts", HTTP_RETRY_ATTEMPTS))
    base = _adaptive_retry_base(host)

    last_rate_limited = False
    async with _global_semaphore, sem:
        attempt = 0
        while True:
            try:
                timeout = httpx.Timeout(
                    connect=float(limits.get("connect_timeout", HTTP_CONNECT_TIMEOUT)),
                    read=float(limits.get("read_timeout", HTTP_READ_TIMEOUT)),
                    write=float(limits.get("write_timeout", HTTP_WRITE_TIMEOUT)),
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    wants_stream = bool(module.primary.get("stream", False)) or str(params.get("stream","")).lower() in {"1","true","yes"}
                    if wants_stream and method.upper() == "GET":
                        async with client.stream("GET", url, params=params) as resp:
                            if resp.status_code == 429 and attempt < retry_attempts:
                                last_rate_limited = True
                                ra = resp.headers.get("Retry-After")
                                if ra and str(ra).isdigit():
                                    await asyncio.sleep(float(ra))
                                else:
                                    await asyncio.sleep(base * (2 ** attempt))
                                attempt += 1
                                continue
                            resp.raise_for_status()
                            items = []
                            async for line in resp.aiter_lines():
                                if not line:
                                    continue
                                try:
                                    items.append(json.loads(line))
                                except Exception:
                                    continue
                            data = {"ndjson": items}
                            cc = resp.headers.get("Cache-Control","").lower()
                            if "no-store" not in cc:
                                CACHE.set(ck, data, ttl_seconds=ttl)
                            _cb_on_success(host)
                            return data
                    else:
                        if method.upper() == "GET":
                            resp = await client.get(url, params=params)
                        elif method.upper() == "POST":
                            resp = await client.post(url, json=json_body, params=params)
                        else:
                            resp = await client.request(method, url, params=params, json=json_body)

                    if resp.status_code == 429 and attempt < retry_attempts:
                        last_rate_limited = True
                        ra = resp.headers.get("Retry-After")
                        if ra and str(ra).isdigit():
                            await asyncio.sleep(float(ra))
                        else:
                            await asyncio.sleep(base * (2 ** attempt))
                        attempt += 1
                        continue

                    resp.raise_for_status()
                    # Try JSON; if not JSON but 200, treat as text payload
                    data = None
                    try:
                        data = resp.json()
                    except Exception:
                        txt = resp.text
                        data = {"_text": txt}
                    cc = resp.headers.get("Cache-Control","").lower()
                    if "no-store" not in cc:
                        CACHE.set(ck, data, ttl_seconds=ttl)
                    _cb_on_success(host)
                    return data
            except Exception as e:
                was_429 = isinstance(e, httpx.HTTPStatusError) and getattr(e, "response", None) is not None and getattr(e.response, "status_code", None) == 429
                _cb_on_failure(host, was_429=was_429 or last_rate_limited)
                if attempt < retry_attempts:
                    await asyncio.sleep(base * (2 ** attempt))
                    attempt += 1
                    continue
                for fb in module.fallbacks:
                    try:
                        data = await _fetch(module, fb["url"], fb.get("method","GET"), {}, None, ttl)
                        return data
                    except Exception:
                        continue
                detail = f"{module.key} fetch error from {url}: {e}"
                if last_rate_limited:
                    raise HTTPException(status_code=429, detail=detail)
                raise HTTPException(status_code=502, detail=detail)



def build_params(module_key: str, ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
    """Return (params, json_body) shaped for the given module and context."""
    params: Dict[str, Any] = {}
    body: Any = None

    if module_key in {"genetics-l2g","genetics-coloc","genetics-pqtl","genetics-mqtl-coloc"}:
        # GraphQL handled elsewhere
        pass
    elif module_key == "genetics-mr":
        params = {"exposure": ctx.get("efo") or ctx.get("ensg") or ctx.get("hgnc"), "outcome": ctx.get("efo")}
    elif module_key in {"genetics-annotation","genetics-nmd-inference"}:
        v = ctx.get("rsid") or ctx.get("region")
        if v:
            body = {"variants": [v]}
    elif module_key == "genetics-sqtl":
        params = {"gencodeId": ctx.get("ensg"), "tissueSiteDetailId": ctx.get("tissue")}
    elif module_key in {"genetics-regulatory","genetics-chromatin-contacts","genetics-3d-maps"}:
        # ENCODE/4DN search
        params = {"searchTerm": ctx.get("hgnc") or ctx.get("ensg") or ctx.get("region"), "format":"json"}
    elif module_key == "genetics-rare":
        params = {"db":"clinvar","term": (ctx.get("hgnc") or ctx.get("ensg"))}
    elif module_key == "genetics-consortia-summary":
        params = {"q": ctx.get("efo") or ctx.get("trait_label"), "size": 20}
    elif module_key == "genetics-ase-check":
        q = f'("{ctx.get("hgnc") or ctx.get("ensg")}") AND (ASE OR allele-specific)'
        params = {"query": q, "format": "json", "pageSize": 25}
    elif module_key == "genetics-ptm-signal-lite":
        params = {"query": f'accession:{ctx.get("uniprot")} AND (annotation:(type:PTM))'}
    elif module_key in {"genetics-lncrna","genetics-mirna"}:
        params = {"ids": ctx.get("hgnc") or ctx.get("ensg") or ctx.get("uniprot")}
    elif module_key.startswith("mech-") and "omnipath" in MODULES[module_key].primary["url"]:
        params = {"genesymbols": ctx.get("hgnc"), "fields": "is_directed,is_stimulation"}
    elif module_key == "mech-ppi":
        params = {"identifiers": ctx.get("hgnc"), "species": 9606}
    elif module_key == "mech-pathways":
        body = {"interactors":"true"}  # token step; downstream calls omitted
    elif module_key in {"assoc-perturb","assoc-spatial","tract-immunogenicity"}:
        q = f'("{ctx.get("hgnc") or ctx.get("ensg")}") AND ("{ctx.get("trait_label") or ctx.get("efo")}")'
        params = {"query": q, "format": "json", "pageSize": 50}
    elif module_key == "perturb-crispr-screens":
        params = {"searchNames": ctx.get("hgnc"), "format":"json"}
    elif module_key == "perturb-lincs-signatures":
        params = {"q": ctx.get("hgnc")}
    elif module_key == "perturb-signature-enrichment":
        params = {"keyword": ctx.get("hgnc")}
    elif module_key == "perturb-connectivity":
        params = {"sig": ctx.get("hgnc")}
    elif module_key == "perturb-perturbseq-encode":
        params = {"searchTerm": ctx.get("hgnc") or ctx.get("ensg"), "type":"Annotation","format":"json"}
    elif module_key == "expr-baseline":
        params = {"geneId": ctx.get("ensg") or ctx.get("hgnc")}
    elif module_key in {"assoc-sc","sc-hubmap"}:
        params = {"q": ctx.get("hgnc") or ctx.get("ensg")}
    elif module_key == "expr-localization":
        params = {"query": ctx.get("uniprot") or ctx.get("hgnc"), "fields":"cc_subcellular_location"}
    elif module_key == "assoc-bulk-prot":
        params = {"accession": ctx.get("uniprot")}
    elif module_key == "assoc-omics-phosphoproteomics":
        params = {"q": ctx.get("uniprot") or ctx.get("hgnc"), "pageSize": 25}
    elif module_key == "expr-inducibility":
        params = {"geneQuery": ctx.get("ensg") or ctx.get("hgnc")}
    elif module_key == "assoc-bulk-rna":
        params = {"db":"gds","term": (ctx.get("hgnc") or ctx.get("ensg"))}
    elif module_key == "assoc-metabolomics":
        params = {"study_type":"DISEASE"}
    elif module_key == "assoc-proteomics":
        params = {"name": ctx.get("hgnc")}
    elif module_key.startswith("tract-") and module_key != "tract-ligandability-oligo":
        params = {"query": ctx.get("uniprot") or ctx.get("hgnc")}
    elif module_key == "tract-ligandability-oligo":
        v = ctx.get("rsid") or ctx.get("region")
        if v:
            body = {"variants": [v]}
    elif module_key == "tract-drugs":
        params = {"molecule_chembl_id__icontains": ctx.get("hgnc")}
    elif module_key == "perturb-drug-response":
        params = {"target": ctx.get("hgnc")}
    elif module_key in {"clin-safety","clin-rwe"}:
        params = {"search": ctx.get("hgnc") or ctx.get("trait_label"), "count":"10"}
    elif module_key == "clin-on-target-ae-prior":
        params = {"search": ctx.get("hgnc")}
    elif module_key in {"clin-endpoints","clin-feasibility","clin-pipeline"}:
        params = {"cond": ctx.get("trait_label") or ctx.get("efo"), "term": ctx.get("hgnc")}
    elif module_key in {"clin-biomarker-fit"}:
        params = {"q": ctx.get("trait_label") or ctx.get("efo")}
    elif module_key.startswith("comp-"):
        params = {"q": ctx.get("hgnc") or ctx.get("trait_label") or ctx.get("ensg")}
    return params, body

# ============================================================================
# Module runner
# ============================================================================

async def run_module(module_key: str, ctx: Dict[str, Any]) -> EvidenceEnvelope:
    if module_key not in MODULES:
        raise HTTPException(status_code=400, detail=f"Unknown module: {module_key}")
    mod = MODULES[module_key]
    prov = Provenance(sources=[], query=ctx, module_order=[module_key])
    env = EvidenceEnvelope(module=module_key, context=EnvelopeContext(
        gene_id=ctx.get("ensg") or ctx.get("uniprot") or ctx.get("hgnc"),
        target=ctx.get("hgnc"),
        trait_id=ctx.get("efo") or ctx.get("trait_label"),
        variant_id=ctx.get("rsid") or ctx.get("region"),
        qtl_stage=mod.qtl_stage
    ))

    try:
        if mod.family == "graphql":
            template_key = mod.primary.get("template")
            gql = GQL_TEMPLATES.get(template_key, "")
            variables = {"ensg": ctx.get("ensg") or "", "efo": ctx.get("efo") or None}
            data = await _fetch(mod, mod.primary["url"], method="POST", json_body={"query": gql, "variables": variables})
        else:
            params, body = build_params(module_key, ctx)
            data = await _fetch(mod, mod.primary["url"], method=mod.primary.get("method","GET"), params=params, json_body=body)

        env.records = [{"raw": data}]
        env.edges = [Edge(kind="module_result", src=module_key, dst=env.context.gene_id, meta={"module": module_key, "stage": mod.qtl_stage})]
        prov.sources.append(mod.primary["url"])
        env.provenance = prov
        return env
    except HTTPException as e:
        # try fallbacks
        for fb in mod.fallbacks:
            try:
                data = await _fetch(mod, fb["url"], method=fb.get("method","GET"), params={}, json_body=None)
                env.records = [{"raw": data}]
                env.edges = [Edge(kind="module_result", src=module_key, dst=env.context.gene_id, meta={"module": module_key, "fallback": True})]
                prov.sources.append(fb["url"])
                env.provenance = prov
                return env
            except Exception:
                continue
        raise e

# ============================================================================
# Literature Seek Overlay (EPMC + Crossref + Unpaywall + PubTator3)
# ============================================================================

async def seek_literature(ctx: Dict[str, Any], edges: List[Edge], question_id: int) -> LitEvidence:
    # Build grounded query
    bits = []
    if ctx.get("hgnc"): bits.append(f'"{ctx["hgnc"]}"')
    if ctx.get("ensg"): bits.append(f'"{ctx["ensg"]}"')
    if ctx.get("trait_label"): bits.append(f'"{ctx["trait_label"]}"')
    if ctx.get("efo"): bits.append(f'"{ctx["efo"]}"')
    q = " AND ".join(bits) or "human"
    params = {"query": q, "format": "json", "pageSize": 25}
    hits = []
    tags: Dict[str, List[str]] = {}
    errors: List[str] = []
    try:
        data = await _fetch(MODULES["assoc-perturb"], EPMC_SEARCH, "GET", params)
        hits = data.get("resultList", {}).get("result", []) if isinstance(data, dict) else []
        # naive tagging: SUPPORTS if any hit; production would map to edge sentences via PubTator3
        for e in edges:
            key = f"{e.kind}:{e.src}->{e.dst}"
            tags[key] = ["SUPPORTS"] if hits else []
    except Exception as ex:
        errors.append(str(ex))
    return LitEvidence(query=q, hits=hits, tags=tags, errors=errors)

# ============================================================================
# Math synthesis (internal, scoreless) — stubs hooked per question
# ============================================================================

def math_noisy_or(summary: str, families: List[str]) -> Dict[str, Any]:
    return {"method": "Noisy-OR + heuristics", "families": families, "summary": summary}

def math_mr_sensitivity() -> Dict[str, Any]:
    return {"method": "MR (IVW/Egger/median/mode) + Steiger", "summary": "Direction & robustness across MR models"}

def math_pcst_mpp() -> Dict[str, Any]:
    return {"method": "Prize-Collecting Steiner Tree", "summary": "Minimal Plausible Pathway extracted from network edges"}

def math_multiview_fusion() -> Dict[str, Any]:
    return {"method": "Multi-view CCA/NMF", "summary": "Align e/s/p/mQTL & expression by tissue-consistent components"}

def math_signature_alignment() -> Dict[str, Any]:
    return {"method": "Signature alignment (cosine) + GSEA", "summary": "Concordance of perturbation with hypothesis"}

def math_selectivity_jsd() -> Dict[str, Any]:
    return {"method": "Jensen–Shannon divergence", "summary": "Selectivity of expression vs vital organs"}

def math_disproportionality() -> Dict[str, Any]:
    return {"method": "Pharmacovigilance PRR/ROR/EBGM", "summary": "Safety signal disproportion in FAERS with mechanism mapping"}

def math_mcda() -> Dict[str, Any]:
    return {"method": "Multi-criteria decision analysis", "summary": "Endpoint credibility × feasibility × time-to-readout"}

# ============================================================================
# Question plan (22) with modules & math
# ============================================================================

QUESTION_PLAN: Dict[int, Tuple[str, List[str], Callable[[], Dict[str, Any]]]] = {
    1: ("Robust human genetic causality?", ["genetics-l2g","genetics-coloc","genetics-mr","genetics-rare","genetics-mendelian","genetics-phewas-human-knockout","genetics-consortia-summary"], lambda: math_noisy_or("Combine families for causal stance", ["GWAS","coloc","MR","rare","PheWAS"])),
    2: ("Fine-mapping/coloc & direction consistent?", ["genetics-l2g","genetics-coloc","genetics-annotation","genetics-mr"], math_mr_sensitivity),
    3: ("cis-QTL chain coherent in right tissue?", ["genetics-sqtl","genetics-pqtl","genetics-regulatory","genetics-caqtl-lite","genetics-3d-maps","genetics-chromatin-contacts","genetics-mqtl-coloc"], math_multiview_fusion),
    4: ("Promoter/isoform/splicing/NMD mechanism?", ["genetics-sqtl","genetics-nmd-inference","genetics-ase-check","genetics-mavedb"], lambda: {"method": "Bayesian model selection", "summary": "Pick most plausible splice/NMD path"}),
    5: ("Protein/PTM supports RNA story?", ["genetics-pqtl","genetics-ptm-signal-lite","assoc-omics-phosphoproteomics","assoc-bulk-prot","assoc-proteomics"], lambda: {"method": "Graph alignment + consistency index", "summary": "Transcript→protein consistency"}),
    6: ("Perturbation reproduces direction?", ["assoc-perturb","perturb-crispr-screens","perturb-lincs-signatures","perturb-signature-enrichment","perturb-connectivity","perturb-perturbseq-encode"], math_signature_alignment),
    7: ("Concise mechanistic path to disease biology?", ["mech-directed-signaling","mech-tf-target","mech-kinase-substrate","mech-ligrec","mech-ppi","mech-complexes","mech-pathways","biology-causal-pathways"], math_pcst_mpp),
    8: ("Trans relay ≤3 nodes, tissue-matched, orthogonal?", ["genetics-lncrna","genetics-mirna","mech-ligrec","mech-tf-target","mech-mirna-target","assoc-perturb"], lambda: {"method": "Constrained path search", "summary": "Enforce relay length & orthogonality"}),
    9: ("Replication/heterogeneity & pleiotropy?", ["genetics-l2g","genetics-coloc","genetics-mr"], math_mr_sensitivity),
    10: ("Where active; off-tissue exposure risk?", ["expr-baseline","assoc-sc","sc-hubmap","assoc-spatial","expr-localization","assoc-bulk-prot","assoc-bulk-rna"], math_selectivity_jsd),
    11: ("Partial modulation tolerability?", ["genetics-intolerance","genetics-pathogenicity-priors","genetics-rare","genetics-phewas-human-knockout"], lambda: math_noisy_or("Tolerability from genetics + phenotypes", ["constraint","rare","knockout"])),
    12: ("Essentiality/dependency red flags?", ["function-dependency","perturb-depmap-dependency"], lambda: {"method": "Multi-objective filter", "summary": "Dependency × selectivity"}),
    13: ("Immunogenicity/HLA risks?", ["tract-iedb-epitopes","tract-mhc-binding","immuno/hla-coverage","tract-immunogenicity"], lambda: {"method": "Bayesian aggregation", "summary": "Observed + predicted epitopes × HLA frequencies"}),
    14: ("Which modality is most viable?", ["mech-structure","tract-ligandability-sm","tract-ligandability-ab","tract-surfaceome","tract-ligandability-oligo","tract-modality","tract-drugs"], lambda: {"method": "Rule-based + Pareto", "summary": "Feasible/fast/risk trade-offs"}),
    15: ("Starting points & polypharmacology stance?", ["tract-drugs","perturb-drug-response","mech-ppi","mech-pathways"], lambda: {"method": "Chemotype clusters + network proximity", "summary": "Interpret series vs biology"}),
    16: ("Reach site-of-action (systemic/local/BBB)?", ["expr-baseline","assoc-sc","expr-localization"], lambda: {"method": "Constraint satisfaction", "summary": "Tissue × modality × route"}),
    17: ("NA therapy: LNP/vehicle & surface markers?", ["assoc-sc","sc-hubmap","tract-ligandability-oligo"], lambda: {"method": "Bipartite matching", "summary": "Marker ↔ vehicle"}),
    18: ("Fastest Proof-of-Mechanism?", ["assoc-perturb","perturb-crispr-screens","mech-pathways","expr-baseline","assoc-sc"], lambda: {"method": "Value of information + PCST", "summary": "Minimal readout to change decision"}),
    19: ("Models/assays with best translation fidelity?", ["assoc-sc","expr-inducibility","assoc-bulk-prot","assoc-bulk-rna"], lambda: {"method": "Multi-view concordance + ladder", "summary": "Rank model sequences"}),
    20: ("TE & response PD markers repeatable in humans?", ["assoc-bulk-prot","assoc-omics-phosphoproteomics","perturb-lincs-signatures","mech-pathways"], lambda: {"method": "Signature→endpoint + communities", "summary": "Coherent PD panels"}),
    21: ("Clinical safety priors & RWE signals?", ["clin-safety","clin-rwe","clin-on-target-ae-prior"], math_disproportionality),
    22: ("Clinical translation landscape (endpoints/feasibility/pipeline)?", ["clin-endpoints","clin-biomarker-fit","clin-feasibility","clin-pipeline"], math_mcda),
}

# Skeptic & Killer (short summaries)
SKEPTIC: Dict[int, List[str]] = {
    1:["LD confounding; ancestry artifacts; publication bias"],
    2:["PP3 vs PP4 ties; MR pleiotropy"],
    3:["Tissue mismatch; assay batch effects"],
    4:["Isoform misannotation; NMD prediction uncertainty"],
    5:["Proteomics underpower; PTM site ambiguity"],
    6:["Cell-line drift; off-target CRISPR effects"],
    7:["Network hairball; direction mis-assignments"],
    8:["Relay overfitting; mediator not expressed"],
    9:["Heterogeneity across cohorts; weak instruments"],
    10:["Off-tissue high baseline compromises window"],
    11:["Constraint not indication-specific"],
    12:["Dependency context not human-relevant"],
    13:["Epitope prediction false positives; HLA sampling bias"],
    14:["Cryptic pockets; epitope masking; internalization kinetics"],
    15:["Tool artifacts; assay interference"],
    16:["Route infeasible; BBB barrier"],
    17:["Poor uptake; endosomal escape limits"],
    18:["Readout not proximal enough"],
    19:["Species differences; model missing cell types"],
    20:["PD panel not specific; matrix impractical"],
    21:["Confounding/reporting bias in FAERS"],
    22:["Endpoint not accepted; recruitment unrealistic"],
}
KILLER: Dict[int, List[str]] = {
    1:["Replication in new ancestry cohort; coloc PP4 dominance check"],
    2:["Conditional coloc in target tissue; MR Steiger direction test"],
    3:["Tissue-matched QTL re-run; enhancer reporter assay"],
    4:["Mini-gene splice assay; junction RT-PCR in primary cells"],
    5:["Targeted phospho-proteomics at implicated sites"],
    6:["CRISPR KO/KD in disease-primary cells; LINCS signature concordance"],
    7:["Edge-level KO/OE with rescue; pathway node readouts"],
    8:["Mediator CRISPRi/a; secreted factor blockade"],
    9:["Leave-one-study MR; ancestry-stratified replication"],
    10:["Single-cell confirm in tissue; spatial ISH"],
    11:["Human knockout cohort phenotyping"],
    12:["Essentiality screen in primary vital-organ cells"],
    13:["Tetramer assays; population HLA binding assays"],
    14:["Epitope exposure/shedding tests; pocket ligandability NMR"],
    15:["SAR triage; off-target panel profiling"],
    16:["Device/formulation pilot in ex vivo tissue"],
    17:["LNP uptake in organoids; receptor competition"],
    18:["Minimal PD marker demo in human-relevant system"],
    19:["Cross-species isoform/PTM mapping"],
    20:["Longitudinal PD reproducibility study"],
    21:["Mechanism-linked AE disproportionality re-check"],
    22:["Feasibility pilot; endpoint surrogate validation"],
}

# ============================================================================
# Question runner and final synthesis
# ============================================================================

async def _run_question(qid: int, ctx: Dict[str, Any]) -> QuestionAnswer:
    title, modules, math_fn = QUESTION_PLAN[qid]
    envelopes: List[EvidenceEnvelope] = []
    for i in range(0, len(modules), 5):
        burst = modules[i:i+5]
        results = await asyncio.gather(*[run_module(m, ctx) for m in burst])
        envelopes.extend(results)
    all_edges = [e for env in envelopes for e in env.edges]
    lit = await seek_literature(ctx, all_edges, qid)
    math_summary = math_fn() if callable(math_fn) else {}
    return QuestionAnswer(
        question_id=qid,
        title=title,
        envelopes=envelopes,
        literature=lit,
        math_summary=math_summary,
        skeptic_view=SKEPTIC.get(qid, []),
        killer_experiments=KILLER.get(qid, []),
    )

def _final_recommendation(answers: List[QuestionAnswer]) -> FinalRecommendation:
    why = [f"Q{a.question_id}: {a.title}" for a in answers]
    modality_plan = {"primary": "TBD (from Q14)", "alternates": ["TBD"], "delivery": "TBD (Q16/Q17)"}
    pd_plan = {"target_engagement": "TBD (Q20)", "response": ["TBD"], "matrix": "plasma/CSF/tissue TBD"}
    risk_register = {"on_target": ["TBD (Q10-Q13)"], "class_mechanism": ["TBD (Q21)"]}
    mos_plan = {"anchor": "MABEL/NOAEL", "controls": ["dose", "route", "schedule"]}
    fastest_pom = {"experiment": "TBD (Q18)", "readout": "TBD"}
    next_experiments = ["Top 3 from KILLER lists aggregated"]
    decision = "Go-with-plan"
    return FinalRecommendation(
        decision=decision,
        why=why,
        modality_plan=modality_plan,
        pd_plan=pd_plan,
        risk_register=risk_register,
        mos_plan=mos_plan,
        fastest_pom=fastest_pom,
        next_experiments=next_experiments,
        differentiation=None,
    )

# ============================================================================
# Public endpoint
# ============================================================================

@router.post("/run", response_model=RunResponse)
async def run_gateway(req: RunRequest) -> RunResponse:
    gene = _normalize_gene(req)
    trait = _normalize_trait(req)
    var = _normalize_variant(req)
    ctx = {**gene, **trait, **var}
    qids = req.run_questions or list(range(1, 23))
    answers: List[QuestionAnswer] = []
    for qid in qids:
        if qid not in QUESTION_PLAN:
            raise HTTPException(status_code=400, detail=f"Question {qid} not in 1..22")
        ans = await _run_question(qid, ctx)
        answers.append(ans)
    final = _final_recommendation(answers)
    return RunResponse(context=ctx, questions=answers, final_recommendation=final)



# ----------------------------------------------------------------------------
# Zero-data detection (generic heuristics)
# ----------------------------------------------------------------------------
def _is_no_data(payload):
    try:
        if payload is None:
            return True
        if isinstance(payload, (list, tuple, set)) and len(payload) == 0:
            return True
        if isinstance(payload, dict):
            if not payload:
                return True
            if "rows" in payload and isinstance(payload["rows"], list) and len(payload["rows"]) == 0:
                return True
            rl = payload.get("resultList")
            if isinstance(rl, dict) and len(rl.get("result", []) or []) == 0:
                return True
    except Exception:
        pass
    return False



@router.get("/status")
async def status():
    # report circuit + semaphore + overrides
    try:
        hosts = {}
        for host in sorted(list(HOST_ALLOW or set())):
            st = _host_state.get(host, {})
            sem = _host_semaphores.get(host)
            ov = HOST_OVERRIDES.get(host, {})
            hosts[host] = {
                "circuit": {
                    "open": bool(st.get("open_until", 0) > _now_ts()),
                    "open_until": st.get("open_until", 0),
                    "fails": st.get("fails", 0),
                    "threshold": CB_FAILURE_THRESHOLD,
                    "open_seconds": CB_OPEN_SECONDS,
                },
                "semaphore": getattr(sem, "_value", None) if sem else None,
                "overrides": ov,
            }
        return {
            "hosts": hosts,
            "global_concurrency": GLOBAL_CONCURRENCY,
            "defaults": {
                "retry_attempts": HTTP_RETRY_ATTEMPTS,
                "retry_base": HTTP_RETRY_BASE,
                "timeouts": {
                    "connect": HTTP_CONNECT_TIMEOUT,
                    "read": HTTP_READ_TIMEOUT,
                    "write": HTTP_WRITE_TIMEOUT
                },
                "cb": {"threshold": CB_FAILURE_THRESHOLD, "open_seconds": CB_OPEN_SECONDS},
                "ttl": {"FAST": TTL_FAST, "MODERATE": TTL_MODERATE, "SLOW": TTL_SLOW},
            },
        }
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"status endpoint error: {ex}")
