from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from storybook.agents.pipeline import run_pipeline
from storybook.api.models import CreateSessionRequest, SessionResponse
from storybook.db import store
from storybook.models import ACTIVE_AGE_RANGES, PipelineState
from storybook.tools import gcs

log = logging.getLogger(__name__)
router = APIRouter()

# In-memory store for active sessions — DB is the durable layer
_sessions: dict[str, PipelineState] = {}
_queues: dict[str, asyncio.Queue] = {}
_tasks: dict[str, asyncio.Task] = {}


_LUCKY_PROMPT = """You are a children's-book art director picking ONE storybook config.

Choose a real public-domain source and an unexpected artistic treatment. Surprise me.

CRITICAL: vary `target_age` widely across runs. The 5 bands are 2-3, 4-5, 6-7, 8-9, 10-12.
Pick the band that genuinely fits the source you chose. Toddler picture books and simple
animal tales are 2-3 or 4-5. Aesop's shorter fables, Beatrix Potter, simple folk tales
land at 4-5 or 6-7. Adventure abridgements and richer myths land at 8-9 or 10-12. Do NOT
default to the oldest band — most of these sources should be adapted DOWN to the child.

SOURCES — mix of registers, not just adult literature:
- Picture-friendly: Aesop, Beatrix Potter, Brothers Grimm, Hans Christian Andersen,
  Anansi tales, Nasreddin Hodja, Jataka Tales, single Mother Goose rhymes,
  short fables from any tradition.
- Mid-grade: Just So Stories (Kipling), The Wind in the Willows (a single chapter),
  Norse / Greek / Hindu / Egyptian myths (one episode), 1001 Nights single tales,
  La Fontaine, Wilde's fairy tales, A Child's Garden of Verses.
- Older: Homer / Virgil / Dante (one episode), Pushkin, Carroll, Verne, Wells,
  Stevenson, Poe, Whitman, Bashō, Rumi, Li Bai, Dickinson.
Treat all of these as ABRIDGEABLE — even Dante can land at 8-9 if you pick one canto and
strip the theology.

ART STYLE — be hyper-specific, one direction per run. A few seeds (do not just rotate
these — riff on them):
risograph 3-color; illuminated manuscript with gilded borders; Soviet constructivist
poster; Taisho-era woodblock (shin-hanga); Oaxacan folk art on amate bark; Art Nouveau
(Mucha); Norwegian rosemaling; Persian/Mughal miniature; lino-cut, 2 colors max;
stained glass with heavy leading; Scandinavian mid-century gouache (Beskow); silhouette
papercut; Byzantine mosaic; naive/outsider folk art; 1970s psychedelic.

LAYOUT + TYPOGRAPHY — pick one of each and weave them naturally into `image_spec`:
- Layout: full-bleed-with-text-panel | top-2/3-image / bottom-text | left-image / right-text
  | text-band-top / image-below | right-image / left-text-on-color
- Type+ground: jewel-tone with cream text | sepia parchment with ink-brown text | black
  page with white text and one vivid accent | pastel ground with a bold display face |
  cream ground with tall serif and colored drop caps | kraft paper hand-lettered |
  bright white with one vivid accent color for highlights

CUSTOM INSTRUCTIONS — pick ONE strategy (do not stack):
A. No motif. 2-3 sentences on emotional arc, narrative voice, and what to foreground.
B. One light motif: a subtle recurring element on some (not all) pages.
C. One strong rule: a character catchphrase (quote it), a named hidden object, or an
   ongoing count. Keep to ONE rule.
D. Character voice + story beats: one character's distinctive speech, plus the emotional
   beats the adaptation should hit.

Return JSON:
- title, author: exact title and author as they appear on Project Gutenberg
- target_age: literal "2-3" | "4-5" | "6-7" | "8-9" | "10-12" (vary widely!)
- page_count: integer between 10 and 20
- text_spec: 1-3 sentences describing the form, or "" for plain prose
- image_spec: 2-3 sentences combining art direction + layout + typography
- custom_instructions: 2-4 sentences in your chosen strategy
"""


AgeLiteral = Literal["2-3", "4-5", "6-7", "8-9", "10-12"]


class _LuckyOutput(BaseModel):
    title: str
    author: str
    target_age: AgeLiteral
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
            # 1.3 keeps the surprise but produces clean JSON on the first try; 1.9 was
            # spending a lot of latency on retries.
            temperature=1.3,
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


# ── Shuffle: per-field re-rolls ───────────────────────────────────────────────

ShuffleField = Literal[
    "title_author",
    "text_spec",
    "image_spec",
    "custom_instructions",
]


class ShuffleRequest(BaseModel):
    field: ShuffleField
    title: str = ""
    author: str = ""
    target_age: str = ""
    text_spec: str = ""
    image_spec: str = ""
    custom_instructions: str = ""


class ShuffleResponse(BaseModel):
    # Only the fields touched by the shuffle are populated. Caller merges.
    title: Optional[str] = None
    author: Optional[str] = None
    text_spec: Optional[str] = None
    image_spec: Optional[str] = None
    custom_instructions: Optional[str] = None


def _shuffle_context(req: ShuffleRequest) -> str:
    """Build a 'what we know so far' block for field-specific prompts."""
    bits = []
    if req.title or req.author:
        bits.append(f"Title: {req.title or '(unset)'} — Author: {req.author or '(unset)'}")
    if req.target_age:
        bits.append(f"Target age: {req.target_age}")
    if req.text_spec and req.field != "text_spec":
        bits.append(f"Text spec: {req.text_spec}")
    if req.image_spec and req.field != "image_spec":
        bits.append(f"Image spec: {req.image_spec}")
    if req.custom_instructions and req.field != "custom_instructions":
        bits.append(f"Custom instructions: {req.custom_instructions}")
    return "\n".join(bits) if bits else "(nothing set yet — pick freely)"


