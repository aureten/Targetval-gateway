# targetval_router_merged_v2.py
# -*- coding: utf-8 -*-
"""
Targetval Gateway — Unified Live Router (Keyless, Live v2)

This router merges the strongest parts of the two router files you shared:
- broad module surface & EvidenceEnvelope discipline
- hardened HTTP runtime (timeouts, retries with Retry-After, per-host concurrency, allowlist, caching, NDJSON streaming)
- uniform primary→fallback failover (and 0-hit failover for assoc/clin/perturb only)
- zero stubs: every registered module performs a real live fetch to its PRIMARY source
- removed internal modules are NOT registered

IMPORTANT:
• Keyless only. No API keys. Any source that requires auth must NOT be registered.
• For GraphQL-based modules (e.g., OpenTargets, gnomAD, PDC, Pharos), the handler expects a GraphQL query payload.
  You can POST JSON {"query": "...", "variables": {...}} or send ?query=... as a query parameter.
• For REST modules, the gateway forwards all query parameters to the upstream PRIMARY endpoint (pass-through),
  with robust retries/backoff and policy guards.

Run:
  uvicorn targetval_router_merged_v2:app --host 0.0.0.0 --port 8080

This file intentionally contains no project-specific imports beyond FastAPI/httpx/pydantic,
so you can drop it into your stack.

Author: Targetval (Alp) / 2025-10-22
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# Third party (installed in your app runtime)
try:
    import httpx
    from fastapi import FastAPI, APIRouter, Request, Response, Query, Body, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
    from pydantic import BaseModel, Field
except Exception as e:  # pragma: no cover - safe import guard for static file creation
    # These imports are required at runtime, not for static file creation
    pass


# -----------------------------
# Envelope & schema (Pydantic v2)
# -----------------------------

class ProvenanceSource(BaseModel):
    name: str
    url: str
    host: str
    status_code: int | None = None
    response_ms: float | None = None
    bytes: int | None = None
    cached: bool = False
    attempt_index: int = 0
    attempted_at: str | None = None  # ISO8601


class Provenance(BaseModel):
    sources: list[ProvenanceSource] = Field(default_factory=list)
    query: dict[str, Any] = Field(default_factory=dict)
    accessed_at: str = Field(default_factory=lambda: dt.datetime.utcnow().isoformat() + "Z")
    version: str = "keyless-live-v2"
    module_order: list[str] = Field(default_factory=list)


class Paging(BaseModel):
    cursor: str | None = None
    next: str | None = None


class EvidenceEnvelope(BaseModel):
    module: str
    domain: str
    context: dict[str, Any] = Field(default_factory=dict)  # includes qtl_type, qtl_stage if relevant
    records: list[Any] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)
    paging: Paging | None = None
    notes: list[str] = Field(default_factory=list)


# Force rebuild as per v2 typing discipline to avoid UndefinedAnnotation
for _M in (ProvenanceSource, Provenance, Paging, EvidenceEnvelope):
    try:
        _M.model_rebuild(_types_namespace={**globals()})
    except Exception:
        pass


# -----------------------------
# Runtime policy (constants)
# -----------------------------

DEFAULT_CONNECT_TIMEOUT = 6.0
DEFAULT_TOTAL_TIMEOUT = 12.0  # total budget per attempt

MAX_RETRIES = 5
BACKOFF_BASE = 0.6  # seconds, exponential with jitter

# Concurrency
DEFAULT_PER_HOST_LIMIT = 4
GLOBAL_CONCURRENCY_LIMIT = int(os.environ.get("TV_GLOBAL_LIMIT", "64"))

# TTL cache (seconds) – conservative, per config
TTL_FAST = 12 * 3600      # 6–24h → pick 12h
TTL_MODERATE = 48 * 3600  # 24–72h → pick 48h
TTL_SLOW = 14 * 86400     # 7–30d → pick 14d


# -----------------------------
# Source & Module registry
# -----------------------------

class SourceSpec(BaseModel):
    """Describes an upstream data source for a module."""
    name: str
    kind: str = Field(pattern="^(rest|graphql)$")  # 'rest' or 'graphql'
    url: str  # FULL upstream URL endpoint (we do allowlist by host from this)
    cache_class: str = Field(default="moderate", pattern="^(fast|moderate|slow)$")
    allow_stream: bool = False
    requires_auth: bool = False  # MUST be False for registration
    headers: dict[str, str] | None = None


class ModuleSpec(BaseModel):
    key: str                 # e.g., "genetics-l2g"
    path: str                # e.g., "/genetics/l2g"
    domain: str              # D1..D6 names
    qtl_stage: str | None = None  # E0..E6 if applicable
    primary: list[SourceSpec] = Field(default_factory=list)
    fallback: list[SourceSpec] = Field(default_factory=list)
    zero_hit_failover: bool = False  # assoc/clin/perturb → True
    removed: bool = False            # if True → do not register


def _s(url: str, name: str, kind: str = "rest", cache: str = "moderate", stream: bool = False) -> SourceSpec:
    return SourceSpec(name=name, kind=kind, url=url, cache_class=cache, allow_stream=stream, requires_auth=False)


def _m(key: str, path: str, domain: str, qtl: str | None, primary: list[SourceSpec],
       fallback: list[SourceSpec] | None = None, zero_hit: bool = False, removed: bool = False) -> ModuleSpec:
    return ModuleSpec(key=key, path=path, domain=domain, qtl_stage=qtl,
                      primary=primary, fallback=fallback or [], zero_hit_failover=zero_hit, removed=removed)


# Helper: quick domains
D1 = "Genetic causality & human validation"
D2 = "Functional & mechanistic validation"
D3 = "Expression, selectivity & cell-state context"
D4 = "Druggability & modality tractability"
D5 = "Therapeutic index & safety translation"
D6 = "Clinical & translational evidence"


# NOTE: For GraphQL endpoints, we use canonical, public endpoints.
# For REST, we choose stable, well-known base endpoints that accept pass-through parameters.
# This registry adheres to your v2 config (primary vs fallback).

REGISTRY: list[ModuleSpec] = [

    # --- D1 Genetics ---
    _m("genetics-l2g", "/genetics/l2g", D1, "E6",
       primary=[_s("https://api.platform.opentargets.org/api/v4/graphql", "OpenTargets GraphQL", kind="graphql", cache="moderate")],
       fallback=[_s("https://www.ebi.ac.uk/gwas/rest/api", "GWAS Catalog REST", cache="moderate")],
    ),

    _m("genetics-coloc", "/genetics/coloc", D1, "E1/E6",
       primary=[_s("https://api.platform.opentargets.org/api/v4/graphql", "OpenTargets GraphQL", kind="graphql")],
       fallback=[_s("https://www.ebi.ac.uk/eqtl/api", "eQTL Catalogue API"),
                 _s("https://api.opengwas.io/api", "IEU OpenGWAS API")],
    ),

    _m("genetics-mr", "/genetics/mr", D1, "E1/E6",
       primary=[_s("https://api.opengwas.io/api", "IEU OpenGWAS API")],
       fallback=[_s("http://www.phenoscanner.medschl.cam.ac.uk/api", "PhenoScanner v2 API", cache="moderate")]
    ),

    _m("genetics-chromatin-contacts", "/genetics/chromatin-contacts", D1, "E0",
       primary=[_s("https://www.encodeproject.org/search/", "ENCODE REST")],
       fallback=[_s("https://data.4dnucleome.org/search/", "4D Nucleome API"),
                 _s("https://api.genome.ucsc.edu", "UCSC Genome Browser API")]
    ),

    _m("genetics-3d-maps", "/genetics/3d-maps", D1, "E0",
       primary=[_s("https://data.4dnucleome.org/search/", "4D Nucleome API")],
       fallback=[_s("https://api.genome.ucsc.edu", "UCSC Genome Browser API")]
    ),

    _m("genetics-regulatory", "/genetics/regulatory", D1, "E0",
       primary=[_s("https://www.encodeproject.org/search/", "ENCODE REST")],
       fallback=[_s("https://www.ebi.ac.uk/eqtl/api", "eQTL Catalogue API")]
    ),

    _m("genetics-sqtl", "/genetics/sqtl", D1, "E2",
       primary=[_s("https://gtexportal.org/api", "GTEx API")],
       fallback=[_s("https://www.ebi.ac.uk/eqtl/api", "eQTL Catalogue API")]
    ),

    _m("genetics-pqtl", "/genetics/pqtl", D1, "E3",
       primary=[_s("https://api.platform.opentargets.org/api/v4/graphql", "OpenTargets GraphQL", kind="graphql")],
       fallback=[_s("https://api.opengwas.io/api", "IEU OpenGWAS API")]
    ),

    _m("genetics-annotation", "/genetics/annotation", D1, "E0/E2",
       primary=[_s("https://rest.ensembl.org", "Ensembl VEP REST")],
       fallback=[_s("https://myvariant.info/v1", "MyVariant.info"),
                 _s("https://rest.ensembl.org", "Ensembl REST VEP"),
                 _s("https://gnomad.broadinstitute.org/api", "gnomAD GraphQL", kind="graphql")]
    ),

    _m("genetics-pathogenicity-priors", "/genetics/pathogenicity-priors", D1, "E0",
       primary=[_s("https://gnomad.broadinstitute.org/api", "gnomAD GraphQL", kind="graphql")],
       fallback=[_s("https://cadd.gs.washington.edu/api", "CADD API", cache="moderate")]
    ),

    _m("genetics-intolerance", "/genetics/intolerance", D1, "E0",
       primary=[_s("https://gnomad.broadinstitute.org/api", "gnomAD GraphQL", kind="graphql")]
    ),

    _m("genetics-rare", "/genetics/rare", D1, "E6",
       primary=[_s("https://eutils.ncbi.nlm.nih.gov/entrez/eutils", "NCBI E-utilities (ClinVar)")],
       fallback=[_s("https://myvariant.info/v1", "MyVariant.info"),
                 _s("https://rest.ensembl.org", "Ensembl VEP REST")]
    ),

    _m("genetics-mendelian", "/genetics/mendelian", D1, "E6",
       primary=[_s("https://search.clinicalgenome.org/kb", "ClinGen APIs")],  # graph/gene validity resources
    ),

    _m("genetics-phewas-human-knockout", "/genetics/phewas-human-knockout", D1, "E6",
       primary=[_s("https://www.phenoscanner.medschl.cam.ac.uk/api", "PhenoScanner v2 API")],
       fallback=[_s("https://api.opengwas.io/api", "OpenGWAS API"),
                 _s("https://api.monarchinitiative.org/api", "Monarch/HPO API")]
    ),

    _m("genetics-functional", "/genetics/functional", D1, "E4",
       primary=[_s("https://webservice.thebiogrid.org", "BioGRID ORCS REST")],
       fallback=[_s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")]
    ),

    _m("genetics-mavedb", "/genetics/mavedb", D1, "E4",
       primary=[_s("https://api.mavedb.org/graphql", "MaveDB GraphQL", kind="graphql")]
    ),

    _m("genetics-consortia-summary", "/genetics/consortia-summary", D1, "E6",
       primary=[_s("https://api.opengwas.io/api", "OpenGWAS API")]
    ),

    _m("genetics-caqtl-lite", "/genetics/caqtl-lite", D1, "E0",
       primary=[_s("https://www.encodeproject.org/search/", "ENCODE REST"),
                _s("https://data.4dnucleome.org/search/", "4D Nucleome API"),
                _s("https://rest.ensembl.org", "Ensembl VEP REST")]
    ),

    _m("genetics-nmd-inference", "/genetics/nmd-inference", D1, "E2",
       primary=[_s("https://gtexportal.org/api", "GTEx API"),
                _s("https://rest.ensembl.org", "Ensembl REST")]
    ),

    _m("genetics-mqtl-coloc", "/genetics/mqtl-coloc", D1, "E5",
       primary=[_s("https://api.platform.opentargets.org/api/v4/graphql", "OpenTargets GraphQL", kind="graphql")],
       fallback=[_s("https://api.opengwas.io/api", "OpenGWAS API")]
    ),

    _m("genetics-ase-check", "/genetics/ase-check", D1, "E1",
       primary=[_s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")]
    ),

    _m("genetics-ptm-signal-lite", "/genetics/ptm-signal-lite", D1, "E3",
       primary=[_s("https://rest.uniprot.org", "UniProtKB API"),
                _s("https://www.ebi.ac.uk/pride/ws/archive", "PRIDE Archive API")]
    ),

    # --- D2 Mechanism / Associations ---
    _m("assoc-bulk-rna", "/assoc/bulk-rna", D2, None,
       primary=[_s("https://eutils.ncbi.nlm.nih.gov/entrez/eutils", "NCBI GEO E-utilities")],
       fallback=[_s("https://www.ebi.ac.uk/biostudies/api/v1", "BioStudies API"),
                 _s("https://www.ebi.ac.uk/gxa/json", "EBI Expression Atlas API")]
    ),

    _m("assoc-metabolomics", "/assoc/metabolomics", D2, "E5",
       primary=[_s("https://www.ebi.ac.uk/metabolights/ws", "MetaboLights API")],
       fallback=[_s("https://www.metabolomicsworkbench.org/rest", "Metabolomics Workbench API")]
    ),

    _m("assoc-proteomics", "/assoc/proteomics", D2, "E3",
       primary=[_s("https://www.proteomicsdb.org/proteomicsdb/logic/api", "ProteomicsDB API")],
       fallback=[_s("https://pdc.cancer.gov/graphql", "PDC GraphQL", kind="graphql"),
                 _s("https://www.ebi.ac.uk/pride/ws/archive", "PRIDE Archive API")]
    ),

    _m("assoc-perturb", "/assoc/perturb", D2, "E4/E5",
       primary=[_s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")],
       fallback=[_s("https://www.ilincs.org/api", "iLINCS API"),
                 _s("https://api.lincscloud.org", "LINCS LDP3 API")]
    ),

    _m("mech-ppi", "/mech/ppi", D2, "E4",
      primary=[_s("https://string-db.org/api/json/network", "STRING API")],
      fallback=[_s("https://www.ebi.ac.uk/Tools/webservices/psicquic/intact/webservices/current/search/query", "IntAct PSICQUIC"),
                _s("https://omnipathdb.org/interactions", "OmniPath API")]
    ),

    _m("mech-pathways", "/mech/pathways", D2, "E4/E5",
       primary=[_s("https://reactome.org/ContentService", "Reactome Content Service"),
                _s("https://reactome.org/AnalysisService", "Reactome Analysis Service")],
       fallback=[_s("https://www.pathwaycommons.org/pc2", "Pathway Commons API"),
                 _s("https://www.ebi.ac.uk/QuickGO/services/annotation", "QuickGO API"),
                 _s("https://signor.uniroma2.it/api", "SIGNOR API")]
    ),

    _m("mech-ligrec", "/mech/ligrec", D2, "E4",
       primary=[_s("https://omnipathdb.org/interactions", "OmniPath API")],
       fallback=[_s("https://www.guidetopharmacology.org/services", "IUPHAR/Guide to Pharmacology API"),
                 _s("https://reactome.org/ContentService", "Reactome Content Service")]
    ),

    _m("biology-causal-pathways", "/biology/causal-pathways", D2, "E4/E5",
       primary=[_s("https://reactome.org/ContentService", "Reactome Content Service")],
       fallback=[_s("https://signor.uniroma2.it/api", "SIGNOR API")]
    ),

    _m("perturb-crispr-screens", "/perturb/crispr-screens", D2, "E4",
       primary=[_s("https://webservice.thebiogrid.org", "BioGRID ORCS REST")],
       fallback=[_s("https://cellmodelpassports.sanger.ac.uk/api/v1", "Sanger Cell Model Passports API")]
    ),

    _m("perturb-signature-enrichment", "/perturb/signature-enrichment", D2, "E4/E5",
       primary=[_s("https://www.ilincs.org/api", "iLINCS API")],
       fallback=[_s("https://api.lincscloud.org", "LINCS LDP3 API")]
    ),

    _m("assoc-bulk-prot", "/assoc/bulk-prot", D2, "E3",
       primary=[_s("https://www.proteomicsdb.org/proteomicsdb/logic/api", "ProteomicsDB API")],
       fallback=[_s("https://pdc.cancer.gov/graphql", "PDC GraphQL", kind="graphql")]
    ),

    _m("assoc-omics-phosphoproteomics", "/assoc/omics-phosphoproteomics", D2, "E3",
       primary=[_s("https://www.ebi.ac.uk/pride/ws/archive", "PRIDE Archive API")]
    ),

    _m("mech-structure", "/mech/structure", D2, "E4",
       primary=[_s("https://rest.uniprot.org", "UniProtKB API")],
       fallback=[_s("https://alphafold.ebi.ac.uk/api", "AlphaFold DB API"),
                 _s("https://www.ebi.ac.uk/pdbe/api", "PDBe API")]
    ),

    _m("perturb-lincs-signatures", "/perturb/lincs-signatures", D2, "E4/E5",
       primary=[_s("https://api.lincscloud.org", "LINCS LDP3 API")],
       fallback=[_s("https://www.ilincs.org/api", "iLINCS API")]
    ),

    _m("perturb-connectivity", "/perturb/connectivity", D2, "E4/E5",
       primary=[_s("https://www.ilincs.org/api", "iLINCS API")],
       fallback=[_s("https://api.lincscloud.org", "LINCS LDP3 API")]
    ),

    _m("assoc-spatial", "/assoc/spatial", D2, "E3/E4",
       primary=[_s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")]
    ),

    # --- D3 Expression ---
    _m("expr-baseline", "/expr/baseline", D3, None,
       primary=[_s("https://gtexportal.org/api", "GTEx API")],
       fallback=[_s("https://www.ebi.ac.uk/gxa/json", "EBI Expression Atlas API")]
    ),

    _m("expr-localization", "/expr/localization", D3, None,
       primary=[_s("https://rest.uniprot.org", "UniProtKB API")]
    ),

    _m("expr-inducibility", "/expr/inducibility", D3, None,
       primary=[_s("https://www.ebi.ac.uk/gxa/json", "EBI Expression Atlas API")],
       fallback=[_s("https://eutils.ncbi.nlm.nih.gov/entrez/eutils", "NCBI GEO E-utilities"),
                 _s("https://www.ebi.ac.uk/biostudies/api/v1", "BioStudies API")]
    ),

    _m("assoc-sc", "/assoc/sc", D3, None,
       primary=[_s("https://service.azul.data.humancellatlas.org", "HCA Azul APIs")],
       fallback=[_s("https://www.ebi.ac.uk/gxa/sc", "Single-Cell Expression Atlas API"),
                 _s("https://api.cellxgene.cziscience.com/dp/v1", "CELLxGENE Discover API")]
    ),

    _m("assoc-hpa-pathology", "/assoc/hpa-pathology", D3, None,
       primary=[_s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")]
    ),

    _m("sc-hubmap", "/sc/hubmap", D3, None,
       primary=[_s("https://search.api.hubmapconsortium.org", "HuBMAP Search API")],
       fallback=[_s("https://service.azul.data.humancellatlas.org", "HCA Azul APIs")]
    ),

    # --- D4 Tractability / Modality ---
    _m("tract-drugs", "/tract/drugs", D4, None,
       primary=[_s("https://www.ebi.ac.uk/chembl/api/data", "ChEMBL API")],
       fallback=[_s("https://drugcentral.org/api", "DrugCentral API"),
                 _s("https://www.bindingdb.org/axis2/services", "BindingDB Services"),
                 _s("https://pubchem.ncbi.nlm.nih.gov/rest/pug", "PubChem PUG-REST"),
                 _s("http://stitch.embl.de/api", "STITCH API"),
                 _s("https://pharos-api.ncats.io/graphql", "Pharos GraphQL", kind="graphql")]
    ),

    _m("tract-ligandability-sm", "/tract/ligandability-sm", D4, None,
       primary=[_s("https://rest.uniprot.org", "UniProtKB API")],
       fallback=[_s("https://alphafold.ebi.ac.uk/api", "AlphaFold DB API"),
                 _s("https://www.ebi.ac.uk/pdbe/api", "PDBe API"),
                 _s("https://www.ebi.ac.uk/pdbe/pdbe-kb/api", "PDBe-KB API"),
                 _s("https://www.bindingdb.org/axis2/services", "BindingDB Services")]
    ),

    _m("tract-ligandability-oligo", "/tract/ligandability-oligo", D4, None,
       primary=[_s("https://rest.ensembl.org", "Ensembl VEP REST")],
       fallback=[_s("https://rnacentral.org/api", "RNAcentral API"),
                 _s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")]
    ),

    _m("tract-modality", "/tract/modality", D4, None,
       primary=[_s("https://rest.uniprot.org", "UniProtKB API")],
       fallback=[_s("https://alphafold.ebi.ac.uk/api", "AlphaFold DB API"),
                 _s("https://pharos-api.ncats.io/graphql", "Pharos GraphQL", kind="graphql"),
                 _s("https://www.guidetopharmacology.org/services", "IUPHAR/Guide to Pharmacology API")]
    ),

    # --- D5 Safety / Immunology ---
    _m("perturb-drug-response", "/perturb/drug-response", D5, None,
       primary=[_s("https://api.pharmacodb.ca/v2", "PharmacoDB API")],
       fallback=[_s("https://discover.nci.nih.gov/cellminerapi/api", "NCI-60 CellMiner API")]
    ),

    _m("tract-immunogenicity", "/tract/immunogenicity", D5, "E4",
       primary=[_s("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "Europe PMC API")],
       fallback=[_s("https://api.nextflow.iedb.org", "IEDB Tools API"),  # generic gateway; concrete tools are subpaths
                 _s("https://www.ebi.ac.uk/ipd/api", "IPD-IMGT/HLA API")]
    ),

    _m("tract-mhc-binding", "/tract/mhc-binding", D5, "E4",
       primary=[_s("https://api.nextflow.iedb.org", "IEDB Tools API")],
       fallback=[_s("https://www.ebi.ac.uk/ipd/api", "IPD-IMGT/HLA API")]
    ),

    _m("tract-iedb-epitopes", "/tract/iedb-epitopes", D5, "E4",
       primary=[_s("https://www.iedb.org/api", "IEDB Observed Epitopes API")],
       fallback=[_s("https://api.nextflow.iedb.org", "IEDB Tools API")]
    ),

    _m("immuno-hla-coverage", "/immuno/hla-coverage", D5, "E4",
       primary=[_s("https://www.ebi.ac.uk/ipd/api", "IPD-IMGT/HLA API")],
       fallback=[_s("https://api.nextflow.iedb.org", "IEDB Tools API")]
    ),

    _m("clin-safety", "/clin/safety", D5, "E6",
       primary=[_s("https://api.fda.gov/drug/event.json", "openFDA FAERS API", cache="moderate")],
       fallback=[_s("https://www.mousephenotype.org/data/api", "IMPC API")]
    ),

    _m("clin-rwe", "/clin/rwe", D5, "E6",
       primary=[_s("https://api.fda.gov/drug/event.json", "openFDA FAERS API", cache="moderate")]
    ),

    _m("clin-on-target-ae-prior", "/clin/on-target-ae-prior", D5, "E6",
       primary=[_s("https://drugcentral.org/api", "DrugCentral API")],
       fallback=[_s("https://api.fda.gov/drug/event.json", "openFDA FAERS API")]
    ),

    # --- D6 Clinical & translational ---
    _m("function-dependency", "/function/dependency", D2, "E4",
       primary=[_s("https://webservice.thebiogrid.org", "BioGRID ORCS REST")],
       fallback=[_s("https://cellmodelpassports.sanger.ac.uk/api/v1", "Sanger Cell Model Passports API")]
    ),

    _m("perturb-depmap-dependency", "/perturb/depmap-dependency", D2, "E4",
       primary=[_s("https://cellmodelpassports.sanger.ac.uk/api/v1", "Sanger Cell Model Passports API")],
       fallback=[_s("https://webservice.thebiogrid.org", "BioGRID ORCS REST")]
    ),

    _m("clin-endpoints", "/clin/endpoints", D6, "E6",
       primary=[_s("https://clinicaltrials.gov/api/v2", "ClinicalTrials.gov v2 API")],
       fallback=[_s("https://trialsearch.who.int/api", "WHO ICTRP web service")]
    ),

    _m("clin-biomarker-fit", "/clin/biomarker-fit", D6, "E6",
       primary=[_s("https://api.monarchinitiative.org/api", "HPO/Monarch APIs")],
       fallback=[_s("https://api.pharmgkb.org/v1", "PharmGKB API")]
    ),

    _m("clin-pipeline", "/clin/pipeline", D6, "E6",
       primary=[_s("https://clinicaltrials.gov/api/v2", "ClinicalTrials.gov v2 API")],
       fallback=[_s("https://drugcentral.org/api", "DrugCentral API")]
    ),

    _m("clin-feasibility", "/clin/feasibility", D6, "E6",
       primary=[_s("https://clinicaltrials.gov/api/v2", "ClinicalTrials.gov v2 API")],
       fallback=[_s("https://trialsearch.who.int/api", "WHO ICTRP web service")]
    ),

    _m("comp-intensity", "/comp/intensity", D6, None,
       primary=[_s("https://api.patentsview.org/patents/query", "PatentsView API", cache="slow")]
    ),

    _m("comp-freedom", "/comp/freedom", D6, None,
       primary=[_s("https://api.patentsview.org/patents/query", "PatentsView API", cache="slow")]
    ),

    # Removed modules (not registered)
    _m("perturb-qc", "/perturb/qc", D2, None, primary=[], removed=True),
    _m("perturb-scrna-summary", "/perturb/scrna-summary", D2, None, primary=[], removed=True),
]


# Build lookups
PATH_TO_MODULE: Dict[str, ModuleSpec] = {m.path: m for m in REGISTRY if not m.removed}
KEY_TO_MODULE: Dict[str, ModuleSpec] = {m.key: m for m in REGISTRY if not m.removed}


# -----------------------------
# TTL Cache (in-memory)
# -----------------------------

class TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, bytes, Dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _ttl_seconds(self, klass: str) -> int:
        return {"fast": TTL_FAST, "moderate": TTL_MODERATE, "slow": TTL_SLOW}.get(klass, TTL_MODERATE)

    @staticmethod
    def _key(method: str, url: str, params: Dict[str, Any] | None, body: Any | None) -> str:
        h = hashlib.sha256()
        h.update(method.upper().encode())
        h.update(url.encode())
        if params:
            h.update(json.dumps(sorted(params.items()), separators=(",", ":"), ensure_ascii=False).encode())
        if body is not None:
            try:
                h.update(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
            except Exception:
                h.update(str(body).encode())
        return h.hexdigest()

    async def get(self, klass: str, method: str, url: str, params: Dict[str, Any] | None, body: Any | None) -> Tuple[bool, Optional[bytes], Optional[Dict[str, Any]]]:
        k = self._key(method, url, params, body)
        async with self._lock:
            item = self._store.get(k)
            if not item:
                return False, None, None
            expires_at, data, meta = item
            if time.time() > expires_at:
                self._store.pop(k, None)
                return False, None, None
            return True, data, meta

    async def set(self, klass: str, method: str, url: str, params: Dict[str, Any] | None, body: Any | None, data: bytes, meta: Dict[str, Any]) -> None:
        ttl = self._ttl_seconds(klass)
        k = self._key(method, url, params, body)
        async with self._lock:
            self._store[k] = (time.time() + ttl, data, meta)


CACHE = TTLCache()


# -----------------------------
# HTTP Client & Policy
# -----------------------------

class HttpRuntime:
    def __init__(self, per_host_limit: int = DEFAULT_PER_HOST_LIMIT, global_limit: int = GLOBAL_CONCURRENCY_LIMIT):
        self.client: httpx.AsyncClient | None = None
        self.host_semaphores: Dict[str, asyncio.Semaphore] = {}
        self.global_semaphore = asyncio.Semaphore(global_limit)
        self.per_host_limit = per_host_limit
        self.logger = logging.getLogger("targetval.router")

        # Build allowlist (hosts from registry, primary + fallback)
        self.allowed_hosts: set[str] = set()
        for m in PATH_TO_MODULE.values():
            for s in (m.primary + m.fallback):
                try:
                    host = urllib.parse.urlparse(s.url).hostname
                    if host:
                        self.allowed_hosts.add(host)
                except Exception:
                    pass

    async def startup(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=DEFAULT_CONNECT_TIMEOUT, read=DEFAULT_TOTAL_TIMEOUT, write=DEFAULT_TOTAL_TIMEOUT),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            http2=True,
            headers={"User-Agent": "Targetval-Gateway/Keyless-Live-v2"}
        )
        # Create semaphores for allowed hosts
        for host in self.allowed_hosts:
            self.host_semaphores[host] = asyncio.Semaphore(self.per_host_limit)
        self.logger.info("HTTP runtime started", extra={"allowed_hosts": sorted(self.allowed_hosts)})

    async def shutdown(self):
        if self.client is not None:
            await self.client.aclose()
        self.logger.info("HTTP runtime shut down")

    def _pick_cache_class(self, source: SourceSpec) -> str:
        return source.cache_class or "moderate"

    @staticmethod
    def _should_retry(status: int) -> bool:
        # Retry on 5xx and 429; optionally on some 4xx (treat 408 as retryable)
        return status >= 500 or status in (408, 429)

    @staticmethod
    def _parse_retry_after(headers: Dict[str, str]) -> float | None:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if not ra:
            return None
        try:
            # seconds
            return float(ra)
        except ValueError:
            # HTTP-date
            try:
                dtg = dt.datetime.strptime(ra, "%a, %d %b %Y %H:%M:%S GMT")
                return max(0.0, (dtg - dt.datetime.utcnow()).total_seconds())
            except Exception:
                return None

    async def fetch(self, source: SourceSpec, method: str, url: str,
                    params: Dict[str, Any] | None, json_body: Any | None,
                    stream: bool = False) -> Tuple[bytes, Dict[str, Any], int, bool]:
        """
        Returns: (bytes, response_headers, status_code, cached_flag)
        """
        if self.client is None:
            raise RuntimeError("HTTP runtime not started")

        # Allowlist enforcement
        host = urllib.parse.urlparse(url).hostname
        if not host or host not in self.allowed_hosts:
            raise HTTPException(status_code=400, detail=f"Host '{host}' not in allowlist for this gateway")

        # Caching (GET only, when not streaming)
        cached = False
        if method.upper() == "GET" and not stream:
            hit, data, meta = await CACHE.get(source.cache_class, method, url, params, json_body)
            if hit and data is not None and meta is not None:
                return data, meta, 200, True

        # Concurrency
        sem = self.host_semaphores.get(host) or asyncio.Semaphore(DEFAULT_PER_HOST_LIMIT)

        attempt = 0
        exc: Exception | None = None
        last_meta: Dict[str, Any] = {}
        while attempt < MAX_RETRIES:
            attempt += 1
            t0 = time.perf_counter()
            retry_after = 0.0
            try:
                async with self.global_semaphore:
                    async with sem:
                        if stream:
                            # streaming proxy (GET only)
                            async with self.client.stream("GET", url, params=params, headers=source.headers) as r:
                                status = r.status_code
                                # Retry logic header-based
                                retry_after = self._parse_retry_after(r.headers) or 0.0
                                content = b""
                                # For streaming, we don't buffer here; caller will re-open stream
                                # But we still return small content for metadata probe
                                t1 = time.perf_counter()
                                last_meta = {"headers": dict(r.headers), "elapsed_ms": int(1000 * (t1 - t0))}
                                if self._should_retry(status) and attempt < MAX_RETRIES:
                                    # Respect Retry-After if present
                                    await asyncio.sleep(retry_after or (BACKOFF_BASE * (2 ** (attempt - 1))))
                                    continue
                                # If streaming requested, return headers only; caller will open a fresh stream
                                return content, last_meta, status, False

                        else:
                            r = await self.client.request(method.upper(), url, params=params, json=json_body, headers=source.headers)
                            t1 = time.perf_counter()
                            status = r.status_code
                            body = await r.aread()
                            last_meta = {"headers": dict(r.headers), "elapsed_ms": int(1000 * (t1 - t0))}
                            if 200 <= status < 300:
                                # Cache GETs
                                if method.upper() == "GET":
                                    await CACHE.set(source.cache_class, method, url, params, json_body, body, last_meta)
                                return body, last_meta, status, False
                            else:
                                retry_after = self._parse_retry_after(r.headers) or 0.0
                                if self._should_retry(status) and attempt < MAX_RETRIES:
                                    await asyncio.sleep(retry_after or (BACKOFF_BASE * (2 ** (attempt - 1))))
                                    continue
                                # Non-retryable failure -> return as-is
                                return body, last_meta, status, False

            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
                exc = e
                # exponential backoff
                await asyncio.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            except Exception as e:
                exc = e
                break

        if exc is not None:
            raise HTTPException(status_code=504, detail=f"Upstream fetch error after {MAX_RETRIES} attempts: {exc}")
        else:
            # Should not reach here
            raise HTTPException(status_code=502, detail="Upstream error without exception")


# -----------------------------
# Router assembly
# -----------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="Targetval Gateway — Keyless, Live v2", version="2025-10-22")
    router = APIRouter()
    runtime = HttpRuntime()

    @app.on_event("startup")
    async def _startup():
        await runtime.startup()

    @app.on_event("shutdown")
    async def _shutdown():
        await runtime.shutdown()

    def extract_records(obj: Any) -> list[Any]:
        # Conservative: try common keys, else wrap
        if obj is None:
            return []
        if isinstance(obj, list):
            return obj
        if isinstance(obj, (str, bytes)):
            return [obj]  # raw payload
        if isinstance(obj, dict):
            for k in ("results", "data", "items", "hits", "records"):
                if k in obj and isinstance(obj[k], list):
                    return obj[k]
            return [obj]
        # fallback
        return [obj]

    def module_family(key: str) -> str:
        # assoc/clin/perturb families get 0-hit failover
        if key.startswith("assoc-"):
            return "assoc"
        if key.startswith("clin-"):
            return "clin"
        if key.startswith("perturb-"):
            return "perturb"
        return key.split("-", 1)[0]

    async def handle_module(request: Request, mod: ModuleSpec, stream: int = 0) -> Response:
        """
        Generic handler per module: tries PRIMARY then FALLBACK sources per policy.
        Accepts both GET (query params) and POST (JSON) for GraphQL modules.
        """
        # Reserve params
        qp = dict(request.query_params)
        stream_flag = str(stream) == "1" or qp.pop("stream", "0") == "1"

        # Body (GraphQL)
        body_json = None
        if request.method.upper() in ("POST", "PUT", "PATCH"):
            try:
                body_json = await request.json()
            except Exception:
                body_json = None

        envelope = EvidenceEnvelope(
            module=mod.key,
            domain=mod.domain,
            context={k: v for k, v in qp.items() if k in ("target", "gene", "gene_id", "trait", "trait_id", "variant", "variant_id", "tissue", "cell_type", "assay", "qtl_type")},
            notes=[],
        )
        envelope.provenance.query = {**qp}
        envelope.provenance.module_order = [s.name for s in (mod.primary + mod.fallback)]

        # Determine ordered sources
        sources = mod.primary + mod.fallback
        attempts_meta: list[ProvenanceSource] = []

        # Iterate sources
        last_error_payload: dict[str, Any] | None = None
        for idx, src in enumerate(sources):
            # Determine URL & method
            url = src.url
            method = "POST" if (src.kind == "graphql" and (request.method.upper() == "POST" or "query" in qp or body_json)) else "GET"
            json_body = None

            # GraphQL handling
            if src.kind == "graphql":
                if request.method.upper() == "POST" and body_json and "query" in body_json:
                    json_body = body_json
                elif "query" in qp:
                    json_body = {"query": qp["query"]}
                    qp.pop("query", None)
                else:
                    # No GraphQL payload provided — return structured error
                    attempts_meta.append(ProvenanceSource(
                        name=src.name, url=url, host=urllib.parse.urlparse(url).hostname or "", status_code=400,
                        response_ms=0, bytes=0, cached=False, attempt_index=idx + 1,
                        attempted_at=dt.datetime.utcnow().isoformat() + "Z"
                    ))
                    last_error_payload = {"error": "Missing GraphQL 'query' for GraphQL source", "source": src.name}
                    continue  # try next source if any

            # Fetch
            try:
                # Streaming policy
                if stream_flag and src.allow_stream and method == "GET":
                    # Open a new streaming connection and proxy chunks
                    # Note: We open the stream fresh to return a StreamingResponse.
                    async def _stream_gen():
                        # New stream to ensure generator context
                        async with runtime.client.stream("GET", url, params=qp, headers=src.headers) as r:
                            yield from ()  # placeholder to satisfy type checker
                            async for chunk in r.aiter_raw():
                                yield chunk
                    # Build provenance entry
                    attempts_meta.append(ProvenanceSource(
                        name=src.name, url=url, host=urllib.parse.urlparse(url).hostname or "",
                        status_code=200, response_ms=0, bytes=None, cached=False, attempt_index=idx + 1,
                        attempted_at=dt.datetime.utcnow().isoformat() + "Z"))
                    return StreamingResponse(_stream_gen(), media_type="application/x-ndjson")

                # Normal (buffered) request
                raw, meta, status, cached = await runtime.fetch(src, method, url, params=qp, json_body=json_body, stream=False)

                # Provenance
                attempts_meta.append(ProvenanceSource(
                    name=src.name, url=url, host=urllib.parse.urlparse(url).hostname or "",
                    status_code=status, response_ms=meta.get("elapsed_ms"), bytes=len(raw), cached=cached,
                    attempt_index=idx + 1, attempted_at=dt.datetime.utcnow().isoformat() + "Z"
                ))

                # Check status
                if not (200 <= status < 300):
                    # Try next source on error
                    last_error_payload = {"upstream_status": status, "source": src.name, "headers": meta.get("headers", {})}
                    continue

                # Parse JSON
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    # Not JSON – return as text
                    payload = {"raw": raw.decode("utf-8", errors="replace")}

                recs = extract_records(payload)

                # 0-hit failover policy
                if (not recs) and mod.zero_hit_failover and idx < (len(sources) - 1):
                    # Try next source
                    last_error_payload = {"no_data": True, "source": src.name}
                    continue

                # Success → return envelope
                envelope.records = recs
                envelope.provenance.sources = attempts_meta
                envelope.context["qtl_stage"] = mod.qtl_stage
                return JSONResponse(envelope.model_dump())

            except HTTPException as he:
                # Propagate only if last source; else try fallback
                attempts_meta.append(ProvenanceSource(
                    name=src.name, url=url, host=urllib.parse.urlparse(url).hostname or "",
                    status_code=he.status_code, response_ms=None, bytes=None, cached=False,
                    attempt_index=idx + 1, attempted_at=dt.datetime.utcnow().isoformat() + "Z"
                ))
                last_error_payload = {"error": str(he.detail), "source": src.name, "status": he.status_code}
                continue
            except Exception as e:
                attempts_meta.append(ProvenanceSource(
                    name=src.name, url=url, host=urllib.parse.urlparse(url).hostname or "",
                    status_code=502, response_ms=None, bytes=None, cached=False,
                    attempt_index=idx + 1, attempted_at=dt.datetime.utcnow().isoformat() + "Z"
                ))
                last_error_payload = {"error": str(e), "source": src.name}
                continue

        # If we exhausted all sources → return structured NO_DATA or UPSTREAM_ERROR
        envelope.provenance.sources = attempts_meta
        if last_error_payload and last_error_payload.get("no_data"):
            return JSONResponse({"status": "NO_DATA", "module": mod.key, "provenance": envelope.provenance.model_dump()}, status_code=204)
        else:
            return JSONResponse({"status": "UPSTREAM_ERROR", "module": mod.key, "error": last_error_payload, "provenance": envelope.provenance.model_dump()}, status_code=502)

    # Register routes dynamically
    for mod in REGISTRY:
        if mod.removed:
            continue

        # Decide zero-hit policy by family if not explicitly set
        if mod.zero_hit_failover is False:
            fam = module_family(mod.key)
            if fam in {"assoc", "clin", "perturb"}:
                mod.zero_hit_failover = True

        # GET + POST for GraphQL-friendly handlers
        async def _handler(request: Request, stream: int = Query(0)):
            return await handle_module(request, mod, stream=stream)

        router.add_api_route(mod.path, _handler, methods=["GET", "POST"], name=mod.key)

    # Health & registry endpoints
    @router.get("/__registry")
    async def __registry():
        return {
            "modules": [m.model_dump() for m in REGISTRY if not m.removed],
            "removed": [m.model_dump() for m in REGISTRY if m.removed],
            "allowed_hosts": sorted(list(runtime.allowed_hosts)),
            "policy": {
                "timeouts": {"connect": DEFAULT_CONNECT_TIMEOUT, "total": DEFAULT_TOTAL_TIMEOUT},
                "retries": MAX_RETRIES, "backoff_base": BACKOFF_BASE,
                "concurrency": {"per_host": DEFAULT_PER_HOST_LIMIT, "global": GLOBAL_CONCURRENCY_LIMIT},
                "cache_ttl": {"fast": TTL_FAST, "moderate": TTL_MODERATE, "slow": TTL_SLOW},
            },
        }

    @router.get("/__health")
    async def __health():
        return {"ok": True, "utc": dt.datetime.utcnow().isoformat() + "Z"}

    app.include_router(router)
    return app


# Expose app for uvicorn
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("targetval_router_merged_v2:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), reload=False)
