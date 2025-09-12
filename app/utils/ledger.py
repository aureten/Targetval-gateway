# app/utils/ledger.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import List, Dict, Any
import asyncio, time

@dataclass
class Row:
    ts: float
    module: str
    status: str     # "OK" | "NO_DATA" | "ERROR"
    fetched_n: int
    source: str
    note: str = ""

class Ledger:
    def __init__(self):
        self._rows: List[Row] = []
        self._lock = asyncio.Lock()

    async def add(self, module: str, status: str, fetched_n: int, source: str, note: str = ""):
        async with self._lock:
            self._rows.append(Row(time.time(), module, status, fetched_n, source, note))

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            rows = [asdict(r) for r in self._rows[-1000:]]  # last N entries
        # simple pass/fail rollup
        rollup: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            m = r["module"]
            roll = rollup.setdefault(m, {"runs": 0, "ok": 0, "last": None})
            roll["runs"] += 1
            if r["status"] == "OK":
                roll["ok"] += 1
            roll["last"] = r
        return {"rows": rows, "rollup": rollup}

    async def reset(self):
        async with self._lock:
            self._rows.clear()

ledger = Ledger()
