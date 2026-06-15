"""
Main pipeline orchestrator.

Architecture:
  Workflow (sequential):
    1. literature_fetcher  — fetches source text via URL or Gutenberg search
    2. adapt_validate_loop — LoopAgent: story_adapter → text_validator (up to 4 retries)
    3. page_splitter_step  — deterministic function splits text into N pages
    4. character_bible     — builds visual consistency doc
    5. image_loop_step     — Python function: for each page, run prompt→generate→validate
    6. pdf_step            — composes final PDF, uploads to GCS, returns signed URL

Progress events are written to an asyncio.Queue for SSE streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from storybook.agents.character_bible import character_bible_agent
from storybook.agents.image_generator import generate_image
from storybook.agents.image_validator import image_validator
from storybook.agents.illustration_prompter import illustration_prompter
from storybook.agents.page_splitter import split_into_pages
from storybook.agents.pdf_compositor import compose_pdf
from storybook.agents.story_adapter import story_adapter
from storybook.agents.text_validator import text_validator
from storybook.config import settings
from storybook.models import PipelineState, SessionConfig
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


async def _run_agent(runner: Runner, session_id: str, message: str, output_key: str | None = None) -> str:
    """Run an ADK agent and return the final text response.

    If output_key is provided, the return value is read from ADK session state
    rather than the streamed response — use this when a LoopAgent contains
    multiple sub-agents whose final responses would otherwise be concatenated.
    """
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="pipeline",
        session_id=session_id,
    )
    final = ""
    async for event in runner.run_async(
        session_id=session_id,
        user_id="pipeline",
        new_message=types.Content(parts=[types.Part(text=message)]),
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    final = part.text  # overwrite — keep last event only
    if output_key:
        session = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id="pipeline",
            session_id=session_id,
        )
        return (session.state.get(output_key) or final).strip()
    return final.strip()


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    state: PipelineState,
    progress_queue: asyncio.Queue,
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

    # ── 1. Fetch source text ──────────────────────────────────────────────────
    # Plain function — no LLM needed, and passing a full book through a model
    # context just to echo it back causes timeouts.
    await emit("fetching", 5)
    if cfg.source.gutenberg_url:
        state.source_text = await asyncio.to_thread(fetch_gutenberg_url, cfg.source.gutenberg_url)
    else:
        # Try progressively looser queries — combined rarely works on gutendex
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

    # ── 2. Adapt + validate text ──────────────────────────────────────────────
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
    gcs.write_json(sid, "adapted", "story.json", data={
        "title": cfg.source.title or "Untitled",
        "author": cfg.source.author or "Unknown",
        "target_age": cfg.target_age,
        "adapted_text": state.adapted_text,
    })
    await emit("adapting_text", 30, message="Story adapted and validated")

    # ── 3. Split into pages ───────────────────────────────────────────────────
    await emit("splitting_pages", 32)
    state.pages = split_into_pages(
        state.adapted_text,
        cfg.page_count,
        age_params["max_words_per_page"],
    )
    for i, page_text in enumerate(state.pages, 1):
        gcs.write_text(sid, "pages", f"page_{i:02d}.txt", content=page_text)
    await emit("splitting_pages", 35, message=f"Split into {len(state.pages)} pages")

    # ── 4. Build character bible ──────────────────────────────────────────────
    await emit("building_character_bible", 36)
    bible_runner = _make_runner(character_bible_agent)
    bible_json_str = await _run_agent(
        bible_runner,
        sid,
        json.dumps({
            "adapted_text": state.adapted_text,
            "config": {"image_spec": cfg.image_spec, "target_age": cfg.target_age},
        }),
    )
    # Strip markdown code fences if the model wrapped the JSON
    bible_json_str = bible_json_str.strip().removeprefix("```json").removesuffix("```").strip()
    bible_dict = json.loads(bible_json_str)
    gcs.write_json(sid, "character_bible.json", data=bible_dict)
    state.character_bible = bible_dict  # type: ignore[assignment]
    await emit("building_character_bible", 40, message="Character bible built")

    # ── 5. Generate images per page ───────────────────────────────────────────
    total_pages = len(state.pages)
    image_bytes_list: list[bytes] = []
    first_page_image_bytes: bytes | None = None

    for i, page_text in enumerate(state.pages, 1):
        page_pct_start = 40 + int((i - 1) / total_pages * 50)
        page_pct_end = 40 + int(i / total_pages * 50)
        await emit("generating_image", page_pct_start, page=i, of=total_pages)

        prompt_input = json.dumps({
            "page_text": page_text,
            "page_number": i,
            "total_pages": total_pages,
            "character_bible": bible_dict,
            "config": {"image_spec": cfg.image_spec},
            "is_first_page": i == 1,
        })

        # Generate prompt
        prompt_runner = _make_runner(illustration_prompter)
        image_prompt = await _run_agent(prompt_runner, f"{sid}-prompt-{i}", prompt_input)
        gcs.write_text(sid, "prompts", f"page_{i:02d}_prompt.txt", content=image_prompt)

        # Generate image with retries
        img_bytes = await _generate_with_retries(
            session_id=sid,
            page_number=i,
            image_prompt=image_prompt,
            page_text=page_text,
            bible_dict=bible_dict,
            reference_image=first_page_image_bytes,
            progress_queue=progress_queue,
        )

        gcs.write_bytes(sid, "images", f"page_{i:02d}.png", data=img_bytes, content_type="image/png")
        image_bytes_list.append(img_bytes)

        if i == 1:
            first_page_image_bytes = img_bytes

        await emit("generating_image", page_pct_end, page=i, of=total_pages, message="done")

    state.image_gcs_uris = [
        f"gs://{settings.gcs_artifacts_bucket}/sessions/{sid}/images/page_{i:02d}.png"
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
    )
    state.pdf_gcs_uri = gcs.write_bytes(
        sid, "final", "storybook.pdf", data=pdf_bytes, content_type="application/pdf"
    )
    await emit("done", 100, session_id=sid)

    state.current_stage = "done"
    state.progress_pct = 100
    return state


async def _generate_with_retries(
    session_id: str,
    page_number: int,
    image_prompt: str,
    page_text: str,
    bible_dict: dict,
    reference_image: bytes | None,
    progress_queue: asyncio.Queue,
) -> bytes:
    """Generate an image with up to image_max_retries validation retries."""
    current_prompt = image_prompt
    validator_runner = _make_runner(image_validator)

    for attempt in range(1, settings.image_max_retries + 2):
        img_bytes = await asyncio.to_thread(generate_image, current_prompt)

        # Validate — pass reference image on pages > 1
        validate_input = json.dumps({
            "image_prompt": current_prompt,
            "page_text": page_text,
            "character_bible": bible_dict,
            "page_number": page_number,
            "reference_image_available": reference_image is not None,
        })
        # TODO: attach reference_image bytes as multimodal part when ADK runner supports it
        result = await _run_agent(
            validator_runner, f"{session_id}-imgval-{page_number}-{attempt}", validate_input
        )

        if "approved" in result.lower():
            return img_bytes

        if attempt <= settings.image_max_retries:
            await progress_queue.put({
                "stage": "image_retry",
                "pct": 0,
                "page": page_number,
                "attempt": attempt,
                "reason": result[:200],
            })
            # Pick up revised prompt from ADK session state (written by reject_image tool)
            session = await validator_runner.session_service.get_session(
                app_name=validator_runner.app_name,
                user_id="pipeline",
                session_id=f"{session_id}-imgval-{page_number}-{attempt}",
            )
            if session and session.state.get("revised_image_prompt"):
                current_prompt = session.state["revised_image_prompt"]

        log.warning("Image validation attempt %d failed for page %d", attempt, page_number)

    # Return last attempt even if validation didn't pass (we've exhausted retries)
    log.error("Exhausted image retries for page %d — using last generated image", page_number)
    return img_bytes
