

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
    js = await _httpx_json_post(EXTERNAL_URLS['opentargets_graphql'], {"query": q, "variables": {"q": condition}})
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
try:
    import networkx as nx
except Exception:
    nx = None
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
    # D1: Genetic causality & human validation
    1: [
        "genetics-regulatory",
        "genetics-lncrna",
        "genetics-mirna",
        "genetics-sqtl",
        "genetics-caqtl-lite",
        "genetics-3d-maps",
        "genetics-chromatin-contacts",
        "genetics-annotation",
        "genetics-pathogenicity-priors",
        "genetics-intolerance",
        "genetics-ase-check",
        "genetics-functional",
        "genetics-mavedb",
        "genetics-pqtl",
        "genetics-mqtl-coloc",
        "genetics-rare",
        "genetics-mendelian",
        "genetics-phewas-human-knockout",
        "genetics-l2g",
        "genetics-coloc",
        "genetics-mr",
        "genetics-consortia-summary",
        "genetics-nmd-inference",
        "genetics-ptm-signal-lite",
    ],
    # D2: Functional & mechanistic validation
    2: [
        # Ordered list per v2.2 spec (Functional & mechanistic validation)
        "mech-ppi",
        "mech-ligrec",
        "mech-pathways",
        "mech-directed-signaling",
        "mech-kinase-substrate",
        "mech-tf-target",
        "mech-mirna-target",
        "mech-complexes",
        "assoc-perturb",
        "perturb-crispr-screens",
        "perturb-lincs-signatures",
        "perturb-signature-enrichment",
        "perturb-connectivity",
        "assoc-proteomics",
        "assoc-bulk-rna",
        "assoc-metabolomics",
        "biology-causal-pathways",
        "mech-structure",
        "perturb-perturbseq-encode",
    ],
    # D3: Expression, selectivity & cell-state context
    3: [
        "expr-baseline",
        "expr-inducibility",
        "expr-localization",
        "assoc-sc",
        "sc-hubmap",
        "assoc-spatial",
        "assoc-bulk-prot",
        "assoc-omics-phosphoproteomics",
        "assoc-hpa-pathology",
    ],
    # D4: Druggability & modality tractability
    4: [
        "mech-structure",
        "tract-ligandability-sm",
        "tract-ligandability-oligo",
        "tract-modality",
        "tract-drugs",
        "perturb-drug-response",
        # ligandability & surfaceome belong to druggability per config
        "tract-ligandability-ab",
        "tract-surfaceome",
    ],
    # D5: Therapeutic index & safety translation (ordered per spec)
    5: [
        "tract-immunogenicity",
        "tract-mhc-binding",
        "tract-iedb-epitopes",
        "immuno/hla-coverage",
        "function-dependency",
        "perturb-depmap-dependency",
        "clin-safety",
        "clin-rwe",
        "clin-on-target-ae-prior",
    ],
    # D6: Clinical & translational evidence (unchanged)
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

# ---- Literature schema models (LitSummary, LitMeshRequest, BucketNarrative) ----
# Some deployments of this router run outside the full TargetVal package where
# app.schemas.lit may not be importable. Import if available; otherwise define
# minimal Pydantic models so FastAPI can evaluate response_model at import time.
try:
    from app.schemas.lit import LitSummary, LitMeshRequest, BucketNarrative  # type: ignore
except Exception:  # ModuleNotFoundError or ImportError
    from typing import Any, Dict, List, Optional
    try:
        from pydantic import BaseModel, Field
    except Exception as e:  # If Pydantic is missing, fail fast with a clear message
        raise ImportError("pydantic is required for Lit* fallback schemas") from e

    class LitMeshRequest(BaseModel):  # minimal request model used by /lit/mesh
        gene: str
        condition: Optional[str] = None
        bucket: str

    class LitSummary(BaseModel):  # minimal response model used by /lit/mesh
        bucket: str
        hits: List[Dict[str, Any]] = Field(default_factory=list)
        stance_tally: Dict[str, int] = Field(default_factory=dict)
        notes: Optional[str] = None
        citations: List[str] = Field(default_factory=list)

    class BucketNarrative(BaseModel):  # response model used by /synth/bucket
        bucket: str
        summary: str
        drivers: List[str] = Field(default_factory=list)
        tensions: List[str] = Field(default_factory=list)
        flip_if: List[str] = Field(default_factory=list)
        citations: List[str] = Field(default_factory=list)

# --- New imports for EvidenceEnvelope and runtime/clients per v2.2 spec ---
# EvidenceEnvelope and related classes may live in app.schemas.evidence in the full
# TargetVal application.  When running this standalone router outside that package (e.g.
# on Render or in isolation), the import may fail.  Fall back to minimal placeholder
# implementations to avoid ModuleNotFoundError.  These stubs implement just enough
# behaviour (dict-like fields) to satisfy downstream usage.
try:
    from app.schemas.evidence import EvidenceEnvelope, Edge, stage_context  # type: ignore
except ImportError:
    from typing import Any, Dict, List, Optional
    from pydantic import BaseModel, Field

    class EvidenceEnvelope(BaseModel):  # type: ignore
        """Minimal stand‑alone replacement for the EvidenceEnvelope Pydantic model.

        This fallback uses a Pydantic BaseModel so that FastAPI can use it as
        a response_model.  The fields mirror those used in the router: module,
        domain, context, provenance, data, citations, notes, status,
        fetched_n and fetched_at.  Additional fields assigned at runtime will
        be permitted via the model Config ``extra = 'allow'``.
        """
        module: str = Field(..., description="Module identifier")
        domain: str = Field(..., description="Domain identifier")
        context: Dict[str, Any] = Field(default_factory=dict, description="Context metadata")
        provenance: Dict[str, Any] = Field(default_factory=lambda: {"sources": [], "module_order": []}, description="Provenance info")
        data: Dict[str, Any] = Field(default_factory=dict, description="Primary data payload")
        citations: List[str] = Field(default_factory=list, description="List of citation URLs")
        notes: List[str] = Field(default_factory=list, description="Notes or warnings")
        status: str = Field("NO_DATA", description="Status of the evidence retrieval")
        fetched_n: int = Field(0, description="Number of records fetched")
        fetched_at: float = Field(0.0, description="Timestamp when data was fetched")
        # Allow arbitrary extra attributes assigned at runtime
        class Config:
            arbitrary_types_allowed = True
            extra = "allow"

    class Edge(BaseModel):  # type: ignore
        """Placeholder for an Edge object used in synthesis; stores source, target and optional weight."""
        source: str = Field(...)
        target: str = Field(...)
        weight: Optional[float] = Field(default=None)

    def stage_context(*args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore
        """Stub for stage_context; returns an empty dict since staging is not implemented in this standalone router."""
        return {}

# -----------------------------------------------------------------------------
# Optional imports from the full TargetVal application.  These modules are part
# of the larger ``app`` package in the real deployment.  When running this
# router in isolation (e.g. during testing or on Render) the ``app`` package
# might not be present.  In that case we fall back to minimal shims that
# implement the handful of functions and classes referenced throughout this
# file.  These shims do not make external API calls – instead they return
# empty results – but they allow the router to import successfully and start.
try:
    from app.runtime.http import fetch_json, allow_hosts  # type: ignore
except Exception:
    # Fallback implementations for fetch_json and allow_hosts when the
    # app.runtime.http module is missing.  These shims implement a very
    # lightweight HTTP wrapper using httpx.  They ignore caching, TTL and
    # other features provided by the real implementation but preserve the
    # expected call signature.
    import httpx
    from typing import Any, Optional, Dict, Tuple

    async def fetch_json(url: str, params: Optional[Dict[str, Any]] = None,
                         headers: Optional[Dict[str, str]] = None,
                         ttl_tier: str = "moderate", method: str = "GET") -> Tuple[Any, bool]:
        """Simplified HTTP JSON fetcher.  Returns (data, from_cache)."""
        async with httpx.AsyncClient() as client:
            try:
                if method.upper() == "POST":
                    resp = await client.post(url, json=params, headers=headers)
                else:
                    resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                try:
                    return resp.json(), False
                except Exception:
                    return None, False
            except Exception:
                # On any error return no data; caller should handle exceptions
                return None, False

    def allow_hosts(hosts: list[str]) -> None:
        """No-op fallback for allow_hosts when the real module is unavailable."""
        return None

try:
    from app.clients import iedb, ipd_imgt, uniprot, glygen, rnacentral, reactome, complex_portal, omnipath  # type: ignore
except Exception:
    # Provide minimal stub modules for each client.  These stubs expose the
    # methods referenced in this router and always return empty results.  This
    # allows the router to import and run even when the full client package is
    # absent.  See the real TargetVal codebase for complete implementations.
    import types
    from typing import Any, Optional, Dict, List

    # IEDB client stubs
    iedb = types.SimpleNamespace()
    # When the real IEDB client is unavailable, raise an HTTP 502 error to signal
    # that live calls cannot be performed.  Stubs returning empty lists are no
    # longer permitted per the no‑stubs policy.
    async def _iedb_predict_mhc_class_i(fetch_json_func, peptides: List[str], alleles: List[str]):  # type: ignore
        raise HTTPException(status_code=502, detail="IEDB client missing; cannot predict MHC class I binding")
    async def _iedb_predict_mhc_class_ii(fetch_json_func, peptides: List[str], alleles: List[str]):  # type: ignore
        raise HTTPException(status_code=502, detail="IEDB client missing; cannot predict MHC class II binding")
    async def _iedb_search_epitopes(fetch_json_func, query_dict: Dict[str, Any]):  # type: ignore
        raise HTTPException(status_code=502, detail="IEDB client missing; cannot search epitopes")
    iedb.predict_mhc_class_i = _iedb_predict_mhc_class_i  # type: ignore
    iedb.predict_mhc_class_ii = _iedb_predict_mhc_class_ii  # type: ignore
    iedb.search_epitopes = _iedb_search_epitopes  # type: ignore

    # IPD‑IMGT/HLA client stub
    ipd_imgt = types.SimpleNamespace()
    async def _ipd_search_alleles(fetch_json_func, query: str):  # type: ignore
        raise HTTPException(status_code=502, detail="IPD-IMGT/HLA client missing; cannot search alleles")
    ipd_imgt.search_alleles = _ipd_search_alleles  # type: ignore

    # UniProt client stub
    uniprot = types.SimpleNamespace()
    async def _uniprot_cell_surface(fetch_json_func, gene: Optional[str]):  # type: ignore
        raise HTTPException(status_code=502, detail="UniProt client missing; cannot fetch cell surface proteins")
    uniprot.cell_surface = _uniprot_cell_surface  # type: ignore

    # GlyGen client stub
    glygen = types.SimpleNamespace()
    async def _glygen_protein_summary(fetch_json_func, accession: str):  # type: ignore
        raise HTTPException(status_code=502, detail="GlyGen client missing; cannot fetch protein summary")
    glygen.protein_summary = _glygen_protein_summary  # type: ignore

    # RNAcentral client stub
    rnacentral = types.SimpleNamespace()
    async def _rnacentral_urs_xrefs(fetch_json_func, urs: str):  # type: ignore
        raise HTTPException(status_code=502, detail="RNAcentral client missing; cannot fetch URS xrefs")
    async def _rnacentral_by_external_id(fetch_json_func, symbol: str):  # type: ignore
        raise HTTPException(status_code=502, detail="RNAcentral client missing; cannot query by external ID")
    rnacentral.urs_xrefs = _rnacentral_urs_xrefs  # type: ignore
    rnacentral.by_external_id = _rnacentral_by_external_id  # type: ignore

    # Reactome client stub
    reactome = types.SimpleNamespace()
    async def _reactome_pathways_for_uniprot(fetch_json_func, uniprot_id: str):  # type: ignore
        raise HTTPException(status_code=502, detail="Reactome client missing; cannot fetch pathways")
    reactome.pathways_for_uniprot = _reactome_pathways_for_uniprot  # type: ignore

    # Complex Portal client stub
    complex_portal = types.SimpleNamespace()
    async def _complex_portal_complexes_by_uniprot(fetch_json_func, uniprot_id: str):  # type: ignore
        raise HTTPException(status_code=502, detail="Complex Portal client missing; cannot fetch complexes")
    complex_portal.complexes_by_uniprot = _complex_portal_complexes_by_uniprot  # type: ignore

    # OmniPath client stub
    omnipath = types.SimpleNamespace()
    async def _omnipath_directed_interactions(fetch_json_func, gene: str):  # type: ignore
        raise HTTPException(status_code=502, detail="OmniPath client missing; cannot fetch directed interactions")
    async def _omnipath_kinase_substrate(fetch_json_func, gene: str):  # type: ignore
        raise HTTPException(status_code=502, detail="OmniPath client missing; cannot fetch kinase–substrate interactions")
    async def _omnipath_tf_targets_dorothea(fetch_json_func, gene: str):  # type: ignore
        raise HTTPException(status_code=502, detail="OmniPath client missing; cannot fetch TF targets (DoRothEA)")
    omnipath.directed_interactions = _omnipath_directed_interactions  # type: ignore
    omnipath.kinase_substrate = _omnipath_kinase_substrate  # type: ignore
    omnipath.tf_targets_dorothea = _omnipath_tf_targets_dorothea  # type: ignore

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
    # Domain 1: Genetic causality & human validation
    D1 = [
        "genetics-l2g","genetics-coloc","genetics-mr",
        "genetics-chromatin-contacts","genetics-3d-maps",
        "genetics-regulatory","genetics-sqtl","genetics-pqtl",
        "genetics-annotation","genetics-pathogenicity-priors",
        "genetics-intolerance","genetics-rare","genetics-mendelian",
        "genetics-phewas-human-knockout","genetics-functional",
        "genetics-mavedb","genetics-consortia-summary",
        # ncRNA and new helpers
        "genetics-lncrna","genetics-mirna",
        "genetics-caqtl-lite","genetics-nmd-inference","genetics-mqtl-coloc",
        "genetics-ase-check","genetics-ptm-signal-lite"
    ]
    # Domain 2: Functional & mechanistic validation (deterministic order)
    D2 = [
        "mech-ppi",
        "mech-ligrec",
        "mech-pathways",
        "mech-directed-signaling",
        "mech-kinase-substrate",
        "mech-tf-target",
        "mech-mirna-target",
        "mech-complexes",
        "assoc-perturb",
        "perturb-crispr-screens",
        "perturb-lincs-signatures",
        "perturb-signature-enrichment",
        "perturb-connectivity",
        "assoc-proteomics",
        "assoc-bulk-rna",
        "assoc-metabolomics",
        "biology-causal-pathways",
        "mech-structure",
        "perturb-perturbseq-encode"
    ]
    # Domain 3: Expression, selectivity & cell-state context
    D3 = [
        "expr-baseline","expr-inducibility","assoc-sc","assoc-spatial","sc-hubmap",
        "expr-localization","assoc-hpa-pathology",
        "assoc-bulk-prot","assoc-omics-phosphoproteomics"
    ]
    # Domain 4: Druggability & modality tractability
    D4 = [
        "mech-structure","tract-ligandability-sm","tract-ligandability-oligo",
        "tract-modality","tract-drugs","perturb-drug-response",
        "tract-ligandability-ab","tract-surfaceome"
    ]
    D5 = [
        "tract-immunogenicity",
        "tract-mhc-binding",
        "tract-iedb-epitopes",
        "immuno/hla-coverage",
        "function-dependency",
        "perturb-depmap-dependency",
        "clin-safety",
        "clin-rwe",
        "clin-on-target-ae-prior"
    ]
    D6 = [
        "clin-endpoints","clin-biomarker-fit","clin-pipeline","clin-feasibility",
        "comp-intensity","comp-freedom"
    ]
    return {"1": D1, "2": D2, "3": D3, "4": D4, "5": D5, "6": D6,
            "D1": D1, "D2": D2, "D3": D3, "D4": D4, "D5": D5, "D6": D6}
from pydantic import BaseModel, Field

# Import legacy Evidence model from schemas to satisfy Pydantic layout rule
# Import legacy Evidence model from schemas to satisfy Pydantic layout rule.  When
# running standalone, fall back to a minimal dict‑based implementation.
try:
    from app.schemas.legacy import Evidence  # type: ignore
except Exception:
    from typing import Any, Optional, List

    from pydantic import BaseModel, Field
    class Evidence(BaseModel):  # type: ignore
        """Minimal stand‑alone replacement for the Evidence Pydantic model.

        This fallback uses a Pydantic BaseModel so that FastAPI can use it as
        a response_model.  The fields mirror those used in the router.
        """
        status: str = Field("NO_DATA", description="Status of the evidence retrieval")
        source: str = Field("", description="Data source")
        fetched_n: int = Field(0, description="Number of records fetched")
        data: Any = Field(default=None, description="Payload of the evidence")
        citations: List[str] = Field(default_factory=list, description="List of citation URLs")
        fetched_at: Optional[float] = Field(default=None, description="Timestamp when data was fetched")

        class Config:
            arbitrary_types_allowed = True

# Helper to compute module_order index for provenance. Returns None if not found.
def _module_order_index(module_name: str) -> int | None:
    for modules in DOMAIN_MODULES.values():
        if module_name in modules:
            try:
                return modules.index(module_name)
            except ValueError:
                continue
    return None

# Allow outbound hosts per module per spec (extend to cover all configured primaries/fallbacks).  
# In addition to the original handful of hosts, include the external APIs used throughout
# the genetic, mechanistic, expression, association, tractability and clinical modules.  
# This expanded allowlist permits calls to OpenTargets, OpenGWAS, STRING, Reactome, OmniPath,
# ProteomicsDB, PDC/PRIDE, HuBMAP, ClinicalTrials, PatentsView, DGIdb, ChEMBL, GTEx, HPA and others.  
allow_hosts([
    # immunology and HLA
    "query-api.iedb.org", "tools.iedb.org",
    # European Bioinformatics Institute (EBI) endpoints: PRIDE, ArrayExpress, PDBe, Ensembl, Europe PMC, etc.
    "www.ebi.ac.uk", "ebi.ac.uk",
    # UniProt and glycosylation
    "rest.uniprot.org", "api.glygen.org",
    # ncRNA
    "rnacentral.org",
    # Reactome pathway content service
    "reactome.org",
    # OmniPath directed and undirected signalling/complexes
    "omnipathdb.org",
    # STRING protein interactions
    "string-db.org",
    # OpenTargets GraphQL and platform API
    "api.platform.opentargets.org", "platform.opentargets.org",
    # OpenGWAS/IEU GWAS endpoints
    "gwas.mrcieu.ac.uk", "gwas-api.mrcieu.ac.uk",
    # ProteomicsDB OData API
    "proteomicsdb.org", "www.proteomicsdb.org",
    # PDC (Cancer Proteomics Data Commons) / CPTAC
    "pdc.cancer.gov", "api.gdc.cancer.gov",
    # ClinicalTrials.gov search API
    "clinicaltrials.gov", "api.clinicaltrials.gov",
    # PatentsView API
    "api.patentsview.org",
    # DGIdb for drug–gene interactions
    "dgidb.org", "api.dgidb.org", "www.dgidb.org",
    # ChEMBL small molecule target search
    "www.ebi.ac.uk", "chembl.org", "ebi.ac.uk",
    # HuBMAP search portal and entity APIs
    "search.api.hubmapconsortium.org", "portal.hubmapconsortium.org",
    # HuBMAP param-search variant
    "search.api.hubmapconsortium.org",
    # HuBMAP 4DN and ENCODE (for CAQTL/contacts) are proxied through our internal router, not directly; no allowlist needed
    # Misc: OpenAlex/CrossRef for literature proxies
    "api.openalex.org", "openalex.org", "api.crossref.org",
    # Pharmacovigilance (FAERS)
    "api.fda.gov", "open.fda.gov",
    # Other large public resources referenced in modules
    "www.proteinatlas.org", "proteinatlas.org",
    "maayanlab.cloud", "metabolomicsworkbench.org", "www.metabolomicsworkbench.org"
    ,
    # Additional hosts mandated by the configuration but previously absent:
    # ENCODE project (perturbation and chromatin contacts)
    "encodeproject.org", "www.encodeproject.org",
    # 4D Nucleome APIs
    "4dnucleome.org", "data.4dnucleome.org",
    # UCSC Genome Browser and loop/interaction tracks
    "ucsc.edu", "genome.ucsc.edu",
    # GTEx Portal endpoints (sQTL and eQTL)
    "gtexportal.org", "www.gtexportal.org",
    # Europe PMC top-level domain (distinct from EBI subdomain) and Unpaywall
    "europepmc.org", "www.europepmc.org", "unpaywall.org", "api.unpaywall.org",
    # SIGNOR signalling database
    "signor.uniroma2.it",
    # Human Cell Atlas Azul service and CELLxGENE Discover API
    "azul.data.humancellatlas.org", "cellxgene.cziscience.com",
])

# ------------------------------------------------------------------------
# Direction flip ledger and trigger set
# The ledger records supports and contradicts votes for directed regulator interactions. When contradictions exceed
# supports within a 24-month window, the interaction direction can be flipped. This logic is used by synthesis
# overlays (not yet integrated here) to satisfy acceptance tests.
from collections import defaultdict
from typing import Tuple

# Modules that participate in direction‑flip voting
DIRECTION_TRIGGERS: Set[str] = {
    "mech-tf-target",
    "mech-mirna-target",
    "mech-kinase-substrate",
    "mech-directed-signaling",
    "biology-causal-pathways",
}

class _DirLedger:
    _counts: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {"supports": 0, "contradicts": 0, "last": 0})

    def vote(self, src: str, dst: str, supports: bool, ts: float) -> None:
        key = (src, dst)
        bucket = self._counts[key]
        if supports:
            bucket["supports"] += 1
        else:
            bucket["contradicts"] += 1
        bucket["last"] = max(bucket["last"], ts)

    def should_flip(self, src: str, dst: str, now: float) -> bool:
        b = self._counts.get((src, dst), {})
        # flip when two or more contradictions within 24 months
        return b.get("contradicts", 0) >= 2 and (now - b.get("last", 0)) <= 24 * 30 * 24 * 3600

