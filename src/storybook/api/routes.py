from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from storybook.agents.pipeline import run_pipeline
from storybook.api.models import CreateSessionRequest, SessionResponse
from storybook.db import store
from storybook.models import PipelineState
from storybook.tools import gcs

log = logging.getLogger(__name__)
router = APIRouter()

# In-memory store for active sessions — DB is the durable layer
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

PAGE LAYOUT & TYPOGRAPHY — pick one layout and one typography direction and work them into image_spec:
Layouts (pick one):
- Full-bleed illustration as page background with the story text in a semi-transparent panel at the bottom
- Illustration fills the top two-thirds; text sits beneath in a clean band
- Illustration occupies the left half; text runs in a narrow column on the right
- Text floats at the top of the page; illustration fills everything below
- Illustration on the right half; text on the left in a contrasting color band

Typography & color (pick one):
- Deep jewel-tone page (navy, forest, burgundy) with white or cream text
- Warm sepia/parchment background with ink-brown text; aged-paper feel
- High-contrast black page with bright single-color illustration accents and white text
- Pastel ground with a bold decorative display font for the story text
- Cream background with a tall elegant serif; subtle colored drop caps on first word of each page
- Kraft paper texture, hand-lettered style text, earthy palette
- Bright white with a single vivid accent color used consistently for text highlights

CUSTOM INSTRUCTIONS — vary the approach each time. Do NOT default to a recurring motif on every page. \
Choose ONE of these strategies at random:

Strategy A (no motif — pure story focus): Give 2-3 sentences that define the emotional arc, \
the narrative voice, and which part of the source work to foreground. No recurring visual gimmick.

Strategy B (one light motif): A single subtle recurring element — a color, an animal glimpsed in the \
background, or an object that appears on a few (not all) pages when it fits naturally. Plus a story-focus sentence.

Strategy C (one strong motif): One vivid, specific rule that shapes every spread: a character catchphrase \
(quote it), a hidden object (name it precisely), or an ongoing count. Keep it to one rule only.

Strategy D (character voice + story arc): Define one character's distinctive personality or speech \
pattern, then give the emotional beats the adaptation should hit across its pages.

Be bold, specific, and surprising — pick something the user would never have thought of themselves. \
Mix unexpected source+style pairings (e.g. Moby Dick in Byzantine mosaic; Aesop in Soviet propaganda style). \
Actively avoid combinations you may have generated before.

Return JSON with these exact fields:
- title: exact title as it appears on Project Gutenberg
- author: author's name
- target_age: one of "4-5", "6-8", or "9-12" (match to source complexity)
- page_count: integer between 10 and 20
- text_spec: literary/narrative form for the adaptation (1-3 sentences, or empty string for prose)
- image_spec: exactly 2-3 sentences — hyper-specific art direction PLUS the chosen page layout \
  and typography direction woven in naturally
