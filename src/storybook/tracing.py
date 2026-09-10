from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace

log = logging.getLogger(__name__)

_project_id: str = ""

# OpenTelemetry GenAI semantic conventions
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_USAGE_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
GEN_AI_USAGE_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"


class SpanContextManager:
    """Context manager supporting both synchronous (`with`) and asynchronous (`async with`)."""

    def __init__(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        tracer: trace.Tracer | None = None,
    ) -> None:
        self.name = name
        self.attributes = attributes or {}
        self.tracer = tracer
        self._cm: Any = None
        self.span: trace.Span | None = None

    def __enter__(self) -> trace.Span:
        tracer = self.tracer or get_tracer()
        self._cm = tracer.start_as_current_span(
            self.name,
            attributes=self.attributes,
        )
        self.span = self._cm.__enter__()
        return self.span

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        if self.span is not None and exc_val is None:
            if self.span.is_recording():
                status = getattr(self.span, "status", None)
                if (
                    status is not None
                    and getattr(status, "status_code", None) == trace.StatusCode.UNSET
                ):
                    self.span.set_status(trace.StatusCode.OK)
        if self._cm is not None:
            return self._cm.__exit__(exc_type, exc_val, exc_tb)
        return False

    async def __aenter__(self) -> trace.Span:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        return self.__exit__(exc_type, exc_val, exc_tb)


def trace_stage(stage_name: str, **attributes: Any) -> SpanContextManager:
    """Wrap a pipeline stage with proper span hierarchy and error recording."""
    return SpanContextManager(stage_name, attributes=attributes)


def trace_agent_call(
    agent_name: str,
    model: str | None = None,
    **attributes: Any,
) -> SpanContextManager:
    """Wrap model/agent calls with standard GenAI semantic conventions."""
    span_name = (
        agent_name
        if ("." in agent_name or agent_name.startswith("agent."))
        else f"agent.{agent_name}"
    )
    system = attributes.pop(GEN_AI_SYSTEM, "gemini")
    operation_name = attributes.pop(GEN_AI_OPERATION_NAME, "generate_content")
    span_attrs: dict[str, Any] = {
        GEN_AI_SYSTEM: system,
        GEN_AI_OPERATION_NAME: operation_name,
        "agent.name": agent_name,
    }
    if model:
        span_attrs[GEN_AI_REQUEST_MODEL] = model
    span_attrs.update(attributes)
    return SpanContextManager(span_name, attributes=span_attrs)


def trace_retry_attempt(
    operation_name: str,
    attempt: int = 1,
    max_attempts: int | None = None,
    **attributes: Any,
) -> SpanContextManager:
    """Wrap retry attempts and attach validation/rejection details."""
    span_attrs: dict[str, Any] = {
        "retry.attempt": attempt,
        "retry.operation": operation_name,
    }
    if max_attempts is not None:
        span_attrs["retry.max_attempts"] = max_attempts
    span_attrs.update(attributes)
    return SpanContextManager(operation_name, attributes=span_attrs)


def set_span_token_usage(
    span: trace.Span | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """Attach token usage metrics to a span if available."""
    target_span = span or trace.get_current_span()
    if target_span is not None and target_span.is_recording():
        if prompt_tokens is not None:
            target_span.set_attribute(GEN_AI_USAGE_PROMPT_TOKENS, prompt_tokens)
        if completion_tokens is not None:
            target_span.set_attribute(GEN_AI_USAGE_COMPLETION_TOKENS, completion_tokens)


class CloudLoggingTraceFilter(logging.Filter):
    """Logging filter that extracts active OpenTelemetry span context and attaches GCP trace fields.
    """

    def __init__(self, project_id: str | None = None) -> None:
        super().__init__()
        self.project_id = project_id

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        if span is not None:
            ctx = span.get_span_context()
            if ctx is not None and ctx.is_valid:
                proj = self.project_id or _project_id or "default"
                trace_id_hex = format(ctx.trace_id, "032x")
                span_id_hex = format(ctx.span_id, "016x")
                setattr(
                    record,
                    "logging.googleapis.com/trace",
                    f"projects/{proj}/traces/{trace_id_hex}",
                )
                setattr(record, "logging.googleapis.com/spanId", span_id_hex)
                setattr(record, "logging.googleapis.com/trace_sampled", True)
        return True


def init_tracing(project_id: str = "") -> None:
    """Initialize OpenTelemetry tracing with Cloud Trace or local hermetic fallback."""
    global _project_id
    _project_id = project_id
    if not project_id:
        log.info("No project_id provided; using local hermetic TracerProvider")
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
        trace.set_tracer_provider(provider)
        return

    try:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter(project_id=project_id))
        )
        trace.set_tracer_provider(provider)
        log.info("Cloud Trace initialized for project %s", project_id)
    except Exception:
        log.warning("Failed to initialize Cloud Trace — falling back to local TracerProvider")
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
        trace.set_tracer_provider(provider)


def set_test_tracer_provider(
    provider: Any | None = None,
) -> Any:
    """Helper so unit tests can plug in an InMemorySpanExporter hermetically."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    if hasattr(trace, "_TRACER_PROVIDER_SET_ONCE"):
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE._done = False

    exporter = InMemorySpanExporter()
    target_provider = provider if provider is not None else TracerProvider()
    target_provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(target_provider)
    return exporter


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("storybook")


def set_project_id(project_id: str) -> None:
    global _project_id
    _project_id = project_id


def get_project_id() -> str:
    return _project_id


def make_trace_url(trace_id: int) -> str:
    tid = format(trace_id, "032x")
    proj = _project_id or "default"
    return f"https://console.cloud.google.com/traces/list?tid={tid}&project={proj}"
