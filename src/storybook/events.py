"""Pluggable progress event publishing sink."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from storybook.db import store
from storybook.models import PipelineState
from storybook.tools import gcs

log = logging.getLogger(__name__)


@runtime_checkable
class EventSink(Protocol):
    """Protocol for pipeline progress event sinks (compatible with asyncio.Queue)."""

    async def put(self, item: dict[str, Any] | None) -> None: ...


class GCSProgressSink:
    """Persists pipeline progress events and state checkpoints to GCS and rqlite."""

    def __init__(
        self,
        session_id: str,
        state: PipelineState,
        existing_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session_id = session_id
        self.state = state
        self._events: list[dict[str, Any]] = list(existing_events or [])
        self._done = False
        self._lock = asyncio.Lock()
        self._last_checkpoint_stage = ""

    async def put(self, item: dict[str, Any] | None) -> None:
        async with self._lock:
            if item is None:
                self._done = True
                await self._flush(checkpoint_state=True)
                return

            ev = dict(item)
            ev["seq"] = len(self._events)
            ev.setdefault("ts", datetime.now(timezone.utc).isoformat())
            self._events.append(ev)

            stage = ev.get("stage", "")
            if stage in ("done", "error"):
                self._done = True

            # Checkpoint full state.json on stage transitions, spread completions, or terminal events
            should_checkpoint = (
                stage != self._last_checkpoint_stage
                or stage in ("done", "error")
                or ev.get("message") in ("done", "cached")
            )
            if stage and stage != "image_retry":
                self._last_checkpoint_stage = stage

            await self._flush(checkpoint_state=should_checkpoint)

    async def checkpoint(self) -> None:
        """Explicitly flush state.json and rqlite checkpoint."""
        async with self._lock:
            await self._flush(checkpoint_state=True)

    async def _flush(self, checkpoint_state: bool = False) -> None:
        try:
            await asyncio.to_thread(
                gcs.save_progress_events,
                self.session_id,
                list(self._events),
                self._done,
            )
        except Exception:
            log.exception("Failed to write events.json to GCS for session %s", self.session_id)

        if checkpoint_state:
            try:
                await asyncio.to_thread(gcs.save_pipeline_state, self.session_id, self.state)
            except Exception:
                log.exception("Failed to write state.json to GCS for session %s", self.session_id)

        try:
            await store.upsert_session(self.state)
        except Exception:
            log.debug("Could not upsert session %s to rqlite during flush", self.session_id)
