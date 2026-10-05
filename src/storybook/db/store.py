from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException

from storybook.config import settings
from storybook.db.client import RqliteClient
from storybook.models import PipelineState

log = logging.getLogger(__name__)

LEGACY_USER_EMAIL = "legacy@storybook.local"
LEGACY_OWNER_EMAIL = settings.legacy_owner_email

_client: RqliteClient | None = None


class SessionAccessDeniedError(HTTPException):
    """Raised when a user attempts to access a session owned by another user."""

    def __init__(self, detail: str = "Forbidden: session belongs to another user") -> None:
        super().__init__(status_code=403, detail=detail)


def _get() -> RqliteClient:
    global _client
    if _client is None:
        _client = RqliteClient(settings.rqlite_url)
    return _client


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def init_db() -> None:
    c = _get()
    await c.execute_batch([
        [
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL DEFAULT 'initializing', "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, "
            "data TEXT NOT NULL, "
            "user_email TEXT NOT NULL DEFAULT 'legacy@storybook.local'"
            ")"
        ],
        ["CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)"],
        ["CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at)"],
        [
            "CREATE TABLE IF NOT EXISTS session_tokens ("
            "session_id TEXT PRIMARY KEY, "
            "user_email TEXT NOT NULL, "
            "downscoped_token TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        ],
    ])
    # Auto-migration for pre-existing tables without user_email
    try:
        await c.execute(
            "ALTER TABLE sessions "
            "ADD COLUMN user_email TEXT NOT NULL DEFAULT 'legacy@storybook.local'"
        )
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            log.debug("ALTER TABLE sessions ADD COLUMN user_email note: %s", exc)

    await c.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_email ON sessions(user_email)"
    )
    # Assign legacy unscoped sessions to the configured legacy owner
    try:
        await c.execute(
            "UPDATE sessions SET user_email = ? WHERE user_email = ?",
            LEGACY_OWNER_EMAIL,
            LEGACY_USER_EMAIL,
        )
    except Exception as exc:
        log.debug("Legacy user_email migration note: %s", exc)


async def save_actor_credentials(
    session_id: str,
    user_email: str,
    downscoped_token: str | None,
) -> None:
    """Persist ephemeral STS downscoped token for a Substrate Actor session."""
    await _get().execute(
        "INSERT OR REPLACE INTO session_tokens "
        "(session_id, user_email, downscoped_token, updated_at) "
        "VALUES (?, ?, ?, ?)",
        session_id,
        (user_email or "").strip().lower(),
        downscoped_token or "",
        _now(),
    )


async def get_actor_credentials(session_id: str) -> tuple[str, str | None]:
    """Retrieve (user_email, downscoped_token) for a Substrate Actor session."""
    try:
        rows = await _get().query(
            "SELECT user_email, downscoped_token FROM session_tokens WHERE session_id = ?",
            session_id,
        )
        if rows:
            email = str(rows[0].get("user_email") or "").strip().lower()
            token = str(rows[0].get("downscoped_token") or "").strip() or None
            return email, token
    except Exception as exc:
        log.debug("Could not fetch actor credentials for %s: %s", session_id, exc)
    return "", None


async def upsert_session(
    state: PipelineState,
    user_email: str | None = None,
) -> None:
    created_at = state.started_at or _now()
    owner = (
        user_email
        or getattr(state, "user_email", None)
        or LEGACY_OWNER_EMAIL
    ).strip().lower()
    state.user_email = owner
    await _get().execute(
        "INSERT OR REPLACE INTO sessions "
        "(session_id, user_email, status, created_at, updated_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        state.session_id,
        owner,
        state.current_stage,
        created_at,
        _now(),
        state.model_dump_json(),
    )


async def get_session(
    session_id: str,
    user_email: str | None = None,
) -> PipelineState | None:
    rows = await _get().query(
        "SELECT user_email, data FROM sessions WHERE session_id = ?",
        session_id,
    )
    if not rows:
        return None
    state = PipelineState.model_validate_json(rows[0]["data"])
    row_owner = (
        rows[0].get("user_email") or getattr(state, "user_email", None) or LEGACY_USER_EMAIL
    ).strip().lower()
    state.user_email = row_owner

    if user_email is not None:
        req_user = user_email.strip().lower()
        allowed_owners = {req_user}
        if req_user == LEGACY_OWNER_EMAIL:
            allowed_owners.add(LEGACY_USER_EMAIL)
        if row_owner not in allowed_owners:
            raise SessionAccessDeniedError()

    return state


async def list_sessions(
    limit: int | None = None,
    offset: int = 0,
    user_email: str | None = None,
    *,
    status: str | None = None,
    sort: str = "created_at_desc",
) -> list[PipelineState]:
    parts = ["SELECT user_email, data FROM sessions"]
    where_clauses: list[str] = []
    args: list[object] = []

    if user_email is not None:
        req_user = user_email.strip().lower()
        if req_user == LEGACY_OWNER_EMAIL:
            where_clauses.append("user_email IN (?, ?)")
            args.extend([req_user, LEGACY_USER_EMAIL])
        else:
            where_clauses.append("user_email = ?")
            args.append(req_user)

    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            where_clauses.append(f"status IN ({placeholders})")
            args.extend(statuses)

    if where_clauses:
        parts.append("WHERE " + " AND ".join(where_clauses))

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
            st = PipelineState.model_validate_json(r["data"])
            if r.get("user_email"):
                st.user_email = str(r["user_email"]).strip().lower()
            states.append(st)
        except Exception:
            log.warning("Skipping corrupt session row: %s", r.get("data", "")[:80])
    return states
