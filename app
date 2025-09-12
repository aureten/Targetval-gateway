import os, time, urllib.parse, asyncio
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import httpx

API_KEY = os.getenv("API_KEY")

app = FastAPI(title="TARGETVAL Gateway", version="0.1.0")

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: dict
    citations: list[str]
    fetched_at: float

def require_key(x_api_key: str | None):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server missing API key")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")

@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}

@app.get("/clinical/ctgov", response_model=Evidence)
async def ctgov(condition: str, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    base = "https://clinicaltrials.gov/api/v2/studies"
    q = f"{base}?query.cond={urllib.parse.quote(condition)}&pageSize=3"
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=3.0)) as client:
        for wait in (0.5, 1.0, 2.0):
            try:
                r = await client.get(q)
                r.raise_for_status()
                studies = r.json().get("studies", [])
                return Evidence(
                    status="OK",
                    source="ClinicalTrials.gov v2",
                    fetched_n=len(studies),
                    data={"studies": studies},
                    citations=[q],
                    fetched_at=time.time(),
                )
            except Exception:
                await asyncio.sleep(wait)
    raise HTTPException(status_code=500, detail="Failed to fetch studies")
