from __future__ import annotations

import asyncio
import logging

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from storybook.agents.image_validator import (
    check as check_image_validation,
)
from storybook.agents.image_validator import (
    record_validation_result,
)
from storybook.tracing import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_COMPLETION_TOKENS,
    GEN_AI_USAGE_PROMPT_TOKENS,
    CloudLoggingTraceFilter,
    init_tracing,
    make_trace_url,
    set_project_id,
    set_span_token_usage,
    set_test_tracer_provider,
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
