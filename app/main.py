from __future__ import annotations
import asyncio, inspect, logging, os, re, sys, time, traceback, uuid
from typing import Any, Dict, List, Optional, Tuple, Iterable
from fastapi import FastAPI, HTTPException, Query, Request, Path, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
try:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
except Exception:
    ProxyHeadersMiddleware = None
try:
    from starlette.middleware.trustedhost import TrustedHostMiddleware
except Exception:
    TrustedHostMiddleware = None
APP_NAME=os.getenv("APP_NAME","TargetVal Gateway"); APP_VERSION=os.getenv("APP_VERSION","2025.10-final")
DEBUG=os.getenv("DEBUG","false").lower() in {"1","true","yes"}
ROOT_PATH=os.getenv("ROOT_PATH",""); DOCS_URL=os.getenv("DOCS_URL","/docs"); REDOC_URL=os.getenv("REDOC_URL","/redoc"); OPENAPI_URL=os.getenv("OPENAPI_URL","/openapi.json")
ALLOW_ORIGINS=os.getenv("ALLOW_ORIGINS","*"); ALLOW_METHODS=os.getenv("ALLOW_METHODS","*"); ALLOW_HEADERS=os.getenv("ALLOW_HEADERS","*"); TRUSTED_HOSTS=os.getenv("TRUSTED_HOSTS","*")
REGISTRY_PATH=os.getenv("TARGETVAL_MODULE_CONFIG","app/routers/targetval_modules.yaml")
DEFAULT_ORDER=os.getenv("AGG_DEFAULT_ORDER","sequential").lower(); DEFAULT_CONCURRENCY=int(os.getenv("AGG_CONCURRENCY","6")); DEFAULT_CONTINUE=os.getenv("AGG_CONTINUE_ON_ERROR","true").lower() in {"1","true","yes"}
class AggregateRequest(BaseModel):
    gene: Optional[str]=None; symbol: Optional[str]=None; ensembl_id: Optional[str]=None; efo: Optional[str]=None; condition: Optional[str]=None; limit: Optional[int]=50
    modules: Optional[List[str]]=None; domain: Optional[str|int]=None; primary_only: bool=True
    order: str=Field(default=DEFAULT_ORDER, pattern="^(sequential|parallel)$"); continue_on_error: bool=DEFAULT_CONTINUE
    species: Optional[int]=None; cutoff: Optional[float]=None; extra: Optional[Dict[str,Any]]=None
class ModuleRunRequest(BaseModel):
    module_key: str; gene: Optional[str]=None; symbol: Optional[str]=None; ensembl: Optional[str]=None; efo: Optional[str]=None; condition: Optional[str]=None; limit: Optional[int]=50; extra: Optional[Dict[str,Any]]=None
def _import_router_module():
    attempts=[]; 
    def _try(mod:str):
        try:
            m=__import__(mod, fromlist=["router"]); 
            if getattr(m,"router",None) is None: raise ImportError(f"module {mod} has no attribute 'router'")
            return m,mod,None
        except Exception: return None,None,traceback.format_exc()
    for mod in ("app.routers.targetval_router","routers.targetval_router","targetval_router","router"):
        module,where,err=_try(mod)
        if module is not None: return module,where,None
        attempts.append(f"=== Attempt {mod} failed ===\n{err}")
    return None,None,"\n".join(attempts)
class RequestIDMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, header_name: str="X-Request-ID"): super().__init__(app); self.header_name=header_name
    async def dispatch(self, request: Request, call_next):
        rid=request.headers.get(self.header_name) or str(uuid.uuid4()); request.state.request_id=rid; start=time.perf_counter()
        response=await call_next(request); response.headers.setdefault(self.header_name, rid)
        try:
            logging.getLogger("uvicorn.access").info("%s %s -> %s (%.1f ms) rid=%s", request.method, request.url.path, response.status_code, (time.perf_counter()-start)*1000.0, rid)
        except Exception: pass
        return response
