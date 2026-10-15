"""A2UI event sink, asset URL helpers, and A2A SSE stream generator for Gemini Enterprise (WASD)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from storybook.a2a.a2ui import (
    STAGE_LABELS,
    build_download_chip_update,
    build_message_parts,
    build_progress_surface_init,
    build_spread_card_update,
    build_stage_update,
    format_breadcrumb_line,
)
from storybook.config import settings
from storybook.events import EventSink
from storybook.substrate import ate
from storybook.tools import gcs

log = logging.getLogger(__name__)

DEFAULT_PUBLIC_BASE_URL = "https://pushkin-agent.duckdns.org"

_VALID_IMAGE_RE = re.compile(r"^(reference|page_\d+|spread_\d+(?:_img\d+)?)\.png$")


def _normalize_base_url(base_url: str = "") -> str:
    prefix = (base_url or DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    if prefix.endswith("/a2a"):
        prefix = prefix[:-4]
    if "pushkin-agent.duckdns.org" in prefix and prefix.startswith("http://"):
        prefix = "https://" + prefix[len("http://") :]
    return prefix


def get_public_asset_url(
    session_id: str,
    asset_type: str,
    filename: str = "",
    base_url: str = "",
) -> str:
    """Return the direct FastAPI proxy URL for a session PDF or spread image."""
    prefix = _normalize_base_url(base_url)
    if asset_type.lower() == "pdf":
        return f"{prefix}/a2a/sessions/{session_id}/pdf"
    fname = filename or "spread_00_img0.png"
    return f"{prefix}/a2a/sessions/{session_id}/images/{fname}"


def get_signed_or_api_url(
    session_id: str,
    asset_type: str,
    filename: str = "",
    base_url: str = "",
) -> str:
    """Return a V4 signed GCS URL for a session asset, or fall back to the FastAPI proxy URL."""
    prefix = _normalize_base_url(base_url)
    is_pdf = asset_type.lower() == "pdf"
    if is_pdf:
        blob_parts = ("final", filename or "storybook.pdf")
        fallback_path = f"{prefix}/a2a/sessions/{session_id}/pdf"
    else:
        fname = filename or "spread_00_img0.png"
        blob_parts = ("images", fname)
        fallback_path = f"{prefix}/a2a/sessions/{session_id}/images/{fname}"

    try:
        bucket = gcs._bucket()
        blob_name = gcs._blob_path(session_id, *blob_parts)
        blob = bucket.blob(blob_name)
        sign_kwargs: dict[str, Any] = {
            "version": "v4",
            "expiration": timedelta(hours=1),
            "method": "GET",
        }
        if is_pdf:
            sign_kwargs["response_disposition"] = f'attachment; filename="storybook-{session_id}.pdf"'  # noqa: E501
            sign_kwargs["response_type"] = "application/pdf"
        signed = blob.generate_signed_url(**sign_kwargs)
        if isinstance(signed, str) and signed.startswith("http"):
            return signed
    except Exception as exc:
        log.debug("Falling back to API route URL for %s/%s: %s", session_id, asset_type, exc)

    return fallback_path


def make_task_status_event(
    task_id: str,
    state: str,
    final: bool = False,
    text: str | None = None,
    envelopes: list[dict[str, Any]] | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Create an A2A TaskStatusUpdateEvent payload with WASD-compatible A2UI parts."""
    status_obj: dict[str, Any] = {
        "state": state,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if text is not None or envelopes:
        parts = build_message_parts(text or "", envelopes or [])
        status_obj["message"] = {
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "parts": parts,
        }

    return {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": context_id or task_id,
        "status": status_obj,
        "final": final,
    }


def make_task_artifact_event(
    task_id: str,
    text: str,
    envelopes: list[dict[str, Any]],
    context_id: str | None = None,
    artifact_id: str | None = None,
    name: str | None = None,
    last_chunk: bool = True,
) -> dict[str, Any]:
    """Create an A2A TaskArtifactUpdateEvent payload with WASD-compatible A2UI parts."""
    return {
        "kind": "artifact-update",
        "taskId": task_id,
        "contextId": context_id or task_id,
        "artifact": {
            "artifactId": artifact_id or f"storybook-{task_id}",
            "name": name or "Storybook Output",
            "parts": build_message_parts(text, envelopes),
        },
        "lastChunk": last_chunk,
    }


def _parse_spread_index_from_filename(filename: str, default_idx: int = 1) -> int:
    """Parse a 1-based display spread index from a GCS image filename (0 for reference.png)."""
    if filename == "reference.png":
        return 0
    m_spread_img = re.match(r"^spread_(\d+)_img(\d+)\.png$", filename)
    if m_spread_img:
        return int(m_spread_img.group(1)) + 1
    m_zero_padded = re.match(r"^spread_(0\d+)\.png$", filename)
    if m_zero_padded:
        return int(m_zero_padded.group(1)) + 1
    m_legacy = re.search(r"(?:spread|page)_0*(\d+)", filename)
    if m_legacy:
        try:
            val = int(m_legacy.group(1))
            return val if val >= 1 else val + 1
        except Exception:
            pass
    return default_idx


def _extract_spread_text_from_obj(sp_obj: Any) -> tuple[int | None, str]:
    """Extract (1-based display spread index, story text) from a SpreadContent or dict."""
    if isinstance(sp_obj, dict):
        sp_num = sp_obj.get("spread_number")
        sp_idx = sp_obj.get("spread_index")
        verso = sp_obj.get("verso_text")
        recto = sp_obj.get("recto_text")
        legacy = sp_obj.get("text") or sp_obj.get("page_text") or sp_obj.get("caption")
    else:
        sp_num = getattr(sp_obj, "spread_number", None)
        sp_idx = getattr(sp_obj, "spread_index", None)
        verso = getattr(sp_obj, "verso_text", None)
        recto = getattr(sp_obj, "recto_text", None)
        legacy = getattr(sp_obj, "text", None) or getattr(sp_obj, "page_text", None)

    parts = [str(p).strip() for p in (verso, recto) if p and str(p).strip()]
    if not parts and legacy and str(legacy).strip():
        parts = [str(legacy).strip()]
    combined = "\n\n".join(parts)

    if sp_idx is not None:
        return int(sp_idx), combined
    if sp_num is not None:
        return int(sp_num) + 1, combined
    return None, combined


def _load_spread_captions(session_id: str) -> dict[int, str]:
    """Load story page/spread text keyed by 1-based display spread index (and 0 for reference)."""
    captions: dict[int, str] = {
        0: "Character Bible & visual style reference sheet",
    }
    try:
        state = gcs.load_pipeline_state(session_id)
    except Exception:
        state = None

    if state is not None:
        for sp_obj in getattr(state, "spread_contents", None) or []:
            disp_idx, txt = _extract_spread_text_from_obj(sp_obj)
            if disp_idx is not None and txt:
                captions[disp_idx] = txt
        for sp_obj in getattr(state, "spreads", None) or []:
            disp_idx, txt = _extract_spread_text_from_obj(sp_obj)
            if disp_idx is not None and txt and disp_idx not in captions:
                captions[disp_idx] = txt
        for page_i, page_obj in enumerate(getattr(state, "pages", None) or [], start=1):
            p_txt = (
                page_obj.get("story_text")
                if isinstance(page_obj, dict)
                else getattr(page_obj, "story_text", None)
            )
            if p_txt and str(p_txt).strip() and page_i not in captions:
                captions[page_i] = str(p_txt).strip()

    if len(captions) <= 1:
        try:
            for sp_obj in gcs.load_spread_contents(session_id) or []:
                disp_idx, txt = _extract_spread_text_from_obj(sp_obj)
                if disp_idx is not None and txt and disp_idx not in captions:
                    captions[disp_idx] = txt
        except Exception:
            pass

    return captions


class A2UIEventSink:
    """Adapter around EventSink that translates pipeline progress events into A2UI SSE updates."""

    def __init__(
        self,
        session_id: str,
        inner_sink: EventSink | None = None,
        base_url: str = "",
        title: str = "Storybook",
        author: str = "",
        surface_id: str | None = None,
        context_id: str | None = None,
        out_queue: asyncio.Queue[dict[str, Any] | None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.inner_sink = inner_sink
        self.base_url = _normalize_base_url(base_url)
        self.title = title
        self.author = author
        self.surface_id = surface_id or f"storybook_run_{session_id}"
        self.context_id = context_id or session_id
        self.queue: asyncio.Queue[dict[str, Any] | None] = out_queue or asyncio.Queue()
        self.spreads: list[dict[str, Any]] = []
        self._emitted_spread_keys: set[str] = set()
        self.breadcrumbs: list[str] = ["✅ [5%] Initializing session"]
        self.last_stage: str = "initializing"
        self.last_pct: int | float = 5
        self.last_detail: str = "Initializing session"
        self._initialized = False
        self._closed = False

    @property
    def spread_card_ids(self) -> list[str]:
        return [f"spread-card-{sp['spread_index']}" for sp in self.spreads]

    @property
    def recent_breadcrumbs(self) -> list[str]:
        return self.breadcrumbs[-6:]

    def _record_breadcrumb(self, ev: dict[str, Any]) -> str:
        line = format_breadcrumb_line(ev, default_pct=self.last_pct)
        if line and (not self.breadcrumbs or self.breadcrumbs[-1] != line):
            self.breadcrumbs.append(line)
        return line

    def format_status_text(self, headline: str, elapsed_seconds: float | None = None) -> str:
        trail = "\n".join(self.recent_breadcrumbs)
        elapsed_line = (
            f"\n⏱️ Elapsed: {int(elapsed_seconds)}s" if elapsed_seconds is not None else ""
        )
        return f"{headline}{elapsed_line}\n\n**Live Activity Breadcrumbs:**\n{trail}"

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        trace_url = f"{self.base_url}/api/sessions/{self.session_id}/trace"
        init_envelopes = build_progress_surface_init(
            surface_id=self.surface_id,
            title=self.title,
            author=self.author,
            trace_url=trace_url,
            breadcrumbs=self.recent_breadcrumbs,
            session_id=self.session_id,
        )
        await self.queue.put(
            make_task_status_event(
                task_id=self.session_id,
                context_id=self.context_id,
                state="working",
                final=False,
                text=self.format_status_text(
                    f"⏳ Starting storybook pipeline for **{self.title}** on GKE (5%)..."
                ),
                envelopes=init_envelopes,
            )
        )

    async def put(self, item: dict[str, Any] | None) -> None:
        if self.inner_sink is not None:
            await self.inner_sink.put(item)

        if item is None:
            if not self._closed:
                self._closed = True
                await self.queue.put(None)
            return

        await self.ensure_initialized()

        stage = str(item.get("stage", ""))
        status = str(item.get("status", "in_progress"))
        detail = str(item.get("detail") or item.get("message") or "")
        message = str(item.get("message") or detail or "")
        pct = item.get("pct")
        if pct is not None:
            self.last_pct = pct
        if stage:
            self.last_stage = stage

        breadcrumb_line = self._record_breadcrumb(item)
        human_detail = re.sub(r"^[^\[]*\[\d+%\]\s*", "", breadcrumb_line).strip() or detail
        if human_detail:
            self.last_detail = human_detail

        if stage in ("error", "cancelled"):
            envelopes = build_stage_update(
                surface_id=self.surface_id,
                stage="error",
                status="failed",
                detail=message or "Pipeline error",
                pct=self.last_pct,
                title=self.title,
                author=self.author,
                spreads=self.spreads,
                breadcrumbs=self.recent_breadcrumbs,
                session_id=self.session_id,
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="failed",
                    final=True,
                    text=self.format_status_text(
                        f"❌ Storybook pipeline failed: {message or 'Unknown error'}"
                    ),
                    envelopes=envelopes,
                )
            )
            self._closed = True
            await self.queue.put(None)
            return

        if stage == "done":
            self.last_pct = 100
            pdf_url = str(item.get("pdf_url") or "") or get_public_asset_url(
                self.session_id,
                "pdf",
                "storybook.pdf",
                base_url=self.base_url,
            )
            envelopes = build_download_chip_update(
                surface_id=self.surface_id,
                pdf_url=pdf_url,
                title=self.title,
                author=self.author,
                spreads=self.spreads,
                breadcrumbs=self.recent_breadcrumbs,
                session_id=self.session_id,
            )
            done_text = (
                f"🎉 Your storybook **{self.title}**"
                + (f" by **{self.author}**" if self.author else "")
                + f" is complete!\n\n[📥 Download Completed Storybook PDF]({pdf_url})"
                + "\n\n**Live Activity Breadcrumbs:**\n"
                + "\n".join(self.recent_breadcrumbs)
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="working",
                    final=False,
                    text=done_text,
                    envelopes=envelopes,
                )
            )
            await self.queue.put(
                make_task_artifact_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    text=done_text,
                    envelopes=envelopes,
                    artifact_id=f"storybook-pdf-{self.session_id}",
                    name=f"Storybook: {self.title}",
                    last_chunk=True,
                )
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="completed",
                    final=True,
                    text=done_text,
                    envelopes=envelopes,
                )
            )
            self._closed = True
            await self.queue.put(None)
            return

        spread_one = None
        for k in ("spread_index", "spread_number", "page_number"):
            if item.get(k) is not None:
                spread_one = int(item[k])
                break
        spread_zero = int(item["spread"]) if item.get("spread") is not None else None
        raw_msg_low = message.strip().lower()

        has_spread_image = stage in ("generating_image", "image_generation", "spread_image") and (
            bool(item.get("image_url"))
            or bool(item.get("filename"))
            or status in ("completed", "done")
            or raw_msg_low in ("image ready", "done", "cached")
            or (spread_one is not None and raw_msg_low not in ("start", "prompt ready"))
        )

        if has_spread_image:
            raw_fname = str(item.get("filename") or "")
            if spread_one is not None:
                idx = spread_one
                img_filename = raw_fname or (
                    "reference.png" if idx == 0 else f"spread_{max(0, idx - 1):02d}_img0.png"
                )
            elif spread_zero is not None:
                idx = spread_zero + 1
                img_filename = raw_fname or f"spread_{spread_zero:02d}_img0.png"
            elif raw_fname:
                idx = _parse_spread_index_from_filename(raw_fname, len(self.spreads) + 1)
                img_filename = raw_fname
            else:
                idx = len(self.spreads) + 1
                img_filename = f"spread_{max(0, idx - 1):02d}_img0.png"

            if raw_fname and spread_one is None and spread_zero is None:
                idx = _parse_spread_index_from_filename(raw_fname, idx)

            dedup_key = f"{idx}:{img_filename}"
            image_url = str(item.get("image_url") or "") or get_public_asset_url(
                self.session_id,
                "image",
                filename=img_filename,
                base_url=self.base_url,
            )
            default_sp_title = "Character & Style Reference" if idx == 0 else f"Spread {idx}"
            sp_title = str(item.get("title") or default_sp_title)
            caption = str(
                item.get("caption")
                or item.get("page_text")
                or (detail if detail not in ("image ready", "done", "cached", "start") else "")
                or f"{sp_title} illustrated"
            )
            spread_entry = {
                "spread_index": idx,
                "title": sp_title,
                "image_url": image_url,
                "caption": caption,
                "filename": img_filename,
            }
            if dedup_key not in self._emitted_spread_keys:
                self._emitted_spread_keys.add(dedup_key)
                existing_pos = next(
                    (i for i, s in enumerate(self.spreads) if s.get("spread_index") == idx),
                    None,
                )
                if existing_pos is not None:
                    self.spreads[existing_pos] = spread_entry
                else:
                    self.spreads.append(spread_entry)
                    self.spreads.sort(key=lambda s: int(s.get("spread_index", 1)))

            envelopes = build_spread_card_update(
                surface_id=self.surface_id,
                spread_index=idx,
                image_url=image_url,
                caption=caption,
                title=sp_title,
                all_spreads=self.spreads,
                book_title=self.title,
                book_author=self.author,
                pct=self.last_pct,
                stage=self.last_stage,
                detail=human_detail,
                breadcrumbs=self.recent_breadcrumbs,
                session_id=self.session_id,
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="working",
                    final=False,
                    text=self.format_status_text(
                        f"🎨 [{int(self.last_pct)}%] Illustrated {sp_title}: {caption}"
                    ),
                    envelopes=envelopes,
                )
            )
            return

        if stage:
            envelopes = build_stage_update(
                surface_id=self.surface_id,
                stage=stage,
                status=status,
                detail=human_detail or status,
                pct=self.last_pct,
                title=self.title,
                author=self.author,
                spreads=self.spreads,
                breadcrumbs=self.recent_breadcrumbs,
                session_id=self.session_id,
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="working",
                    final=False,
                    text=self.format_status_text(breadcrumb_line),
                    envelopes=envelopes,
                )
            )

    async def checkpoint(self) -> None:
        if self.inner_sink is not None and hasattr(self.inner_sink, "checkpoint"):
            await self.inner_sink.checkpoint()


