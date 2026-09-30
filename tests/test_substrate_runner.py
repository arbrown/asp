"""Unit tests for Substrate Actor integration, GCSProgressSink, and standalone runner."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from storybook.api.models import CreateSessionRequest
from storybook.api.routes import cancel_session, create_session, resume_session, stream_session
from storybook.events import GCSProgressSink
from storybook.models import PipelineState, SessionConfig, SourceConfig
from storybook.runner import execute_session


@pytest.mark.asyncio
async def test_gcs_progress_sink_checkpoints_state_and_events():
    state = PipelineState(
        session_id="sink-test-123",
        config=SessionConfig(source=SourceConfig(title="Alice", author="Carroll")),
    )
    saved_events = []
    saved_states = []

    def fake_save_events(sid, events, done=False):
        saved_events.append((sid, list(events), done))
        return "gs://bucket/events.json"

    def fake_save_state(sid, st):
        saved_states.append((sid, st.current_stage, st.progress_pct))
        return "gs://bucket/state.json"

    with patch("storybook.events.gcs.save_progress_events", side_effect=fake_save_events), \
         patch("storybook.events.gcs.save_pipeline_state", side_effect=fake_save_state), \
         patch("storybook.events.store.upsert_session", new_callable=AsyncMock):
        sink = GCSProgressSink("sink-test-123", state)
        state.current_stage = "fetching"
        state.progress_pct = 10
        await sink.put({"stage": "fetching", "pct": 10})
        state.current_stage = "done"
        state.progress_pct = 100
        await sink.put({"stage": "done", "pct": 100})

    assert len(saved_events) == 2
    assert saved_events[-1][2] is True
    assert len(saved_states) == 2
    assert saved_states[-1] == ("sink-test-123", "done", 100)


@pytest.mark.asyncio
async def test_routes_create_cancel_resume_actor_lifecycle():
    cfg = SessionConfig(source=SourceConfig(title="The Odyssey", author="Homer"))
    req = CreateSessionRequest(config=cfg)

    with patch("storybook.api.routes.gcs.save_pipeline_state", return_value="gs://b/state.json"), \
         patch("storybook.api.routes.gcs.save_progress_events", return_value="gs://b/events.json"), \
         patch("storybook.api.routes.store.upsert_session", new_callable=AsyncMock), \
         patch("storybook.api.routes.ate.create_actor", new_callable=AsyncMock) as mock_create, \
         patch("storybook.api.routes.ate.stop_actor", new_callable=AsyncMock) as mock_stop, \
         patch("storybook.api.routes.ate.is_actor_running", new_callable=AsyncMock) as mock_running:
        mock_create.return_value = {"name": "sid-1", "atespace": "asp", "state": "ACTOR_STATE_RUNNING"}
        resp = await create_session(req)
        sid = resp.session_id
        mock_create.assert_awaited_once_with(
            template="asp-runner",
            atespace="asp",
            name=sid,
            resume=False,
        )

        # Cancel session -> calls ate.stop_actor
        state = PipelineState(session_id=sid, config=cfg, current_stage="generating_image", progress_pct=50)
        with patch("storybook.api.routes.gcs.load_pipeline_state", return_value=state), \
             patch("storybook.api.routes.gcs.load_progress_events", return_value=([], False)):
            cancel_resp = await cancel_session(sid)
            mock_stop.assert_awaited_once_with(name=sid, atespace="asp")
            assert cancel_resp.current_stage == "error"
            assert cancel_resp.resumable is True

            # Resume session -> calls ate.create_actor(..., resume=True)
            mock_running.return_value = False
            resume_resp = await resume_session(sid)
            assert resume_resp.current_stage == "resuming"
            assert mock_create.await_count == 2
            assert mock_create.await_args_list[-1].kwargs["resume"] is True


@pytest.mark.asyncio
async def test_runner_execute_session_loads_from_gcs_and_runs_pipeline():
    sid = "runner-test-session"
    state = PipelineState(
        session_id=sid,
        config=SessionConfig(source=SourceConfig(title="Peter Pan", author="Barrie")),
        current_stage="initializing",
    )

    async def fake_run_pipeline(st, sink, resume=False):
        st.current_stage = "done"
        st.progress_pct = 100
        await sink.put({"stage": "done", "pct": 100})
        return st

    with patch("storybook.runner.init_tracing"), \
         patch("storybook.runner.store.init_db", new_callable=AsyncMock), \
         patch("storybook.runner.gcs.load_pipeline_state", return_value=state), \
         patch("storybook.runner.gcs.load_progress_events", return_value=([{"stage": "fetching", "pct": 10, "seq": 0}], False)), \
         patch("storybook.events.gcs.save_progress_events") as mock_save_events, \
         patch("storybook.events.gcs.save_pipeline_state"), \
         patch("storybook.events.store.upsert_session", new_callable=AsyncMock), \
         patch("storybook.runner.run_pipeline", side_effect=fake_run_pipeline) as mock_run:
        code = await execute_session(sid, force_resume=True)
        assert code == 0
        mock_run.assert_awaited_once()
        assert mock_run.await_args.kwargs["resume"] is True
        assert state.current_stage == "done"
        assert state.finished_at is not None
        final_events = mock_save_events.call_args[0][1]
        assert [e["seq"] for e in final_events] == [0, 1, 2]
        assert final_events[1]["stage"] == "resuming"
        assert final_events[2]["stage"] == "done"
