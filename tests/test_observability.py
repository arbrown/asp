from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from storybook.agents.image_validator import (
    check as check_image_validation,
)
from storybook.agents.image_validator import (
    record_validation_result,
)
from storybook.tools.gutenberg import (
    fetch_gutenberg_url,
    search_gutenberg,
)
from storybook.tracing import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_COMPLETION_TOKENS,
    GEN_AI_USAGE_PROMPT_TOKENS,
    CloudLoggingTraceFilter,
    GcpJsonFormatter,
    init_tracing,
    make_trace_url,
    set_project_id,
    set_span_token_usage,
    set_test_tracer_provider,
    setup_logging,
    trace_agent_call,
    trace_retry_attempt,
    trace_stage,
)


@pytest.fixture
def memory_exporter():
    """Fixture providing a fresh in-memory span exporter for each test."""
    exporter = set_test_tracer_provider()
    exporter.clear()
    return exporter


def test_trace_stage_success(memory_exporter):
    """Verify trace_stage produces span with expected name, attributes, and OK status."""
    with trace_stage("stage.fetch_literature", session_id="sess-123", book_title="Alice"):
        pass

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "stage.fetch_literature"
    assert span.attributes["session_id"] == "sess-123"
    assert span.attributes["book_title"] == "Alice"
    assert span.status.status_code == StatusCode.OK


def test_trace_stage_error(memory_exporter):
    """Verify trace_stage records exceptions and sets ERROR status."""
    with pytest.raises(ValueError, match="Failed to fetch"):
        with trace_stage("stage.fetch_literature", session_id="sess-err"):
            raise ValueError("Failed to fetch")

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "stage.fetch_literature"
    assert span.status.status_code == StatusCode.ERROR
    assert "Failed to fetch" in span.status.description
    events = [e.name for e in span.events]
    assert "exception" in events


def test_nested_spans_hierarchy(memory_exporter):
    """Verify child spans nest properly under the parent stage span."""
    with trace_stage("stage.generate_illustrations", session_id="sess-nest") as stage_span:
        stage_sc = stage_span.get_span_context()
        with trace_retry_attempt("image.generate_and_validate", attempt=1) as retry_span:
            retry_sc = retry_span.get_span_context()
            with trace_agent_call("image_generator", model="gemini-3.1-flash-image"):
                pass

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 3
    spans_by_name = {s.name: s for s in spans}

    stage_s = spans_by_name["stage.generate_illustrations"]
    retry_s = spans_by_name["image.generate_and_validate"]
    agent_s = spans_by_name["agent.image_generator"]

    # Verify hierarchy
    assert retry_s.parent.span_id == stage_s.context.span_id
    assert agent_s.parent.span_id == retry_s.context.span_id

    # Verify all share the same trace ID
    assert stage_s.context.trace_id == retry_s.context.trace_id == agent_s.context.trace_id
    assert stage_sc.trace_id == retry_sc.trace_id


def test_validation_retry_attributes_failure(memory_exporter):
    """Verify validation failure attributes on retry spans."""
    with check_image_validation(
        attempt=1, max_attempts=3, spread_number=1, image_index=0
    ) as val_span:
        record_validation_result(
            val_span,
            passed=False,
            score=0.0,
            attempt=1,
            reasons=["Character costume does not match bible", "Setting incorrect"],
        )

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "image_validator.check"
    assert span.attributes["validation.passed"] is False
    assert span.attributes["validation.score"] == 0.0
    assert span.attributes["validation.attempt"] == 1
    reasons = span.attributes["validation.reasons"]
    assert "Character costume does not match bible" in reasons


def test_validation_retry_attributes_success(memory_exporter):
    """Verify validation success attributes on retry spans."""
    with check_image_validation(
        attempt=2, max_attempts=3, spread_number=1, image_index=0
    ) as val_span:
        record_validation_result(val_span, passed=True, score=1.0, attempt=2)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "image_validator.check"
    assert span.attributes["validation.passed"] is True
    assert span.attributes["validation.score"] == 1.0
    assert span.attributes["validation.attempt"] == 2


