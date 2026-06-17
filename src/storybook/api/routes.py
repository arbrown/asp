from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from storybook.agents.pipeline import run_pipeline
from storybook.api.models import CreateSessionRequest, SessionResponse
from storybook.models import PipelineState
from storybook.tools import gcs

log = logging.getLogger(__name__)
router = APIRouter()

# In-memory session store — single user, single pod, no persistence needed
_sessions: dict[str, PipelineState] = {}
_queues: dict[str, asyncio.Queue] = {}
_tasks: dict[str, asyncio.Task] = {}


_LUCKY_PROMPT = """You are a wildly creative children's book art director with encyclopedic knowledge \
of world literature and art history. Generate a surprising, one-of-a-kind storybook configuration \
by selecting a real public domain literary work and giving it an unexpected artistic treatment.

VARY WIDELY across these dimensions each time:

SOURCE WORKS — pick from any era and culture, not just fairy tales:
Epic poetry (Homer, Virgil, Dante, Milton), Romantic poetry (Keats, Shelley, Byron, Pushkin, Goethe), \
Lyric poetry (Emily Dickinson, Walt Whitman, Matsuo Bashō, Li Bai, Rumi), \
Myths (Greek, Norse, Hindu, Egyptian, Aztec, Celtic), \
Fables (Aesop, La Fontaine, Panchatantra, Jataka Tales), \
Folk tales (1001 Nights, Slavic, African, Japanese, Native American), \
Adventure (Jules Verne, Robert Louis Stevenson, H.G. Wells), \
Satire (Voltaire's Candide, Swift's Gulliver, Carroll's Alice), \
Gothic (Poe, Stoker, Shelley), Nature (Thoreau, John Muir), etc.

ART STYLES — be HIGHLY specific, pick something unexpected:
- Risograph printing, strict 3-color palette, visible grain and halftone dots
- Medieval illuminated manuscript with gilded geometric borders and flat tempera
- 1930s Soviet constructivist poster art with diagonal composition and bold sans-serif
- Japanese Taisho-era woodblock (shin-hanga) with bokashi gradients and kiwame seal
- Mexican Oaxacan folk art, amate bark paper texture, Zapotec pattern borders
- Art Nouveau poster (Mucha-esque), flowing hair merging with botanical frames
- Norwegian rosemaling, symmetrical C-scroll florals on deep indigo grounds
- Persian/Mughal miniature, lapis and gold, intricate geometric tile borders
- Lino-cut with rough uneven edges, 2 colors max, no gradients
- Stained glass, heavy black leading lines, pure jewel-tone fills, no shading
- Scandinavian mid-century (Elsa Beskow-style), mushroom tones, soft gouache
- Silhouette papercut, solid black figures on single vivid background color
- Byzantine mosaic, tesserae visible, gold leaf ground, frontal figures
- Naive/outsider folk art, flattened perspective, pure unmixed colors, pattern fills
- 1970s psychedelic, surreal scale, organic lettering, impossible architecture

CUSTOM INSTRUCTIONS must include 2-3 FUN RULES chosen from:
- A character always wears or carries one very specific item visible in every scene
- A character has a verbal catchphrase they say under pressure (quote it)
- Every page hides a specific small object for the reader to find (name it)
- A particular animal appears in the background of every spread (name it)
- One character communicates only through humming or gestures
- A specific color appears in a meaningful pattern across pages
- A character secretly counts objects and the count continues across pages
- Tiny handwritten notes or letters are hidden in the illustrations

Be bold, specific, and surprising — pick something the user would never have thought of themselves. \
Mix unexpected source+style pairings (e.g. Moby Dick in Byzantine mosaic; Aesop in Soviet propaganda style).

Return JSON with these exact fields:
- title: exact title as it appears on Project Gutenberg
- author: author's name
- target_age: one of "4-5", "6-8", or "9-12" (match to source complexity)
- page_count: integer between 10 and 20
- text_spec: literary/narrative form for the adaptation (1-3 sentences, or empty string for prose)
- image_spec: exactly 2-3 sentences, hyper-specific art direction
- custom_instructions: exactly 2-4 sentences with character rules and story focus
"""