def format_jsonrpc_sse(rpc_id: str | int | None, result: dict[str, Any]) -> str:
    """Format an A2A JSON-RPC 2.0 response payload as an SSE data frame."""
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": result,
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stop_session_actor_safely(session_id: str) -> None:
    """Stop the Substrate actor for `session_id`, ignoring not-found/transport errors."""
    try:
        await ate.stop_actor(name=session_id, atespace=settings.substrate_atespace)
    except Exception as exc:
        log.debug("ate.stop_actor cleanup for session %s ignored error: %s", session_id, exc)


async def stream_session_a2ui_events(
    session_id: str,
    rpc_id: str | int | None,
    base_url: str = "",
    title: str = "Storybook",
    author: str = "",
    context_id: str | None = None,
    queue: asyncio.Queue[dict[str, Any] | None] | None = None,
    poll_interval: float = 1.5,
    heartbeat_interval: float = 10.0,
) -> AsyncIterator[str]:
    """Yield JSON-RPC 2.0 SSE frames with real-time breadcrumbs and spread images for a storybook session."""  # noqa: E501
    ctx_id = context_id or session_id
    started_monotonic = time.monotonic()

    # 1. Always emit initial 'submitted' frame first as required by WASD
    yield format_jsonrpc_sse(
        rpc_id,
        make_task_status_event(
            task_id=session_id,
            context_id=ctx_id,
            state="submitted",
            final=False,
        ),
    )

    if queue is not None:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
            except TimeoutError:
                elapsed_s = int(time.monotonic() - started_monotonic)
                hb_ev = make_task_status_event(
                    task_id=session_id,
                    context_id=ctx_id,
                    state="working",
                    final=False,
                    text=f"⏳ Working on **{title}**...\n⏱️ Elapsed: {elapsed_s}s",
                )
                yield format_jsonrpc_sse(rpc_id, hb_ev)
                continue
            if ev is None:
                await _stop_session_actor_safely(session_id)
                break
            yield format_jsonrpc_sse(rpc_id, ev)
            if ev.get("final") is True:
                await _stop_session_actor_safely(session_id)
                break
        return

    sink = A2UIEventSink(
        session_id=session_id,
        base_url=base_url,
        title=title,
        author=author,
        context_id=ctx_id,
    )
    await sink.ensure_initialized()
    while not sink.queue.empty():
        init_ev = await sink.queue.get()
        if init_ev is not None:
            yield format_jsonrpc_sse(rpc_id, init_ev)

    cursor = -1
    seen_images: set[str] = set()
    elapsed_since_heartbeat = 0.0
    terminal_reached = False

    while True:
        emitted_in_tick = False
        try:
            events, is_done = await asyncio.to_thread(gcs.load_progress_events, session_id)
        except Exception:
            events, is_done = [], False

        new_events: list[dict[str, Any]] = []
        for idx, raw_ev in enumerate(events):
            if not isinstance(raw_ev, dict):
                continue
            seq = int(raw_ev.get("seq", idx))
            if seq <= cursor:
                continue
            cursor = seq
            new_events.append(raw_ev)

        # Check GCS for newly written reference or spread images
        try:
            current_images = await asyncio.to_thread(gcs.list_session_images, session_id)
        except Exception:
            current_images = []

        valid_images = [img for img in current_images if _VALID_IMAGE_RE.match(img)]
        new_images = [img for img in valid_images if img not in seen_images]

        if new_images:
            spread_captions = await asyncio.to_thread(_load_spread_captions, session_id)
            for img_name in new_images:
                seen_images.add(img_name)
                sp_idx = _parse_spread_index_from_filename(img_name, len(seen_images))
                sp_title = (
                    "Character & Style Reference"
                    if img_name == "reference.png" or sp_idx == 0
                    else f"Spread {sp_idx}"
                )
                caption = spread_captions.get(
                    sp_idx, f"Illustrated {sp_title} ({img_name})"
                )
                await sink.put(
                    {
                        "stage": "spread_image",
                        "status": "completed",
                        "spread_index": sp_idx,
                        "filename": img_name,
                        "title": sp_title,
                        "caption": caption,
                        "pct": sink.last_pct,
                        "message": f"Generated {sp_title} illustration ({img_name})",
                    }
                )
                while not sink.queue.empty():
                    a2ui_ev = await sink.queue.get()
                    if a2ui_ev is None:
                        await _stop_session_actor_safely(session_id)
                        return
                    emitted_in_tick = True
                    yield format_jsonrpc_sse(rpc_id, a2ui_ev)

        for raw_ev in new_events:
            ev_stage = str(raw_ev.get("stage") or "")
            # Enrich spread_image / generating_image events with story text from state.json
            if ev_stage in ("generating_image", "image_generation", "spread_image"):
                s0 = raw_ev.get("spread")
                s1 = raw_ev.get("spread_index") or raw_ev.get("spread_number")
                disp_idx = int(s1) if s1 is not None else (int(s0) + 1 if s0 is not None else None)
                if disp_idx is not None and not raw_ev.get("caption"):
                    captions = await asyncio.to_thread(_load_spread_captions, session_id)
                    if disp_idx in captions:
                        raw_ev = {**raw_ev, "caption": captions[disp_idx]}

            await sink.put(raw_ev)
            if ev_stage in ("done", "error", "cancelled"):
                terminal_reached = True
            while not sink.queue.empty():
                a2ui_ev = await sink.queue.get()
                if a2ui_ev is None:
                    await _stop_session_actor_safely(session_id)
                    return
                emitted_in_tick = True
                yield format_jsonrpc_sse(rpc_id, a2ui_ev)
                if a2ui_ev.get("final") is True:
                    await _stop_session_actor_safely(session_id)
                    return

        if not is_done and not terminal_reached:
            try:
                state = await asyncio.to_thread(gcs.load_pipeline_state, session_id)
            except Exception:
                state = None
            if state is not None and state.current_stage in ("done", "error", "cancelled"):
                if state.current_stage == "done":
                    is_done = True
                else:
                    err_msg = "; ".join(state.errors) if state.errors else "Pipeline failed"
                    await sink.put(
                        {"stage": "error", "message": err_msg, "pct": state.progress_pct}
                    )
                    while not sink.queue.empty():
                        a2ui_ev = await sink.queue.get()
                        if a2ui_ev is not None:
                            yield format_jsonrpc_sse(rpc_id, a2ui_ev)
                    await _stop_session_actor_safely(session_id)
                    return

        if is_done or terminal_reached:
            if not sink._closed:
                await sink.put({"stage": "done", "pct": 100})
                while not sink.queue.empty():
                    a2ui_ev = await sink.queue.get()
                    if a2ui_ev is not None:
                        yield format_jsonrpc_sse(rpc_id, a2ui_ev)
            await _stop_session_actor_safely(session_id)
            return

        await asyncio.sleep(poll_interval)
        if emitted_in_tick:
            elapsed_since_heartbeat = 0.0
        else:
            elapsed_since_heartbeat += poll_interval
            if elapsed_since_heartbeat >= heartbeat_interval:
                elapsed_since_heartbeat = 0.0
                total_elapsed = time.monotonic() - started_monotonic
                stage_label = STAGE_LABELS.get(
                    sink.last_stage, sink.last_stage.replace("_", " ").title()
                )
                hb_envelopes = build_stage_update(
                    surface_id=sink.surface_id,
                    stage=sink.last_stage,
                    status="in_progress",
                    detail=f"{sink.last_detail} (⏱️ {int(total_elapsed)}s elapsed)",
                    pct=sink.last_pct,
                    title=sink.title,
                    author=sink.author,
                    spreads=sink.spreads,
                    breadcrumbs=sink.recent_breadcrumbs,
                    session_id=session_id,
                )
                hb_headline = (
                    f"⏳ [{int(sink.last_pct)}%] {stage_label}: {sink.last_detail}"
                )
                hb_event = make_task_status_event(
                    task_id=session_id,
                    context_id=ctx_id,
                    state="working",
                    final=False,
                    text=sink.format_status_text(hb_headline, elapsed_seconds=total_elapsed),
                    envelopes=hb_envelopes,
                )
                yield format_jsonrpc_sse(rpc_id, hb_event)
