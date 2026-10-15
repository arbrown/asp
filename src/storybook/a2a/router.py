"""A2A JSON-RPC 2.0 router and /.well-known/agent.json endpoint with A2UI v0.9 support."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from storybook.a2a.a2ui import (
    A2UI_MIME_TYPE,
    A2UI_VERSION,
    GEMINI_ENTERPRISE_CATALOG_ID,
    build_config_surface,
    build_download_chip_update,
    build_lucky_data_model_update,
    build_progress_surface_init,
    build_stage_update,
    normalize_config_fields,
    wrap_a2ui_data_part,
)
from storybook.a2a.sink import (
    get_signed_or_api_url,
    stream_session_a2ui_events,
)
from storybook.api import routes as api_routes
from storybook.config import settings
from storybook.models import ACTIVE_AGE_RANGES, PipelineState, SessionConfig, SourceConfig
from storybook.substrate import ate
from storybook.tools import gcs

log = logging.getLogger(__name__)

a2a_router = APIRouter()

# Optional in-memory queues keyed by session_id for live in-process streaming or testing
SESSION_A2UI_QUEUES: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}

FALLBACK_LUCKY_DEFAULTS: dict[str, Any] = {
    "title": "Alice's Adventures in Wonderland",
    "author": "Lewis Carroll",
    "target_age": "6-7",
    "age_range": "6-7",
    "page_count": 12,
    "image_spec": "Mid-century gouache picture-book illustration with warm Risograph textures.",
    "style": "Mid-century gouache picture-book illustration with warm Risograph textures.",
    "text_spec": "Alice, the White Rabbit, and the Cheshire Cat in playful rhythmic prose.",
    "characters": "Alice, the White Rabbit, and the Cheshire Cat in playful rhythmic prose.",
    "custom_instructions": "Foreground whimsical scale shifts and a recurring pocket-watch motif.",
    "freeform_notes": "Foreground whimsical scale shifts and a recurring pocket-watch motif.",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _base_url_from_request(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def build_agent_card(base_url: str = "http://localhost:8000") -> dict[str, Any]:
    """Build the A2A AgentCard advertising streaming and A2UI v0.9 capabilities."""
    clean_base = base_url.rstrip("/")
    return {
        "name": "Storybook Agent",
        "description": (
            "Adapts classic literature into illustrated, age-tailored children's storybooks "
            "with live spread previews and print-ready PDF composition."
        ),
        "url": f"{clean_base}/a2a",
        "version": "0.1.0",
        "protocolVersion": "0.2.5",
        "defaultInputModes": ["text/plain", "application/json", A2UI_MIME_TYPE],
        "defaultOutputModes": ["text/plain", "application/json", A2UI_MIME_TYPE],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
            "extensions": [
                {
                    "uri": "https://a2ui.org/specification/v0_9",
                    "version": A2UI_VERSION,
                    "required": False,
                    "description": "A2UI v0.9 declarative UI surfaces for Gemini Enterprise",
                    "params": {
                        "supportedCatalogIds": [GEMINI_ENTERPRISE_CATALOG_ID],
                        "mimeType": A2UI_MIME_TYPE,
                    },
                }
            ],
        },
        "skills": [
            {
                "id": "configure_storybook",
                "name": "Configure Storybook",
                "description": (
                    "Open an interactive A2UI configuration card to choose target age range, "
                    "title, author, illustration style, characters, and notes."
                ),
                "tags": ["storybook", "configuration", "a2ui"],
                "examples": ["Create a storybook", "Configure a children's book"],
            },
            {
                "id": "im_feeling_lucky",
                "name": "I'm Feeling Lucky",
                "description": (
                    "Auto-populate the storybook configuration with a creative classic "
                    "adaptation idea and unexpected illustration direction."
                ),
                "tags": ["storybook", "lucky", "a2ui"],
                "examples": ["I'm feeling lucky", "Surprise me with a classic book idea"],
            },
            {
                "id": "generate_storybook",
                "name": "Generate Storybook",
                "description": (
                    "Run the multi-agent storybook pipeline in a live A2UI Canvas, streaming "
                    "illustrated spreads and a downloadable print-ready PDF."
                ),
                "tags": ["storybook", "generation", "pdf", "a2ui"],
                "examples": ["Generate a storybook of Peter Pan for ages 6-7"],
            },
        ],
    }


@a2a_router.get("/.well-known/agent.json")
async def get_agent_card(request: Request) -> JSONResponse:
    """Return the A2A AgentCard for Gemini Enterprise discovery."""
    return JSONResponse(content=build_agent_card(_base_url_from_request(request)))


def _unwrap_context_value(val: Any) -> Any:
    if isinstance(val, dict):
        if "value" in val:
            return val["value"]
        if "literalString" in val:
            return val["literalString"]
        if "path" in val and len(val) == 1:
            return ""
    return val


def _extract_action_and_text(params: dict[str, Any]) -> tuple[str | None, dict[str, Any], str, str]:
    """Extract (action_name, action_context, surface_id, combined_text) from A2A JSON-RPC params."""
    message = params.get("message") or {}
    parts = message.get("parts") or []
    texts: list[str] = []
    action_name: str | None = None
    action_context: dict[str, Any] = {}
    surface_id = "storybook_config"

    # Check top-level or message metadata first
    for container in (params, params.get("metadata") or {}, message, message.get("metadata") or {}):
        if not isinstance(container, dict):
            continue
        act = container.get("action") or container.get("userAction") or container.get("event")
        if isinstance(act, str):
            action_name = act
        elif isinstance(act, dict):
            ev = act.get("event") if isinstance(act.get("event"), dict) else act
            action_name = action_name or ev.get("name") or act.get("actionName")
            raw_ctx = ev.get("context") or act.get("context") or {}
            if isinstance(raw_ctx, dict):
                action_context.update({k: _unwrap_context_value(v) for k, v in raw_ctx.items()})
            if act.get("surfaceId"):
                surface_id = str(act["surfaceId"])

    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind == "text" or "text" in part:
            t = str(part.get("text") or "").strip()
            if t:
                texts.append(t)
        if kind == "data" or "data" in part:
            data = part.get("data")
            if isinstance(data, dict):
                if data.get("surfaceId"):
                    surface_id = str(data["surfaceId"])
                act = (
                    data.get("action")
                    or data.get("userAction")
                    or data.get("event")
                    or (data if "name" in data else None)
                )
                if isinstance(act, str):
                    action_name = action_name or act
                elif isinstance(act, dict):
                    ev = act.get("event") if isinstance(act.get("event"), dict) else act
                    action_name = action_name or ev.get("name") or act.get("actionName")
                    raw_ctx = ev.get("context") or act.get("context") or {}
                    if isinstance(raw_ctx, dict):
                        action_context.update(
                            {k: _unwrap_context_value(v) for k, v in raw_ctx.items()}
                        )
                    if act.get("surfaceId"):
                        surface_id = str(act["surfaceId"])

    combined_text = " ".join(texts).strip()
    lower_text = combined_text.lower()
    if not action_name:
        if "feeling lucky" in lower_text or lower_text == "lucky":
            action_name = "im_feeling_lucky"

    return action_name, action_context, surface_id, combined_text


def _build_task_response(
    task_id: str,
    state: str,
    text: str,
    envelopes: list[dict[str, Any]],
    context_id: str | None = None,
) -> dict[str, Any]:
    ctx_id = context_id or task_id
    msg = {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "taskId": task_id,
        "contextId": ctx_id,
        "parts": [
            {"kind": "text", "text": text},
            wrap_a2ui_data_part(envelopes),
        ],
    }
    return {
        "kind": "task",
        "id": task_id,
        "contextId": ctx_id,
        "status": {
            "state": state,
            "timestamp": _now(),
            "message": msg,
        },
        "history": [msg],
    }


async def _run_lucky_generation(context: dict[str, Any]) -> dict[str, Any]:
    """Call _generate_lucky() with fallback to canonical defaults when Vertex AI is unavailable."""
    requested_age = str(context.get("age_range") or context.get("target_age") or "").strip()
    try:
        lucky_data = await asyncio.to_thread(api_routes._generate_lucky)
        if not isinstance(lucky_data, dict):
            lucky_data = dict(FALLBACK_LUCKY_DEFAULTS)
    except Exception as exc:
        log.debug("Using fallback lucky configuration (Vertex AI call failed): %s", exc)
        lucky_data = dict(FALLBACK_LUCKY_DEFAULTS)

    if requested_age in ACTIVE_AGE_RANGES:
        lucky_data["age_range"] = requested_age
        lucky_data["target_age"] = requested_age
    return lucky_data


async def _create_and_start_storybook_session(
    cfg_fields: dict[str, Any],
    session_id: str | None = None,
) -> PipelineState:
    """Create a PipelineState session, persist to DB/GCS, and provision the Substrate actor."""
    norm = normalize_config_fields(cfg_fields)
    title = norm["title"] or "Alice's Adventures in Wonderland"
    author = norm["author"] or "Lewis Carroll"

    session_cfg = SessionConfig(
        source=SourceConfig(title=title, author=author),
        target_age=norm["age_range"],
        page_count=int(cfg_fields.get("page_count") or 12),
        text_spec=norm["characters"] or None,
        image_spec=norm["style"] or None,
        custom_instructions=norm["freeform_notes"] or None,
    )
    state_kwargs: dict[str, Any] = {
        "config": session_cfg,
        "started_at": _now(),
    }
    if session_id:
        state_kwargs["session_id"] = session_id

    state = PipelineState(**state_kwargs)
    sid = state.session_id

    try:
        await api_routes._persist_session(state)
    except Exception:
        log.debug("Could not persist session %s via _persist_session", sid)

    try:
        await asyncio.to_thread(gcs.save_progress_events, sid, [], False)
    except Exception:
        log.debug("Could not initialize empty progress events in GCS for session %s", sid)

    try:
        await ate.create_actor(
            template=settings.substrate_template,
            atespace=settings.substrate_atespace,
            name=sid,
            resume=False,
        )
    except Exception as exc:
        log.debug("Substrate actor provisioning skipped/failed for session %s: %s", sid, exc)

    return state


@a2a_router.post("/a2a")
async def handle_a2a_rpc(request: Request):
    """Handle A2A JSON-RPC 2.0 requests (message/send, message/stream, tasks/get)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
        )

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}
    base_url = _base_url_from_request(request)

    if method == "message/send":
        action_name, action_ctx, surface_id, text = _extract_action_and_text(params)
        task_id = (
            params.get("taskId")
            or params.get("id")
            or (params.get("message") or {}).get("taskId")
            or str(uuid.uuid4())
        )
        context_id = (
            params.get("contextId") or (params.get("message") or {}).get("contextId") or task_id
        )

        if action_name == "im_feeling_lucky":
            lucky_fields = await _run_lucky_generation(action_ctx)
            envelopes = build_lucky_data_model_update(
                surface_id=surface_id,
                lucky_fields=lucky_fields,
            )
            norm = normalize_config_fields(lucky_fields)
            task = _build_task_response(
                task_id=task_id,
                state="input-required",
                text=(
                    f"Populated configuration with '{norm['title']}' by {norm['author']} "
                    f"(Ages {norm['age_range']}). Review or click Generate Storybook!"
                ),
                envelopes=envelopes,
                context_id=context_id,
            )
            return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": task})

        if action_name == "generate_storybook":
            state = await _create_and_start_storybook_session(
                action_ctx,
                session_id=params.get("sessionId") or params.get("taskId"),
            )
            sid = state.session_id
            title = state.config.source.title or "Storybook"
            envelopes = build_progress_surface_init(session_id=sid, title=title)
            task = _build_task_response(
                task_id=sid,
                state="working",
                text=f"Started generating storybook '{title}' (session {sid}).",
                envelopes=envelopes,
                context_id=sid,
            )
            return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": task})

        # Default: return the interactive Storybook configuration A2UI card
        envelopes = build_config_surface(surface_id=surface_id, defaults=action_ctx or None)
        task = _build_task_response(
            task_id=task_id,
            state="input-required",
            text=(
                'Configure your storybook below, or click "I\'m Feeling Lucky" '
                'for a creative classic adaptation.'
            ),
            envelopes=envelopes,
            context_id=context_id,
        )
        return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": task})

    if method == "message/stream":
        action_name, action_ctx, _, _ = _extract_action_and_text(params)
        session_id = (
            params.get("sessionId")
            or params.get("taskId")
            or params.get("id")
            or (params.get("message") or {}).get("taskId")
            or (params.get("message") or {}).get("contextId")
        )
        book_title = str(action_ctx.get("title") or params.get("title") or "Storybook")

        if action_name == "generate_storybook" or not session_id:
            state = await _create_and_start_storybook_session(
                action_ctx,
                session_id=session_id,
            )
            session_id = state.session_id
            if state.config.source.title:
                book_title = state.config.source.title

        queue = SESSION_A2UI_QUEUES.get(session_id)
        poll_interval = float(params.get("pollInterval") or 0.05)
        heartbeat_interval = float(params.get("heartbeatInterval") or 15.0)

        return StreamingResponse(
            stream_session_a2ui_events(
                session_id=session_id,
                rpc_id=rpc_id,
                book_title=book_title,
                base_url=base_url,
                queue=queue,
                poll_interval=poll_interval,
                heartbeat_interval=heartbeat_interval,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    if method == "tasks/get":
        task_id = str(params.get("id") or params.get("taskId") or params.get("sessionId") or "")
        if not task_id:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "Missing task id"},
                },
            )

        state = await api_routes._load_session_state(task_id, prefer_gcs=True)
        if state is None:
            return JSONResponse(
                status_code=404,
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32001, "message": f"Task {task_id} not found"},
                },
            )

        title = state.config.source.title or "Storybook"
        if state.current_stage == "done":
            pdf_url = get_signed_or_api_url(task_id, "pdf", "storybook.pdf", base_url=base_url)
            envelopes = build_download_chip_update(task_id, pdf_url=pdf_url, book_title=title)
            a2a_state = "completed"
            summary = f"Storybook '{title}' is complete."
        elif state.current_stage in ("error", "cancelled"):
            err_detail = "; ".join(state.errors) if state.errors else "Pipeline failed"
            envelopes = build_stage_update(task_id, "error", "failed", detail=err_detail)
            a2a_state = "failed"
            summary = f"Storybook failed: {err_detail}"
        else:
            envelopes = build_stage_update(
                task_id,
                state.current_stage,
                f"{state.progress_pct}%",
            )
            a2a_state = "working"
            summary = (
                f"Storybook '{title}' in progress ({state.current_stage}, {state.progress_pct}%)."
            )

        task = _build_task_response(
            task_id=task_id,
            state=a2a_state,
            text=summary,
            envelopes=envelopes,
            context_id=task_id,
        )
        return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": task})

    return JSONResponse(
        status_code=400,
        content={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        },
    )
