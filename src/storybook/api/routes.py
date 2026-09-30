from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import random
from datetime import datetime, timezone
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from storybook.api.models import CreateSessionRequest, SessionResponse
from storybook.config import settings
from storybook.db import store
from storybook.models import ACTIVE_AGE_RANGES, PipelineState
from storybook.substrate import ate
from storybook.tools import gcs

log = logging.getLogger(__name__)
router = APIRouter()


# Catalogs of diverse art styles, literary traditions, and creative moods for dynamic seed injection
_ART_STYLE_SEEDS = [
    "Scandinavian mid-century gouache (in the spirit of Elsa Beskow or Olle Eksell)",
    "Japanese shin-hanga or ukiyo-e woodblock printing (Kawase Hasui, Hiroshige)",
    "Oaxacan folk art with bold painted figures on textured amate bark",
    "Cyanotype botanical blueprint print with stark Prussian blue and white silhouettes",
    "Persian / Safavid or Mughal miniature painting with intricate floral borders and flat perspective",
    "1920s Bauhaus geometric collage with bold primary colors and dynamic diagonals",
    "Medieval illuminated manuscript with rich lapis lazuli, vermilion, and gilded borders",
    "Linocut printmaking with expressive gouge marks and a strict 2-color spot palette",
    "Victorian scratchboard / scraperboard with crisp white-on-black engraved linework",
    "Stained glass panel aesthetic with heavy leaded cames and luminous jewel tones",
    "Appalachian patchwork quilt and folk textile applique aesthetic",
    "1970s psychedelic folk art with liquid curves, chromatic vibration, and ink flourishes",
    "Polish Wycinanki papercutting with symmetrical layered silhouettes and high contrast",
    "Pointillist / divisionist tempera on unprimed linen canvas",
    "Sumi-e Japanese ink wash with expressive dry-brush calligraphic strokes and negative space",
    "Art Nouveau botanical decorative art (flowing organic whiplash curves in the style of Alphonse Mucha)",
    "Chalk pastel and charcoal on toned raw kraft paper with visible grain",
    "Papercraft shadowbox with layered cut paper, realistic depth, and directional ambient lighting",
    "Fauvist expressive painting with vivid, non-naturalistic color blocks and thick impasto strokes",
    "Risograph printing with overlapping translucent fluorescent inks and subtle grain misregistration",
    "Byzantine mosaic with shimmering tesserae tiles and gilded halo accents",
    "Gouache folk painting with naive proportions and decorative borders (Scandinavian rosemaling or Slavic lubok)",
    "Modern flat vector editorial illustration with exaggerated proportions and retro mid-century palettes",
    "Early 20th-century travel poster with flat color planes and bold atmospheric lighting",
]

