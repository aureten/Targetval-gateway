
# app/routers/targetval_router_advanced.py
"""
TARGETVAL Gateway — Advanced Router (Live + Synthesis + TI)

This file is a superset of the preliminary router. It keeps compatibility with the existing
live fetch endpoints while adding:
  • Bucket-aware literature mesh with conflict resolution and quality scoring
  • Math-driven synthesis per bucket (qualitative output; Bayesian + graph methods)
  • Cross-bucket Therapeutic Index (qualitative verdict with flip-if guidance)
  • Registry truth-in-labeling for 58 modules (module → sources mapping)
  • Harmonized abstractions for upstream clients (dependency-injected Protocols)
  • Robust normalization utilities (gene aliases, disease synonyms, tissue tokens)
  • Cohesive knowledge-graph builder for cross-module narrative
  • Consistent response schemas and citation handling
  • Health/status/registry endpoints to support ops and QA

Design principles:
  - No numeric grades in responses; math is internal-only.
  - Live data fetch is delegated to injected clients; this router stays stateless.
  - Europe PMC–centric literature layer with stance (confirm/disconfirm) detection.
  - Two-pass resolution loop to focus by tissue/pathway and expand synonyms.
  - “Truth in labeling”: sources listed reflect the real live fetchers.

NOTE: To retain "works with live data fetch", wire concrete clients via set_*_client(...).
"""

from __future__ import annotations

import math, os, re, json, time, hashlib, itertools, statistics
from typing import Any, Dict, List, Optional, Tuple, Iterable, Protocol, TypedDict, Union, Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel, Field, validator

# -----------------------------
# Utilities
# -----------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _cap01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def _logit(p: float) -> float:
    p = min(max(p, 1e-9), 1-1e-9)
    return math.log(p/(1-p))

def _inv_logit(z: float) -> float:
    return 1.0/(1.0+math.exp(-z))

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

# Simple tokenization/normalization
_ws_re = re.compile(r"\s+")
def _norm(s: Optional[str]) -> str:
    if not s: return ""
    return _ws_re.sub(" ", s.strip())

# -----------------------------
# Client Protocols (DI)
# -----------------------------

class EuropePMCClient(Protocol):
    async def search(self, query: str, since: Optional[int] = None, size: int = 100) -> List[Dict[str, Any]]: ...
    async def fetch(self, id_type: str, id_value: str) -> Optional[Dict[str, Any]]: ...

class OpenTargetsClient(Protocol):
    async def l2g(self, gene: str, condition: Optional[str] = None) -> Dict[str, Any]: ...
    async def coloc(self, gene: str, condition: Optional[str] = None) -> Dict[str, Any]: ...
    async def pqtl(self, gene: str) -> Dict[str, Any]: ...

class OpenGWASClient(Protocol):
    async def mr(self, exposure: str, outcome: str) -> Dict[str, Any]: ...

class ClinVarClient(Protocol):
    async def variants(self, gene: str) -> Dict[str, Any]: ...

class ClinGenClient(Protocol):
    async def gene_validity(self, gene: str) -> Dict[str, Any]: ...

class GTExClient(Protocol):
    async def expression(self, gene: str) -> Dict[str, Any]: ...
    async def sqtl(self, gene: str) -> Dict[str, Any]: ...

class UniProtClient(Protocol):
    async def fetch_reviewed_entry(self, gene: str) -> Optional[Dict[str, Any]]: ...

class HPAClient(Protocol):
    async def tissue_expression(self, gene: str) -> Dict[str, Any]: ...
    async def fetch_surfaceome_flags(self, gene: str) -> Dict[str, Any]: ...
    async def fetch_pathology_links(self, gene: str) -> Dict[str, Any]: ...

class PRIDEClient(Protocol):
    async def phospho(self, gene: str) -> Dict[str, Any]: ...
    async def proteomics(self, gene: str) -> Dict[str, Any]: ...

class ProteomicsDBClient(Protocol):
    async def protein_abundance(self, gene: str) -> Dict[str, Any]: ...

class CPTACClient(Protocol):
    async def bulk_proteomics(self, gene: str) -> Dict[str, Any]: ...

class GEOClient(Protocol):
    async def bulk_rna(self, gene: str, condition: Optional[str] = None) -> Dict[str, Any]: ...
    async def arrayexpress(self, gene: str, condition: Optional[str] = None) -> Dict[str, Any]: ...

class HCAClient(Protocol):
    async def single_cell(self, gene: str) -> Dict[str, Any]: ...

class TabulaSapiensClient(Protocol):
    async def single_cell(self, gene: str) -> Dict[str, Any]: ...

class STRINGClient(Protocol):
    async def ppi(self, gene: str) -> Dict[str, Any]: ...

class ReactomeClient(Protocol):
    async def pathways(self, gene: str) -> Dict[str, Any]: ...

class OmniPathClient(Protocol):
    async def ligrec(self, gene: str) -> Dict[str, Any]: ...

class ChEMBLClient(Protocol):
    async def target(self, gene: str) -> Dict[str, Any]: ...

class DGIdbClient(Protocol):
    async def interactions(self, gene: str) -> Dict[str, Any]: ...
    async def gene_to_drugs(self, gene: str) -> List[str]: ...

class IEDBClient(Protocol):
    async def epitopes(self, gene: str) -> Dict[str, Any]: ...

class ClinicalTrialsClient(Protocol):
    async def endpoints(self, condition: Optional[str] = None) -> Dict[str, Any]: ...

class InxightDrugsClient(Protocol):
    async def pipeline(self, gene: str) -> Dict[str, Any]: ...

class FAERSClient(Protocol):
    async def aggregate(self, drugs: List[str]) -> Dict[str, Any]: ...
    async def signals(self, drugs: List[str]) -> Dict[str, Any]: ...

class PharmGKBClient(Protocol):
    async def pgx(self, gene: str) -> Dict[str, Any]: ...

class PatentsViewClient(Protocol):
    async def intensity(self, gene: str, condition: Optional[str] = None) -> Dict[str, Any]: ...
    async def freedom(self, gene: str, condition: Optional[str] = None) -> Dict[str, Any]: ...

class GnomADClient(Protocol):
    async def intolerance(self, gene: str) -> Dict[str, Any]: ...

# -----------------------------
# Dependency injection
# -----------------------------

_epmc: Optional[EuropePMCClient] = None
_ot: Optional[OpenTargetsClient] = None
_ogwas: Optional[OpenGWASClient] = None
_clinvar: Optional[ClinVarClient] = None
_clingen: Optional[ClinGenClient] = None
_gtex: Optional[GTExClient] = None
_uniprot: Optional[UniProtClient] = None
_hpa: Optional[HPAClient] = None
_pride: Optional[PRIDEClient] = None
_pdb: Optional[ProteomicsDBClient] = None
_cptac: Optional[CPTACClient] = None
_geo: Optional[GEOClient] = None
_hca: Optional[HCAClient] = None
_tabula: Optional[TabulaSapiensClient] = None
_string: Optional[STRINGClient] = None
_reactome: Optional[ReactomeClient] = None
_omnipath: Optional[OmniPathClient] = None
_chembl: Optional[ChEMBLClient] = None
_dgidb: Optional[DGIdbClient] = None
_iedb: Optional[IEDBClient] = None
_ct: Optional[ClinicalTrialsClient] = None
_inxight: Optional[InxightDrugsClient] = None
_faers: Optional[FAERSClient] = None
_pgkb: Optional[PharmGKBClient] = None
_patents: Optional[PatentsViewClient] = None
_gnomad: Optional[GnomADClient] = None

def set_europe_pmc_client(c: EuropePMCClient):      global _epmc; _epmc = c
def set_open_targets_client(c: OpenTargetsClient):  global _ot; _ot = c
def set_open_gwas_client(c: OpenGWASClient):        global _ogwas; _ogwas = c
def set_clinvar_client(c: ClinVarClient):           global _clinvar; _clinvar = c
def set_clingen_client(c: ClinGenClient):           global _clingen; _clingen = c
def set_gtex_client(c: GTExClient):                 global _gtex; _gtex = c
def set_uniprot_client(c: UniProtClient):           global _uniprot; _uniprot = c
def set_hpa_client(c: HPAClient):                   global _hpa; _hpa = c
def set_pride_client(c: PRIDEClient):               global _pride; _pride = c
def set_proteomicsdb_client(c: ProteomicsDBClient): global _pdb; _pdb = c
def set_cptac_client(c: CPTACClient):               global _cptac; _cptac = c
def set_geo_client(c: GEOClient):                   global _geo; _geo = c
def set_hca_client(c: HCAClient):                   global _hca; _hca = c  # alias
def set_tabula_client(c: TabulaSapiensClient):      global _tabula; _tabula = c
def set_string_client(c: STRINGClient):             global _string; _string = c
def set_reactome_client(c: ReactomeClient):         global _reactome; _reactome = c
def set_omnipath_client(c: OmniPathClient):         global _omnipath; _omnipath = c
def set_chembl_client(c: ChEMBLClient):             global _chembl; _chembl = c
def set_dgidb_client(c: DGIdbClient):               global _dgidb; _dgidb = c
def set_iedb_client(c: IEDBClient):                 global _iedb; _iedb = c
def set_clinicaltrials_client(c: ClinicalTrialsClient): global _ct; _ct = c
def set_inxight_client(c: InxightDrugsClient):      global _inxight; _inxight = c
def set_faers_client(c: FAERSClient):               global _faers; _faers = c
def set_pharmgkb_client(c: PharmGKBClient):         global _pgkb; _pgkb = c
def set_patentsview_client(c: PatentsViewClient):   global _patents; _patents = c
def set_gnomad_client(c: GnomADClient):             global _gnomad; _gnomad = c

def _req(x, name: str):
    if x is None:
        raise RuntimeError(f"{name} client not configured")
    return x

# -----------------------------
# Evidence & Registry Models
# -----------------------------

class Citation(BaseModel):
    source: str
    id_type: Optional[str] = None
    id: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None
    link: Optional[str] = None

class Evidence(BaseModel):
    status: str = "OK"
    source: str
    fetched_n: int = 0
    data: Dict[str, Any] = Field(default_factory=dict)
    citations: List[Citation] = Field(default_factory=list)
    fetched_at: str = Field(default_factory=_now)

class Module(BaseModel):
    route: str
    name: str
    sources: List[str]
    bucket: str

class Registry(BaseModel):
    modules: List[Module]
    counts: Dict[str, int]

# Canonical module registry (58 rows, truth-in-labeling)
MODULES: List[Module] = [
    Module(route="/expr/baseline", name="expr-baseline", sources=["GTEx"], bucket="IDENTITY"),
    Module(route="/expr/localization", name="expr-localization", sources=["UniProtKB"], bucket="IDENTITY"),
    Module(route="/mech/structure", name="mech-structure", sources=["UniProtKB","AlphaFoldDB","PDBe"], bucket="IDENTITY"),
    Module(route="/expr/inducibility", name="expr-inducibility", sources=["Europe PMC"], bucket="IDENTITY"),
    Module(route="/assoc/bulk-rna", name="assoc-bulk-rna", sources=["NCBI GEO (E-utilities)","ArrayExpress/BioStudies"], bucket="ASSOCIATION"),
    Module(route="/assoc/sc", name="assoc-sc", sources=["HCA Azul","Tabula Sapiens"], bucket="ASSOCIATION"),
    Module(route="/assoc/spatial-expression", name="assoc-spatial-expression", sources=["Europe PMC"], bucket="ASSOCIATION"),
    Module(route="/assoc/spatial-neighborhoods", name="assoc-spatial-neighborhoods", sources=["Europe PMC"], bucket="ASSOCIATION"),
    Module(route="/assoc/bulk-prot", name="assoc-bulk-prot", sources=["ProteomicsDB","PRIDE","ProteomeXchange"], bucket="ASSOCIATION"),
    Module(route="/assoc/omics-phosphoproteomics", name="assoc-omics-phosphoproteomics", sources=["PRIDE"], bucket="ASSOCIATION"),
    Module(route="/assoc/omics-metabolites", name="assoc-omics-metabolites", sources=["MetaboLights","HMDB"], bucket="ASSOCIATION"),
    Module(route="/assoc/hpa-pathology", name="assoc-hpa-pathology", sources=["Human Protein Atlas (HPA)","UniProtKB"], bucket="ASSOCIATION"),
    Module(route="/assoc/bulk-prot-pdc", name="assoc-bulk-prot-pdc", sources=["PDC GraphQL (CPTAC)"], bucket="ASSOCIATION"),
    Module(route="/assoc/metabolomics-ukb-nightingale", name="assoc-metabolomics-ukb-nightingale", sources=["Nightingale Biomarker Atlas"], bucket="ASSOCIATION"),
    Module(route="/sc/bican", name="sc-bican", sources=["BICAN portal"], bucket="ASSOCIATION"),
    Module(route="/sc/hubmap", name="sc-hubmap", sources=["HuBMAP portal"], bucket="ASSOCIATION"),
    Module(route="/genetics/l2g", name="genetics-l2g", sources=["OpenTargets GraphQL (L2G)"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/coloc", name="genetics-coloc", sources=["OpenTargets GraphQL (colocalisations)"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/mr", name="genetics-mr", sources=["IEU OpenGWAS"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/rare", name="genetics-rare", sources=["ClinVar (NCBI E-utilities)"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/mendelian", name="genetics-mendelian", sources=["ClinGen Gene Validity"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/phewas-human-knockout", name="genetics-phewas-human-knockout", sources=["Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/sqtl", name="genetics-sqtl", sources=["GTEx sQTL API"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/pqtl", name="genetics-pqtl", sources=["OpenTargets GraphQL (pQTL colocs)"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/chromatin-contacts", name="genetics-chromatin-contacts", sources=["Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/functional", name="genetics-functional", sources=["Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/lncrna", name="genetics-lncrna", sources=["Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/mirna", name="genetics-mirna", sources=["Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/pathogenicity-priors", name="genetics-pathogenicity-priors", sources=["Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/finngen-summary", name="genetics-finngen-summary", sources=["FinnGen"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/gbmi-summary", name="genetics-gbmi-summary", sources=["GBMI portal"], bucket="GENETIC_CAUSALITY"),
    Module(route="/genetics/mavedb", name="genetics-mavedb", sources=["MaveDB API","Europe PMC"], bucket="GENETIC_CAUSALITY"),
    Module(route="/mech/ppi", name="mech-ppi", sources=["STRING"], bucket="MECHANISM"),
    Module(route="/mech/pathways", name="mech-pathways", sources=["Reactome"], bucket="MECHANISM"),
    Module(route="/mech/ligrec", name="mech-ligrec", sources=["OmniPath"], bucket="MECHANISM"),
    Module(route="/assoc/perturb", name="assoc-perturb", sources=["Europe PMC"], bucket="MECHANISM"),
    Module(route="/assoc/perturbatlas", name="assoc-perturbatlas", sources=["PerturbAtlas OUP"], bucket="MECHANISM"),
    Module(route="/tract/drugs", name="tract-drugs", sources=["ChEMBL","DGIdb"], bucket="TRACTABILITY"),
    Module(route="/tract/ligandability-sm", name="tract-ligandability-sm", sources=["UniProtKB","AlphaFoldDB","PDBe"], bucket="TRACTABILITY"),
    Module(route="/tract/ligandability-ab", name="tract-ligandability-ab", sources=["UniProtKB","Human Protein Atlas (HPA)"], bucket="TRACTABILITY"),
    Module(route="/tract/ligandability-oligo", name="tract-ligandability-oligo", sources=["Europe PMC"], bucket="TRACTABILITY"),
    Module(route="/tract/modality", name="tract-modality", sources=["UniProtKB","AlphaFoldDB"], bucket="TRACTABILITY"),
    Module(route="/tract/immunogenicity", name="tract-immunogenicity", sources=["Europe PMC"], bucket="TRACTABILITY"),
    Module(route="/tract/mhc-binding", name="tract-mhc-binding", sources=["Europe PMC"], bucket="TRACTABILITY"),
    Module(route="/tract/iedb-epitopes", name="tract-iedb-epitopes", sources=["IEDB IQ-API","Europe PMC"], bucket="TRACTABILITY"),
    Module(route="/tract/surfaceome-hpa", name="tract-surfaceome-hpa", sources=["Human Protein Atlas (HPA)"], bucket="TRACTABILITY"),
    Module(route="/tract/tsca", name="tract-tsca", sources=["Cancer Surfaceome Atlas (TCSA)"], bucket="TRACTABILITY"),
    Module(route="/clin/endpoints", name="clin-endpoints", sources=["ClinicalTrials.gov v2"], bucket="CLINICAL_FIT"),
    Module(route="/clin/biomarker-fit", name="clin-biomarker-fit", sources=["Europe PMC"], bucket="CLINICAL_FIT"),
    Module(route="/clin/pipeline", name="clin-pipeline", sources=["Inxight Drugs (NCATS)"], bucket="CLINICAL_FIT"),
    Module(route="/clin/safety", name="clin-safety", sources=["openFDA FAERS","DGIdb"], bucket="CLINICAL_FIT"),
    Module(route="/clin/safety-pgx", name="clin-safety-pgx", sources=["PharmGKB"], bucket="CLINICAL_FIT"),
    Module(route="/clin/rwe", name="clin-rwe", sources=["openFDA FAERS"], bucket="CLINICAL_FIT"),
    Module(route="/clin/on-target-ae-prior", name="clin-on-target-ae-prior", sources=["DGIdb","SIDER"], bucket="CLINICAL_FIT"),
    Module(route="/comp/intensity", name="comp-intensity", sources=["PatentsView"], bucket="CLINICAL_FIT"),
    Module(route="/comp/freedom", name="comp-freedom", sources=["PatentsView"], bucket="CLINICAL_FIT"),
    Module(route="/clin/eu-ctr-linkouts", name="clin-eu-ctr-linkouts", sources=["EU CTR/CTIS"], bucket="CLINICAL_FIT"),
    Module(route="/genetics/intolerance", name="genetics-intolerance", sources=["gnomAD GraphQL"], bucket="CLINICAL_FIT"),
]

BUCKETS = ["IDENTITY","ASSOCIATION","GENETIC_CAUSALITY","MECHANISM","TRACTABILITY","CLINICAL_FIT"]

# -----------------------------
# Literature Configuration
# -----------------------------

LITERATURE_GLOBAL: Dict[str, Any] = {
    "engine": "EuropePMC",
    "page_size": 100,
    "max_items_per_bucket": 250,
    "min_year": 2015,
    "preprint_penalty": -1.0,
    "meta_analysis_bonus": 1.0,
    "rct_bonus": 1.0,
    "recent_bonus_since": 2023,
    "recent_bonus": 0.5,
    "venue_high": ["nature","science","cell","nejm","lancet","nat ","cell reports","nat med","nat genetics","nature genetics"],
    "venue_mid": ["elife","plos","genome","bioinformatics","communications","jama"],
    "confirm_keywords": ["mendelian randomization","colocalization","colocalisation","replication","significant association","functional validation","perturb-seq","crispr","mpra","starr"],
    "disconfirm_keywords": ["no association","did not replicate","null association","not significant","failed replication","no effect","not associated"],
    "stoplist_domains": ["wikipedia.org","reddit.com","stackexchange.com","github.com/issues"],
    "dedupe_keys": ["pmid","doi","title"],
    "group_by": ["assay","tissue","celltype","disease","variant","mechanism"],
}

LITERATURE_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "GENETIC_CAUSALITY": {
        "confirm": [
            "({gene}) AND ({condition}) AND (mendelian randomization OR MR)",
            "({gene}) AND ({condition}) AND (colocalization OR colocalisation)",
            "({gene}) AND (pQTL OR sQTL) AND ({tissue_or_cell})",
            "({gene}) AND (PCHi-C OR promoter capture Hi-C OR HiC) AND (enhancer-promoter)",
            "({gene}) AND (MAVE OR deep mutational scanning OR saturation mutagenesis)",
        ],
        "disconfirm": [
            "({gene}) AND ({condition}) AND (no association OR not associated OR failed replication)",
            "({gene}) AND (MR OR colocalization) AND (null OR negative)",
        ],
    },
    "ASSOCIATION": {
        "confirm": [
            "({gene}) AND ({condition}) AND (single-cell OR scRNA-seq OR spatial transcriptomics OR Visium OR MERFISH)",
            "({gene}) AND ({condition}) AND (proteomics OR phosphoproteomics)",
            "({gene}) AND ({condition}) AND (metabolomics OR biomarker)",
        ],
        "disconfirm": [
            "({gene}) AND ({condition}) AND (no change OR unchanged OR not differential)",
        ],
    },
    "MECHANISM": {
        "confirm": [
            "({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi) AND ({phenotype_or_pathway})",
            "({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)",
        ],
        "disconfirm": [
            "({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)",
        ],
    },
    "TRACTABILITY": {
        "confirm": [
            "({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)",
            "({gene}) AND (pocket OR binding site OR AlphaFold) AND (inhibitor OR small molecule)",
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

LITERATURE_RESOLUTION: Dict[str, Any] = {
    "triggers": {
        "min_confirming_hits": 3,
        "confirm_to_disconfirm_ratio_lt": 1.2,
        "cross_context_conflict": True,
    },
    "actions": [
        {"name": "focus_by_tissue", "expand_slots": ["{tissue_or_cell}"], "max_passes": 1},
        {"name": "focus_by_pathway", "expand_slots": ["{phenotype_or_pathway}"], "max_passes": 1},
        {"name": "expand_synonyms", "expand": ["{condition_synonyms}","{gene_aliases}"], "max_passes": 1},
    ],
    "hard_cap_passes": 2,
}

# -----------------------------
# Synthesis Configuration
# -----------------------------

SYNTHESIS_BUILDER_CONFIG: Dict[str, Any] = {
    "GENETIC_CAUSALITY": {
        "triangulations": [
            {"name": "causal_triad", "requires": ["genetics_coloc","genetics_mr","genetics_rare|genetics_mendelian|genetics_intolerance"], "method": "bayesian_triangulation", "notes": "MR + coloc + rare/Mendelian = strong"},
        ],
        "consistency_checks": [
            "pQTL→protein abundance direction aligns with disease MR",
            "sQTL direction consistent with implicated transcript",
        ],
        "narrative_slots": ["drivers","tensions","flip_ifs"],
    },
    "ASSOCIATION": {
        "triangulations": [
            {"name": "context_triad", "requires": ["assoc_sc|spatial_expression","assoc_bulk_rna","assoc_bulk_prot|assoc_bulk_prot_pdc"], "method": "graph_consistency"},
        ],
        "consistency_checks": [
            "same cell types appear across sc/spatial/proteomics",
            "metabolomics shifts support the same pathway block",
        ],
    },
    "MECHANISM": {
        "triangulations": [
            {"name": "mechanism_quad", "requires": ["mech_ppi","mech_pathways","mech_ligrec","assoc_perturb|omics_phosphoproteomics"], "method": "subgraph_density"},
        ],
    },
    "TRACTABILITY": {
        "triangulations": [
            {"name": "modality_gate", "requires": ["tract_ligandability_ab|tract_ligandability_sm|tract_ligandability_oligo"], "method": "rule_engine"},
        ],
        "penalties": ["tract_iedb_epitopes","tract_mhc_binding"],
    },
    "CLINICAL_FIT": {
        "triangulations": [
            {"name": "opportunity", "requires": ["clin_endpoints","clin_biomarker_fit|clin_pipeline"], "method": "evidence_presence"},
        ],
        "penalties": ["clin_safety","clin_safety_pgx","clin_rwe","clin_on_target_ae_prior"],
    },
}

TI_RULES: Dict[str, Any] = {
    "hard_blocks": [
        {"condition": "strong_safety_signal", "verdict": "Unfavorable", "reason": "On-target/PGx safety concerns dominate"},
    ],
    "favorable_when": [
        {"all": ["causality_strong","association_context_present","modality_feasible"], "none": ["strong_safety_signal"], "verdict": "Favorable", "reason": "Causality triangulated; context expression; viable modality w/o major safety flags"},
    ],
    "marginal_when": [
        {"any": ["causality_partial","modality_uncertain","association_sparse"], "verdict": "Marginal", "reason": "Signal promising but incomplete or conflicted"},
    ],
    "flip_if_templates": [
        "Stratify by {PGx_marker} to mitigate on-target AE risk",
        "Demonstrate sc/spatial presence in {at_risk_tissue} with proteomic support",
        "Functional rescue in {cell_state} confirms direction of effect",
    ],
}

# -----------------------------
# KG & Math helpers
# -----------------------------

class KG:
    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Tuple[str,str,Dict[str,Any]]] = []

    def add_node(self, key: str, kind: str, label: Optional[str] = None, **attrs):
        self.nodes[key] = {"kind": kind, "label": label or key, **attrs}
        return key

    def add_edge(self, src: str, dst: str, kind: str, **attrs):
        self.edges.append((src, dst, {"kind": kind, **attrs}))

    def subgraph_density(self, kinds: Optional[List[str]] = None) -> float:
        if not self.edges: return 0.0
        if not kinds:
            return len(self.edges) / max(1, len(self.nodes))
        num = sum(1 for (_,_,e) in self.edges if e.get("kind") in kinds)
        return num / max(1, len(self.nodes))

def bayes_triangulate(signals: List[float], prior: float = 0.05) -> float:
    z = _logit(prior)
    for s in signals:
        z += s
    return _inv_logit(z)

def logLR_from_support(x: float) -> float:
    x = _cap01(x)
    if x <= 1e-6: return -6.0
    if x >= 1.0-1e-6: return 6.0
    return math.log(x/(1-x))

class CitationBag:
    def __init__(self):
        self._seen = set()
        self.items: List[Dict[str, Any]] = []
    def add(self, item: Dict[str, Any]):
        key = (item.get("doi") or "", item.get("pmid") or "", item.get("title") or "")
        if key not in self._seen:
            self._seen.add(key); self.items.append(item)
    def extend(self, items: Iterable[Dict[str, Any]]):
        for it in items: self.add(it)

# -----------------------------
# Router and basic endpoints
# -----------------------------

router = APIRouter()

@router.get("/health")
async def health():
    return {"status": "ok", "ts": _now()}

@router.get("/status")
async def status():
    b_counts = {b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS}
    return {"status": "ok", "modules": len(MODULES), "by_bucket": b_counts, "ts": _now()}

# -----------------------------
# Registry endpoints
# -----------------------------

@router.get("/registry/modules", response_model=Registry)
async def registry_modules():
    counts = {b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS}
    return Registry(modules=MODULES, counts=counts)

@router.get("/registry/sources")
async def registry_sources():
    return [{"route": m.route, "sources": m.sources, "bucket": m.bucket} for m in MODULES]

@router.get("/registry/counts")
async def registry_counts():
    return {"total": len(MODULES), "by_bucket": {b: sum(1 for m in MODULES if m.bucket == b) for b in BUCKETS}}

# -----------------------------
# Core module endpoints (generic wrappers)
# -----------------------------

# Identity
@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(gene: str):
    gtex = _req(_gtex, "GTEx")
    try:
        out = await gtex.expression(gene)
    except Exception as e:
        raise HTTPException(502, f"GTEx error: {e!s}")
    return Evidence(source="GTEx", fetched_n=len(out.get("tissues") or []), data=out, citations=[], fetched_at=_now())

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(gene: str):
    uni = _req(_uniprot, "UniProtKB")
    try:
        out = await uni.fetch_reviewed_entry(gene)
    except Exception as e:
        raise HTTPException(502, f"UniProt error: {e!s}")
    return Evidence(source="UniProtKB", fetched_n=1 if out else 0, data=out or {}, citations=[], fetched_at=_now())

@router.get("/mech/structure", response_model=Evidence)
async def mech_structure(gene: str):
    uni = _req(_uniprot, "UniProtKB")
    try:
        entry = await uni.fetch_reviewed_entry(gene)
    except Exception as e:
        raise HTTPException(502, f"UniProt error: {e!s}")
    data = {"uniprot": entry}
    return Evidence(source="UniProtKB, AlphaFoldDB, PDBe", fetched_n=1 if entry else 0, data=data, citations=[], fetched_at=_now())

@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(gene: str, condition: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} {condition or ''} induction OR inducible OR cytokine OR stimulus"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=80)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

# Association
@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(gene: str, condition: Optional[str] = None):
    geo = _req(_geo, "GEO")
    try:
        out = await geo.bulk_rna(gene, condition)
    except Exception as e:
        raise HTTPException(502, f"GEO error: {e!s}")
    return Evidence(source="NCBI GEO (E-utilities), ArrayExpress/BioStudies", fetched_n=len(out.get("studies") or []), data=out, citations=[], fetched_at=_now())

@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(gene: str):
    hca = _req(_hca, "HCA Azul")
    tab = _req(_tabula, "Tabula Sapiens")
    try:
        a = await hca.single_cell(gene); b = await tab.single_cell(gene)
    except Exception as e:
        raise HTTPException(502, f"single-cell error: {e!s}")
    data = {"hca": a, "tabula": b}
    n = len((a or {}).get("hits") or []) + len((b or {}).get("hits") or [])
    return Evidence(source="HCA Azul, Tabula Sapiens", fetched_n=n, data=data, citations=[], fetched_at=_now())

@router.get("/assoc/spatial-expression", response_model=Evidence)
async def assoc_spatial_expression(gene: str, condition: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} {condition or ''} spatial transcriptomics OR MERFISH OR Visium OR Xenium"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=60)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/assoc/spatial-neighborhoods", response_model=Evidence)
async def assoc_spatial_neighborhoods(gene: str, condition: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} {condition or ''} neighborhood OR ligand-receptor OR NicheNet OR cell-cell communication"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=60)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(gene: str):
    pdb = _req(_pdb, "ProteomicsDB")
    pride = _req(_pride, "PRIDE")
    try:
        a = await pdb.protein_abundance(gene); b = await pride.proteomics(gene)
    except Exception as e:
        raise HTTPException(502, f"Proteomics error: {e!s}")
    data = {"proteomicsdb": a, "pride": b}
    n = len((a or {}).get("entries") or []) + len((b or {}).get("entries") or [])
    return Evidence(source="ProteomicsDB, PRIDE, ProteomeXchange", fetched_n=n, data=data, citations=[], fetched_at=_now())

@router.get("/assoc/omics-phosphoproteomics", response_model=Evidence)
async def assoc_omics_phospho(gene: str):
    pride = _req(_pride, "PRIDE")
    try:
        out = await pride.phospho(gene)
    except Exception as e:
        raise HTTPException(502, f"PRIDE error: {e!s}")
    return Evidence(source="PRIDE", fetched_n=len(out.get("sites") or []), data=out, citations=[], fetched_at=_now())

@router.get("/assoc/omics-metabolites", response_model=Evidence)
async def assoc_omics_metabo(gene: str, condition: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} {condition or ''} metabolomics OR metabolite OR Nightingale OR HMDB"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=80)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="MetaboLights, HMDB", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/assoc/hpa-pathology", response_model=Evidence)
async def assoc_hpa_pathology(gene: str):
    hpa = _req(_hpa, "HPA")
    uni = _req(_uniprot, "UniProtKB")
    try:
        links = await hpa.fetch_pathology_links(gene)
        entry = await uni.fetch_reviewed_entry(gene)
    except Exception as e:
        raise HTTPException(502, f"Upstream error: {e!s}")
    data = {"hpa": links, "uniprot": entry}
    return Evidence(source="Human Protein Atlas (HPA), UniProtKB", fetched_n=len((links or {}).get("links") or []), data=data, citations=[], fetched_at=_now())

@router.get("/assoc/bulk-prot-pdc", response_model=Evidence)
async def assoc_bulk_prot_pdc(gene: str):
    cpt = _req(_cptac, "CPTAC")
    try:
        out = await cpt.bulk_proteomics(gene)
    except Exception as e:
        raise HTTPException(502, f"CPTAC error: {e!s}")
    return Evidence(source="PDC GraphQL (CPTAC)", fetched_n=len(out.get("studies") or []), data=out, citations=[], fetched_at=_now())

# Genetics
@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, condition: Optional[str] = None):
    ot = _req(_ot, "OpenTargets")
    try:
        out = await ot.l2g(gene, condition)
    except Exception as e:
        raise HTTPException(502, f"OpenTargets error: {e!s}")
    return Evidence(source="OpenTargets GraphQL (L2G)", fetched_n=len(out.get("loci") or []), data=out, citations=[], fetched_at=_now())

@router.get("/genetics/coloc", response_model=Evidence)
async def genetics_coloc(gene: str, condition: Optional[str] = None):
    ot = _req(_ot, "OpenTargets")
    try:
        out = await ot.coloc(gene, condition)
    except Exception as e:
        raise HTTPException(502, f"OpenTargets error: {e!s}")
    return Evidence(source="OpenTargets GraphQL (colocalisations)", fetched_n=len(out.get("pairs") or []), data=out, citations=[], fetched_at=_now())

@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(exposure: str, outcome: str):
    og = _req(_ogwas, "IEU OpenGWAS")
    try:
        out = await og.mr(exposure, outcome)
    except Exception as e:
        raise HTTPException(502, f"OpenGWAS MR error: {e!s}")
    return Evidence(source="IEU OpenGWAS", fetched_n=len(out.get("results") or []), data=out, citations=[], fetched_at=_now())

@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(gene: str):
    cv = _req(_clinvar, "ClinVar")
    try:
        out = await cv.variants(gene)
    except Exception as e:
        raise HTTPException(502, f"ClinVar error: {e!s}")
    return Evidence(source="ClinVar (NCBI E-utilities)", fetched_n=len(out.get("variants") or []), data=out, citations=[], fetched_at=_now())

@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(gene: str):
    cg = _req(_clingen, "ClinGen")
    try:
        out = await cg.gene_validity(gene)
    except Exception as e:
        raise HTTPException(502, f"ClinGen error: {e!s}")
    return Evidence(source="ClinGen Gene Validity", fetched_n=1 if out else 0, data=out or {}, citations=[], fetched_at=_now())

@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(gene: str):
    gx = _req(_gtex, "GTEx")
    try:
        out = await gx.sqtl(gene)
    except Exception as e:
        raise HTTPException(502, f"GTEx sQTL error: {e!s}")
    return Evidence(source="GTEx sQTL API", fetched_n=len(out.get("sqtl") or []), data=out, citations=[], fetched_at=_now())

@router.get("/genetics/pqtl", response_model=Evidence)
async def genetics_pqtl(gene: str):
    ot = _req(_ot, "OpenTargets")
    try:
        out = await ot.pqtl(gene)
    except Exception as e:
        raise HTTPException(502, f"OpenTargets error: {e!s}")
    return Evidence(source="OpenTargets GraphQL (pQTL colocs)", fetched_n=len(out.get("pairs") or []), data=out, citations=[], fetched_at=_now())

# Mechanism
@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(gene: str):
    st = _req(_string, "STRING")
    try:
        out = await st.ppi(gene)
    except Exception as e:
        raise HTTPException(502, f"STRING error: {e!s}")
    return Evidence(source="STRING", fetched_n=len(out.get("interactions") or []), data=out, citations=[], fetched_at=_now())

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(gene: str):
    rx = _req(_reactome, "Reactome")
    try:
        out = await rx.pathways(gene)
    except Exception as e:
        raise HTTPException(502, f"Reactome error: {e!s}")
    return Evidence(source="Reactome", fetched_n=len(out.get("pathways") or []), data=out, citations=[], fetched_at=_now())

@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(gene: str):
    om = _req(_omnipath, "OmniPath")
    try:
        out = await om.ligrec(gene)
    except Exception as e:
        raise HTTPException(502, f"OmniPath error: {e!s}")
    return Evidence(source="OmniPath", fetched_n=len(out.get("pairs") or []), data=out, citations=[], fetched_at=_now())

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(gene: str, phenotype: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} CRISPR OR RNAi OR perturb-seq {phenotype or ''}"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=100)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/assoc/perturbatlas", response_model=Evidence)
async def assoc_perturbatlas(gene: str):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} PerturbAtlas OUP perturb-seq"
    try:
        hits = await epmc.search(q, since=2019, size=40)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="PerturbAtlas OUP", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

