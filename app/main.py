
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Body, Query, Path as FPath
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# Imports (robust): support root-level router.py or packaged app/router.py
# -----------------------------------------------------------------------------
def _load_router_module():
    import os, sys, importlib.util
    # 1) Try package imports first
    try:
        import router as _router_mod  # type: ignore
        return _router_mod
    except Exception as e1:
        err1 = e1
    try:
        import app.router as _router_mod  # type: ignore
        return _router_mod
    except Exception as e2:
        err2 = e2
    # 2) Try file-based imports in common locations (Render uses /opt/render/project/src)
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
            assert spec.loader is not None
            spec.loader.exec_module(mod)  # type: ignore
            return mod
    raise RuntimeError(f"Failed to import router module from any known location. "
                       f"Errors: import router -> {err1!s}; import app.router -> {err2!s}")

_router_mod = _load_router_module()
tv_router = getattr(_router_mod, "router")
MODULES = getattr(_router_mod, "MODULES", None)
ROUTER_DOMAINS_META = getattr(_router_mod, "DOMAINS_META", None)
ROUTER_DOMAIN_MODULES = getattr(_router_mod, "DOMAIN_MODULES", None)

APP_TITLE = os.getenv("APP_TITLE", "TargetVal Gateway (Actions Surface) â Full")
APP_VERSION = os.getenv("APP_VERSION", "2025.10")
ROOT_PATH = os.getenv("ROOT_PATH", "")
DOCS_URL = os.getenv("DOCS_URL", "/docs")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/openapi.json")

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    docs_url=DOCS_URL,
    openapi_url=OPENAPI_URL,
    root_path=ROOT_PATH,
)

# CORS: permissive by default; tighten via env if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount router at /v1 (this carries all 64 module routes and /domains endpoints)
app.include_router(tv_router, prefix="/v1")


# ----------------------------- Schemas (public) -----------------------------

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


# ----------------------------- Domain mapping (from router) ------------------

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
        D = {str(k): v for k, v in ROUTER_DOMAIN_MODULES.items()}
        D.update({f"D{k}": v for k, v in ROUTER_DOMAIN_MODULES.items()})
        return D
    D1 = [
        "genetics-l2g","genetics-coloc","genetics-mr","genetics-chromatin-contacts","genetics-3d-maps",
        "genetics-regulatory","genetics-sqtl","genetics-pqtl","genetics-annotation","genetics-pathogenicity-priors",
        "genetics-intolerance","genetics-rare","genetics-mendelian","genetics-phewas-human-knockout",
        "genetics-functional","genetics-mavedb","genetics-consortia-summary"
    ]
    D2 = [
        "mech-pathways","biology-causal-pathways","mech-ppi","mech-ligrec",
        "assoc-proteomics","assoc-metabolomics","assoc-bulk-rna","assoc-perturb",
        "perturb-lincs-signatures","perturb-connectivity","perturb-signature-enrichment",
        "perturb-perturbseq-encode","perturb-crispr-screens","genetics-lncrna","genetics-mirna",
        "perturb-qc (internal)","perturb-scrna-summary (internal)"
    ]
    D3 = [
        "expr-baseline","expr-inducibility","assoc-sc","assoc-spatial","sc-hubmap",
        "expr-localization","assoc-hpa-pathology","tract-ligandability-ab","tract-surfaceome"
    ]
    D4 = [
        "mech-structure","tract-ligandability-sm","tract-ligandability-oligo","tract-modality",
        "tract-drugs","perturb-drug-response"
    ]
    D5 = [
        "function-dependency","immuno/hla-coverage","tract-immunogenicity","tract-mhc-binding",
        "tract-iedb-epitopes","clin-safety","clin-rwe","clin-on-target-ae-prior","perturb-depmap-dependency"
    ]
    D6 = [
        "clin-endpoints","clin-biomarker-fit","clin-pipeline","clin-feasibility","comp-intensity","comp-freedom"
    ]
    mapping = {"1": D1, "2": D2, "3": D3, "4": D4, "5": D5, "6": D6,
               "D1": D1, "D2": D2, "D3": D3, "D4": D4, "D5": D5, "D6": D6}
    return mapping


# ----------------------------- Health / readiness ----------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}

