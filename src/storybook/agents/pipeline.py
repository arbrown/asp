"""
Main pipeline orchestrator.

Architecture:
  Workflow (sequential):
    1. literature_fetcher   — fetches source text via URL or Gutenberg search
    2. adapt_validate_loop  — LoopAgent: story_adapter → text_validator
                              Adapter returns JSON: {spreads: [{spread_number, verso_text, ...}]}
    3. character_bible      — builds visual consistency doc from spread text
    4. spread_planner       — plans illustration coverage, aspect ratios, and global typography
    5. image_spread_step    — Python: for each spread, run prompt→generate→validate per image,
                              then render spread HTML and run layout verifier
    6. pdf_step             — composes wide PDF (17×11) + publishing PDF (8.5×11), uploads to GCS

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
from storybook.agents.html_page_verifier import html_page_verifier
from storybook.agents.image_generator import ImageContentPolicyError, ImageTokenLimitError, generate_image
from storybook.agents.image_validator import image_validator
from storybook.agents.illustration_prompter import illustration_prompter
from storybook.agents.pdf_compositor import (
    _build_spread_context,
    compose_spread_pdf_publishing,
    compose_spread_pdf_wide,
    render_cover_html,
    render_spread_html,
)
from storybook.agents.spread_planner import spread_planner
from storybook.agents.story_adapter import story_adapter
from storybook.agents.text_validator import text_validator
from storybook.config import settings
from storybook.models import IllustrationEntry, PipelineState, SessionConfig, SpreadContent, SpreadPlan
from storybook.tools import gcs
from storybook.tools.gutenberg import fetch_gutenberg_url, search_gutenberg

log = logging.getLogger(__name__)

# ── ADK sub-agents ────────────────────────────────────────────────────────────

_text_loop = LoopAgent(
    name="text_adapt_validate",
    sub_agents=[story_adapter, text_validator],
    max_iterations=settings.text_max_retries + 1,
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
    prev_spread_image: bytes | None = None,
) -> str:
    """Run an ADK agent and return the final text response.

    If output_key is provided, the return value is read from ADK session state.
    subject_image: the image being evaluated.
    reference_image: spread 0/1's image for style comparison.
    prev_spread_image: the previous spread's illustration for continuity checking.

    Retries with exponential backoff on 429 RESOURCE_EXHAUSTED errors.
    """
    parts: list[types.Part] = [types.Part(text=message)]
    if subject_image:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=subject_image)))
    if reference_image:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=reference_image)))
    if prev_spread_image:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=prev_spread_image)))

    for retry in range(5):
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
                            final = part.text

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
                wait = 10 * (2 ** retry)
                log.warning("Rate limited on %s; retrying in %ds (attempt %d/5)", session_id, wait, retry + 1)
                await asyncio.sleep(wait)
            else:
                raise


# ── Spread numbering ──────────────────────────────────────────────────────────

def _compute_spreads_meta(page_count: int) -> tuple[int, list[dict]]:
    """Compute total spread count and per-spread metadata from page_count."""
    spread_count = page_count // 2 + 1
    spreads_meta = []
    for s in range(spread_count):
        if s == 0:
            spreads_meta.append({"spread_number": 0, "has_verso": False, "has_recto": True})
        else:
            verso_page = s * 2
            recto_page = s * 2 + 1
            spreads_meta.append({
                "spread_number": s,
                "has_verso": verso_page <= page_count,
                "has_recto": recto_page <= page_count,
            })
    return spread_count, spreads_meta


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    state: PipelineState,
    progress_queue: asyncio.Queue,
    resume: bool = False,
) -> PipelineState:
    """Execute the full storybook pipeline. Writes progress events to progress_queue."""
    from storybook.tracing import get_tracer, make_trace_url

    tracer = get_tracer()
    with tracer.start_as_current_span(
        "pipeline.run",
        attributes={
            "session.id": state.session_id,
            "config.target_age": state.config.target_age,
            "config.page_count": state.config.page_count,
        },
    ) as root_span:
        ctx = root_span.get_span_context()
        if ctx.is_valid:
            state.trace_url = make_trace_url(ctx.trace_id)
        return await _run_pipeline(state, progress_queue, resume)


async def _run_pipeline(
    state: PipelineState,
    progress_queue: asyncio.Queue,
    resume: bool = False,
) -> PipelineState:

    async def emit(stage: str, pct: int, **extra):
        await progress_queue.put({"stage": stage, "pct": pct, **extra})

    sid = state.session_id
    cfg = state.config

    spread_count, spreads_meta = _compute_spreads_meta(cfg.page_count)

    bible_dict: dict = {}

    if resume:
        try:
            story = await asyncio.to_thread(gcs.load_adapted_story, sid)
            state.adapted_text = story.get("adapted_text", "")
            await emit("fetching", 10, message="Loaded source from previous run")
            await emit("adapting_text", 35, message="Loaded adapted story from previous run")

            spread_contents = await asyncio.to_thread(gcs.load_spread_contents, sid)
            state.spread_contents = spread_contents
            await emit("adapting_text", 35, message=f"Loaded {len(spread_contents)} spreads")

            bible_dict = await asyncio.to_thread(gcs.load_character_bible, sid)
            state.character_bible = bible_dict  # type: ignore[assignment]
            await emit("building_character_bible", 40, message="Loaded character bible from previous run")
            log.info("Resume: loaded stages 1-4 from GCS for session %s", sid)
        except Exception as exc:
            log.warning("Resume: could not load GCS artifacts (%s) — re-running stages 1-4", exc)
            resume = False

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
                raise RuntimeError(
                    f"No Gutenberg results found for: {cfg.source.title!r} / {cfg.source.author!r}"
                )
            state.source_text = await asyncio.to_thread(fetch_gutenberg_url, results[0]["download_url"])
        gcs.write_text(sid, "original", "source_text.txt", content=state.source_text)
        await emit("fetching", 10, message="Source text fetched")

        # ── 2. Adapt + validate text (per-spread output) ──────────────────────
        await emit("adapting_text", 15)
        loop_runner = _make_runner(_text_loop)
        adapter_config = {
            **cfg.model_dump(),
            "spread_count": spread_count,
            "spreads_meta": spreads_meta,
        }
        state.adapted_text = await _run_agent(
            loop_runner,
            sid,
            json.dumps({
                "source_text": state.source_text,
                "config": adapter_config,
            }),
            output_key="adapted_text",
        )
        adapted_raw = state.adapted_text.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            parsed = json.loads(adapted_raw)
            state.spread_contents = [SpreadContent(**s) for s in parsed["spreads"]]
        except Exception as exc:
            raise RuntimeError(
                f"Story adapter returned invalid JSON: {exc}\n\nRaw output:\n{state.adapted_text[:500]}"
            )
        if len(state.spread_contents) != spread_count:
            raise RuntimeError(
                f"Story adapter produced {len(state.spread_contents)} spreads "
                f"but {spread_count} were requested."
            )
        gcs.write_json(sid, "adapted", "story.json", data={
            "title": cfg.source.title or "Untitled",
            "author": cfg.source.author or "Unknown",
            "target_age": cfg.target_age,
            "adapted_text": state.adapted_text,
        })
        for sc in state.spread_contents:
            gcs.write_json(sid, "spreads", f"spread_{sc.spread_number:02d}.json", data=sc.model_dump())

        cover_html = render_cover_html(
            title=cfg.source.title or "A Children's Storybook",
            author=cfg.source.author or "Unknown",
        )
        gcs.write_text(sid, "pages", "cover.html", content=cover_html)
        await emit("adapting_text", 35, message=f"Story adapted into {spread_count} spreads")

        # ── 3. Build character bible ──────────────────────────────────────────
        await emit("building_character_bible", 36)
        bible_runner = _make_runner(character_bible_agent)
        all_text_parts = []
        for sc in state.spread_contents:
            if sc.verso_text:
                all_text_parts.append(sc.verso_text)
            if sc.recto_text:
                all_text_parts.append(sc.recto_text)
        story_text_for_bible = "\n\n".join(all_text_parts)
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

    # ── 4. Spread planner — illustration coverage + global typography ──────────
    await emit("planning_spreads", 41)
    planner_runner = _make_runner(spread_planner)
    planner_json_str = await _run_agent(
        planner_runner,
        f"{sid}-planner",
        json.dumps({
            "spreads": [sc.model_dump() for sc in state.spread_contents],
            "config": {
                "image_spec": cfg.image_spec or "",
                "custom_instructions": cfg.custom_instructions or "",
                "text_spec": cfg.text_spec or "",
            },
        }),
    )
    planner_json_str = planner_json_str.strip().removeprefix("```json").removesuffix("```").strip()
    planner_output = json.loads(planner_json_str)

    layout_spec = {
        "font_family": planner_output.get("font_family", "Georgia, serif"),
        "background_color": planner_output.get("background_color", "#fffdf7"),
        "text_color": planner_output.get("text_color", "#1a1a1a"),
        "accent_color": planner_output.get("accent_color", "#2c1a0e"),
        "layout_notes": planner_output.get("layout_notes", ""),
    }
    state.layout_spec = layout_spec

    spread_plans_raw = planner_output.get("spreads", [])
    state.spread_plans = [
        SpreadPlan(
            spread_number=sp["spread_number"],
            illustration_plan=[IllustrationEntry(**e) for e in sp.get("illustration_plan", [])],
            text_treatment=sp.get("text_treatment", "gradient_dark"),
            text_position=sp.get("text_position", "bottom"),
        )
        for sp in spread_plans_raw
    ]
    plan_by_spread = {sp.spread_number: sp for sp in state.spread_plans}

    log.info("Spread planner: %s", layout_spec)
    await emit("planning_spreads", 43, message="Spread plan ready")

    # ── 5. Generate images per spread (parallel) ──────────────────────────────
    total_spreads = len(state.spread_contents)
    image_sem = asyncio.Semaphore(settings.image_concurrency)
    llm_sem = asyncio.Semaphore(settings.llm_concurrency)
    ref_ready: asyncio.Event = asyncio.Event()
    ref_image: list[bytes] = []
    completed: list[int] = [0]
    # Maps spread_number -> first image bytes for continuity checking
    completed_spread_images: dict[int, bytes] = {}

    async def _render_and_verify_spread(
        spread_number: int,
        spread_content: SpreadContent,
        illustration_plan: list[IllustrationEntry],
        image_bytes_by_index: dict[int, bytes],
    ) -> str:
        plan = plan_by_spread.get(spread_number, SpreadPlan(spread_number=spread_number))
        base_treatment = plan.text_treatment
        base_position = plan.text_position
        verifier_runner = _make_runner(html_page_verifier)
        spread_html = ""
        accumulated_overrides: dict = {}
        # Pass the first image to verifier so it can judge actual legibility
        primary_img_bytes = image_bytes_by_index.get(0) or image_bytes_by_index.get(1)
        secondary_img_bytes = image_bytes_by_index.get(1) if 0 in image_bytes_by_index else None

        for verify_attempt in range(1, 3):
            applied_treatment = accumulated_overrides.get("text_treatment", base_treatment)
            spread_html = render_spread_html(
                spread_number=spread_number,
                verso_text=spread_content.verso_text,
                recto_text=spread_content.recto_text,
                illustration_plan=[e.model_dump() for e in illustration_plan],
                image_bytes_by_index=image_bytes_by_index,
                layout_spec=layout_spec,
                target_age=cfg.target_age,
                css_overrides=accumulated_overrides if accumulated_overrides else None,
                text_treatment=applied_treatment,
                text_position=base_position,
            )
            html_for_verify = re.sub(
                r'src="data:image/[^;]+;base64,[^"]*"',
                'src="[image-omitted]"',
                spread_html,
            )
            verify_input = json.dumps({
                "html_code": html_for_verify,
                "illustration_plan": [e.model_dump() for e in illustration_plan],
                "verso_text": spread_content.verso_text,
                "recto_text": spread_content.recto_text,
                "spread_number": spread_number,
            })
            async with llm_sem:
                verify_result = await _run_agent(
                    verifier_runner,
                    f"{sid}-htmlverify-{spread_number}-{verify_attempt}",
                    verify_input,
                    subject_image=primary_img_bytes,
                    reference_image=secondary_img_bytes,
                )
            if "approved" in verify_result.lower():
                break
            log.warning(
                "Spread HTML layout verification failed for spread %d (attempt %d): %s",
                spread_number, verify_attempt, verify_result[:200],
            )
            vsession = await verifier_runner.session_service.get_session(
                app_name=verifier_runner.app_name,
                user_id="pipeline",
                session_id=f"{sid}-htmlverify-{spread_number}-{verify_attempt}",
            )
            if vsession:
                new_overrides = vsession.state.get("layout_css_overrides") or {}
                if isinstance(new_overrides, dict):
                    accumulated_overrides.update(new_overrides)
                    log.info(
                        "Spread %d verifier feedback: %s → applying overrides: %s",
                        spread_number,
                        vsession.state.get("layout_feedback", "")[:120],
                        accumulated_overrides,
                    )
        return spread_html

    async def _process_spread(spread_content: SpreadContent) -> tuple[int, dict[int, bytes]]:
        s = spread_content.spread_number
        plan = plan_by_spread.get(s, SpreadPlan(spread_number=s))
        illustration_plan = plan.illustration_plan

        image_bytes_by_index: dict[int, bytes] = {}

        for entry in illustration_plan:
            img_idx = entry.image_index

            if resume and await asyncio.to_thread(gcs.spread_image_exists, sid, s, img_idx):
                img_bytes = await asyncio.to_thread(gcs.load_spread_image_bytes, sid, s, img_idx)
                image_bytes_by_index[img_idx] = img_bytes
                if not ref_image:
                    ref_image.append(img_bytes)
                    ref_ready.set()
                log.info("Resume: loaded existing image for spread %d img %d", s, img_idx)
                continue

            await emit("generating_image", 43 + int(completed[0] / total_spreads * 48),
                       spread=s, of=total_spreads)

            is_first = not ref_image
            prompt_input = json.dumps({
                "verso_text": spread_content.verso_text,
                "recto_text": spread_content.recto_text,
                "verso_instructions": spread_content.verso_instructions,
                "recto_instructions": spread_content.recto_instructions,
                "spread_number": s,
                "total_spreads": total_spreads,
                "character_bible": bible_dict,
                "config": {"image_spec": cfg.image_spec},
                "coverage": entry.coverage,
                "aspect_ratio": entry.aspect_ratio,
                "illustration_notes": entry.illustration_notes,
                "is_first_spread": is_first,
            })

            prompt_runner = _make_runner(illustration_prompter)
            async with llm_sem:
                image_prompt = await _run_agent(
                    prompt_runner, f"{sid}-prompt-{s}-{img_idx}", prompt_input,
                    output_key="image_prompt",
                )
            await asyncio.to_thread(
                gcs.write_text, sid, "prompts", f"spread_{s:02d}_img{img_idx}_prompt.txt",
                content=image_prompt,
            )

            img_bytes = await _generate_with_retries(
                session_id=sid,
                spread_number=s,
                image_index=img_idx,
                image_prompt=image_prompt,
                spread_content=spread_content,
                illustration_entry=entry,
                bible_dict=bible_dict,
                image_sem=image_sem,
                llm_sem=llm_sem,
                ref_ready=ref_ready,
                ref_image=ref_image,
                progress_queue=progress_queue,
                completed_spread_images=completed_spread_images,
            )

            await asyncio.to_thread(
                gcs.write_bytes, sid, "images", f"spread_{s:02d}_img{img_idx}.png",
                data=img_bytes, content_type="image/png",
            )
            image_bytes_by_index[img_idx] = img_bytes

            if not ref_image:
                ref_image.append(img_bytes)
                ref_ready.set()

        completed_spread_images[s] = image_bytes_by_index.get(0, b"")
        completed[0] += 1
        pct = 43 + int(completed[0] / total_spreads * 48)
        await emit("generating_image", pct, spread=s, of=total_spreads, message="done")

        spread_html = await _render_and_verify_spread(
            s, spread_content, illustration_plan, image_bytes_by_index
        )
        await asyncio.to_thread(
            gcs.write_text, sid, "spreads", f"spread_{s:02d}.html", content=spread_html
        )

        return s, image_bytes_by_index

    tasks = [_process_spread(sc) for sc in state.spread_contents]
    results: dict[int, dict[int, bytes]] = {s: imgs for s, imgs in await asyncio.gather(*tasks)}

    state.html_gcs_uris = [
        f"gs://{settings.gcs_artifacts_bucket}/sessions/{sid}/spreads/spread_{s:02d}.html"
        for s in range(total_spreads)
    ]
    state.image_gcs_uris = [
        f"gs://{settings.gcs_artifacts_bucket}/sessions/{sid}/images/spread_{s:02d}_img0.png"
        for s in range(total_spreads)
        if results.get(s)
    ]

    # ── 6. Compose PDFs ───────────────────────────────────────────────────────
    await emit("composing_pdf", 92)

    spread_contexts = []
    for sc in state.spread_contents:
        s = sc.spread_number
        plan = plan_by_spread.get(s, SpreadPlan(spread_number=s))
        font_size = {"4-5": 20, "6-8": 16, "9-12": 13}.get(cfg.target_age, 16)
        ctx = _build_spread_context(
            spread_number=s,
            verso_text=sc.verso_text,
            recto_text=sc.recto_text,
            illustration_plan=[e.model_dump() for e in plan.illustration_plan],
            image_bytes_by_index=results.get(s, {}),
            layout_spec=layout_spec,
            font_size=font_size,
            text_treatment=plan.text_treatment,
            text_position=plan.text_position,
        )
        spread_contexts.append(ctx)

    title = cfg.source.title or "A Children's Storybook"
    author = cfg.source.author or "Unknown"

    wide_pdf = compose_spread_pdf_wide(
        title=title,
        author=author,
        spread_contexts=spread_contexts,
        layout_spec=layout_spec,
        target_age=cfg.target_age,
    )
    state.wide_pdf_gcs_uri = gcs.write_bytes(
        sid, "final", "storybook_wide.pdf", data=wide_pdf, content_type="application/pdf"
    )

    publishing_pdf = compose_spread_pdf_publishing(
        title=title,
        author=author,
        spread_contexts=spread_contexts,
        layout_spec=layout_spec,
        target_age=cfg.target_age,
    )
    state.pdf_gcs_uri = gcs.write_bytes(
        sid, "final", "storybook.pdf", data=publishing_pdf, content_type="application/pdf"
    )

    await emit("done", 100, session_id=sid)
    state.current_stage = "done"
    state.progress_pct = 100
    return state


# ── Image generation helpers ──────────────────────────────────────────────────

def _simplify_prompt(original_prompt: str, bible_dict: dict, attempt: int) -> str:
    """Return a progressively simpler prompt for content-policy retries."""
    style = bible_dict.get("style", "children's book illustration, watercolor")
    if attempt == 1:
        first_sentence = original_prompt.split(".")[0].strip()
        return (
            f"{first_sentence}. {style}. "
            "Children's storybook illustration, age-appropriate, cheerful, no violence, no adult content."
        )
    return (
        f"A cheerful children's storybook illustration in the style of: {style}. "
        "A whimsical outdoor scene with soft colors. No text, no people, no faces."
    )


def _placeholder_image(label: str) -> bytes:
    """Generate a simple pastel placeholder PNG when all image attempts fail."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    colors = ["#F4C2C2", "#C2D4F4", "#C2F4D4", "#F4E8C2", "#E8C2F4"]
    h = hash(label) % len(colors)
    img = Image.new("RGB", (768, 768), colors[h])
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 747, 747], outline="#888888", width=3)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    draw.text((384, 384), label, fill="#555555", font=font, anchor="mm")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _generate_with_retries(
    session_id: str,
    spread_number: int,
    image_index: int,
    image_prompt: str,
    spread_content: SpreadContent,
    illustration_entry: IllustrationEntry,
    bible_dict: dict,
    image_sem: asyncio.Semaphore,
    llm_sem: asyncio.Semaphore,
    ref_ready: asyncio.Event,
    ref_image: list[bytes],
    progress_queue: asyncio.Queue,
    completed_spread_images: dict[int, bytes],
) -> bytes:
    """Generate an image for a spread with validation retries.

    image_sem gates concurrent Vertex AI calls.
    ref_ready / ref_image let later spreads wait for a style reference.
    Content-policy refusals are retried with progressively simpler prompts;
    if all attempts fail, a placeholder PNG is returned.
    """
    current_prompt = image_prompt
    validator_runner = _make_runner(image_validator)
    img_bytes: bytes | None = None

    for attempt in range(1, settings.image_max_retries + 2):
        try:
            async with image_sem:
                img_bytes = await asyncio.to_thread(
                    generate_image, current_prompt, illustration_entry.aspect_ratio
                )
        except (ImageContentPolicyError, ImageTokenLimitError) as exc:
            is_policy = isinstance(exc, ImageContentPolicyError)
            log.warning(
                "%s on spread %d img %d attempt %d",
                "Content policy refusal" if is_policy else "Token limit hit",
                spread_number, image_index, attempt,
            )
            if attempt <= settings.image_max_retries:
                await progress_queue.put({
                    "stage": "image_retry",
                    "pct": 0,
                    "spread": spread_number,
                    "attempt": attempt,
                    "reason": str(exc),
                })
                if is_policy or attempt > 1:
                    current_prompt = _simplify_prompt(current_prompt, bible_dict, attempt)
            else:
                log.error(
                    "All image attempts failed for spread %d img %d — using placeholder",
                    spread_number, image_index,
                )
                return _placeholder_image(f"Spread {spread_number}")
            continue

        style_ref: bytes | None = None
        if spread_number > 0 or image_index > 0:
            await ref_ready.wait()
            style_ref = ref_image[0] if ref_image else None

        prev_img: bytes | None = completed_spread_images.get(spread_number - 1)

        validate_input = json.dumps({
            "image_prompt": current_prompt,
            "verso_text": spread_content.verso_text,
            "recto_text": spread_content.recto_text,
            "verso_instructions": spread_content.verso_instructions,
            "recto_instructions": spread_content.recto_instructions,
            "illustration_notes": illustration_entry.illustration_notes,
            "coverage": illustration_entry.coverage,
            "character_bible": bible_dict,
            "spread_number": spread_number,
        })
        async with llm_sem:
            result = await _run_agent(
                validator_runner,
                f"{session_id}-imgval-{spread_number}-{image_index}-{attempt}",
                validate_input,
                subject_image=img_bytes,
                reference_image=style_ref,
                prev_spread_image=prev_img,
            )

        if "approved" in result.lower():
            return img_bytes

        log.warning("Image validation attempt %d failed for spread %d img %d", attempt, spread_number, image_index)

        if attempt <= settings.image_max_retries:
            await progress_queue.put({
                "stage": "image_retry",
                "pct": 0,
                "spread": spread_number,
                "attempt": attempt,
                "reason": result[:200],
            })
            session = await validator_runner.session_service.get_session(
                app_name=validator_runner.app_name,
                user_id="pipeline",
                session_id=f"{session_id}-imgval-{spread_number}-{image_index}-{attempt}",
            )
            if session and session.state.get("revised_image_prompt"):
                current_prompt = session.state["revised_image_prompt"]

    log.error(
        "Exhausted image retries for spread %d img %d — using last generated image",
        spread_number, image_index,
    )
    return img_bytes if img_bytes is not None else _placeholder_image(f"Spread {spread_number}")
