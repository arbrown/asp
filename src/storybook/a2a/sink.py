"""A2UI event sink, asset URL helpers, and A2A SSE stream generator for Gemini Enterprise (WASD)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from storybook.a2a.a2ui import (
    build_download_chip_update,
    build_message_parts,
    build_progress_surface_init,
    build_spread_card_update,
    build_stage_update,
)
from storybook.events import EventSink
from storybook.tools import gcs

log = logging.getLogger(__name__)

DEFAULT_PUBLIC_BASE_URL = "https://pushkin-agent.duckdns.org"


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
    fname = filename or "spread_01_img0.png"
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
        self._initialized = False
        self._closed = False

    @property
    def spread_card_ids(self) -> list[str]:
        return [f"spread-card-{sp['spread_index']}" for sp in self.spreads]

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
        )
        await self.queue.put(
            make_task_status_event(
                task_id=self.session_id,
                context_id=self.context_id,
                state="working",
                final=False,
                text=f"⏳ Starting storybook pipeline for **{self.title}** on GKE...",
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

        if stage == "error":
            envelopes = build_stage_update(
                surface_id=self.surface_id,
                stage="error",
                status="failed",
                detail=message or "Pipeline error",
                pct=pct,
                title=self.title,
                author=self.author,
                spreads=self.spreads,
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="failed",
                    final=True,
                    text=f"❌ Storybook pipeline failed: {message or 'Unknown error'}",
                    envelopes=envelopes,
                )
            )
            self._closed = True
            await self.queue.put(None)
            return

        if stage == "done":
            pdf_url = get_public_asset_url(
                self.session_id,
                "pdf",
                filename=str(item.get("pdf_filename") or "storybook.pdf"),
                base_url=self.base_url,
            )
            envelopes = build_download_chip_update(
                surface_id=self.surface_id,
                pdf_url=pdf_url,
                title=self.title,
                author=self.author,
                spreads=self.spreads,
            )
            done_text = (
                f"🎉 Your storybook **{self.title}**"
                + (f" by **{self.author}**" if self.author else "")
                + f" is complete!\n\n[📥 Download Completed Storybook PDF]({pdf_url})"
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

        spread_idx = item.get("spread_index") or item.get("spread_number") or item.get("page_number")  # noqa: E501
        has_spread_image = (
            stage in ("generating_image", "image_generation", "spread_image")
            and (
                spread_idx is not None
                or item.get("image_url")
                or item.get("filename")
                or status in ("completed", "done")
            )
        )

        if has_spread_image:
            idx = int(spread_idx) if spread_idx is not None else len(self.spreads) + 1
            img_filename = str(item.get("filename") or f"spread_{idx:02d}_img0.png")
            dedup_key = f"{idx}:{img_filename}"
            image_url = str(item.get("image_url") or "") or get_public_asset_url(
                self.session_id,
                "image",
                filename=img_filename,
                base_url=self.base_url,
            )
            caption = str(item.get("caption") or item.get("page_text") or detail or f"Spread {idx} illustrated")  # noqa: E501
            sp_title = str(item.get("title") or f"Spread {idx}")
            if dedup_key not in self._emitted_spread_keys:
                self._emitted_spread_keys.add(dedup_key)
                self.spreads.append(
                    {
                        "spread_index": idx,
                        "title": sp_title,
                        "image_url": image_url,
                        "caption": caption,
                    }
                )
            envelopes = build_spread_card_update(
                surface_id=self.surface_id,
                spread_index=idx,
                image_url=image_url,
                caption=caption,
                title=sp_title,
                all_spreads=self.spreads,
                book_title=self.title,
                book_author=self.author,
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="working",
                    final=False,
                    text=f"🎨 Illustrated Spread {idx}: {caption}",
                    envelopes=envelopes,
                )
            )
            return

        if stage:
            envelopes = build_stage_update(
                surface_id=self.surface_id,
                stage=stage,
                status=status,
                detail=detail,
                pct=pct,
                title=self.title,
                author=self.author,
                spreads=self.spreads,
            )
            await self.queue.put(
                make_task_status_event(
                    task_id=self.session_id,
                    context_id=self.context_id,
                    state="working",
                    final=False,
                    text=f"⏳ Stage '{stage}': {detail or status}",
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


def _parse_spread_index_from_filename(filename: str, default_idx: int) -> int:
    m = re.search(r"(?:spread|page)_0*(\d+)", filename)
    if m:
        try:
            val = int(m.group(1))
            return val if val >= 1 else val + 1
        except Exception:
            pass
    return default_idx


async def stream_session_a2ui_events(
    session_id: str,
    rpc_id: str | int | None,
    base_url: str = "",
    title: str = "Storybook",
    author: str = "",
    context_id: str | None = None,
    queue: asyncio.Queue[dict[str, Any] | None] | None = None,
    poll_interval: float = 1.0,
    heartbeat_interval: float = 15.0,
) -> AsyncIterator[str]:
    """Yield JSON-RPC 2.0 SSE frames for a running storybook session."""
    ctx_id = context_id or session_id
    # 1. Always emit initial 'submitted' and 'working' frames first as required by WASD
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
                yield ": keepalive heartbeat\n\n"
                continue
            if ev is None:
                break
            yield format_jsonrpc_sse(rpc_id, ev)
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

    seen_count = 0
    seen_images: set[str] = set()
    elapsed_since_heartbeat = 0.0

    while True:
        try:
            events, is_done = gcs.load_progress_events(session_id)
        except Exception:
            events, is_done = [], False

        new_events = events[seen_count:]
        seen_count = len(events)

        for raw_ev in new_events:
            await sink.put(raw_ev)
            while not sink.queue.empty():
                a2ui_ev = await sink.queue.get()
                if a2ui_ev is None:
                    return
                yield format_jsonrpc_sse(rpc_id, a2ui_ev)

        # Also check GCS for newly written spread images (spread_1.png / spread_01_img0.png)
        try:
            current_images = gcs.list_session_images(session_id)
        except Exception:
            current_images = []

        new_images = [img for img in current_images if img not in seen_images]
        if new_images:
            spread_captions: dict[int, str] = {}
            try:
                spread_objs = gcs.load_spread_contents(session_id)
                for sp_obj in spread_objs:
                    sp_num = getattr(sp_obj, "spread_number", None)
                    sp_txt = getattr(sp_obj, "text", None) or getattr(sp_obj, "page_text", None)
                    if sp_num is not None and sp_txt:
                        spread_captions[int(sp_num)] = str(sp_txt)
            except Exception:
                pass

            for img_name in new_images:
                seen_images.add(img_name)
                sp_idx = _parse_spread_index_from_filename(img_name, len(seen_images))
                caption = spread_captions.get(sp_idx, f"Illustrated spread {sp_idx} ({img_name})")
                await sink.put(
                    {
                        "stage": "spread_image",
                        "status": "completed",
                        "spread_index": sp_idx,
                        "filename": img_name,
                        "caption": caption,
                    }
                )
                while not sink.queue.empty():
                    a2ui_ev = await sink.queue.get()
                    if a2ui_ev is None:
                        return
                    yield format_jsonrpc_sse(rpc_id, a2ui_ev)

        if is_done:
            await sink.put({"stage": "done", "pct": 100})
            while not sink.queue.empty():
                a2ui_ev = await sink.queue.get()
                if a2ui_ev is not None:
                    yield format_jsonrpc_sse(rpc_id, a2ui_ev)
            return

        await asyncio.sleep(poll_interval)
        elapsed_since_heartbeat += poll_interval
        if elapsed_since_heartbeat >= heartbeat_interval:
            elapsed_since_heartbeat = 0.0
            yield ": keepalive heartbeat\n\n"
