# app/utils/http.py
from __future__ import annotations
import asyncio
import httpx

DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=4.0)

async def get_json(url: str, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT, tries: int = 3) -> dict | list:
    err = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for _ in range(tries):
            try:
                r = await client.get(url)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                err = e
                await asyncio.sleep(0.8)
    raise RuntimeError(f"GET failed for {url}: {err}")

async def post_json(url: str, payload: dict, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT, tries: int = 3) -> dict | list:
    err = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for _ in range(tries):
            try:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                err = e
                await asyncio.sleep(0.8)
    raise RuntimeError(f"POST failed for {url}: {err}")
