
from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Body, Query
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

    # Include the dynamic router at /v1/*
    app.include_router(tv_router, prefix="/v1")

    # Health probes ---------------------------------------------------------
    @app.get("/livez")
    async def livez():
        # If router imported, our inline registry is available
        return {
            "ok": True,
            "router": "targetval_router",
            "import_ok": True,
            "synthesis_v2": False,
        }

    @app.get("/readyz")
    async def readyz():
        return {"ok": True, "root_path": ROOT_PATH, "docs": DOCS_URL, "synthesis_v2": False}

    # Registry views (YAML-free; use inline MODCFG) ------------------------
    @app.get("/v1/modules")
    async def list_modules() -> List[str]:
        return [m["key"] for m in MODCFG.get("modules", [])]

    # Single module runner (aggregator facade) -----------------------------
    @app.get("/v1/module/{key}", response_model=Evidence)
    async def run_single_module(
        key: str,
        symbol: Optional[str] = Query(None, description="HGNC symbol"),
        ensembl_id: Optional[str] = Query(None, description="ENSG"),
        uniprot_id: Optional[str] = Query(None, description="UniProt accession"),
        condition: Optional[str] = Query(None, description="Condition/trait label"),
        efo: Optional[str] = Query(None, description="EFO/MONDO id or label"),
        tissue: Optional[str] = Query(None),
        cell_type: Optional[str] = Query(None),
        species: Optional[str] = Query("human"),
        limit: Optional[int] = Query(100, ge=1, le=500),
        offset: Optional[int] = Query(0, ge=0),
        strict: Optional[bool] = Query(False),
    ) -> Evidence:
        keys = {m["key"] for m in MODCFG.get("modules", [])}
        if key not in keys:
            raise HTTPException(status_code=404, detail=f"Unknown module: {key}")
        params = ModuleParams(
            symbol=symbol, ensembl_id=ensembl_id, uniprot_id=uniprot_id,
            condition=condition, efo=efo, tissue=tissue, cell_type=cell_type,
            species=species, limit=limit, offset=offset, strict=strict
        )
        return await dispatch_module(key, params)

    # Batch aggregator (sequential) ----------------------------------------
    from pydantic import BaseModel, Field

    class AggregateRequest(BaseModel):
        # Query-like fields
        symbol: Optional[str] = None
        ensembl_id: Optional[str] = None
        uniprot_id: Optional[str] = None
        condition: Optional[str] = None
        efo: Optional[str] = None
        tissue: Optional[str] = None
        cell_type: Optional[str] = None
        species: Optional[str] = "human"
        limit: Optional[int] = 100
        offset: Optional[int] = 0
        strict: Optional[bool] = False

        # Which modules to run
        modules: Optional[List[str]] = None

        # Execution behaviour
        order: str = Field(default="sequential", pattern="^(sequential|parallel)$")
        continue_on_error: bool = True

    @app.post("/v1/aggregate")
    async def aggregate(req: AggregateRequest):
        all_keys = [m["key"] for m in MODCFG.get("modules", [])]
        wanted = req.modules or all_keys

        # Validate keys early
        invalid = [k for k in wanted if k not in all_keys]
        if invalid:
            raise HTTPException(status_code=404, detail=f"Unknown module(s): {', '.join(invalid)}")

        # Build shared params
        base_params = dict(
            symbol=req.symbol, ensembl_id=req.ensembl_id, uniprot_id=req.uniprot_id,
            condition=req.condition, efo=req.efo, tissue=req.tissue,
            cell_type=req.cell_type, species=req.species,
            limit=req.limit, offset=req.offset, strict=req.strict
        )

        results: Dict[str, Evidence] = {}

        async def run_one(k: str):
            try:
                ev = await dispatch_module(k, ModuleParams(**base_params))
                results[k] = ev
            except Exception as e:
                if req.continue_on_error:
                    results[k] = Evidence(
                        status="ERROR", source="Targetval Gateway",
                        fetched_n=0, data={"error": str(e)},
                        citations=[], fetched_at=int(time.time())
                    )
                else:
                    raise

        if req.order == "parallel":
            # bounded parallelism (cap at 8)
            sem = asyncio.Semaphore(int(os.getenv("AGG_CONCURRENCY", "8")))
            async def guarded(k):
                async with sem:
                    await run_one(k)
            await asyncio.gather(*(guarded(k) for k in wanted))
        else:
            # sequential
            for k in wanted:
                await run_one(k)

        return {
            "query": {k: getattr(req, k) for k in [
                "symbol","ensembl_id","uniprot_id","condition","efo",
                "tissue","cell_type","species","limit","offset","strict"
            ]},
            "order": req.order,
            "results": results,
        }

    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
