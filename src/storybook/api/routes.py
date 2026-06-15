from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from storybook.agents.pipeline import run_pipeline
from storybook.api.models import CreateSessionRequest, SessionResponse
from storybook.models import PipelineState

log = logging.getLogger(__name__)
router = APIRouter()

# In-memory session store — single user, single pod, no persistence needed
_sessions: dict[str, PipelineState] = {}
_queues: dict[str, asyncio.Queue] = {}
_tasks: dict[str, asyncio.Task] = {}


@router.post("/sessions", response_model=SessionResponse, status_code=202)
async def create_session(body: CreateSessionRequest) -> SessionResponse:
    state = PipelineState(config=body.config)
    sid = state.session_id

    q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = state
    _queues[sid] = q

    _tasks[sid] = asyncio.create_task(_run(sid, state, q))

    return SessionResponse(
        session_id=sid,
        current_stage=state.current_stage,
        progress_pct=state.progress_pct,
    )


async def _run(sid: str, state: PipelineState, q: asyncio.Queue) -> None:
    try:
        result = await run_pipeline(state, q)
        _sessions[sid] = result
    except Exception as exc:
        log.exception("Pipeline failed for session %s", sid)
        _sessions[sid].errors.append(str(exc))
        _sessions[sid].current_stage = "error"
        await q.put({"stage": "error", "pct": 0, "message": str(exc)})
    finally:
        await q.put(None)  # sentinel — stream is done


@router.get("/sessions/{session_id}/stream")
async def stream_session(session_id: str) -> StreamingResponse:
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    q = _queues.get(session_id)
    if q is None:
        raise HTTPException(status_code=410, detail="Stream already consumed")

    async def event_generator() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            _queues.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str) -> SessionResponse:
    state = _sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionResponse(
        session_id=session_id,
        current_stage=state.current_stage,
        progress_pct=state.progress_pct,
        config=state.config,
        pdf_signed_url=f"/api/v1/sessions/{session_id}/pdf" if state.pdf_gcs_uri else None,
        errors=state.errors,
    )


@router.get("/sessions/{session_id}/pdf")
async def download_pdf(session_id: str) -> Response:
    state = _sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not state.pdf_gcs_uri:
        raise HTTPException(status_code=404, detail="PDF not ready")
    data, content_type = gcs.read_blob(session_id, "final", "storybook.pdf")
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="storybook-{session_id[:8]}.pdf"'},
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions() -> list[SessionResponse]:
    return [
        SessionResponse(
            session_id=sid,
            current_stage=s.current_stage,
            progress_pct=s.progress_pct,
            config=s.config,
            errors=s.errors,
        )
        for sid, s in _sessions.items()
    ]