class _TitleAuthorOut(BaseModel):
    title: str
    author: str


class _SingleStringOut(BaseModel):
    value: str


_SHUFFLE_TITLE_AUTHOR = """You are picking ONE real public-domain source work for a
children's book adaptation.

What we know so far about the project:
{context}

If a target age is given, pick a source the kid in that band would actually enjoy
(toddlers want short animal tales; 10-12 can take Verne or Pushkin). If a text_spec
is given (e.g. a poetic form), pick a source compatible with that form. If image_spec
suggests a culture or era, lean into a source from that tradition.

Return JSON: title (exact title from Project Gutenberg), author. Be surprising —
don't default to Alice or Peter Rabbit.
"""

_SHUFFLE_TEXT_SPEC = """You are choosing a literary/narrative form for ONE storybook
adaptation.

What we know so far:
{context}

Pick a form that suits the source work and age. Examples:
- 2-3 / 4-5: simple repetition, refrain, AABB rhyming couplets, short prose with sound words
- 6-7 / 8-9: ABAB quatrains, free verse, prose with a recurring chorus, limericks
- 10-12: Onegin stanzas, blank verse, Spenserian stanzas, structured prose with epigraphs
Or just "" for plain prose if a form would feel forced.

Return JSON: {{ "value": "<1-3 sentence text spec, or empty string>" }}.
"""

_SHUFFLE_IMAGE_SPEC = """You are choosing the illustration style for ONE storybook.

What we know so far:
{context}

Pick a hyper-specific, unexpected art direction (2-3 sentences) AND weave in a page
layout + typography choice. Match the source's culture/era when it would be more
interesting than to ignore it. A few seed directions to riff on (do not just rotate):
risograph 3-color; illuminated manuscript; constructivist; shin-hanga woodblock;
Oaxacan folk art; Mucha-esque Art Nouveau; rosemaling; Mughal miniature; lino-cut;
stained glass; Beskow mid-century gouache; silhouette papercut; Byzantine mosaic;
naive folk; 1970s psychedelic.

Return JSON: {{ "value": "<2-3 sentence image spec>" }}.
"""

_SHUFFLE_CUSTOM = """You are writing CUSTOM INSTRUCTIONS for ONE storybook adaptation.

What we know so far:
{context}

Pick ONE strategy and write 2-4 sentences:
A. No motif: emotional arc + narrative voice + what to foreground from the source.
B. One light motif: a subtle recurring visual element on some (not all) pages.
C. One strong rule: a quoted catchphrase, named hidden object, or ongoing count.
D. Character voice + story beats.

Return JSON: {{ "value": "<2-4 sentence custom instructions>" }}.
"""


def _shuffle(req: ShuffleRequest) -> ShuffleResponse:
    from google import genai
    from google.genai import types as gtypes
    from storybook.config import settings

    client = genai.Client(vertexai=True, project=settings.gcp_project_id, location="global")
    ctx = _shuffle_context(req)

    if req.field == "title_author":
        prompt = _SHUFFLE_TITLE_AUTHOR.format(context=ctx)
        schema: type[BaseModel] = _TitleAuthorOut
    else:
        tmpl = {
            "text_spec": _SHUFFLE_TEXT_SPEC,
            "image_spec": _SHUFFLE_IMAGE_SPEC,
            "custom_instructions": _SHUFFLE_CUSTOM,
        }[req.field]
        prompt = tmpl.format(context=ctx)
        schema = _SingleStringOut

    response = client.models.generate_content(
        model=settings.model_fast,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            temperature=1.3,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    data = json.loads(response.text)

    if req.field == "title_author":
        return ShuffleResponse(title=data["title"], author=data["author"])
    return ShuffleResponse(**{req.field: data["value"]})


@router.post("/shuffle", response_model=ShuffleResponse)
async def shuffle(req: ShuffleRequest) -> ShuffleResponse:
    try:
        return await asyncio.to_thread(_shuffle, req)
    except Exception as exc:
        log.exception("Shuffle failed for field %s", req.field)
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
    """Legacy per-page image route, kept for the History thumbnail.

    Sessions generated before the spread redesign (2026-06-17) wrote per-page
    files at images/page_NN.png. Sessions after that write per-spread files at
    images/spread_NN_imgX.png. Try the legacy path first, then fall back to the
    cover spread for new-format sessions.
    """
    await _require_session(session_id)
    candidates = [
        f"page_{page_number:02d}.png",
        "spread_00_img0.png",
        "spread_00_img1.png",
    ]
    for fname in candidates:
        try:
            data, _ = gcs.read_blob(session_id, "images", fname)
        except Exception:
            continue
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    raise HTTPException(status_code=404, detail="Image not ready")


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
    """Return one spread image.

    The frontend's progress preview always asks for image_index=0, but the spread
    planner may produce a single-image spread at index 1 (e.g. coverage="recto"),
    or a text-only spread with no images at all. Be tolerant: if the requested
    index isn't there, fall through to the other index before 404-ing. Truly
    image-less spreads still 404 — there's nothing to show.
    """
    await _require_session(session_id)
    candidates = [image_index] + [i for i in (0, 1) if i != image_index]
    for idx in candidates:
        try:
            data, _ = gcs.read_blob(
                session_id, "images", f"spread_{spread_number:02d}_img{idx}.png"
            )
        except Exception:
            continue
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    raise HTTPException(status_code=404, detail="Spread image not ready")


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
