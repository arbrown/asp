"""Standalone Storybook ADK pipeline runner entrypoint.

Supports two execution modes:
1. CLI mode:
   python -m storybook.runner --session-id <id> [--resume]
2. Substrate Actor mode (default when --session-id is omitted):
   Pre-imports heavy dependencies for the golden snapshot, serves /readyz on port
   8080, waits until restored into a non-golden atespace via the Substrate
   systemInfo metadata volume (/var/run/secrets/ate.dev/metadata), executes the
   pipeline for actor-name (<session_id>), checkpoints state to GCS and rqlite,
   and terminates upon completion.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Pre-import heavy modules before /readyz responds so the Substrate golden
# snapshot captures a warm Python interpreter with ADK, GenAI, GCS, and WeasyPrint loaded.
import weasyprint  # noqa: F401
from google import genai  # noqa: F401
from google.cloud import storage  # noqa: F401

from storybook.agents.pipeline import run_pipeline
from storybook.config import settings
from storybook.db import store
from storybook.events import GCSProgressSink
from storybook.tools import gcs
from storybook.tracing import init_tracing, setup_logging

log = logging.getLogger(__name__)

METADATA_DIRS = [
    Path("/run/ate/metadata"),
    Path("/var/run/secrets/ate.dev/metadata"),
]
GOLDEN_ATESPACE = "ate-golden"


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for Substrate golden snapshot readiness probe."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/readyz", "/healthz", "/"):
            body = b'{"status":"ready"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _start_readyz_server(port: int = 8080) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Runner readiness server listening on 0.0.0.0:%d", port)
    return server


def _read_metadata_file(name: str) -> str:
    for d in METADATA_DIRS:
        path = d / name
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


async def _wait_for_actor_assignment() -> str:
    """Wait until the actor is restored out of ate-golden and assigned a session ID."""
    log.info("Waiting for Substrate actor assignment in %s...", METADATA_DIRS[0])
    while True:
        atespace = _read_metadata_file("actor-atespace")
        actor_name = _read_metadata_file("actor-name")
        if atespace and atespace != GOLDEN_ATESPACE and actor_name:
            log.info(
                "Actor restored into atespace=%r actor_name=%r",
                atespace,
                actor_name,
            )
            return actor_name
        await asyncio.sleep(0.05)


async def execute_session(session_id: str, force_resume: bool = False) -> int:
    """Load PipelineState from GCS, run the 11-stage ADK pipeline, and checkpoint."""
    # Re-seed PRNG after potential snapshot restore
    random.seed(os.urandom(16))

    init_tracing(settings.gcp_project_id)
    await store.init_db()

    log.info("Loading pipeline state from GCS for session %s...", session_id)
    state = None
    for attempt in range(1, 11):
        state = await asyncio.to_thread(gcs.load_pipeline_state, session_id)
        if state is not None:
            break
        state = await store.get_session(session_id)
        if state is not None:
            break
        log.warning(
            "PipelineState not found yet for session %s (attempt %d/10); retrying...",
            session_id,
            attempt,
        )
        await asyncio.sleep(0.5)

    if state is None:
        log.error("Could not load PipelineState for session %s from GCS or rqlite", session_id)
        return 1

    resume = (
        force_resume
        or state.current_stage == "resuming"
        or os.environ.get("STORYBOOK_RESUME") == "1"
    )
    log.info(
        "Starting runner for session %s (resume=%s, current_stage=%s, title=%r)",
        session_id,
        resume,
        state.current_stage,
        state.config.source.title,
    )

    existing_events: list[dict] = []
    if resume:
        existing_events, _ = await asyncio.to_thread(gcs.load_progress_events, session_id)

    sink = GCSProgressSink(session_id, state, existing_events=existing_events)
    if resume:
        await sink.put({"stage": "resuming", "pct": state.progress_pct or 5})

    try:
        await run_pipeline(state, sink, resume=resume)
        state.finished_at = datetime.now(timezone.utc).isoformat()
        await sink.put(None)
        log.info("Runner completed session %s successfully", session_id)
        return 0
    except Exception as exc:
        log.exception("Pipeline failed for session %s", session_id)
        state.current_stage = "error"
        state.finished_at = datetime.now(timezone.utc).isoformat()
        state.errors.append(str(exc))
        await sink.put({"stage": "error", "message": str(exc)})
        await sink.put(None)
        return 1


async def _async_main(args: argparse.Namespace) -> int:
    session_id = args.session_id
    if not session_id:
        port = int(os.environ.get("PORT", str(args.port)))
        _start_readyz_server(port=port)
        session_id = await _wait_for_actor_assignment()

    return await execute_session(session_id=session_id, force_resume=args.resume)


def main() -> None:
    setup_logging(settings.gcp_project_id)
    parser = argparse.ArgumentParser(description="Storybook standalone ADK pipeline runner")
    parser.add_argument(
        "--session-id",
        default=os.environ.get("STORYBOOK_SESSION_ID", ""),
        help="Session ID to execute. If omitted, runs in Substrate Actor mode and reads actor-name from metadata.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume pipeline execution from existing GCS checkpoints.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for the /readyz HTTP server in Substrate Actor mode.",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(_async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