# Western-canon buckets. Each entry names concrete, widely-recognized exemplars so the
# model anchors on stories an ordinary American adult already knows, rather than drifting
# to obscure sources. Skewed toward English-language works, with the major Continental,
# Classical, and Russian pillars represented.
_TRADITION_SEEDS = [
    "Greek mythology and epic (the Odyssey, the Trojan horse, Theseus and the Minotaur, "
    "Icarus, King Midas, Pandora's box, Persephone, Perseus and Medusa, the labors of Hercules)",
    "Aesop's best-known fables (the Tortoise and the Hare, the Boy Who Cried Wolf, the Lion "
    "and the Mouse, the Ant and the Grasshopper, the Fox and the Grapes, the Goose that Laid the Golden Eggs)",
    "Roman and Latin classics (Ovid's Metamorphoses — Echo and Narcissus, Daedalus, Arachne; "
    "Romulus and Remus; Virgil's Aeneid; Androcles and the Lion)",
    "Norse mythology (Thor and his hammer, Loki's tricks, Odin, Fenrir, the rainbow bridge, Ragnarok)",
    "Arthurian legend and English medieval romance (King Arthur, the sword in the stone, Excalibur, "
    "the Round Table, Merlin, Sir Gawain and the Green Knight, Robin Hood)",
    "Grimm and Andersen headline fairy tales (Cinderella, Hansel and Gretel, Snow White, Rumpelstiltskin, "
    "the Bremen Town Musicians; the Ugly Duckling, the Little Mermaid, the Emperor's New Clothes, the Princess and the Pea)",
    "French classics (Perrault — Puss in Boots, Sleeping Beauty, Little Red Riding Hood, Cinderella; "
    "Beauty and the Beast; La Fontaine's fables; Jules Verne — Around the World in Eighty Days, 20,000 Leagues; "
    "Dumas — The Three Musketeers; Hugo — The Hunchback of Notre-Dame)",
    "Spanish and Italian classics (Cervantes' Don Quixote and the windmills; Collodi's Pinocchio; "
    "Dante's Inferno; the tale of El Cid)",
    "Russian classics and Russian fairy tales (Pushkin — The Tale of Tsar Saltan, The Golden Cockerel; "
    "the Firebird; Baba Yaga; Vasilisa the Beautiful; Tolstoy's short tales for children)",
    "English children's classics (Alice's Adventures in Wonderland, The Wind in the Willows, Peter Pan, "
    "The Jungle Book, Just So Stories, The Tale of Peter Rabbit, The Secret Garden, A Little Princess, The Velveteen Rabbit)",
    "American classics (Tom Sawyer, Huckleberry Finn, Rip Van Winkle, The Legend of Sleepy Hollow, "
    "Little Women, The Call of the Wild, The Wonderful Wizard of Oz, Moby-Dick, The Last of the Mohicans)",
    "British adventure and Victorian favorites (Treasure Island, Robinson Crusoe, Gulliver's Travels, "
    "The Swiss Family Robinson, A Christmas Carol, Oliver Twist, Dr. Jekyll and Mr. Hyde, The Three Little Pigs)",
    "Narrative poems that everyone half-remembers (The Rime of the Ancient Mariner, Paul Revere's Ride, "
    "The Charge of the Light Brigade, Jabberwocky, The Owl and the Pussycat, Casey at the Bat, "
    "A Visit from St. Nicholas, The Pied Piper of Hamelin)",
    "Shakespeare adapted for children (A Midsummer Night's Dream, The Tempest, Macbeth's witches, "
    "Romeo and Juliet, Twelfth Night — Lamb's Tales from Shakespeare is the usual doorway)",
    "Gothic and early science fiction (Frankenstein, Dracula, The Time Machine, The War of the Worlds, "
    "Poe's The Raven, The Legend of the Headless Horseman, A Connecticut Yankee in King Arthur's Court)",
    "The Arabian Nights as the West received it (Aladdin and the lamp, Ali Baba and the Forty Thieves, "
    "the voyages of Sinbad, Scheherazade)",
    "American tall tales and frontier folklore (Paul Bunyan and Babe, Johnny Appleseed, Pecos Bill, "
    "John Henry, Davy Crockett)",
    "Allegory and quest literature (The Pilgrim's Progress, Milton's Paradise Lost, Homer's Iliad, "
    "Jason and the Argonauts, the Fables of Bidpai as retold in Europe)",
]

_MOOD_SEEDS = [
    "Whimsical, playful, and sun-drenched with gentle humor",
    "Dreamy, atmospheric, and quiet with a sense of midnight wonder",
    "Energetic, rhythmic, and jaunty with rapid momentum",
    "Eerie, mystical, and shadow-dappled but comforting for children",
    "Grand, mythical, and heroic with majestic scale",
    "Tender, heartfelt, and introspective with cozy warmth",
]

# Rolling recency buffer to prevent repetitive outputs across consecutive calls.
# Seeded with frequent attractors so they are avoided even on a fresh process start.
_MAX_LUCKY_HISTORY = 10
_lucky_history: deque[dict[str, str]] = deque(
    [
        {"title": "Just So Stories", "author": "Rudyard Kipling", "style": "Soviet constructivist poster"},
        {"title": "Alice's Adventures in Wonderland", "author": "Lewis Carroll", "style": "Victorian engraving"},
        {"title": "The Wind in the Willows", "author": "Kenneth Grahame", "style": "Classic watercolor with ink wash"},
    ],
    maxlen=_MAX_LUCKY_HISTORY,
)