class _LuckyOutput(BaseModel):
    title: str
    author: str
    target_age: str
    page_count: int
    text_spec: str
    image_spec: str
    custom_instructions: str


def _generate_lucky() -> dict:
    from google import genai
    from google.genai import types as gtypes
    from storybook.config import settings

    client = genai.Client(vertexai=True, project=settings.gcp_project_id, location="global")
    response = client.models.generate_content(
        model=settings.model_fast,
        contents=_LUCKY_PROMPT,
        config=gtypes.GenerateContentConfig(
            temperature=1.9,
            response_mime_type="application/json",
            response_schema=_LuckyOutput,
        ),
    )
    return json.loads(response.text)


@router.get("/lucky")
async def lucky() -> dict:
    try:
        return await asyncio.to_thread(_generate_lucky)
    except Exception as exc:
        log.exception("Lucky generation failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/sessions", response_model=SessionResponse, status_code=202)
async def create_session(body: CreateSessionRequest) -> SessionResponse:
    state = PipelineState(config=body.config)
    sid = state.session_id

    q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = state
    _queues[sid] = q
    _tasks[sid] = asyncio.create_task(_run(sid, state, q))

    await asyncio.to_thread(gcs.save_session_meta, sid, _session_meta(state))

    return SessionResponse(
        session_id=sid,
        current_stage=state.current_stage,
        progress_pct=state.progress_pct,
        config=state.config,
    )


def _session_meta(state: PipelineState) -> dict:
    return {
        "session_id": state.session_id,
        "config": state.config.model_dump(),
        "current_stage": state.current_stage,
        "progress_pct": state.progress_pct,
        "pdf_gcs_uri": state.pdf_gcs_uri,
        "errors": state.errors,
    }


async def _run(sid: str, state: PipelineState, q: asyncio.Queue, resume: bool = False) -> None:
    try:
        result = await run_pipeline(state, q, resume=resume)
        _sessions[sid] = result
    except Exception as exc:
        log.exception("Pipeline failed for session %s", sid)
        _sessions[sid].errors.append(str(exc))
        _sessions[sid].current_stage = "error"
        await q.put({"stage": "error", "pct": 0, "message": str(exc)})
    finally:
        await q.put(None)  # sentinel — stream is done
        await asyncio.to_thread(gcs.save_session_meta, sid, _session_meta(_sessions[sid]))


@router.post("/sessions/{session_id}/resume", response_model=SessionResponse, status_code=202)
async def resume_session(session_id: str) -> SessionResponse:
    state = _sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if state.current_stage not in ("error", "done"):
        raise HTTPException(status_code=409, detail="Session is still running")

    state.errors = []
    state.current_stage = "resuming"
    state.progress_pct = 0

    q: asyncio.Queue = asyncio.Queue()
    _queues[session_id] = q
    _tasks[session_id] = asyncio.create_task(_run(session_id, state, q, resume=True))

    await asyncio.to_thread(gcs.save_session_meta, session_id, _session_meta(state))

    return SessionResponse(
        session_id=session_id,
        current_stage=state.current_stage,
        progress_pct=state.progress_pct,
        config=state.config,
    )


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
        resumable=state.current_stage == "error",
    )


@router.get("/sessions/{session_id}/pages/{page_number}/html")
async def get_page_html(session_id: str, page_number: int) -> Response:
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        data = gcs.read_bytes(session_id, "pages", f"page_{page_number:02d}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Page HTML not ready")
    return Response(
        content=data,
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sessions/{session_id}/images/{page_number}")
async def get_page_image(session_id: str, page_number: int) -> Response:
    state = _sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        data, _ = gcs.read_blob(session_id, "images", f"page_{page_number:02d}.png")
    except Exception:
        raise HTTPException(status_code=404, detail="Image not ready")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
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
        headers={"Content-Disposition": f'inline; filename="storybook-{session_id[:8]}.pdf"'},
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
            resumable=s.current_stage == "error",
        )
        for sid, s in _sessions.items()
    ]
