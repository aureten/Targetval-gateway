
# main.py (2025-10-17)
# TargetVal Gateway — Actions Surface (scoreless), aligned with 64-module config
# Wires the router (per-module live fetchers) and exposes generic /module, /aggregate, /domain, /lit, /synth endpoints.

import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Path as _Path, Query, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from router import router as module_router
from router import MODULES  # list[Module] with .name and .route

# App init
app = FastAPI(title="TargetVal Gateway (Actions Surface) — Full", version="2025.10", openapi_url="/v1/openapi.json")

# Include the per-module router under /v1
app.include_router(module_router, prefix="/v1")

from fastapi.middleware.cors import CORSMiddleware

# Import the merged router + inline registry + dispatcher from router module
# Expect this module to live as `targetval_router.py` at import path.
from targetval_router import (
    router as tv_router,
    MODCFG,
    ModuleParams,
    dispatch_module,
    Evidence,
)

APP_NAME = os.getenv("APP_NAME", "Targetval Gateway")
APP_VERSION = os.getenv("APP_VERSION", "noyaml-main-1")
ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
REDOC_URL = os.getenv("REDOC_URL", "/redoc")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        root_path=ROOT_PATH,
        docs_url=DOCS_URL,
        redoc_url=REDOC_URL,
        openapi_url=OPENAPI_URL,
    )

    # CORS: wide open by default; tighten as needed in Render env
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[os.getenv("ALLOW_ORIGINS", "*")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---- Data models (mirror openapi.json) ----

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: str
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
    order: str = Field("sequential", regex="^(sequential|parallel)$")
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

class SynthIntegrateRequest(BaseModel):
    gene: Optional[str] = Field(None, description="Alias of 'symbol'.")
    symbol: Optional[str] = None
    efo: Optional[str] = None
    condition: Optional[str] = None
    modules: Optional[List[str]] = None
    method: str = Field("math", regex="^(math|vote|rank|bayes|hybrid)$")
    extra: Optional[Dict[str, Any]] = None

# ---- Registry helpers ----

# Build name->route map from MODULES
NAME_TO_ROUTE: Dict[str, str] = {m.name: m.route for m in MODULES}

# Domain→module keys order (from config)
DOMAIN_MODULES: Dict[str, List[str]] = {
    "1": ["genetics-l2g","genetics-coloc","genetics-mr","genetics-chromatin-contacts","genetics-3d-maps",
          "genetics-regulatory","genetics-sqtl","genetics-pqtl","genetics-annotation","genetics-pathogenicity-priors",
          "genetics-intolerance","genetics-rare","genetics-mendelian","genetics-phewas-human-knockout",
          "genetics-functional","genetics-mavedb","genetics-consortia-summary"],
    "2": ["mech-pathways","biology-causal-pathways","mech-ppi","mech-ligrec","assoc-proteomics","assoc-metabolomics",
          "assoc-bulk-rna","assoc-perturb","perturb-lincs-signatures","perturb-connectivity","perturb-signature-enrichment",
          "perturb-perturbseq-encode","perturb-crispr-screens","genetics-lncrna","genetics-mirna",
          "perturb-qc","perturb-scrna-summary"],
    "3": ["expr-baseline","expr-inducibility","assoc-sc","assoc-spatial","sc-hubmap","expr-localization",
          "assoc-hpa-pathology","tract-ligandability-ab","tract-surfaceome"],
    "4": ["mech-structure","tract-ligandability-sm","tract-ligandability-oligo","tract-modality","tract-drugs","perturb-drug-response"],
    "5": ["function-dependency","immuno/hla-coverage","tract-immunogenicity","tract-mhc-binding","tract-iedb-epitopes",
          "clin-safety","clin-rwe","clin-on-target-ae-prior","perturb-depmap-dependency"],
    "6": ["clin-endpoints","clin-biomarker-fit","clin-pipeline","clin-feasibility","comp-intensity","comp-freedom"],
}

# ---- Utilities ----

def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

async def _call_module(route: str, params: Dict[str, Any]) -> Dict[str, Any]:
    # Route is like "/expr/baseline"; mounted under "/v1"
    import httpx
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.get("/v1" + route, params=params)
        if resp.status_code != 200:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HTTPException(status_code=resp.status_code, detail={"route": route, "error": detail})
        return resp.json()

def _ensure_symbol(query: Dict[str, Any]) -> Dict[str, Any]:
    # Accept 'gene' and map to 'symbol' if only gene is present
    q = dict(query or {})
    if "symbol" not in q and "gene" in q and q["gene"]:
        q["symbol"] = q["gene"]
    return q

# ---- Actions Surface ----

@app.get("/v1/healthz", summary="Health check", operation_id="healthzV1")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "ts": _now_iso(), "modules": len(MODULES)}

