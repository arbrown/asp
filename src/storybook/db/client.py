from __future__ import annotations

import logging
import sqlite3

import httpx

log = logging.getLogger(__name__)


class RqliteClient:
    def __init__(self, url: str) -> None:
        self._url = url
        self._sqlite_conn: sqlite3.Connection | None = None
        self._http: httpx.AsyncClient | None = None
        if url.startswith("sqlite://"):
            db_path = url.removeprefix("sqlite:///") or ":memory:"
            self._sqlite_conn = sqlite3.connect(db_path, check_same_thread=False)
        else:
            self._http = httpx.AsyncClient(base_url=url, timeout=10.0)

    async def execute(self, sql: str, *args: object) -> None:
        if self._sqlite_conn is not None:
            try:
                self._sqlite_conn.execute(sql, args)
                self._sqlite_conn.commit()
            except sqlite3.Error as exc:
                raise RuntimeError(f"rqlite: {exc}") from exc
            return

        assert self._http is not None
        resp = await self._http.post("/db/execute", json=[[sql, *args]])
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            if "error" in r:
                raise RuntimeError(f"rqlite: {r['error']}")

    async def execute_batch(self, statements: list[list]) -> None:
        """Execute multiple SQL statements in one round trip."""
        if self._sqlite_conn is not None:
            try:
                for stmt in statements:
                    self._sqlite_conn.execute(stmt[0], stmt[1:])
                self._sqlite_conn.commit()
            except sqlite3.Error as exc:
                raise RuntimeError(f"rqlite: {exc}") from exc
            return

        assert self._http is not None
        resp = await self._http.post("/db/execute", json=statements)
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            if "error" in r:
                raise RuntimeError(f"rqlite: {r['error']}")

    async def query(self, sql: str, *args: object) -> list[dict]:
        if self._sqlite_conn is not None:
            try:
                cur = self._sqlite_conn.execute(sql, args)
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall()
                return [dict(zip(cols, row)) for row in rows]
            except sqlite3.Error as exc:
                raise RuntimeError(f"rqlite: {exc}") from exc

        assert self._http is not None
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
        if self._sqlite_conn is not None:
            self._sqlite_conn.close()
        if self._http is not None:
            await self._http.aclose()
