"""
src/targetval/canonical/entities.py  (DRAFT — pre-built from SPECFREEZE v2 Part II §3-4)

11 canonical entity models + all enums. REQUIRED file per spec repo tree.
Identity-resolution precedence (do not silently merge ambiguous entities):
  genes    = HGNC -> Ensembl -> MyGene.info -> NCBI
  diseases = MONDO -> EFO -> MeSH -> DOID
  drugs    = ChEMBL -> DrugBank -> PubChem
  variants = rsID -> ClinVar -> HGVS
  pathways = Reactome -> KEGG -> GO

NOTE: This is a faithful draft of the spec contracts. Verify field-for-field against
docs/SPECFREEZE §4 before freezing. Pydantic v2.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums (SPECFREEZE §5 / §20 canonical/entities.py responsibility)
# ---------------------------------------------------------------------------
class RelationType(str, Enum):
    # GPMap (3)
    COLOCALIZES_IN_GROUP = "colocalizes_in_group"
    HAS_PLEIOTROPY_PROFILE = "has_pleiotropy_profile"
    COLOCALIZES_IN_TISSUE = "colocalizes_in_tissue"
    # Perturbation (2)
    PERTURBS_TRANSCRIPTOME = "perturbs_transcriptome"
    CONTEXT_DEPENDENT_EFFECT = "context_dependent_effect"
    # Original (14)
    CAUSES = "causes"
    ASSOCIATES = "associates"
    REGULATES = "regulates"
    COLOCALIZES = "colocalizes"
    MR_EFFECT = "MR_effect"
    CONTACTS_3D = "contacts_3D"
    EXPRESSED_IN = "expressed_in"
    SELECTIVE_FOR = "selective_for"
    BINDS_TO = "binds_to"
    POCKET_ON = "pocket_on"
    EPITOPE_OF = "epitope_of"
    LEADS_TO_AE = "leads_to_AE"
    USED_IN_TRIAL = "used_in_trial"
    IMPROVES_ENDPOINT = "improves_endpoint"


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class DependencyType(str, Enum):
    SHARED_DATASET = "shared_dataset"
    SHARED_COHORT = "shared_cohort"
    SHARED_CONSORTIUM = "shared_consortium"
    SHARED_PRIOR = "shared_prior"
    SHARED_ONTOLOGY = "shared_ontology"
    SHARED_PUBLICATION = "shared_publication"


class ValidationResult(str, Enum):
    PASS = "PASS"
    QUARANTINE = "QUARANTINE"
    SOFT_FLAG = "SOFT_FLAG"


class EvidenceFamily(str, Enum):
    GENETICS = "genetics"
    COLOCALIZATION_TOPOLOGY = "colocalization_topology"  # GPMap
    PERTURBATION = "perturbation"
    EXPRESSION = "expression"
    PATHWAY = "pathway"
    DRUGGABILITY = "druggability"
    CLINICAL = "clinical"
    LITERATURE = "literature"


class OperatingMode(str, Enum):
    SEARCH = "SEARCH"
    SETTLE = "SETTLE"


class ResolutionConfidence(str, Enum):
    EXACT = "exact"
    HIGH = "high"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


# ---------------------------------------------------------------------------
# 11 canonical entities (9 original + ColocalizationGroup + PleiotropyProfile)
# ---------------------------------------------------------------------------
class Gene(BaseModel):
    canonical_id: str  # 'gene:TP53'
    symbol: str  # HGNC approved
    hgnc_id: Optional[str] = None
    ensembl_gene_id: Optional[str] = None
    entrez_id: Optional[str] = None
    uniprot_ids: list[str] = []
    synonyms: list[str] = []
    taxon_id: int = 9606


class Protein(BaseModel):
    canonical_id: str  # 'protein:P04637'
    uniprot_id: str
    gene_canonical_id: Optional[str] = None
    name: Optional[str] = None
    synonyms: list[str] = []


class Disease(BaseModel):
    canonical_id: str  # 'disease:MONDO_0007254'
    name: str
    mondo_id: Optional[str] = None
    efo_id: Optional[str] = None
    mesh_ids: list[str] = []
    synonyms: list[str] = []


class Variant(BaseModel):
    canonical_id: str  # 'variant:rs429358'
    rsid: Optional[str] = None
    hgvs: Optional[str] = None
    clinvar_id: Optional[str] = None
    chromosome: Optional[str] = None
    position: Optional[int] = None
    gene_canonical_id: Optional[str] = None


class Drug(BaseModel):
    canonical_id: str  # 'drug:CHEMBL25'
    name: str
    chembl_id: Optional[str] = None
    max_phase: Optional[int] = None


class Pathway(BaseModel):
    canonical_id: str  # 'pathway:R-HSA-109582'
    name: str
    reactome_id: Optional[str] = None
    kegg_id: Optional[str] = None
    go_id: Optional[str] = None


class CellType(BaseModel):
    canonical_id: str  # 'celltype:CL_0000235'
    name: str
    cl_id: Optional[str] = None


class Biomarker(BaseModel):
    canonical_id: str
    name: str


class AdverseEvent(BaseModel):
    canonical_id: str
    name: str
    meddra_id: Optional[str] = None


# GPMap integration entities (NEW)
class ColocalizationGroup(BaseModel):
    canonical_id: str  # 'coloc_group:gpmap_12345'
    gpmap_group_id: str
    candidate_variant: Optional[str] = None
    chromosome: Optional[str] = None
    position: Optional[int] = None
    num_traits: int = 0
    num_complex_traits: int = 0
    num_molecular_traits: int = 0
    trait_categories: list[str] = []
    protein_coding_genes: list[str] = []
    tissues: list[str] = []
    group_connectedness: float = 0.0
    h4_min: float = 0.0


class PleiotropyProfile(BaseModel):
    canonical_id: str  # 'pleiotropy:gene_TP53'
    gene_canonical_id: str
    distinct_trait_categories: int = 0  # GPMap pleiotropy score
    distinct_protein_coding_genes: int = 0
    num_coloc_groups: int = 0
    tissue_specificity_index: int = 0  # tissues with colocalizing QTLs
    tissue_list: list[str] = []
    rs_bucket: str = "unknown"  # 'low' (0-1), 'medium' (2-15), 'high' (>15)


# ---------------------------------------------------------------------------
# Request / fetch objects (SPECFREEZE §3)
# ---------------------------------------------------------------------------
class TargetQuery(BaseModel):
    gene_symbol: str
    disease_name: str
    gene_id: Optional[str] = None  # resolved canonical ID
    disease_id: Optional[str] = None  # resolved canonical ID
    mode: OperatingMode = OperatingMode.SETTLE
    requested_domains: Optional[list[int]] = None  # None = all 6