@app.get("/readyz")
async def readyz():
    return {"ok": True, "root_path": ROOT_PATH, "docs": DOCS_URL, "version": APP_VERSION}


# ----------------------------- Registry actions ------------------------------

def _module_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if MODULES:
        for m in MODULES:
            name = getattr(m, "name", None)
            route = getattr(m, "route", None)
            if name and route:
                mapping[str(name)] = str(route)
        return mapping
    for r in getattr(tv_router, "routes", []):
        p = getattr(r, "path", "")
        if not p or p.count("/") < 2:
            continue
        parts = [x for x in p.split("/") if x]
        if len(parts) >= 2:
            fam, mod = parts[0], parts[1]
            key = f"{fam}-{mod}"
            mapping[key] = p
    return mapping

def _normalize_symbol(symbol: Optional[str], gene: Optional[str]) -> Optional[str]:
    return (symbol or gene or None)

async def _invoke_module_via_asgi(route_path: str, params: Dict[str, Any]) -> Any:
    import httpx
    path = "/v1" + (route_path if route_path.startswith("/") else "/" + route_path)
    q = {k: v for k, v in params.items() if v is not None}
    async with httpx.AsyncClient(app=app, base_url="http://app.internal") as client:
        resp = await client.get(path, params=q, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:2000])
        return resp.json()

@app.get("/v1/modules")
async def list_modules() -> List[str]:
    if MODULES:
        return [getattr(m, "name") for m in MODULES]
    return sorted(_module_map().keys())

@app.post("/v1/module", response_model=Evidence)
async def run_module(req: ModuleRunRequest = Body(...)) -> Any:
    modmap = _module_map()
    if req.module_key not in modmap:
        raise HTTPException(status_code=404, detail=f"Unknown module_key: {req.module_key}")
    params = dict(
        symbol=_normalize_symbol(req.symbol, req.gene),
        ensembl_id=req.ensembl_id,
        uniprot_id=req.uniprot_id,
        efo=req.efo,
        condition=req.condition,
        tissue=req.tissue,
        cell_type=req.cell_type,
        species=req.species,
        limit=req.limit,
        offset=req.offset,
        strict=req.strict,
    )
    return await _invoke_module_via_asgi(modmap[req.module_key], params)

@app.get("/v1/module/{module_key}", response_model=Evidence)
async def run_module_by_name(
    module_key: str = FPath(...),
    gene: Optional[str] = Query(None, description="Alias of 'symbol'."),
    symbol: Optional[str] = None,
    ensembl_id: Optional[str] = None,
    uniprot_id: Optional[str] = None,
    efo: Optional[str] = None,
    condition: Optional[str] = None,
    tissue: Optional[str] = None,
    cell_type: Optional[str] = None,
    species: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    strict: bool = Query(False),
) -> Any:
    modmap = _module_map()
    if module_key not in modmap:
        raise HTTPException(status_code=404, detail=f"Unknown module_key: {module_key}")
    params = dict(
        symbol=_normalize_symbol(symbol, gene),
        ensembl_id=ensembl_id,
        uniprot_id=uniprot_id,
        efo=efo,
        condition=condition,
        tissue=tissue,
        cell_type=cell_type,
        species=species,
        limit=limit,
        offset=offset,
        strict=strict,
    )
    return await _invoke_module_via_asgi(modmap[module_key], params)

