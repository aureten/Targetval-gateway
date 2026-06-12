"""
src/targetval/evidence/models.py  (DRAFT — pre-built from SPECFREEZE v2 Part II §3,5 + §12a)

The evidence + synthesis object contracts: RawRecord, Provenance, DependencyTag,
EvidenceContext, TypedClaim, DomainEvidencePacket, CausalPath, GapReport, InsightCard,
ArmAssessment, CrossVectorAssessment. REQUIRED file per spec repo tree.

These are the load-bearing models. The two-pass convergence + causal-path design lives in
the synthesis engine, but the *fields* it reads/writes are all here — keep them faithful to
the spec. Pydantic v2.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from .entities import (  # adjust import path to your tree
    Direction,
    DependencyType,
    EvidenceFamily,
    RelationType,
)


# ---------------------------------------------------------------------------
# Fetch + provenance
# ---------------------------------------------------------------------------
class RawRecord(BaseModel):
    source_name: str
    record_id: str
    record_type: str  # e.g. 'gwas_association', 'mr_result'
    module_key: str  # e.g. 'genetics-l2g'
    payload: dict
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    api_version: str = ""
    http_status: int = 200
    latency_ms: float = 0.0


class Provenance(BaseModel):
    module_key: str
    source_name: str
    dataset_id: Optional[str] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    nct: Optional[str] = None
    accession: Optional[str] = None
    record_id: Optional[str] = None
    extraction_method: str = "api_fetch"
    fetched_at: datetime
    source_version: Optional[str] = None


class DependencyTag(BaseModel):
    tag_type: DependencyType
    coupled_module: str
    coupled_source: str
    detail: str = ""
    estimated_discount: float = 0.5


class EvidenceContext(BaseModel):
    tissue: Optional[str] = None
    cell_type: Optional[str] = None
    cell_state: Optional[str] = None
    organism: str = "human"
    experimental_system: Optional[str] = None
    sample_size: Optional[int] = None
    ancestry: Optional[str] = None  # population ancestry for genetics
    cascade_layers: list[str] = []  # QTL cascade layers traversed


# ---------------------------------------------------------------------------
# Typed claim (the atom of evidence)
# ---------------------------------------------------------------------------
class TypedClaim(BaseModel):
    claim_id: str
    subject_id: str  # canonical_id of source entity
    object_id: str  # canonical_id of target entity
    relation: RelationType
    direction: Direction = Direction.UNKNOWN
    effect_size: Optional[float] = None
    p_value: Optional[float] = None
    confidence_interval: Optional[tuple[float, float]] = None
    tier: int = 3  # assigned post-normalization (1, 2, or 3)
    cascade_depth: int = 0  # QTL cascade layers spanned
    context: EvidenceContext
    provenance: Provenance
    dependency_tags: list[DependencyTag] = []
    valid_from: datetime
    valid_to: Optional[datetime] = None


class DomainEvidencePacket(BaseModel):
    domain: int  # 1-6
    module_key: str
    evidence_family: EvidenceFamily
    target_canonical_id: str
    disease_canonical_id: str
    claims: list[TypedClaim] = []
    supporting_edges: list[TypedClaim] = []
    contradicting_edges: list[TypedClaim] = []
    assumptions: list[str] = []
    limitations: list[str] = []


# ---------------------------------------------------------------------------
# Causal path (the unit of knowledge)
# ---------------------------------------------------------------------------
class CausalPath(BaseModel):
    path_id: str
    edges: list[TypedClaim]
    entity_types_crossed: list[str]
    grammar_matched: str  # 'genetics_1A' .. '1D', 'perturbation', 'genetics_7', etc.
    has_mechanistic_bridge: bool
    has_causal_anchor: bool
    tier_composition: dict[int, int] = {}  # {1: N, 2: N, 3: N}
    cascade_depth: int = 0
    cascade_layers: list[str] = []  # e.g. ['chromatin','transcription','protein']
    path_quality: str = ""  # 'strong' | 'moderate' | 'weak'
    bridge_tier: Optional[int] = None
    # Sprint 5+ (Grammar 6) convergence fields — present for forward-compat:
    convergence_genes: list[str] = []
    convergence_pvalue: Optional[float] = None
    convergence_type: Optional[str] = None


class GapReport(BaseModel):
    gap_type: str  # domain-level or 'cascade:'/'gpmap:'-prefixed
    description: str
    recommendation: str


# ---------------------------------------------------------------------------
# GPMap cross-vector inference (coordinator Step 4.5) — SPECFREEZE §12a
# ---------------------------------------------------------------------------
class ArmAssessment(BaseModel):
    arm_id: str
    arm_tissue: str  # dominant tissue label
    arm_locus_count: int
    arm_trait_categories: list[str]
    arm_pleiotropy_median: float
    arm_pleiotropy_bucket: str  # 'low' | 'medium' | 'high'
    target_coloc_groups_on_arm: int
    arm_coverage_fraction: float


class CrossVectorAssessment(BaseModel):
    target_canonical_id: str
    disease_canonical_id: str
    total_disease_coloc_groups: int
    total_disease_arms: int
    clustering_method: str = "ward_hierarchical"
    silhouette_score: float
    target_arms: list[ArmAssessment]
    target_arm_specificity: str  # 'single_arm' | 'multi_arm' | 'hub'
    cleanest_arm: Optional[ArmAssessment] = None  # lowest arm_pleiotropy_median
    recommendation: str


# ---------------------------------------------------------------------------
# Insight Card (the output)
# ---------------------------------------------------------------------------
class InsightCard(BaseModel):
    card_id: str
    target_canonical_id: str
    disease_canonical_id: str
    domain: Optional[int] = None
    conclusion: str
    supporting_paths: list[CausalPath] = []
    contradicting_evidence: list[TypedClaim] = []
    effective_independent_count: float = 0.0
    tier_distribution: dict[int, int] = {}
    # Two-pass convergence (core design):
    convergence_all_claims: str = "none"  # Pass 1: all validated claims
    convergence_path_filtered: str = "none"  # Pass 2: path-eligible claims only
    mechanism_coverage: float = 0.0  # fraction of claims in valid paths
    has_causal_path: bool = False
    best_path_grammar: Optional[str] = None
    gaps: list[GapReport] = []  # all gaps (domain + cascade + gpmap)
    actionable_recommendations: list[str] = []
    confidence_context: str = ""
    key_assumptions: list[str] = []
    provenance_summary: dict = {}
    mode: str = "SETTLE"
    # GPMap colocalization topology (NEW)
    coloc_group_count: Optional[int] = None
    coloc_group_source: str = "pairwise"  # 'gpmap' when GPMap available
    pleiotropy_score: Optional[int] = None
    pleiotropy_bucket: Optional[str] = None  # 'low'/'medium'/'high'
    tissue_specificity_index: Optional[int] = None
    # Cross-vector inference (NEW)
    cross_vector_assessment: Optional[dict] = None  # CrossVectorAssessment serialized
    arm_context: Optional[str] = None  # e.g. 'brain-BMI arm (115 loci)'
    generated_at: datetime = Field(default_factory=datetime.utcnow)
