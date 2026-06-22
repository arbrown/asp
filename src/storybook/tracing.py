from __future__ import annotations

import logging

from opentelemetry import trace

log = logging.getLogger(__name__)

_project_id: str = ""


def init_tracing(project_id: str) -> None:
    global _project_id
    _project_id = project_id
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
        log.exception("Failed to initialize Cloud Trace — tracing disabled")


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("storybook")


def make_trace_url(trace_id: int) -> str:
    tid = format(trace_id, "032x")
    return f"https://console.cloud.google.com/traces/list?tid={tid}&project={_project_id}"
