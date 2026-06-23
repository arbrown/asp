from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from storybook.db.client import RqliteClient
from storybook.models import PipelineState

log = logging.getLogger(__name__)

_client: RqliteClient | None = None


def _get() -> RqliteClient:
    global _client
    if _client is None:
        from storybook.config import settings
        _client = RqliteClient(settings.rqlite_url)
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    c = _get()
    await c.execute_batch([
        [
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL DEFAULT 'initializing', "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, "
            "data TEXT NOT NULL"
            ")"
        ],
        ["CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)"],
        ["CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at)"],
    ])


async def upsert_session(state: PipelineState) -> None:
    created_at = state.started_at or _now()
    await _get().execute(
        "INSERT OR REPLACE INTO sessions "
        "(session_id, status, created_at, updated_at, data) "
        "VALUES (?, ?, ?, ?, ?)",
        state.session_id,
        state.current_stage,
        created_at,
        _now(),
        state.model_dump_json(),
    )


async def get_session(session_id: str) -> Optional[PipelineState]:
    rows = await _get().query(
        "SELECT data FROM sessions WHERE session_id = ?",
        session_id,
    )
    if not rows:
        return None
    return PipelineState.model_validate_json(rows[0]["data"])


async def list_sessions(
    *,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    sort: str = "created_at_desc",
) -> list[PipelineState]:
    parts = ["SELECT data FROM sessions"]
    args: list[object] = []

    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            parts.append(f"WHERE status IN ({placeholders})")
            args.extend(statuses)

    order = "ASC" if sort == "created_at_asc" else "DESC"
    parts.append(f"ORDER BY created_at {order}")

    if limit is not None:
        parts.append("LIMIT ?")
        args.append(limit)
        if offset:
            parts.append("OFFSET ?")
            args.append(offset)

    rows = await _get().query(" ".join(parts), *args)
    states = []
    for r in rows:
        try:
            states.append(PipelineState.model_validate_json(r["data"]))
        except Exception:
            log.warning("Skipping corrupt session row: %s", r.get("data", "")[:80])
    return states
