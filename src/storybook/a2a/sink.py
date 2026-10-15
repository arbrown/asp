"""A2UI v0.9 event sink, GCS V4 signed URL helper, and A2A SSE stream generator."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from storybook.a2a.a2ui import (
    build_download_chip_update,
    build_progress_surface_init,
    build_spread_card_update,
    build_stage_update,
    wrap_a2ui_data_part,
)
from storybook.events import EventSink
from storybook.tools import gcs

log = logging.getLogger(__name__)


def get_signed_or_api_url(
    session_id: str,
    asset_type: str,
    filename: str = "",
    base_url: str = "",
) -> str:
    """Return a V4 signed GCS URL for a session asset, or fall back to the FastAPI proxy URL.

    Args:
        session_id: The storybook session ID.
        asset_type: Either "pdf" or "image" (or "images").
        filename: Asset filename (e.g. "spread_00_img0.png" or "storybook.pdf").
        base_url: Optional HTTP base URL for the API fallback route.
    """
    prefix = base_url.rstrip("/")
    if prefix.endswith("/a2a"):
        prefix = prefix[:-4]
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
            dl_name = filename or "storybook.pdf"
            sign_kwargs["response_disposition"] = f'attachment; filename="{dl_name}"'
            sign_kwargs["response_type"] = "application/pdf"
        signed_url = blob.generate_signed_url(**sign_kwargs)
        if isinstance(signed_url, str) and signed_url.startswith("http"):
            return signed_url
    except Exception as exc:
        log.debug(
            "GCS V4 signed URL unavailable for session %s (%s), falling back to API route: %s",
            session_id,
            asset_type,
            exc,
        )

    return fallback_path


def make_task_status_event(
    session_id: str,
    state: str,
    envelopes: list[dict[str, Any]],
    text: str,
    final: bool = False,
) -> dict[str, Any]:
    """Create an A2A TaskStatusUpdateEvent carrying A2UI v0.9 envelopes."""
    return {
        "kind": "status-update",
        "taskId": session_id,
        "contextId": session_id,
        "final": final,
        "status": {
            "state": state,
            "timestamp": datetime.now(UTC).isoformat(),
            "message": {
                "kind": "message",
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "taskId": session_id,
                "contextId": session_id,
                "parts": [
                    {"kind": "text", "text": text},
                    wrap_a2ui_data_part(envelopes),
                ],
            },
        },
    }


class A2UIEventSink:
    """EventSink wrapper that translates pipeline progress events into A2A + A2UI v0.9 events."""

    def __init__(
        self,
        session_id: str,
        inner: EventSink | None = None,
        queue: asyncio.Queue[dict[str, Any] | None] | None = None,
        book_title: str = "Storybook",
        base_url: str = "",
        emit_init_on_start: bool = True,
    ) -> None:
        self.session_id = session_id
        self.inner = inner
        self.queue: asyncio.Queue[dict[str, Any] | None] = queue or asyncio.Queue()
        self.book_title = book_title or "Storybook"
        self.base_url = base_url
        self.emit_init_on_start = emit_init_on_start
        self._initialized = False
        self._emitted_spreads: set[int] = set()
        self._spread_card_ids: list[str] = []
        self._closed = False

    async def ensure_initialized(self) -> None:
        """Emit the initial Canvas createSurface + updateComponents event once."""
        if not self._initialized:
            self._initialized = True
            if self.emit_init_on_start:
                init_envelopes = build_progress_surface_init(self.session_id, title=self.book_title)
                init_ev = make_task_status_event(
                    session_id=self.session_id,
                    state="working",
                    envelopes=init_envelopes,
                    text=f"Opening storybook workspace for '{self.book_title}'...",
                    final=False,
                )
                await self.queue.put(init_ev)

    async def put(self, item: dict[str, Any] | None) -> None:
        if self.inner is not None:
            await self.inner.put(item)

        if item is None:
            if not self._closed:
                self._closed = True
                await self.queue.put(None)
            return

        await self.ensure_initialized()

        stage = str(item.get("stage") or "working")
        pct = item.get("pct")
        message = str(item.get("message") or "")
        status_str = f"{pct}%" if pct is not None else str(item.get("status") or "working")

        if stage == "done":
            pdf_url = item.get("pdf_url") or get_signed_or_api_url(
                self.session_id,
                "pdf",
                filename="storybook.pdf",
                base_url=self.base_url,
            )
            title = str(item.get("book_title") or self.book_title or "Storybook")
            dl_envelopes = build_download_chip_update(
                self.session_id,
                pdf_url=pdf_url,
                book_title=title,
            )
            done_ev = make_task_status_event(
                session_id=self.session_id,
                state="completed",
                envelopes=dl_envelopes,
                text=f"Storybook '{title}' is complete and ready to download.",
                final=True,
            )
            await self.queue.put(done_ev)
            return

        if stage == "error":
            err_envelopes = build_stage_update(
                self.session_id,
                stage="error",
                status="failed",
                detail=message or "Pipeline error",
            )
            err_ev = make_task_status_event(
                session_id=self.session_id,
                state="failed",
                envelopes=err_envelopes,
                text=f"Storybook pipeline failed: {message or 'Unknown error'}",
                final=True,
            )
            await self.queue.put(err_ev)
            return

        # Check if this event represents a completed spread illustration
        spread_raw = (
            item.get("spread_index") if item.get("spread_index") is not None else item.get("spread")
        )
        is_spread_event = (
            stage in ("image_generation", "spread_image")
            or (
                stage == "generating_image"
                and message in ("image ready", "done", "cached", "")
                and spread_raw is not None
            )
            or ("image_url" in item and spread_raw is not None)
        )

        if is_spread_event and spread_raw is not None:
            spread_idx = int(spread_raw)
            if spread_idx not in self._emitted_spreads or "image_url" in item:
                self._emitted_spreads.add(spread_idx)
                filename = str(item.get("filename") or f"spread_{spread_idx:02d}_img0.png")
                image_url = str(
                    item.get("image_url")
                    or get_signed_or_api_url(
                        self.session_id,
                        "image",
                        filename=filename,
                        base_url=self.base_url,
                    )
                )
                caption = str(
                    item.get("caption")
                    or (f"Spread {spread_idx} — {message}" if message else f"Spread {spread_idx}")
                )
                card_id = f"spread_card_{spread_idx}"
                spread_envelopes = build_spread_card_update(
                    self.session_id,
                    spread_index=spread_idx,
                    image_url=image_url,
                    caption=caption,
                    existing_spread_ids=list(self._spread_card_ids),
                )
                if card_id not in self._spread_card_ids:
                    self._spread_card_ids.append(card_id)
                stage_envelopes = build_stage_update(
                    self.session_id,
                    stage=stage,
                    status=status_str,
                    detail=f"Spread {spread_idx} ready",
                )
                ev = make_task_status_event(
                    session_id=self.session_id,
                    state="working",
                    envelopes=stage_envelopes + spread_envelopes,
                    text=f"Illustrated Spread {spread_idx}",
                    final=False,
                )
                await self.queue.put(ev)
                return

        # Standard pipeline stage update
        stage_envelopes = build_stage_update(
            self.session_id,
            stage=stage,
            status=status_str,
            detail=message,
        )
        stage_ev = make_task_status_event(
            session_id=self.session_id,
            state="working",
            envelopes=stage_envelopes,
            text=f"Stage: {stage} ({status_str})" + (f" — {message}" if message else ""),
            final=False,
        )
        await self.queue.put(stage_ev)

    async def checkpoint(self) -> None:
        if self.inner is not None and hasattr(self.inner, "checkpoint"):
            await self.inner.checkpoint()  # type: ignore[attr-defined]


def format_jsonrpc_sse(rpc_id: str | int | None, result: dict[str, Any]) -> str:
    """Format a JSON-RPC 2.0 response payload as an SSE data frame."""
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": result,
    }
    return f"data: {json.dumps(payload)}\n\n"


async def stream_session_a2ui_events(
    session_id: str,
    rpc_id: str | int | None = None,
    book_title: str = "Storybook",
    base_url: str = "",
    queue: asyncio.Queue[dict[str, Any] | None] | None = None,
    poll_interval: float = 1.0,
    heartbeat_interval: float = 15.0,
) -> AsyncIterator[str]:
    """Stream A2A JSON-RPC 2.0 TaskStatusUpdateEvent SSE frames with 15s keepalive heartbeats.

    Supports both:
    - Live in-memory `A2UIEventSink` queue (when `queue` is provided)
    - Polling GCS `load_progress_events(session_id)` for Substrate actor runs
    """
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
            if ev.get("final"):
                break
        return

    # GCS-backed progress stream converter
    sink = A2UIEventSink(
        session_id=session_id,
        book_title=book_title,
        base_url=base_url,
        emit_init_on_start=True,
    )
    await sink.ensure_initialized()
    while not sink.queue.empty():
        init_ev = await sink.queue.get()
        if init_ev is not None:
            yield format_jsonrpc_sse(rpc_id, init_ev)

    cursor = 0
    elapsed_since_heartbeat = 0.0

    while True:
        try:
            events, is_done = await asyncio.to_thread(gcs.load_progress_events, session_id)
        except Exception:
            events, is_done = [], False

        new_events = events[cursor:]
        for raw_ev in new_events:
            cursor += 1
            await sink.put(raw_ev)
            while not sink.queue.empty():
                a2ui_ev = await sink.queue.get()
                if a2ui_ev is None:
                    return
                yield format_jsonrpc_sse(rpc_id, a2ui_ev)
                if a2ui_ev.get("final"):
                    return

        if is_done:
            # If GCS marked done without an explicit "done" stage event in events list
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