@app.get("/v1/modules", summary="List canonical module keys (64) from registry", operation_id="listModulesV1")
async def list_modules() -> List[str]:
    return sorted(NAME_TO_ROUTE.keys())

@app.post("/v1/module", summary="Run a single module by key", operation_id="runModuleV1", response_model=Evidence)
async def run_module(req: ModuleRunRequest = Body(...)) -> Any:
    key = req.module_key
    route = NAME_TO_ROUTE.get(key)
    if not route:
        raise HTTPException(status_code=404, detail=f"Unknown module_key: {key}")
    params = _ensure_symbol({
        "gene": req.gene,
        "symbol": req.symbol,
        "ensembl_id": req.ensembl_id,
        "uniprot_id": req.uniprot_id,
        "efo": req.efo,
        "condition": req.condition,
        "tissue": req.tissue,
        "cell_type": req.cell_type,
        "species": req.species,
        "limit": req.limit,
        "offset": req.offset,
        "strict": req.strict,
    })
    res = await _call_module(route, params)
    return res

@app.get("/v1/module/{name}", summary="Run a single module by key (GET) (supports perturb-* keys)", operation_id="runModuleByNameV1", response_model=Evidence)
async def run_module_by_name(
    name: str = _Path(...),
    gene: Optional[str] = Query(None, description="Alias of 'symbol'."),
    symbol: Optional[str] = Query(None),
    ensembl_id: Optional[str] = Query(None),
    uniprot_id: Optional[str] = Query(None),
    efo: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    tissue: Optional[str] = Query(None),
    cell_type: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    limit: Optional[int] = Query(100, ge=1, le=500),
    offset: Optional[int] = Query(0, ge=0),
    strict: Optional[bool] = Query(False),
) -> Any:
    route = NAME_TO_ROUTE.get(name)
    if not route:
        raise HTTPException(status_code=404, detail=f"Unknown module key: {name}")
    params = _ensure_symbol({
        "gene": gene, "symbol": symbol, "ensembl_id": ensembl_id, "uniprot_id": uniprot_id,
        "efo": efo, "condition": condition, "tissue": tissue, "cell_type": cell_type, "species": species,
        "limit": limit, "offset": offset, "strict": strict
    })
    return await _call_module(route, params)

@app.post("/v1/aggregate", summary="Run many modules; sequential or parallel; optional domain filter", operation_id="aggregateModulesV1")
async def aggregate_modules(req: AggregateRequest) -> Dict[str, Any]:
    # Determine list of module keys
    keys: List[str] = []
    if req.modules:
        keys = req.modules
    elif req.domain:
        d = str(req.domain).replace("D","")
        keys = DOMAIN_MODULES.get(d, [])
    else:
        raise HTTPException(status_code=422, detail="Provide 'modules' or 'domain'.")
    # Filter unknown
    missing = [k for k in keys if k not in NAME_TO_ROUTE]
    if missing:
        raise HTTPException(status_code=404, detail={"unknown_module_keys": missing})

    qbase = _ensure_symbol({
        "gene": req.gene, "symbol": req.symbol, "ensembl_id": req.ensembl_id, "uniprot_id": req.uniprot_id,
        "efo": req.efo, "condition": req.condition, "tissue": req.tissue, "cell_type": req.cell_type,
        "species": req.species, "limit": req.limit, "offset": req.offset
    })

    results: Dict[str, Any] = {"status": "OK", "fetched_at": _now_iso(), "items": []}

    async def _run_one(k: str) -> Tuple[str, Dict[str, Any], Optional[str]]:
        try:
            route = NAME_TO_ROUTE[k]
            res = await _call_module(route, qbase)
            return k, res, None
        except Exception as e:
            if req.continue_on_error:
                return k, {"status": "ERROR", "source": "aggregate", "fetched_n": 0, "data": {"error": str(e)}, "citations": [], "fetched_at": _now_iso()}, str(e)
            raise

    if req.order == "parallel":
        vals = await asyncio.gather(*[_run_one(k) for k in keys])
    else:
        vals = []
        for k in keys:
            vals.append(await _run_one(k))

    for k, res, err in vals:
        results["items"].append({"module": k, "result": res})
    return results