@app.post("/v1/aggregate")
async def aggregate_modules(req: AggregateRequest = Body(...)) -> Dict[str, Any]:
    modmap = _module_map()
    if req.modules:
        missing = [m for m in req.modules if m not in modmap]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown modules: {missing}")
        modules = req.modules
    elif req.domain:
        dmods = _domain_modules().get(str(req.domain).upper().lstrip("D")) or _domain_modules().get(str(req.domain).upper())
        if not dmods:
            raise HTTPException(status_code=400, detail=f"Unknown domain: {req.domain}")
        modules = [m for m in dmods if m in modmap]
    else:
        raise HTTPException(status_code=400, detail="Provide 'modules' or 'domain'.")

    base_params = dict(
        symbol=_normalize_symbol(req.symbol, req.gene),
        ensembl_id=req.ensembl_id,
        uniprot_id=req.uniprot_id,
        efo=req.efo,
        condition=req.condition,
        tissue=req.tissue,
        cell_type=req.cell_type,
        species=req.species,
        limit=req.limit,
        offset=req.offset,
    )

    results: Dict[str, Any] = {}
    t0 = time.time()

    async def run_one(key: str):
        try:
            route = modmap[key]
            res = await _invoke_module_via_asgi(route, base_params)
            results[key] = res
        except Exception as e:
            if req.continue_on_error:
                results[key] = {"status": "ERROR", "detail": str(e)}
            else:
                raise

    if req.order == "parallel":
        sem = asyncio.Semaphore(int(os.getenv("AGG_CONCURRENCY", "6")))
        async def run_one_bounded(k: str):
            async with sem:
                await run_one(k)
        await asyncio.gather(*[run_one_bounded(k) for k in modules])
    else:
        for k in modules:
            await run_one(k)

    # optional literature overlay for domains
    overlay = None
    try:
        if req.domain:
            dnum = int(str(req.domain).lstrip("D"))
            q = _build_domain_query(dnum, _normalize_symbol(req.symbol, req.gene), req.condition, req.efo)
            from httpx import AsyncClient
            ojs = await _epmc_search(q, size=min(100, req.limit or 100))
            overlay = {"query": q, "hits": _gate_items_epmc(ojs)}
    except Exception:
        overlay = None

    return {
        "ok": True,
        "domain": req.domain,
        "domain_name": DOMAINS_META.get(int(str(req.domain).lstrip("D")), {}).get("name") if req.domain else None,
        "modules": modules,
        "n_modules": len(modules),
        "elapsed_s": round(time.time() - t0, 3),
        "results": results,
        "literature_overlay": overlay,
    }

@app.post("/v1/domain/{domain_id}/run")
async def run_domain(
    domain_id: int = FPath(..., ge=1, le=6),
    req: DomainRunRequest = Body(None)
) -> Dict[str, Any]:
    dkey = f"{domain_id}"
    dmods = _domain_modules().get(dkey) or []
    if not dmods:
        raise HTTPException(status_code=400, detail=f"Unknown domain: {domain_id}")
    agg_req = AggregateRequest(
        modules=dmods,
        domain=dkey,
        primary_only=req.primary_only if req else True,
        order="sequential",
        continue_on_error=True,
        limit=req.limit if req else 100,
        offset=req.offset if req else 0,
        gene=req.gene if req else None,
        symbol=req.symbol if req else None,
        ensembl_id=req.ensembl_id if req else None,
        uniprot_id=req.uniprot_id if req else None,
        efo=req.efo if req else None,
        condition=req.condition if req else None,
        tissue=req.tissue if req else None,
        cell_type=req.cell_type if req else None,
        species=req.species if req else None,
        cutoff=req.cutoff if req else None,
        extra=req.extra if req else None,
    )
    return await aggregate_modules(agg_req)


# ----------------------------- Literature layer ------------------------------

import httpx

async def _epmc_search(query: str, size: int = 50) -> Dict[str, Any]:
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": query, "format": "json", "pageSize": str(size)}
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

async def _crossref(doi: str) -> Dict[str, Any]:
    if not doi:
        return {}
    url = f"https://api.crossref.org/works/{doi}"
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return {}
        return r.json()

def _build_domain_query(domain_id: int, symbol: Optional[str], condition: Optional[str], efo: Optional[str]) -> str:
    sym = (symbol or "").strip()
    cond = (condition or "").strip()
    trait = (efo or "").strip()
    tail = ""
    if cond: tail += f' AND ("{cond}")'
    if trait: tail += f' AND ({trait})'
    if domain_id == 1:
        core = f'{sym} AND (GWAS OR "Mendelian" OR colocalization OR "Mendelian randomization")'
    elif domain_id == 2:
        core = f'{sym} AND (pathway OR "protein interaction" OR causal OR perturbation OR "gene network")'
    elif domain_id == 3:
        core = f'{sym} AND (expression OR "single cell" OR "cell type" OR localization OR IHC OR pathology)'
    elif domain_id == 4:
        core = f'{sym} AND (drug OR antibody OR small molecule OR ligandability OR pocket OR modality)'
    elif domain_id == 5:
        core = f'{sym} AND (toxicity OR "adverse event" OR safety OR immunogenicity OR essentiality)'
    else:
        core = f'{sym} AND (clinical trial OR endpoint OR biomarker OR NCT)'
    return f"{core}{tail}"

