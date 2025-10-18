# main.py
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Robust router import: supports `router.py` at root or `app/router.py`,
# and also searches Render's working paths if needed.
# -----------------------------------------------------------------------------
def _load_router_module():
    import importlib.util
    import sys

    # 1) Try normal module imports first
    err1 = err2 = None
    try:
        import router as _router_mod  # type: ignore
        return _router_mod
    except Exception as e:
        err1 = e
    try:
        import app.router as _router_mod  # type: ignore
        return _router_mod
    except Exception as e:
        err2 = e

    # 2) Try common file locations explicitly
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "router.py"),
        os.path.join(here, "app", "router.py"),
        os.path.join(os.path.dirname(here), "router.py"),
        os.path.join(os.path.dirname(here), "app", "router.py"),
        "/opt/render/project/src/router.py",
        "/opt/render/project/src/app/router.py",
    ]
    for cand in candidates:
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("router", cand)
            mod = importlib.util.module_from_spec(spec)  # type: ignore
            sys.modules["router"] = mod
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(mod)  # type: ignore
            return mod

    raise RuntimeError(
        "Failed to import router module from any known location. "
        f"Errors: import router -> {err1!s}; import app.router -> {err2!s}"
    )


_router_mod = _load_router_module()

# The APIRouter instance the app will mount
tv_router = getattr(_router_mod, "router")

# Optional metadata (if router exports them)
MODULES = getattr(_router_mod, "MODULES", None)
ROUTER_DOMAINS_META = getattr(_router_mod, "DOMAINS_META", None)
ROUTER_DOMAIN_MODULES = getattr(_router_mod, "DOMAIN_MODULES", None)


# -----------------------------------------------------------------------------
# App configuration
# -----------------------------------------------------------------------------
APP_TITLE = os.getenv("APP_TITLE", "TargetVal Gateway (Actions Surface) — Full")
APP_VERSION = os.getenv("APP_VERSION", "2025.10")
ROOT_PATH = os.getenv("ROOT_PATH", "")            # e.g., when behind a proxy
DOCS_URL = os.getenv("DOCS_URL", "/docs")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    docs_url=DOCS_URL,
    openapi_url=OPENAPI_URL,
    root_path=ROOT_PATH,
)

# CORS: permissive by default; tighten with CORS_ALLOW_ORIGINS env if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the router (all feature routes live here)
app.include_router(tv_router, prefix="/v1")


# -----------------------------------------------------------------------------
# Public schemas (stable envelopes/types the gateway exposes)
# -----------------------------------------------------------------------------
class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float
    debug: Optional[Dict[str, Any]] = None


class ModuleRunRequest(BaseModel):
    module_key: str = Field(..., description="One of the 64 keys from /v1/modules")
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
    symbol: Optional[str] = None
    ensembl_id: Optional[str] = None
    uniprot_id: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    tissue: Optional[str] = None
    cell_type: Optional[str] = None
    species: Optional[str] = None
    limit: Optional[int] = Field(100, ge=1, le=500)
    offset: Optional[int] = Field(0, ge=0)
    strict: Optional[bool] = False
    extra: Optional[Dict[str, Any]] = None


class AggregateRequest(BaseModel):
    modules: Optional[List[str]] = None
    domain: Optional[str] = Field(None, description="Accepts 'D1'..'D6' or '1'..'6'.")
    primary_only: bool = True
    order: str = Field("sequential", pattern="^(sequential|parallel)$")
    continue_on_error: bool = True
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
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


class DomainRunRequest(BaseModel):
    primary_only: bool = True
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
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


# -----------------------------------------------------------------------------
# Domain metadata fallback (if router doesn’t export them)
# -----------------------------------------------------------------------------
DOMAINS_META = ROUTER_DOMAINS_META or {
    1: {"name": "Genetic causality & human validation"},
    2: {"name": "Functional & mechanistic validation"},
    3: {"name": "Expression, selectivity & cell-state context"},
    4: {"name": "Druggability & modality tractability"},
    5: {"name": "Therapeutic index & safety translation"},
    6: {"name": "Clinical & translational evidence"},
}


def _domain_modules() -> Dict[str, List[str]]:
    if ROUTER_DOMAIN_MODULES:
        # Normalize keys as strings and "D#" aliases
        D = {str(k): v for k, v in ROUTER_DOMAIN_MODULES.items()}
        D.update({f"D{k}": v for k, v in ROUTER_DOMAIN_MODULES.items()})
        return D

    # Fallback static mapping (kept here for OpenAPI stability)
    D1 = [
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
    ]
    D2 = [
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
    ]
    D3 = [
        "expr-baseline",
        "expr-inducibility",
        "assoc-sc",
        "assoc-spatial",
        "sc-hubmap",
        "expr-localization",
        "assoc-hpa-pathology",
        "tract-ligandability-ab",
        "tract-surfaceome",
    ]
    D4 = [
        "mech-structure",
        "tract-ligandability-sm",
        "tract-ligandability-oligo",
        "tract-modality",
        "tract-drugs",
        "perturb-drug-response",
    ]
    D5 = [
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
    D6 = [
        "clin-endpoints",
        "clin-biomarker-fit",
        "clin-pipeline",
        "clin-feasibility",
        "comp-intensity",
        "comp-freedom",
    ]
    return {
        "1": D1,
        "2": D2,
        "3": D3,
        "4": D4,
        "5": D5,
        "6": D6,
        "D1": D1,
        "D2": D2,
        "D3": D3,
        "D4": D4,
        "D5": D5,
        "D6": D6,
    }


# -----------------------------------------------------------------------------
# Health / readiness
# -----------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}
