from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class RqliteClient:
    def __init__(self, url: str) -> None:
        self._http = httpx.AsyncClient(base_url=url, timeout=10.0)

    async def execute(self, sql: str, *args: object) -> None:
        resp = await self._http.post("/db/execute", json=[[sql, *args]])
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            if "error" in r:
                raise RuntimeError(f"rqlite: {r['error']}")

    async def execute_batch(self, statements: list[list]) -> None:
        """Execute multiple SQL statements in one round trip."""
        resp = await self._http.post("/db/execute", json=statements)
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            if "error" in r:
                raise RuntimeError(f"rqlite: {r['error']}")

    async def query(self, sql: str, *args: object) -> list[dict]:
        resp = await self._http.post("/db/query", json=[[sql, *args]])
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return []
        first = results[0]
        if "error" in first:
            raise RuntimeError(f"rqlite: {first['error']}")
        cols = first.get("columns", [])
        rows = first.get("values", [])
        return [dict(zip(cols, row)) for row in rows]

    async def close(self) -> None:
        await self._http.aclose()