- custom_instructions: 2-4 sentences using whichever strategy you chose above
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _require_session(session_id: str) -> PipelineState:
    state = _sessions.get(session_id)
    if state is None:
        try:
            state = await store.get_session(session_id)
        except Exception:
            log.exception("DB unavailable for session %s", session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return state


def _to_session_response(state: PipelineState) -> SessionResponse:
    sid = state.session_id
    return SessionResponse(
        session_id=sid,
        current_stage=state.current_stage,
        progress_pct=state.progress_pct,
        config=state.config,
        pdf_signed_url=f"/api/v1/sessions/{sid}/pdf" if state.pdf_gcs_uri else None,
        wide_pdf_url=f"/api/v1/sessions/{sid}/pdf/wide" if state.wide_pdf_gcs_uri else None,
        trace_url=state.trace_url or None,
        errors=state.errors,
        resumable=state.current_stage == "error",
        started_at=state.started_at,
        finished_at=state.finished_at,
    )


def _session_meta(state: PipelineState) -> dict:
    return {
        "session_id": state.session_id,
        "config": state.config.model_dump(),
        "current_stage": state.current_stage,
        "progress_pct": state.progress_pct,
        "pdf_gcs_uri": state.pdf_gcs_uri,
        "wide_pdf_gcs_uri": state.wide_pdf_gcs_uri,
        "trace_url": state.trace_url,
        "errors": state.errors,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
    }


@router.post("/sessions", response_model=SessionResponse, status_code=202)
async def create_session(body: CreateSessionRequest) -> SessionResponse:
    state = PipelineState(config=body.config, started_at=_now())
    sid = state.session_id

    q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = state
    _queues[sid] = q
    _tasks[sid] = asyncio.create_task(_run(sid, state, q))

    await asyncio.to_thread(gcs.save_session_meta, sid, _session_meta(state))
    try:
        await store.upsert_session(state)
    except Exception:
        log.exception("Failed to persist new session %s to DB", sid)

    return _to_session_response(state)


async def _run(sid: str, state: PipelineState, q: asyncio.Queue, resume: bool = False) -> None:
    try:
        result = await run_pipeline(state, q, resume=resume)
        result.finished_at = _now()
        _sessions[sid] = result
    except Exception as exc:
        log.exception("Pipeline failed for session %s", sid)
        _sessions[sid].errors.append(str(exc))
        _sessions[sid].current_stage = "error"
        _sessions[sid].finished_at = _now()
        await q.put({"stage": "error", "pct": 0, "message": str(exc)})
    finally:
        await q.put(None)  # sentinel — stream is done
        final = _sessions[sid]
        await asyncio.to_thread(gcs.save_session_meta, sid, _session_meta(final))
        try:
            await store.upsert_session(final)
        except Exception:
            log.exception("Failed to persist completed session %s to DB", sid)


@router.post("/sessions/{session_id}/resume", response_model=SessionResponse, status_code=202)
async def resume_session(session_id: str) -> SessionResponse:
    state = _sessions.get(session_id)
    if state is None:
        try:
            state = await store.get_session(session_id)
        except Exception:
            log.exception("DB unavailable for resume %s", session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # Re-register in _sessions so the pipeline runner can update it
    _sessions[session_id] = state
    if state.current_stage not in ("error", "done"):
        raise HTTPException(status_code=409, detail="Session is still running")

    state.errors = []
    state.current_stage = "resuming"
    state.progress_pct = 0
    state.finished_at = None

    q: asyncio.Queue = asyncio.Queue()
    _queues[session_id] = q
    _tasks[session_id] = asyncio.create_task(_run(session_id, state, q, resume=True))

    await asyncio.to_thread(gcs.save_session_meta, session_id, _session_meta(state))
    try:
        await store.upsert_session(state)
    except Exception:
        log.exception("Failed to persist resumed session %s to DB", session_id)

    return _to_session_response(state)


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
        try:
            state = await store.get_session(session_id)
        except Exception:
            log.exception("DB unavailable for get_session %s", session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_session_response(state)


@router.get("/sessions/{session_id}/pages/{page_number}/html")
async def get_page_html(session_id: str, page_number: int) -> Response:
    await _require_session(session_id)
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
    await _require_session(session_id)
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
        try:
            state = await store.get_session(session_id)
        except Exception:
            pass
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


@router.get("/sessions/{session_id}/spreads/{spread_number}/html")
async def get_spread_html(session_id: str, spread_number: int) -> Response:
    await _require_session(session_id)
    try:
        data = gcs.read_bytes(session_id, "spreads", f"spread_{spread_number:02d}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Spread HTML not ready")
    return Response(
        content=data,
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sessions/{session_id}/spreads/{spread_number}/image/{image_index}")
async def get_spread_image(session_id: str, spread_number: int, image_index: int) -> Response:
    await _require_session(session_id)
    try:
        data, _ = gcs.read_blob(
            session_id, "images", f"spread_{spread_number:02d}_img{image_index}.png"
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Spread image not ready")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sessions/{session_id}/pdf/wide")
async def download_wide_pdf(session_id: str) -> Response:
    state = _sessions.get(session_id)
    if state is None:
        try:
            state = await store.get_session(session_id)
        except Exception:
            pass
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not state.wide_pdf_gcs_uri:
        raise HTTPException(status_code=404, detail="Wide PDF not ready")
    data, content_type = gcs.read_blob(session_id, "final", "storybook_wide.pdf")
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="storybook-wide-{session_id[:8]}.pdf"'},
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions_route(
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    sort: str = "created_at_desc",
) -> list[SessionResponse]:
    db_states: list[PipelineState] = []
    db_available = True
    try:
        db_states = await store.list_sessions(
            status=status, limit=limit, offset=offset, sort=sort
        )
    except Exception:
        log.exception("DB unavailable — falling back to in-memory sessions")
        db_available = False

    if db_available:
        # Overlay in-memory state for sessions that are actively running
        state_map = {s.session_id: s for s in db_states}
        for sid in list(state_map.keys()):
            if sid in _sessions:
                state_map[sid] = _sessions[sid]
        states = list(state_map.values())
    else:
        # Best-effort fallback: filter and sort in Python
        states = list(_sessions.values())
        if status:
            allowed = {s.strip() for s in status.split(",") if s.strip()}
            states = [s for s in states if s.current_stage in allowed]
        reverse = sort != "created_at_asc"
        states.sort(key=lambda s: s.started_at or "", reverse=reverse)
        if limit is not None:
            states = states[offset: offset + limit]

    return [_to_session_response(s) for s in states]