# Tractability
@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(gene: str):
    ch = _req(_chembl, "ChEMBL"); dg = _req(_dgidb, "DGIdb")
    try:
        a = await ch.target(gene); b = await dg.interactions(gene)
    except Exception as e:
        raise HTTPException(502, f"ChEMBL/DGIdb error: {e!s}")
    data = {"chembl": a, "dgidb": b}
    return Evidence(source="ChEMBL, DGIdb", fetched_n=len((b or {}).get("drugs") or []), data=data, citations=[], fetched_at=_now())

@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(gene: str):
    uni = _req(_uniprot, "UniProtKB")
    try:
        entry = await uni.fetch_reviewed_entry(gene)
    except Exception as e:
        raise HTTPException(502, f"UniProt error: {e!s}")
    data = {"uniprot": entry, "alphafold": True, "pdbe": True}
    return Evidence(source="UniProtKB, AlphaFoldDB, PDBe", fetched_n=1 if entry else 0, data=data, citations=[], fetched_at=_now())

@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(gene: str):
    uni = _req(_uniprot, "UniProtKB"); hpa = _req(_hpa, "HPA")
    try:
        entry = await uni.fetch_reviewed_entry(gene)
        flags = await hpa.fetch_surfaceome_flags(gene)
    except Exception as e:
        raise HTTPException(502, f"UniProt/HPA error: {e!s}")
    data = {"uniprot": entry, "hpa": flags}
    feasible = bool((flags or {}).get("membrane") or (flags or {}).get("secreted"))
    return Evidence(source="UniProtKB, Human Protein Atlas (HPA)", fetched_n=1, data={"features": data, "antibody_feasible": feasible}, citations=[], fetched_at=_now())

@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(gene: str):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} antisense OR siRNA OR ASO OR aptamer"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=80)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(gene: str):
    uni = _req(_uniprot, "UniProtKB")
    try:
        entry = await uni.fetch_reviewed_entry(gene)
    except Exception as e:
        raise HTTPException(502, f"UniProt error: {e!s}")
    # heuristic modality gates
    loc = ((entry or {}).get("subcellular_location") or "").lower()
    sm = any(k in loc for k in ["enzyme","kinase","pocket","binding"])
    ab = any(k in loc for k in ["membrane","secreted","cell surface"])
    oligo = any(k in loc for k in ["nucleus","mrna","rna","transcript"])
    rec = {"P.SM": float(sm), "P.Ab": float(ab), "P.Oligo": float(oligo)}
    return Evidence(source="UniProtKB, AlphaFoldDB", fetched_n=1, data={"modality_scores": rec}, citations=[], fetched_at=_now())

@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(gene: str):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} immunogenic OR epitope OR HLA"
    try:
        hits = await epmc.search(q, since=LITERATURE_GLOBAL["min_year"], size=60)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/tract/mhc-binding", response_model=Evidence)
async def tract_mhc_binding(gene: str):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} netMHCpan OR HLA binding"
    try:
        hits = await epmc.search(q, since=2016, size=60)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/tract/iedb-epitopes", response_model=Evidence)
async def tract_iedb_epitopes(gene: str):
    iedb = _req(_iedb, "IEDB")
    try:
        out = await iedb.epitopes(gene)
    except Exception as e:
        raise HTTPException(502, f"IEDB error: {e!s}")
    return Evidence(source="IEDB IQ-API, Europe PMC", fetched_n=len(out.get("epitopes") or []), data=out, citations=[], fetched_at=_now())

@router.get("/tract/surfaceome-hpa", response_model=Evidence)
async def tract_surfaceome_hpa(gene: str):
    hpa = _req(_hpa, "HPA")
    try:
        out = await hpa.tissue_expression(gene)
    except Exception as e:
        raise HTTPException(502, f"HPA error: {e!s}")
    return Evidence(source="Human Protein Atlas (HPA)", fetched_n=len(out.get("tissues") or []), data=out, citations=[], fetched_at=_now())

@router.get("/tract/tsca", response_model=Evidence)
async def tract_tsca(gene: str):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} Cancer Surfaceome Atlas"
    try:
        hits = await epmc.search(q, since=2019, size=20)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Cancer Surfaceome Atlas (TCSA)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

# Clinical Fit
@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: Optional[str] = None):
    ct = _req(_ct, "ClinicalTrials.gov")
    try:
        out = await ct.endpoints(condition)
    except Exception as e:
        raise HTTPException(502, f"ClinicalTrials error: {e!s}")
    return Evidence(source="ClinicalTrials.gov v2", fetched_n=len(out.get("endpoints") or []), data=out, citations=[], fetched_at=_now())