def test_cloud_logging_trace_filter(memory_exporter):
    """Verify CloudLoggingTraceFilter populates GCP trace attributes when span is active."""
    logger = logging.getLogger("test.storybook.filter")
    logger.setLevel(logging.INFO)

    records: list[logging.LogRecord] = []

    class MemoryHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = MemoryHandler()
    trace_filter = CloudLoggingTraceFilter(project_id="storybook-prod")
    handler.addFilter(trace_filter)
    logger.addHandler(handler)

    try:
        # 1. Log inside an active span
        with trace_stage("stage.story_adapter", session_id="sess-log") as stage_span:
            ctx = stage_span.get_span_context()
            logger.info("Inside active span")

        # 2. Log outside any span
        logger.info("Outside active span")

        assert len(records) == 2
        rec_inside, rec_outside = records

        # Verify inside span record has trace correlation attributes
        expected_trace = f"projects/storybook-prod/traces/{format(ctx.trace_id, '032x')}"
        expected_span_id = format(ctx.span_id, "016x")

        assert getattr(rec_inside, "logging.googleapis.com/trace", None) == expected_trace
        assert getattr(rec_inside, "logging.googleapis.com/spanId", None) == expected_span_id
        assert getattr(rec_inside, "logging.googleapis.com/trace_sampled", None) is True

        # Verify outside span record does NOT have trace correlation attributes
        assert getattr(rec_outside, "logging.googleapis.com/trace", None) is None
        assert getattr(rec_outside, "logging.googleapis.com/spanId", None) is None
    finally:
        logger.removeHandler(handler)


def test_gen_ai_semantic_conventions(memory_exporter):
    """Verify OpenTelemetry GenAI semantic conventions on agent calls."""
    with trace_agent_call(
        "craft_adapter",
        model="gemini-3.1-pro",
        custom_key="custom_val",
    ) as span:
        set_span_token_usage(span, prompt_tokens=450, completion_tokens=180)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "agent.craft_adapter"
    assert span.attributes[GEN_AI_SYSTEM] == "gemini"
    assert span.attributes[GEN_AI_REQUEST_MODEL] == "gemini-3.1-pro"
    assert span.attributes[GEN_AI_OPERATION_NAME] == "generate_content"
    assert span.attributes[GEN_AI_USAGE_PROMPT_TOKENS] == 450
    assert span.attributes[GEN_AI_USAGE_COMPLETION_TOKENS] == 180
    assert span.attributes["custom_key"] == "custom_val"


async def test_async_context_manager(memory_exporter):
    """Verify async with support for trace_stage and trace_agent_call."""
    async with trace_stage("stage.composite_pdf", session_id="async-sess"):
        await asyncio.sleep(0.01)
        async with trace_agent_call("pdf_agent", model="gemini-3.5-flash"):
            await asyncio.sleep(0.01)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 2
    names = [s.name for s in spans]
    assert "agent.pdf_agent" in names
    assert "stage.composite_pdf" in names


async def test_parallel_tasks_trace_propagation(memory_exporter):
    """Verify asyncio parallel tasks inherit stage span context."""
    async def chunk_task(idx: int):
        with trace_agent_call(f"chunk_worker_{idx}", model="gemini-3.5-flash"):
            await asyncio.sleep(0.01)

    with trace_stage("stage.story_adapter", session_id="sess-parallel"):
        tasks = [chunk_task(i) for i in range(3)]
        await asyncio.gather(*tasks)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 4
    stage_s = next(s for s in spans if s.name == "stage.story_adapter")
    worker_spans = [s for s in spans if s.name.startswith("agent.chunk_worker_")]
    assert len(worker_spans) == 3
    for w in worker_spans:
        assert w.parent.span_id == stage_s.context.span_id
        assert w.context.trace_id == stage_s.context.trace_id


