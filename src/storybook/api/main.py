import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from storybook.api.routes import _sessions, router
from storybook.models import PipelineState, SessionConfig
from storybook.tools import gcs

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

app = FastAPI(title="Storybook Agent API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.on_event("startup")
async def reload_sessions() -> None:
    """Restore session history from GCS so deploys don't wipe the session list."""
    import asyncio
    try:
        metas = await asyncio.to_thread(gcs.load_all_session_meta)
        for m in metas:
            sid = m["session_id"]
            if sid not in _sessions:
                state = PipelineState(
                    session_id=sid,
                    config=SessionConfig(**m["config"]),
                    current_stage=m.get("current_stage", "unknown"),
                    progress_pct=m.get("progress_pct", 0),
                    pdf_gcs_uri=m.get("pdf_gcs_uri", ""),
                    errors=m.get("errors", []),
                )
                _sessions[sid] = state
        log.info("Reloaded %d session(s) from GCS", len(metas))
    except Exception:
        log.exception("Failed to reload sessions from GCS — starting with empty session list")
