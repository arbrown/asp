import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from storybook.api.routes import router
from storybook.config import settings
from storybook.db import store
from storybook.models import PipelineState, SessionConfig
from storybook.tools import gcs
from storybook.tracing import init_tracing, setup_logging

setup_logging(settings.gcp_project_id)

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
    """Backfill session metadata from GCS into rqlite on first boot."""
    metas = await asyncio.to_thread(gcs.load_all_session_meta)
    loaded = 0
    for m in metas:
        sid = m.get("session_id")
        if not sid:
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
                adapted_from_source=m.get("adapted_from_source", True),
            )
            await store.upsert_session(state)
            loaded += 1
        except Exception:
            log.warning("Skipping malformed or failed GCS session backfill for %s", sid)
    return loaded


@app.on_event("startup")
async def startup() -> None:
    setup_logging(settings.gcp_project_id)
    db_ready = False
    try:
        await store.init_db()
        db_ready = True
        log.info("rqlite schema ready")
    except Exception:
        log.exception("Failed to initialize rqlite schema — will fall back to GCS")

    if db_ready:
        try:
            states = await store.list_sessions(limit=1)
            if states:
                log.info("rqlite already populated with session history")
                return
            log.info("rqlite is empty — seeding from GCS")
            loaded = await _load_from_gcs_and_backfill()
            log.info("Backfilled %d session(s) from GCS into rqlite", loaded)
        except Exception:
            log.exception("DB startup check/backfill failed")


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}

