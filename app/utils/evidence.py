# app/utils/evidence.py
from __future__ import annotations
from typing import Any, Dict, List
from pydantic import BaseModel
import time

class Evidence(BaseModel):
    status: str                 # "OK" | "NO_DATA" | "ERROR"
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float

def now_ts() -> float:
    return time.time()