def _gate_items_epmc(js: Dict[str, Any]) -> List[Dict[str, Any]]:
    recs = ((js or {}).get("resultList") or {}).get("result") or []
    out = []
    for it in recs:
        src = (it.get("source") or "").upper()
        pubtypes = " ".join([p.get("name", "") for p in (it.get("pubTypeList") or {}).get("pubType", [])])
        is_preprint = (src == "PPR")
        is_retracted = ("Retracted" in pubtypes) or ("retraction" in (it.get("title", "") or "").lower())
        out.append({
            "id": it.get("id"),
            "title": it.get("title"),
            "authorString": it.get("authorString"),
            "journalTitle": it.get("journalTitle"),
            "pubYear": it.get("pubYear"),
            "doi": it.get("doi"),
            "source": it.get("source"),
            "is_preprint": is_preprint,
            "is_retracted": is_retracted,
        })
    return out

@app.get("/v1/lit/meta")
async def lit_meta(
    query: Optional[str] = Query(None),
    gene: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    efo: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    q = query or _build_domain_query(2, symbol or gene, condition, efo)
    js = await _epmc_search(q, size=limit)
    items = _gate_items_epmc(js)
    return {"ok": True, "query": q, "hits": items[:limit]}

@app.get("/v1/lit/search")
async def lit_search(
    symbol: str = Query(...),
    condition: Optional[str] = Query(None),
    window_days: int = Query(1825),
    page_size: int = Query(50),
) -> Dict[str, Any]:
    q = f'{symbol} AND ("{condition}")' if condition else symbol
    js = await _epmc_search(q, size=page_size)
    items = _gate_items_epmc(js)
    return {"ok": True, "query": q, "hits": items[:page_size]}

@app.get("/v1/lit/claims")
async def lit_claims(
    symbol: str = Query(...),
    condition: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    base_q = f'{symbol} AND (causes OR regulates OR "adverse event" OR toxicity OR improves OR inhibits)'
    q = f'{base_q} AND ("{condition}")' if condition else base_q
    js = await _epmc_search(q, size=limit)
    items = _gate_items_epmc(js)
    return {"ok": True, "query": q, "claims": items[:limit]}

@app.post("/v1/lit/score")
async def lit_score(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    items = payload.get("items") or []
    scored = []
    for it in items:
        src = (it.get("source") or "").upper()
        is_preprint = (src == "PPR") or ("bioRxiv" in (it.get("journalTitle") or ""))
        title = (it.get("title") or "").lower()
        flags = {
            "preprint": is_preprint,
            "human_hint": any(k in title for k in ["human", "patient", "clinical"]),
            "trial_hint": "nct" in title or "randomized" in title,
            "retraction_hint": "retract" in title,
        }
        scored.append({"id": it.get("id") or it.get("doi"), "flags": flags})
    return {"ok": True, "items": scored}

# Domain 5 convenience bundle with literature overlay
@app.get("/v1/synth/therapeutic-index")
async def synth_therapeutic_index(
    gene: Optional[str] = Query(None, description="Alias of 'symbol'."),
    symbol: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    d5 = _domain_modules()["5"]
    base_params = dict(symbol=_normalize_symbol(symbol, gene), condition=condition, limit=limit, offset=0)
    results: Dict[str, Any] = {}
    async def call_one(key: str):
        route = _module_map()[key]
        res = await _invoke_module_via_asgi(route, base_params)
        results[key] = res
    for key in d5:
        try:
            await call_one(key)
        except Exception as e:
            results[key] = {"status": "ERROR", "detail": str(e)}
    q = _build_domain_query(5, _normalize_symbol(symbol, gene), condition, None)
    overlay = await _epmc_search(q, size=min(100, limit))
    hits = _gate_items_epmc(overlay)
    return {
        "ok": True,
        "domain": 5,
        "domain_name": DOMAINS_META[5]["name"],
        "modules": d5,
        "results": results,
        "literature": {"query": q, "hits": hits},
    }


# Entrypoint for local runs ---------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