def _safe_load_yaml(path:str)->Dict[str,Any]:
    import yaml
    with open(path,"r",encoding="utf-8") as fh: return yaml.safe_load(fh) or {}
def _build_key_to_path_and_domains(reg:Dict[str,Any])->Tuple[Dict[str,str],Dict[str,List[int]],List[str]]:
    ktp:Dict[str,str]={}; ktd:Dict[str,List[int]]={}; keys:List[str]=[]
    for m in reg.get("modules",[]):
        k=m.get("key"); p=m.get("path"); 
        if not k or not p: continue
        keys.append(k); ktp[k]=p; doms=(m.get("domains") or {}).get("primary") or []; ktd[k]=[int(d) for d in doms if str(d).isdigit()]
    return ktp,ktd,keys
def create_app()->FastAPI:
    app=FastAPI(title=APP_NAME,version=APP_VERSION,debug=DEBUG,root_path=ROOT_PATH,docs_url=DOCS_URL,redoc_url=REDOC_URL,openapi_url=OPENAPI_URL)
    app.add_middleware(GZipMiddleware, minimum_size=1024); app.add_middleware(RequestIDMiddleware)
    if ProxyHeadersMiddleware: app.add_middleware(ProxyHeadersMiddleware)
    if TRUSTED_HOSTS.strip()!="*" and TrustedHostMiddleware:
        hosts=[h.strip() for h in TRUSTED_HOSTS.split(",") if h.strip()]; 
        if hosts: app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(CORSMiddleware, allow_origins=["*"] if ALLOW_ORIGINS=="*" else [o.strip() for o in ALLOW_ORIGINS.split(",") if o.strip()], allow_credentials=True, allow_methods=["*"] if ALLOW_METHODS=="*" else [m.strip() for m in ALLOW_METHODS.split(",") if m.strip()], allow_headers=["*"] if ALLOW_HEADERS=="*" else [h.strip() for h in ALLOW_HEADERS.split(",") if h.strip()])
    router_module,where,import_error=_import_router_module(); app.state.router_location=where; app.state.router_import_error=import_error
    if router_module: app.include_router(router_module.router, prefix="/v1")
    try: reg=_safe_load_yaml(REGISTRY_PATH)
    except Exception as e: logging.exception("Failed to load registry %s: %r", REGISTRY_PATH, e); reg={"modules":[]}
    key_to_path,key_to_domains,all_keys=_build_key_to_path_and_domains(reg); app.state.module_keys=all_keys; app.state.key_to_domains=key_to_domains
    path_to_endpoint:Dict[str,Any]={}
    if router_module and hasattr(router_module,"router"):
        for r in router_module.router.routes:
            try:
                p=getattr(r,"path","") or ""; ep=getattr(r,"endpoint",None)
                if p and ep: path_to_endpoint[p]=ep
            except Exception: pass
    def resolve_callable(module_key:str)->Optional[Any]:
        p=key_to_path.get(module_key)
        if not p: return None
        return path_to_endpoint.get(p) or path_to_endpoint.get("/v1"+p)
    app.state.resolve_callable=resolve_callable
    @app.get("/v1/healthz") 
    def healthz(): return {"ok":True,"modules":len(app.state.module_keys),"version":APP_VERSION}
    @app.get("/v1/modules") 
    def list_modules(): return sorted(app.state.module_keys)
    SYMBOLISH=re.compile(r"^[A-Za-z0-9-]+$")
    def looks_like_symbol(s:Optional[str])->bool:
        if not s: return False
        u=s.upper(); 
        return not (u.startswith("ENSG") or ":" in u or "_" in u) and bool(SYMBOLISH.match(u))
    def bind_kwargs(func:Any,prov:Dict[str,Any])->Dict[str,Any]:
        try:
            allowed=set(inspect.signature(func).parameters.keys()); 
            return {k:v for k,v in prov.items() if k in allowed and v is not None}
        except Exception:
            return {k:v for k,v in prov.items() if k in {"gene","symbol","ensembl","ensembl_id","efo","condition","limit"} and v is not None}
    async def run_callable(name:str, fn:Any, kw_all:Dict[str,Any])->Tuple[str,Dict[str,Any]]:
        kw=bind_kwargs(fn,kw_all)
        try:
            res=fn(**kw); 
            if asyncio.iscoroutine(res): res=await res
            if hasattr(res,"dict"): return name, res.dict()
            if hasattr(res,"model_dump"): return name, res.model_dump()
            if isinstance(res,dict): return name, res
            return name, {"status":"ERROR","source":"Unexpected return type","fetched_n":0,"data":{},"citations":[],"fetched_at":0.0}
        except HTTPException as he:
            return name, {"status":"ERROR","source":f"HTTP {he.status_code}: {he.detail}","fetched_n":0,"data":{},"citations":[],"fetched_at":0.0}
        except Exception as e:
            return name, {"status":"ERROR","source":str(e),"fetched_n":0,"data":{},"citations":[],"fetched_at":0.0}
    def shared_kwargs(body:Dict[str,Any])->Dict[str,Any]:
        gene=body.get("gene"); symbol=body.get("symbol"); ensg=body.get("ensembl_id") or body.get("ensembl"); efo=body.get("efo"); cond=body.get("condition"); limit=body.get("limit",50)
        sym=symbol if symbol else (gene if looks_like_symbol(gene) else None)
        out={"gene":gene,"symbol":sym,"ensembl":ensg,"ensembl_id":ensg,"efo":efo,"condition":cond,"disease":cond,"limit":limit}
        for k in ("species","cutoff"):
            if k in body and body.get(k) is not None: out[k]=body[k]
        extra=body.get("extra") or {}
        if isinstance(extra,dict): out.update(extra)
        return out
    @app.post("/v1/module")
    async def run_module_post(body: ModuleRunRequest):
        fn=app.state.resolve_callable(body.module_key)
        if not fn: raise HTTPException(status_code=404, detail=f"Unknown module: {body.module_key}")
        _,res=await run_callable(body.module_key, fn, shared_kwargs(body.model_dump()))
        return res
    @app.get("/v1/module/{name}")
    async def run_module_get(request:Request, name:str, gene:Optional[str]=Query(None), symbol:Optional[str]=Query(None), ensembl:Optional[str]=Query(None), efo:Optional[str]=Query(None), condition:Optional[str]=Query(None), limit:int=Query(50,ge=1,le=1000)):
        fn=app.state.resolve_callable(name)
        if not fn: raise HTTPException(status_code=404, detail=f"Unknown module: {name}")
        known={"gene","symbol","ensembl","efo","condition","limit"}; extras={k:v for k,v in request.query_params.items() if k not in known}
        _,res=await run_callable(name, fn, shared_kwargs({"gene":gene,"symbol":symbol,"ensembl":ensembl,"efo":efo,"condition":condition,"limit":limit,"extra":extras}))
        return res
    async def aggregate_impl(mod_keys:List[str], body:Dict[str,Any], order:str, cont:bool)->Dict[str,Any]:
        res:Dict[str,Any]={}; kw_all=shared_kwargs(body)
        if order=="parallel":
            sem=asyncio.Semaphore(DEFAULT_CONCURRENCY)
            async def _g(mk:str, fn:Any):
                async with sem: _,r=await run_callable(mk,fn,kw_all); return mk,r
            tasks=[_g(mk, app.state.resolve_callable(mk)) for mk in mod_keys if app.state.resolve_callable(mk)]
            if tasks:
                pairs=await asyncio.gather(*tasks); res={k:v for k,v in pairs}
            return res
        for mk in mod_keys:
            fn=app.state.resolve_callable(mk)
            if not fn:
                res[mk]={"status":"ERROR","source":"Unknown module","data":{}}
                if not cont: break
                continue
            _,r=await run_callable(mk,fn,kw_all); res[mk]=r
            if not cont and (not isinstance(r,dict) or r.get("status")=="ERROR"): break
        return res
    def filter_domain(keys:Iterable[str], k2d:Dict[str,List[int]], d:int, primary_only:bool=True)->List[str]:
        return [k for k in keys if d in k2d.get(k,[])]
    @app.post("/v1/aggregate")
    async def aggregate(body: AggregateRequest):
        keys=body.modules or list(app.state.module_keys)
        dom=None
        if body.domain is not None:
            ds=str(body.domain).strip().upper().lstrip("D")
            if ds.isdigit(): dom=int(ds)
            if dom not in {1,2,3,4,5,6}: raise HTTPException(status_code=400, detail="domain must be 1..6 or 'D1'..'D6'")
            keys=filter_domain(keys, app.state.key_to_domains, dom, primary_only=body.primary_only)
        order_map={k:i for i,k in enumerate(app.state.module_keys)}; keys=sorted(keys, key=lambda k: order_map.get(k,1_000_000))
        results=await aggregate_impl(keys, body.model_dump(), body.order, body.continue_on_error)
        sym=body.symbol if body.symbol else (body.gene if (body.gene and re.match(r"^[A-Za-z0-9-]+$",body.gene) and not body.gene.upper().startswith("ENSG") and ":" not in body.gene and "_" not in body.gene) else None)
        return {"query":{"gene":body.gene,"symbol":sym,"ensembl_id":body.ensembl_id,"efo":body.efo,"condition":body.condition,"limit":body.limit or 50,"modules":keys,"domain":dom,"order":body.order},"results":results}
    @app.post("/v1/domain/{domain_id}/run")
    async def run_domain(domain_id:int=Path(ge=1,le=6), body:Dict[str,Any]|None=Body(default=None)):
        payload=body or {}; keys=filter_domain(app.state.module_keys, app.state.key_to_domains, domain_id, primary_only=bool(payload.get("primary_only",True)))
        if not keys: raise HTTPException(status_code=404, detail=f"No modules registered for domain {domain_id}")
        payload.setdefault("order","sequential"); payload.setdefault("continue_on_error",True)
        results=await aggregate_impl(keys, payload, "sequential", True)
        return {"query":{"domain":domain_id,"modules":keys,"order":"sequential"},"results":results}
    # Minimal, validator-proof Actions spec (OpenAPI 3.0.0; no components)
    @app.get("/v1/actions-openapi.json", include_in_schema=False)
    def actions_openapi():
        return JSONResponse({"openapi":"3.0.0","info":{"title":"TargetVal Gateway Actions","version":"1.0.0","description":"Minimal schema for ChatGPT Actions. Dynamic 55-module access via /v1/module; aggregate or run domain (sequential); literature/synthesis via router."},"servers":[{"url":"https://targetval-gateway.onrender.com/v1"}],"paths":{"/healthz":{"get":{"summary":"Health","responses":{"200":{"description":"OK"}}}},"/modules":{"get":{"summary":"List 55 module keys","responses":{"200":{"description":"OK"}}}},"/module":{"post":{"summary":"Run one module by key","responses":{"200":{"description":"OK"}}}},"/module/{name}":{"get":{"summary":"Run one module by key (GET flavor)","responses":{"200":{"description":"OK"}}}},"/aggregate":{"post":{"summary":"Run many modules; sequential or parallel","responses":{"200":{"description":"OK"}}}},"/domain/{domain_id}/run":{"post":{"summary":"Run all modules of a domain sequentially","responses":{"200":{"description":"OK"}}}},"/lit/meta":{"get":{"summary":"Literature meta","responses":{"200":{"description":"OK"}}}},"/synth/integrate":{"post":{"summary":"Synthesis integrate","responses":{"200":{"description":"OK"}}}},"/synth/bucket":{"get":{"summary":"Synthesis bucket","responses":{"200":{"description":"OK"}}}}}})
    return app
app=create_app()
if __name__=="__main__":
    try:
        import uvicorn
    except Exception:
        print("uvicorn is not installed. For local dev: pip install uvicorn[standard]"); sys.exit(1)
    port=int(os.getenv("PORT","8000")); uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