@app.post("/v1/domain/{domain_id}/run", summary="Run all modules of a domain sequentially", operation_id="runDomainV1")
async def run_domain(domain_id: int = _Path(..., ge=1, le=6), req: DomainRunRequest = Body(None)) -> Dict[str, Any]:
    keys = DOMAIN_MODULES.get(str(domain_id), [])
    if not keys:
        raise HTTPException(status_code=404, detail=f"Unknown domain: {domain_id}")
    data = req.dict() if req else {}
    areq = AggregateRequest(**data, modules=keys, order="sequential")
    return await aggregate_modules(areq)

# --- Literature proxy endpoints (pass-through to router-defined implementations where available) ---

@app.get("/v1/lit/meta", summary="Literature meta/search", operation_id="litMetaV1")
async def lit_meta(query: Optional[str] = Query(None), gene: Optional[str] = Query(None), symbol: Optional[str] = Query(None),
                   efo: Optional[str] = Query(None), condition: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
    params = {"query": query, "gene": gene, "symbol": symbol, "efo": efo, "condition": condition, "limit": limit}
    try:
        return await _call_module("/lit/meta", params)
    except Exception:
        return {"status": "ERROR", "source": "lit-meta", "fetched_n": 0, "data": params, "citations": [], "fetched_at": _now_iso()}

@app.get("/v1/lit/search", summary="Europe PMC search (5y default window)", operation_id="litSearchV1", response_model=Evidence)
async def lit_search(symbol: str = Query(...), condition: Optional[str] = Query(None), window_days: int = Query(1825), page_size: int = Query(50)) -> Any:
    return await _call_module("/lit/search", {"symbol": symbol, "condition": condition, "window_days": window_days, "page_size": page_size})

@app.get("/v1/lit/claims", summary="PubTator3 relations proxy", operation_id="litClaimsV1", response_model=Evidence)
async def lit_claims(symbol: str = Query(...), condition: Optional[str] = Query(None), limit: int = Query(50)) -> Any:
    return await _call_module("/lit/claims", {"symbol": symbol, "condition": condition, "limit": limit})

@app.post("/v1/lit/score", summary="Score literature items (objective rubric)", operation_id="litScoreV1", response_model=Evidence)
async def lit_score(payload: Dict[str, Any] = Body(...)) -> Any:
    return await _call_module("/lit/score", payload)

# --- Synthesis endpoints (qualitative; no per-module grades) ---

@app.post("/v1/synth/targetcard", summary="Assemble a target card (modules + literature)", operation_id="synthTargetcardV1", response_model=Evidence)
async def synth_targetcard(req: SynthIntegrateRequest) -> Any:
    return await _call_module("/synth/targetcard", req.dict())

@app.post("/v1/synth/integrate", summary="Synthesis/integration across modules (qualitative; no per-module scores)", operation_id="synthIntegrateV1")
async def synth_integrate(req: SynthIntegrateRequest) -> Dict[str, Any]:
    return await _call_module("/synth/integrate", req.dict())

@app.get("/v1/synth/bucket", summary="Mathematical synthesis per bucket", operation_id="synthBucketV1")
async def synth_bucket(name: str = Query(...), gene: Optional[str] = Query(None), symbol: Optional[str] = Query(None),
                       efo: Optional[str] = Query(None), condition: Optional[str] = Query(None),
                       limit: int = Query(50, ge=1, le=1000), mode: Optional[str] = Query(None, regex="^(auto|live|snapshot)$")) -> Dict[str, Any]:
    return await _call_module("/synth/bucket", {"name": name, "gene": gene, "symbol": symbol, "efo": efo, "condition": condition, "limit": limit, "mode": mode})

# Root redirect
@app.get("/", include_in_schema=False)
async def _root():
    return JSONResponse({"ok": True, "msg": "TargetVal Gateway — see /v1/openapi.json"})

# Entrypoint if run directly
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
