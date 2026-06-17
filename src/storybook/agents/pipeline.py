"""
Main pipeline orchestrator.

Architecture:
  Workflow (sequential):
    1. literature_fetcher  — fetches source text via URL or Gutenberg search
    2. adapt_validate_loop — LoopAgent: story_adapter → text_validator (up to 4 retries)
                             Adapter returns structured JSON: {pages: [{story_text, page_instructions}]}
    3. character_bible     — builds visual consistency doc from assembled story_text
    4. image_loop_step     — Python function: for each page, run prompt→generate→validate
    5. pdf_step            — composes final PDF (story_text only), uploads to GCS

Progress events are written to an asyncio.Queue for SSE streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from storybook.agents.character_bible import character_bible_agent
from storybook.agents.html_layout_extractor import html_layout_extractor
from storybook.agents.html_page_verifier import html_page_verifier
from storybook.agents.image_generator import ImageContentPolicyError, ImageTokenLimitError, generate_image
from storybook.agents.image_validator import image_validator
from storybook.agents.illustration_prompter import illustration_prompter
from storybook.agents.pdf_compositor import compose_pdf, render_cover_html, render_page_html
from storybook.agents.story_adapter import story_adapter
from storybook.agents.text_validator import text_validator
from storybook.config import settings
from storybook.models import PipelineState, SessionConfig, StoryPage
from storybook.tools import gcs
from storybook.tools.gutenberg import fetch_gutenberg_url, search_gutenberg

log = logging.getLogger(__name__)

# ── ADK sub-agents ────────────────────────────────────────────────────────────

# text retry loop: adapter writes, validator approves or rejects with feedback
_text_loop = LoopAgent(
    name="text_adapt_validate",
    sub_agents=[story_adapter, text_validator],
    max_iterations=settings.text_max_retries + 1,
)

# image retry loop (per page): generate → validate; validator escalates on pass
_image_retry_loop = LoopAgent(
    name="image_generate_validate",
    sub_agents=[illustration_prompter, image_validator],
    max_iterations=settings.image_max_retries + 1,
)

# ── Runner helpers ────────────────────────────────────────────────────────────

def _make_runner(agent: LlmAgent | LoopAgent) -> Runner:
    return Runner(
        agent=agent,
        app_name="storybook",
        session_service=InMemorySessionService(),
    )


async def _run_agent(
    runner: Runner,
    session_id: str,
    message: str,
    output_key: str | None = None,
    subject_image: bytes | None = None,
    reference_image: bytes | None = None,
    prev_page_image: bytes | None = None,
) -> str:
    """Run an ADK agent and return the final text response.

    If output_key is provided, the return value is read from ADK session state
    rather than the streamed response — use this when a LoopAgent contains
    multiple sub-agents whose final responses would otherwise be concatenated.

    subject_image: the image being evaluated (passed first, before reference).
    reference_image: page 1's image for style comparison (second image part).
    prev_page_image: the previous page's illustration for continuity checking (third image part).

    Retries with exponential backoff on 429 RESOURCE_EXHAUSTED errors.
    """
    parts: list[types.Part] = [types.Part(text=message)]
    if subject_image:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=subject_image)))
    if reference_image:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=reference_image)))
    if prev_page_image:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=prev_page_image)))

    for retry in range(5):
        # Use a suffixed session ID on retries to avoid session-already-exists conflicts
        sid = session_id if retry == 0 else f"{session_id}-retry{retry}"
        await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id="pipeline",
            session_id=sid,
        )
        try:
            final = ""
            async for event in runner.run_async(
                session_id=sid,
                user_id="pipeline",
                new_message=types.Content(parts=parts),
            ):
                if event.is_final_response() and event.content:
                    for part in event.content.parts:
                        if part.text:
                            final = part.text  # overwrite — keep last event only

            if output_key:
                session = await runner.session_service.get_session(
                    app_name=runner.app_name,
                    user_id="pipeline",
                    session_id=sid,
                )
                return (session.state.get(output_key) or final).strip()
            return final.strip()

        except Exception as exc:
            msg = str(exc)
            if ("RESOURCE_EXHAUSTED" in msg or "429" in msg) and retry < 4:
                wait = 10 * (2 ** retry)  # 10s, 20s, 40s, 80s
                log.warning("Rate limited on %s; retrying in %ds (attempt %d/5)", session_id, wait, retry + 1)
                await asyncio.sleep(wait)
            else:
                raise


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    state: PipelineState,
    progress_queue: asyncio.Queue,
    resume: bool = False,
) -> PipelineState:
    """
    Execute the full storybook pipeline. Writes progress events to progress_queue.
    Each event is a dict suitable for SSE serialisation.
    """

    async def emit(stage: str, pct: int, **extra):
        await progress_queue.put({"stage": stage, "pct": pct, **extra})

    sid = state.session_id
    cfg = state.config
    age_params = cfg.age_params

    # ── 1-4. Fetch / adapt / split / character bible ──────────────────────────
    # On resume, load completed artifacts from GCS and skip re-running stages.
    bible_dict: dict = {}

    if resume:
        try:
            story = await asyncio.to_thread(gcs.load_adapted_story, sid)
            state.adapted_text = story.get("adapted_text", "")
            await emit("fetching", 10, message="Loaded source from previous run")
            await emit("adapting_text", 35, message="Loaded adapted story from previous run")

            state.pages = await asyncio.to_thread(gcs.load_pages, sid)
            await emit("adapting_text", 35, message=f"Loaded {len(state.pages)} pages")

            bible_dict = await asyncio.to_thread(gcs.load_character_bible, sid)
            state.character_bible = bible_dict  # type: ignore[assignment]
            await emit("building_character_bible", 40, message="Loaded character bible from previous run")
            log.info("Resume: loaded stages 1-4 from GCS for session %s", sid)
        except Exception as exc:
            log.warning("Resume: could not load GCS artifacts (%s) — re-running stages 1-4", exc)
            resume = False  # fall through to fresh execution below

    if not resume:
        # ── 1. Fetch source text ──────────────────────────────────────────────
        await emit("fetching", 5)
        if cfg.source.gutenberg_url:
            state.source_text = await asyncio.to_thread(fetch_gutenberg_url, cfg.source.gutenberg_url)
        else:
            candidates = [
                cfg.source.title,
                cfg.source.author,
                cfg.source.title.split()[0] if cfg.source.title else None,
            ]
            results = []
            for q in filter(None, candidates):
                results = await asyncio.to_thread(search_gutenberg, q)
                if results:
                    break
            if not results:
                raise RuntimeError(f"No Gutenberg results found for: {cfg.source.title!r} / {cfg.source.author!r}")
            state.source_text = await asyncio.to_thread(fetch_gutenberg_url, results[0]["download_url"])
        gcs.write_text(sid, "original", "source_text.txt", content=state.source_text)
        await emit("fetching", 10, message="Source text fetched")

        # ── 2. Adapt + validate text ──────────────────────────────────────────
        # The adapter produces structured JSON: {"pages": [{story_text, page_instructions}]}
        await emit("adapting_text", 15)
        loop_runner = _make_runner(_text_loop)
        state.adapted_text = await _run_agent(
            loop_runner,
            sid,
            json.dumps({
                "source_text": state.source_text,
                "config": cfg.model_dump(),
            }),
            output_key="adapted_text",
        )
        # Parse structured output into StoryPage objects
        adapted_raw = state.adapted_text.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            parsed = json.loads(adapted_raw)
            state.pages = [StoryPage(**p) for p in parsed["pages"]]
        except Exception as exc:
            raise RuntimeError(f"Story adapter returned invalid JSON: {exc}\n\nRaw output:\n{state.adapted_text[:500]}")
        if len(state.pages) != cfg.page_count:
            raise RuntimeError(
                f"Story adapter produced {len(state.pages)} pages but {cfg.page_count} were requested."
            )
        gcs.write_json(sid, "adapted", "story.json", data={
            "title": cfg.source.title or "Untitled",
            "author": cfg.source.author or "Unknown",
            "target_age": cfg.target_age,
            "adapted_text": state.adapted_text,
        })
        for i, page in enumerate(state.pages, 1):
            gcs.write_json(sid, "pages", f"page_{i:02d}.json", data=page.model_dump())
        cover_html = render_cover_html(
            title=cfg.source.title or "A Children's Storybook",
            author=cfg.source.author or "Unknown",
        )
        gcs.write_text(sid, "pages", "cover.html", content=cover_html)
        await emit("adapting_text", 35, message=f"Story adapted and split into {len(state.pages)} pages")

        # ── 4. Build character bible ──────────────────────────────────────────
        await emit("building_character_bible", 36)
        bible_runner = _make_runner(character_bible_agent)
        # Pass assembled story_text (not raw JSON) so the bible builder reads narrative prose
        story_text_for_bible = "\n\n".join(p.story_text for p in state.pages)
        bible_json_str = await _run_agent(
            bible_runner,
            sid,
            json.dumps({
                "adapted_text": story_text_for_bible,
                "config": {"image_spec": cfg.image_spec, "target_age": cfg.target_age},
            }),
        )
        bible_json_str = bible_json_str.strip().removeprefix("```json").removesuffix("```").strip()
        bible_dict = json.loads(bible_json_str)
        gcs.write_json(sid, "character_bible.json", data=bible_dict)
        state.character_bible = bible_dict  # type: ignore[assignment]
        await emit("building_character_bible", 40, message="Character bible built")

    # ── 4.5. Extract layout spec from session instructions ─────────────────────
    # Always runs (cheap LLM call; config is always available regardless of resume).
    layout_runner = _make_runner(html_layout_extractor)
    layout_json_str = await _run_agent(
        layout_runner,
        f"{sid}-layout",
        json.dumps({
            "custom_instructions": cfg.custom_instructions or "",
            "image_spec": cfg.image_spec or "",
            "text_spec": cfg.text_spec or "",
            "total_pages": len(state.pages),
        }),
    )
    layout_json_str = layout_json_str.strip().removeprefix("```json").removesuffix("```").strip()
    layout_spec: dict = json.loads(layout_json_str)
    state.layout_spec = layout_spec
    log.info("Layout spec: %s", layout_spec)

    # ── 5. Generate images per page (parallel) ────────────────────────────────
    total_pages = len(state.pages)
    image_sem = asyncio.Semaphore(settings.image_concurrency)
    llm_sem = asyncio.Semaphore(settings.llm_concurrency)
    # page1_ready lets pages 2+ wait for the reference image before validating
    page1_ready: asyncio.Event = asyncio.Event()
    page1_image: list[bytes] = []  # single-element list so the coroutine can write it
    completed: list[int] = [0]     # mutable counter safe in single-threaded asyncio
    # Maps page_number -> (img_bytes, page_text) once that page finishes successfully.
    # Used to provide the previous page's illustration and text to the validator.
    completed_pages: dict[int, tuple[bytes, str]] = {}

    async def _render_and_verify_html(page_number: int, story_text: str, img_bytes: bytes) -> str:
        """Render page HTML with layout spec and run the verifier loop (up to 2 attempts)."""
        verifier_runner = _make_runner(html_page_verifier)
        page_layouts = layout_spec.get("page_layouts", [])
        per_page_pos = page_layouts[page_number - 1] if page_number <= len(page_layouts) else "top"
        page_layout = {**layout_spec, "image_position": per_page_pos}
        page_html = ""
        for verify_attempt in range(1, 3):
            page_html = render_page_html(page_number, story_text, img_bytes, cfg.target_age, page_layout)
            html_for_verify = re.sub(
                r'src="data:image/[^;]+;base64,[^"]*"',
                'src="[image-omitted]"',
                page_html,
            )
            verify_input = json.dumps({
                "html_code": html_for_verify,
                "layout_spec": page_layout,
                "original_instructions": cfg.custom_instructions or "",
                "page_number": page_number,
            })
            async with llm_sem:
                verify_result = await _run_agent(
                    verifier_runner,
                    f"{sid}-htmlverify-{page_number}-{verify_attempt}",
                    verify_input,
                )
            if "approved" in verify_result.lower():
                break
            log.warning("HTML layout verification failed for page %d (attempt %d): %s",
                        page_number, verify_attempt, verify_result[:200])
            vsession = await verifier_runner.session_service.get_session(
                app_name=verifier_runner.app_name,
                user_id="pipeline",
                session_id=f"{sid}-htmlverify-{page_number}-{verify_attempt}",
            )
            if vsession and vsession.state.get("corrected_image_position"):
                page_layout = {**page_layout, "image_position": vsession.state["corrected_image_position"]}
        return page_html

    async def _process_page(i: int, page: StoryPage) -> tuple[int, bytes]:
        # Resume: reuse image if it was already successfully generated
        if resume and await asyncio.to_thread(gcs.image_exists, sid, i):
            img_bytes = await asyncio.to_thread(gcs.load_image_bytes, sid, i)
            if i == 1:
                page1_image.append(img_bytes)
                page1_ready.set()
            completed_pages[i] = (img_bytes, page.story_text)
            completed[0] += 1
            pct = 40 + int(completed[0] / total_pages * 50)
            await emit("generating_image", pct, page=i, of=total_pages, message="cached")
            log.info("Resume: loaded existing image for page %d", i)
            page_html = await _render_and_verify_html(i, page.story_text, img_bytes)
            await asyncio.to_thread(gcs.write_text, sid, "pages", f"page_{i:02d}.html", content=page_html)
            return i, img_bytes

        await emit("generating_image", 40 + int(completed[0] / total_pages * 50), page=i, of=total_pages)

        page_layouts = layout_spec.get("page_layouts", [])
        target_layout = page_layouts[i - 1] if i <= len(page_layouts) else "top"
        prompt_input = json.dumps({
            "page_text": page.story_text,
            "page_instructions": page.page_instructions,
            "page_number": i,
            "total_pages": total_pages,
            "character_bible": bible_dict,
            "config": {"image_spec": cfg.image_spec},
            "is_first_page": i == 1,
            "target_layout": target_layout,
        })

        prompt_runner = _make_runner(illustration_prompter)
        async with llm_sem:
            image_prompt = await _run_agent(prompt_runner, f"{sid}-prompt-{i}", prompt_input)
        await asyncio.to_thread(gcs.write_text, sid, "prompts", f"page_{i:02d}_prompt.txt", content=image_prompt)

        img_bytes = await _generate_with_retries(
            session_id=sid,
            page_number=i,
            image_prompt=image_prompt,
            page_text=page.story_text,
            page_instructions=page.page_instructions,
            bible_dict=bible_dict,
            image_sem=image_sem,
            llm_sem=llm_sem,
            page1_ready=page1_ready,
            page1_image=page1_image,
            progress_queue=progress_queue,
            completed_pages=completed_pages,
        )

        await asyncio.to_thread(
            gcs.write_bytes, sid, "images", f"page_{i:02d}.png", data=img_bytes, content_type="image/png"
        )
        page_html = await _render_and_verify_html(i, page.story_text, img_bytes)
        await asyncio.to_thread(gcs.write_text, sid, "pages", f"page_{i:02d}.html", content=page_html)

        if i == 1:
            page1_image.append(img_bytes)
            page1_ready.set()

        # Store result before emitting "done" so adjacent pages can use it immediately
        completed_pages[i] = (img_bytes, page.story_text)
        completed[0] += 1
        pct = 40 + int(completed[0] / total_pages * 50)
        await emit("generating_image", pct, page=i, of=total_pages, message="done")
        return i, img_bytes

    tasks = [_process_page(i, page) for i, page in enumerate(state.pages, 1)]
    results: dict[int, bytes] = {i: b for i, b in await asyncio.gather(*tasks)}
    image_bytes_list = [results[i] for i in range(1, total_pages + 1)]

    state.image_gcs_uris = [
        f"gs://{settings.gcs_artifacts_bucket}/sessions/{sid}/images/page_{i:02d}.png"
        for i in range(1, total_pages + 1)
    ]
    state.html_gcs_uris = [
        f"gs://{settings.gcs_artifacts_bucket}/sessions/{sid}/pages/page_{i:02d}.html"
        for i in range(1, total_pages + 1)
    ]

    # ── 6. Compose PDF ────────────────────────────────────────────────────────
    await emit("composing_pdf", 91)
    pdf_bytes = compose_pdf(
        title=cfg.source.title or "A Children's Storybook",
        author=cfg.source.author or "Unknown",
        pages=state.pages,
        image_bytes_list=image_bytes_list,
        target_age=cfg.target_age,
        layout_spec=layout_spec,
    )
    state.pdf_gcs_uri = gcs.write_bytes(
        sid, "final", "storybook.pdf", data=pdf_bytes, content_type="application/pdf"
    )
    await emit("done", 100, session_id=sid)

    state.current_stage = "done"
    state.progress_pct = 100
    return state


def _simplify_prompt(original_prompt: str, bible_dict: dict, attempt: int) -> str:
    """Return a progressively simpler prompt for content-policy retries."""
    style = bible_dict.get("style", "children's book illustration, watercolor")
    if attempt == 1:
        # Strip character descriptions; keep only style + brief scene summary
        first_sentence = original_prompt.split(".")[0].strip()
        return (
            f"{first_sentence}. {style}. "
            "Children's storybook illustration, age-appropriate, cheerful, no violence, no adult content."
        )
    # Final fallback: fully generic scene
    return (
        f"A cheerful children's storybook illustration in the style of: {style}. "
        "A whimsical outdoor scene with soft colors. No text, no people, no faces."
    )


def _placeholder_image(page_number: int) -> bytes:
    """Generate a simple pastel placeholder PNG when all image attempts fail."""
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    colors = ["#F4C2C2", "#C2D4F4", "#C2F4D4", "#F4E8C2", "#E8C2F4"]
    bg = colors[(page_number - 1) % len(colors)]
    img = Image.new("RGB", (768, 768), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 747, 747], outline="#888888", width=3)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    draw.text((384, 384), f"Page {page_number}", fill="#555555", font=font, anchor="mm")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _generate_with_retries(
    session_id: str,
    page_number: int,
    image_prompt: str,
    page_text: str,
    page_instructions: str,
    bible_dict: dict,
    image_sem: asyncio.Semaphore,
    llm_sem: asyncio.Semaphore,
    page1_ready: asyncio.Event,
    page1_image: list[bytes],
    progress_queue: asyncio.Queue,
    completed_pages: dict[int, tuple[bytes, str]],
) -> bytes:
    """Generate an image with up to image_max_retries validation retries.

    image_sem gates concurrent generate_image() calls against Vertex AI quota.
    page1_ready / page1_image let pages 2+ wait for the reference image before
    running validation — the actual generation does not wait.
    completed_pages provides the previous page's illustration for continuity checking
    if it has already finished (best-effort — no waiting).
    Content-policy refusals (NO_IMAGE) are retried with progressively simpler
    prompts before falling back to a placeholder so the pipeline never crashes.
    """
    current_prompt = image_prompt
    validator_runner = _make_runner(image_validator)
    img_bytes: bytes | None = None

    for attempt in range(1, settings.image_max_retries + 2):
        try:
            async with image_sem:
                img_bytes = await asyncio.to_thread(generate_image, current_prompt)
        except (ImageContentPolicyError, ImageTokenLimitError) as exc:
            is_policy = isinstance(exc, ImageContentPolicyError)
            log.warning(
                "%s on page %d attempt %d",
                "Content policy refusal" if is_policy else "Token limit hit",
                page_number, attempt,
            )
            if attempt <= settings.image_max_retries:
                await progress_queue.put({
                    "stage": "image_retry",
                    "pct": 0,
                    "page": page_number,
                    "attempt": attempt,
                    "reason": str(exc),
                })
                # Policy refusals: simplify immediately (same prompt won't help).
                # Token limit: retry same prompt first (may be transient); simplify on attempt 2+.
                if is_policy or attempt > 1:
                    current_prompt = _simplify_prompt(current_prompt, bible_dict, attempt)
            else:
                log.error("All image attempts failed for page %d — using placeholder", page_number)
                return _placeholder_image(page_number)
            continue

        # For pages 2+, wait for page 1's image to be available as a style reference.
        # This is usually a no-op: page 1 goes through the semaphore first and sets
        # page1_ready before any other page finishes generation.
        ref_image: bytes | None = None
        if page_number > 1:
            await page1_ready.wait()
            ref_image = page1_image[0] if page1_image else None
            log.debug("Page %d: reference image %s", page_number, "attached" if ref_image else "unavailable")

        # Previous page context for continuity — best-effort, no waiting.
        prev_result = completed_pages.get(page_number - 1) if page_number > 1 else None
        prev_page_image_bytes = prev_result[0] if prev_result else None
        prev_page_text_str = prev_result[1] if prev_result else None
        if page_number > 1:
            log.debug(
                "Page %d: previous page context %s",
                page_number,
                "attached" if prev_page_image_bytes else "not yet available",
            )

        validate_input = json.dumps({
            "image_prompt": current_prompt,
            "page_text": page_text,
            "page_instructions": page_instructions,
            "character_bible": bible_dict,
            "page_number": page_number,
            "prev_page_text": prev_page_text_str,
        })
        async with llm_sem:
            result = await _run_agent(
                validator_runner,
                f"{session_id}-imgval-{page_number}-{attempt}",
                validate_input,
                subject_image=img_bytes,
                reference_image=ref_image,
                prev_page_image=prev_page_image_bytes,
            )

        if "approved" in result.lower():
            return img_bytes

        log.warning("Image validation attempt %d failed for page %d", attempt, page_number)

        if attempt <= settings.image_max_retries:
            await progress_queue.put({
                "stage": "image_retry",
                "pct": 0,
                "page": page_number,
                "attempt": attempt,
                "reason": result[:200],
            })
            session = await validator_runner.session_service.get_session(
                app_name=validator_runner.app_name,
                user_id="pipeline",
                session_id=f"{session_id}-imgval-{page_number}-{attempt}",
            )
            if session and session.state.get("revised_image_prompt"):
                current_prompt = session.state["revised_image_prompt"]

    log.error("Exhausted image retries for page %d — using last generated image", page_number)
    return img_bytes if img_bytes is not None else _placeholder_image(page_number)
