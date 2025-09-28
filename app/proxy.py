# app/proxy.py
import os
from fastapi import FastAPI, Request, Response
import httpx

# Upstream gateway (env overrides allowed)
TARGET = os.getenv("TARGETVAL_GATEWAY", "https://targetval-gateway.onrender.com")

app = FastAPI(title="TargetVal MCP Proxy", version="1.0.0")

@app.get("/healthz")
def healthz():
    return {"ok": True, "proxying_to": TARGET}

@app.api_route("/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
async def proxy(path: str, request: Request):
    # Rebuild URL to upstream
    url = f"{TARGET}/{path}"
    # Copy incoming query string and body
    body = await request.body()
    params = dict(request.query_params)
    # Strip hop-by-hop headers and Host to avoid upstream confusion
    hop = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in hop}

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(request.method, url, params=params, content=body, headers=headers)

    # Pass through status and headers (minus hop-by-hop)
    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in hop}
    return Response(content=r.content, status_code=r.status_code, headers=resp_headers)
