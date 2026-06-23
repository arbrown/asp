import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from storybook.api.routes import _sessions, router
from storybook.config import settings
from storybook.db import store
from storybook.models import PipelineState, SessionConfig
from storybook.tools import gcs
from storybook.tracing import init_tracing

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

app = FastAPI(title="Storybook Agent API", version="0.1.0")

init_tracing(settings.gcp_project_id)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


async def _load_from_gcs_and_backfill() -> int:
    """Load session metadata from GCS into memory and write to DB. Returns count loaded."""
    metas = await asyncio.to_thread(gcs.load_all_session_meta)
    loaded = 0
    for m in metas:
        sid = m.get("session_id")
        if not sid or sid in _sessions:
            continue
        try:
            state = PipelineState(
                session_id=sid,
                config=SessionConfig(**m["config"]),
                current_stage=m.get("current_stage", "unknown"),
                progress_pct=m.get("progress_pct", 0),
                pdf_gcs_uri=m.get("pdf_gcs_uri", ""),
                wide_pdf_gcs_uri=m.get("wide_pdf_gcs_uri", ""),
                trace_url=m.get("trace_url", ""),
                errors=m.get("errors", []),
                started_at=m.get("started_at"),
                finished_at=m.get("finished_at"),
            )
            _sessions[sid] = state
            loaded += 1
            try:
                await store.upsert_session(state)
            except Exception:
                log.warning("DB backfill failed for session %s — in-memory only", sid)
        except Exception:
            log.warning("Skipping malformed GCS session meta for %s", sid)
    return loaded


@app.on_event("startup")
async def startup() -> None:
    # Initialize rqlite schema
    db_ready = False
    try:
        await store.init_db()
        db_ready = True
        log.info("rqlite schema ready")
    except Exception:
        log.exception("Failed to initialize rqlite schema — will fall back to GCS")

    # Load from DB if it has sessions, otherwise seed from GCS
    if db_ready:
        try:
            states = await store.list_sessions()
            if states:
                for state in states:
                    if state.session_id not in _sessions:
                        _sessions[state.session_id] = state
                log.info("Reloaded %d session(s) from DB", len(states))
                return
            # DB is healthy but empty — this is the first boot after migration.
            # Seed from GCS and backfill into DB so future restarts use the DB.
            log.info("DB is empty — seeding from GCS")
        except Exception:
            log.exception("DB load failed — falling back to GCS")

    # GCS path: covers both DB-empty first boot and DB-unavailable fallback
    try:
        loaded = await _load_from_gcs_and_backfill()
        log.info("Loaded %d session(s) from GCS%s", loaded,
                 " (backfilled to DB)" if db_ready else " (DB unavailable, in-memory only)")
    except Exception:
        log.exception("GCS load also failed — starting with empty session list")


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}