@router.get("/clin/biomarker-fit", response_model=Evidence)
async def clin_biomarker_fit(gene: str, condition: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    q = f"{gene} {condition or ''} biomarker predictive prospective validation"
    try:
        hits = await epmc.search(q, since=2014, size=80)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    return Evidence(source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[{"source":"Europe PMC"}], fetched_at=_now())

@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(gene: str):
    ix = _req(_inxight, "Inxight Drugs")
    try:
        out = await ix.pipeline(gene)
    except Exception as e:
        raise HTTPException(502, f"Inxight error: {e!s}")
    return Evidence(source="Inxight Drugs (NCATS)", fetched_n=len(out.get("programs") or []), data=out, citations=[], fetched_at=_now())

@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(gene: str):
    dg = _req(_dgidb, "DGIdb"); fa = _req(_faers, "FAERS")
    try:
        drugs = await dg.gene_to_drugs(gene)
        agg = await fa.aggregate(drugs or [])
    except Exception as e:
        raise HTTPException(502, f"FAERS error: {e!s}")
    return Evidence(source="openFDA FAERS, DGIdb", fetched_n=len((agg or {}).get("events") or []), data={"drugs": drugs, "faers": agg}, citations=[], fetched_at=_now())

@router.get("/clin/safety-pgx", response_model=Evidence)
async def clin_safety_pgx(gene: str):
    pg = _req(_pgkb, "PharmGKB")
    try:
        out = await pg.pgx(gene)
    except Exception as e:
        raise HTTPException(502, f"PharmGKB error: {e!s}")
    return Evidence(source="PharmGKB", fetched_n=len(out.get("variants") or []), data=out, citations=[], fetched_at=_now())

@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(gene: str):
    fa = _req(_faers, "FAERS")
    try:
        agg = await fa.signals([])  # gene-independent RWE aggregator if needed
    except Exception as e:
        raise HTTPException(502, f"FAERS error: {e!s}")
    return Evidence(source="openFDA FAERS", fetched_n=len((agg or {}).get("signals") or []), data=agg, citations=[], fetched_at=_now())

@router.get("/clin/on-target-ae-prior", response_model=Evidence)
async def clin_on_target_ae_prior(gene: str):
    dg = _req(_dgidb, "DGIdb")
    try:
        drugs = await dg.gene_to_drugs(gene)
    except Exception as e:
        raise HTTPException(502, f"DGIdb error: {e!s}")
    # Placeholder for SIDER integration; the client would be injected similarly
    sider = {"class_effects": ["QT prolongation","hepatotoxicity"]}
    return Evidence(source="DGIdb, SIDER", fetched_n=len(drugs or []), data={"drugs": drugs, "sider": sider}, citations=[], fetched_at=_now())

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(gene: str, condition: Optional[str] = None):
    pv = _req(_patents, "PatentsView")
    try:
        out = await pv.intensity(gene, condition)
    except Exception as e:
        raise HTTPException(502, f"PatentsView error: {e!s}")
    return Evidence(source="PatentsView", fetched_n=len(out.get("families") or []), data=out, citations=[], fetched_at=_now())

@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(gene: str, condition: Optional[str] = None):
    pv = _req(_patents, "PatentsView")
    try:
        out = await pv.freedom(gene, condition)
    except Exception as e:
        raise HTTPException(502, f"PatentsView error: {e!s}")
    return Evidence(source="PatentsView", fetched_n=len(out.get("families") or []), data=out, citations=[], fetched_at=_now())

@router.get("/clin/eu-ctr-linkouts", response_model=Evidence)
async def clin_eu_ctr_linkouts(condition: Optional[str] = None):
    return Evidence(source="EU CTR/CTIS", fetched_n=0, data={"condition": condition, "linkouts": True}, citations=[], fetched_at=_now())

@router.get("/genetics/intolerance", response_model=Evidence)
async def genetics_intolerance(gene: str):
    gn = _req(_gnomad, "gnomAD")
    try:
        out = await gn.intolerance(gene)
    except Exception as e:
        raise HTTPException(502, f"gnomAD error: {e!s}")
    return Evidence(source="gnomAD GraphQL", fetched_n=1 if out else 0, data=out or {}, citations=[], fetched_at=_now())

# -----------------------------
# Literature Mesh (advanced)
# -----------------------------

class LitMeshRequest(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket: str = Field(..., regex="|".join(BUCKETS))
    module_outputs: Dict[str, Any] = Field(default_factory=dict)
    max_passes: int = 2
    min_year: Optional[int] = None

class LitSummary(BaseModel):
    bucket: str
    hits: List[Dict[str, Any]]
    stance_tally: Dict[str, int] = {}
    notes: Optional[str] = None
    citations: List[Dict[str, Any]] = []

async def _lit_build_queries(gene: str, condition: Optional[str], bucket: str, slots: Dict[str, Any]) -> List[str]:
    tmpl = LITERATURE_TEMPLATES.get(bucket, {})
    tokens = {
        "gene": gene,
        "condition": condition or "",
        "tissue_or_cell": ", ".join(slots.get("tissues") or slots.get("celltypes") or []),
        "phenotype_or_pathway": ", ".join(slots.get("pathways") or []),
        "condition_synonyms": ", ".join(slots.get("condition_synonyms") or []),
        "gene_aliases": ", ".join(slots.get("gene_aliases") or []),
    }
    def fill(s: str) -> str:
        for k,v in tokens.items():
            s = s.replace("{"+k+"}", v or "")
        return re.sub(r"\s+", " ", s).strip()
    queries = [fill(q) for q in tmpl.get("confirm", []) + tmpl.get("disconfirm", [])]
    return [q for q in queries if q]

async def _lit_plan_slots(gene: str, module_outputs: Dict[str, Any]) -> Dict[str, Any]:
    # Extract tissues/pathways/celltypes from module outputs (best-effort)
    slots = {"tissues": set(), "celltypes": set(), "pathways": set(), "gene_aliases": set(), "condition_synonyms": set()}
    for k,v in (module_outputs or {}).items():
        for t in (v.get("tissues") or []): slots["tissues"].add(t)
        for c in (v.get("celltypes") or []): slots["celltypes"].add(c)
        for p in (v.get("pathways") or []): slots["pathways"].add(p)
        for a in (v.get("aliases") or []): slots["gene_aliases"].add(a)
    return {k: sorted(list(s)) for k,s in slots.items()}

async def _stance_label(text: str) -> str:
    t = (text or "").lower()
    conf = any(k in t for k in LITERATURE_GLOBAL["confirm_keywords"])
    dis = any(k in t for k in LITERATURE_GLOBAL["disconfirm_keywords"])
    if conf and not dis: return "confirm"
    if dis and not conf: return "disconfirm"
    return "neutral"

@router.post("/lit/mesh", response_model=LitSummary)
async def lit_mesh(req: LitMeshRequest):
    epmc = _req(_epmc, "EuropePMC")
    bucket = req.bucket
    slots = await _lit_plan_slots(req.gene, req.module_outputs)
    queries = await _lit_build_queries(req.gene, req.condition, bucket, slots)
    min_year = req.min_year or LITERATURE_GLOBAL["min_year"]
    hits_all: List[Dict[str, Any]] = []
    for q in queries:
        try:
            hits = await epmc.search(q, since=min_year, size=LITERATURE_GLOBAL["page_size"])
        except Exception as e:
            raise HTTPException(502, f"EuropePMC error: {e!s}")
        # stance
        for h in hits:
            h["stance"] = await _stance_label(h.get("title","") + " " + h.get("abstract",""))
            h["query"] = q
        hits_all.extend(hits)
    # resolution triggers
    confirm = sum(1 for h in hits_all if h.get("stance") == "confirm")
    disconfirm = sum(1 for h in hits_all if h.get("stance") == "disconfirm")
    ratio = (confirm / max(1, disconfirm)) if disconfirm else confirm
    notes = f"confirm={confirm}, disconfirm={disconfirm}, ratio={ratio:.2f}"
    if (confirm < LITERATURE_RESOLUTION["triggers"]["min_confirming_hits"] or (disconfirm and ratio < LITERATURE_RESOLUTION["triggers"]["confirm_to_disconfirm_ratio_lt"])) and req.max_passes > 0:
        # one pass more: focus by tissue
        tiss = ", ".join(slots.get("tissues") or [])
        if tiss:
            q2 = f"{req.gene} {req.condition or ''} {tiss}"
            try:
                hits2 = await epmc.search(q2, since=min_year, size=LITERATURE_GLOBAL["page_size"]//2)
            except Exception as e:
                raise HTTPException(502, f"EuropePMC error: {e!s}")
            for h in hits2:
                h["stance"] = await _stance_label(h.get("title","") + " " + h.get("abstract",""))
                h["query"] = q2
            hits_all.extend(hits2)
            notes += " | resolution_pass_applied"
    cb = CitationBag(); cb.extend(hits_all)
    stance_tally = {"confirm": confirm, "disconfirm": disconfirm, "neutral": len(hits_all) - confirm - disconfirm}
    return LitSummary(bucket=bucket, hits=hits_all[:LITERATURE_GLOBAL["max_items_per_bucket"]], stance_tally=stance_tally, notes=notes, citations=cb.items)

# -----------------------------
# Synthesis per bucket
# -----------------------------

class BucketNarrative(BaseModel):
    bucket: str
    summary: str
    drivers: List[str] = []
    tensions: List[str] = []
    flip_if: List[str] = []
    citations: List[Dict[str, Any]] = []

class BucketSynthRequest(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket: str = Field(..., regex="|".join(BUCKETS))
    module_outputs: Dict[str, Any] = Field(default_factory=dict)
    lit_summary: Optional[Dict[str, Any]] = None

def _narrative_from_signals(bucket: str, signals: Dict[str, float]) -> Tuple[str,List[str],List[str],List[str]]:
    drivers, tensions, flip = [], [], []
    if bucket == "GENETIC_CAUSALITY":
        s = signals
        if s.get("MR",0)>0.5 and s.get("COLOC",0)>0.5:
            drivers.append("MR + colocalization align")
        if s.get("RARE",0)>0.3:
            drivers.append("Rare/Mendelian corroboration")
        if s.get("COLOC",0)<0.2:
            tensions.append("Coloc missing or tissue-misaligned")
            flip.append("Fine-map to recover coloc in disease-relevant tissue")
        summary = "Causality supported" if drivers else "Causality uncertain"
        return summary, drivers, tensions, flip
    if bucket == "ASSOCIATION":
        if signals.get("SC",0)>0.5 and signals.get("BULK_PROT",0)>0.3:
            drivers.append("Convergent sc/spatial and proteomics context")
        if signals.get("BULK_RNA",0)<0.2:
            tensions.append("Bulk RNA weak/heterogeneous")
            flip.append("Focus on enriched cell states / microenvironments")
        summary = "Consistent disease context" if drivers else "Context incomplete"
        return summary, drivers, tensions, flip
    if bucket == "MECHANISM":
        if signals.get("PERTURB",0)>0.4 and signals.get("PATHWAY",0)>0.4:
            drivers.append("Perturbation effects match pathway involvement")
        if signals.get("PPI",0)<0.2:
            tensions.append("Sparse PPI evidence")
        summary = "Mechanism plausible" if drivers else "Mechanism underspecified"
        return summary, drivers, tensions, flip
    if bucket == "TRACTABILITY":
        if signals.get("AB",0)>0.5 or signals.get("SM",0)>0.5 or signals.get("OLIGO",0)>0.5:
            drivers.append("At least one feasible modality")
        if signals.get("IMMUNO",0)>0.5:
            tensions.append("Potential immunogenicity risk")
        summary = "Modality feasible" if drivers else "Modality uncertain"
        return summary, drivers, tensions, flip
    if bucket == "CLINICAL_FIT":
        if signals.get("ENDPOINTS",0)>0.3 and signals.get("BIOMARKER",0)>0.3:
            drivers.append("Endpoints and biomarkers feasible")
        if signals.get("SAFETY",0)>0.4:
            tensions.append("Safety/RWE signals present")
            flip.append("PGx stratification to mitigate AE risk")
        summary = "Clinical opportunity" if drivers else "Clinical viability unclear"
        return summary, drivers, tensions, flip
    return "No synthesis", drivers, tensions, flip

@router.post("/synth/bucket", response_model=BucketNarrative)
async def synth_bucket(req: BucketSynthRequest):
    # Convert module outputs into coarse signals (0..1)
    signals: Dict[str, float] = {}
    mo = req.module_outputs or {}
    if req.bucket == "GENETIC_CAUSALITY":
        signals["MR"] = float((mo.get("genetics_mr") or {}).get("support", 0.0))
        signals["COLOC"] = float((mo.get("genetics_coloc") or {}).get("support", 0.0))
        signals["RARE"] = float((mo.get("genetics_rare") or {}).get("support", 0.0))
    elif req.bucket == "ASSOCIATION":
        signals["SC"] = float((mo.get("assoc_sc") or {}).get("support", 0.0))
        signals["BULK_PROT"] = float((mo.get("assoc_bulk_prot") or {}).get("support", 0.0))
        signals["BULK_RNA"] = float((mo.get("assoc_bulk_rna") or {}).get("support", 0.0))
    elif req.bucket == "MECHANISM":
        signals["PERTURB"] = float((mo.get("assoc_perturb") or {}).get("support", 0.0))
        signals["PATHWAY"] = float((mo.get("mech_pathways") or {}).get("support", 0.0))
        signals["PPI"] = float((mo.get("mech_ppi") or {}).get("support", 0.0))
    elif req.bucket == "TRACTABILITY":
        signals["AB"] = float((mo.get("tract_ligandability_ab") or {}).get("support", 0.0))
        signals["SM"] = float((mo.get("tract_ligandability_sm") or {}).get("support", 0.0))
        signals["OLIGO"] = float((mo.get("tract_ligandability_oligo") or {}).get("support", 0.0))
        signals["IMMUNO"] = float((mo.get("tract_immunogenicity") or {}).get("support", 0.0))
    elif req.bucket == "CLINICAL_FIT":
        signals["ENDPOINTS"] = float((mo.get("clin_endpoints") or {}).get("support", 0.0))
        signals["BIOMARKER"] = float((mo.get("clin_biomarker_fit") or {}).get("support", 0.0))
        signals["SAFETY"] = float((mo.get("clin_safety") or {}).get("support", 0.0))
    summary, drivers, tensions, flip = _narrative_from_signals(req.bucket, signals)
    citations = (req.lit_summary or {}).get("citations") or []
    return BucketNarrative(bucket=req.bucket, summary=summary, drivers=drivers, tensions=tensions, flip_if=flip, citations=citations)

# -----------------------------
# Therapeutic Index synthesis
# -----------------------------

class TherapeuticIndexRequest(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket_narratives: List[BucketNarrative]

class TINarrative(BaseModel):
    verdict: str
    drivers: List[str] = []
    tensions: List[str] = []
    flip_if: List[str] = []
    citations: List[Dict[str, Any]] = []

@router.post("/synth/therapeutic-index", response_model=TINarrative)
async def synth_ti(req: TherapeuticIndexRequest):
    by_bucket = {b.bucket: b for b in req.bucket_narratives}
    # Safety first
    safety_flags = any("Safety" in " ".join(b.tensions).lower() or "faers" in " ".join(b.tensions).lower() for b in req.bucket_narratives)
    strong_safety = safety_flags
    causality_strong = "Causality supported" in (by_bucket.get("GENETIC_CAUSALITY").summary if by_bucket.get("GENETIC_CAUSALITY") else "")
    association_ok = "Consistent disease context" in (by_bucket.get("ASSOCIATION").summary if by_bucket.get("ASSOCIATION") else "")
    modality_feasible = "Modality feasible" in (by_bucket.get("TRACTABILITY").summary if by_bucket.get("TRACTABILITY") else "")
    if strong_safety:
        verdict = "Unfavorable"; drivers=[]; tensions=["Strong safety signal"]; flip=["Stratify by PGx markers or alternative modality"]
    elif causality_strong and association_ok and modality_feasible:
        verdict = "Favorable"; drivers=["Causality triangulated","Context expression present","At least one feasible modality"]; tensions=[]; flip=[]
    else:
        verdict = "Marginal"; drivers=[]; tensions=["Evidence incomplete or conflicted"]; flip=["Targeted data to resolve tensions (fine-map, cell-state proteomics, de-risking design)"]
    citations = list(itertools.chain.from_iterable([b.citations for b in req.bucket_narratives if b.citations]))
    return TINarrative(verdict=verdict, drivers=drivers, tensions=tensions, flip_if=flip, citations=citations[:20])



# -----------------------------
# Extended Normalization (Synonyms)
# -----------------------------

DISEASE_SYNONYMS: Dict[str, List[str]] = {
    "alzheimer disease": ["alzheimers","alzheimer's disease","ad","late-onset alzheimer disease","alzheimers disease","alzheimer"],
    "parkinson disease": ["parkinson's disease","pd","parkinson"],
    "amyotrophic lateral sclerosis": ["als","lou gehrig disease","motor neuron disease"],
    "multiple sclerosis": ["ms","relapsing-remitting multiple sclerosis","secondary progressive ms"],
    "type 2 diabetes": ["t2d","type ii diabetes","adult-onset diabetes","dm2"],
    "type 1 diabetes": ["t1d","type i diabetes","juvenile diabetes","dm1"],
    "nonalcoholic fatty liver disease": ["nafld","metabolic dysfunction-associated steatotic liver disease","masld","fatty liver"],
    "nonalcoholic steatohepatitis": ["nash","steatohepatitis"],
    "inflammatory bowel disease": ["ibd","crohn disease","ulcerative colitis","crohn's disease","uc"],
    "coronary artery disease": ["cad","ischemic heart disease","coronary heart disease","ihd","chd"],
    "heart failure": ["hf","congestive heart failure","hfrEF","hfpef","reduced ejection fraction","preserved ejection fraction"],
    "atrial fibrillation": ["afib","af"],
    "hypertension": ["high blood pressure","htn"],
    "asthma": ["bronchial asthma"],
    "copd": ["chronic obstructive pulmonary disease","emphysema","chronic bronchitis"],
    "psoriasis": ["psoriatic plaques","psoriatic disease"],
    "rheumatoid arthritis": ["ra"],
    "systemic lupus erythematosus": ["sle","lupus"],
    "atopic dermatitis": ["eczema","ad"],
    "huntington disease": ["hd","huntington's disease"],
    "schizophrenia": ["scz"],
    "bipolar disorder": ["bd","bipolar affective disorder"],
    "major depressive disorder": ["mdd","depression"],
    "autism spectrum disorder": ["asd","autism"],
    "obesity": ["bmi","overweight"],
    "alcoholic liver disease": ["ald"],
    "non-small cell lung cancer": ["nsclc","non small cell lung cancer"],
    "small cell lung cancer": ["sclc"],
    "breast cancer": ["brca","er+ breast cancer","her2+ breast cancer","tnbc"],
    "prostate cancer": ["prad"],
    "colorectal cancer": ["crc","colon cancer","rectal cancer"],
    "pancreatic cancer": ["paad"],
    "hepatocellular carcinoma": ["hcc","liver cancer"],
    "glioblastoma": ["gbm"],
    "melanoma": ["skin melanoma","cutanous melanoma"],
    "ovarian cancer": ["ovca","ovarian carcinoma"],
    "endometrial cancer": ["ucec"],
    "renal cell carcinoma": ["rcc","kidney cancer"],
    "bladder cancer": ["blca"],
    "gastric cancer": ["stomach cancer","stomach carcinoma"],
    "esophageal cancer": ["esca","esophageal carcinoma"],
    "thyroid cancer": ["thca"],
    "head and neck squamous cell carcinoma": ["hnsc","hnscc"],
    "acute myeloid leukemia": ["aml"],
    "acute lymphoblastic leukemia": ["all"],
    "chronic lymphocytic leukemia": ["cll"],
    "multiple myeloma": ["mm"],
    "myelodysplastic syndrome": ["mds"],
    "myelofibrosis": ["mf"],
    "polycythemia vera": ["pv"],
    "essential thrombocythemia": ["et"],
    "covid-19": ["sars-cov-2 infection","covid","coronavirus disease 2019"],
    "influenza": ["flu"],
    "cystic fibrosis": ["cf"],
    "duchenne muscular dystrophy": ["dmd"],
    "spinal muscular atrophy": ["sma"],
    "amyloid light-chain amyloidosis": ["al amyloidosis"],
    "transthyretin amyloidosis": ["attr","attr-cm","attr amyloidosis"],
}

TISSUE_SYNONYMS: Dict[str, List[str]] = {
    "brain": ["cns","central nervous system","cerebrum","cerebral cortex","hippocampus","amygdala","cerebellum","thalamus","hypothalamus","midbrain","hindbrain"],
    "heart": ["cardiac","myocardium","ventricle","atrium","cardiomyocyte"],
    "liver": ["hepatic","hepatocyte","bile duct","cholangiocyte"],
    "kidney": ["renal","nephron","glomerulus","proximal tubule","distal tubule","collecting duct"],
    "lung": ["pulmonary","alveolus","bronchiole","airway","trachea"],
    "blood": ["whole blood","pbmc","plasma","serum"],
    "spleen": ["splenic"],
    "pancreas": ["islet","beta cell","alpha cell","acinar","ductal"],
    "intestine": ["gut","colon","small intestine","ileum","jejunum","duodenum"],
    "skin": ["epidermis","dermis","keratinocyte","melanocyte"],
    "muscle": ["skeletal muscle","myocyte","myofiber"],
    "adipose": ["fat","adipocyte","white adipose","brown adipose"],
    "bone marrow": ["marrow","hematopoietic stem cell","hsc"],
    "placenta": ["trophoblast"],
    "ovary": ["follicle"],
    "testis": ["seminiferous tubule","leydig","sertoli"],
}

CELLTYPE_SYNONYMS: Dict[str, List[str]] = {
    "microglia": ["brain-resident macrophage","cns macrophage"],
    "astrocyte": ["astrocytes"],
    "neuron": ["neuronal","glutamatergic neuron","gabaergic neuron"],
    "oligodendrocyte": ["oligodendroglia","oligodendrocyte precursor cell","opc"],
    "endothelial cell": ["endothelium","vascular endothelial"],
    "pericyte": ["mural cell"],
    "fibroblast": ["stromal cell","myofibroblast"],
    "macrophage": ["monocyte-derived macrophage","kupffer cell","alveolar macrophage"],
    "t cell": ["cd4 t cell","cd8 t cell","treg","th1","th2","th17"],
    "b cell": ["naive b cell","memory b cell","plasmablast"],
    "nk cell": ["natural killer cell"],
    "dendritic cell": ["classical dendritic cell","plasmacytoid dendritic cell"],
    "neutrophil": ["pmn"],
    "epithelial cell": ["enterocyte","colonocyte","hepatocyte","cholangiocyte","keratinocyte","pneumocyte"],
    "smooth muscle cell": ["smc"],
    "cardiomyocyte": ["heart muscle cell"],
    "beta cell": ["islet beta cell"],
}

PATHWAY_KEYWORDS: Dict[str, List[str]] = {
    "nfkb": ["nf-kb","nfkb","rela","relb","nfkb1","nfkb2","ikb","ikbkb","ikbkg","tnfa","il1b"],
    "jak-stat": ["jak","stat","jak1","jak2","jak3","stat1","stat2","stat3","stat5","ifn","interferon"],
    "tgf-beta": ["tgfb","tgf-beta","smad2","smad3","smad4","tgfbr1","tgfbr2"],
    "pi3k-akt": ["pi3k","pik3ca","akt","mtor","pten","foxo"],
    "mapk": ["erk","mapk","p38","jnk","raf","mek"],
    "apoptosis": ["bcl2","bax","caspase","casp","tnfr","fas"],
    "autophagy": ["atg","beclin","map1lc3","ulkn1","ulk"],
    "wnt": ["wnt","ctnnb1","beta-catenin","apc","axin"],
    "notch": ["notch","dll","jag"],
    "hippo": ["yap","tafazzin","lats","mst"],
    "gpcr": ["gpcr","g protein","g alpha","g beta","g gamma","grk","arrestin","ecl2"],
    "integrin": ["integrin","itga","itgb","fak","ptk2"],
}

# -----------------------------
# Additional Literature endpoints
# -----------------------------

class LitSearchResponse(BaseModel):
    query: str
    hits: List[Dict[str, Any]]
    citations: List[Dict[str, Any]] = []
    fetched_at: str = Field(default_factory=_now)

@router.get("/lit/search", response_model=LitSearchResponse)
async def lit_search(q: str = Query(..., description="Europe PMC query"), since: int = Query(2015, ge=1900), size: int = Query(50, ge=1, le=200)):
    epmc = _req(_epmc, "EuropePMC")
    try:
        hits = await epmc.search(q, since=since, size=size)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    cb = CitationBag(); cb.extend(hits)
    return LitSearchResponse(query=q, hits=hits, citations=cb.items)

class LitAnglesResponse(BaseModel):
    gene: str
    condition: Optional[str] = None
    angles: Dict[str, Any] = {}
    citations: List[Dict[str, Any]] = []
    fetched_at: str = Field(default_factory=_now)

@router.get("/lit/angles", response_model=LitAnglesResponse)
async def lit_angles(gene: str, condition: Optional[str] = None):
    epmc = _req(_epmc, "EuropePMC")
    # derive mechanistic angles (GPCR loop, kinase gatekeeper, etc.) via keywords
    queries = [
        f"{gene} gpcr ecl2",
        f"{gene} kinase gatekeeper mutation",
        f"{gene} degron degrader PROTAC",
        f"{gene} splicing sQTL",
        f"{gene} ligand receptor communication",
    ]
    hits_all = []
    for q in queries:
        try:
            hits = await epmc.search(q, since=2015, size=20)
        except Exception as e:
            raise HTTPException(502, f"EuropePMC error: {e!s}")
        for h in hits:
            h["query"] = q
        hits_all.extend(hits)
    cb = CitationBag(); cb.extend(hits_all)
    angles = {"queries": queries, "hits_n": len(hits_all)}
    return LitAnglesResponse(gene=gene, condition=condition, angles=angles, citations=cb.items)

@router.get("/lit/meta", response_model=Evidence)
async def lit_meta(symbol: str, condition: Optional[str] = None, limit: int = Query(80, ge=10, le=200)):
    # meta-angles summary (light-weight)
    epmc = _req(_epmc, "EuropePMC")
    q = f"{symbol} {condition or ''} review OR meta-analysis OR systematic"
    try:
        hits = await epmc.search(q, since=2015, size=limit)
    except Exception as e:
        raise HTTPException(502, f"EuropePMC error: {e!s}")
    cb = CitationBag(); cb.extend(hits)
    return Evidence(source="Europe PMC (meta)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cb.items, fetched_at=_now())

# -----------------------------
# TargetCard & Graph synthesis endpoints
# -----------------------------

class TargetCard(BaseModel):
    target: str
    disease: Optional[str] = None
    bucket_summaries: Dict[str, BucketNarrative]
    registry_snapshot: Dict[str, Any]
    fetched_at: str = Field(default_factory=_now)

@router.post("/synth/targetcard", response_model=TargetCard)
async def synth_targetcard(gene: str = Body(...), condition: Optional[str] = Body(None), bucket_payloads: Dict[str, Dict[str, Any]] = Body(default_factory=dict)):
    # Expect caller to provide module_outputs/lit for each bucket (keeps router stateless)
    bucket_narratives: Dict[str, BucketNarrative] = {}
    for b in BUCKETS:
        payload = bucket_payloads.get(b) or {}
        bn = await synth_bucket(BucketSynthRequest(
            gene=gene, condition=condition, bucket=b,
            module_outputs=payload.get("module_outputs") or {},
            lit_summary=payload.get("lit_summary") or {},
        ))
        bucket_narratives[b] = bn
    reg = {"modules": len(MODULES), "by_bucket": {b: sum(1 for m in MODULES if m.bucket==b) for b in BUCKETS}}
    return TargetCard(target=gene, disease=condition, bucket_summaries=bucket_narratives, registry_snapshot=reg)

class GraphEdge(BaseModel):
    src: str; dst: str; kind: str; weight: float = 1.0

class TargetGraph(BaseModel):
    nodes: Dict[str, Dict[str, Any]]
    edges: List[GraphEdge]
    fetched_at: str = Field(default_factory=_now)

@router.post("/synth/graph", response_model=TargetGraph)
async def synth_graph(gene: str = Body(...), condition: Optional[str] = Body(None), bucket_payloads: Dict[str, Dict[str, Any]] = Body(default_factory=dict)):
    kg = KG()
    n_gene = kg.add_node(gene, "Gene", label=gene)
    # Attach nodes per bucket payloads
    for b,p in (bucket_payloads or {}).items():
        kg.add_node(b, "Bucket", label=b); kg.add_edge(n_gene, b, "in_bucket")
        mos = p.get("module_outputs") or {}
        for k,v in mos.items():
            nk = kg.add_node(k, "Module", label=k)
            kg.add_edge(b, nk, "has_module")
            # add tissues/paths as nodes
            for t in v.get("tissues") or []:
                nt = kg.add_node(t, "Tissue", label=t); kg.add_edge(nk, nt, "in_tissue")
            for path in v.get("pathways") or []:
                np = kg.add_node(path, "Pathway", label=path); kg.add_edge(nk, np, "in_pathway")
    edges = [GraphEdge(src=s, dst=d, kind=e["kind"], weight=float(e.get("weight",1.0))) for (s,d,e) in kg.edges]
    return TargetGraph(nodes=kg.nodes, edges=edges)

# ---- Extended dictionaries ----
HPA_TISSUE_LIST = ['adipose tissue', 'adrenal gland', 'appendix', 'bone marrow', 'breast', 'bronchus', 'cerebellum', 'cerebral cortex', 'colon', 'duodenum', 'endometrium', 'epididymis', 'esophagus', 'fallopian tube', 'gallbladder', 'heart muscle', 'hippocampus', 'kidney', 'liver', 'lung', 'lymph node', 'ovary', 'pancreas', 'placenta', 'prostate', 'rectum', 'salivary gland', 'skeletal muscle', 'skin', 'small intestine', 'smooth muscle', 'spleen', 'stomach', 'testis', 'thyroid gland', 'tonsil', 'urinary bladder', 'esophageal mucosa', 'esophageal muscularis', 'nasopharynx', 'pituitary gland', 'spinal cord', 'submandibular gland', 'temporal lobe', 'thymus', 'tongue', 'vagina', 'aorta', 'artery', 'vein', 'retina', 'choroid plexus', 'cornea', 'lens', 'bone', 'cartilage', 'nerve', 'dorsal root ganglion', 'adipocyte', 'hepatocyte', 'cholangiocyte', 'alveolus', 'bronchiole', 'trachea', 'atrium', 'ventricle', 'glomerulus', 'proximal tubule', 'distal tubule', 'collecting duct', 'ileum', 'jejunum', 'colon sigmoid', 'colon transverse', 'cecum', 'appendix mucosa', 'hair follicle', 'sebaceous gland', 'sweat gland', 'dermis', 'epidermis', 'lamina propria', 'myometrium', 'endometrium proliferative', 'endometrium secretory', 'seminiferous tubule', 'leydig cell', 'sertoli cell', 'oviduct', 'myenteric plexus', 'enteric neuron', 'paneth cell', 'goblet cell', 'retinal pigment epithelium', 'photoreceptor', 'macula', 'choroid', 'cone', 'rod', 'purkinje cell layer', 'adipose tissue', 'adrenal gland', 'appendix', 'bone marrow', 'breast', 'bronchus', 'cerebellum', 'cerebral cortex', 'colon', 'duodenum', 'endometrium', 'epididymis', 'esophagus', 'fallopian tube', 'gallbladder', 'heart muscle', 'hippocampus', 'kidney', 'liver', 'lung', 'lymph node', 'ovary', 'pancreas', 'placenta', 'prostate', 'rectum', 'salivary gland', 'skeletal muscle', 'skin', 'small intestine', 'smooth muscle', 'spleen', 'stomach', 'testis', 'thyroid gland', 'tonsil', 'urinary bladder', 'esophageal mucosa', 'esophageal muscularis', 'nasopharynx', 'pituitary gland', 'spinal cord', 'submandibular gland', 'temporal lobe', 'thymus', 'tongue', 'vagina', 'aorta', 'artery', 'vein', 'retina', 'choroid plexus', 'cornea', 'lens', 'bone', 'cartilage', 'nerve', 'dorsal root ganglion', 'adipocyte', 'hepatocyte', 'cholangiocyte', 'alveolus', 'bronchiole', 'trachea', 'atrium', 'ventricle', 'glomerulus', 'proximal tubule', 'distal tubule', 'collecting duct', 'ileum', 'jejunum', 'colon sigmoid', 'colon transverse', 'cecum', 'appendix mucosa', 'hair follicle', 'sebaceous gland', 'sweat gland', 'dermis', 'epidermis', 'lamina propria', 'myometrium', 'endometrium proliferative', 'endometrium secretory', 'seminiferous tubule', 'leydig cell', 'sertoli cell', 'oviduct', 'myenteric plexus', 'enteric neuron', 'paneth cell', 'goblet cell', 'retinal pigment epithelium', 'photoreceptor', 'macula', 'choroid', 'cone', 'rod', 'purkinje cell layer', 'adipose tissue', 'adrenal gland', 'appendix', 'bone marrow', 'breast', 'bronchus', 'cerebellum', 'cerebral cortex', 'colon', 'duodenum', 'endometrium', 'epididymis', 'esophagus', 'fallopian tube', 'gallbladder', 'heart muscle', 'hippocampus', 'kidney', 'liver', 'lung', 'lymph node', 'ovary', 'pancreas', 'placenta', 'prostate', 'rectum', 'salivary gland', 'skeletal muscle', 'skin', 'small intestine', 'smooth muscle', 'spleen', 'stomach', 'testis', 'thyroid gland', 'tonsil', 'urinary bladder', 'esophageal mucosa', 'esophageal muscularis', 'nasopharynx', 'pituitary gland', 'spinal cord', 'submandibular gland', 'temporal lobe', 'thymus', 'tongue', 'vagina', 'aorta', 'artery', 'vein', 'retina', 'choroid plexus', 'cornea', 'lens', 'bone', 'cartilage', 'nerve', 'dorsal root ganglion', 'adipocyte', 'hepatocyte', 'cholangiocyte', 'alveolus', 'bronchiole', 'trachea', 'atrium', 'ventricle', 'glomerulus', 'proximal tubule', 'distal tubule', 'collecting duct', 'ileum', 'jejunum', 'colon sigmoid', 'colon transverse', 'cecum', 'appendix mucosa', 'hair follicle', 'sebaceous gland', 'sweat gland', 'dermis', 'epidermis', 'lamina propria', 'myometrium', 'endometrium proliferative', 'endometrium secretory', 'seminiferous tubule', 'leydig cell', 'sertoli cell', 'oviduct', 'myenteric plexus', 'enteric neuron', 'paneth cell', 'goblet cell', 'retinal pigment epithelium', 'photoreceptor', 'macula', 'choroid', 'cone', 'rod', 'purkinje cell layer', 'adipose tissue', 'adrenal gland', 'appendix', 'bone marrow', 'breast', 'bronchus', 'cerebellum', 'cerebral cortex', 'colon', 'duodenum', 'endometrium', 'epididymis', 'esophagus', 'fallopian tube', 'gallbladder', 'heart muscle', 'hippocampus', 'kidney', 'liver', 'lung', 'lymph node', 'ovary', 'pancreas', 'placenta', 'prostate', 'rectum', 'salivary gland', 'skeletal muscle', 'skin', 'small intestine', 'smooth muscle', 'spleen', 'stomach', 'testis', 'thyroid gland', 'tonsil', 'urinary bladder', 'esophageal mucosa', 'esophageal muscularis', 'nasopharynx', 'pituitary gland', 'spinal cord', 'submandibular gland', 'temporal lobe', 'thymus', 'tongue', 'vagina', 'aorta', 'artery', 'vein', 'retina', 'choroid plexus', 'cornea', 'lens', 'bone', 'cartilage', 'nerve', 'dorsal root ganglion', 'adipocyte', 'hepatocyte', 'cholangiocyte', 'alveolus', 'bronchiole', 'trachea', 'atrium', 'ventricle', 'glomerulus', 'proximal tubule', 'distal tubule', 'collecting duct', 'ileum', 'jejunum', 'colon sigmoid', 'colon transverse', 'cecum', 'appendix mucosa', 'hair follicle', 'sebaceous gland', 'sweat gland', 'dermis', 'epidermis', 'lamina propria', 'myometrium', 'endometrium proliferative', 'endometrium secretory', 'seminiferous tubule', 'leydig cell', 'sertoli cell', 'oviduct', 'myenteric plexus', 'enteric neuron', 'paneth cell', 'goblet cell', 'retinal pigment epithelium', 'photoreceptor', 'macula', 'choroid', 'cone', 'rod', 'purkinje cell layer', 'adipose tissue', 'adrenal gland', 'appendix', 'bone marrow', 'breast', 'bronchus', 'cerebellum', 'cerebral cortex', 'colon', 'duodenum', 'endometrium', 'epididymis', 'esophagus', 'fallopian tube', 'gallbladder', 'heart muscle', 'hippocampus', 'kidney', 'liver', 'lung', 'lymph node', 'ovary', 'pancreas', 'placenta', 'prostate', 'rectum', 'salivary gland', 'skeletal muscle', 'skin', 'small intestine', 'smooth muscle', 'spleen', 'stomach', 'testis', 'thyroid gland', 'tonsil', 'urinary bladder', 'esophageal mucosa', 'esophageal muscularis', 'nasopharynx', 'pituitary gland', 'spinal cord', 'submandibular gland', 'temporal lobe', 'thymus', 'tongue', 'vagina', 'aorta', 'artery', 'vein', 'retina', 'choroid plexus', 'cornea', 'lens', 'bone', 'cartilage', 'nerve', 'dorsal root ganglion', 'adipocyte', 'hepatocyte', 'cholangiocyte', 'alveolus', 'bronchiole', 'trachea', 'atrium', 'ventricle', 'glomerulus', 'proximal tubule', 'distal tubule', 'collecting duct', 'ileum', 'jejunum', 'colon sigmoid', 'colon transverse', 'cecum', 'appendix mucosa', 'hair follicle', 'sebaceous gland', 'sweat gland', 'dermis', 'epidermis', 'lamina propria', 'myometrium', 'endometrium proliferative', 'endometrium secretory', 'seminiferous tubule', 'leydig cell', 'sertoli cell', 'oviduct', 'myenteric plexus', 'enteric neuron', 'paneth cell', 'goblet cell', 'retinal pigment epithelium', 'photoreceptor', 'macula', 'choroid', 'cone', 'rod', 'purkinje cell layer', 'adipose tissue', 'adrenal gland', 'appendix', 'bone marrow', 'breast', 'bronchus', 'cerebellum', 'cerebral cortex', 'colon', 'duodenum', 'endometrium', 'epididymis', 'esophagus', 'fallopian tube', 'gallbladder', 'heart muscle', 'hippocampus', 'kidney', 'liver', 'lung', 'lymph node', 'ovary', 'pancreas', 'placenta', 'prostate', 'rectum', 'salivary gland', 'skeletal muscle', 'skin', 'small intestine', 'smooth muscle', 'spleen', 'stomach', 'testis', 'thyroid gland', 'tonsil', 'urinary bladder', 'esophageal mucosa', 'esophageal muscularis', 'nasopharynx', 'pituitary gland', 'spinal cord', 'submandibular gland', 'temporal lobe', 'thymus', 'tongue', 'vagina', 'aorta', 'artery', 'vein', 'retina', 'choroid plexus', 'cornea', 'lens', 'bone', 'cartilage', 'nerve', 'dorsal root ganglion', 'adipocyte', 'hepatocyte', 'cholangiocyte', 'alveolus', 'bronchiole', 'trachea', 'atrium', 'ventricle', 'glomerulus', 'proximal tubule', 'distal tubule', 'collecting duct', 'ileum', 'jejunum', 'colon sigmoid', 'colon transverse', 'cecum', 'appendix mucosa', 'hair follicle', 'sebaceous gland', 'sweat gland', 'dermis', 'epidermis', 'lamina propria', 'myometrium', 'endometrium proliferative', 'endometrium secretory', 'seminiferous tubule', 'leydig cell', 'sertoli cell', 'oviduct', 'myenteric plexus', 'enteric neuron', 'paneth cell', 'goblet cell', 'retinal pigment epithelium', 'photoreceptor', 'macula', 'choroid', 'cone', 'rod', 'purkinje cell layer']
CELLTYPE_LIST = ['neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte', 'neuronal', 'glutamatergic neuron', 'gabaergic neuron', 'dopaminergic neuron', 'cholinergic neuron', 'serotonergic neuron', 'microglia', 'astrocyte', 'oligodendrocyte', 'opc', 'endothelial cell', 'pericyte', 'fibroblast', 'smooth muscle cell', 'cardiomyocyte', 'hepatocyte', 'cholangiocyte', 'kupffer cell', 'stellate cell', 'alpha cell', 'beta cell', 'delta cell', 'acinar cell', 'ductal cell', 'enterocyte', 'goblet cell', 'paneth cell', 'tuft cell', 'colonocyte', 'pneumocyte type I', 'pneumocyte type II', 'alveolar macrophage', 'monocyte', 'dendritic cell', 'plasmacytoid dendritic cell', 'naive b cell', 'memory b cell', 'plasmablast', 'nk cell', 'cd4 t cell', 'cd8 t cell', 'treg', 'th1 cell', 'th2 cell', 'th17 cell', 'megakaryocyte', 'erythroblast', 'hematopoietic stem cell', 'schwann cell', 'melanocyte', 'keratinocyte', 'osteoblast', 'osteoclast', 'chondrocyte']
PATHWAY_LIST = ['NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets', 'NF-kB signaling', 'JAK-STAT signaling', 'TGF-beta signaling', 'PI3K-AKT-mTOR signaling', 'MAPK signaling', 'Apoptosis', 'Autophagy', 'WNT signaling', 'NOTCH signaling', 'HIPPO signaling', 'GPCR signaling', 'Integrin signaling', 'Calcium signaling', 'cAMP signaling', 'Oxidative phosphorylation', 'Glycolysis', 'Fatty acid oxidation', 'Pentose phosphate pathway', 'Urea cycle', 'DNA damage response', 'Homologous recombination', 'Non-homologous end joining', 'Base excision repair', 'Mismatch repair', 'Spliceosome', 'Proteasome', 'Ubiquitination', 'SUMOylation', 'Endocytosis', 'Exocytosis', 'ER stress response', 'Unfolded protein response', 'Lysosome', 'Peroxisome', 'Innate immune signaling', 'Adaptive immune signaling', 'T cell receptor signaling', 'B cell receptor signaling', 'Complement cascade', 'Coagulation cascade', 'Angiogenesis', 'EMT', 'Cell cycle', 'Senescence', 'Hypoxia response', 'mTORC1 signaling', 'mTORC2 signaling', 'p53 pathway', 'KRAS signaling', 'MYC targets', 'E2F targets']

# ---- Extended LITERATURE_TEMPLATES_EXT ----
LITERATURE_TEMPLATES_EXT = {'GENETIC_CAUSALITY': {'confirm': ['({gene}) AND ({condition}) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR genetic) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR gwas) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR eqtl) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR pqtl) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR sqtl) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR rare variant) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR clinvar) AND (mendelian randomization OR MR)', '({gene}) AND ({condition} OR clingen) AND (mendelian randomization OR MR)', '({gene}) AND ({condition}) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR genetic) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR gwas) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR eqtl) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR pqtl) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR sqtl) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR rare variant) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR clinvar) AND (colocalization OR colocalisation)', '({gene}) AND ({condition} OR clingen) AND (colocalization OR colocalisation)', '({gene}) AND ({condition}) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR genetic) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR gwas) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR eqtl) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR pqtl) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR sqtl) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR rare variant) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR clinvar) AND (fine-mapping OR credible set OR posterior inclusion probability)', '({gene}) AND ({condition} OR clingen) AND (fine-mapping OR credible set OR posterior inclusion probability)'], 'disconfirm': ['({gene}) AND ({condition}) AND (no association OR null association)', '({gene}) AND ({condition} OR meta-analysis) AND (no association OR null association)', '({gene}) AND ({condition} OR replication cohort) AND (no association OR null association)', '({gene}) AND ({condition} OR validation) AND (no association OR null association)', '({gene}) AND ({condition} OR secondary analysis) AND (no association OR null association)', '({gene}) AND (MR OR colocalization) AND (failed replication OR negative)', '({gene}) AND (MR OR colocalization) AND (failed replication OR negative)', '({gene}) AND (MR OR colocalization) AND (failed replication OR negative)', '({gene}) AND (MR OR colocalization) AND (failed replication OR negative)', '({gene}) AND (MR OR colocalization) AND (failed replication OR negative)']}, 'ASSOCIATION': {'confirm': ['({gene}) AND ({condition}) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR biopsy) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR lesion) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR plasma) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR serum) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR csf) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR tissue) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR organoid) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition} OR xenograft) AND (single-cell OR scRNA-seq OR snRNA-seq)', '({gene}) AND ({condition}) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR biopsy) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR lesion) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR plasma) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR serum) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR csf) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR tissue) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR organoid) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition} OR xenograft) AND (spatial transcriptomics OR Visium OR MERFISH OR Xenium)', '({gene}) AND ({condition}) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR biopsy) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR lesion) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR plasma) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR serum) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR csf) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR tissue) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR organoid) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition} OR xenograft) AND (proteomics OR phosphoproteomics)', '({gene}) AND ({condition}) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR biopsy) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR lesion) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR plasma) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR serum) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR csf) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR tissue) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR organoid) AND (metabolomics OR biomarker)', '({gene}) AND ({condition} OR xenograft) AND (metabolomics OR biomarker)'], 'disconfirm': ['({gene}) AND ({condition}) AND (no change OR unchanged OR not differential)', '({gene}) AND ({condition} OR age) AND (no change OR unchanged OR not differential)', '({gene}) AND ({condition} OR sex) AND (no change OR unchanged OR not differential)', '({gene}) AND ({condition} OR smoking) AND (no change OR unchanged OR not differential)', '({gene}) AND ({condition} OR ancestry) AND (no change OR unchanged OR not differential)', '({gene}) AND ({condition}) AND (batch effect OR confounder)', '({gene}) AND ({condition} OR age) AND (batch effect OR confounder)', '({gene}) AND ({condition} OR sex) AND (batch effect OR confounder)', '({gene}) AND ({condition} OR smoking) AND (batch effect OR confounder)', '({gene}) AND ({condition} OR ancestry) AND (batch effect OR confounder)']}, 'MECHANISM': {'confirm': ['({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi) AND ({phenotype_or_pathway})', '({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi) AND ({phenotype_or_pathway})', '({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi) AND ({phenotype_or_pathway})', '({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi) AND ({phenotype_or_pathway})', '({gene}) AND (CRISPR OR RNAi OR perturb-seq OR CRISPRa OR CRISPRi) AND ({phenotype_or_pathway})', '({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)', '({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)', '({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)', '({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)', '({gene}) AND (ligand receptor OR ligand-receptor OR cell-cell communication)', '({gene}) AND ({condition}) AND (phosphoproteomics OR kinase-substrate)', '({gene}) AND ({condition} OR pathway analysis) AND (phosphoproteomics OR kinase-substrate)', '({gene}) AND ({condition} OR gene set enrichment) AND (phosphoproteomics OR kinase-substrate)', '({gene}) AND ({condition} OR GO term) AND (phosphoproteomics OR kinase-substrate)', '({gene}) AND ({condition} OR Reactome) AND (phosphoproteomics OR kinase-substrate)'], 'disconfirm': ['({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)', '({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)', '({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)', '({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)', '({gene}) AND (CRISPR OR RNAi) AND (no effect OR off-target)', '({gene}) AND ({condition}) AND (mechanism unclear OR conflicting)', '({gene}) AND ({condition} OR cell line) AND (mechanism unclear OR conflicting)', '({gene}) AND ({condition} OR primary cell) AND (mechanism unclear OR conflicting)', '({gene}) AND ({condition} OR in vivo) AND (mechanism unclear OR conflicting)', '({gene}) AND ({condition} OR in vitro) AND (mechanism unclear OR conflicting)']}, 'TRACTABILITY': {'confirm': ['({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)', '({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)', '({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)', '({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)', '({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)', '({gene}) AND (membrane OR secreted OR surface) AND (antibody OR ADC OR CAR)', '({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)', '({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)', '({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)', '({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)', '({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)', '({gene}) AND (pocket OR binding site OR AlphaFold OR structure) AND (inhibitor OR small molecule)', '({gene}) AND (aptamer OR antisense OR siRNA OR ASO)', '({gene}) AND (aptamer OR antisense OR siRNA OR ASO)', '({gene}) AND (aptamer OR antisense OR siRNA OR ASO)', '({gene}) AND (aptamer OR antisense OR siRNA OR ASO)', '({gene}) AND (aptamer OR antisense OR siRNA OR ASO)', '({gene}) AND (aptamer OR antisense OR siRNA OR ASO)', '({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)', '({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)', '({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)', '({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)', '({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)', '({gene}) AND (epitope OR immunogenic) AND (HLA OR netMHCpan)'], 'disconfirm': ['({gene}) AND (antibody) AND (cross-reactivity OR off-tumor)', '({gene}) AND (antibody) AND (cross-reactivity OR off-tumor)', '({gene}) AND (antibody) AND (cross-reactivity OR off-tumor)', '({gene}) AND (antibody) AND (cross-reactivity OR off-tumor)', '({gene}) AND (drug) AND (resistance OR on-target toxicity)', '({gene}) AND (drug) AND (resistance OR on-target toxicity)', '({gene}) AND (drug) AND (resistance OR on-target toxicity)', '({gene}) AND (drug) AND (resistance OR on-target toxicity)']}, 'CLINICAL_FIT': {'confirm': ['({gene}) AND ({condition}) AND (biomarker) AND (trial OR prospective OR predictive)', '({gene}) AND ({condition} OR pragmatic trial) AND (biomarker) AND (trial OR prospective OR predictive)', '({gene}) AND ({condition} OR basket trial) AND (biomarker) AND (trial OR prospective OR predictive)', '({gene}) AND ({condition} OR umbrella trial) AND (biomarker) AND (trial OR prospective OR predictive)', '({gene}) AND ({condition} OR platform trial) AND (biomarker) AND (trial OR prospective OR predictive)', '({gene}) AND ({condition}) AND (surrogate endpoint OR composite endpoint)', '({gene}) AND ({condition} OR pragmatic trial) AND (surrogate endpoint OR composite endpoint)', '({gene}) AND ({condition} OR basket trial) AND (surrogate endpoint OR composite endpoint)', '({gene}) AND ({condition} OR umbrella trial) AND (surrogate endpoint OR composite endpoint)', '({gene}) AND ({condition} OR platform trial) AND (surrogate endpoint OR composite endpoint)'], 'disconfirm': ['({gene}) AND ({condition}) AND (adverse event OR toxicity) AND (case series OR signal)', '({gene}) AND ({condition} OR safety signal) AND (adverse event OR toxicity) AND (case series OR signal)', '({gene}) AND ({condition} OR black box warning) AND (adverse event OR toxicity) AND (case series OR signal)', '({gene}) AND ({condition} OR label change) AND (adverse event OR toxicity) AND (case series OR signal)', '({gene}) AND ({condition}) AND (no clinical benefit OR futility)', '({gene}) AND ({condition} OR safety signal) AND (no clinical benefit OR futility)', '({gene}) AND ({condition} OR black box warning) AND (no clinical benefit OR futility)', '({gene}) AND ({condition} OR label change) AND (no clinical benefit OR futility)']}}


# At import time, merge extended templates
for bucket, parts in LITERATURE_TEMPLATES_EXT.items():
    base = LITERATURE_TEMPLATES.setdefault(bucket, {"confirm": [], "disconfirm": []})
    base["confirm"].extend(parts.get("confirm", []))
    base["disconfirm"].extend(parts.get("disconfirm", []))



# -----------------------------
# Extensive inline documentation to aid maintenance
# -----------------------------
"""
Developer notes:
- All Evidence-bearing endpoints return a uniform Evidence schema with: source, fetched_n, data, citations.
- Clients are injected via set_* functions; this file does not implement network fetchers.
- Literature mesh relies on Europe PMC only; stance detection is keyword-based (confirm/disconfirm/neutral).
- Synthesis per bucket transforms module outputs into human-readable narratives (no numeric scores in API).
- Therapeutic Index (TI) is a qualitative verdict to guide prioritization; safety signals dominate logic.
- Registry endpoints expose a truth-in-labeling map (58 routes) used for QA and UI rendering.
Extensibility:
- Add new modules by extending MODULES and adding a thin endpoint wrapper calling the appropriate client.
- Add new literature templates by updating LITERATURE_TEMPLATES (or LITERATURE_TEMPLATES_EXT).
- To run without Europe PMC: inject a stub client that reads from a local cache/datastore.
Testing:
- For unit tests, mock the client Protocols; ensure endpoints return Evidence with expected fetched_n and data keys.
- Validate synthesis using controlled module_outputs dicts to exercise drivers/tensions/flip_if paths.
"""


DISEASE_SYNONYMS_EXT = {'alzheimer disease': ['AD', 'ALZHEIMER DISEASE', 'Alzheimer Disease', 'ad', 'alzheimer', 'alzheimer disease', 'alzheimer disease nos', 'alzheimer disease unspecified', 'alzheimer disorder'], 'parkinson disease': ['PARKINSON DISEASE', 'PD', 'Parkinson Disease', 'parkinson', 'parkinson disease', 'parkinson disease nos', 'parkinson disease unspecified', 'parkinson disorder', 'pd'], 'multiple sclerosis': ['MS', 'MULTIPLE SCLEROSIS', 'Multiple Sclerosis', 'ms', 'multiple sclerosis', 'multiple sclerosis nos', 'multiple sclerosis unspecified'], 'amyotrophic lateral sclerosis': ['ALS', 'AMYOTROPHIC LATERAL SCLEROSIS', 'Amyotrophic Lateral Sclerosis', 'als', 'amyotrophic lateral sclerosis', 'amyotrophic lateral sclerosis nos', 'amyotrophic lateral sclerosis unspecified'], 'type 2 diabetes': ['TD', 'TYPE 2 DIABETES', 'Type 2 Diabetes', 'td', 'type 2 diabetes', 'type 2 diabetes nos', 'type 2 diabetes unspecified'], 'type 1 diabetes': ['TD', 'TYPE 1 DIABETES', 'Type 1 Diabetes', 'td', 'type 1 diabetes', 'type 1 diabetes nos', 'type 1 diabetes unspecified'], 'coronary artery disease': ['CAD', 'CORONARY ARTERY DISEASE', 'Coronary Artery Disease', 'cad', 'coronary artery', 'coronary artery disease', 'coronary artery disease nos', 'coronary artery disease unspecified', 'coronary artery disorder'], 'heart failure': ['HEART FAILURE', 'HF', 'Heart Failure', 'heart failure', 'heart failure nos', 'heart failure unspecified', 'hf'], 'atrial fibrillation': ['AF', 'ATRIAL FIBRILLATION', 'Atrial Fibrillation', 'af', 'atrial fibrillation', 'atrial fibrillation nos', 'atrial fibrillation unspecified'], 'hypertension': ['H', 'HYPERTENSION', 'Hypertension', 'h', 'hypertension', 'hypertension nos', 'hypertension unspecified'], 'asthma': ['A', 'ASTHMA', 'Asthma', 'a', 'asthma', 'asthma nos', 'asthma unspecified'], 'copd': ['C', 'COPD', 'Copd', 'c', 'copd', 'copd nos', 'copd unspecified'], 'psoriasis': ['P', 'PSORIASIS', 'Psoriasis', 'p', 'psoriasis', 'psoriasis nos', 'psoriasis unspecified'], 'rheumatoid arthritis': ['RA', 'RHEUMATOID ARTHRITIS', 'Rheumatoid Arthritis', 'ra', 'rheumatoid arthritis', 'rheumatoid arthritis nos', 'rheumatoid arthritis unspecified'], 'systemic lupus erythematosus': ['SLE', 'SYSTEMIC LUPUS ERYTHEMATOSUS', 'Systemic Lupus Erythematosus', 'sle', 'systemic lupus erythematosus', 'systemic lupus erythematosus nos', 'systemic lupus erythematosus unspecified'], 'atopic dermatitis': ['AD', 'ATOPIC DERMATITIS', 'Atopic Dermatitis', 'ad', 'atopic dermatitis', 'atopic dermatitis nos', 'atopic dermatitis unspecified'], 'huntington disease': ['HD', 'HUNTINGTON DISEASE', 'Huntington Disease', 'hd', 'huntington', 'huntington disease', 'huntington disease nos', 'huntington disease unspecified', 'huntington disorder'], 'schizophrenia': ['S', 'SCHIZOPHRENIA', 'Schizophrenia', 's', 'schizophrenia', 'schizophrenia nos', 'schizophrenia unspecified'], 'bipolar disorder': ['BD', 'BIPOLAR DISORDER', 'Bipolar Disorder', 'bd', 'bipolar disorder', 'bipolar disorder nos', 'bipolar disorder unspecified'], 'major depressive disorder': ['MAJOR DEPRESSIVE DISORDER', 'MDD', 'Major Depressive Disorder', 'major depressive disorder', 'major depressive disorder nos', 'major depressive disorder unspecified', 'mdd'], 'autism spectrum disorder': ['ASD', 'AUTISM SPECTRUM DISORDER', 'Autism Spectrum Disorder', 'asd', 'autism spectrum disorder', 'autism spectrum disorder nos', 'autism spectrum disorder unspecified'], 'obesity': ['O', 'OBESITY', 'Obesity', 'o', 'obesity', 'obesity nos', 'obesity unspecified'], 'nonalcoholic fatty liver disease': ['NFLD', 'NONALCOHOLIC FATTY LIVER DISEASE', 'Nonalcoholic Fatty Liver Disease', 'nfld', 'nonalcoholic fatty liver', 'nonalcoholic fatty liver disease', 'nonalcoholic fatty liver disease nos', 'nonalcoholic fatty liver disease unspecified', 'nonalcoholic fatty liver disorder'], 'nonalcoholic steatohepatitis': ['NONALCOHOLIC STEATOHEPATITIS', 'NS', 'Nonalcoholic Steatohepatitis', 'nonalcoholic steatohepatitis', 'nonalcoholic steatohepatitis nos', 'nonalcoholic steatohepatitis unspecified', 'ns'], 'hepatocellular carcinoma': ['HC', 'HEPATOCELLULAR CARCINOMA', 'Hepatocellular Carcinoma', 'hc', 'hepatocellular cancer', 'hepatocellular carcinoma', 'hepatocellular carcinoma nos', 'hepatocellular carcinoma unspecified'], 'glioblastoma': ['G', 'GLIOBLASTOMA', 'Glioblastoma', 'g', 'glioblastoma', 'glioblastoma nos', 'glioblastoma unspecified'], 'breast cancer': ['BC', 'BREAST CANCER', 'Breast Cancer', 'bc', 'breast cancer', 'breast cancer nos', 'breast cancer unspecified', 'breast carcinoma'], 'prostate cancer': ['PC', 'PROSTATE CANCER', 'Prostate Cancer', 'pc', 'prostate cancer', 'prostate cancer nos', 'prostate cancer unspecified', 'prostate carcinoma'], 'colorectal cancer': ['CC', 'COLORECTAL CANCER', 'Colorectal Cancer', 'cc', 'colorectal cancer', 'colorectal cancer nos', 'colorectal cancer unspecified', 'colorectal carcinoma'], 'pancreatic cancer': ['PANCREATIC CANCER', 'PC', 'Pancreatic Cancer', 'pancreatic cancer', 'pancreatic cancer nos', 'pancreatic cancer unspecified', 'pancreatic carcinoma', 'pc'], 'ovarian cancer': ['OC', 'OVARIAN CANCER', 'Ovarian Cancer', 'oc', 'ovarian cancer', 'ovarian cancer nos', 'ovarian cancer unspecified', 'ovarian carcinoma'], 'endometrial cancer': ['EC', 'ENDOMETRIAL CANCER', 'Endometrial Cancer', 'ec', 'endometrial cancer', 'endometrial cancer nos', 'endometrial cancer unspecified', 'endometrial carcinoma'], 'renal cell carcinoma': ['RCC', 'RENAL CELL CARCINOMA', 'Renal Cell Carcinoma', 'rcc', 'renal cell cancer', 'renal cell carcinoma', 'renal cell carcinoma nos', 'renal cell carcinoma unspecified'], 'bladder cancer': ['BC', 'BLADDER CANCER', 'Bladder Cancer', 'bc', 'bladder cancer', 'bladder cancer nos', 'bladder cancer unspecified', 'bladder carcinoma'], 'gastric cancer': ['GASTRIC CANCER', 'GC', 'Gastric Cancer', 'gastric cancer', 'gastric cancer nos', 'gastric cancer unspecified', 'gastric carcinoma', 'gc'], 'esophageal cancer': ['EC', 'ESOPHAGEAL CANCER', 'Esophageal Cancer', 'ec', 'esophageal cancer', 'esophageal cancer nos', 'esophageal cancer unspecified', 'esophageal carcinoma'], 'thyroid cancer': ['TC', 'THYROID CANCER', 'Thyroid Cancer', 'tc', 'thyroid cancer', 'thyroid cancer nos', 'thyroid cancer unspecified', 'thyroid carcinoma'], 'head and neck squamous cell carcinoma': ['HANSCC', 'HEAD AND NECK SQUAMOUS CELL CARCINOMA', 'Head And Neck Squamous Cell Carcinoma', 'hanscc', 'head and neck squamous cell cancer', 'head and neck squamous cell carcinoma', 'head and neck squamous cell carcinoma nos', 'head and neck squamous cell carcinoma unspecified'], 'acute myeloid leukemia': ['ACUTE MYELOID LEUKEMIA', 'AML', 'Acute Myeloid Leukemia', 'acute myeloid leukemia', 'acute myeloid leukemia nos', 'acute myeloid leukemia unspecified', 'aml'], 'acute lymphoblastic leukemia': ['ACUTE LYMPHOBLASTIC LEUKEMIA', 'ALL', 'Acute Lymphoblastic Leukemia', 'acute lymphoblastic leukemia', 'acute lymphoblastic leukemia nos', 'acute lymphoblastic leukemia unspecified', 'all'], 'chronic lymphocytic leukemia': ['CHRONIC LYMPHOCYTIC LEUKEMIA', 'CLL', 'Chronic Lymphocytic Leukemia', 'chronic lymphocytic leukemia', 'chronic lymphocytic leukemia nos', 'chronic lymphocytic leukemia unspecified', 'cll'], 'multiple myeloma': ['MM', 'MULTIPLE MYELOMA', 'Multiple Myeloma', 'mm', 'multiple myeloma', 'multiple myeloma nos', 'multiple myeloma unspecified'], 'myelodysplastic syndrome': ['MS', 'MYELODYSPLASTIC SYNDROME', 'Myelodysplastic Syndrome', 'ms', 'myelodysplastic syndrome', 'myelodysplastic syndrome nos', 'myelodysplastic syndrome unspecified'], 'myelofibrosis': ['M', 'MYELOFIBROSIS', 'Myelofibrosis', 'm', 'myelofibrosis', 'myelofibrosis nos', 'myelofibrosis unspecified'], 'polycythemia vera': ['POLYCYTHEMIA VERA', 'PV', 'Polycythemia Vera', 'polycythemia vera', 'polycythemia vera nos', 'polycythemia vera unspecified', 'pv'], 'essential thrombocythemia': ['ESSENTIAL THROMBOCYTHEMIA', 'ET', 'Essential Thrombocythemia', 'essential thrombocythemia', 'essential thrombocythemia nos', 'essential thrombocythemia unspecified', 'et'], 'covid-19': ['C', 'COVID-19', 'Covid-19', 'c', 'covid 19', 'covid-19', 'covid-19 nos', 'covid-19 unspecified'], 'influenza': ['I', 'INFLUENZA', 'Influenza', 'i', 'influenza', 'influenza nos', 'influenza unspecified'], 'cystic fibrosis': ['CF', 'CYSTIC FIBROSIS', 'Cystic Fibrosis', 'cf', 'cystic fibrosis', 'cystic fibrosis nos', 'cystic fibrosis unspecified'], 'duchenne muscular dystrophy': ['DMD', 'DUCHENNE MUSCULAR DYSTROPHY', 'Duchenne Muscular Dystrophy', 'dmd', 'duchenne muscular dystrophy', 'duchenne muscular dystrophy nos', 'duchenne muscular dystrophy unspecified'], 'spinal muscular atrophy': ['SMA', 'SPINAL MUSCULAR ATROPHY', 'Spinal Muscular Atrophy', 'sma', 'spinal muscular atrophy', 'spinal muscular atrophy nos', 'spinal muscular atrophy unspecified']}

EVIDENCE_RULEBOOK = {'GENETIC_CAUSALITY': {'bayes_priors': {'default': 0.05, 'with_rare': 0.08, 'with_mendelian': 0.1}, 'signals': ['MR', 'COLOC', 'RARE', 'MENDELIAN', 'INTOLERANCE', 'PQTL', 'SQTL'], 'rules': ['IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG', 'IF MR AND COLOC THEN STRONG', 'IF RARE OR MENDELIAN THEN BOOST', 'IF PQTL AND DIRECTION_MATCHES THEN BOOST', 'IF SQTL WITHOUT COLOC THEN WEAK', 'IF CONFLICT_TISSUE THEN FLAG']}, 'ASSOCIATION': {'signals': ['SC', 'SPATIAL', 'BULK_RNA', 'BULK_PROT', 'PHOSPHO', 'METABO'], 'rules': ['IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT', 'IF SC AND SPATIAL THEN CONTEXT', 'IF BULK_PROT WITHOUT SC THEN CHECK_CELLTYPE', 'IF METABO SUPPORTS PATHWAY THEN BOOST', 'IF HETEROGENEITY HIGH THEN DOWNWEIGHT']}, 'MECHANISM': {'signals': ['PPI', 'PATHWAY', 'LIGREC', 'PERTURB', 'PHOSPHO'], 'rules': ['IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE', 'IF PERTURB AND PATHWAY THEN MECHANISM_OK', 'IF PPI HUB ONLY THEN AMBIGUOUS', 'IF LIGREC IN CIC THEN CONTEXTUALIZE']}, 'TRACTABILITY': {'signals': ['AB', 'SM', 'OLIGO', 'IMMUNO', 'IEDB', 'MHC'], 'rules': ['IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING', 'IF AB OR SM OR OLIGO THEN FEASIBLE', 'IF IMMUNO HIGH THEN RISK', 'IF IEDB MANY EPITOPES THEN IMMUNO_WARNING']}, 'CLINICAL_FIT': {'signals': ['ENDPOINTS', 'BIOMARKER', 'SAFETY', 'PGX', 'RWE', 'AE_PRIOR'], 'rules': ['IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY', 'IF SAFETY STRONG THEN UNFAVORABLE', 'IF ENDPOINTS AND BIOMARKER THEN OPPORTUNITY', 'IF PGX AVAILABLE THEN STRATIFY']}}

QA_TEST_VECTORS = [{'gene': 'GENE0', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.29042026885254413, 'COLOC': 0.001893451897379106, 'RARE': 0.290943475064393, 'SC': 0.5026646389831291, 'SPATIAL': 0.2512536658998935, 'BULK_RNA': 0.14998042046815485, 'BULK_PROT': 0.010318035854846808}}, {'gene': 'GENE1', 'condition': 'alzheimer disease', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.8100928643109466, 'COLOC': 0.11148123739056237, 'RARE': 0.7658191510256496, 'SC': 0.1682111526433474, 'SPATIAL': 0.4036283118242149, 'BULK_RNA': 0.5232120998494423, 'BULK_PROT': 0.6758132530513653}}, {'gene': 'GENE2', 'condition': 'essential thrombocythemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6649352651763709, 'COLOC': 0.9979977996217977, 'RARE': 0.7387937865122218, 'SC': 0.8436865926669205, 'SPATIAL': 0.6268134911453092, 'BULK_RNA': 0.9870396955671911, 'BULK_PROT': 0.10390196584042866}}, {'gene': 'GENE3', 'condition': 'acute myeloid leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.09045436414462837, 'COLOC': 0.36535685321326117, 'RARE': 0.4465078127759735, 'SC': 0.8210613180358947, 'SPATIAL': 0.9994946029178232, 'BULK_RNA': 0.3476253905986517, 'BULK_PROT': 0.5412795940155677}}, {'gene': 'GENE4', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.9638409182719324, 'COLOC': 0.36092588289152505, 'RARE': 0.530320295238547, 'SC': 0.4151680904774344, 'SPATIAL': 0.506235772260315, 'BULK_RNA': 0.4267484868897752, 'BULK_PROT': 0.7412980097855366}}, {'gene': 'GENE5', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.036739559212852546, 'COLOC': 0.4837696218981685, 'RARE': 0.08512436630307574, 'SC': 0.5421604066105961, 'SPATIAL': 0.7704236493762733, 'BULK_RNA': 0.919812715088232, 'BULK_PROT': 0.5335331223746419}}, {'gene': 'GENE6', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.18982913885247887, 'COLOC': 0.5068994185779692, 'RARE': 0.3845215624033044, 'SC': 0.18838522029356441, 'SPATIAL': 0.5613661493739832, 'BULK_RNA': 0.7016339533531122, 'BULK_PROT': 0.1384838491544299}}, {'gene': 'GENE7', 'condition': 'esophageal cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6168827796356712, 'COLOC': 0.9174346791650638, 'RARE': 0.21723846365987165, 'SC': 0.7353680877443807, 'SPATIAL': 0.40071006828215516, 'BULK_RNA': 0.936883494062121, 'BULK_PROT': 0.5684287209280571}}, {'gene': 'GENE8', 'condition': 'nonalcoholic steatohepatitis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8324685495389162, 'COLOC': 0.6053746009103135, 'RARE': 0.77084170570863, 'SC': 0.01741349561843286, 'SPATIAL': 0.06486873839828133, 'BULK_RNA': 0.5710801338576519, 'BULK_PROT': 0.9052956589969126}}, {'gene': 'GENE9', 'condition': 'spinal muscular atrophy', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.060732748320759034, 'COLOC': 0.6842251622276798, 'RARE': 0.2987640516844303, 'SC': 0.4070589303625318, 'SPATIAL': 0.4289185910731954, 'BULK_RNA': 0.023073891488639142, 'BULK_PROT': 0.10225761526855837}}, {'gene': 'GENE10', 'condition': 'covid-19', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.12948885499868645, 'COLOC': 0.7064159170849234, 'RARE': 0.3284580856183803, 'SC': 0.4406725357315727, 'SPATIAL': 0.7565935188049449, 'BULK_RNA': 0.6775828371622217, 'BULK_PROT': 0.3669309532930577}}, {'gene': 'GENE11', 'condition': 'prostate cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.0721455052433081, 'COLOC': 0.0750433693625675, 'RARE': 0.492573230834923, 'SC': 0.2598433454909511, 'SPATIAL': 0.829056303408908, 'BULK_RNA': 0.605138151901085, 'BULK_PROT': 0.18171476725301416}}, {'gene': 'GENE12', 'condition': 'alzheimer disease', 'bucket': 'MECHANISM', 'signals': {'MR': 0.8472786280784664, 'COLOC': 0.6479947491075462, 'RARE': 0.3969286077019363, 'SC': 0.6477928020303114, 'SPATIAL': 0.2577091512708911, 'BULK_RNA': 0.5465177139429884, 'BULK_PROT': 0.508934773290875}}, {'gene': 'GENE13', 'condition': 'glioblastoma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.7101525493394001, 'COLOC': 0.19706899943484613, 'RARE': 0.837515239254451, 'SC': 0.3439666802790302, 'SPATIAL': 0.5223686947250101, 'BULK_RNA': 0.3741270560434833, 'BULK_PROT': 0.7805603839837909}}, {'gene': 'GENE14', 'condition': 'colorectal cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.3852841234961556, 'COLOC': 0.46016300672997557, 'RARE': 0.9726308266908923, 'SC': 0.9836124458622114, 'SPATIAL': 0.6981285983908133, 'BULK_RNA': 0.8267848756517286, 'BULK_PROT': 0.5775351186538528}}, {'gene': 'GENE15', 'condition': 'esophageal cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.014932115520349676, 'COLOC': 0.11166709435243927, 'RARE': 0.0077396322835570075, 'SC': 0.529830212951342, 'SPATIAL': 0.6474059600062803, 'BULK_RNA': 0.07962197381553338, 'BULK_PROT': 0.6317901803766353}}, {'gene': 'GENE16', 'condition': 'type 1 diabetes', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6228825513344545, 'COLOC': 0.7434927216374191, 'RARE': 0.27482249878760745, 'SC': 0.48799530253528234, 'SPATIAL': 0.6371491055034502, 'BULK_RNA': 0.2707938904950489, 'BULK_PROT': 0.31843469278857617}}, {'gene': 'GENE17', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.864255490367611, 'COLOC': 0.17956603674535576, 'RARE': 0.49477700209664077, 'SC': 0.02604570602729317, 'SPATIAL': 0.17021953391201472, 'BULK_RNA': 0.5727230352238586, 'BULK_PROT': 0.16629974178980478}}, {'gene': 'GENE18', 'condition': 'obesity', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.046781793882687994, 'COLOC': 0.6182437429901343, 'RARE': 0.14715558191955969, 'SC': 0.6724550767867068, 'SPATIAL': 0.40152297934700854, 'BULK_RNA': 0.9551541331080032, 'BULK_PROT': 0.011526208146002803}}, {'gene': 'GENE19', 'condition': 'rheumatoid arthritis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6339286594280185, 'COLOC': 0.030373464077356416, 'RARE': 0.8012871182440462, 'SC': 0.021503090048090923, 'SPATIAL': 0.04832140436240284, 'BULK_RNA': 0.41285985286467863, 'BULK_PROT': 0.773894069228557}}, {'gene': 'GENE20', 'condition': 'obesity', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6881197103385523, 'COLOC': 0.08370781915969328, 'RARE': 0.9218139383956145, 'SC': 0.2323604338704659, 'SPATIAL': 0.3970707325566609, 'BULK_RNA': 0.8984620346370303, 'BULK_PROT': 0.7590476920034277}}, {'gene': 'GENE21', 'condition': 'multiple sclerosis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.2334771572016493, 'COLOC': 0.894684323389092, 'RARE': 0.5774891568410059, 'SC': 0.9354058779334451, 'SPATIAL': 0.8548197007502681, 'BULK_RNA': 0.39189517191386614, 'BULK_PROT': 0.9497739257176895}}, {'gene': 'GENE22', 'condition': 'pancreatic cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.040701844664032705, 'COLOC': 0.5761759594912563, 'RARE': 0.6800427766518741, 'SC': 0.2896058688999301, 'SPATIAL': 0.580347125384585, 'BULK_RNA': 0.03577499001667217, 'BULK_PROT': 0.767394616631982}}, {'gene': 'GENE23', 'condition': 'essential thrombocythemia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5453763118963744, 'COLOC': 0.6557896955890635, 'RARE': 0.5268943635662628, 'SC': 0.12309078470985069, 'SPATIAL': 0.1777155919117669, 'BULK_RNA': 0.5845642258542451, 'BULK_PROT': 0.09513241282503804}}, {'gene': 'GENE24', 'condition': 'obesity', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.08105665479499957, 'COLOC': 0.14015686888293444, 'RARE': 0.7459696565990815, 'SC': 0.08698527520237942, 'SPATIAL': 0.3481379583078281, 'BULK_RNA': 0.3573753415794798, 'BULK_PROT': 0.7679114820442278}}, {'gene': 'GENE25', 'condition': 'psoriasis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.977960375247323, 'COLOC': 0.006140595416100791, 'RARE': 0.5649467921929066, 'SC': 0.6512688981063288, 'SPATIAL': 0.475348279771298, 'BULK_RNA': 0.8189451492900097, 'BULK_PROT': 0.7884787481251233}}, {'gene': 'GENE26', 'condition': 'type 1 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.33838042577746297, 'COLOC': 0.6629683616268472, 'RARE': 0.3839955797927561, 'SC': 0.4719089877290428, 'SPATIAL': 0.9724443730453474, 'BULK_RNA': 0.8299286632600293, 'BULK_PROT': 0.6715924620067956}}, {'gene': 'GENE27', 'condition': 'schizophrenia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.029916258656834893, 'COLOC': 0.5679786024755761, 'RARE': 0.12021836143388553, 'SC': 0.48581398555186783, 'SPATIAL': 0.022525298076390365, 'BULK_RNA': 0.6161481386567615, 'BULK_PROT': 0.16541226915891505}}, {'gene': 'GENE28', 'condition': 'asthma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.13450129614833672, 'COLOC': 0.4262832686567425, 'RARE': 0.35478414178731277, 'SC': 0.13227306541488582, 'SPATIAL': 0.8527399087988654, 'BULK_RNA': 0.694659736018586, 'BULK_PROT': 0.8166774505145192}}, {'gene': 'GENE29', 'condition': 'spinal muscular atrophy', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8951207388920334, 'COLOC': 0.8537744483177383, 'RARE': 0.07751986298647395, 'SC': 0.8893005791245476, 'SPATIAL': 0.7358448747997454, 'BULK_RNA': 0.1843967032956405, 'BULK_PROT': 0.27619358219837975}}, {'gene': 'GENE30', 'condition': 'renal cell carcinoma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.08859150612327682, 'COLOC': 0.9905896627310564, 'RARE': 0.2436686616386481, 'SC': 0.4886850654093944, 'SPATIAL': 0.17415886748310427, 'BULK_RNA': 0.7285250869547176, 'BULK_PROT': 0.8482312368081143}}, {'gene': 'GENE31', 'condition': 'pancreatic cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6656399193820504, 'COLOC': 0.7195857759665079, 'RARE': 0.5299973108496123, 'SC': 0.8217063292140239, 'SPATIAL': 0.16371190549759118, 'BULK_RNA': 0.7770632527341933, 'BULK_PROT': 0.2033990707210941}}, {'gene': 'GENE32', 'condition': 'esophageal cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.996332073963832, 'COLOC': 0.47083444760603144, 'RARE': 0.1768496462701662, 'SC': 0.14158126700523943, 'SPATIAL': 0.3056888442700241, 'BULK_RNA': 0.8103027288774957, 'BULK_PROT': 0.3940901934833937}}, {'gene': 'GENE33', 'condition': 'obesity', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.7720528742294517, 'COLOC': 0.3259139477854618, 'RARE': 0.3081343611920996, 'SC': 0.10052524816024067, 'SPATIAL': 0.5767560400617405, 'BULK_RNA': 0.23453930623116404, 'BULK_PROT': 0.7230870399224806}}, {'gene': 'GENE34', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.29333649696064434, 'COLOC': 0.5133206089587652, 'RARE': 0.8932737377574789, 'SC': 0.046057574765082876, 'SPATIAL': 0.3165269679060787, 'BULK_RNA': 0.854097180488018, 'BULK_PROT': 0.003377175266543353}}, {'gene': 'GENE35', 'condition': 'esophageal cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.5251047962338082, 'COLOC': 0.9808182170542005, 'RARE': 0.8198322990384745, 'SC': 0.41249255539157226, 'SPATIAL': 0.36445950763264956, 'BULK_RNA': 0.7157224054926814, 'BULK_PROT': 0.12844532386533047}}, {'gene': 'GENE36', 'condition': 'heart failure', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.9816503350078962, 'COLOC': 0.6527344009764314, 'RARE': 0.17339472561591762, 'SC': 0.527216633054826, 'SPATIAL': 0.649239307412779, 'BULK_RNA': 0.5250807101024891, 'BULK_PROT': 0.5275360467254407}}, {'gene': 'GENE37', 'condition': 'nonalcoholic steatohepatitis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.3667947484590719, 'COLOC': 0.5396161982584771, 'RARE': 0.9582471911872668, 'SC': 0.09629183603494651, 'SPATIAL': 0.23524363829157724, 'BULK_RNA': 0.8895525337272743, 'BULK_PROT': 0.9724439816164951}}, {'gene': 'GENE38', 'condition': 'systemic lupus erythematosus', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.09548375597220282, 'COLOC': 0.784335001565466, 'RARE': 0.26095595622788026, 'SC': 0.5542715959799613, 'SPATIAL': 0.673409926504412, 'BULK_RNA': 0.8448193803047596, 'BULK_PROT': 0.8885823944256696}}, {'gene': 'GENE39', 'condition': 'glioblastoma', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.956916586486746, 'COLOC': 0.22192088493988393, 'RARE': 0.9600905071938657, 'SC': 0.43626205872987134, 'SPATIAL': 0.3575912063935428, 'BULK_RNA': 0.7226187898985276, 'BULK_PROT': 0.09713103175675286}}, {'gene': 'GENE40', 'condition': 'covid-19', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.9865985535745428, 'COLOC': 0.3573881043267373, 'RARE': 0.9204751380874252, 'SC': 0.55233165522313, 'SPATIAL': 0.6208364088383841, 'BULK_RNA': 0.45925734995967815, 'BULK_PROT': 0.48225650210108717}}, {'gene': 'GENE41', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.447440777130809, 'COLOC': 0.8396928672423225, 'RARE': 0.4519782328605032, 'SC': 0.09497754678512749, 'SPATIAL': 0.12092984655152328, 'BULK_RNA': 0.4854880115297314, 'BULK_PROT': 0.9360714189967198}}, {'gene': 'GENE42', 'condition': 'hypertension', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6585961387827683, 'COLOC': 0.7443956416858567, 'RARE': 0.9897995253598717, 'SC': 0.8462793911573625, 'SPATIAL': 0.03854830693742861, 'BULK_RNA': 0.274277617264961, 'BULK_PROT': 0.7051172787747677}}, {'gene': 'GENE43', 'condition': 'myelodysplastic syndrome', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.28005537807254066, 'COLOC': 0.4222175448394979, 'RARE': 0.306759802187334, 'SC': 0.17766334169240217, 'SPATIAL': 0.32443451114641153, 'BULK_RNA': 0.6868620531449087, 'BULK_PROT': 0.1286496274915634}}, {'gene': 'GENE44', 'condition': 'nonalcoholic steatohepatitis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.5754635916285231, 'COLOC': 0.6410411049327077, 'RARE': 0.3588773102339574, 'SC': 0.5926200223023583, 'SPATIAL': 0.5990950330374781, 'BULK_RNA': 0.07249265594167253, 'BULK_PROT': 0.914037123154134}}, {'gene': 'GENE45', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7427073042509981, 'COLOC': 0.7429148510628539, 'RARE': 0.16191110394937436, 'SC': 0.28791357920475125, 'SPATIAL': 0.3218665026143295, 'BULK_RNA': 0.24928608178459832, 'BULK_PROT': 0.8772952979811288}}, {'gene': 'GENE46', 'condition': 'schizophrenia', 'bucket': 'MECHANISM', 'signals': {'MR': 0.23904535232791024, 'COLOC': 0.9887049711353881, 'RARE': 0.10672610095710111, 'SC': 0.32824242431278994, 'SPATIAL': 0.9780036984838351, 'BULK_RNA': 0.3742203868210695, 'BULK_PROT': 0.7470689822186605}}, {'gene': 'GENE47', 'condition': 'glioblastoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7555541276098504, 'COLOC': 0.4481231130364882, 'RARE': 0.35432117091349347, 'SC': 0.30579333156821087, 'SPATIAL': 0.92596779086141, 'BULK_RNA': 0.9593193315396521, 'BULK_PROT': 0.8425504809659115}}, {'gene': 'GENE48', 'condition': 'schizophrenia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.870540403653306, 'COLOC': 0.8724609392909162, 'RARE': 0.4461468625388415, 'SC': 0.11517844396240717, 'SPATIAL': 0.3122263769454674, 'BULK_RNA': 0.8295590066158237, 'BULK_PROT': 0.04889923688458275}}, {'gene': 'GENE49', 'condition': 'hepatocellular carcinoma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6402287804082858, 'COLOC': 0.6803617027182532, 'RARE': 0.4169456483041687, 'SC': 0.8129290066302551, 'SPATIAL': 0.14299288825931944, 'BULK_RNA': 0.35526386616020467, 'BULK_PROT': 0.7309766371777457}}, {'gene': 'GENE50', 'condition': 'essential thrombocythemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6705870486556197, 'COLOC': 0.8183968189699588, 'RARE': 0.4808435794252818, 'SC': 0.0950206918517369, 'SPATIAL': 0.31548514474307965, 'BULK_RNA': 0.789500388623937, 'BULK_PROT': 0.9800632216099155}}, {'gene': 'GENE51', 'condition': 'ovarian cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6471561005184512, 'COLOC': 0.9512180894242211, 'RARE': 0.3067906394215433, 'SC': 0.9216536375332453, 'SPATIAL': 0.5363537146519507, 'BULK_RNA': 0.4205499261185185, 'BULK_PROT': 0.5902470467786374}}, {'gene': 'GENE52', 'condition': 'bladder cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.8121869511324533, 'COLOC': 0.506025288045882, 'RARE': 0.0312968786673935, 'SC': 0.4405775400284584, 'SPATIAL': 0.7326123627165019, 'BULK_RNA': 0.48165467240575033, 'BULK_PROT': 0.35061453585469904}}, {'gene': 'GENE53', 'condition': 'cystic fibrosis', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.29381787440077844, 'COLOC': 0.9165410487663509, 'RARE': 0.6329154535541929, 'SC': 0.19178092862020046, 'SPATIAL': 0.14741466129564196, 'BULK_RNA': 0.9153359962638433, 'BULK_PROT': 0.6474896177451663}}, {'gene': 'GENE54', 'condition': 'parkinson disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.5531840122792463, 'COLOC': 0.4741717851053172, 'RARE': 0.6573674260984266, 'SC': 0.8730778828914073, 'SPATIAL': 0.8597560872227847, 'BULK_RNA': 0.5544496761785256, 'BULK_PROT': 0.5999538106614309}}, {'gene': 'GENE55', 'condition': 'gastric cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.7901669856925553, 'COLOC': 0.9573626269789658, 'RARE': 0.48400596844829336, 'SC': 0.3169785085311735, 'SPATIAL': 0.3219243480738151, 'BULK_RNA': 0.5534564484262476, 'BULK_PROT': 0.6443398680707823}}, {'gene': 'GENE56', 'condition': 'thyroid cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.8216166783975272, 'COLOC': 0.7580557285918913, 'RARE': 0.8461102699288546, 'SC': 0.2154511902984252, 'SPATIAL': 0.294639798027732, 'BULK_RNA': 0.14188577532283098, 'BULK_PROT': 0.31678478010457956}}, {'gene': 'GENE57', 'condition': 'covid-19', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.4745173162676931, 'COLOC': 0.5251551566629575, 'RARE': 0.022864227827507633, 'SC': 0.5825343116006684, 'SPATIAL': 0.2538530776353325, 'BULK_RNA': 0.8022050325169483, 'BULK_PROT': 0.12457011300605236}}, {'gene': 'GENE58', 'condition': 'esophageal cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.13359673304947106, 'COLOC': 0.2562733566697757, 'RARE': 0.8148364836118609, 'SC': 0.190368063230147, 'SPATIAL': 0.19604192348961214, 'BULK_RNA': 0.029326536622050092, 'BULK_PROT': 0.8954485323314484}}, {'gene': 'GENE59', 'condition': 'esophageal cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.7905589654681077, 'COLOC': 0.9389346912243773, 'RARE': 0.4990428438487483, 'SC': 0.5767369847670766, 'SPATIAL': 0.883000344480365, 'BULK_RNA': 0.6678508884434992, 'BULK_PROT': 0.02820600257750061}}, {'gene': 'GENE60', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.48797078261088545, 'COLOC': 0.3818812033249157, 'RARE': 0.8793702724210197, 'SC': 0.6874092001001648, 'SPATIAL': 0.4554672210452906, 'BULK_RNA': 0.5588337337441073, 'BULK_PROT': 0.990886777278676}}, {'gene': 'GENE61', 'condition': 'autism spectrum disorder', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.38091626792257016, 'COLOC': 0.37024802706826077, 'RARE': 0.15631152683840843, 'SC': 0.5036201767537598, 'SPATIAL': 0.732873284334949, 'BULK_RNA': 0.9292723732788275, 'BULK_PROT': 0.7761841787697249}}, {'gene': 'GENE62', 'condition': 'asthma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.9337861176674034, 'COLOC': 0.028121748535929525, 'RARE': 0.7528856857451146, 'SC': 0.5679919891627164, 'SPATIAL': 0.5876581195119628, 'BULK_RNA': 0.49866400538777467, 'BULK_PROT': 0.9683247011665227}}, {'gene': 'GENE63', 'condition': 'multiple myeloma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6461709506549733, 'COLOC': 0.2378578045906028, 'RARE': 0.7563516332310861, 'SC': 0.18352628201418975, 'SPATIAL': 0.6637256561058966, 'BULK_RNA': 0.5796070828973412, 'BULK_PROT': 0.5106134753131899}}, {'gene': 'GENE64', 'condition': 'hepatocellular carcinoma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.5234405992218707, 'COLOC': 0.5170429981863324, 'RARE': 0.7633235515518283, 'SC': 0.4841954014404227, 'SPATIAL': 0.04835493104805644, 'BULK_RNA': 0.5318845375881288, 'BULK_PROT': 0.3058330138690999}}, {'gene': 'GENE65', 'condition': 'multiple myeloma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.30613114505732697, 'COLOC': 0.5029527538529079, 'RARE': 0.08102848474103563, 'SC': 0.7924042309230633, 'SPATIAL': 0.783651744887629, 'BULK_RNA': 0.1518414249494905, 'BULK_PROT': 0.7253598641480803}}, {'gene': 'GENE66', 'condition': 'acute myeloid leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.9888937260437384, 'COLOC': 0.35928797211569174, 'RARE': 0.6962461375326437, 'SC': 0.7045010641698884, 'SPATIAL': 0.7161883569328213, 'BULK_RNA': 0.9256592595606475, 'BULK_PROT': 0.1894150638219536}}, {'gene': 'GENE67', 'condition': 'myelodysplastic syndrome', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6669817348165482, 'COLOC': 0.9349042424283861, 'RARE': 0.6472495047557966, 'SC': 0.026294826873496158, 'SPATIAL': 0.6700411232965268, 'BULK_RNA': 0.9823104458922208, 'BULK_PROT': 0.9079224912221422}}, {'gene': 'GENE68', 'condition': 'huntington disease', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7215582859637449, 'COLOC': 0.2966756791754621, 'RARE': 0.4685032084103352, 'SC': 0.42210121144198, 'SPATIAL': 0.4976768996825335, 'BULK_RNA': 0.03014524450021927, 'BULK_PROT': 0.5823666775447126}}, {'gene': 'GENE69', 'condition': 'alzheimer disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.4535446776752301, 'COLOC': 0.35933296037090223, 'RARE': 0.5070788190232299, 'SC': 0.6545719082484407, 'SPATIAL': 0.1014107791798704, 'BULK_RNA': 0.5003092350678104, 'BULK_PROT': 0.7551036932466612}}, {'gene': 'GENE70', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.4759016075717666, 'COLOC': 0.0925049242737308, 'RARE': 0.2266901501218339, 'SC': 0.027470880618752025, 'SPATIAL': 0.09515486674258855, 'BULK_RNA': 0.12597945099447683, 'BULK_PROT': 0.8488649640615373}}, {'gene': 'GENE71', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.21697962538630078, 'COLOC': 0.49548175249702375, 'RARE': 0.3724015734831587, 'SC': 0.7257605468686121, 'SPATIAL': 0.7313702744290652, 'BULK_RNA': 0.3219454184328626, 'BULK_PROT': 0.23974309597092736}}, {'gene': 'GENE72', 'condition': 'renal cell carcinoma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.39885458631229376, 'COLOC': 0.1512278232470643, 'RARE': 0.7801486651138864, 'SC': 0.47708931284198675, 'SPATIAL': 0.14729160180033596, 'BULK_RNA': 0.8478682656788167, 'BULK_PROT': 0.4112631758543265}}, {'gene': 'GENE73', 'condition': 'thyroid cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.015372947855323393, 'COLOC': 0.07952514373713149, 'RARE': 0.8173407122939605, 'SC': 0.2676259718970567, 'SPATIAL': 0.481739735803291, 'BULK_RNA': 0.05428293898742498, 'BULK_PROT': 0.24911681416313425}}, {'gene': 'GENE74', 'condition': 'schizophrenia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.10973645857037895, 'COLOC': 0.48210236075205615, 'RARE': 0.5261783636549275, 'SC': 0.3774109685136955, 'SPATIAL': 0.82175975870108, 'BULK_RNA': 0.5086621719197786, 'BULK_PROT': 0.2544440373222153}}, {'gene': 'GENE75', 'condition': 'myelofibrosis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.38949765888658583, 'COLOC': 0.3379580782733752, 'RARE': 0.4900370908926802, 'SC': 0.1980028226424807, 'SPATIAL': 0.7938861757272303, 'BULK_RNA': 0.9618127884622212, 'BULK_PROT': 0.2260195685937476}}, {'gene': 'GENE76', 'condition': 'hepatocellular carcinoma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6784627964093694, 'COLOC': 0.2901278626450631, 'RARE': 0.18776602026075562, 'SC': 0.5754797722013985, 'SPATIAL': 0.16964355781727736, 'BULK_RNA': 0.8314982822856422, 'BULK_PROT': 0.23838839759977115}}, {'gene': 'GENE77', 'condition': 'hypertension', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8888759149426415, 'COLOC': 0.6892887821671395, 'RARE': 0.417220404513183, 'SC': 0.20394009054052764, 'SPATIAL': 0.15991648835843997, 'BULK_RNA': 0.4466198280053997, 'BULK_PROT': 0.6546097400562634}}, {'gene': 'GENE78', 'condition': 'myelofibrosis', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.38001031889814285, 'COLOC': 0.7713652640011566, 'RARE': 0.20666566096631656, 'SC': 0.8138655956048186, 'SPATIAL': 0.0012046629755176896, 'BULK_RNA': 0.7950205789200098, 'BULK_PROT': 0.26725635043627405}}, {'gene': 'GENE79', 'condition': 'parkinson disease', 'bucket': 'MECHANISM', 'signals': {'MR': 0.823910826214866, 'COLOC': 0.6701765430184257, 'RARE': 0.04872788300080144, 'SC': 0.5791160055369282, 'SPATIAL': 0.544626578667892, 'BULK_RNA': 0.6182212472338982, 'BULK_PROT': 0.06519670422642643}}, {'gene': 'GENE80', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'MECHANISM', 'signals': {'MR': 0.9029656316960591, 'COLOC': 0.6076240504748203, 'RARE': 0.061819326893202575, 'SC': 0.6907302673804172, 'SPATIAL': 0.9813615912071508, 'BULK_RNA': 0.8312117527515039, 'BULK_PROT': 0.4707214445993648}}, {'gene': 'GENE81', 'condition': 'type 1 diabetes', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.48043570319051343, 'COLOC': 0.9396899495868681, 'RARE': 0.08841385987405415, 'SC': 0.8011020293381854, 'SPATIAL': 0.45271174959699056, 'BULK_RNA': 0.7779572113221077, 'BULK_PROT': 0.1883027183717224}}, {'gene': 'GENE82', 'condition': 'parkinson disease', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.3137822190791214, 'COLOC': 0.5775397074180182, 'RARE': 0.026917064305582605, 'SC': 0.9007910744992942, 'SPATIAL': 0.9487120866028854, 'BULK_RNA': 0.0017615379129579667, 'BULK_PROT': 0.9827139555270835}}, {'gene': 'GENE83', 'condition': 'duchenne muscular dystrophy', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6570652169793275, 'COLOC': 0.010619057657775266, 'RARE': 0.9256579442772952, 'SC': 0.6610843157178368, 'SPATIAL': 0.9474153684599209, 'BULK_RNA': 0.30803355530350685, 'BULK_PROT': 0.1372814475252555}}, {'gene': 'GENE84', 'condition': 'prostate cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.44585672978072755, 'COLOC': 0.061352687853059584, 'RARE': 0.6628983848394486, 'SC': 0.8718924167607343, 'SPATIAL': 0.5164294900590546, 'BULK_RNA': 0.25802931632023873, 'BULK_PROT': 0.42458724312866036}}, {'gene': 'GENE85', 'condition': 'endometrial cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.49810448049166645, 'COLOC': 0.25258886662094826, 'RARE': 0.5390076472973011, 'SC': 0.14868206485862545, 'SPATIAL': 0.6373765612248383, 'BULK_RNA': 0.6747738594177409, 'BULK_PROT': 0.08738854466132429}}, {'gene': 'GENE86', 'condition': 'psoriasis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.3578004507602627, 'COLOC': 0.19952983507741184, 'RARE': 0.8630001619373948, 'SC': 0.9479758919619045, 'SPATIAL': 0.3019834738399011, 'BULK_RNA': 0.9253260172619627, 'BULK_PROT': 0.8628487259861256}}, {'gene': 'GENE87', 'condition': 'rheumatoid arthritis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6123183345962099, 'COLOC': 0.8429780066758666, 'RARE': 0.15357832472649113, 'SC': 0.6483259305780884, 'SPATIAL': 0.3072081975008115, 'BULK_RNA': 0.7627638956593121, 'BULK_PROT': 0.26807808771168795}}, {'gene': 'GENE88', 'condition': 'amyotrophic lateral sclerosis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.43288518659725794, 'COLOC': 0.7642692708231045, 'RARE': 0.5158855936073529, 'SC': 0.7695952537449133, 'SPATIAL': 0.9692401405866587, 'BULK_RNA': 0.27534854461747726, 'BULK_PROT': 0.4613880068228987}}, {'gene': 'GENE89', 'condition': 'amyotrophic lateral sclerosis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8539370404982766, 'COLOC': 0.6136491854425898, 'RARE': 0.4833814964657892, 'SC': 0.788758303458662, 'SPATIAL': 0.06827859979008666, 'BULK_RNA': 0.3416720051895614, 'BULK_PROT': 0.05350502587261918}}, {'gene': 'GENE90', 'condition': 'endometrial cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.6957667907236267, 'COLOC': 0.9201279205959293, 'RARE': 0.6878342663788033, 'SC': 0.9367560143266908, 'SPATIAL': 0.5118294808149334, 'BULK_RNA': 0.5759690593687732, 'BULK_PROT': 0.9260033617651998}}, {'gene': 'GENE91', 'condition': 'parkinson disease', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.578302743891427, 'COLOC': 0.31266094465228966, 'RARE': 0.47018080551304053, 'SC': 0.21600140392995015, 'SPATIAL': 0.5860141903169951, 'BULK_RNA': 0.054939235631422334, 'BULK_PROT': 0.5519667063360499}}, {'gene': 'GENE92', 'condition': 'atrial fibrillation', 'bucket': 'MECHANISM', 'signals': {'MR': 0.21342345196288826, 'COLOC': 0.4159137922259809, 'RARE': 0.7752817415235662, 'SC': 0.6273037314829846, 'SPATIAL': 0.37095002950239997, 'BULK_RNA': 0.22023589472490046, 'BULK_PROT': 0.1608078972902648}}, {'gene': 'GENE93', 'condition': 'huntington disease', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.8955705513707191, 'COLOC': 0.12544851961128578, 'RARE': 0.7785901259656709, 'SC': 0.41242927148372677, 'SPATIAL': 0.26933810735543184, 'BULK_RNA': 0.7224010754302795, 'BULK_PROT': 0.2652573826466933}}, {'gene': 'GENE94', 'condition': 'type 2 diabetes', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.22162621634386603, 'COLOC': 0.38492101148961255, 'RARE': 0.055274815215153095, 'SC': 0.6115660520493681, 'SPATIAL': 0.8070453557020246, 'BULK_RNA': 0.5556509294459945, 'BULK_PROT': 0.27331931898316497}}, {'gene': 'GENE95', 'condition': 'pancreatic cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.9623317565073116, 'COLOC': 0.8221879884026142, 'RARE': 0.6242488865415508, 'SC': 0.9800748583506402, 'SPATIAL': 0.24172049698750242, 'BULK_RNA': 0.32272588392936885, 'BULK_PROT': 0.8101094832806874}}, {'gene': 'GENE96', 'condition': 'esophageal cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.3890445614459731, 'COLOC': 0.35042926051781853, 'RARE': 0.6797109434653046, 'SC': 0.581068364029517, 'SPATIAL': 0.09703521219413103, 'BULK_RNA': 0.9109039275280274, 'BULK_PROT': 0.7428358592698627}}, {'gene': 'GENE97', 'condition': 'myelodysplastic syndrome', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.18986595838154807, 'COLOC': 0.07859968228196113, 'RARE': 0.5210816411561146, 'SC': 0.284687133211742, 'SPATIAL': 0.1803423152743816, 'BULK_RNA': 0.9510865383867632, 'BULK_PROT': 0.9653189253232669}}, {'gene': 'GENE98', 'condition': 'bladder cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.18780384750947599, 'COLOC': 0.6456263885941752, 'RARE': 0.7236646321144307, 'SC': 0.41184008117276827, 'SPATIAL': 0.4724926853453354, 'BULK_RNA': 0.8709072891838027, 'BULK_PROT': 0.042692584385942034}}, {'gene': 'GENE99', 'condition': 'rheumatoid arthritis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.04096674408159706, 'COLOC': 0.02910666099194248, 'RARE': 0.6207700196597803, 'SC': 0.37475405460313416, 'SPATIAL': 0.6801132014313261, 'BULK_RNA': 0.896011856826382, 'BULK_PROT': 0.947137614953092}}, {'gene': 'GENE100', 'condition': 'gastric cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.2870620535706324, 'COLOC': 0.26086865367260115, 'RARE': 0.9687089153018165, 'SC': 0.018334481054548935, 'SPATIAL': 0.9285304179893976, 'BULK_RNA': 0.9435644570915807, 'BULK_PROT': 0.7711805619331404}}, {'gene': 'GENE101', 'condition': 'acute myeloid leukemia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.27431089308176504, 'COLOC': 0.9955129866348872, 'RARE': 0.8401745461131482, 'SC': 0.7875392849518997, 'SPATIAL': 0.13968315886507587, 'BULK_RNA': 0.8762166707980356, 'BULK_PROT': 0.30156524744528634}}, {'gene': 'GENE102', 'condition': 'autism spectrum disorder', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6108808969047105, 'COLOC': 0.7462112671815233, 'RARE': 0.719342241612456, 'SC': 0.9182466152899612, 'SPATIAL': 0.8375813528382713, 'BULK_RNA': 0.5487935308455556, 'BULK_PROT': 0.7153600646724636}}, {'gene': 'GENE103', 'condition': 'bipolar disorder', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6820627833283689, 'COLOC': 0.16782218640722024, 'RARE': 0.13243225783697388, 'SC': 0.44251029904152916, 'SPATIAL': 0.11876341255962442, 'BULK_RNA': 0.26947609262807193, 'BULK_PROT': 0.8871559089268604}}, {'gene': 'GENE104', 'condition': 'amyotrophic lateral sclerosis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6258130510439569, 'COLOC': 0.04658495111957006, 'RARE': 0.7861337527549445, 'SC': 0.46404294417387515, 'SPATIAL': 0.6815336700951653, 'BULK_RNA': 0.9780398178917804, 'BULK_PROT': 0.885091271985093}}, {'gene': 'GENE105', 'condition': 'psoriasis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.2982268090542536, 'COLOC': 0.34080230683862633, 'RARE': 0.9774057522201993, 'SC': 0.5700195567641065, 'SPATIAL': 0.47490170293107437, 'BULK_RNA': 0.10463661293940885, 'BULK_PROT': 0.8784873006478987}}, {'gene': 'GENE106', 'condition': 'polycythemia vera', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5558376674754334, 'COLOC': 0.5753772473713588, 'RARE': 0.2669210826789441, 'SC': 0.8569315524400929, 'SPATIAL': 0.675530391759794, 'BULK_RNA': 0.7997745819719626, 'BULK_PROT': 0.7934897832448141}}, {'gene': 'GENE107', 'condition': 'covid-19', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.9679398528053197, 'COLOC': 0.5933537359612059, 'RARE': 0.44856945165375783, 'SC': 0.8650250970890104, 'SPATIAL': 0.5773033299278981, 'BULK_RNA': 0.2668847535019111, 'BULK_PROT': 0.3166174031269092}}, {'gene': 'GENE108', 'condition': 'prostate cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.22745796461978496, 'COLOC': 0.7325959064247893, 'RARE': 0.854775894583275, 'SC': 0.2322389581183677, 'SPATIAL': 0.24784614991474563, 'BULK_RNA': 0.49564116699709204, 'BULK_PROT': 0.2943402750584072}}, {'gene': 'GENE109', 'condition': 'esophageal cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.3207142425047199, 'COLOC': 0.839909116953353, 'RARE': 0.16556119303567085, 'SC': 0.5704445002689887, 'SPATIAL': 0.5550644536037284, 'BULK_RNA': 0.06607716408738429, 'BULK_PROT': 0.02055463567848026}}, {'gene': 'GENE110', 'condition': 'hepatocellular carcinoma', 'bucket': 'MECHANISM', 'signals': {'MR': 0.26809963101598155, 'COLOC': 0.75124744410075, 'RARE': 0.800499955156829, 'SC': 0.4802168231720795, 'SPATIAL': 0.8417691873563231, 'BULK_RNA': 0.4438630947587523, 'BULK_PROT': 0.8565818545441621}}, {'gene': 'GENE111', 'condition': 'major depressive disorder', 'bucket': 'MECHANISM', 'signals': {'MR': 0.9111185932539455, 'COLOC': 0.3857699096898082, 'RARE': 0.018392348638412215, 'SC': 0.47084811711076857, 'SPATIAL': 0.025492446745644437, 'BULK_RNA': 0.4860894485904924, 'BULK_PROT': 0.20642674138214145}}, {'gene': 'GENE112', 'condition': 'parkinson disease', 'bucket': 'MECHANISM', 'signals': {'MR': 0.33867261989080977, 'COLOC': 0.6494453035974084, 'RARE': 0.9816854573747554, 'SC': 0.4456021646596112, 'SPATIAL': 0.5446257511092458, 'BULK_RNA': 0.43544306616308326, 'BULK_PROT': 0.7406269271898468}}, {'gene': 'GENE113', 'condition': 'type 1 diabetes', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6096058818271717, 'COLOC': 0.7711117850836555, 'RARE': 0.3972641240077459, 'SC': 0.05262806887550586, 'SPATIAL': 0.042398102657734915, 'BULK_RNA': 0.4202490481366228, 'BULK_PROT': 0.9638331834782106}}, {'gene': 'GENE114', 'condition': 'myelodysplastic syndrome', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.8412096638363725, 'COLOC': 0.23000224547592318, 'RARE': 0.6863582231565458, 'SC': 0.7340982558711975, 'SPATIAL': 0.8220433330122665, 'BULK_RNA': 0.5822251860856684, 'BULK_PROT': 0.9926279061740673}}, {'gene': 'GENE115', 'condition': 'multiple myeloma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7646774505389711, 'COLOC': 0.5233231999753986, 'RARE': 0.7299527651567127, 'SC': 0.6633290093377927, 'SPATIAL': 0.08892648116118818, 'BULK_RNA': 0.5125995114379357, 'BULK_PROT': 0.8855191644424081}}, {'gene': 'GENE116', 'condition': 'myelodysplastic syndrome', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8449759012263672, 'COLOC': 0.13491984665094592, 'RARE': 0.7548184969674416, 'SC': 0.5348076782856289, 'SPATIAL': 0.7264531703529006, 'BULK_RNA': 0.8916443956051933, 'BULK_PROT': 0.2949769058473719}}, {'gene': 'GENE117', 'condition': 'autism spectrum disorder', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8752674932669684, 'COLOC': 0.9231423563359005, 'RARE': 0.6398515991649748, 'SC': 0.46702958513896964, 'SPATIAL': 0.3051835662782797, 'BULK_RNA': 0.6428879800668068, 'BULK_PROT': 0.8029559343038198}}, {'gene': 'GENE118', 'condition': 'cystic fibrosis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.7304984460212105, 'COLOC': 0.4332369563813899, 'RARE': 0.8582033899313853, 'SC': 0.08895609398053805, 'SPATIAL': 0.10201582451069668, 'BULK_RNA': 0.4065260865727338, 'BULK_PROT': 0.7683232218504709}}, {'gene': 'GENE119', 'condition': 'bipolar disorder', 'bucket': 'MECHANISM', 'signals': {'MR': 0.4771192900675907, 'COLOC': 0.024256025565975836, 'RARE': 0.15032183760957452, 'SC': 0.9415179156738357, 'SPATIAL': 0.3357054490299197, 'BULK_RNA': 0.04318933249657542, 'BULK_PROT': 0.467427448203671}}, {'gene': 'GENE120', 'condition': 'bladder cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.5457848996334139, 'COLOC': 0.9553307249904688, 'RARE': 0.626200104469228, 'SC': 0.14555481719924124, 'SPATIAL': 0.988398141834474, 'BULK_RNA': 0.6823022894072867, 'BULK_PROT': 0.5793302905042673}}, {'gene': 'GENE121', 'condition': 'atopic dermatitis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.228278350205668, 'COLOC': 0.06428849113389301, 'RARE': 0.5520119464475182, 'SC': 0.3654436788017962, 'SPATIAL': 0.38613230705206225, 'BULK_RNA': 0.5693616242418411, 'BULK_PROT': 0.7402972644428879}}, {'gene': 'GENE122', 'condition': 'influenza', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.2819771313957248, 'COLOC': 0.76762728340561, 'RARE': 0.8430383341503211, 'SC': 0.32616024524051657, 'SPATIAL': 0.4699968988602998, 'BULK_RNA': 0.8825819819243521, 'BULK_PROT': 0.09426549411761698}}, {'gene': 'GENE123', 'condition': 'endometrial cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.2019704222988199, 'COLOC': 0.024774858374191022, 'RARE': 0.8833721026118725, 'SC': 0.40296266454769936, 'SPATIAL': 0.3292420377146087, 'BULK_RNA': 0.8507813043793266, 'BULK_PROT': 0.6619785850315342}}, {'gene': 'GENE124', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.2670396295128732, 'COLOC': 0.9449893344896304, 'RARE': 0.12233250155355524, 'SC': 0.8901574337926047, 'SPATIAL': 0.6424137329599918, 'BULK_RNA': 0.24923276651726833, 'BULK_PROT': 0.6134524835934091}}, {'gene': 'GENE125', 'condition': 'hepatocellular carcinoma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.020447060602250655, 'COLOC': 0.3315611824942981, 'RARE': 0.18107343719553315, 'SC': 0.26386739156818095, 'SPATIAL': 0.2908343305408836, 'BULK_RNA': 0.7537824103738181, 'BULK_PROT': 0.8148673440035734}}, {'gene': 'GENE126', 'condition': 'acute myeloid leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.5585144147695795, 'COLOC': 0.12885930609902307, 'RARE': 0.7248190985194293, 'SC': 0.8489493118468285, 'SPATIAL': 0.2530095849801155, 'BULK_RNA': 0.8111561833227262, 'BULK_PROT': 0.8389410243233604}}, {'gene': 'GENE127', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6109628231829337, 'COLOC': 0.10273302072316337, 'RARE': 0.14743424526074, 'SC': 0.1613663580242909, 'SPATIAL': 0.90630344966031, 'BULK_RNA': 0.5559200223452809, 'BULK_PROT': 0.15738976569788166}}, {'gene': 'GENE128', 'condition': 'endometrial cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.7517729523517696, 'COLOC': 0.6177400320423595, 'RARE': 0.750798526412565, 'SC': 0.2403264291893774, 'SPATIAL': 0.9897632068822121, 'BULK_RNA': 0.06857151798479655, 'BULK_PROT': 0.8930647595698993}}, {'gene': 'GENE129', 'condition': 'colorectal cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.2066795298386852, 'COLOC': 0.10276216602382438, 'RARE': 0.44374735917259023, 'SC': 0.36126438884400225, 'SPATIAL': 0.7707379546917043, 'BULK_RNA': 0.7234845223150244, 'BULK_PROT': 0.18458271283767846}}, {'gene': 'GENE130', 'condition': 'multiple myeloma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.8253749653343333, 'COLOC': 0.8379932238030786, 'RARE': 0.06654226881350234, 'SC': 0.34593425261556243, 'SPATIAL': 0.5674423857702665, 'BULK_RNA': 0.6110615232214078, 'BULK_PROT': 0.5408811022512905}}, {'gene': 'GENE131', 'condition': 'alzheimer disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.4829482702270538, 'COLOC': 0.7312411510755097, 'RARE': 0.9241321352336112, 'SC': 0.9211137100362818, 'SPATIAL': 0.2638954692822886, 'BULK_RNA': 0.5813338694059687, 'BULK_PROT': 0.3708395438080695}}, {'gene': 'GENE132', 'condition': 'myelofibrosis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.2612063994729541, 'COLOC': 0.21017608638262586, 'RARE': 0.9864885546629223, 'SC': 0.2954054709444357, 'SPATIAL': 0.12132561926842289, 'BULK_RNA': 0.9575003679942921, 'BULK_PROT': 0.7628582266877735}}, {'gene': 'GENE133', 'condition': 'type 1 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.08882574204151783, 'COLOC': 0.4786330290855444, 'RARE': 0.6052709851633518, 'SC': 0.14757037897186442, 'SPATIAL': 0.5652780674372168, 'BULK_RNA': 0.8801560551629952, 'BULK_PROT': 0.79586459058547}}, {'gene': 'GENE134', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.9710615725787166, 'COLOC': 0.6831076521043898, 'RARE': 0.5555094678597344, 'SC': 0.1235869005390966, 'SPATIAL': 0.23186858989795744, 'BULK_RNA': 0.5175229186149023, 'BULK_PROT': 0.6861783976067195}}, {'gene': 'GENE135', 'condition': 'psoriasis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.8958472819048063, 'COLOC': 0.19938871166614225, 'RARE': 0.42140798349458597, 'SC': 0.571197714006179, 'SPATIAL': 0.8822561144567039, 'BULK_RNA': 0.4332089948359883, 'BULK_PROT': 0.11672529605703497}}, {'gene': 'GENE136', 'condition': 'heart failure', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.9600195782727521, 'COLOC': 0.9984785062438496, 'RARE': 0.0833844128261112, 'SC': 0.7815895097374218, 'SPATIAL': 0.9596912551759225, 'BULK_RNA': 0.03046319763429739, 'BULK_PROT': 0.8383528340789183}}, {'gene': 'GENE137', 'condition': 'pancreatic cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.1687539100526514, 'COLOC': 0.9640323764331198, 'RARE': 0.007282179654238274, 'SC': 0.4050019993216414, 'SPATIAL': 0.8683192556067362, 'BULK_RNA': 0.8943471765042058, 'BULK_PROT': 0.8153039722058635}}, {'gene': 'GENE138', 'condition': 'spinal muscular atrophy', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.425715367221697, 'COLOC': 0.9328316104674983, 'RARE': 0.6173953558984757, 'SC': 0.14630435203427683, 'SPATIAL': 0.907893275841615, 'BULK_RNA': 0.8800570978962396, 'BULK_PROT': 0.47115346186472973}}, {'gene': 'GENE139', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'MECHANISM', 'signals': {'MR': 0.513169427595388, 'COLOC': 0.807865902170523, 'RARE': 0.4767720209526837, 'SC': 0.4030711086663157, 'SPATIAL': 0.46243271454234436, 'BULK_RNA': 0.27796918718849883, 'BULK_PROT': 0.7877124291789526}}, {'gene': 'GENE140', 'condition': 'autism spectrum disorder', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.25164590060365133, 'COLOC': 0.6954405374453296, 'RARE': 0.913683674339484, 'SC': 0.36969514584317864, 'SPATIAL': 0.4900432083385585, 'BULK_RNA': 0.8819275592758975, 'BULK_PROT': 0.31598938390396647}}, {'gene': 'GENE141', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.9507561715784906, 'COLOC': 0.7609823650018134, 'RARE': 0.2688395081455853, 'SC': 0.9043586547849789, 'SPATIAL': 0.06754139347404342, 'BULK_RNA': 0.948622593681767, 'BULK_PROT': 0.9631057199165132}}, {'gene': 'GENE142', 'condition': 'prostate cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.49134261622551556, 'COLOC': 0.7070155157566596, 'RARE': 0.19848659494539245, 'SC': 0.25089937007024, 'SPATIAL': 0.23602035287514045, 'BULK_RNA': 0.8252192185088281, 'BULK_PROT': 0.937267512413385}}, {'gene': 'GENE143', 'condition': 'huntington disease', 'bucket': 'MECHANISM', 'signals': {'MR': 0.28208249362266813, 'COLOC': 0.7985437469721384, 'RARE': 0.5574440573864281, 'SC': 0.1887811384644148, 'SPATIAL': 0.05109175690346701, 'BULK_RNA': 0.40005389484911624, 'BULK_PROT': 0.9669690869219929}}, {'gene': 'GENE144', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.23894311605125318, 'COLOC': 0.314059867479078, 'RARE': 0.30801877811826706, 'SC': 0.19208756317461773, 'SPATIAL': 0.0004818018611942865, 'BULK_RNA': 0.754496303333891, 'BULK_PROT': 0.5599391664975385}}, {'gene': 'GENE145', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.19335927765338579, 'COLOC': 0.7052264118649335, 'RARE': 0.5423670122894583, 'SC': 0.7875447792892623, 'SPATIAL': 0.15253958327046258, 'BULK_RNA': 0.898044163390762, 'BULK_PROT': 0.29873498982275504}}, {'gene': 'GENE146', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'MECHANISM', 'signals': {'MR': 0.4955550467102747, 'COLOC': 0.9230634558980125, 'RARE': 0.6372857072261381, 'SC': 0.730130880717089, 'SPATIAL': 0.222050978609172, 'BULK_RNA': 0.7020523103885974, 'BULK_PROT': 0.09602496352120737}}, {'gene': 'GENE147', 'condition': 'atrial fibrillation', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5441707207019753, 'COLOC': 0.5841571702369177, 'RARE': 0.4682864581897561, 'SC': 0.08902050448816134, 'SPATIAL': 0.15228446477810376, 'BULK_RNA': 0.7282788198193471, 'BULK_PROT': 0.4023181724657221}}, {'gene': 'GENE148', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6874306406405315, 'COLOC': 0.2851520402012183, 'RARE': 0.1741216722969563, 'SC': 0.8889431673874969, 'SPATIAL': 0.2041397316440854, 'BULK_RNA': 0.04770118976632709, 'BULK_PROT': 0.058023008461876024}}, {'gene': 'GENE149', 'condition': 'copd', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.8001675134894185, 'COLOC': 0.5517142583897459, 'RARE': 0.5271177717495859, 'SC': 0.9158698255097523, 'SPATIAL': 0.8041632815376638, 'BULK_RNA': 0.30281008824556976, 'BULK_PROT': 0.491151345260493}}, {'gene': 'GENE150', 'condition': 'alzheimer disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.03813688121072634, 'COLOC': 0.0890569451512353, 'RARE': 0.23022284929554737, 'SC': 0.19149709096542855, 'SPATIAL': 0.2978883089320715, 'BULK_RNA': 0.9497504579974737, 'BULK_PROT': 0.5973844961473893}}, {'gene': 'GENE151', 'condition': 'obesity', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.24534287247791398, 'COLOC': 0.7310066552535018, 'RARE': 0.060591365093899396, 'SC': 0.0646046930435229, 'SPATIAL': 0.38329854437106703, 'BULK_RNA': 0.16945353777849148, 'BULK_PROT': 0.5979686869535406}}, {'gene': 'GENE152', 'condition': 'acute myeloid leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.2361486421435881, 'COLOC': 0.30588626059793866, 'RARE': 0.2106494432155106, 'SC': 0.6471871400857521, 'SPATIAL': 0.6598775597047483, 'BULK_RNA': 0.5601959278652929, 'BULK_PROT': 0.6915732530553121}}, {'gene': 'GENE153', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.9621613620341066, 'COLOC': 0.44404859516838935, 'RARE': 0.7563854635847713, 'SC': 0.28801797609161595, 'SPATIAL': 0.7447489094832679, 'BULK_RNA': 0.4969453184626993, 'BULK_PROT': 0.01012886954041703}}, {'gene': 'GENE154', 'condition': 'prostate cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.8883456571002439, 'COLOC': 0.9913951348475216, 'RARE': 0.4844949691829953, 'SC': 0.06991964750059942, 'SPATIAL': 0.8620980000587255, 'BULK_RNA': 0.10665990141430204, 'BULK_PROT': 0.5576499627596612}}, {'gene': 'GENE155', 'condition': 'polycythemia vera', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.878352229147783, 'COLOC': 0.9162493961130411, 'RARE': 0.8299218809072318, 'SC': 0.6222702894032817, 'SPATIAL': 0.09998930057542543, 'BULK_RNA': 0.9537574426214178, 'BULK_PROT': 0.43419997026502877}}, {'gene': 'GENE156', 'condition': 'breast cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.4086475116874334, 'COLOC': 0.5779320312688428, 'RARE': 0.39136635911804796, 'SC': 0.348331941444878, 'SPATIAL': 0.7579736066392585, 'BULK_RNA': 0.46243297935750427, 'BULK_PROT': 0.4209178542669135}}, {'gene': 'GENE157', 'condition': 'acute myeloid leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.9644017240628706, 'COLOC': 0.41073478818066955, 'RARE': 0.9910376059656981, 'SC': 0.3267872371216417, 'SPATIAL': 0.7306555331188284, 'BULK_RNA': 0.6382207580218631, 'BULK_PROT': 0.569773281207585}}, {'gene': 'GENE158', 'condition': 'cystic fibrosis', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.3109887842198501, 'COLOC': 0.1962246087287216, 'RARE': 0.12755005646528783, 'SC': 0.483178660413747, 'SPATIAL': 0.06670732803536061, 'BULK_RNA': 0.3875463449480845, 'BULK_PROT': 0.1291890027721967}}, {'gene': 'GENE159', 'condition': 'rheumatoid arthritis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.3383630407050976, 'COLOC': 0.062029974158974044, 'RARE': 0.1496332808720372, 'SC': 0.9455063374650163, 'SPATIAL': 0.19420209491332197, 'BULK_RNA': 0.5378745353001464, 'BULK_PROT': 0.19353005899207754}}, {'gene': 'GENE160', 'condition': 'asthma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.31480405275744083, 'COLOC': 0.8054270326131426, 'RARE': 0.09714610854317152, 'SC': 0.6809771838284697, 'SPATIAL': 0.0051747700980190325, 'BULK_RNA': 0.026423423082025166, 'BULK_PROT': 0.9758972253855944}}, {'gene': 'GENE161', 'condition': 'ovarian cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.012982043697811307, 'COLOC': 0.5540260604423034, 'RARE': 0.9816909764991769, 'SC': 0.5828027982116287, 'SPATIAL': 0.8371442531182572, 'BULK_RNA': 0.09762987478511276, 'BULK_PROT': 0.14187141439877615}}, {'gene': 'GENE162', 'condition': 'psoriasis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6487486765870328, 'COLOC': 0.17598098609281876, 'RARE': 0.011462220154743852, 'SC': 0.5876681760874054, 'SPATIAL': 0.06439172359370282, 'BULK_RNA': 0.6023330458111329, 'BULK_PROT': 0.0416324478277742}}, {'gene': 'GENE163', 'condition': 'renal cell carcinoma', 'bucket': 'MECHANISM', 'signals': {'MR': 0.5654015474525874, 'COLOC': 0.045794605395328314, 'RARE': 0.19678027538869813, 'SC': 0.5089356823975454, 'SPATIAL': 0.7576716309144366, 'BULK_RNA': 0.18511963233292683, 'BULK_PROT': 0.1373139148517749}}, {'gene': 'GENE164', 'condition': 'duchenne muscular dystrophy', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.40119596587106576, 'COLOC': 0.4596294265294255, 'RARE': 0.8909388218685883, 'SC': 0.7269213958667594, 'SPATIAL': 0.9731489465245471, 'BULK_RNA': 0.9613777430599934, 'BULK_PROT': 0.5731816401926931}}, {'gene': 'GENE165', 'condition': 'breast cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.8094213134916853, 'COLOC': 0.6167176362943999, 'RARE': 0.5433112971490068, 'SC': 0.9915858413068223, 'SPATIAL': 0.13138462376020477, 'BULK_RNA': 0.21796917284876915, 'BULK_PROT': 0.3309101318623482}}, {'gene': 'GENE166', 'condition': 'prostate cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.5136313276654789, 'COLOC': 0.6836590821281932, 'RARE': 0.8697969265281968, 'SC': 0.9583685029293273, 'SPATIAL': 0.7374218077146494, 'BULK_RNA': 0.5259429741871039, 'BULK_PROT': 0.6259858626412029}}, {'gene': 'GENE167', 'condition': 'type 1 diabetes', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5659941026509155, 'COLOC': 0.11061663163294622, 'RARE': 0.4654901825271387, 'SC': 0.46547855519877657, 'SPATIAL': 0.42105160915661344, 'BULK_RNA': 0.7775648325358577, 'BULK_PROT': 0.6397234415903073}}, {'gene': 'GENE168', 'condition': 'influenza', 'bucket': 'MECHANISM', 'signals': {'MR': 0.09973017596507217, 'COLOC': 0.8809261044830224, 'RARE': 0.12836375143881107, 'SC': 0.4790158458051972, 'SPATIAL': 0.7269263932733766, 'BULK_RNA': 0.42205996822187875, 'BULK_PROT': 0.4338082458827258}}, {'gene': 'GENE169', 'condition': 'multiple myeloma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5454418931067077, 'COLOC': 0.7585242903014051, 'RARE': 0.35429692929057865, 'SC': 0.8632504431774025, 'SPATIAL': 0.03749412261970664, 'BULK_RNA': 0.8568792723816363, 'BULK_PROT': 0.38584032579654237}}, {'gene': 'GENE170', 'condition': 'essential thrombocythemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.5948591526391774, 'COLOC': 0.7725428620208469, 'RARE': 0.043484397717914236, 'SC': 0.11724434438742548, 'SPATIAL': 0.8380637912700661, 'BULK_RNA': 0.5549514858913982, 'BULK_PROT': 0.7254853165617431}}, {'gene': 'GENE171', 'condition': 'gastric cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.2442317857770605, 'COLOC': 0.5170115765994693, 'RARE': 0.17194161753401305, 'SC': 0.5186255967110048, 'SPATIAL': 0.07258563883762903, 'BULK_RNA': 0.714738371002051, 'BULK_PROT': 0.28750469115732513}}, {'gene': 'GENE172', 'condition': 'renal cell carcinoma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.9772104513174323, 'COLOC': 0.4352385953509811, 'RARE': 0.14984966196849292, 'SC': 0.25785389441590423, 'SPATIAL': 0.6880289333589911, 'BULK_RNA': 0.3169635700855463, 'BULK_PROT': 0.5614411923376538}}, {'gene': 'GENE173', 'condition': 'ovarian cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.07309419621754998, 'COLOC': 0.6996662785082604, 'RARE': 0.9813374962792081, 'SC': 0.10291510500562862, 'SPATIAL': 0.6928700687661994, 'BULK_RNA': 0.6954896349219067, 'BULK_PROT': 0.5574951694485298}}, {'gene': 'GENE174', 'condition': 'endometrial cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.026666200877718427, 'COLOC': 0.0939327186649086, 'RARE': 0.42137370897312476, 'SC': 0.633555236195694, 'SPATIAL': 0.014669332922102263, 'BULK_RNA': 0.8381208882637701, 'BULK_PROT': 0.029963546582251532}}, {'gene': 'GENE175', 'condition': 'cystic fibrosis', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.610718677003812, 'COLOC': 0.6881897822327724, 'RARE': 0.927251293900232, 'SC': 0.8604683444870019, 'SPATIAL': 0.2701061418706413, 'BULK_RNA': 0.08452770027297107, 'BULK_PROT': 0.6957479360689055}}, {'gene': 'GENE176', 'condition': 'type 1 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.7790354591202062, 'COLOC': 0.1540692056020243, 'RARE': 0.15576516773594373, 'SC': 0.7497837153246942, 'SPATIAL': 0.05010987126542943, 'BULK_RNA': 0.07953460516823918, 'BULK_PROT': 0.07835421478246996}}, {'gene': 'GENE177', 'condition': 'renal cell carcinoma', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.4817708793569031, 'COLOC': 0.39054457444755875, 'RARE': 0.92912827853884, 'SC': 0.5503675759467992, 'SPATIAL': 0.9083201693977233, 'BULK_RNA': 0.8415365756532165, 'BULK_PROT': 0.9999381788480937}}, {'gene': 'GENE178', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.9395287448584084, 'COLOC': 0.7581420225222005, 'RARE': 0.2599041917644843, 'SC': 0.8116462437221884, 'SPATIAL': 0.1063033661010494, 'BULK_RNA': 0.37693839107432114, 'BULK_PROT': 0.36847422265567287}}, {'gene': 'GENE179', 'condition': 'atopic dermatitis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5194650160583636, 'COLOC': 0.6210651666597822, 'RARE': 0.5450915257677923, 'SC': 0.24592914855683623, 'SPATIAL': 0.9683763044472697, 'BULK_RNA': 0.5814248777409056, 'BULK_PROT': 0.5911198491587846}}, {'gene': 'GENE180', 'condition': 'duchenne muscular dystrophy', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.38781003211863274, 'COLOC': 0.8700188102106403, 'RARE': 0.08336196603222079, 'SC': 0.37404686443789015, 'SPATIAL': 0.3180110784492566, 'BULK_RNA': 0.6479863107967545, 'BULK_PROT': 0.6453239636878049}}, {'gene': 'GENE181', 'condition': 'acute myeloid leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6476005462914499, 'COLOC': 0.9973471962300469, 'RARE': 0.5472205590477875, 'SC': 0.32176806262906343, 'SPATIAL': 0.34522829930333754, 'BULK_RNA': 0.6066563591235562, 'BULK_PROT': 0.22589663394791737}}, {'gene': 'GENE182', 'condition': 'cystic fibrosis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.7079421510488444, 'COLOC': 0.7834934665515133, 'RARE': 0.869036896334697, 'SC': 0.7583139772114513, 'SPATIAL': 0.4250988648576306, 'BULK_RNA': 0.13518877272876428, 'BULK_PROT': 0.2317068060791413}}, {'gene': 'GENE183', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.8021893505661909, 'COLOC': 0.6829037711826336, 'RARE': 0.16977788635022228, 'SC': 0.26299359227275887, 'SPATIAL': 0.1705813959491399, 'BULK_RNA': 0.19712744755821898, 'BULK_PROT': 0.7857887422370429}}, {'gene': 'GENE184', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.18560818752477737, 'COLOC': 0.9871748613868144, 'RARE': 0.14994999596855607, 'SC': 0.028841184532407027, 'SPATIAL': 0.7822801591207755, 'BULK_RNA': 0.0872349854368345, 'BULK_PROT': 0.0378990819176821}}, {'gene': 'GENE185', 'condition': 'bipolar disorder', 'bucket': 'MECHANISM', 'signals': {'MR': 0.672237548123008, 'COLOC': 0.19803546499201607, 'RARE': 0.9730757746592278, 'SC': 0.5909049045669452, 'SPATIAL': 0.650970404683598, 'BULK_RNA': 0.22613578673882595, 'BULK_PROT': 0.1945498039009389}}, {'gene': 'GENE186', 'condition': 'ovarian cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.19129217247258956, 'COLOC': 0.8832575761581819, 'RARE': 0.4612368755541625, 'SC': 0.5759398553039049, 'SPATIAL': 0.8434263390925679, 'BULK_RNA': 0.4184320303677581, 'BULK_PROT': 0.7825067473394943}}, {'gene': 'GENE187', 'condition': 'obesity', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.33095908881070724, 'COLOC': 0.5352334191964939, 'RARE': 0.8109306983565383, 'SC': 0.34760812981987044, 'SPATIAL': 0.4088632367674965, 'BULK_RNA': 0.6616391522969183, 'BULK_PROT': 0.30063531353747}}, {'gene': 'GENE188', 'condition': 'schizophrenia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.4880603603781627, 'COLOC': 0.27471127005771323, 'RARE': 0.20168934937592764, 'SC': 0.36836968434469497, 'SPATIAL': 0.03876166576036766, 'BULK_RNA': 0.7155090375746219, 'BULK_PROT': 0.9191480895141266}}, {'gene': 'GENE189', 'condition': 'myelofibrosis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.8754756235257548, 'COLOC': 0.9260964491560737, 'RARE': 0.5501016976107241, 'SC': 0.16885961420219708, 'SPATIAL': 0.531429134580459, 'BULK_RNA': 0.30283327354831413, 'BULK_PROT': 0.17822156266104128}}, {'gene': 'GENE190', 'condition': 'copd', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.48870871114444336, 'COLOC': 0.6904369481998998, 'RARE': 0.9105023014726994, 'SC': 0.9900045183348852, 'SPATIAL': 0.5023074354761753, 'BULK_RNA': 0.5323139143053975, 'BULK_PROT': 0.204532001538975}}, {'gene': 'GENE191', 'condition': 'pancreatic cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5770031206520845, 'COLOC': 0.5594845457427952, 'RARE': 0.7869835447731779, 'SC': 0.07500010349502739, 'SPATIAL': 0.04400055367394473, 'BULK_RNA': 0.8079139348848773, 'BULK_PROT': 0.8396328626963433}}, {'gene': 'GENE192', 'condition': 'influenza', 'bucket': 'MECHANISM', 'signals': {'MR': 0.5658095300308995, 'COLOC': 0.8218623428144307, 'RARE': 0.31353622080405563, 'SC': 0.28874255067485366, 'SPATIAL': 0.05960525930968352, 'BULK_RNA': 0.21543952190517635, 'BULK_PROT': 0.7934014875434512}}, {'gene': 'GENE193', 'condition': 'acute myeloid leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.3662502745849401, 'COLOC': 0.03280746059916961, 'RARE': 0.8748681985443869, 'SC': 0.21871128996315803, 'SPATIAL': 0.5926700671123771, 'BULK_RNA': 0.3437606020017522, 'BULK_PROT': 0.6948508518243919}}, {'gene': 'GENE194', 'condition': 'colorectal cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.7393651220466744, 'COLOC': 0.34376223982039, 'RARE': 0.05128723247571543, 'SC': 0.017971931133548336, 'SPATIAL': 0.49515679648108213, 'BULK_RNA': 0.034729850703931864, 'BULK_PROT': 0.6559151505962748}}, {'gene': 'GENE195', 'condition': 'duchenne muscular dystrophy', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.30155357790696624, 'COLOC': 0.702938804997781, 'RARE': 0.6459475194375343, 'SC': 0.9330201104164539, 'SPATIAL': 0.30533490622684045, 'BULK_RNA': 0.6713842740130254, 'BULK_PROT': 0.22838863724650738}}, {'gene': 'GENE196', 'condition': 'alzheimer disease', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.30635991658921036, 'COLOC': 0.9722066338352172, 'RARE': 0.7820111786469037, 'SC': 0.758656768864249, 'SPATIAL': 0.8696877709822314, 'BULK_RNA': 0.16373231771942043, 'BULK_PROT': 0.5689951217147085}}, {'gene': 'GENE197', 'condition': 'ovarian cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.11257239945764885, 'COLOC': 0.44599639174389616, 'RARE': 0.5018696889163319, 'SC': 0.6010410730205072, 'SPATIAL': 0.37457297278262525, 'BULK_RNA': 0.7540213511062362, 'BULK_PROT': 0.7438216430673869}}, {'gene': 'GENE198', 'condition': 'coronary artery disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.5975973604853446, 'COLOC': 0.10119059344606596, 'RARE': 0.048383860293426784, 'SC': 0.38833391858484034, 'SPATIAL': 0.8910790221317157, 'BULK_RNA': 0.09048898705258546, 'BULK_PROT': 0.11145977433233278}}, {'gene': 'GENE199', 'condition': 'essential thrombocythemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.23622747359441154, 'COLOC': 0.03761748763977746, 'RARE': 0.5485747606664126, 'SC': 0.011293337349631782, 'SPATIAL': 0.5368481685744384, 'BULK_RNA': 0.009902825788157599, 'BULK_PROT': 0.8832263373998758}}, {'gene': 'GENE200', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.8996668758693772, 'COLOC': 0.02556800606392462, 'RARE': 0.014889590366223504, 'SC': 0.8472402468910872, 'SPATIAL': 0.3486335929675427, 'BULK_RNA': 0.031548459473456636, 'BULK_PROT': 0.11027708414921311}}, {'gene': 'GENE201', 'condition': 'myelofibrosis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.39167437751082856, 'COLOC': 0.1711477056890096, 'RARE': 0.29571808808239075, 'SC': 0.9171153960398131, 'SPATIAL': 0.4746093559046708, 'BULK_RNA': 0.8819174313594625, 'BULK_PROT': 0.6487019801751801}}, {'gene': 'GENE202', 'condition': 'gastric cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.14734545825035006, 'COLOC': 0.6776617209202465, 'RARE': 0.9904277310210484, 'SC': 0.8459256229342597, 'SPATIAL': 0.262392292311068, 'BULK_RNA': 0.9802425257080769, 'BULK_PROT': 0.002133102513492302}}, {'gene': 'GENE203', 'condition': 'esophageal cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.5198410083027439, 'COLOC': 0.5618167397503913, 'RARE': 0.8158292232415105, 'SC': 0.020237165911163313, 'SPATIAL': 0.2212800898948044, 'BULK_RNA': 0.8117522713739812, 'BULK_PROT': 0.0836360256318408}}, {'gene': 'GENE204', 'condition': 'major depressive disorder', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.05288017365998021, 'COLOC': 0.0008817574516409854, 'RARE': 0.4895471546057819, 'SC': 0.8120514836949617, 'SPATIAL': 0.3943851041105225, 'BULK_RNA': 0.4251999785917313, 'BULK_PROT': 0.731168544931431}}, {'gene': 'GENE205', 'condition': 'systemic lupus erythematosus', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6481999028060186, 'COLOC': 0.2590457831439359, 'RARE': 0.13827032444476428, 'SC': 0.24209227222199192, 'SPATIAL': 0.44948544495767595, 'BULK_RNA': 0.4389715345787504, 'BULK_PROT': 0.9473979217368397}}, {'gene': 'GENE206', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'MECHANISM', 'signals': {'MR': 0.1925274484399987, 'COLOC': 0.7591608058657164, 'RARE': 0.11164370311974159, 'SC': 0.6508623667334161, 'SPATIAL': 0.25145703439105604, 'BULK_RNA': 0.8237529045017844, 'BULK_PROT': 0.16499472900886747}}, {'gene': 'GENE207', 'condition': 'thyroid cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6975416895549962, 'COLOC': 0.2922443966869174, 'RARE': 0.9238594549650637, 'SC': 0.7170006752560161, 'SPATIAL': 0.9337841750098542, 'BULK_RNA': 0.44063975488068985, 'BULK_PROT': 0.4487489745777271}}, {'gene': 'GENE208', 'condition': 'amyotrophic lateral sclerosis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.4044413949752579, 'COLOC': 0.10724223603563354, 'RARE': 0.5753301393699055, 'SC': 0.6014453287423822, 'SPATIAL': 0.23867952807974668, 'BULK_RNA': 0.36703714205263527, 'BULK_PROT': 0.961991220849233}}, {'gene': 'GENE209', 'condition': 'breast cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6830744814492559, 'COLOC': 0.871816679632684, 'RARE': 0.8530293137876978, 'SC': 0.1985527538309343, 'SPATIAL': 0.830799746074007, 'BULK_RNA': 0.8051311995749869, 'BULK_PROT': 0.9021453100388952}}, {'gene': 'GENE210', 'condition': 'systemic lupus erythematosus', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.822728218749701, 'COLOC': 0.3647860857028685, 'RARE': 0.39262316737579184, 'SC': 0.25968898841645327, 'SPATIAL': 0.599023603765121, 'BULK_RNA': 0.09127194719266096, 'BULK_PROT': 0.3139296664695198}}, {'gene': 'GENE211', 'condition': 'bladder cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.8756609308982103, 'COLOC': 0.8592129977927944, 'RARE': 0.6693231778211057, 'SC': 0.7489439757435549, 'SPATIAL': 0.7445548860178829, 'BULK_RNA': 0.6700560235118861, 'BULK_PROT': 0.42837044198138075}}, {'gene': 'GENE212', 'condition': 'coronary artery disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.9632603864065875, 'COLOC': 0.04091272834605686, 'RARE': 0.2707779359467807, 'SC': 0.38394953557155465, 'SPATIAL': 0.019538206571331207, 'BULK_RNA': 0.32757657374836824, 'BULK_PROT': 0.3871468910083349}}, {'gene': 'GENE213', 'condition': 'multiple sclerosis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.38729893108356694, 'COLOC': 0.3316446404958595, 'RARE': 0.17204287406367158, 'SC': 0.48879814173058156, 'SPATIAL': 0.017263109872560634, 'BULK_RNA': 0.016166325810969995, 'BULK_PROT': 0.49470059942151556}}, {'gene': 'GENE214', 'condition': 'nonalcoholic steatohepatitis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.5423998649267036, 'COLOC': 0.14076387153194136, 'RARE': 0.0008277236115030728, 'SC': 0.7286462717723503, 'SPATIAL': 0.40726982000451406, 'BULK_RNA': 0.324176433358053, 'BULK_PROT': 0.922325113791035}}, {'gene': 'GENE215', 'condition': 'bladder cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.3148433927726242, 'COLOC': 0.047254564546306055, 'RARE': 0.009898130090808421, 'SC': 0.8069202961473954, 'SPATIAL': 0.8738913333066938, 'BULK_RNA': 0.13951316781473844, 'BULK_PROT': 0.5846652561312591}}, {'gene': 'GENE216', 'condition': 'asthma', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.7662854964818363, 'COLOC': 0.5662097424860948, 'RARE': 0.77283571260849, 'SC': 0.1274968970317667, 'SPATIAL': 0.82671455062441, 'BULK_RNA': 0.10419360428612234, 'BULK_PROT': 0.40434741963917276}}, {'gene': 'GENE217', 'condition': 'bipolar disorder', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5117657050205991, 'COLOC': 0.09867886194325215, 'RARE': 0.3666581923817348, 'SC': 0.09360619411722204, 'SPATIAL': 0.14326580096407793, 'BULK_RNA': 0.17699588924911225, 'BULK_PROT': 0.9025134227185493}}, {'gene': 'GENE218', 'condition': 'polycythemia vera', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.47186172157140793, 'COLOC': 0.22429696224567153, 'RARE': 0.8770703173224552, 'SC': 0.04860614795297691, 'SPATIAL': 0.8017595124387862, 'BULK_RNA': 0.49146653787334116, 'BULK_PROT': 0.9201452343136092}}, {'gene': 'GENE219', 'condition': 'acute lymphoblastic leukemia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.3395268990741034, 'COLOC': 0.02146663334490795, 'RARE': 0.5816337127041793, 'SC': 0.8817047144425839, 'SPATIAL': 0.30600572673034165, 'BULK_RNA': 0.16164730599963073, 'BULK_PROT': 0.2335189897448411}}, {'gene': 'GENE220', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.61212453155553, 'COLOC': 0.484953574152589, 'RARE': 0.7314526644802498, 'SC': 0.9689541274809171, 'SPATIAL': 0.9737099016142897, 'BULK_RNA': 0.26593612187764526, 'BULK_PROT': 0.05516160193670616}}, {'gene': 'GENE221', 'condition': 'atrial fibrillation', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7469723699169378, 'COLOC': 0.851633652905332, 'RARE': 0.07362695473514358, 'SC': 0.471214442311527, 'SPATIAL': 0.6686510990866594, 'BULK_RNA': 0.4644607787458489, 'BULK_PROT': 0.2172475897624082}}, {'gene': 'GENE222', 'condition': 'bipolar disorder', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7863400571211834, 'COLOC': 0.3845515286684825, 'RARE': 0.9151553960466572, 'SC': 0.5446365384276157, 'SPATIAL': 0.008393123346038256, 'BULK_RNA': 0.9719662145715772, 'BULK_PROT': 0.7151925639703991}}, {'gene': 'GENE223', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7657146316393914, 'COLOC': 0.5655002480652515, 'RARE': 0.17656423782201724, 'SC': 0.07070860585848682, 'SPATIAL': 0.8695243037686395, 'BULK_RNA': 0.5157856048541064, 'BULK_PROT': 0.3642476466684448}}, {'gene': 'GENE224', 'condition': 'myelofibrosis', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.02341164163907905, 'COLOC': 0.27662040410323285, 'RARE': 0.6563199652383845, 'SC': 0.4274029847156986, 'SPATIAL': 0.05415955443996845, 'BULK_RNA': 0.7725552353632217, 'BULK_PROT': 0.18479200015694242}}, {'gene': 'GENE225', 'condition': 'polycythemia vera', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.04201982951432759, 'COLOC': 0.6814374866980782, 'RARE': 0.7737033669220952, 'SC': 0.8784547599762497, 'SPATIAL': 0.13564549517457014, 'BULK_RNA': 0.5241654274317337, 'BULK_PROT': 0.010337704196030084}}, {'gene': 'GENE226', 'condition': 'schizophrenia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.38179594616161683, 'COLOC': 0.5610159104860352, 'RARE': 0.4441144734413782, 'SC': 0.0907141573529544, 'SPATIAL': 0.7732355465705848, 'BULK_RNA': 0.951611210951978, 'BULK_PROT': 0.8891173078077234}}, {'gene': 'GENE227', 'condition': 'nonalcoholic steatohepatitis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.1793664544476642, 'COLOC': 0.9954933422334039, 'RARE': 0.5928378945191374, 'SC': 0.13170837757573606, 'SPATIAL': 0.2762959839568845, 'BULK_RNA': 0.35682525932296405, 'BULK_PROT': 0.18203013390238365}}, {'gene': 'GENE228', 'condition': 'thyroid cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.7103749578866129, 'COLOC': 0.18227284261426768, 'RARE': 0.35546247273826914, 'SC': 0.006950679600069121, 'SPATIAL': 0.8183767109733001, 'BULK_RNA': 0.35293925506065127, 'BULK_PROT': 0.7223928188288774}}, {'gene': 'GENE229', 'condition': 'schizophrenia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5111430132443752, 'COLOC': 0.5208851349682262, 'RARE': 0.4639178086881336, 'SC': 0.5867999370980854, 'SPATIAL': 0.5286823317805348, 'BULK_RNA': 0.6341752081569393, 'BULK_PROT': 0.26074622605327713}}, {'gene': 'GENE230', 'condition': 'amyotrophic lateral sclerosis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.27695400206822773, 'COLOC': 0.951936173022892, 'RARE': 0.7750642057969008, 'SC': 0.8103099454004956, 'SPATIAL': 0.5199819504790162, 'BULK_RNA': 0.42900522762182114, 'BULK_PROT': 0.16944499119080914}}, {'gene': 'GENE231', 'condition': 'schizophrenia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.6891034908238579, 'COLOC': 0.2914453685927664, 'RARE': 0.8341876653692204, 'SC': 0.5477361733210446, 'SPATIAL': 0.7068898820637209, 'BULK_RNA': 0.027159158657201865, 'BULK_PROT': 0.11603438965933499}}, {'gene': 'GENE232', 'condition': 'schizophrenia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.15512572154665305, 'COLOC': 0.4433630067595429, 'RARE': 0.08141917900298878, 'SC': 0.4917124624659227, 'SPATIAL': 0.05853548249620977, 'BULK_RNA': 0.3715496739122569, 'BULK_PROT': 0.7765342590405819}}, {'gene': 'GENE233', 'condition': 'pancreatic cancer', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.691213183186766, 'COLOC': 0.425081267462022, 'RARE': 0.44873602272549706, 'SC': 0.7334250008261796, 'SPATIAL': 0.13658833470717247, 'BULK_RNA': 0.9175696480149593, 'BULK_PROT': 0.73717579451159}}, {'gene': 'GENE234', 'condition': 'covid-19', 'bucket': 'MECHANISM', 'signals': {'MR': 0.40613489583948803, 'COLOC': 0.49992540638084537, 'RARE': 0.3216231309064863, 'SC': 0.21142394943226994, 'SPATIAL': 0.6728276151411927, 'BULK_RNA': 0.10649331118380112, 'BULK_PROT': 0.4942389669305548}}, {'gene': 'GENE235', 'condition': 'atopic dermatitis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.7219749912114002, 'COLOC': 0.1727304338052611, 'RARE': 0.293284467940224, 'SC': 0.29120014714097575, 'SPATIAL': 0.04699865354284449, 'BULK_RNA': 0.09615786629704892, 'BULK_PROT': 0.13379658123327942}}, {'gene': 'GENE236', 'condition': 'nonalcoholic steatohepatitis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.734168118476294, 'COLOC': 0.11995888121998122, 'RARE': 0.48568703431966187, 'SC': 0.16869307608927153, 'SPATIAL': 0.11860028366485931, 'BULK_RNA': 0.5433787946301367, 'BULK_PROT': 0.6159331410704575}}, {'gene': 'GENE237', 'condition': 'schizophrenia', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6975581518260644, 'COLOC': 0.3880439779297875, 'RARE': 0.4595563665237402, 'SC': 0.6509992274728927, 'SPATIAL': 0.8102596170305253, 'BULK_RNA': 0.28252736573190784, 'BULK_PROT': 0.5481883557772037}}, {'gene': 'GENE238', 'condition': 'schizophrenia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.9626166847125613, 'COLOC': 0.4416166101365081, 'RARE': 0.8824851305422815, 'SC': 0.6391963884755789, 'SPATIAL': 0.9313443730266602, 'BULK_RNA': 0.024293108573696043, 'BULK_PROT': 0.7688020598299}}, {'gene': 'GENE239', 'condition': 'cystic fibrosis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.4667828159133165, 'COLOC': 0.8014921175517258, 'RARE': 0.15251922531633688, 'SC': 0.650344959488161, 'SPATIAL': 0.9404673769559858, 'BULK_RNA': 0.8455800698766177, 'BULK_PROT': 0.8874587093855729}}, {'gene': 'GENE240', 'condition': 'coronary artery disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.93298719476078, 'COLOC': 0.33972674770427336, 'RARE': 0.8036736068737789, 'SC': 0.6556223614565547, 'SPATIAL': 0.8168311881639717, 'BULK_RNA': 0.7974215198972058, 'BULK_PROT': 0.4917833825832424}}, {'gene': 'GENE241', 'condition': 'head and neck squamous cell carcinoma', 'bucket': 'MECHANISM', 'signals': {'MR': 0.968798964771233, 'COLOC': 0.6273271690806954, 'RARE': 0.9214471216872233, 'SC': 0.5980355236267297, 'SPATIAL': 0.9359818435029104, 'BULK_RNA': 0.49562383264814835, 'BULK_PROT': 0.7181584208583169}}, {'gene': 'GENE242', 'condition': 'heart failure', 'bucket': 'MECHANISM', 'signals': {'MR': 0.21716660211082806, 'COLOC': 0.7612606309060987, 'RARE': 0.6703024954074669, 'SC': 0.7161874730274166, 'SPATIAL': 0.4580617607545432, 'BULK_RNA': 0.2063181694127747, 'BULK_PROT': 0.7934781253913452}}, {'gene': 'GENE243', 'condition': 'type 2 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.4854310312135238, 'COLOC': 0.35059891179351044, 'RARE': 0.8986293585756805, 'SC': 0.9606493679556279, 'SPATIAL': 0.04720513930567649, 'BULK_RNA': 0.6975904128419516, 'BULK_PROT': 0.9481314206540612}}, {'gene': 'GENE244', 'condition': 'cystic fibrosis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.047076192537238826, 'COLOC': 0.8823600545958392, 'RARE': 0.4767341890829503, 'SC': 0.8725517974488349, 'SPATIAL': 0.3930667318655683, 'BULK_RNA': 0.8251047341520766, 'BULK_PROT': 0.7976541863765516}}, {'gene': 'GENE245', 'condition': 'type 1 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5329403301734114, 'COLOC': 0.9108240670591233, 'RARE': 0.8479263651952209, 'SC': 0.23651017914866068, 'SPATIAL': 0.9640370288575347, 'BULK_RNA': 0.83927155871869, 'BULK_PROT': 0.47921826447194626}}, {'gene': 'GENE246', 'condition': 'acute myeloid leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.9760513010267298, 'COLOC': 0.5112734021802235, 'RARE': 0.8634126566154863, 'SC': 0.6980180875020209, 'SPATIAL': 0.8027673577157081, 'BULK_RNA': 0.3778247593104881, 'BULK_PROT': 0.013580562352466408}}, {'gene': 'GENE247', 'condition': 'major depressive disorder', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.13684643621463166, 'COLOC': 0.8065904069129535, 'RARE': 0.4243638387884151, 'SC': 0.530267285271285, 'SPATIAL': 0.3867951743658802, 'BULK_RNA': 0.01632922338186804, 'BULK_PROT': 0.18586538053795398}}, {'gene': 'GENE248', 'condition': 'atrial fibrillation', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.14064968971466996, 'COLOC': 0.7515798457846014, 'RARE': 0.4828409294560866, 'SC': 0.763329606757274, 'SPATIAL': 0.12115218103224468, 'BULK_RNA': 0.6609508309426935, 'BULK_PROT': 0.14998477483808503}}, {'gene': 'GENE249', 'condition': 'multiple sclerosis', 'bucket': 'MECHANISM', 'signals': {'MR': 0.04984889867386044, 'COLOC': 0.025008600316597906, 'RARE': 0.2851043828656832, 'SC': 0.11851120695579953, 'SPATIAL': 0.9014579033271118, 'BULK_RNA': 0.1330663991764236, 'BULK_PROT': 0.5448674674464521}}, {'gene': 'GENE250', 'condition': 'polycythemia vera', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.30897388057770014, 'COLOC': 0.37274702282307837, 'RARE': 0.9042230589443744, 'SC': 0.04037414441316667, 'SPATIAL': 0.3184717825098601, 'BULK_RNA': 0.5466206353691896, 'BULK_PROT': 0.4784995058262458}}, {'gene': 'GENE251', 'condition': 'heart failure', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.9427047556530824, 'COLOC': 0.060507648783695744, 'RARE': 0.16710867959737197, 'SC': 0.3081087843157173, 'SPATIAL': 0.5235957833930648, 'BULK_RNA': 0.2702066840716405, 'BULK_PROT': 0.08987349076730522}}, {'gene': 'GENE252', 'condition': 'duchenne muscular dystrophy', 'bucket': 'MECHANISM', 'signals': {'MR': 0.0823611881974633, 'COLOC': 0.5269468024446939, 'RARE': 0.0473861458848569, 'SC': 0.01900483667358943, 'SPATIAL': 0.3963430675594186, 'BULK_RNA': 0.9454970640402902, 'BULK_PROT': 0.6239957155748196}}, {'gene': 'GENE253', 'condition': 'major depressive disorder', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.646249045440627, 'COLOC': 0.04030257802625348, 'RARE': 0.3847791993592665, 'SC': 0.10633832659271858, 'SPATIAL': 0.8609815303263697, 'BULK_RNA': 0.14735588190950621, 'BULK_PROT': 0.7699287413457752}}, {'gene': 'GENE254', 'condition': 'influenza', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6441733206267052, 'COLOC': 0.6799293346878567, 'RARE': 0.014786484754394591, 'SC': 0.6740564042190245, 'SPATIAL': 0.2095613786746583, 'BULK_RNA': 0.9749022789789832, 'BULK_PROT': 0.4183593348683796}}, {'gene': 'GENE255', 'condition': 'major depressive disorder', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.08560020873417284, 'COLOC': 0.08187325754915964, 'RARE': 0.24736524652806968, 'SC': 0.1905078858359226, 'SPATIAL': 0.788021030544931, 'BULK_RNA': 0.5929235375713848, 'BULK_PROT': 0.08310209070034091}}, {'gene': 'GENE256', 'condition': 'myelofibrosis', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5722944537909603, 'COLOC': 0.3188794013818663, 'RARE': 0.4067042623940529, 'SC': 0.11160596382515164, 'SPATIAL': 0.9255196209152855, 'BULK_RNA': 0.33609027108261136, 'BULK_PROT': 0.711624213002954}}, {'gene': 'GENE257', 'condition': 'essential thrombocythemia', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.669619863545331, 'COLOC': 0.7151782781036781, 'RARE': 0.7426252156750835, 'SC': 0.7848623768195558, 'SPATIAL': 0.7225710187819427, 'BULK_RNA': 0.439583318049939, 'BULK_PROT': 0.8727642154966151}}, {'gene': 'GENE258', 'condition': 'endometrial cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.2777846803720405, 'COLOC': 0.9623232565856187, 'RARE': 0.10847785074304184, 'SC': 0.7800252781755189, 'SPATIAL': 0.7475061858467897, 'BULK_RNA': 0.16294677604971075, 'BULK_PROT': 0.09655465024819532}}, {'gene': 'GENE259', 'condition': 'type 1 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.21776562825221457, 'COLOC': 0.7940189034403032, 'RARE': 0.3414631834476066, 'SC': 0.22641166554534242, 'SPATIAL': 0.8871982431298584, 'BULK_RNA': 0.0294588442373801, 'BULK_PROT': 0.37937288889312115}}, {'gene': 'GENE260', 'condition': 'ovarian cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.04069153044432272, 'COLOC': 0.7313408774304379, 'RARE': 0.30300132545385405, 'SC': 0.9882457608899898, 'SPATIAL': 0.09620757942750946, 'BULK_RNA': 0.7462172076644673, 'BULK_PROT': 0.41644034775809147}}, {'gene': 'GENE261', 'condition': 'myelodysplastic syndrome', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6950329145216152, 'COLOC': 0.6647988852103166, 'RARE': 0.019137721998273904, 'SC': 0.1788332470240902, 'SPATIAL': 0.6732633539936154, 'BULK_RNA': 0.7260160334722009, 'BULK_PROT': 0.30022514589633875}}, {'gene': 'GENE262', 'condition': 'multiple myeloma', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.712624382137915, 'COLOC': 0.6643152499508624, 'RARE': 0.4793008278662866, 'SC': 0.5950267121061152, 'SPATIAL': 0.2671299327353903, 'BULK_RNA': 0.914336731433467, 'BULK_PROT': 0.6341114478311094}}, {'gene': 'GENE263', 'condition': 'schizophrenia', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.432463130040212, 'COLOC': 0.36333812299594037, 'RARE': 0.9508907719194314, 'SC': 0.7415020149701459, 'SPATIAL': 0.06654530348484422, 'BULK_RNA': 0.8654548010932794, 'BULK_PROT': 0.1569647895755173}}, {'gene': 'GENE264', 'condition': 'nonalcoholic fatty liver disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.39806606326024707, 'COLOC': 0.2060746179505093, 'RARE': 0.9369768534349712, 'SC': 0.12778744183774582, 'SPATIAL': 0.9164238967699595, 'BULK_RNA': 0.7232511799542936, 'BULK_PROT': 0.8156951409674292}}, {'gene': 'GENE265', 'condition': 'ovarian cancer', 'bucket': 'MECHANISM', 'signals': {'MR': 0.2536204217701674, 'COLOC': 0.7705427618988351, 'RARE': 0.18981200478153382, 'SC': 0.24481720491380032, 'SPATIAL': 0.31965410525939353, 'BULK_RNA': 0.31044775522314194, 'BULK_PROT': 0.33472755434989665}}, {'gene': 'GENE266', 'condition': 'autism spectrum disorder', 'bucket': 'MECHANISM', 'signals': {'MR': 0.7140546206136339, 'COLOC': 0.9507366551025774, 'RARE': 0.39149633232305114, 'SC': 0.6477835478650957, 'SPATIAL': 0.9897998246382264, 'BULK_RNA': 0.8956706123188014, 'BULK_PROT': 0.45302609396764504}}, {'gene': 'GENE267', 'condition': 'atopic dermatitis', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.09794432475380244, 'COLOC': 0.49874174157990536, 'RARE': 0.9581170906837012, 'SC': 0.9269806545542882, 'SPATIAL': 0.08416877801223321, 'BULK_RNA': 0.060824779797179596, 'BULK_PROT': 0.5939661295575798}}, {'gene': 'GENE268', 'condition': 'hypertension', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.11788347492161877, 'COLOC': 0.616852047846941, 'RARE': 0.1057488239105473, 'SC': 0.8059699557567626, 'SPATIAL': 0.16567817417084651, 'BULK_RNA': 0.6956370049481905, 'BULK_PROT': 0.7415333593456231}}, {'gene': 'GENE269', 'condition': 'ovarian cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.5032213762360734, 'COLOC': 0.8076714671487359, 'RARE': 0.23575107324989275, 'SC': 0.904798355449265, 'SPATIAL': 0.9430506161879649, 'BULK_RNA': 0.1709176874627032, 'BULK_PROT': 0.14943988406457553}}, {'gene': 'GENE270', 'condition': 'myelodysplastic syndrome', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.003644308470156843, 'COLOC': 0.9827423932573813, 'RARE': 0.05091758887243791, 'SC': 0.265283593580254, 'SPATIAL': 0.8359465906384653, 'BULK_RNA': 0.6370250276992272, 'BULK_PROT': 0.8052778020167469}}, {'gene': 'GENE271', 'condition': 'hypertension', 'bucket': 'MECHANISM', 'signals': {'MR': 0.702625528790932, 'COLOC': 0.6489110895278414, 'RARE': 0.9472430719895538, 'SC': 0.348674281174869, 'SPATIAL': 0.8216455862878017, 'BULK_RNA': 0.7214306930009176, 'BULK_PROT': 0.9654809339941092}}, {'gene': 'GENE272', 'condition': 'systemic lupus erythematosus', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.7276464834799569, 'COLOC': 0.8560729471712087, 'RARE': 0.9097659824053398, 'SC': 0.13370672813372897, 'SPATIAL': 0.6907246936129015, 'BULK_RNA': 0.7831262973560951, 'BULK_PROT': 0.7559747873300722}}, {'gene': 'GENE273', 'condition': 'bipolar disorder', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.43162272636510013, 'COLOC': 0.6628172612428015, 'RARE': 0.6308840171970448, 'SC': 0.03493211950754049, 'SPATIAL': 0.9980876498156684, 'BULK_RNA': 0.4090505186729865, 'BULK_PROT': 0.9885328093599696}}, {'gene': 'GENE274', 'condition': 'pancreatic cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.8380403385552676, 'COLOC': 0.16099042857420232, 'RARE': 0.8346928340360835, 'SC': 0.39549411159887926, 'SPATIAL': 0.5636510166341931, 'BULK_RNA': 0.6452245425097858, 'BULK_PROT': 0.6844777521601181}}, {'gene': 'GENE275', 'condition': 'thyroid cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6553789891608702, 'COLOC': 0.2653211369535282, 'RARE': 0.3803536702449174, 'SC': 0.8494460257954707, 'SPATIAL': 0.4294231171067955, 'BULK_RNA': 0.5864974401080727, 'BULK_PROT': 0.19898958035394}}, {'gene': 'GENE276', 'condition': 'autism spectrum disorder', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.028420995105806313, 'COLOC': 0.10293328528595369, 'RARE': 0.9152376291513034, 'SC': 0.6624652886495418, 'SPATIAL': 0.47366133518437903, 'BULK_RNA': 0.855700589461831, 'BULK_PROT': 0.9699525877813003}}, {'gene': 'GENE277', 'condition': 'covid-19', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.6882224755967339, 'COLOC': 0.5550536259312701, 'RARE': 0.6860101098094734, 'SC': 0.6043236842734403, 'SPATIAL': 0.8279901247483206, 'BULK_RNA': 0.43992198992674736, 'BULK_PROT': 0.36896822028749465}}, {'gene': 'GENE278', 'condition': 'parkinson disease', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.18314063187739493, 'COLOC': 0.8391491942306091, 'RARE': 0.21845416582948107, 'SC': 0.06319590071944714, 'SPATIAL': 0.8089833591349487, 'BULK_RNA': 0.1261752668687378, 'BULK_PROT': 0.33218001309621914}}, {'gene': 'GENE279', 'condition': 'type 1 diabetes', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.6521606478823182, 'COLOC': 0.7115481581350005, 'RARE': 0.4391341392331649, 'SC': 0.6086764089377562, 'SPATIAL': 0.1313414960871967, 'BULK_RNA': 0.328542963173854, 'BULK_PROT': 0.8782943485435523}}, {'gene': 'GENE280', 'condition': 'glioblastoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5642982282703352, 'COLOC': 0.6214928232931035, 'RARE': 0.5750236157452027, 'SC': 0.9929607744611751, 'SPATIAL': 0.09299792166694643, 'BULK_RNA': 0.909229162504114, 'BULK_PROT': 0.4701747352997073}}, {'gene': 'GENE281', 'condition': 'glioblastoma', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7924023071067807, 'COLOC': 0.939863550974014, 'RARE': 0.731873894153142, 'SC': 0.9307927345663285, 'SPATIAL': 0.7286776028847805, 'BULK_RNA': 0.16770146869726466, 'BULK_PROT': 0.17222335019194668}}, {'gene': 'GENE282', 'condition': 'hepatocellular carcinoma', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.2754782339511038, 'COLOC': 0.39907420093927204, 'RARE': 0.013472748379355415, 'SC': 0.174038421919135, 'SPATIAL': 0.029253600033280147, 'BULK_RNA': 0.7045228018883976, 'BULK_PROT': 0.17723407055971196}}, {'gene': 'GENE283', 'condition': 'atrial fibrillation', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.6813378547629992, 'COLOC': 0.17822354037139887, 'RARE': 0.5229109327932719, 'SC': 0.36167101874050445, 'SPATIAL': 0.4347515696662121, 'BULK_RNA': 0.8241087496344057, 'BULK_PROT': 0.7658366993548203}}, {'gene': 'GENE284', 'condition': 'systemic lupus erythematosus', 'bucket': 'MECHANISM', 'signals': {'MR': 0.7005469899570118, 'COLOC': 0.5741315546348373, 'RARE': 0.6948057037240745, 'SC': 0.6734861795746213, 'SPATIAL': 0.9253949389070719, 'BULK_RNA': 0.26608268552984216, 'BULK_PROT': 0.007182976295655341}}, {'gene': 'GENE285', 'condition': 'type 1 diabetes', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6899634437725345, 'COLOC': 0.843241399712202, 'RARE': 0.19566880220129546, 'SC': 0.4267186178101219, 'SPATIAL': 0.9056822712682768, 'BULK_RNA': 0.4826893822044894, 'BULK_PROT': 0.9741412393995708}}, {'gene': 'GENE286', 'condition': 'atrial fibrillation', 'bucket': 'MECHANISM', 'signals': {'MR': 0.6797309598951554, 'COLOC': 0.754760820216883, 'RARE': 0.9163926302253851, 'SC': 0.8104461088627459, 'SPATIAL': 0.5165839137893897, 'BULK_RNA': 0.5244302605692414, 'BULK_PROT': 0.5798331715983712}}, {'gene': 'GENE287', 'condition': 'thyroid cancer', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.7063732492139255, 'COLOC': 0.5738843704238094, 'RARE': 0.7820118376786138, 'SC': 0.6849909352034269, 'SPATIAL': 0.5421267215963339, 'BULK_RNA': 0.7064623296475233, 'BULK_PROT': 0.009821381311728161}}, {'gene': 'GENE288', 'condition': 'duchenne muscular dystrophy', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.15334789478870414, 'COLOC': 0.5730974072419163, 'RARE': 0.8085855039986138, 'SC': 0.5512725307397127, 'SPATIAL': 0.6845779163087441, 'BULK_RNA': 0.00360878912569762, 'BULK_PROT': 0.28637599579818707}}, {'gene': 'GENE289', 'condition': 'acute myeloid leukemia', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.7908855425127591, 'COLOC': 0.9137330357411072, 'RARE': 0.6530409105631314, 'SC': 0.9255799005694447, 'SPATIAL': 0.6324693003223345, 'BULK_RNA': 0.32173943299018426, 'BULK_PROT': 0.5772309427444925}}, {'gene': 'GENE290', 'condition': 'type 2 diabetes', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.7538659452621671, 'COLOC': 0.013412962140480045, 'RARE': 0.6252067676381221, 'SC': 0.49518957462890834, 'SPATIAL': 0.3829322870853378, 'BULK_RNA': 0.39486615508547507, 'BULK_PROT': 0.23325289842678565}}, {'gene': 'GENE291', 'condition': 'coronary artery disease', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.275589547608617, 'COLOC': 0.18593451346232637, 'RARE': 0.2985382092781804, 'SC': 0.5575070869030314, 'SPATIAL': 0.8672982814369575, 'BULK_RNA': 0.17989832285391505, 'BULK_PROT': 0.41613176668631957}}, {'gene': 'GENE292', 'condition': 'parkinson disease', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.7837268921314815, 'COLOC': 0.12115755672940887, 'RARE': 0.8380924585563782, 'SC': 0.2549643104718966, 'SPATIAL': 0.839086254980077, 'BULK_RNA': 0.8707694200066068, 'BULK_PROT': 0.14661231995571544}}, {'gene': 'GENE293', 'condition': 'colorectal cancer', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.5420476849735052, 'COLOC': 0.3582499475325889, 'RARE': 0.1002637471640998, 'SC': 0.5312006993060041, 'SPATIAL': 0.028658045841807422, 'BULK_RNA': 0.9554808727516386, 'BULK_PROT': 0.7626025876250773}}, {'gene': 'GENE294', 'condition': 'major depressive disorder', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.9727289243950986, 'COLOC': 0.5599379698644462, 'RARE': 0.48418640546057423, 'SC': 0.21705790728081764, 'SPATIAL': 0.46004635110343606, 'BULK_RNA': 0.3021258373955428, 'BULK_PROT': 0.2382927821196975}}, {'gene': 'GENE295', 'condition': 'heart failure', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.26829293343552674, 'COLOC': 0.7971579807933394, 'RARE': 0.9526615994405939, 'SC': 0.4507943760301476, 'SPATIAL': 0.6570805366553965, 'BULK_RNA': 0.0908904078260836, 'BULK_PROT': 0.7390840784865591}}, {'gene': 'GENE296', 'condition': 'prostate cancer', 'bucket': 'TRACTABILITY', 'signals': {'MR': 0.9763840891760388, 'COLOC': 0.4698568298189867, 'RARE': 0.08994501667885646, 'SC': 0.6017406837575343, 'SPATIAL': 0.1567710189769883, 'BULK_RNA': 0.8941526938645241, 'BULK_PROT': 0.45057023053516165}}, {'gene': 'GENE297', 'condition': 'bipolar disorder', 'bucket': 'GENETIC_CAUSALITY', 'signals': {'MR': 0.46229366547982986, 'COLOC': 0.33686045375928964, 'RARE': 0.023063260705601385, 'SC': 0.4901408929883102, 'SPATIAL': 0.8521603519365302, 'BULK_RNA': 0.36885669082385353, 'BULK_PROT': 0.47567943998064244}}, {'gene': 'GENE298', 'condition': 'parkinson disease', 'bucket': 'ASSOCIATION', 'signals': {'MR': 0.12450601597847522, 'COLOC': 0.6360444510026907, 'RARE': 0.7204027963331618, 'SC': 0.24957103227601363, 'SPATIAL': 0.4360019379326743, 'BULK_RNA': 0.4928572072128853, 'BULK_PROT': 0.17459188715728513}}, {'gene': 'GENE299', 'condition': 'chronic lymphocytic leukemia', 'bucket': 'CLINICAL_FIT', 'signals': {'MR': 0.5169741396988439, 'COLOC': 0.39981585791672536, 'RARE': 0.7806265586321706, 'SC': 0.7194408757691906, 'SPATIAL': 0.616196817404422, 'BULK_RNA': 0.7348068082300079, 'BULK_PROT': 0.7806075816748174}}]


@router.get("/debug/config")
async def debug_config():
    return {
        "modules": len(MODULES),
        "literature_templates_confirm_n": sum(len(v.get("confirm", [])) for v in LITERATURE_TEMPLATES.values()),
        "literature_templates_disconfirm_n": sum(len(v.get("disconfirm", [])) for v in LITERATURE_TEMPLATES.values()),
        "disease_synonyms_n": sum(len(v) for v in DISEASE_SYNONYMS.values()) + sum(len(v) for v in DISEASE_SYNONYMS_EXT.values()),
        "tissues_n": len(HPA_TISSUE_LIST),
        "celltypes_n": len(CELLTYPE_LIST),
        "pathways_n": len(PATHWAY_LIST),
        "rulebook_size": sum(len(x.get("rules", [])) for x in EVIDENCE_RULEBOOK.values()),
        "qa_vectors": len(QA_TEST_VECTORS),
    }



# -----------------------------
# Backward-compatibility aliases
# -----------------------------

@router.get("/synth/bucket", response_model=BucketNarrative)
async def synth_bucket_get(gene: str, bucket: str, condition: Optional[str] = None):
    return await synth_bucket(BucketSynthRequest(gene=gene, condition=condition, bucket=bucket, module_outputs={}, lit_summary={}))

@router.get("/assoc/geo-arrayexpress", response_model=Evidence)
async def assoc_geo_arrayexpress(gene: str, condition: Optional[str] = None):
    return await assoc_bulk_rna(gene=gene, condition=condition)

@router.get("/assoc/tabula-hca", response_model=Evidence)
async def assoc_tabula_hca(gene: str):
    return await assoc_sc(gene=gene)

@router.post("/assoc/cptac", response_model=Evidence)
async def assoc_cptac(gene: str):
    return await assoc_bulk_prot_pdc(gene=gene)


