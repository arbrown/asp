import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from storybook.api.routes import router
from storybook.config import settings
from storybook.db import store
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


@app.on_event("startup")
async def startup() -> None:
    setup_logging(settings.gcp_project_id)
    try:
        await store.init_db()
        log.info("rqlite schema ready")
    except Exception:
        log.exception("Failed to initialize rqlite schema")


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}