def _sample_page_count() -> int:
    """Sample page count from a normal distribution with mean 24, stdev 4.

    Clamped to [6, 64] to match the supported page count range.
    """
    return max(6, min(64, round(random.gauss(24, 4))))


def _build_lucky_prompt(page_count: int | None = None) -> str:
    if page_count is None:
        page_count = _sample_page_count()
    styles_sample = random.sample(_ART_STYLE_SEEDS, 3)
    tradition_sample = random.sample(_TRADITION_SEEDS, 2)
    mood = random.choice(_MOOD_SEEDS)

    recent_lines = [
        f'- "{item["title"]}" by {item["author"]} (Style: {item.get("style", "unspecified")})'
        for item in _lucky_history
    ]
    recency_exclusions = "\n".join(recent_lines)

    return f"""You are a children's-book art director picking ONE storybook config.

Choose a WELL-KNOWN classic of the Western canon and give it an unexpected artistic
treatment. The surprise should come from the art direction, the age band, and which
episode you adapt — NOT from digging up an obscure source.

CRITICAL EXCLUSIONS — RECENT RUNS (DO NOT REPEAT ANY OF THESE AUTHORS, TITLES, OR STYLES):
{recency_exclusions}

THE RECOGNITION BAR (this is the most important constraint):
An average, reasonably well-read American adult must recognize the title on sight, and
should usually be able to summarize the plot from memory. Think "the kind of book that
shows up on a high-school reading list, a Disney adaptation, or a shelf of children's
classics." If you find yourself picking something you'd have to explain, pick again.
- Strongly prefer English-language works. Major French, Spanish, Italian, German,
  Russian, Greek, and Latin classics are welcome, but only their famous ones.
- Vary the era, country, and genre from run to run — do not settle into one shelf.
- Choose a real public-domain work available on Project Gutenberg.
- Avoid: obscure regional folklore, minor works by famous authors, and anthologies no
  one outside a literature department has heard of.

CREATIVE CATALYSTS FOR THIS RUN (Draw strong inspiration from these; do not default to generic standbys):
- Canon shelves to draw from — pick a famous title from one of these, or from the
  Western canon broadly if neither shelf inspires you:
  * {tradition_sample[0]}
  * {tradition_sample[1]}
- Art direction sparks (pick one or riff creatively):
  * {styles_sample[0]}
  * {styles_sample[1]}
  * {styles_sample[2]}
- Tone / Atmosphere spark:
  * {mood}

CRITICAL: vary `target_age` widely across runs. The 5 bands are 2-3, 4-5, 6-7, 8-9, 10-12.
Pick the band that genuinely fits the source you chose. Toddler picture books and simple
animal tales are 2-3 or 4-5. Aesop's shorter fables, Beatrix Potter, simple folk tales
land at 4-5 or 6-7. Adventure abridgements and richer myths land at 8-9 or 10-12. Do NOT
default to the oldest band — most of these sources should be adapted DOWN to the child.

SOURCES:
Treat all sources as ABRIDGEABLE — the interesting move is taking a big, famous work and
adapting ONE chapter, fable, episode, myth, or canto down to the chosen age band. A
Moby-Dick for six-year-olds or a single labor of Hercules for toddlers is exactly the
kind of stretch this tool exists to test. Scope the narrative arc and pacing to fit
{page_count} pages.

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
- page_count: integer, exactly {page_count}
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

    page_count = _sample_page_count()
    client = genai.Client(vertexai=True, project=settings.gcp_project_id, location="global")
    prompt = _build_lucky_prompt(page_count=page_count)
    response = client.models.generate_content(
        model=settings.model_fast,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            # 1.3 keeps the surprise but produces clean JSON on the first try; 1.9 was
            # spending a lot of latency on retries.
            temperature=1.3,
            response_mime_type="application/json",
            response_schema=_LuckyOutput,
        ),
    )
    result = json.loads(response.text)
    result["page_count"] = page_count
    _lucky_history.append(
        {
            "title": result.get("title", ""),
            "author": result.get("author", ""),
            "style": (result.get("image_spec", "")[:80] + "...") if len(result.get("image_spec", "")) > 80 else result.get("image_spec", ""),
        }
    )
    return result


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
    "page_count",
]


class ShuffleRequest(BaseModel):
    field: ShuffleField
    title: str = ""
    author: str = ""
    target_age: str = ""
    page_count: Optional[int] = None
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
    page_count: Optional[int] = None


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

THE RECOGNITION BAR: pick a well-known classic of the Western canon — something an
average, reasonably well-read American adult would recognize on sight and could usually
summarize from memory. Skew English-language; famous French, Spanish, Italian, German,
Russian, Greek, and Latin classics are also welcome. Avoid obscure regional folklore and
minor works by famous authors.

If a target age is given, pick a source the kid in that band would actually enjoy
(toddlers want short animal tales and simple fables; 10-12 can take Treasure Island,
Verne, or the Odyssey). If a text_spec is given (e.g. a poetic form), pick a source
compatible with that form. If image_spec suggests a culture or era, lean into a canon
work from that tradition.

Return JSON: title (exact title from Project Gutenberg), author. Surprise me with WHICH
classic — vary era, country, and genre — but stay inside the canon.
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
{style_seeds}

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
    if req.field == "page_count":
        return ShuffleResponse(page_count=_sample_page_count())

    from google import genai
    from google.genai import types as gtypes
    from storybook.config import settings

    client = genai.Client(vertexai=True, project=settings.gcp_project_id, location="global")
    ctx = _shuffle_context(req)

    if req.field == "title_author":
        prompt = _SHUFFLE_TITLE_AUTHOR.format(context=ctx)
        schema: type[BaseModel] = _TitleAuthorOut
    elif req.field == "image_spec":
        seeds = "; ".join(random.sample(_ART_STYLE_SEEDS, 4))
        prompt = _SHUFFLE_IMAGE_SPEC.format(context=ctx, style_seeds=seeds)
        schema = _SingleStringOut
    else:
        tmpl = {
            "text_spec": _SHUFFLE_TEXT_SPEC,
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


async def _load_session_state(session_id: str, prefer_gcs: bool = False) -> Optional[PipelineState]:
    """Load session state from rqlite or GCS checkpoint."""
    state: Optional[PipelineState] = None
    if prefer_gcs:
        try:
            state = await asyncio.to_thread(gcs.load_pipeline_state, session_id)
        except Exception:
            log.debug("Could not load state.json from GCS for %s", session_id)
    if state is None:
        try:
            state = await store.get_session(session_id)
        except Exception:
            log.exception("DB unavailable for session %s", session_id)
    if state is None and not prefer_gcs:
        try:
            state = await asyncio.to_thread(gcs.load_pipeline_state, session_id)
        except Exception:
            log.debug("Could not load state.json from GCS for %s", session_id)
    return state


async def _require_session(session_id: str, prefer_gcs: bool = False) -> PipelineState:
    state = await _load_session_state(session_id, prefer_gcs=prefer_gcs)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return state


def _to_session_response(state: PipelineState, is_running: Optional[bool] = None) -> SessionResponse:
    sid = state.session_id
    if is_running is None:
        is_running = state.current_stage not in ("done", "error", "cancelled")
    return SessionResponse(
        session_id=sid,
        current_stage=state.current_stage,
        progress_pct=state.progress_pct,
        config=state.config,
        pdf_signed_url=f"/api/v1/sessions/{sid}/pdf" if state.pdf_gcs_uri else None,
        wide_pdf_url=f"/api/v1/sessions/{sid}/pdf/wide" if state.wide_pdf_gcs_uri else None,
        trace_url=state.trace_url or None,
        errors=state.errors,
        resumable=state.current_stage in ("error", "cancelled")
        or (state.current_stage != "done" and not is_running),
        started_at=state.started_at,
        finished_at=state.finished_at,
        adapted_from_source=state.adapted_from_source,
    )


async def _persist_session(state: PipelineState) -> None:
    sid = state.session_id
    await asyncio.to_thread(gcs.save_pipeline_state, sid, state)
    try:
        await store.upsert_session(state)
    except Exception:
        log.exception("Failed to persist session %s to DB", sid)


@router.post("/sessions", response_model=SessionResponse, status_code=202)
async def create_session(body: CreateSessionRequest) -> SessionResponse:
    state = PipelineState(config=body.config, started_at=_now())
    sid = state.session_id

    await _persist_session(state)
    await asyncio.to_thread(gcs.save_progress_events, sid, [], False)

    try:
        actor_info = await ate.create_actor(
            template=settings.substrate_template,
            atespace=settings.substrate_atespace,
            name=sid,
            resume=False,
        )
        log.info(
            "Provisioned Substrate Actor for session %s: %s",
            sid,
            actor_info,
        )
    except Exception as exc:
        log.exception("Failed to provision Substrate Actor for session %s", sid)
        state.current_stage = "error"
        state.finished_at = _now()
        state.errors.append(f"Failed to provision Substrate Actor: {exc}")
        await _persist_session(state)
        await asyncio.to_thread(
            gcs.save_progress_events,
            sid,
            [{"stage": "error", "pct": 0, "message": str(exc), "seq": 0, "ts": _now()}],
            True,
        )
        raise HTTPException(status_code=500, detail=f"Actor provisioning failed: {exc}")

    return _to_session_response(state, is_running=True)


@router.post("/sessions/{session_id}/cancel", response_model=SessionResponse)
async def cancel_session(session_id: str) -> SessionResponse:
    state = await _require_session(session_id, prefer_gcs=True)

    await ate.stop_actor(name=session_id, atespace=settings.substrate_atespace)

    if state.current_stage != "done":
        state.current_stage = "error"
        state.finished_at = _now()
        if "Cancelled by user" not in state.errors:
            state.errors.append("Cancelled by user")
        await _persist_session(state)

        events, _ = await asyncio.to_thread(gcs.load_progress_events, session_id)
        events.append(
            {
                "stage": "error",
                "pct": state.progress_pct,
                "message": "Cancelled by user",
                "seq": len(events),
                "ts": _now(),
            }
        )
        await asyncio.to_thread(gcs.save_progress_events, session_id, events, True)

    return _to_session_response(state, is_running=False)


@router.post("/sessions/{session_id}/resume", response_model=SessionResponse, status_code=202)
async def resume_session(session_id: str) -> SessionResponse:
    state = await _require_session(session_id, prefer_gcs=True)

    if await ate.is_actor_running(name=session_id, atespace=settings.substrate_atespace):
        raise HTTPException(status_code=409, detail="Session actor is still running")

    state.errors = []
    state.current_stage = "resuming"
    state.finished_at = None

    await _persist_session(state)
    await asyncio.to_thread(
        gcs.save_progress_events,
        session_id,
        [
            {
                "stage": "resuming",
                "pct": state.progress_pct or 5,
                "message": "Resuming session on new Substrate Actor",
                "seq": 0,
                "ts": _now(),
            }
        ],
        False,
    )

    try:
        actor_info = await ate.create_actor(
            template=settings.substrate_template,
            atespace=settings.substrate_atespace,
            name=session_id,
            resume=True,
        )
        log.info(
            "Provisioned resumed Substrate Actor for session %s: %s",
            session_id,
            actor_info,
        )
    except Exception as exc:
        log.exception("Failed to provision resumed Substrate Actor for session %s", session_id)
        state.current_stage = "error"
        state.finished_at = _now()
        state.errors.append(f"Failed to resume Substrate Actor: {exc}")
        await _persist_session(state)
        raise HTTPException(status_code=500, detail=f"Actor resume failed: {exc}")

    return _to_session_response(state, is_running=True)


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    last_seq: int = Query(default=-1, description="Last event sequence number received"),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    await _require_session(session_id, prefer_gcs=True)

    initial_cursor = last_seq
    if last_event_id is not None:
        try:
            initial_cursor = max(initial_cursor, int(last_event_id))
        except ValueError:
            pass

    async def event_generator() -> AsyncIterator[str]:
        cursor = initial_cursor
        ticks = 0
        while True:
            events, is_done = await asyncio.to_thread(gcs.load_progress_events, session_id)
            emitted_terminal = False
            for idx, ev in enumerate(events):
                seq = int(ev.get("seq", idx))
                if seq <= cursor:
                    continue
                cursor = seq
                payload = dict(ev)
                payload["seq"] = seq
                yield f"id: {seq}\ndata: {json.dumps(payload)}\n\n"
                if payload.get("stage") in ("done", "error"):
                    emitted_terminal = True

            if is_done or emitted_terminal:
                # Reclaim actor resources in the background once terminal
                asyncio.create_task(
                    ate.stop_actor(name=session_id, atespace=settings.substrate_atespace)
                )
                break

            ticks += 1
            # Every ~4s check whether the actor died unexpectedly mid-pipeline
            if ticks % 4 == 0:
                actor = await ate.get_actor(
                    name=session_id, atespace=settings.substrate_atespace
                )
                actor_state = (actor or {}).get("state", "")
                if actor is None or actor_state in (
                    "ACTOR_STATE_CRASHED",
                    "ACTOR_STATE_SUSPENDED",
                    "ACTOR_STATE_DELETING",
                ):
                    # Re-check GCS events/state once to avoid racing normal completion
                    await asyncio.sleep(0.5)
                    latest_events, latest_done = await asyncio.to_thread(
                        gcs.load_progress_events, session_id
                    )
                    if latest_done or any(
                        e.get("stage") in ("done", "error") for e in latest_events
                    ):
                        continue
                    latest_state = await _load_session_state(session_id, prefer_gcs=True)
                    if latest_state and latest_state.current_stage not in ("done", "error"):
                        err_msg = (
                            f"Actor terminated unexpectedly ({actor_state or 'DELETED'}) "
                            f"at stage '{latest_state.current_stage}'"
                        )
                        log.warning("Session %s: %s", session_id, err_msg)
                        latest_state.current_stage = "error"
                        latest_state.finished_at = _now()
                        latest_state.errors.append(err_msg)
                        await _persist_session(latest_state)
                        err_seq = len(latest_events)
                        err_ev = {
                            "stage": "error",
                            "pct": latest_state.progress_pct,
                            "message": err_msg,
                            "seq": err_seq,
                            "ts": _now(),
                        }
                        latest_events.append(err_ev)
                        await asyncio.to_thread(
                            gcs.save_progress_events, session_id, latest_events, True
                        )
                        yield f"id: {err_seq}\ndata: {json.dumps(err_ev)}\n\n"
                        break

            if ticks % 15 == 0:
                yield ": heartbeat\n\n"

            await asyncio.sleep(1.0)

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
    state = await _require_session(session_id, prefer_gcs=True)
    is_running = False
    if state.current_stage not in ("done", "error", "cancelled"):
        is_running = await ate.is_actor_running(
            name=session_id, atespace=settings.substrate_atespace
        )
        if not is_running:
            # Refresh from GCS in case the actor just completed
            refreshed = await _load_session_state(session_id, prefer_gcs=True)
            if refreshed is not None:
                state = refreshed
    return _to_session_response(state, is_running=is_running)


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
    state = await _require_session(session_id, prefer_gcs=True)
    if not state.pdf_gcs_uri:
        raise HTTPException(status_code=404, detail="PDF not ready")
    data, _ = gcs.read_blob(session_id, "final", "storybook.pdf")
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
    state = await _require_session(session_id, prefer_gcs=True)
    if not state.wide_pdf_gcs_uri:
        raise HTTPException(status_code=404, detail="Wide PDF not ready")
    data, _ = gcs.read_blob(session_id, "final", "storybook_wide.pdf")
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
    states = await store.list_sessions(
        status=status, limit=limit, offset=offset, sort=sort
    )
    return [_to_session_response(s) for s in states]