def test_hermetic_fallback_empty_project_id():
    """Verify init_tracing with empty project_id initializes hermetic local TracerProvider."""
    init_tracing("")
    tracer = trace.get_tracer("storybook")
    with tracer.start_as_current_span("test.local_span") as span:
        assert span is not None


def test_make_trace_url():
    """Verify make_trace_url produces expected Cloud Trace console URL."""
    set_project_id("my-storybook-proj")
    trace_id = 0x1234567890ABCDEF1234567890ABCDEF
    url = make_trace_url(trace_id)
    assert url == (
        "https://console.cloud.google.com/traces/list?"
        "tid=1234567890abcdef1234567890abcdef&project=my-storybook-proj"
    )


def test_gcp_json_formatter(memory_exporter):
    """Verify JSON output structure, severity mapping, timestamp, and trace/spanId injection."""
    formatter = GcpJsonFormatter(project_id="test-proj")

    # 1. Standard log record formatting
    record = logging.LogRecord(
        name="storybook.test",
        level=logging.INFO,
        pathname="/tmp/app.py",
        lineno=42,
        msg="Processing story for session %s",
        args=("sess-001",),
        exc_info=None,
        func="test_func",
    )
    output = formatter.format(record)
    data = json.loads(output)

    assert data["severity"] == "INFO"
    assert data["message"] == "Processing story for session sess-001"
    assert "time" in data
    dt = datetime.datetime.fromisoformat(data["time"])
    assert dt.tzinfo is not None
    assert data["logging.googleapis.com/sourceLocation"]["file"] == "/tmp/app.py"
    assert data["logging.googleapis.com/sourceLocation"]["line"] == 42
    assert data["logging.googleapis.com/sourceLocation"]["function"] == "test_func"

    # 2. Severity mappings
    for py_level, gcp_sev in [
        (logging.DEBUG, "DEBUG"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ]:
        rec = logging.LogRecord("test", py_level, "app.py", 1, "msg", (), None)
        assert json.loads(formatter.format(rec))["severity"] == gcp_sev

    # 3. Active span trace & spanId injection
    with trace_stage("stage.test_active_span", session_id="sess-active"):
        span_rec = logging.LogRecord("test", logging.INFO, "app.py", 10, "trace test", (), None)
        span_data = json.loads(formatter.format(span_rec))
        assert "logging.googleapis.com/trace" in span_data
        assert span_data["logging.googleapis.com/trace"].startswith("projects/test-proj/traces/")
        assert "logging.googleapis.com/spanId" in span_data
        assert len(span_data["logging.googleapis.com/spanId"]) == 16
        assert "logging.googleapis.com/trace_sampled" in span_data

    # 4. Exception formatting
    try:
        raise ValueError("simulated pipeline error")
    except ValueError:
        exc_info = sys.exc_info()

    err_rec = logging.LogRecord("test", logging.ERROR, "app.py", 99, "Stage failed", (), exc_info)
    err_data = json.loads(formatter.format(err_rec))
    assert "exception" in err_data
    assert "ValueError: simulated pipeline error" in err_data["exception"]


def test_gutenberg_fetch_spans(memory_exporter, monkeypatch):
    """Mock httpx in gutenberg.py and verify gutenberg.search and download attempt spans."""
    search_payload = {
        "results": [
            {
                "id": 11,
                "title": "Alice's Adventures in Wonderland",
                "authors": [{"name": "Carroll, Lewis"}],
                "formats": {
                    "text/plain; charset=utf-8": "https://www.gutenberg.org/files/11/11-0.txt",
                },
            }
        ]
    }
    book_text = (
        "*** START OF THE PROJECT GUTENBERG EBOOK ALICE ***\n"
        "Down the rabbit hole.\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK ALICE ***"
    )

    def mock_get(url, *args, **kwargs):
        class MockResponse:
            def __init__(
                self,
                status_code: int,
                content: bytes,
                text: str | None = None,
                json_data: Any = None,
            ):
                self.status_code = status_code
                self.content = content
                self._text = text or content.decode("utf-8")
                self._json = json_data

            @property
            def text(self) -> str:
                return self._text

            def json(self) -> Any:
                return self._json

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    req = httpx.Request("GET", url)
                    resp = httpx.Response(self.status_code, request=req)
                    raise httpx.HTTPStatusError("HTTP error", request=req, response=resp)

        if "gutendex.com" in url or "books" in url:
            return MockResponse(200, b"", text="", json_data=search_payload)
        return MockResponse(200, book_text.encode("utf-8"), text=book_text)

    monkeypatch.setattr("storybook.tools.gutenberg.httpx.get", mock_get)

    # 1. Search spans
    results = search_gutenberg("Alice in Wonderland")
    assert len(results) == 1
    assert results[0]["id"] == 11

    spans = memory_exporter.get_finished_spans()
    search_spans = [s for s in spans if s.name == "gutenberg.search"]
    assert len(search_spans) == 1
    search_span = search_spans[0]
    assert search_span.attributes["search.query"] == "Alice in Wonderland"
    assert search_span.attributes["search.backend"] == "gutendex"
    assert search_span.attributes["search.results_count"] == 1
    assert "search.duration_ms" in search_span.attributes

    # 2. Fetch and download attempt spans
    text = fetch_gutenberg_url("https://www.gutenberg.org/files/11/11-0.txt")
    assert "Down the rabbit hole." in text

    spans = memory_exporter.get_finished_spans()
    fetch_spans = [s for s in spans if s.name == "gutenberg.fetch"]
    assert len(fetch_spans) == 1
    fetch_span = fetch_spans[0]
    assert fetch_span.attributes["gutenberg.url"] == "https://www.gutenberg.org/files/11/11-0.txt"
    assert fetch_span.attributes["gutenberg.book_id"] == "11"

    download_spans = [s for s in spans if s.name == "gutenberg.download_attempt"]
    assert len(download_spans) == 1
    download_span = download_spans[0]
    assert download_span.attributes["http.url"] == "https://www.gutenberg.org/files/11/11-0.txt"
    assert download_span.attributes["http.mirror_index"] == 1
    assert download_span.attributes["http.attempt"] == 1
    assert download_span.attributes["http.status_code"] == 200
    assert download_span.attributes["download.bytes"] == len(book_text.encode("utf-8"))
    assert "http.duration_ms" in download_span.attributes


def test_setup_logging():
    """Verify root logger gets GcpJsonFormatter without error and loggers propagate."""
    setup_logging("test-setup-proj", level=logging.DEBUG)

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert isinstance(handler.formatter, GcpJsonFormatter)
    assert handler.formatter.project_id == "test-setup-proj"

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "storybook"):
        assert logging.getLogger(logger_name).propagate is True


@pytest.mark.asyncio
async def test_list_sessions_overlays_active_in_memory():
    """Verify that list_sessions_route includes active running sessions from _sessions with live progress."""
    from unittest.mock import AsyncMock, patch
    from storybook.models import PipelineState, SessionConfig, SourceConfig
    from storybook.api.routes import _sessions, list_sessions_route

    sid = "test-active-overlay"
    state = PipelineState(
        session_id=sid,
        config=SessionConfig(source=SourceConfig(title="Test Book", author="Test Author")),
        current_stage="adapting_text",
        progress_pct=35,
    )
    _sessions[sid] = state
    try:
        with patch("storybook.api.routes.store.list_sessions", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = []
            results = await list_sessions_route()
            active = next((r for r in results if r.session_id == sid), None)
            assert active is not None
            assert active.current_stage == "adapting_text"
            assert active.progress_pct == 35
    finally:
        _sessions.pop(sid, None)