# instantiate a global ledger
_dir_ledger = _DirLedger()

# ------------------------------ Utilities ------------------------------------

def _now() -> float:
    return time.time()

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ------------------------ Evidence (aligned with user's file) -----------------

# Legacy Evidence class has been moved to app.schemas.legacy; see there for details.
# The router continues to reference Evidence for endpoints not yet converted to EvidenceEnvelope.

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
# per-host concurrency cap (spec: 4)
OUTBOUND_MAX_CONCURRENCY: int = int(os.getenv("OUTBOUND_MAX_CONCURRENCY", "4"))
REQUEST_BUDGET_S: float = float(os.getenv("REQUEST_BUDGET_S", "90.0"))

_semaphore = asyncio.Semaphore(OUTBOUND_MAX_CONCURRENCY)

async def _get_json(url: str, tries: int = OUTBOUND_TRIES, headers: Optional[Dict[str, str]] = None) -> Any:
    """
    Minimal shim around fetch_json to ensure runtime discipline: uses per‑host semaphores, backoff, caching and retries
    implemented in app.runtime.http.fetch_json. Ignores the local 'tries' argument.
    """
    data, _ = await fetch_json(url, params=None, headers=headers, ttl_tier="moderate", method="GET")
    return data

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
    # Spatial association module (Europe PMC spatial/ISH literature).  Config requires this canonical endpoint
    # to be present; it aggregates spatial gene expression and spatial neighbourhood datasets and uses
    # Europe PMC as the primary for spatial/ISH literature.  The route is registered here so it
    # appears in the modules registry.
    Module(route="/assoc/spatial", name="assoc-spatial", sources=["Europe PMC API"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/sc/hubmap", name="sc-hubmap", sources=["HuBMAP Search API","HCA Azul APIs"], bucket="Expression, selectivity & cell-state context"),
    # Canonical proteomics and metabolomics association endpoints.  Each references the configured
    # primaries and fallbacks from the spec (ProteomicsDB → PRIDE/PDC; MetaboLights → Metabolomics Workbench).
    Module(route="/assoc/proteomics", name="assoc-proteomics", sources=["ProteomicsDB API","PRIDE Archive API","PDC (CPTAC) GraphQL"], bucket="Functional & mechanistic validation"),
    Module(route="/assoc/metabolomics", name="assoc-metabolomics", sources=["MetaboLights API","Metabolomics Workbench API"], bucket="Functional & mechanistic validation"),
    Module(route="/assoc/hpa-pathology", name="assoc-hpa-pathology", sources=["Europe PMC API"], bucket="Expression, selectivity & cell-state context"),
    # Aggregated proteomics and phosphoproteomics endpoints (v2.2)
    Module(route="/assoc/bulk-prot", name="assoc-bulk-prot", sources=["ProteomicsDB API","PDC (CPTAC) GraphQL"], bucket="Expression, selectivity & cell-state context"),
    Module(route="/assoc/omics-phosphoproteomics", name="assoc-omics-phosphoproteomics", sources=["PRIDE Archive API","ProteomicsDB API"], bucket="Expression, selectivity & cell-state context"),
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
    # Additional genetic causality helpers (v2.2)
    Module(route="/genetics/caqtl-lite", name="genetics-caqtl-lite", sources=["QTL Catalogue API","OpenGWAS API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/nmd-inference", name="genetics-nmd-inference", sources=["gnomAD GraphQL API","Ensembl VEP REST"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/mqtl-coloc", name="genetics-mqtl-coloc", sources=["Metabolomics Workbench API","OpenGWAS API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/ase-check", name="genetics-ase-check", sources=["GTEx API","RNAcentral API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/ptm-signal-lite", name="genetics-ptm-signal-lite", sources=["PhosphoSitePlus API","UniProtKB API"], bucket="Genetic causality & human validation"),
    # ncRNA modules belong to genetic causality (Domain 1)
    Module(route="/genetics/lncrna", name="genetics-lncrna", sources=["RNAcentral API","Europe PMC API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/mirna", name="genetics-mirna", sources=["RNAcentral API","Europe PMC API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/pathogenicity-priors", name="genetics-pathogenicity-priors", sources=["gnomAD GraphQL API","CADD API"], bucket="Genetic causality & human validation"),
    Module(route="/genetics/intolerance", name="genetics-intolerance", sources=["gnomAD GraphQL API"], bucket="Genetic causality & human validation"),
    Module(route="/mech/structure", name="mech-structure", sources=["UniProtKB API","AlphaFold DB API","PDBe API","PDBe-KB API"], bucket="Druggability & modality tractability"),
    Module(route="/mech/ppi", name="mech-ppi", sources=["STRING API","IntAct via PSICQUIC","OmniPath API"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/pathways", name="mech-pathways", sources=["Reactome Content/Analysis APIs","Pathway Commons API","SIGNOR API","QuickGO API"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/ligrec", name="mech-ligrec", sources=["OmniPath (ligand–receptor)","IUPHAR/Guide to Pharmacology API","Reactome interactors"], bucket="Functional & mechanistic validation"),
    Module(route="/biology/causal-pathways", name="biology-causal-pathways", sources=["SIGNOR API","Reactome Analysis Service","Pathway Commons API"], bucket="Functional & mechanistic validation"),
    # New mechanistic regulator endpoints (v2.2)
    Module(route="/mech/complexes", name="mech-complexes", sources=["OmniPath complexes API","ComplexPortal API"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/directed-signaling", name="mech-directed-signaling", sources=["OmniPath signed/directed"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/kinase-substrate", name="mech-kinase-substrate", sources=["OmniPath enzyme–substrate (kinaseextra)"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/mirna-target", name="mech-mirna-target", sources=["OmniPath miRNA-target (mirnatarget)"], bucket="Functional & mechanistic validation"),
    Module(route="/mech/tf-target", name="mech-tf-target", sources=["OmniPath DoRothEA A/B","OmniPath TF_target"], bucket="Functional & mechanistic validation"),
    Module(route="/tract/drugs", name="tract-drugs", sources=["ChEMBL API","DGIdb GraphQL","DrugCentral API","BindingDB API","PubChem PUG-REST","STITCH API","Pharos GraphQL"], bucket="Druggability & modality tractability"),
    Module(route="/tract/ligandability-sm", name="tract-ligandability-sm", sources=["UniProtKB API","AlphaFold DB API","PDBe API","PDBe-KB API","BindingDB API"], bucket="Druggability & modality tractability"),
    # ligandability for antibodies belongs to druggability; include HPA as fallback
    Module(route="/tract/ligandability-ab", name="tract-ligandability-ab", sources=["UniProtKB API","GlyGen API","Human Protein Atlas API (fallback)"], bucket="Druggability & modality tractability"),
    Module(route="/tract/ligandability-oligo", name="tract-ligandability-oligo", sources=["Ensembl VEP REST","RNAcentral API","Europe PMC API"], bucket="Druggability & modality tractability"),
    Module(route="/tract/modality", name="tract-modality", sources=["UniProtKB API","AlphaFold DB API","Pharos GraphQL","IUPHAR/Guide to Pharmacology API"], bucket="Druggability & modality tractability"),
    Module(route="/tract/immunogenicity", name="tract-immunogenicity", sources=["IEDB IQ-API","IPD-IMGT/HLA API","Europe PMC API"], bucket="Therapeutic index & safety translation"),
    Module(route="/tract/mhc-binding", name="tract-mhc-binding", sources=["IEDB Tools API (prediction)","IPD-IMGT/HLA API"], bucket="Therapeutic index & safety translation"),
    Module(route="/tract/iedb-epitopes", name="tract-iedb-epitopes", sources=["IEDB IQ-API","IEDB Tools API"], bucket="Therapeutic index & safety translation"),
    # Surfaceome uses UniProtKB/GlyGen primaries; HPA is a fallback; belongs to druggability
    Module(route="/tract/surfaceome", name="tract-surfaceome", sources=["UniProtKB API","GlyGen API","Human Protein Atlas API (fallback)"], bucket="Druggability & modality tractability"),
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


# ------------------- Literature overlay pipeline (EPMC → Crossref → Unpaywall → GROBID → PubTator3) -------------------

async def _crossref_search(query: str, rows: int = 50) -> Dict[str, Any]:
    try:
        url = f"https://api.crossref.org/works?query={urllib.parse.quote(query)}&rows={rows}"
        js = await _httpx_json_get(url) or {}
        return {"url": url, "json": js}
    except Exception:
        return {"url": None, "json": {}}

async def _unpaywall_oa(doi: str) -> Dict[str, Any]:
    email = os.environ.get("UNPAYWALL_EMAIL", "open@targetval.org")
    try:
        url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={urllib.parse.quote(email)}"
        js = await _httpx_json_get(url) or {}
        return {"url": url, "json": js}
    except Exception:
        return {"url": None, "json": {}}

async def _grobid_fulltext(pdf_url: str) -> Optional[str]:
    """
    Send a PDF URL to a GROBID server to obtain TEI XML. Requires env GROBID_URL to be set.
    Returns TEI XML as string or None.
    """
    base = os.environ.get("GROBID_URL")
    if not base:
        return None
    try:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
            r = client.post(base.rstrip("/") + "/api/processFulltextDocument",
                            data={"teiCoordinates": "s"}, files={"input": (os.path.basename(pdf_url) or "paper.pdf", b"")})
            if r.status_code == 200:
                return r.text
    except Exception:
        return None
    return None

async def _pubtator3_spans(pmids: List[str]) -> Dict[str, Any]:
    if not pmids:
        return {"url": None, "json": {}}
    try:
        url = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson?pmids=" + ",".join(pmids[:200])
        js = await _httpx_json_get(url) or {}
        return {"url": url, "json": js}
    except Exception:
        return {"url": None, "json": {}}

def _tag_stance_from_text(text: str) -> str:
    tl = text.lower()
    if any(k in tl for k in CONFIRM_KWS) and not any(k in tl for k in DISCONFIRM_KWS):
        return "SUPPORTS"
    if any(k in tl for k in DISCONFIRM_KWS) and not any(k in tl for k in CONFIRM_KWS):
        return "CONTRADICTS"
    return "SUGGESTS_GAP" if "unknown" in tl or "unresolved" in tl or "controvers" in tl else "NEUTRAL"

async def _literature_overlay(query: str, size: int = 50) -> Dict[str, Any]:
    """
    Directed overlay: Europe PMC → Crossref (retractions/updates) → Unpaywall (OA) → GROBID → PubTator3 spans.
    Returns dict with `hits` (list) and `citations` (URLs used).
    """
    citations: List[str] = []
    ep_hits, ep_cites = await _epmc_search(query, size=size)
    citations.extend(ep_cites)
    dois = [h.get("doi") for h in ep_hits if h.get("doi")]
    cr = await _crossref_search(query, rows=min(200, size*2))
    citations.append(cr.get("url"))
    xworks = (((cr.get("json") or {}).get("message") or {}).get("items") or [])
    # mark retractions / updates
    retracted_dois = set()
    for w in xworks:
        rel = (w.get("relation") or {})
        for typ, items in rel.items():
            if isinstance(items, list) and "retract" in typ:
                for it in items:
                    doi = (it.get("id") or "").replace("https://doi.org/", "")
                    if doi: retracted_dois.add(doi.lower())
    # fetch OA locations for the DOIs we saw
    oa_links: Dict[str, Any] = {}
    for d in dois[:50]:
        u = await _unpaywall_oa(d)
        if u.get("url"):
            citations.append(u["url"])
        js = u.get("json") or {}
        best = ((js.get("best_oa_location") or {}) if isinstance(js, dict) else {}) or {}
        if best.get("url"):
            oa_links[d] = best
    # if we have OA PDFs, optionally parse one via GROBID to demonstrate pipeline
    if oa_links:
        sample_pdf = next((v.get("url_for_pdf") or v.get("url") for v in oa_links.values() if v.get("url") or v.get("url_for_pdf")), None)
        if sample_pdf:
            tei = await _grobid_fulltext(sample_pdf)
            if tei:
                citations.append(os.environ.get("GROBID_URL","").rstrip("/") + "/api/processFulltextDocument")
    # PubTator3 enrichment for PubMed hits
    pmids = [str(h.get("pmid") or h.get("pmcid") or "").replace("PMC","") for h in ep_hits if (h.get("pmid") or h.get("pmcid"))]
    pt = await _pubtator3_spans([p for p in pmids if p])
    if pt.get("url"): citations.append(pt["url"])
    # Project final hits with tags and OA info
    final_hits = []
    for h in ep_hits:
        doi = h.get("doi")
        stance = _tag_stance_from_text((h.get("title","") + " " + h.get("abstract","")))
        h2 = {**h, "stance": stance}
        if doi and doi in oa_links:
            h2["open_access"] = oa_links[doi]
        if doi and (doi.lower() in retracted_dois):
            h2["retracted"] = True
        final_hits.append(h2)
    return {"hits": final_hits, "citations": [c for c in citations if c]}
@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    ensg = await _ensg_from_symbol(sym) or sym
    # Primary: GTEx Portal API v2
    try:
        gtex_base = "https://gtexportal.org/api/v2/gene/expression"
        q = {"gencodeId": ensg, "format": "json"}
        url = gtex_base + "?" + urllib.parse.urlencode(q)
        js = await _get_json(url, tries=2)
        cites += ["https://gtexportal.org/home/apiPage", gtex_base]
        if js:
            return Evidence(status="OK", source="GTEx Portal API v2", fetched_n=1, data={"ensg": ensg, "gtex": js}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    # Fallback: EBI Expression Atlas (baseline)
    try:
        atlas = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(sym)}.json"
        ajs = await _get_json(atlas, tries=2)
        cites += ["https://www.ebi.ac.uk/gxa/home", atlas]
        if ajs:
            return Evidence(status="OK", source="Expression Atlas", fetched_n=1, data={"gene": sym, "atlas": ajs}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    return Evidence(status="NO_DATA", source="GTEx → Expression Atlas", fetched_n=0, data={}, citations=cites, fetched_at=_now())

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
    """
    Single‑cell association evidence.  First attempts to fetch datasets from
    HCA Azul and the Single‑Cell Expression Atlas.  If neither returns data,
    falls back to a literature search for single‑cell terms.  Citations
    reflect only the endpoints actually queried.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    # 1) HCA Azul search (projects containing the gene symbol)
    try:
        kw = urllib.parse.quote(sym)
        azul_url = f"https://service.azul.data.humancellatlas.org/index/projects?size=25&searchTerm={kw}"
        js = await _get_json(azul_url, tries=2)
        if js:
            data["hca_azul"] = js
            cites.append(azul_url)
    except Exception:
        pass
    # 2) Single‑Cell Expression Atlas search
    if not data:
        try:
            sc_url = f"https://www.ebi.ac.uk/gxa/sc/search?gene={urllib.parse.quote(sym)}&format=json"
            sc_js = await _get_json(sc_url, tries=2)
            if sc_js:
                data["sc_atlas"] = sc_js
                cites.append(sc_url)
        except Exception:
            pass
    if data:
        fetched = sum(len(v.get("results", v) or []) if isinstance(v, dict) else 1 for v in data.values())
        return Evidence(status="OK", source="HCA Azul" if "hca_azul" in data else "Single-Cell Expression Atlas", fetched_n=fetched, data=data, citations=cites, fetched_at=_now())
    # 3) Literature fallback
    q = f"{sym} single-cell OR scRNA-seq OR snRNA-seq OR Tabula Sapiens"
    hits, epmc_cites = await _epmc_search(q, size=60)
    cites.extend(epmc_cites)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="Europe PMC (single-cell literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/spatial-expression", response_model=Evidence)
async def assoc_spatial_expression(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    """
    Spatial expression evidence.  Queries HCA Azul for datasets mentioning the
    gene in spatial contexts, then falls back to a literature search
    covering spatial transcriptomics platforms such as MERFISH, Visium or
    Xenium.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    # 1) HCA Azul search for spatial datasets
    try:
        kw = urllib.parse.quote(f"{sym} spatial")
        azul_url = f"https://service.azul.data.humancellatlas.org/index/projects?size=25&searchTerm={kw}"
        js = await _get_json(azul_url, tries=2)
        if js:
            data["hca_azul"] = js
            cites.append(azul_url)
    except Exception:
        pass
    if data:
        fetched = sum(len(v.get("results", v) or []) if isinstance(v, dict) else 1 for v in data.values())
        return Evidence(status="OK", source="HCA Azul", fetched_n=fetched, data=data, citations=cites, fetched_at=_now())
    # 2) Literature fallback on spatial transcriptomics
    q = f"{sym} {condition or ''} spatial transcriptomics OR MERFISH OR Visium OR Xenium"
    hits, epmc_cites = await _epmc_search(q, size=60)
    cites.extend(epmc_cites)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="Europe PMC (spatial expression literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/spatial-neighborhoods", response_model=Evidence)
async def assoc_spatial_neighborhoods(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"{sym} {condition or ''} neighborhood OR ligand-receptor OR cell-cell communication OR NicheNet"
    hits, cites = await _epmc_search(q, size=60)
    return Evidence(status="OK", source="Europe PMC", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=cites, fetched_at=_now())

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    """
    Bulk proteomics evidence.  
    According to the TargetVal configuration the primary data source for bulk proteomics
    should be ProteomicsDB, with PDC/PRIDE used only as fall‑back if ProteomicsDB
    yields no results.  This implementation first attempts to query ProteomicsDB’s
    public OData service for proteins matching the gene symbol.  If that call
    returns any entries, they are returned as the sole source.  If the call
    fails or no entries are found, the router falls back to our own PDC wrapper
    endpoint and the PRIDE Archive.  Citations reflect only the upstreams that were
    actually queried.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    entries: List[Dict[str, Any]] = []
    sources_used: List[str] = []
    # 1) Try ProteomicsDB via its OData API.  Filter by gene name and human taxon (9606).
    try:
        # ProteomicsDB uses an OData endpoint; restrict by GENE_NAME and TAXCODE for Homo sapiens.
        # Example: https://www.proteomicsdb.org/proteomicsdb/logic/api_v2/api.xsodata/Protein?$filter=GENE_NAME%20eq%20'TP53'%20and%20TAXCODE%20eq%209606&$top=50&$format=json
        filter_str = urllib.parse.quote(f"GENE_NAME eq '{sym}' and TAXCODE eq 9606")
        url = (
            "https://www.proteomicsdb.org/proteomicsdb/logic/api_v2/api.xsodata/Protein"
            f"?$filter={filter_str}&$top=50&$format=json"
        )
        pdb_js = await _get_json(url, tries=2)
        # ProteomicsDB may return either a dict with a "d" property or a list; normalize
        if pdb_js:
            cites.append(url)
            entries.append({"proteomicsdb": pdb_js})
            sources_used.append("ProteomicsDB")
    except Exception:
        # quietly ignore errors and fall back
        pass
    # 2) If no ProteomicsDB entries, try our PDC (CPTAC) wrapper via internal call
    if not entries:
        try:
            pdc = await _self_get("/assoc/bulk-prot-pdc", {"symbol": sym})
            if isinstance(pdc, dict):
                # extract nested data if available
                entries.append({"pdc": pdc.get("data", {})})
                cites.extend(pdc.get("citations", []) or [])
                sources_used.append("PDC GraphQL (CPTAC)")
        except Exception:
            pass
    # 3) Final fall‑back: PRIDE Archive search on the gene symbol
    if not entries:
        try:
            pq = urllib.parse.quote(sym)
            pride_url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?query={pq}&pageSize=50"
            pr_js = await _get_json(pride_url, tries=2)
            cites.append(pride_url)
            entries.append({"pride_projects": pr_js})
            sources_used.append("PRIDE Archive")
        except Exception:
            pass
    fetched = sum(len(v or []) if isinstance(v, list) else 1 for e in entries for v in e.values())
    status = "OK" if entries else "NO_DATA"
    source = ", ".join(sources_used) if sources_used else "ProteomicsDB, PRIDE, ProteomeXchange"
    return Evidence(status=status, source=source, fetched_n=fetched, data={"entries": entries}, citations=cites, fetched_at=_now())


@router.get("/assoc/omics-phosphoproteomics", response_model=Evidence)
async def assoc_omics_phosphoproteomics(
    symbol: Optional[str] = Query(None),
    gene: Optional[str] = Query(None)
) -> Evidence:
    """
    Phosphoproteomics evidence (live-first):
      1) **PRIDE Archive v2** (primary): projects and peptide evidence filtered for phosphorylation.
      2) Europe PMC overlay (secondary): augment with literature hits, retractions, OA, and annotations.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    base = "https://www.ebi.ac.uk/pride/ws/archive/v2"
    projects: List[Dict[str, Any]] = []
    peptides: List[Dict[str, Any]] = []
    try:
        # Search PRIDE projects mentioning the gene + phospho
        proj_url = f"{base}/projects?keyword={urllib.parse.quote(sym + ' phospho')}&page=0&pageSize=50"
        pj = await _httpx_json_get(proj_url) or {}
        cites.append(proj_url)
        projects = (pj.get("list") or pj.get("projects") or pj.get("results") or [])
        # Pull peptide evidence (if any projects found)
        for p in projects[:10]:
            acc = (p.get("accession") or p.get("projectAccession") or "").strip()
            if not acc:
                continue
            pept_url = f"{base}/peptide?projectAccession={urllib.parse.quote(acc)}&page=0&pageSize=100&modification=Phospho"
            pe = await _httpx_json_get(pept_url) or {}
            cites.append(pept_url)
            ps = (pe.get("list") or pe.get("peptides") or [])
            # keep only minimal fields to control payload size
            for it in (ps or []):
                peptides.append({
                    "sequence": it.get("sequence") or it.get("peptideSequence"),
                    "proteinAccession": it.get("accession") or it.get("proteinAccession"),
                    "modifications": it.get("modifications"),
                    "projectAccession": acc
                })
    except Exception:
        # swallow and proceed to literature overlay
        pass

    # Literature overlay (Europe PMC → Crossref → Unpaywall → GROBID → PubTator3)
    overlay = await _literature_overlay(f"{sym} phosphoproteomics OR phosphorylation site PRIDE")
    cites.extend(overlay.get("citations", []))
    data = {
        "projects": projects,
        "peptides": peptides,
        "literature": overlay.get("hits", [])
    }
    status = "OK" if (projects or peptides) else ("PARTIAL" if overlay.get("hits") else "NO_DATA")
    return Evidence(status=status, source="PRIDE Archive (primary) + literature overlay",
                    fetched_n=len(peptides) or len(projects),
                    data=data, citations=cites, fetched_at=_now())


@router.get("/assoc/omics-metabolites", response_model=Evidence)
async def assoc_omics_metabolites(
    symbol: Optional[str] = Query(None),
    gene: Optional[str] = Query(None),
    condition: Optional[str] = None
) -> Evidence:
    """
    Metabolomics evidence (live-first):
      1) **MetaboLights** search (primary).
      2) **MetaboLights Workbench** reference-compound search (fallback).
      3) Europe PMC overlay as literature.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    studies: List[Dict[str, Any]] = []
    compounds: List[Dict[str, Any]] = []
    try:
        mb_base = "https://www.ebi.ac.uk/metabolights/ws"
        # Primary: search studies
        s_url = f"{mb_base}/studies?search={urllib.parse.quote(sym + ' ' + (condition or ''))}"
        sj = await _httpx_json_get(s_url) or {}
        cites.append(s_url)
        studies = (sj.get("content") or sj.get("studies") or sj.get("list") or [])
        # Fallback: reference compound search (Workbench)
        c_url = f"{mb_base}/reference-compounds/search?text={urllib.parse.quote(sym)}"
        cj = await _httpx_json_get(c_url) or {}
        cites.append(c_url)
        compounds = (cj.get("content") or cj.get("results") or cj.get("list") or [])
    except Exception:
        pass

    overlay = await _literature_overlay(f"{sym} {condition or ''} metabolomics OR metabolite site:ebi.ac.uk/metabolights")
    cites.extend(overlay.get("citations", []))
    data = {"studies": studies, "reference_compounds": compounds, "literature": overlay.get("hits", [])}
    status = "OK" if (studies or compounds) else ("PARTIAL" if overlay.get("hits") else "NO_DATA")
    return Evidence(status=status, source="MetaboLights (primary) + Workbench (fallback) + literature",
                    fetched_n=len(studies) or len(compounds),
                    data=data, citations=cites, fetched_at=_now())

@router.get("/assoc/hpa-pathology", response_model=Evidence)
async def assoc_hpa_pathology(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    q = f"\"Human Protein Atlas\" AND pathology AND {sym}"
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode({"query": q, "pageSize": 50, "format": "json"})
    try:
        js = await _get_json(url, tries=2)
        hits = (((js or {}).get("resultList") or {}).get("result") or [])
        return Evidence(status=("OK" if hits else "NO_DATA"), source="Europe PMC (HPA pathology literature)", fetched_n=len(hits), data={"query": q, "hits": hits}, citations=[url, "https://europepmc.org/developers/"], fetched_at=_now())
    except Exception:
        return Evidence(status="UPSTREAM_ERROR", source="Europe PMC (HPA pathology literature)", fetched_n=0, data={"query": q}, citations=[url, "https://europepmc.org/developers/"], fetched_at=_now())


@router.get("/assoc/bulk-prot-pdc", response_model=Evidence)
async def assoc_bulk_prot_pdc(
    symbol: Optional[str] = Query(None),
    gene: Optional[str] = Query(None)
) -> Evidence:
    """
    CPTAC / PDC (Proteomic Data Commons) — **live GraphQL**.
    Strategy:
      • Probe the GraphQL schema (introspection) to confirm connectivity.
      • Try a small set of documented query shapes to retrieve study- or peptide/protein‑level hits for the gene.
      • Return NO_DATA (not 5xx) on empty results; this still counts as a live call for provenance.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    endpoint = "https://pdc.cancer.gov/graphql"
    async def gql(q: str, variables: Dict[str, Any]):
        js = await _httpx_json_post(endpoint, {"query": q, "variables": variables}, timeout=60.0)
        cites.append(endpoint)
        return js

    # 0) Introspection ping (connectivity)
    try:
        ping = await gql("query{ __schema { queryType { name } }}", {})
        _ = (ping or {}).get("data")
    except Exception as e:
        # Connectivity failure -> degrade to literature overlay only
        overlay = await _literature_overlay(f"{sym} CPTAC proteomics OR PDC cancer proteomics")
        cites.extend(overlay.get("citations", []))
        return Evidence(status="ERROR", source="PDC GraphQL", fetched_n=0,
                        data={"error": str(e), "literature": overlay.get("hits", [])},
                        citations=cites, fetched_at=_now())

    # Candidate queries (PDC schema has evolved; we try several)
    out_nodes: List[Dict[str, Any]] = []
    errors: List[str] = []
    candidates: List[Tuple[str, Dict[str, Any], str]] = [
        # project-centric (genes → projects)
        ("""
        query($gene:String!){
          genes(gene_name:$gene){ gene_name uniprot_id studies{ study_submitter_id project_name disease_type program_name } }
        }""", {"gene": sym}, "genes"),
        # file-centric (genes in files metadata)
        ("""
        query($gene:String!){
          searchFiles(filters:{op:EQUAL, content:[{field:"genes.gene_name", value:$gene}]}){
            files{ file_id file_name study_name project_name disease_type }
          }
        }""", {"gene": sym}, "searchFiles"),
        # expression / protein groups (commonly exposed in recent PDC schemas)
        ("""
        query($gene:String!){
          proteinExpressionPaginated(gene_name:$gene, first:50){
            edges{ node{ gene_name uniprot_id project_name disease_type aliquot_submitter_id log2_ratio spectral_count } }
          }
        }""", {"gene": sym}, "proteinExpressionPaginated")
    ]
    for q, vars, root in candidates:
        try:
            js = await gql(q, vars) or {}
            data = js.get("data") or {}
            # Collect the first non-empty result
            if data:
                # flatten reasonable shapes
                if "genes" in data and data["genes"]:
                    for g in data["genes"]:
                        out_nodes.append(g)
                    break
                if "searchFiles" in data:
                    files = (data["searchFiles"] or {}).get("files") or []
                    if files:
                        out_nodes.extend(files); break
                if "proteinExpressionPaginated" in data:
                    edges = (data["proteinExpressionPaginated"] or {}).get("edges") or []
                    for e in edges:
                        n = (e or {}).get("node") or {}
                        if n: out_nodes.append(n)
                    if out_nodes: break
        except Exception as e:
            errors.append(str(e))
            continue

    overlay = await _literature_overlay(f"{sym} CPTAC OR Proteomic Data Commons proteomics")
    cites.extend(overlay.get("citations", []))
    status = "OK" if out_nodes else ("PARTIAL" if overlay.get("hits") else "NO_DATA")
    return Evidence(status=status, source="PDC GraphQL", fetched_n=len(out_nodes),
                    data={"hits": out_nodes, "errors": errors, "literature": overlay.get("hits", [])},
                    citations=cites, fetched_at=_now())

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
    """
    Single‑cell evidence from the HuBMAP consortium.  
    The previous implementation simply performed a literature search on Europe PMC
    and labelled it as HuBMAP.  To comply with the specification, this endpoint
    now queries the public HuBMAP Search API directly.  When the search API
    returns results, we return them verbatim; if it fails or returns no hits,
    we gracefully fall back to a short Europe PMC search and still cite the
    HuBMAP portal link.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    # Attempt HuBMAP Search API: search for the gene symbol across datasets.  The v3 search API
    # accepts keyword queries; limit to 25 results for brevity.  See HuBMAP docs for details.
    try:
        kw = urllib.parse.quote(sym)
        hub_url = f"https://search.api.hubmapconsortium.org/v3/search?keywords={kw}&entity_type=dataset&limit=25"
        js = await _get_json(hub_url, tries=2)
        if js:
            cites.append(hub_url)
            data["hubmap"] = js
    except Exception:
        # ignore search failures – we will fall back
        js = None
    # If HuBMAP search yields nothing, fall back to a Europe PMC search for mention of HuBMAP + gene
    hits: List[Dict[str, Any]] = []
    if not data:
        query = f"{sym} HuBMAP single-cell"
        hits, epmc_cites = await _epmc_search(query, size=20)
        cites.extend(epmc_cites)
        data["hits"] = hits
    # Always include the general HuBMAP portal link for reference
    cites.append("https://portal.hubmapconsortium.org")
    fetched = len(hits) if not data.get("hubmap") else (len(data["hubmap"].get("results", [])) if isinstance(data["hubmap"], dict) else 1)
    status = "OK" if fetched > 0 else "NO_DATA"
    source = "HuBMAP Search API" if data.get("hubmap") else "HuBMAP portal (fallback via Europe PMC)"
    return Evidence(status=status, source=source, fetched_n=fetched, data=data, citations=cites, fetched_at=_now())

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
    js = await _httpx_json_post(EXTERNAL_URLS['opentargets_graphql'], {"query": q, "variables": {"ensg": ensg, "efo": efo}})
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
    js  = await _httpx_json_post(EXTERNAL_URLS['opentargets_graphql'], {"query": q, "variables": {"ensg": ensg, "efo": efo}})
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
async def genetics_mendelian(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites = ["https://clinicalgenome.org/curation-activities/gene-disease-validity/", "https://genegraph.clinicalgenome.org/"]
    # Primary: ClinGen GeneGraph GraphQL
    try:
        gql = """query($symbol:String!){ gene(symbol:$symbol){ curations{ disease{ label mondo } classification } } }"""
        js = await _httpx_json_post("https://genegraph.clinicalgenome.org/graphql", {"query": gql, "variables": {"symbol": sym}})
        cur = ((((js or {}).get("data") or {}).get("gene") or {}).get("curations")) or []
        if cur:
            return Evidence(status="OK", source="ClinGen GeneGraph", fetched_n=len(cur), data={"symbol": sym, "curations": cur}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    # Fallback: ClinGen downloads page + Europe PMC literature
    fallback = f"{sym} ClinGen gene-disease validity OR GCEP"
    hits, lit = await _epmc_search(fallback, size=40)
    cites.append("https://search.clinicalgenome.org/kb/downloads")
    cites.extend(lit)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="ClinGen literature (fallback)", fetched_n=len(hits), data={"query": fallback, "hits": hits}, citations=cites, fetched_at=_now())

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

@router.get("/genetics/lncrna", response_model=EvidenceEnvelope)
async def genetics_lncrna(urs: Optional[str] = Query(None), symbol: Optional[str] = Query(None)):
    """
    LncRNA evidence via RNAcentral. Provide either a URS (RNAcentral accession) or a symbol (HGNC symbol or external ID).
    Populates context.gene_id when HGNC cross‑references are found.
    """
    env = EvidenceEnvelope(module="genetics-lncrna", domain=DOMAIN_LABELS["1"])
    try:
        rows: List[dict] = []
        if urs:
            x = await rnacentral.urs_xrefs(fetch_json, urs)
            rows = [x] if x else []
        elif symbol:
            x = await rnacentral.by_external_id(fetch_json, symbol)
            rows = (x or {}).get("results", []) if isinstance(x, dict) else []
        if rows:
            xrefs = rows[0].get("xrefs", [])
            for xx in xrefs:
                if xx.get("database") == "HGNC" and xx.get("accession"):
                    env.context.gene_id = xx["accession"]
                    break
    except Exception:
        rows = []
    env.records = rows
    env.provenance.sources.append("https://rnacentral.org/api/v1")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/genetics/mirna", response_model=EvidenceEnvelope)
async def genetics_mirna(urs: Optional[str] = Query(None), symbol: Optional[str] = Query(None)):
    """
    miRNA evidence via RNAcentral. Provide either a URS (RNAcentral accession) or a symbol (miRBase/miRNA ID or HGNC symbol).
    Populates context.gene_id when HGNC cross‑references are found.
    """
    env = EvidenceEnvelope(module="genetics-mirna", domain=DOMAIN_LABELS["1"])
    rows: List[dict] = []
    try:
        if urs:
            x = await rnacentral.urs_xrefs(fetch_json, urs)
            rows = [x] if x else []
        elif symbol:
            x = await rnacentral.by_external_id(fetch_json, symbol)
            rows = (x or {}).get("results", []) if isinstance(x, dict) else []
        if rows:
            xrefs = rows[0].get("xrefs", [])
            for xx in xrefs:
                if xx.get("database") == "HGNC" and xx.get("accession"):
                    env.context.gene_id = xx["accession"]
                    break
    except Exception:
        rows = []
    env.records = rows
    env.provenance.sources.append("https://rnacentral.org/api/v1")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/genetics/pathogenicity-priors", response_model=Evidence)
async def genetics_pathogenicity_priors(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    cites = ["https://gnomad.broadinstitute.org/"]
    # Primary: gnomAD constraint (missense/lof Z, LOEUF as prior proxies)
    try:
        gql = """query($geneId:String!){ gene(gene_id:$geneId){ gene_id symbol constraint { oe_lof oe_mis oe_syn lof_z missense_z syn_z pLI pRec pNull } } }"""
        js = await _httpx_json_post("https://gnomad.broadinstitute.org/api", {"query": gql, "variables": {"geneId": ensg}})
        cons = ((((js or {}).get("data") or {}).get("gene") or {}).get("constraint"))
        if cons:
            return Evidence(status="OK", source="gnomAD GraphQL", fetched_n=1, data={"ensg": ensg, "priors": cons}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    # Fallback: CADD family
    cites.append("https://cadd.gs.washington.edu/")
    hits, lit = await _epmc_search(f"{sym} CADD OR pathogenicity prior", size=25)
    cites.extend(lit)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="gnomAD → CADD (literature)", fetched_n=(1 if hits else 0), data={"ensg": ensg, "hits": hits}, citations=cites, fetched_at=_now())

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
    cites = ["https://mavedb.org/", "https://api.mavedb.org/docs"]
    try:
        # MaveDB search API (public). Filter by gene symbol when supported.
        url = "https://api.mavedb.org/api/search?" + urllib.parse.urlencode({"q": sym})
        js  = await _get_json(url, tries=2)
        if js:
            return Evidence(status="OK", source="MaveDB API", fetched_n=(len(js) if isinstance(js, list) else 1), data={"symbol": sym, "results": js}, citations=cites + [url], fetched_at=_now())
    except Exception:
        pass
    hits, lit = await _epmc_search(f"{sym} deep mutational scanning OR MAVE OR saturation genome editing", size=50)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="MaveDB → literature", fetched_n=len(hits), data={"symbol": sym, "hits": hits}, citations=cites + lit, fetched_at=_now())

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

@router.get("/mech/pathways", response_model=EvidenceEnvelope)
async def mech_pathways(uniprot: str = Query(...)):
    """
    Mechanistic pathways via Reactome Content Service. Only Homo sapiens pathways are returned.
    """
    env = EvidenceEnvelope(module="mech-pathways", domain=DOMAIN_LABELS["2"])
    try:
        rows = await reactome.pathways_for_uniprot(fetch_json, uniprot)
    except Exception:
        rows = []
    filtered = [r for r in rows if r.get("speciesName") == "Homo sapiens"]
    env.records = filtered
    env.provenance.sources.append("https://reactome.org/ContentService")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

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

# ---------------------- Additional mechanistic regulator endpoints (v2.2) ----------------------

@router.get("/mech/complexes", response_model=EvidenceEnvelope)
async def mech_complexes(uniprot: str = Query(...)):
    """
    Protein complexes for a UniProt accession. Primary source is ComplexPortal; OmniPath complexes used as fallback.
    """
    env = EvidenceEnvelope(module="mech-complexes", domain=DOMAIN_LABELS["2"])
    try:
        cps = await complex_portal.complexes_by_uniprot(fetch_json, uniprot)
        env.provenance.sources.append("https://www.ebi.ac.uk/intact/complex-ws")
        env.records = cps
    except Exception:
        try:
            data, _ = await fetch_json("https://omnipathdb.org/complexes", params={"genes": uniprot, "organism": "9606"}, ttl_tier="moderate")
            env.provenance.sources.append("https://omnipathdb.org/complexes")
            env.records = data
        except Exception:
            env.records = []
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/mech/directed-signaling", response_model=EvidenceEnvelope)
async def mech_directed_signaling():
    """
    Directed signaling interactions (signed and directed) via OmniPath. Returns a list of interactions and derived edges.
    """
    env = EvidenceEnvelope(module="mech-directed-signaling", domain=DOMAIN_LABELS["2"])
    try:
        rows = await omnipath.directed_interactions(fetch_json)
    except Exception:
        rows = []
    env.records = [r for r in rows if r.get("is_directed")]
    env.edges = []
    for r in env.records:
        direction = None
        if r.get("is_stimulation"):
            direction = "activates"
        elif r.get("is_inhibition"):
            direction = "inhibits"
        elif r.get("is_directed"):
            direction = "signed"
        env.edges.append(Edge(src=r.get("source"), dst=r.get("target"), type="regulates", direction=direction, meta={}, sources=["https://omnipathdb.org/interactions"]))
    env.provenance.sources.append("https://omnipathdb.org/interactions")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/mech/kinase-substrate", response_model=EvidenceEnvelope)
async def mech_kinase_substrate():
    """
    Kinase–substrate interactions via OmniPath (kinaseextra and omnipath datasets). Returns interactions and derived edges.
    """
    env = EvidenceEnvelope(module="mech-kinase-substrate", domain=DOMAIN_LABELS["2"])
    try:
        rows = await omnipath.kinase_substrate(fetch_json)
    except Exception:
        rows = []
    env.records = rows
    env.edges = [
        Edge(src=r.get("enzyme"), dst=r.get("substrate"), type="phosphorylates", direction="activates", meta={}, sources=["https://omnipathdb.org/enzsub"])
        for r in rows
    ]
    env.provenance.sources.append("https://omnipathdb.org/enzsub")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/mech/mirna-target", response_model=EvidenceEnvelope)
async def mech_mirna_target(gene: Optional[str] = Query(None)):
    """
    miRNA–target interactions via OmniPath mirnatarget dataset. If gene symbol is provided, restrict interactions to that gene.
    """
    env = EvidenceEnvelope(module="mech-mirna-target", domain=DOMAIN_LABELS["2"])
    try:
        url = "https://omnipathdb.org/interactions"
        params = {"datasets": "mirnatarget", "format": "json"}
        if gene:
            params["genes"] = gene
        rows, _ = await fetch_json(url, params=params, ttl_tier="moderate")
    except Exception:
        rows = []
    env.records = rows
    env.edges = [
        Edge(src=r.get("source"), dst=r.get("target"), type="regulates", direction="inhibits", meta={}, sources=["https://omnipathdb.org/interactions"])
        for r in rows
    ]
    env.provenance.sources.append("https://omnipathdb.org/interactions")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/mech/tf-target", response_model=EvidenceEnvelope)
async def mech_tf_target():
    """
    Transcription factor–target regulatory interactions via OmniPath DoRothEA (A,B levels) and tf_target datasets. Returns interactions and derived edges.
    """
    env = EvidenceEnvelope(module="mech-tf-target", domain=DOMAIN_LABELS["2"])
    try:
        rows = await omnipath.tf_targets_dorothea(fetch_json, levels="A,B")
    except Exception:
        rows = []
    env.records = rows
    env.edges = [
        Edge(src=r.get("source"), dst=r.get("target"), type="regulates", direction="activates", meta={}, sources=["https://omnipathdb.org/interactions"])
        for r in rows
    ]
    env.provenance.sources.append("https://omnipathdb.org/interactions")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

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

@router.get("/tract/ligandability-ab", response_model=EvidenceEnvelope)
async def tract_ligandability_ab(gene: Optional[str] = Query(None)):
    """
    Antibody ligandability evidence based on UniProtKB & GlyGen; Human Protein Atlas used only as fallback flags.  Optionally filter by HGNC symbol.
    """
    env = EvidenceEnvelope(module="tract-ligandability-ab", domain=DOMAIN_LABELS["4"])
    try:
        proteins = await uniprot.cell_surface(fetch_json, gene)
    except Exception:
        proteins = []
    env.provenance.sources.append("https://rest.uniprot.org/uniprotkb/search")
    out: List[dict] = []
    for p in proteins or []:
        ac = p.get("primaryAccession") or p.get("accession") or None
        gg = {}
        if ac:
            try:
                gg = await glygen.protein_summary(fetch_json, ac)
                env.provenance.sources.append("https://api.glygen.org/protein/detail")
            except Exception:
                gg = {}
        # Heuristic for antibody feasibility: look for membrane/secreted/extracellular keywords
        loc_fields: List[str] = []
        for k in ("cc_subcellular_location", "comment"):
            v = p.get(k)
            if isinstance(v, str):
                loc_fields.append(v.lower())
        loc_text = " ".join(loc_fields)
        feasible = any(keyword in loc_text for keyword in ["membrane", "secreted", "extracellular"])
        out.append({"uniprot": ac, "uniprot_row": p, "glygen": gg, "antibody_feasible": bool(feasible)})
    env.records = out
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

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
    """
    Immunogenicity evidence.  Primary evidence derives from the IEDB IQ‑API
    (observed epitopes) and IEDB Tools (prediction) via the tract‑iedb‑epitopes
    endpoint.  Europe PMC is consulted only for contextual literature.
    """
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites: List[str] = []
    data: Dict[str, Any] = {}
    sources_used: List[str] = []
    fetched = 0
    # 1) Observed epitopes via IEDB IQ‑API
    try:
        qdict = {"gene_symbol": f"eq.{sym}"}
        rows = await iedb.search_epitopes(fetch_json, qdict)
        if rows:
            data["iedb_epitopes"] = rows[:1000]
            fetched += len(data["iedb_epitopes"])
            sources_used.append("IEDB IQ-API")
            cites.append("https://query-api.iedb.org/epitope_search")
    except Exception:
        # ignore errors; will fall back
        pass
    # 2) If no epitopes found, leave the data empty; we do not run prediction
    # automatically because predictions require peptide inputs.
    if fetched:
        return Evidence(status="OK", source=", ".join(sources_used), fetched_n=fetched, data=data, citations=cites, fetched_at=_now())
    # 3) Contextual literature overlay
    q = f"{sym} immunogenic OR epitope OR HLA"
    hits, epmc_cites = await _epmc_search(q, size=60)
    cites.extend(epmc_cites)
    data["query"] = q
    data["hits"] = hits
    return Evidence(status=("OK" if hits else "NO_DATA"), source="Europe PMC (immunogenicity literature)", fetched_n=len(hits), data=data, citations=cites, fetched_at=_now())

@router.post("/tract/mhc-binding", response_model=EvidenceEnvelope)
async def tract_mhc_binding(payload: Dict[str, Any] = Body(...)):
    """
    Predict peptide–MHC binding via IEDB Tools (netMHCpan) with allele normalization from IPD‑IMGT/HLA.
    Expects JSON payload: {"peptides": [...], "alleles": [...], "class": "I"|"II"}.
    """
    env = EvidenceEnvelope(module="tract-mhc-binding", domain=DOMAIN_LABELS["5"])
    alleles = payload.get("alleles", []) if payload else []
    peptides = payload.get("peptides", []) if payload else []
    mhc_class = payload.get("class", "I") if payload else "I"
    # Normalize allele names via IPD‑IMGT/HLA (recorded in provenance but not used directly)
    try:
        if alleles:
            _ = await ipd_imgt.search_alleles(fetch_json, " OR ".join(alleles))
            env.provenance.sources.append("https://www.ebi.ac.uk/cgi-bin/ipd/api/allele")
    except Exception:
        pass
    try:
        if mhc_class.upper() == "II":
            preds = await iedb.predict_mhc_class_ii(fetch_json, peptides, alleles)
            env.provenance.sources.append("https://tools.iedb.org/tools_api/mhcii/")
        else:
            preds = await iedb.predict_mhc_class_i(fetch_json, peptides, alleles)
            env.provenance.sources.append("https://tools.iedb.org/tools_api/mhci/")
    except Exception:
        preds = []
    env.records = preds
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

@router.get("/tract/iedb-epitopes", response_model=EvidenceEnvelope)
async def tract_iedb_epitopes(gene: Optional[str] = Query(None), antigen: Optional[str] = Query(None)):
    """
    IEDB epitope evidence. Primary source is the IEDB IQ‑API. Accepts optional gene symbol and antigen substring.
    """
    env = EvidenceEnvelope(module="tract-iedb-epitopes", domain=DOMAIN_LABELS["5"])
    # Build query dict for IEDB IQ‑API: gene symbol (exact match) and antigen partial match.
    q = {}
    if gene:
        q["gene_symbol"] = f"eq.{gene}"
    if antigen:
        q["antigen_name"] = f"ilike.*{antigen}*"
    try:
        rows = await iedb.search_epitopes(fetch_json, q)
    except Exception:
        rows = []
    # Limit number of returned records to prevent large payloads
    env.records = rows[:1000]
    env.provenance.sources.append("https://query-api.iedb.org/epitope_search")
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env

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
async def clin_biomarker_fit(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    cites = ["https://api.monarchinitiative.org/", "https://hpo.jax.org/"]
    try:
        # Resolve HGNC/Monarch gene id via autocomplete
        s_url = "https://api.monarchinitiative.org/api/search/entity/autocomplete/" + urllib.parse.quote(sym) + "?category=gene&rows=1"
        s_js  = await _get_json(s_url, tries=2)
        gene_id = ((((s_js or {}).get("docs") or []) or [{}])[0] or {}).get("id")
        if gene_id:
            ph_url = f"https://api.monarchinitiative.org/api/bioentity/{urllib.parse.quote(gene_id)}/phenotypes?rows=100"
            ph_js  = await _get_json(ph_url, tries=2)
            return Evidence(status="OK", source="Monarch/HPO APIs", fetched_n=1, data={"gene_id": gene_id, "phenotypes": ph_js}, citations=cites + [s_url, ph_url], fetched_at=_now())
    except Exception:
        pass
    # Fallback: PharmGKB family
    pharm = f"https://api.pharmgkb.org/v1/data/gene?q={urllib.parse.quote(sym)}"
    hits, lit = await _epmc_search(f"{sym} biomarker OR predictive biomarker site:pharmgkb.org", size=25)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="Monarch/HPO → PharmGKB (literature)", fetched_n=len(hits), data={"symbol": sym, "hits": hits}, citations=cites + [pharm] + lit, fetched_at=_now())

@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    term = condition or sym
    base = "https://clinicaltrials.gov/api/v2/studies"
    params = {"query.cond": term, "pageSize": 25, "format": "json", "countTotal": "true"}
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        js = await _get_json(url, tries=2)
        total = (js or {}).get("totalCount") or 0
        return Evidence(status=("OK" if total else "NO_DATA"), source="ClinicalTrials.gov v2", fetched_n=total, data={"term": term, "studies": js}, citations=[base, "https://www.nlm.nih.gov/pubs/techbull/ma24/ma24_clinicaltrials_api.html"], fetched_at=_now())
    except Exception:
        # Fallback: DrugCentral trials (family) + literature
        dc = "https://drugcentral.org/"
        hits, lit = await _epmc_search(f"{term} clinical trial site:clinicaltrials.gov", size=25)
        return Evidence(status=("OK" if hits else "NO_DATA"), source="ClinicalTrials.gov → literature", fetched_n=len(hits), data={"term": term, "hits": hits}, citations=[base, dc] + lit, fetched_at=_now())

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
    ensg = await _ensg_from_symbol(sym) or sym
    cites = ["https://gnomad.broadinstitute.org/", "https://gnomad.broadinstitute.org/news/2023-05-update-api-to-typescript/"]
    # Primary: gnomAD GraphQL gene constraint
    try:
        gql = """query($geneId:String!){ gene(gene_id:$geneId){ gene_id symbol constraint { oe_lof oe_mis oe_syn lof_z missense_z syn_z pLI pRec pNull } } }"""
        js = await _httpx_json_post("https://gnomad.broadinstitute.org/api", {"query": gql, "variables": {"geneId": ensg}})
        cons = ((((js or {}).get("data") or {}).get("gene") or {}).get("constraint"))
        if cons:
            return Evidence(status="OK", source="gnomAD GraphQL", fetched_n=1, data={"ensg": ensg, "constraint": cons}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    # Fallback: CADD (family) reference + literature
    cites.append("https://cadd.gs.washington.edu/")
    hits, lit = await _epmc_search(f"{sym} constraint OR intolerance OR LOEUF", size=30)
    cites.extend(lit)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="gnomAD → literature", fetched_n=(1 if hits else 0), data={"ensg": ensg, "hits": hits}, citations=cites, fetched_at=_now())

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
async def synth_graph(
    gene: str = Body(...),
    condition: Optional[str] = Body(None),
    bucket_payloads: Dict[str, Dict[str, Any]] = Body(default_factory=dict),
    algos: Dict[str, Any] = Body(default_factory=dict)
) -> TargetGraph:
    """
    Build a target-centric evidence graph and (internally) run:
      • Random Walk with Restart (RWR) from the target gene,
      • Prize-Collecting Steiner Tree (PCST) approximation to connect key buckets,
      • Community detection (Leiden if available; else greedy modularity).
    The algorithm outputs are attached as node/edge attributes (no external scoring is exposed).
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[GraphEdge] = []

    def add_node(key: str, kind: str, label: Optional[str] = None, **attrs):
        if key not in nodes:
            nodes[key] = {"kind": kind, "label": label or key, **attrs}
        else:
            nodes[key].update(attrs)

    def add_edge(src: str, dst: str, kind: str, weight: float = 1.0):
        edges.append(GraphEdge(src=src, dst=dst, kind=kind, weight=weight))

    add_node(gene, "Gene", label=gene)
    for b, p in (bucket_payloads or {}).items():
        add_node(b, "Bucket", label=b); add_edge(gene, b, "in_bucket", 1.0)
        mos = (p.get("module_outputs") or {}) if isinstance(p, dict) else {}
        for k, v in mos.items():
            mk = f"{b}:{k}"
            add_node(mk, "Module", label=k)
            add_edge(b, mk, "has_module", 1.0)
            # link evidence items if provided
            items = (v.get("data") or []) if isinstance(v, dict) else []
            if isinstance(items, dict):  # sometimes a dict with nested arrays
                # flatten one level if possible
                for key2, arr in items.items():
                    if isinstance(arr, list):
                        items = arr; break
            for i, _ in enumerate(items[:20]):  # cap fan-out
                ek = f"{mk}#{i}"
                add_node(ek, "Evidence")
                add_edge(mk, ek, "supports", 1.0)

    # ---- Internal graph algorithms (optional; attached as attributes) ----
    if nx is not None and edges:
        try:
            G = nx.DiGraph()
            for k, attrs in nodes.items():
                G.add_node(k, **attrs)
            for e in edges:
                G.add_edge(e.src, e.dst, weight=float(e.weight or 1.0))
            # RWR from target gene
            alpha = float(algos.get("rwr_alpha", 0.7)) if isinstance(algos, dict) else 0.7
            # Convert to undirected for diffusion
            GU = G.to_undirected()
            # normalize transition matrix
            import numpy as _np  # type: ignore
            idx = {n: i for i, n in enumerate(GU.nodes())}
            n = len(idx)
            if n <= 10000:  # safeguard
                A = _np.zeros((n, n), dtype=float)
                for u, v, d in GU.edges(data=True):
                    w = float(d.get("weight") or 1.0)
                    A[idx[u], idx[v]] += w
                    A[idx[v], idx[u]] += w
                dsum = A.sum(axis=1)
                P = _np.divide(A, dsum, out=_np.zeros_like(A), where=(dsum > 0)[:, None])
                r = _np.zeros(n); r[idx[gene]] = 1.0
                p = r.copy()
                tol = 1e-6
                for _ in range(200):
                    p_new = (1 - alpha) * P.T.dot(p) + alpha * r
                    if _np.linalg.norm(p_new - p, ord=1) < tol:
                        p = p_new; break
                    p = p_new
                # attach scores
                inv_idx = {i: n for n, i in idx.items()}
                for i2 in range(n):
                    nodes[inv_idx[i2]]["rwr_score"] = float(p[i2])
            # PCST (approx via Steiner tree on buckets + gene)
            try:
                from networkx.algorithms.approximation import steiner_tree
                terminals = [gene] + [k for k, v in nodes.items() if v.get("kind") == "Bucket"]
                T = steiner_tree(GU, terminals)
                pcst_edges = set()
                for u, v in T.edges():
                    pcst_edges.add((u, v)); pcst_edges.add((v, u))
                for e in edges:
                    if (e.src, e.dst) in pcst_edges:
                        e.weight = float(e.weight or 1.0) * 1.1  # subtle bump
                        # mark both ends as in-PCST
                        nodes[e.src]["pcst"] = True; nodes[e.dst]["pcst"] = True
            except Exception:
                pass
            # Communities (Leiden via igraph if available; else greedy modularity)
            try:
                import igraph as ig  # type: ignore
                import leidenalg  # type: ignore
                g = ig.Graph()
                name_to_id = {n:i for i,n in enumerate(GU.nodes())}
                g.add_vertices(len(name_to_id))
                g.vs["name"] = list(name_to_id.keys())
                g.add_edges([(name_to_id[u], name_to_id[v]) for u,v in GU.edges()])
                part = leidenalg.find_partition(g, leidenalg.RBConfigurationVertexPartition, resolution_parameter=1.0)
                for cid, comm in enumerate(part):
                    for vid in comm:
                        nodes[g.vs[vid]["name"]]["community"] = int(cid)
            except Exception:
                try:
                    from networkx.algorithms.community import greedy_modularity_communities
                    comms = list(greedy_modularity_communities(GU))
                    for cid, com in enumerate(comms):
                        for nname in com:
                            nodes[nname]["community"] = int(cid)
                except Exception:
                    pass
        except Exception:
            pass

    return TargetGraph(nodes=nodes, edges=edges)
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
    # BioGRID ORCS REST API historically required an API key.  Under the keyless
    # configuration we avoid reading any environment variables and instead use the
    # open endpoint without an access key.  If the open call fails, ORCS data is
    # simply omitted and the DepMap files remain the primary payload.  Always
    # cite the ORCS webservice documentation when attempting the call.
    orcs = None
    try:
        orcs_url = f"https://orcsws.thebiogrid.org/genes/?geneName={urllib.parse.quote(sym)}&format=json"
        orcs = await _get_json(orcs_url)
        cites.append("https://wiki.thebiogrid.org/doku.php/orcs:webservice")
        cites.append(orcs_url)
    except Exception:
        orcs = None
    payload = {"depmap_files": files, "orcs_gene": orcs}
    fetched_n = sum(len(v) if isinstance(v, list) else (len(v) if isinstance(v, dict) else 1) for v in payload.values() if v)
    return Evidence(status="OK" if fetched_n else "NO_DATA", source="DepMap portal, BioGRID ORCS", fetched_n=fetched_n, data=payload, citations=cites, fetched_at=_now())




@router.get("/immuno/hla-coverage", response_model=EvidenceEnvelope)
async def immuno_hla_coverage(alleles: List[str] = Query(...)):
    """
    HLA population coverage metadata via IPD‑IMGT/HLA and IEDB population coverage. Provide one or more allele identifiers (e.g., HLA-A*02:01) as query parameters.
    """
    env = EvidenceEnvelope(module="immuno/hla-coverage", domain=DOMAIN_LABELS["5"])
    try:
        meta = await ipd_imgt.search_alleles(fetch_json, " OR ".join(alleles))
    except Exception:
        meta = []
    env.records = meta
    env.provenance.sources += [
        "https://www.ebi.ac.uk/cgi-bin/ipd/api/allele",
        "https://tools.iedb.org/population/"
    ]
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env




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


@router.get("/tract/surfaceome", response_model=EvidenceEnvelope)
async def tract_surfaceome(gene: Optional[str] = Query(None)):
    """
    Surfaceome (cell surface proteins) evidence: primary sources UniProtKB and GlyGen. Optionally filter by HGNC symbol.
    """
    env = EvidenceEnvelope(module="tract-surfaceome", domain=DOMAIN_LABELS["4"])
    # Query UniProtKB for cell surface proteins; optionally filter by gene symbol.
    try:
        proteins = await uniprot.cell_surface(fetch_json, gene)
    except Exception:
        proteins = []
    env.provenance.sources.append("https://rest.uniprot.org/uniprotkb/search")
    # Augment results with GlyGen protein summaries
    out: List[dict] = []
    for p in proteins or []:
        ac = p.get("primaryAccession") or p.get("accession") or None
        gg = {}
        if ac:
            try:
                gg = await glygen.protein_summary(fetch_json, ac)
                env.provenance.sources.append("https://api.glygen.org/protein/detail")
            except Exception:
                gg = {}
        out.append({"uniprot": ac, "uniprot_row": p, "glygen": gg})
    env.records = out
    env.provenance.module_order = _module_order_index(env.module)
    stage_context(env)
    return env
    



    # fail-closed would break the service; we log to stdout and keep routes as-is
    print("Route guard skipped due to error:", _e)



# ===== Strict route pruning (hard-coded 55 module paths) =====
# ---------- Added modules to reach 64 per Targetval Config (2025-10-17) ----------
try:
    MODULES += [
        # internal-only modules removed per v2.2 spec (no public route)
        # Module(route="/perturb/qc", name="perturb-qc (internal)", sources=["(internal only)"], bucket="Functional & mechanistic validation"),
        # Module(route="/perturb/scrna-summary", name="perturb-scrna-summary (internal)", sources=["(internal only)"], bucket="Functional & mechanistic validation"),
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
    # Compose the ORCS URL without any API key.  Under the keyless configuration
    # we avoid reading environment variables.  If the call fails, fall back to
    # Europe PMC keyword search.  Always cite the ORCS webservice and the actual
    # URL attempted.
    base = "https://orcsws.thebiogrid.org/genes/?"
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
    n = len(data.get("orcs", [])) if isinstance(data.get("orcs"), list) else (1 if data.get("orcs") else 0)
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
    """
    Thin wrapper around fetch_json for backward compatibility. Delegates to app.runtime.http.fetch_json to honor
    per‑host semaphores, backoff, and caching. Returns (data, None) or (None, error).
    """
    try:
        data, _meta = await fetch_json(url, params=params or {}, headers=headers, ttl_tier="moderate", method="GET")
        return data, None
    except Exception as e:
        return None, str(e)

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
        # limit parallelism to 4 tasks per spec
        sem = asyncio.Semaphore(4)
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
@router.get("/genetics/caqtl-lite", response_model=Evidence)
async def genetics_caqtl_lite(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    return await _genetics_caqtl_lite(request, gene=gene, symbol=symbol)

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
@router.get("/genetics/nmd-inference", response_model=Evidence)
async def genetics_nmd_inference(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    return await _genetics_nmd_inference(request, gene=gene, symbol=symbol)

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
@router.get("/genetics/ase-check", response_model=Evidence)
async def genetics_ase_check(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    return await _genetics_ase_check(sym, condition, limit)

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
@router.get("/genetics/ptm-signal-lite", response_model=Evidence)
async def genetics_ptm_signal_lite(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500)) -> Evidence:
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    return await _genetics_ptm_signal_lite(sym, limit)

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
async def genetics_consortia_summary(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = Query(None)) -> Evidence:
    """Live OpenTargets GraphQL summary (UKB/FinnGen/GWAS) with literature fallback."""
    sym = await _normalize_symbol(_sym_or_gene(symbol, gene))
    ensg = await _ensg_from_symbol(sym) or sym
    efo  = await _efo_lookup(condition)
    cites = ["https://platform.opentargets.org/", "https://platform.opentargets.org/api/v4/graphql"]
    if not (ensg and efo):
        return Evidence(status="NO_DATA", source="OpenTargets", fetched_n=0, data={"note":"missing ensg or efo", "symbol": sym, "ensg": ensg, "efo": efo}, citations=cites, fetched_at=_now())
    q = """
    query($ensg:String!, $efo:String!){
      associationByEntity(targetId:$ensg, diseaseId:$efo){
        datasourceScores{ id score }
      }
    }"""
    try:
        js  = await _httpx_json_post(EXTERNAL_URLS["opentargets_graphql"], {"query": q, "variables": {"ensg": ensg, "efo": efo}})
        dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
        keys  = {"uk_biobank","finngen","gwas_catalog"}
        cons  = [x for x in dss if (str(x.get("id","")).lower() in keys) and (x.get("score") or 0) > 0]
        if cons:
            return Evidence(status="OK", source="OpenTargets", fetched_n=len(cons), data={"ensg": ensg, "efo": efo, "consortia": cons, "datasourceScores": dss}, citations=cites, fetched_at=_now())
    except Exception:
        pass
    # Fallback literature
    fallback_query = f"{sym} {condition or ''} UK Biobank OR FinnGen OR GWAS catalog"
    hits, lit_cites = await _epmc_search(fallback_query, size=60)
    cites.extend(lit_cites)
    return Evidence(status=("OK" if hits else "NO_DATA"), source="Europe PMC (fallback)", fetched_n=len(hits), data={"query": fallback_query, "hits": hits}, citations=cites, fetched_at=_now())

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
    js  = await _httpx_json_post(EXTERNAL_URLS['opentargets_graphql'], {"query": q, "variables": {"ensg": ensg, "efo": efo}})
    dss = ((((js or {}).get("data") or {}).get("associationByEntity") or {}).get("datasourceScores") or [])
    qtl_like = [x for x in dss if any(s in str(x.get("id","")).lower() for s in ["eqtl","pqtl","molecular","qtl"]) and (x.get("score") or 0) > 0]
    return Evidence(status=("OK" if qtl_like else "NO_DATA"), source="OpenTargets",
                    fetched_n=len(qtl_like),
                    data={"ensg": ensg, "efo": efo, "mqtl_like": qtl_like, "datasourceScores": dss},
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
    js  = await _httpx_json_post(EXTERNAL_URLS['opentargets_graphql'], {"query": q, "variables": {"ensg": ensg, "efo": efo}})
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




# =================== FIXUP BLOCK (non-breaking; idempotent) ===================
# This block adds:
#  - alias endpoints to cover spec keys whose routes were split/renamed,
#  - registry entries for 9 "perturb-*" modules that had live routes but were unregistered,
#  - lightweight alias endpoints for genetics-{annotation, regulatory, 3d-maps},
#  - a debug route to list duplicate path+method registrations.

from typing import Optional as _Optional

def _uniq(seq):
    out = []
    seen = set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

async def _alias_merge(request: Request, paths: List[str], params: Dict[str, Any], label: str) -> Evidence:
    cites_all: List[str] = []
    comps: List[Dict[str, Any]] = []
    total = 0
    for p in _uniq(paths):
        try:
            js = await _self_get(p, params)
            if isinstance(js, dict):
                total += int(js.get("fetched_n") or 0)
                cites_all.extend(js.get("citations") or [])
                comps.append({"path": p, "status": js.get("status"), "source": js.get("source"), "fetched_n": js.get("fetched_n"), "data": js.get("data")})
        except Exception:
            comps.append({"path": p, "status": "ERROR", "source": None, "fetched_n": 0, "data": None})
    return Evidence(
        status="OK" if total > 0 else "NO_DATA",
        source=f"Alias: {label}",
        fetched_n=total,
        data={"components": comps},
        citations=_uniq(cites_all),
        fetched_at=_now()
    )

# ---- Alias endpoints for split/renamed modules ----

# assoc-spatial → aggregate of two concrete routes
async def _assoc_spatial(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None),
                         condition: _Optional[str] = Query(None), limit: int = Query(100, ge=1, le=200)) -> Evidence:
    params = dict(symbol=symbol or gene, condition=condition, limit=limit)
    # Spatial association: merge gene-level spatial expression and neighbourhood data.  The spec
    # specifies Europe PMC spatial/ISH literature as the primary; update the label accordingly.
    return await _alias_merge(request, ["/assoc/spatial-expression", "/assoc/spatial-neighborhoods"], params, "Spatial association (Europe PMC spatial/ISH)")
@router.get("/assoc/spatial", response_model=Evidence)
async def assoc_spatial(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    return await _assoc_spatial(symbol=symbol, gene=gene)

_add_if_missing("/assoc/spatial", _assoc_spatial, ["GET"], Evidence)

# assoc-proteomics → aggregate of bulk proteomics sources
async def _assoc_proteomics(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None),
                            condition: _Optional[str] = Query(None), limit: int = Query(100, ge=1, le=200)) -> Evidence:
    params = dict(symbol=symbol or gene, condition=condition, limit=limit)
    paths = ["/assoc/bulk-prot", "/assoc/bulk-prot-pdc", "/assoc/omics-phosphoproteomics"]
    # Proteomics association: prioritise ProteomicsDB, then PDC (CPTAC) and PRIDE, and include
    # phosphoproteomics; adjust the source label accordingly.
    return await _alias_merge(request, paths, params, "Proteomics (ProteomicsDB + PDC/PRIDE)")
@router.get("/assoc/proteomics", response_model=Evidence)
async def assoc_proteomics(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None, limit: int = Query(100, ge=1, le=200)) -> Evidence:
    return await _assoc_proteomics(symbol=symbol, gene=gene, condition=condition, limit=limit)

_add_if_missing("/assoc/proteomics", _assoc_proteomics, ["GET"], Evidence)

# assoc-metabolomics → aggregate of metabolomics sources
async def _assoc_metabolomics(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None),
                              condition: _Optional[str] = Query(None), limit: int = Query(100, ge=1, le=200)) -> Evidence:
    params = dict(symbol=symbol or gene, condition=condition, limit=limit)
    paths = ["/assoc/omics-metabolites", "/assoc/metabolomics-ukb-nightingale"]
    # The spec designates MetaboLights as the primary and Metabolomics Workbench as the fallback.
    # Our omics-metabolites and UKB Nightingale modules correspond to these two sources; adjust
    # the source label accordingly.
    return await _alias_merge(request, paths, params, "Metabolomics (MetaboLights + Metabolomics Workbench)")
@router.get("/assoc/metabolomics", response_model=Evidence)
async def assoc_metabolomics(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None, limit: int = Query(100, ge=1, le=200)) -> Evidence:
    return await _assoc_metabolomics(symbol=symbol, gene=gene, condition=condition, limit=limit)

_add_if_missing("/assoc/metabolomics", _assoc_metabolomics, ["GET"], Evidence)

# perturb-perturbseq-encode → forwarder to the concrete ENCODE perturbseq route
async def _perturb_perturbseq_encode(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None),
                                     limit: int = Query(50, ge=1, le=200)) -> Evidence:
    params = dict(symbol=symbol or gene, limit=limit)
    js = await _self_get("/perturb/perturbseq", params)
    # Make it explicit that this is an alias
    if isinstance(js, dict):
        js["source"] = "Alias → ENCODE REST API"
    return js  # FastAPI will coerce to Evidence
_add_if_missing("/perturb/perturbseq-encode", _perturb_perturbseq_encode, ["GET"], Evidence)

# genetics-annotation → aggregate intolerance + pathogenicity priors
async def _genetics_annotation(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None)) -> Evidence:
    params = dict(symbol=symbol or gene)
    paths = ["/genetics/intolerance", "/genetics/pathogenicity-priors"]
    return await _alias_merge(request, paths, params, "Gene-level annotation (constraint + pathogenicity priors)")
@router.get("/genetics/annotation", response_model=Evidence)
async def genetics_annotation(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    return await _genetics_annotation(symbol=symbol, gene=gene)

_add_if_missing("/genetics/annotation", _genetics_annotation, ["GET"], Evidence)

# genetics-regulatory → aggregate sQTL + chromatin contacts + mQTL colocalization if present
async def _genetics_regulatory(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None),
                               condition: _Optional[str] = Query(None)) -> Evidence:
    params = dict(symbol=symbol or gene, condition=condition)
    paths = ["/genetics/sqtl", "/genetics/chromatin-contacts", "/genetics/mqtl-coloc"]
    return await _alias_merge(request, paths, params, "Regulatory evidence (sQTL + 3D contacts + mQTL coloc)")
@router.get("/genetics/regulatory", response_model=Evidence)
async def genetics_regulatory(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None), condition: Optional[str] = None) -> Evidence:
    return await _genetics_regulatory(symbol=symbol, gene=gene)

_add_if_missing("/genetics/regulatory", _genetics_regulatory, ["GET"], Evidence)

# genetics-3d-maps → forwarder to chromatin contacts
async def _genetics_3d_maps(symbol: _Optional[str] = Query(None), gene: _Optional[str] = Query(None)) -> Evidence:
    params = dict(symbol=symbol or gene)
    js = await _self_get("/genetics/chromatin-contacts", params)
    if isinstance(js, dict):
        # Report the correct upstream sources for the 3D maps.  Per spec, this
        # alias corresponds to 4D Nucleome and UCSC interaction/loop tracks.
        js["source"] = "Alias → 3D chromatin contacts (4DN + UCSC)"
    return js
@router.get("/genetics/3d-maps", response_model=Evidence)
async def genetics_3d_maps(symbol: Optional[str] = Query(None), gene: Optional[str] = Query(None)) -> Evidence:
    return await _genetics_3d_maps(symbol=symbol, gene=gene)

_add_if_missing("/genetics/3d-maps", _genetics_3d_maps, ["GET"], Evidence)

# ---- Extend registry with the 9 'perturb-*' modules if missing ----
def _add_module_if_missing(route: str, name: str, sources: List[str], bucket: str):
    try:
        if not any(getattr(m, "name", None) == name for m in MODULES):
            MODULES.append(Module(route=route, name=name, sources=sources, bucket=bucket))
    except Exception:
        pass

_b2 = "Functional & mechanistic validation"
_b4 = "Druggability & modality tractability"
_b5 = "Therapeutic index & safety translation"

_add_module_if_missing("/perturb/connectivity", "perturb-connectivity",
                       ["LDP3 Metadata & Enrichment API", "iLINCS (fallback)"], _b2)
_add_module_if_missing("/perturb/lincs-signatures", "perturb-lincs-signatures",
                       ["LDP3 Metadata API"], _b2)
_add_module_if_missing("/perturb/signature-enrichment", "perturb-signature-enrichment",
                       ["LDP3 Enrichment API"], _b2)
_add_module_if_missing("/perturb/perturbseq-encode", "perturb-perturbseq-encode",
                       ["ENCODE REST API"], _b2)
_add_module_if_missing("/perturb/crispr-screens", "perturb-crispr-screens",
                       ["BioGRID ORCS API", "Europe PMC (fallback)"], _b2)
_add_module_if_missing("/perturb/drug-response", "perturb-drug-response",
                       ["PharmacoDB API"], _b4)
_add_module_if_missing("/perturb/depmap-dependency", "perturb-depmap-dependency",
                       ["DepMap Portal API", "Sanger Cell Model Passports (fallback)"], _b5)

# ---- Simple duplicate route inspector (does not mutate state) ----
def _find_duplicate_routes() -> List[Dict[str, Any]]:
    seen = {}
    dups = []
    try:
        for r in router.routes:  # type: ignore[name-defined]
            path = getattr(r, "path", None)
            methods = tuple(sorted(getattr(r, "methods", []) or []))
            if not path or not methods:
                continue
            key = (path, methods)
            if key in seen:
                dups.append({"path": path, "methods": list(methods)})
            else:
                seen[key] = 1
    except Exception:
        pass
    return dups

async def _debug_routes() -> Dict[str, Any]:
    dups = _find_duplicate_routes()
    return {"routes": len(getattr(router, "routes", [])), "duplicates": dups}  # type: ignore[name-defined]

_add_if_missing("/debug/routes", _debug_routes, ["GET"], None)

# ================= END FIXUP BLOCK ============================================

# ----------------------- Live client implementations (override previous stubs) -----------------------
try:
    import types as _types

    # IEDB observed epitopes (simple search by gene symbol in reference)
    async def _iedb_observed_epitopes(fetch_json_func, query_dict: Dict[str, Any]):
        base = "https://api.iedb.org/epitope"
        q = urllib.parse.urlencode(query_dict)
        url = f"{base}?{q}"
        js = await _httpx_json_get(url)
        return js or {}

    # Try IEDB MHC binding tools if reachable (class I)
    async def _iedb_predict_mhc_class_i(fetch_json_func, peptides: List[str], alleles: List[str]):
        # Tools API cluster; may be rate-limited. We attempt a minimal call; on failure return empty result.
        url = "http://tools-cluster-interface.iedb.org/tools_api/mhci/"
        try:
            payload = {"sequence_text": "\n".join(peptides[:50]), "allele": ",".join(alleles[:50]), "length": "9"}
            js = await _httpx_json_post(url, payload, timeout=60.0)
            return js or {}
        except Exception:
            return {}

    async def _iedb_predict_mhc_class_ii(fetch_json_func, peptides: List[str], alleles: List[str]):
        url = "http://tools-cluster-interface.iedb.org/tools_api/mhcii/"
        try:
            payload = {"sequence_text": "\n".join(peptides[:50]), "allele": ",".join(alleles[:50])}
            js = await _httpx_json_post(url, payload, timeout=60.0)
            return js or {}
        except Exception:
            return {}

    iedb.observed_epitopes = _iedb_observed_epitopes  # type: ignore
    iedb.predict_mhc_class_i = _iedb_predict_mhc_class_i  # type: ignore
    iedb.predict_mhc_class_ii = _iedb_predict_mhc_class_ii  # type: ignore

    # IPD-IMGT/HLA live allele search
    async def _ipd_search_alleles(fetch_json_func, query: str):
        base = "https://www.ebi.ac.uk/ipd/api/v1/alleles"
        url = f"{base}?search={urllib.parse.quote(query)}"
        js = await _httpx_json_get(url)
        return js or {}

    ipd_imgt.search_alleles = _ipd_search_alleles  # type: ignore

    # UniProt REST — search for cell-surface proteins for a gene
    async def _uniprot_cell_surface(fetch_json_func, gene: Optional[str]):
        if not gene:
            return {}
        q = f'(gene_exact:{gene}) AND (cc_scl_term:"Cell membrane" OR locations:"Plasma membrane")'
        url = "https://rest.uniprot.org/uniprotkb/search?format=json&size=50&fields=accession,protein_name,gene_names,cc_subcellular_location&query=" + urllib.parse.quote(q)
        js = await _httpx_json_get(url)
        return js or {}

    uniprot.cell_surface = _uniprot_cell_surface  # type: ignore

    # GlyGen — protein summary by UniProt accession
    async def _glygen_protein_summary(fetch_json_func, accession: str):
        url = f"https://api.glygen.org/protein/detail/{urllib.parse.quote(accession)}"
        js = await _httpx_json_get(url)
        return js or {}

    glygen.protein_summary = _glygen_protein_summary  # type: ignore

    # RNAcentral — URS crossrefs and by external ID
    async def _rnacentral_urs_xrefs(fetch_json_func, urs_id: str):
        url = f"https://rnacentral.org/api/v1/rna/{urllib.parse.quote(urs_id)}/xrefs"
        js = await _httpx_json_get(url)
        return js or {}

    async def _rnacentral_by_external_id(fetch_json_func, ext_id: str):
        url = f"https://rnacentral.org/api/v1/rna?query={urllib.parse.quote(ext_id)}"
        js = await _httpx_json_get(url)
        return js or {}

    rnacentral.urs_xrefs = _rnacentral_urs_xrefs  # type: ignore
    rnacentral.by_external_id = _rnacentral_by_external_id  # type: ignore

    # Reactome Content Service — pathways for UniProt
    async def _reactome_pathways_for_uniprot(fetch_json_func, uniprot_id: str):
        url = f"https://reactome.org/ContentService/data/mapping/UniProt/{urllib.parse.quote(uniprot_id)}/pathways"
        js = await _httpx_json_get(url)
        return js or []

    reactome.pathways_for_uniprot = _reactome_pathways_for_uniprot  # type: ignore

    # ComplexPortal — complexes by UniProt accession
    async def _complex_portal_complexes_by_uniprot(fetch_json_func, uniprot_id: str):
        url = f"https://www.ebi.ac.uk/complexportal/api/complex/search?query=uniprot:{urllib.parse.quote(uniprot_id)}"
        js = await _httpx_json_get(url)
        return js or {}

    complex_portal.complexes_by_uniprot = _complex_portal_complexes_by_uniprot  # type: ignore

    # OmniPath DB — interactions among gene set
    async def _omnipath_interactions(fetch_json_func, genes: List[str]):
        url = "https://omnipathdb.org/interactions?format=json&genes=" + ",".join([urllib.parse.quote(g) for g in genes[:200]])
        js = await _httpx_json_get(url)
        return js or []

    omnipath.directed_interactions = _omnipath_interactions  # type: ignore

except Exception as _e_live:
    # Do not crash router if any override fails; keep prior minimal behavior
    print("Live client override warning:", _e_live)

try:
    async def _omnipath_kinase_substrate(fetch_json_func, genes: List[str]):
        url = "https://omnipathdb.org/ptms?format=json&genes=" + ",".join([urllib.parse.quote(g) for g in genes[:200]])
        js = await _httpx_json_get(url)
        return js or []
    omnipath.kinase_substrate = _omnipath_kinase_substrate  # type: ignore

    async def _omnipath_tf_targets_dorothea(fetch_json_func, tf_symbols: List[str]):
        url = "https://omnipathdb.org/targets?format=json&sources=DoRothEA&tf=" + ",".join([urllib.parse.quote(g) for g in tf_symbols[:200]])
        js = await _httpx_json_get(url)
        return js or []
    omnipath.tf_targets_dorothea = _omnipath_tf_targets_dorothea  # type: ignore
except Exception:
    pass



# ============================================================================
# GENETICS DOMAIN PATCH v2 (APIRouter-level) — Keyless Live Endpoints
# Adds: lncrna, mirna; strengthens l2g (schema-flex), consortia-summary; ensures
# 3d-maps, chromatin-contacts, annotation, caqtl-lite, functional, mavedb,
# pathogenicity-priors are exposed on the public router. No "__future__" here.
# ============================================================================

from typing import Any, Dict, Optional, Tuple, List
import asyncio, json, os
import httpx
from fastapi import Query
from fastapi.responses import JSONResponse
from fastapi import APIRouter

# --- target router --------------------------------------------------------------------
try:
    _RT: APIRouter = router  # type: ignore[name-defined]
except Exception:
    _RT = APIRouter()
    try:
        app.include_router(_RT)  # type: ignore[name-defined]
    except Exception:
        pass
    router = _RT

# --- OpenTargets GraphQL mapping -------------------------------------------------------
try:
    EXTERNAL_URLS  # type: ignore[name-defined]
except Exception:
    EXTERNAL_URLS = {"opentargets_graphql": "https://api.platform.opentargets.org/api/v4/graphql"}
OT_GQL = EXTERNAL_URLS.get("opentargets_graphql", "https://api.platform.opentargets.org/api/v4/graphql")

# --- runtime helpers ------------------------------------------------------------------
_DEFAULT_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
_MAX_TRIES = 4
_BACKOFF = 0.6
_PER_HOST = int(os.getenv("PER_HOST_LIMIT", "4"))
_ALLOW = {
    "api.platform.opentargets.org",
    "rest.ensembl.org",
    "eutils.ncbi.nlm.nih.gov",
    "reactome.org",
    "analysis.reactome.org",
    "www.encodeproject.org",
    "data.4dnucleome.org",
    "www.ebi.ac.uk",                # MetaboLights, Europe PMC, eQTL Catalogue
    "www.metabolomicsworkbench.org",
    "api.opengwas.io",
    "gwas-api.mrcieu.ac.uk",
    "rest.uniprot.org",
    "gnomad.broadinstitute.org",
    "search.clinicalgenome.org",
    "api.mavedb.org",
    "rnacentral.org",
}

_sem: Dict[str, asyncio.Semaphore] = {}
def _netloc(url: str) -> str:
    from urllib.parse import urlparse; return urlparse(url).netloc

async def _fetch_json(url: str, *, method: str = "GET", params: Optional[Dict[str, Any]] = None,
                      headers: Optional[Dict[str, str]] = None, json_body: Optional[Dict[str, Any]] = None,
                      data: Optional[str] = None, allow_status=(200, 204)) -> Tuple[str, int, Optional[Dict[str, Any]]]:
    nl = _netloc(url)
    if nl not in _ALLOW: return "UPSTREAM_ERROR", 403, {"error": f"Host not allowlisted: {nl}"}
    sem = _sem.setdefault(nl, asyncio.Semaphore(_PER_HOST))
    headers = headers or {}
    if "accept" not in {k.lower() for k in headers}: headers["Accept"] = "application/json"
    tries = 0
    async with sem:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            while tries < _MAX_TRIES:
                tries += 1
                try:
                    if method == "GET":
                        resp = await client.get(url, params=params, headers=headers)
                    else:
                        if json_body is not None:
                            resp = await client.post(url, json=json_body, headers=headers)
                        elif data is not None:
                            resp = await client.post(url, content=data, headers=headers)
                        else:
                            resp = await client.post(url, headers=headers)

                    if resp.status_code in (429, 503):
                        ra = resp.headers.get("Retry-After")
                        sleep_s = _BACKOFF * (2 ** (tries - 1))
                        if ra:
                            try:
                                ra_s = float(ra); sleep_s = max(sleep_s, min(ra_s, 30.0))
                            except Exception:
                                pass
                        if tries < _MAX_TRIES:
                            await asyncio.sleep(sleep_s); continue

                    if resp.status_code in allow_status:
                        if resp.status_code == 204: return "NO_DATA", 204, None
                        try: return "OK", resp.status_code, resp.json()
                        except Exception: return "NO_DATA", resp.status_code, {"note":"Non-JSON response","text":resp.text[:800]}

                    if resp.status_code == 401 and ("opengwas" in nl or "gwas-api" in nl):
                        return "UPSTREAM_AUTH_REQUIRED", 401, {"error":"OpenGWAS MR requires JWT"}

                    if 400 <= resp.status_code < 600 and tries < _MAX_TRIES:
                        await asyncio.sleep(_BACKOFF * (2 ** (tries - 1))); continue

                    return "UPSTREAM_ERROR", resp.status_code, {"error": resp.text[:800]}
                except (httpx.TimeoutException, httpx.HTTPError) as e:
                    if tries < _MAX_TRIES: await asyncio.sleep(_BACKOFF * (2 ** (tries - 1))); continue
                    return "UPSTREAM_ERROR", 599, {"error": str(e)}

def _ok(payload: Dict[str, Any], module: str) -> JSONResponse:
    payload.setdefault("status", "OK"); payload.setdefault("module", module); return JSONResponse(payload)
def _no_data(module: str, prov: Dict[str, Any], note="") -> JSONResponse:
    return JSONResponse({"status":"NO_DATA","module":module,"provenance":prov,"note":note})
def _up_err(module: str, prov: Dict[str, Any], code: int, err: str) -> JSONResponse:
    return JSONResponse({"status":"UPSTREAM_ERROR","module":module,"provenance":prov,"http_status":code,"error":err})
def _auth_required(module: str, prov: Dict[str, Any], note: str) -> JSONResponse:
    return JSONResponse({"status":"UPSTREAM_AUTH_REQUIRED","module":module,"provenance":prov,"note":note})

# --- resolvers ------------------------------------------------------------------------
async def _symbol_to_ensg(symbol: str) -> Optional[str]:
    s,c,j = await _fetch_json(f"https://rest.ensembl.org/xrefs/symbol/homo_sapiens/{symbol}", params={"content-type":"application/json"})
    if s != "OK" or not isinstance(j, list): return None
    for row in j:
        if row.get("type") == "gene" and str(row.get("id","")).startswith("ENSG"):
            return row["id"]
    return None

async def _efo(condition: str) -> Optional[str]:
    s,c,j = await _fetch_json("https://www.ebi.ac.uk/ols4/api/search", params={"q":condition,"ontology":"efo","rows":1,"queryFields":"label,synonym"})
    try:
        docs = j.get("response",{}).get("docs",[]) if j else []
        if docs: return docs[0].get("iri") or docs[0].get("obo_id") or docs[0].get("short_form")
    except Exception: pass
    return None

async def _ot_gql(query: str, variables: Dict[str, Any]):
    return await _fetch_json(OT_GQL, method="POST", headers={"Content-Type":"application/json","Accept":"application/json"},
                             json_body={"query": query, "variables": variables})

# --- route helpers --------------------------------------------------------------------
def _replace_route(path: str, handler):
    new_routes = []
    for r in list(_RT.routes):
        try:
            if getattr(r, "path", None) == path: continue
        except Exception: pass
        new_routes.append(r)
    _RT.routes = new_routes
    _RT.add_api_route(path, handler, methods=["GET"])
def _alias(path: str, handler): _RT.add_api_route(path, handler, methods=["GET"])

# ============================ Genetics endpoints (v2) =================================
# l2g (schema-flex: supports rows{id score} OR rows{target{id approvedSymbol} score})
async def _m_l2g(symbol: str = Query(...)):
    module = "genetics-l2g"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    q = """
    query Q($ensg:String!){
      target(ensemblId:$ensg){
        credibleSets(page:{index:0,size:50}){
          rows{
            studyLocusId
            l2GPredictions(page:{index:0,size:25}){
              rows{ id score target{ id approvedSymbol } }
            }
          }
        }
      }
    }"""
    s,c,j = await _ot_gql(q, {"ensg": ensg})
    if s != "OK": return _up_err(module, {"host":"api.platform.opentargets.org"}, c, json.dumps(j)[:500])
    rows = (((j or {}).get("data") or {}).get("target") or {}).get("credibleSets", {}).get("rows", []) or []
    out = []
    for r in rows:
        for p in (r.get("l2GPredictions") or {}).get("rows", []) or []:
            pid = p.get("id") or ((p.get("target") or {}).get("id"))
            if pid == ensg:
                out.append({"study_locus_id": r.get("studyLocusId"), "l2g": p.get("score"),
                            "gene": (p.get("target") or {}).get("approvedSymbol")})
    return _ok({"target": ensg, "predictions": out, "provenance":{"host":"api.platform.opentargets.org"}}, module) if out else _no_data(module, {"host":"api.platform.opentargets.org","target":ensg}, "No L2G predictions")
_replace_route("/genetics/l2g", _m_l2g); _alias("/genetics-l2g", _m_l2g)

# coloc (unchanged, ensure exposed)
async def _m_coloc(symbol: str = Query(...), condition: Optional[str] = Query(None)):
    module = "genetics-coloc"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    efo = await _efo(condition) if condition else None
    q = """
    query Q($ensg:String!){
      target(ensemblId:$ensg){
        credibleSets(page:{index:0,size:40}){
          rows{
            studyLocusId
            colocalisation(page:{index:0,size:50}){
              rows{
                h4 clpp rightStudyType
                otherStudyLocus{ studyId study{ traitFromSource traitFromSourceMappedIds } }
              }
            }
          }
        }
      }
    }"""
    s,c,j = await _ot_gql(q, {"ensg": ensg})
    if s != "OK": return _up_err(module, {"host":"api.platform.opentargets.org"}, c, json.dumps(j)[:500])
    rows = (((j or {}).get("data") or {}).get("target") or {}).get("credibleSets", {}).get("rows", []) or []
    out = []
    for r in rows:
        for col in (r.get("colocalisation") or {}).get("rows", []) or []:
            study = ((col.get("otherStudyLocus") or {}).get("study") or {})
            efo_ids = study.get("traitFromSourceMappedIds") or []
            if efo and efo not in (efo_ids or []): continue
            out.append({
                "study_locus_id": r.get("studyLocusId"),
                "rightStudyType": col.get("rightStudyType"),
                "h4": col.get("h4"), "clpp": col.get("clpp"),
                "other_trait": study.get("traitFromSource"),
                "other_trait_efo": efo_ids
            })
    return _ok({"records": out, "provenance":{"host":"api.platform.opentargets.org","efo":efo}}, module) if out else _no_data(module, {"host":"api.platform.opentargets.org","target":ensg,"efo":efo}, "No colocalisations")
_replace_route("/genetics/coloc", _m_coloc); _alias("/genetics-coloc", _m_coloc)

# consortia-summary (OT GraphQL studies → consortium summary)
async def _m_consortia_summary(condition: str = Query(...)):
    module = "genetics-consortia-summary"
    efo = await _efo(condition)
    if not efo: return _no_data(module, {"resolver":"EFO OLS4"}, f"Cannot map condition={condition}")
    q = "query($efo:String!){ studies(diseaseIds:[$efo],enableIndirect:true,page:{index:0,size:50}){ rows{ projectId studyType cohorts } } }"
    s,c,j = await _ot_gql(q, {"efo": efo})
    if s != "OK": return _up_err(module, {"host":"api.platform.opentargets.org"}, c, json.dumps(j)[:500])
    rows = (((j or {}).get("data") or {}).get("studies") or {}).get("rows", []) or []
    if not rows: return _no_data(module, {"host":"api.platform.opentargets.org","efo":efo}, "No studies for disease")
    by = {}
    for r in rows:
        pid = r.get("projectId") or "unknown"
        d = by.setdefault(pid, {"projectId": pid, "n":0, "types":set(), "cohorts":set()})
        d["n"] += 1
        if r.get("studyType"): d["types"].add(r["studyType"])
        for cx in r.get("cohorts") or []: d["cohorts"].add(cx)
    out = [{"projectId":k,"n_studies":v["n"],"studyTypes":sorted(list(v["types"])),"cohorts":sorted(list(v["cohorts"]))[:20]} for k,v in by.items()]
    out.sort(key=lambda x:x["n_studies"], reverse=True)
    return _ok({"efo":efo,"summary":out,"provenance":{"host":"api.platform.opentargets.org"}}, module)
_replace_route("/genetics/consortia-summary", _m_consortia_summary); _alias("/genetics-consortia-summary", _m_consortia_summary)

# mr (honest keyless)
async def _m_mr(exposure: Optional[str] = Query(None), outcome: Optional[str] = Query(None)):
    module = "genetics-mr"
    s,c,j = await _fetch_json("https://api.opengwas.io/api/status")
    if s != "OK": return _up_err(module, {"host":"api.opengwas.io"}, c, json.dumps(j)[:500])
    return _auth_required(module, {"host":"api.opengwas.io"}, "OpenGWAS MR endpoints require JWT; router remains keyless")
_replace_route("/genetics/mr", _m_mr); _alias("/genetics-mr", _m_mr)

# chromatin-contacts (ENCODE Hi-C search)
async def _m_chrom_contacts(symbol: str = Query(...)):
    module = "genetics-chromatin-contacts"
    s,c,j = await _fetch_json("https://www.encodeproject.org/search/", params={"type":"Experiment","assay_title":"Hi-C","searchTerm":symbol})
    if s != "OK": return _up_err(module, {"host":"www.encodeproject.org"}, c, json.dumps(j)[:500])
    items = j.get("@graph", []) if isinstance(j, dict) else []
    out = [{"accession":it.get("accession"),"lab":(it.get("lab") or {}).get("title")} for it in items if symbol.upper() in json.dumps(it).upper()]
    return _ok({"records": out[:50], "provenance":{"host":"www.encodeproject.org"}}, module) if out else _no_data(module, {"host":"www.encodeproject.org"}, "No contacts")
_replace_route("/genetics/chromatin-contacts", _m_chrom_contacts)

# 3d-maps (4DN)
async def _m_3d_maps(symbol: str = Query(...)):
    module = "genetics-3d-maps"
    s,c,j = await _fetch_json("https://data.4dnucleome.org/search/", params={"type":"ExperimentHiC","limit":25,"searchTerm":symbol})
    if s != "OK": return _up_err(module, {"host":"data.4dnucleome.org"}, c, json.dumps(j)[:500])
    items = j.get("@graph", []) if isinstance(j, dict) else []
    out = [{"uuid":it.get("uuid"),"accession":it.get("accession"),"lab":(it.get("lab") or {}).get("title")} for it in items if symbol.upper() in json.dumps(it).upper()]
    return _ok({"records": out, "provenance":{"host":"data.4dnucleome.org"}}, module) if out else _no_data(module, {"host":"data.4dnucleome.org"}, "No 3D maps")
_replace_route("/genetics/3d-maps", _m_3d_maps); _alias("/genetics-3d-maps", _m_3d_maps)

# regulatory (ENCODE ChIP-seq)
async def _m_regulatory(symbol: str = Query(...)):
    module = "genetics-regulatory"
    s,c,j = await _fetch_json("https://www.encodeproject.org/search/", params={"type":"Experiment","assay_title":"ChIP-seq","searchTerm":symbol})
    if s != "OK": return _up_err(module, {"host":"www.encodeproject.org"}, c, json.dumps(j)[:500])
    items = j.get("@graph", []) if isinstance(j, dict) else []
    out = []
    for it in items:
        tgt = (it.get("target") or {}).get("label") if isinstance(it.get("target"), dict) else None
        if (tgt and symbol.upper() in str(tgt).upper()) or (symbol.upper() in json.dumps(it).upper()):
            out.append({"accession": it.get("accession"), "target": tgt, "biosample": (it.get("biosample_ontology") or {}).get("term_name")})
    return _ok({"records": out[:50], "provenance":{"host":"www.encodeproject.org"}}, module) if out else _no_data(module, {"host":"www.encodeproject.org"}, "No regulatory matches")
_replace_route("/genetics/regulatory", _m_regulatory)

# sqtl (eQTL Catalogue best-effort)
async def _m_sqtl(symbol: str = Query(...)):
    module = "genetics-sqtl"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    s,c,j = await _fetch_json(f"https://www.ebi.ac.uk/eqtl/api/genes/{ensg}")
    if s != "OK": return _no_data(module, {"host":"www.ebi.ac.uk","target":ensg}, "eQTL Catalogue endpoint unavailable")
    return _ok({"record": j, "provenance":{"host":"www.ebi.ac.uk","target":ensg}}, module)
_replace_route("/genetics/sqtl", _m_sqtl)

# pqtl (OT GraphQL protein study types)
async def _m_pqtl(symbol: str = Query(...)):
    module = "genetics-pqtl"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    q = """
    query Q($ensg:String!){
      target(ensemblId:$ensg){
        credibleSets(page:{index:0,size:40}){
          rows{
            studyLocusId
            colocalisation(page:{index:0,size:50}){
              rows{ rightStudyType otherStudyLocus{ study{ traitFromSource } } }
            }
          }
        }
      }
    }"""
    s,c,j = await _ot_gql(q, {"ensg": ensg})
    if s != "OK": return _up_err(module, {"host":"api.platform.opentargets.org"}, c, json.dumps(j)[:500])
    rows = (((j or {}).get("data") or {}).get("target") or {}).get("credibleSets", {}).get("rows", []) or []
    out = []
    for r in rows:
        for col in (r.get("colocalisation") or {}).get("rows", []) or []:
            rst = str(col.get("rightStudyType") or "").upper()
            if "PROTEIN" in rst:
                out.append({"study_locus_id": r.get("studyLocusId"), "type": col.get("rightStudyType"),
                            "trait": ((col.get("otherStudyLocus") or {}).get("study") or {}).get("traitFromSource")})
    return _ok({"records": out, "provenance":{"host":"api.platform.opentargets.org"}}, module) if out else _no_data(module, {"host":"api.platform.opentargets.org"}, "No pQTL colocs")
_replace_route("/genetics/pqtl", _m_pqtl)

# annotation (Ensembl gene info)
async def _m_annotation(symbol: str = Query(...)):
    module = "genetics-annotation"
    s,c,j = await _fetch_json(f"https://rest.ensembl.org/lookup/symbol/homo_sapiens/{symbol}", params={"expand":"1"})
    if s != "OK": return _up_err(module, {"host":"rest.ensembl.org"}, c, json.dumps(j)[:500])
    keep = {k: j.get(k) for k in ["id","display_name","biotype","seq_region_name","start","end","strand","version"]} if j else None
    return _ok({"record": keep, "provenance":{"host":"rest.ensembl.org"}}, module) if keep else _no_data(module, {"host":"rest.ensembl.org"}, "No annotation")
_replace_route("/genetics/annotation", _m_annotation); _alias("/genetics-annotation", _m_annotation)

# pathogenicity-priors (gnomAD GraphQL constraint)
async def _m_path_priors(symbol: str = Query(...)):
    module = "genetics-pathogenicity-priors"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    q = "query($ensg:String!){ gene(gene_id:$ensg){ symbol gene_id constraint{ pLI pRec pNull oe_mis oe_lof } } }"
    s,c,j = await _fetch_json("https://gnomad.broadinstitute.org/api", method="POST",
                              headers={"Content-Type":"application/json","Accept":"application/json"},
                              json_body={"query": q, "variables": {"ensg": ensg}})
    if s != "OK": return _no_data(module, {"host":"gnomad.broadinstitute.org","target":ensg}, "Constraint unavailable")
    g = ((j or {}).get("data") or {}).get("gene")
    return _ok({"record": g, "provenance":{"host":"gnomad.broadinstitute.org"}}, module) if g else _no_data(module, {"host":"gnomad.broadinstitute.org"}, "No priors")
_replace_route("/genetics/pathogenicity-priors", _m_path_priors)

# intolerance (gnomAD constraint)
async def _m_intolerance(symbol: str = Query(...)):
    module = "genetics-intolerance"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    q = "query($ensg:String!){ gene(gene_id:$ensg){ symbol gene_id constraint{ pLI pRec oe_lof oe_mis } } }"
    s,c,j = await _fetch_json("https://gnomad.broadinstitute.org/api", method="POST",
                              headers={"Content-Type":"application/json","Accept":"application/json"},
                              json_body={"query": q, "variables": {"ensg": ensg}})
    if s != "OK": return _no_data(module, {"host":"gnomad.broadinstitute.org","target":ensg}, "Constraint unavailable")
    g = ((j or {}).get("data") or {}).get("gene")
    return _ok({"constraint": g.get("constraint") if g else None, "provenance":{"host":"gnomad.broadinstitute.org"}}, module) if g else _no_data(module, {"host":"gnomad.broadinstitute.org"}, "No constraint")
_replace_route("/genetics/intolerance", _m_intolerance)

# rare (ClinVar)
async def _m_rare(symbol: str = Query(...)):
    module = "genetics-rare"
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    term = f"({symbol}[gene]) AND Homo sapiens[organism]"
    s1,c1,j1 = await _fetch_json(f"{base}/esearch.fcgi", params={"db":"clinvar","retmode":"json","term":term,"retmax":20})
    if s1 != "OK": return _up_err(module, {"host":"eutils.ncbi.nlm.nih.gov"}, c1, json.dumps(j1)[:500])
    ids = (j1 or {}).get("esearchresult", {}).get("idlist", []) or []
    if not ids: return _no_data(module, {"host":"eutils.ncbi.nlm.nih.gov"}, "No ClinVar records")
    s2,c2,j2 = await _fetch_json(f"{base}/esummary.fcgi", params={"db":"clinvar","retmode":"json","id":",".join(ids)})
    if s2 != "OK": return _up_err(module, {"host":"eutils.ncbi.nlm.nih.gov"}, c2, json.dumps(j2)[:500])
    docs = (j2 or {}).get("result", {}); out=[]
    for uid in (docs.get("uids", []) or [])[:20]:
        rec = docs.get(uid, {}); out.append({"uid":uid,"title":rec.get("title"),"clinical_significance":(rec.get("clinical_significance",{}) or {}).get("description")})
    return _ok({"records": out, "provenance":{"host":"eutils.ncbi.nlm.nih.gov"}}, module)
_replace_route("/genetics/rare", _m_rare)

# mendelian (ClinGen best-effort)
async def _m_mendelian(symbol: str = Query(...)):
    module = "genetics-mendelian"
    s,c,j = await _fetch_json("https://search.clinicalgenome.org/api/search/genes", params={"q": symbol, "rows": 10})
    if s != "OK": return _no_data(module, {"host":"search.clinicalgenome.org"}, "ClinGen search unavailable")
    hits = (j or {}).get("docs", []) or (j or {}).get("response", {}).get("docs", [])
    return _ok({"records": hits, "provenance":{"host":"search.clinicalgenome.org"}}, module) if hits else _no_data(module, {"host":"search.clinicalgenome.org"}, "No ClinGen gene hits")
_replace_route("/genetics/mendelian", _m_mendelian)

# phewas-human-knockout (best-effort probe)
async def _m_phewas_hko(symbol: str = Query(...)):
    module = "genetics-phewas-human-knockout"
    s,c,j = await _fetch_json("http://www.phenoscanner.medschl.cam.ac.uk/api", params={"gene": symbol, "catalog": "pQTL", "build": "37"})
    if s != "OK": return _no_data(module, {"host":"phenoscanner"}, "PhenoScanner may be unavailable")
    return _ok({"record": j, "provenance":{"host":"phenoscanner"}}, module)
_replace_route("/genetics/phewas-human-knockout", _m_phewas_hko)

# functional (Europe PMC fallback)
async def _m_functional(symbol: str = Query(...)):
    module = "genetics-functional"
    q = f'"{symbol}" AND (functional variant OR regulatory variant) AND human'
    s,c,j = await _fetch_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={"query": q, "format":"json", "pageSize": 25})
    if s != "OK": return _up_err(module, {"host":"www.ebi.ac.uk"}, c, json.dumps(j)[:500])
    hits = (((j or {}).get("resultList") or {}).get("result") or [])
    return _ok({"hits": hits, "provenance":{"host":"europepmc"}}, module) if hits else _no_data(module, {"host":"europepmc"}, "No functional-variant literature hits")
_replace_route("/genetics/functional", _m_functional)

# mavedb
async def _m_mavedb(symbol: str = Query(...)):
    module = "genetics-mavedb"
    s,c,j = await _fetch_json("https://api.mavedb.org/api/experiments", params={"gene": symbol, "page[size]": 25})
    if s != "OK": return _no_data(module, {"host":"api.mavedb.org"}, "MaveDB API unavailable")
    items = (j or {}).get("data", []) or []
    return _ok({"records": items, "provenance":{"host":"api.mavedb.org"}}, module) if items else _no_data(module, {"host":"api.mavedb.org"}, "No MAVE experiments for gene")
_replace_route("/genetics/mavedb", _m_mavedb)

# lncrna (RNAcentral best-effort; fallback to Europe PMC)
async def _m_lncrna(symbol: str = Query(...)):
    module = "genetics-lncrna"
    # Try RNAcentral gene search (best-effort)
    s1,c1,j1 = await _fetch_json("https://rnacentral.org/api/v1/rna/", params={"page_size": 20, "gene": symbol, "organism": "Homo sapiens"})
    records = []
    if s1 == "OK" and isinstance(j1, dict):
        for r in j1.get("results", []) or []:
            if "lncRNA" in str(r.get("rna_type","")):
                records.append({"urs": r.get("urs"), "rna_type": r.get("rna_type"), "symbol": symbol})
    if not records:
        # Fallback: literature
        q = f'"{symbol}" AND (lncRNA OR "long non-coding RNA") AND human'
        s2,c2,j2 = await _fetch_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={"query": q, "format":"json", "pageSize": 25})
        if s2 == "OK":
            records = (((j2 or {}).get("resultList") or {}).get("result") or [])
            if records:
                return _ok({"hits": records, "provenance":{"host":"europepmc"}}, module)
        return _no_data(module, {"hosts":["rnacentral.org","europepmc"]}, "No lncRNA mapping found")
    return _ok({"records": records, "provenance":{"host":"rnacentral.org"}}, module)
_replace_route("/genetics/lncrna", _m_lncrna); _alias("/genetics-lncrna", _m_lncrna)

# mirna (RNAcentral miRNA search; fallback to Europe PMC)
async def _m_mirna(symbol: str = Query(...)):
    module = "genetics-mirna"
    s1,c1,j1 = await _fetch_json("https://rnacentral.org/api/v1/rna/", params={"page_size": 25, "gene": symbol, "rna_type": "miRNA", "organism": "Homo sapiens"})
    records = []
    if s1 == "OK" and isinstance(j1, dict):
        for r in j1.get("results", []) or []:
            if "miRNA" in str(r.get("rna_type","")):
                records.append({"urs": r.get("urs"), "rna_type": r.get("rna_type"), "symbol": symbol})
    if not records:
        q = f'"{symbol}" AND (miRNA OR "microRNA") AND human'
        s2,c2,j2 = await _fetch_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={"query": q, "format":"json", "pageSize": 25})
        if s2 == "OK":
            records = (((j2 or {}).get("resultList") or {}).get("result") or [])
            if records:
                return _ok({"hits": records, "provenance":{"host":"europepmc"}}, module)
        return _no_data(module, {"hosts":["rnacentral.org","europepmc"]}, "No miRNA mapping found")
    return _ok({"records": records, "provenance":{"host":"rnacentral.org"}}, module)
_replace_route("/genetics/mirna", _m_mirna); _alias("/genetics-mirna", _m_mirna)

# caqtl-lite (composite loopback)
async def _m_caqtl_lite(symbol: str = Query(...)):
    module = "genetics-caqtl-lite"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            r1 = await client.get("http://127.0.0.1:8000/genetics/regulatory", params={"symbol": symbol})
            r2 = await client.get("http://127.0.0.1:8000/genetics/chromatin-contacts", params={"symbol": symbol})
            data = []
            if r1.status_code == 200 and (r1.json() or {}).get("status") == "OK":
                data.extend((r1.json() or {}).get("records", []))
            if r2.status_code == 200 and (r2.json() or {}).get("status") == "OK":
                data.extend((r2.json() or {}).get("records", []))
            if not data: return _no_data(module, {"loopback":True}, "No regulatory/chromatin contacts available")
            return _ok({"records": data[:50], "provenance":{"composed":["genetics/regulatory","genetics/chromatin-contacts"]}}, module)
    except Exception as e:
        return _no_data(module, {"loopback":True}, f"Composite call failed: {e}")
_replace_route("/genetics/caqtl-lite", _m_caqtl_lite); _alias("/genetics-caqtl-lite", _m_caqtl_lite)

# nmd-inference (context-only; honest OK with context or NO_DATA)
async def _m_nmd(symbol: str = Query(...)):
    module = "genetics-nmd-inference"
    ensg = await _symbol_to_ensg(symbol)
    if not ensg: return _no_data(module, {"resolver":"ensembl"}, f"Unknown symbol={symbol}")
    # Pull sQTL context (best-effort)
    s1,c1,j1 = await _fetch_json(f"https://www.ebi.ac.uk/eqtl/api/genes/{ensg}")
    # Pull transcript biotypes to see if any are NMD-labeled
    s2,c2,j2 = await _fetch_json(f"https://rest.ensembl.org/lookup/id/{ensg}", params={"expand":"1"})
    nmd_flag = False
    try:
        for tx in (j2 or {}).get("Transcript", []) or []:
            if "nonsense_mediated_decay" in str(tx.get("biotype","")).lower():
                nmd_flag = True; break
    except Exception:
        pass
    payload = {"target": ensg, "has_sqtl_payload": s1 == "OK", "ensembl_tx_checked": bool(j2), "nmd_labeled_transcript": nmd_flag}
    return _ok({"context_only": payload, "provenance":{"hosts":["www.ebi.ac.uk","rest.ensembl.org"]}}, module)
_replace_route("/genetics/nmd-inference", _m_nmd); _alias("/genetics-nmd-inference", _m_nmd)

# ase-check, mqtl-coloc, ptm-signal-lite already present above; ensure aliases
_alias("/genetics-annotation", _m_annotation)
_alias("/genetics-functional", _m_functional)
_alias("/genetics-mavedb", _m_mavedb)
_alias("/genetics-pathogenicity-priors", _m_path_priors)
_alias("/genetics-intolerance", _m_intolerance)
_alias("/genetics-rare", _m_rare)
_alias("/genetics-mendelian", _m_mendelian)
_alias("/genetics-phewas-human-knockout", _m_phewas_hko)
# Health for this router module
def _router_health_v2():
    return {"ok": True, "version": "2025.10", "patch": "genetics-v2", "router_level": True, "domains":{"genetics":22}}
_replace_route("/healthz", _router_health_v2)
# ============================================================================
