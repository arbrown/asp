"""A2A JSON-RPC 2.0 router and /.well-known/agent.json endpoint with WASD A2UI support."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from storybook.a2a.a2ui import (
    A2UI_MIME_TYPE,
    GEMINI_ENTERPRISE_CATALOG_ID,
    STUDIO_AGE_OPTIONS,
    STUDIO_PAGE_OPTIONS,
    build_download_chip_update,
    build_message_parts,
    build_progress_surface_init,
    build_stage_update,
    build_studio_surface,
    default_studio_options_for_book,
    ensure_studio_state,
    normalize_config_fields,
)
from storybook.a2a.sink import (
    DEFAULT_PUBLIC_BASE_URL,
    format_jsonrpc_sse,
    get_public_asset_url,
    make_task_artifact_event,
    make_task_status_event,
    stream_session_a2ui_events,
)
from storybook.api import routes as api_routes
from storybook.config import settings
from storybook.models import PipelineState, SessionConfig, SourceConfig
from storybook.substrate import ate
from storybook.tools import gcs

log = logging.getLogger(__name__)

a2a_router = APIRouter()

# Optional in-memory queues keyed by session_id for live in-process streaming or testing
SESSION_A2UI_QUEUES: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}

# Per-contextId / surfaceId Creative Studio state memory for multi-turn Gemini Enterprise chats
_studio_contexts: dict[str, dict[str, Any]] = {}
_a2a_sessions: dict[str, PipelineState] = {}

FALLBACK_LUCKY_DEFAULTS: dict[str, Any] = {
    "title": "Alice's Adventures in Wonderland",
    "author": "Lewis Carroll",
    "target_age": "6-8",
    "age_range": "6-8",
    "page_count": 12,
    "image_spec": "Mid-century gouache picture-book illustration with warm Risograph textures.",
    "style": "Mid-century gouache picture-book illustration with warm Risograph textures.",
    "text_spec": "Playful, wry read-aloud narrator with whimsical logic puzzles and brisk dialogue.",  # noqa: E501
    "characters": "Playful, wry read-aloud narrator with whimsical logic puzzles and brisk dialogue.",  # noqa: E501
    "custom_instructions": "Foreground whimsical scale shifts and a recurring pocket-watch motif.",
    "freeform_notes": "Foreground whimsical scale shifts and a recurring pocket-watch motif.",
}


class StyleChoiceSchema(BaseModel):
    label: str = Field(
        description="Short chip label (e.g. '19th-Century Russian Woodblock — Stark carbon ink & ochre shadows')"  # noqa: E501
    )
    value: str = Field(
        description="Detailed visual or narrative direction for the storybook pipeline (image_spec or text_spec)"  # noqa: E501
    )


class CreativeStudioResponseSchema(BaseModel):
    conversational_reply: str = Field(
        description=(
            "2-3 engaging paragraphs discussing why this work is fascinating to adapt as a children's "  # noqa: E501
            "picture book, explaining the proposed visual directions and text styles, and inviting the "  # noqa: E501
            "user to pick from the card below or ask for adjustments."
        )
    )
    title: str = Field(description="The book title (e.g. 'Demons')")
    author: str = Field(description="The author (e.g. 'Fyodor Dostoevsky')")
    art_style_options: list[StyleChoiceSchema] = Field(
        description="4 distinct, vivid, tailored art style choices for image_spec"
    )
    text_style_options: list[StyleChoiceSchema] = Field(
        description="4 distinct, clever narrative/text style choices for text_spec"
    )
    recommended_age: str = Field(description="One of '4-5', '6-8', '8-10', '10-12'")
    recommended_pages: str = Field(description="One of '12', '16', '24'")
    suggested_motif: str = Field(
        description="A clever recurring visual motif or custom instruction tailored to the book"
    )
    ready_to_generate: bool = Field(
        default=False,
        description=(
            "True ONLY if the user's message explicitly commands starting/running/generating the book "  # noqa: E501
            "right now; False for brainstorming, asking for ideas, configuring, or refining."
        ),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _base_url_from_request(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme or "https"
        if "pushkin-agent.duckdns.org" in forwarded_host:
            scheme = "https"
        return f"{scheme}://{forwarded_host}"
    base = str(request.base_url).rstrip("/")
    if base.endswith("/a2a"):
        base = base[:-4]
    if "pushkin-agent.duckdns.org" in base and base.startswith("http://"):
        base = "https://" + base[len("http://") :]
    return base or DEFAULT_PUBLIC_BASE_URL


def build_agent_card(base_url: str = "http://localhost:8000") -> dict[str, Any]:
    """Build the A2A AgentCard advertising streaming and A2UI capabilities."""
    clean_base = base_url.rstrip("/")
    return {
        "name": "Agent Storybook Studio",
        "description": (
            "Interactive Storybook Creative Director & GKE Substrate publisher that collaborates "
            "on visual art styles and narrative voices before illustrating print-ready PDF storybooks."  # noqa: E501
        ),
        "url": f"{clean_base}/a2a",
        "version": "0.2.0",
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        " defaultInputModes": ["text", "text/plain", "application/json", A2UI_MIME_TYPE],
        "defaultInputModes": ["text", "text/plain", "application/json", A2UI_MIME_TYPE],
        "defaultOutputModes": ["text", "text/plain", "application/json", A2UI_MIME_TYPE],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
            "extensions": [
                {
                    "uri": "https://a2ui.org/a2a-extension/a2ui/v0.8",
                    "description": "Supports A2UI interactive surfaces in Gemini Enterprise",
                    "required": False,
                    "params": {
                        "supportedCatalogIds": [GEMINI_ENTERPRISE_CATALOG_ID],
                        "acceptsInlineCatalogs": True,
                    },
                },
                {
                    "uri": "https://a2ui.org/a2a-extension/a2ui/v0.9",
                    "description": "Supports A2UI v0.9 surfaces in Gemini Enterprise",
                    "required": False,
                    "params": {
                        "supportedCatalogIds": [GEMINI_ENTERPRISE_CATALOG_ID],
                        "acceptsInlineCatalogs": True,
                    },
                },
            ],
        },
        "skills": [
            {
                "id": "generate_storybook",
                "name": "Storybook Creative Studio & Generator",
                "description": (
                    "Brainstorm tailored visual art styles and clever narrative voices for any classic "  # noqa: E501
                    "work using an interactive A2UI Creative Studio card, then generate the illustrated "  # noqa: E501
                    "storybook PDF on GKE."
                ),
                "tags": ["storybook", "a2ui", "illustration", "pdf", "children"],
                "examples": [
                    "Help me come up with some good storybook art styles and clever text styles for adapting Demons by Dostoevsky",  # noqa: E501
                    "Give me darker woodcut options and make it for ages 10-12",
                    "I'm Feeling Lucky — surprise me with an unusual classic adaptation",
                ],
            }
        ],
    }


@a2a_router.get("/.well-known/agent.json")
@a2a_router.get("/a2a/.well-known/agent.json")
async def get_agent_card(request: Request) -> JSONResponse:
    """Return the A2A AgentCard for Gemini Enterprise discovery."""
    return JSONResponse(content=build_agent_card(_base_url_from_request(request)))


@a2a_router.get("/a2a/sessions/{session_id}/images/{filename}")
async def get_a2a_session_image(session_id: str, filename: str) -> Response:
    """Serve session reference or spread image bytes for A2UI cards without IAP redirect."""
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    data = gcs.load_image(session_id, filename)
    if data is None:
        # Support shorthand spread_1.png -> spread_01_img0.png or page_01.png
        m = re.match(r"^spread_(\d+)\.png$", filename)
        if m:
            idx = int(m.group(1))
            for candidate in (
                f"spread_{idx:02d}_img0.png",
                f"spread_{idx - 1:02d}_img0.png",
                f"page_{idx:02d}.png",
            ):
                data = gcs.load_image(session_id, candidate)
                if data is not None:
                    break
    if data is None:
        try:
            raw_bytes, content_type = gcs.read_blob(session_id, "images", filename)
            return Response(content=raw_bytes, media_type=content_type or "image/png")
        except Exception:
            raise HTTPException(status_code=404, detail=f"Image {filename} not found") from None
    return Response(content=data, media_type="image/png")


@a2a_router.get("/a2a/sessions/{session_id}/pdf")
async def get_a2a_session_pdf(session_id: str) -> Response:
    """Serve session final storybook PDF for A2UI download links without IAP redirect."""
    data = gcs.load_pdf(session_id, "storybook.pdf")
    if data is None:
        try:
            raw_bytes, _ = gcs.read_blob(session_id, "final", "storybook.pdf")
            data = raw_bytes
        except Exception:
            raise HTTPException(status_code=404, detail="Storybook PDF not found") from None
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="storybook-{session_id}.pdf"'},
    )


def _unwrap_context_value(val: Any) -> Any:
    """Recursively unwrap A2UI Button context values (valueMap, literalString, lists, dicts)."""
    if isinstance(val, list):
        if not val:
            return ""
        return _unwrap_context_value(val[0])
    if isinstance(val, dict):
        if "valueString" in val:
            return val["valueString"]
        if "literalString" in val:
            return val["literalString"]
        if "valueMap" in val and isinstance(val["valueMap"], list):
            for entry in val["valueMap"]:
                if isinstance(entry, dict) and ("valueString" in entry or "value" in entry):
                    return _unwrap_context_value(entry.get("valueString", entry.get("value")))
            return ""
        if "value" in val:
            return _unwrap_context_value(val["value"])
        if "0" in val:
            return _unwrap_context_value(val["0"])
        if "selections" in val:
            return _unwrap_context_value(val["selections"])
        if "path" in val and len(val) == 1:
            return ""
        for v in val.values():
            unwrapped = _unwrap_context_value(v)
            if unwrapped not in ("", None):
                return unwrapped
        return ""
    return val


def _extract_context_entries(raw_ctx: Any) -> dict[str, Any]:
    """Normalize A2UI action.context from either list[{key, value}] or dict[k, v]."""
    out: dict[str, Any] = {}
    if isinstance(raw_ctx, list):
        for item in raw_ctx:
            if isinstance(item, dict) and "key" in item:
                val = _unwrap_context_value(item.get("value"))
                if val not in ("", None):
                    out[str(item["key"])] = val
    elif isinstance(raw_ctx, dict):
        for k, v in raw_ctx.items():
            val = _unwrap_context_value(v)
            if val not in ("", None):
                out[str(k)] = val
    return out


def _extract_action_from_message(params: dict[str, Any]) -> tuple[str | None, dict[str, Any], str, str]:  # noqa: E501
    """Extract (action_name, action_context, surface_id, combined_text) from A2A JSON-RPC params."""
    msg = params.get("message") or {}
    parts = msg.get("parts") or params.get("parts") or []
    action_name: str | None = None
    action_ctx: dict[str, Any] = {}
    surface_id = str(params.get("surfaceId") or msg.get("surfaceId") or "storybook_studio")
    texts: list[str] = []

    def _inspect_action_container(container: dict[str, Any]) -> None:
        nonlocal action_name, surface_id
        if container.get("surfaceId"):
            surface_id = str(container["surfaceId"])
        for key in ("action", "userAction", "a2uiAction", "event"):
            act = container.get(key)
            if isinstance(act, dict):
                name = act.get("name") or act.get("actionName") or act.get("id")
                if name:
                    action_name = str(name)
                if act.get("surfaceId"):
                    surface_id = str(act["surfaceId"])
                action_ctx.update(_extract_context_entries(act.get("context")))
                for k, v in act.items():
                    if k not in ("name", "actionName", "id", "context", "surfaceId"):
                        unwrapped = _unwrap_context_value(v)
                        if unwrapped not in ("", None):
                            action_ctx.setdefault(k, unwrapped)
            elif isinstance(act, str) and act:
                action_name = act
                action_ctx.update(_extract_context_entries(container.get("context")))

    _inspect_action_container(params)
    if isinstance(params.get("metadata"), dict):
        _inspect_action_container(params["metadata"])
    _inspect_action_container(msg)
    if isinstance(msg.get("metadata"), dict):
        _inspect_action_container(msg["metadata"])

    for part in parts:
        if not isinstance(part, dict):
            continue
        p_kind = part.get("kind") or part.get("type")
        if p_kind == "text" or ("text" in part and "data" not in part):
            t = str(part.get("text") or "").strip()
            if t:
                # Check if text itself is a serialized JSON action payload from WASD
                if t.startswith("{") and ("action" in t or "userAction" in t):
                    try:
                        parsed_t = json.loads(t)
                        if isinstance(parsed_t, dict):
                            _inspect_action_container(parsed_t)
                            continue
                    except Exception:
                        pass
                texts.append(t)

        data = part.get("data")
        if isinstance(data, dict):
            _inspect_action_container(data)
            if not action_name and (data.get("actionName") or data.get("name")):
                action_name = str(data.get("actionName") or data.get("name"))
                action_ctx.update(_extract_context_entries(data.get("context")))
            for k in (
                "title",
                "author",
                "art_style",
                "image_spec",
                "style",
                "text_style",
                "text_spec",
                "characters",
                "age_range",
                "target_age",
                "page_count",
                "custom_instructions",
                "freeform_notes",
            ):
                if k in data:
                    val = _unwrap_context_value(data[k])
                    if val not in ("", None):
                        action_ctx.setdefault(k, val)

    combined_text = " ".join(texts).strip()
    if not action_name and combined_text:
        lower = combined_text.lower()
        if lower in ("im_feeling_lucky", "i'm feeling lucky", "feeling lucky"):
            action_name = "im_feeling_lucky"
        elif lower in ("shuffle_styles", "shuffle new style ideas"):
            action_name = "shuffle_styles"
        elif lower == "generate_storybook":
            action_name = "generate_storybook"

    return action_name, action_ctx, surface_id, combined_text


# Alias for backwards compatibility
_extract_action_and_text = _extract_action_from_message


def _get_stored_studio_state(
    context_id: str | None,
    task_id: str | None = None,
    surface_id: str | None = None,
) -> dict[str, Any] | None:
    for key in (context_id, task_id, surface_id, "__latest__"):
        if key and key in _studio_contexts:
            return _studio_contexts[key]
    return None


def _save_studio_state(
    state: dict[str, Any],
    context_id: str | None,
    task_id: str | None = None,
    surface_id: str | None = None,
) -> None:
    ensured = ensure_studio_state(state)
    for key in (context_id, task_id, surface_id, "__latest__"):
        if key:
            _studio_contexts[key] = ensured


def _is_explicit_generate_command(text: str) -> bool:
    """Return True ONLY when the user explicitly commands immediate book generation."""
    if not text:
        return False
    lower = text.lower().strip()
    # If the user is asking for ideas, styles, options, brainstorming, or help, never auto-start
    brainstorm_markers = (
        "help me",
        "come up with",
        "art style",
        "text style",
        "options",
        "ideas",
        "what style",
        "suggest",
        "brainstorm",
        "give me",
        "make it for",
        "change the",
        "darker",
        "lighter",
        "woodcut",
        "?",
    )
    if any(m in lower for m in brainstorm_markers):
        return False

    explicit_triggers = (
        "go ahead and generate",
        "generate the book now",
        "generate the storybook now",
        "start generating",
        "run the pipeline",
        "create the pdf now",
        "generate it now",
        "launch the storybook",
    )
    return any(t in lower for t in explicit_triggers)


def _extract_book_from_text_heuristic(
    text: str,
    prior_state: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Heuristic fallback for extracting (title, author) when offline or mocked."""
    lower = (text or "").lower()
    if "demon" in lower or "dostev" in lower or "dostoev" in lower or "besy" in lower:
        return "Demons", "Fyodor Dostoevsky"
    if "crime and punishment" in lower:
        return "Crime and Punishment", "Fyodor Dostoevsky"
    if "brothers karamazov" in lower:
        return "The Brothers Karamazov", "Fyodor Dostoevsky"
    if "moby" in lower or "melville" in lower:
        return "Moby-Dick", "Herman Melville"
    if "odyssey" in lower or "homer" in lower:
        return "The Odyssey", "Homer"
    if "alice" in lower or "wonderland" in lower or "carroll" in lower:
        return "Alice's Adventures in Wonderland", "Lewis Carroll"

    # Pattern: "adapting <Title> by <Author>" or "<Title> by <Author>"
    m = re.search(
        r"(?:adapting|adapt|book|for)\s+['\"]?([A-Z][\w\s'’-]{1,50}?)['\"]?\s+by\s+([A-Z][\w\s.-]{2,40})",  # noqa: E501
        text or "",
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    if prior_state and prior_state.get("title"):
        return str(prior_state["title"]), str(prior_state.get("author") or "Classic Author")

    return "Demons", "Fyodor Dostoevsky"


async def _run_lucky_generation(context: dict[str, Any]) -> dict[str, Any]:
    """Call _generate_lucky() with fallback to canonical defaults, then wrap as a full Studio state."""  # noqa: E501
    lucky: dict[str, Any] | None = None
    try:
        lucky = await asyncio.to_thread(api_routes._generate_lucky)
    except Exception as exc:
        log.debug("Using fallback lucky configuration (Vertex AI call failed): %s", exc)
        lucky = dict(FALLBACK_LUCKY_DEFAULTS)

    merged_raw = {**FALLBACK_LUCKY_DEFAULTS, **(lucky or {})}
    if context.get("age_range") or context.get("target_age"):
        age_override = str(context.get("age_range") or context.get("target_age"))
        merged_raw["age_range"] = age_override
        merged_raw["target_age"] = age_override
    if context.get("page_count"):
        merged_raw["page_count"] = context["page_count"]

    title = str(merged_raw.get("title") or "Alice's Adventures in Wonderland")
    author = str(merged_raw.get("author") or "Lewis Carroll")
    base_studio = default_studio_options_for_book(title, author)

    lucky_art = str(merged_raw.get("image_spec") or merged_raw.get("style") or "").strip()
    if lucky_art:
        short_lbl = lucky_art[:68] + ("..." if len(lucky_art) > 68 else "")
        base_studio["art_style_options"][0] = {
            "label": f"✨ Lucky Pick — {short_lbl}",
            "value": lucky_art,
        }
        base_studio["selected_art_style"] = lucky_art

    lucky_text = str(merged_raw.get("text_spec") or merged_raw.get("characters") or "").strip()
    if lucky_text:
        short_tlbl = lucky_text[:68] + ("..." if len(lucky_text) > 68 else "")
        base_studio["text_style_options"][0] = {
            "label": f"✨ Lucky Voice — {short_tlbl}",
            "value": lucky_text,
        }
        base_studio["selected_text_style"] = lucky_text

    base_studio["recommended_age"] = str(merged_raw.get("age_range") or merged_raw.get("target_age") or "6-8")  # noqa: E501
    base_studio["recommended_pages"] = str(merged_raw.get("page_count") or "12")
    if merged_raw.get("custom_instructions") or merged_raw.get("freeform_notes"):
        base_studio["suggested_motif"] = str(
            merged_raw.get("custom_instructions") or merged_raw.get("freeform_notes")
        )
    base_studio["conversational_reply"] = (
        f"🎲 **I'm Feeling Lucky!** I've selected **{title}** by **{author}** "
        f"(Ages {base_studio['recommended_age']}, {base_studio['recommended_pages']} pages).\n\n"
        f"**Visual Direction:** {base_studio['selected_art_style']}\n\n"
        f"**Narrative Voice:** {base_studio['selected_text_style']}\n\n"
        f"Review or switch any of the tailored options in the **Storybook Creative Studio** card below, "  # noqa: E501
        f"or click **🚀 Generate Storybook on GKE** to launch the illustration pipeline!"
    )
    return ensure_studio_state({**merged_raw, **base_studio})


async def _run_creative_director(
    user_text: str,
    prior_state: dict[str, Any] | None = None,
    action_name: str | None = None,
    action_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Gemini (gemini-2.5-flash on Vertex AI) to collaborate on tailored art and text styles."""  # noqa: E501
    action_ctx = action_ctx or {}
    if action_name == "im_feeling_lucky":
        return await _run_lucky_generation(action_ctx)

    fallback_title, fallback_author = _extract_book_from_text_heuristic(user_text, prior_state)
    if action_ctx.get("title"):
        fallback_title = str(action_ctx["title"]).strip()
    if action_ctx.get("author"):
        fallback_author = str(action_ctx["author"]).strip()

    def _call_vertex_gemini() -> dict[str, Any]:
        os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")
        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location="global",
        )
        prior_summary = ""
        if prior_state:
            prior_summary = (
                f"\nCurrent Studio State in this conversation:\n"
                f"- Book Title: {prior_state.get('title')}\n"
                f"- Author: {prior_state.get('author')}\n"
                f"- Current Age Range: {prior_state.get('recommended_age')}\n"
                f"- Current Page Count: {prior_state.get('recommended_pages')}\n"
                f"- Current Motif: {prior_state.get('suggested_motif')}\n"
            )
        if action_ctx:
            prior_summary += f"\nUser UI Selections / Context: {json.dumps(action_ctx)}\n"

        action_hint = ""
        if action_name == "shuffle_styles":
            action_hint = (
                f"\nThe user clicked 'Shuffle New Style Ideas' for '{fallback_title}' by '{fallback_author}'. "  # noqa: E501
                f"Propose 4 fresh, unexpected, vividly distinct visual art styles and 4 fresh narrative/text styles!"  # noqa: E501
            )

        prompt = (
            "You are the imaginative, literary **Storybook Creative Director** for Agent Storybook Studio on GKE.\n"  # noqa: E501
            "Your job is to collaborate with the user on adapting classic literature (including complex adult "  # noqa: E501
            "novels, plays, myths, or poems like Dostoevsky's 'Demons', 'Crime and Punishment', 'Moby-Dick', etc.) "  # noqa: E501
            "into unforgettably illustrated, age-appropriate children's picture books.\n\n"
            f"{prior_summary}{action_hint}\n"
            f"User message: {user_text or f'Propose creative storybook art and text styles for {fallback_title} by {fallback_author}.'}\n\n"  # noqa: E501
            "Return a structured JSON response matching the schema with:\n"
            "1. `conversational_reply`: 2-3 engaging, insightful paragraphs discussing why this specific work is "  # noqa: E501
            "fascinating to adapt as a children's picture book, explaining the 4 proposed visual art directions "  # noqa: E501
            "and 4 narrative/text styles, and inviting the user to choose from the interactive Storybook Creative "  # noqa: E501
            "Studio card below or ask for refinements.\n"
            "2. `title`: The canonical book title (correct any typos, e.g. 'Dostevsky' -> 'Fyodor Dostoevsky', 'Demons').\n"  # noqa: E501
            "3. `author`: The author's full name.\n"
            "4. `art_style_options`: Exactly 4 distinct, vivid visual art style choices tailored to this book. "  # noqa: E501
            "Each item MUST have a concise `label` (e.g. '19th-Century Russian Woodblock — Stark carbon ink & ochre shadows') "  # noqa: E501
            "and a rich 1-2 sentence `value` suitable as an image generation style prompt (`image_spec`).\n"  # noqa: E501
            "5. `text_style_options`: Exactly 4 distinct, clever narrative/text style choices tailored to this book. "  # noqa: E501
            "Each item MUST have a concise `label` (e.g. 'Lemony Snicket Deadpan Satire — Dry narrator & vocabulary asides') "  # noqa: E501
            "and a rich 1-2 sentence `value` suitable as a narrative voice specification (`text_spec`).\n"  # noqa: E501
            "6. `recommended_age`: One of '4-5', '6-8', '8-10', '10-12' (honor any age band requested by the user).\n"  # noqa: E501
            "7. `recommended_pages`: One of '12', '16', '24' (honor any page count requested by the user).\n"  # noqa: E501
            "8. `suggested_motif`: A clever recurring visual motif or custom instruction tailored to the book.\n"  # noqa: E501
            "9. `ready_to_generate`: Set to `false` for all brainstorming, style exploration, and refinement turns. "  # noqa: E501
            "Set to `true` ONLY if the user explicitly commands immediate generation right now."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                temperature=0.9,
                response_mime_type="application/json",
                response_schema=CreativeStudioResponseSchema,
            ),
        )
        return json.loads(response.text)

    try:
        raw_plan = await asyncio.to_thread(_call_vertex_gemini)
    except Exception as exc:
        log.warning("Gemini Creative Director call failed; using tailored fallback: %s", exc)
        raw_plan = default_studio_options_for_book(fallback_title, fallback_author)

    # Apply any explicit age/page overrides from user text or button context
    lower_text = (user_text or "").lower()
    for age_opt in STUDIO_AGE_OPTIONS:
        if age_opt in lower_text:
            raw_plan["recommended_age"] = age_opt
    if action_ctx.get("age_range"):
        raw_plan["recommended_age"] = str(action_ctx["age_range"])
    for pg_opt in STUDIO_PAGE_OPTIONS:
        if f"{pg_opt} page" in lower_text or f"{pg_opt}-page" in lower_text:
            raw_plan["recommended_pages"] = pg_opt
    if action_ctx.get("page_count"):
        raw_plan["recommended_pages"] = str(action_ctx["page_count"])

    raw_plan["ready_to_generate"] = bool(
        raw_plan.get("ready_to_generate", False) and _is_explicit_generate_command(user_text)
    )
    return ensure_studio_state(raw_plan)


def _build_task_response(
    task_id: str,
    state: str,
    text: str,
    envelopes: list[dict[str, Any]],
    context_id: str | None = None,
) -> dict[str, Any]:
    ctx_id = context_id or task_id
    parts = build_message_parts(text, envelopes)
    return {
        "id": task_id,
        "contextId": ctx_id,
        "kind": "task",
        "status": {
            "state": state,
            "timestamp": _now(),
            "message": {
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "parts": parts,
            },
        },
        "artifacts": [
            {
                "artifactId": f"studio-{task_id}",
                "name": "Storybook Creative Studio",
                "parts": parts,
            }
        ],
    }


async def _create_and_start_storybook_session(
    cfg_fields: dict[str, Any],
    session_id: str | None = None,
    studio_state: dict[str, Any] | None = None,
) -> PipelineState:
    """Create a PipelineState session, persist to DB/GCS, and provision the Substrate actor."""
    norm = normalize_config_fields(cfg_fields, studio_state=studio_state)
    sid = session_id or str(uuid.uuid4())[:8]

    target_age = norm["target_age"]
    try:
        page_count = int(norm["page_count"])
    except ValueError:
        page_count = 12

    config = SessionConfig(
        source=SourceConfig(
            title=norm["title"],
            author=norm["author"],
        ),
        target_age=target_age,
        page_count=page_count,
        text_spec=norm["text_spec"] or None,
        image_spec=norm["image_spec"] or None,
        custom_instructions=norm["custom_instructions"] or None,
    )

    state = PipelineState(
        session_id=sid,
        config=config,
        current_stage="initializing",
        created_at=_now(),
    )

    _a2a_sessions[sid] = state
    

    try:
        await asyncio.to_thread(gcs.save_pipeline_state, sid, state)
    except Exception:
        pass

    try:
        await api_routes._persist_session(state)
    except Exception:
        pass

    try:
        await asyncio.to_thread(gcs.save_progress_events, sid, [], False)
    except Exception:
        pass

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


async def _stream_interactive_studio_turn(
    rpc_id: str | int | None,
    task_id: str,
    context_id: str,
    surface_id: str,
    user_text: str,
    action_name: str | None,
    action_ctx: dict[str, Any],
    prior_state: dict[str, Any] | None,
) -> AsyncIterator[str]:
    """Stream the WASD SSE sequence for an interactive Creative Studio turn and close with final: True."""  # noqa: E501
    # Frame 1: submitted (final: False)
    yield format_jsonrpc_sse(
        rpc_id,
        make_task_status_event(
            task_id=task_id,
            context_id=context_id,
            state="submitted",
            final=False,
        ),
    )

    # Frame 2: working (final: False)
    yield format_jsonrpc_sse(
        rpc_id,
        make_task_status_event(
            task_id=task_id,
            context_id=context_id,
            state="working",
            final=False,
        ),
    )

    studio_state = await _run_creative_director(
        user_text=user_text,
        prior_state=prior_state,
        action_name=action_name,
        action_ctx=action_ctx,
    )
    _save_studio_state(studio_state, context_id=context_id, task_id=task_id, surface_id=surface_id)

    envelopes = build_studio_surface(surface_id=surface_id, studio_state=studio_state)
    reply_text = studio_state["conversational_reply"]

    # Frame 3: status-update (state="working", final=False) with [text_part, beginRendering, surfaceUpdate, dataModelUpdate]  # noqa: E501
    yield format_jsonrpc_sse(
        rpc_id,
        make_task_status_event(
            task_id=task_id,
            context_id=context_id,
            state="working",
            final=False,
            text=reply_text,
            envelopes=envelopes,
        ),
    )

    # Frame 4: artifact-update (lastChunk=True) with [text_part, beginRendering, surfaceUpdate, dataModelUpdate]  # noqa: E501
    yield format_jsonrpc_sse(
        rpc_id,
        make_task_artifact_event(
            task_id=task_id,
            context_id=context_id,
            text=reply_text,
            envelopes=envelopes,
            artifact_id=f"studio-{surface_id}",
            name=f"Storybook Creative Studio: {studio_state['title']}",
            last_chunk=True,
        ),
    )

    # Frame 5: status-update (state="completed", final=True) so WASD mounts the interactive A2UI Card immediately!  # noqa: E501
    yield format_jsonrpc_sse(
        rpc_id,
        make_task_status_event(
            task_id=task_id,
            context_id=context_id,
            state="completed",
            final=True,
            text=reply_text,
            envelopes=envelopes,
        ),
    )


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
        action_name, action_ctx, surface_id, text = _extract_action_from_message(params)
        task_id = str(
            params.get("taskId")
            or params.get("id")
            or (params.get("message") or {}).get("taskId")
            or uuid.uuid4()
        )
        context_id = str(
            params.get("contextId")
            or (params.get("message") or {}).get("contextId")
            or task_id
        )
        prior_state = _get_stored_studio_state(context_id, task_id=task_id, surface_id=surface_id)

        if action_name == "generate_storybook" or (
            action_name is None and _is_explicit_generate_command(text)
        ):
            merged_ctx = {**(prior_state or {}), **action_ctx}
            state = await _create_and_start_storybook_session(
                merged_ctx,
                session_id=params.get("sessionId") or params.get("taskId"),
                studio_state=prior_state,
            )
            trace_url = f"{base_url}/api/sessions/{state.session_id}/trace"
            envelopes = build_progress_surface_init(
                surface_id=f"storybook_run_{state.session_id}",
                title=state.config.source.title,
                author=state.config.source.author,
                trace_url=trace_url,
            )
            task = _build_task_response(
                task_id=state.session_id,
                state="working",
                text=f"🚀 Started storybook generation for '{state.config.source.title}' (session {state.session_id}).",  # noqa: E501
                envelopes=envelopes,
                context_id=context_id,
            )
            return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": task})

        studio_state = await _run_creative_director(
            user_text=text,
            prior_state=prior_state,
            action_name=action_name,
            action_ctx=action_ctx,
        )
        _save_studio_state(studio_state, context_id=context_id, task_id=task_id, surface_id=surface_id)  # noqa: E501
        envelopes = build_studio_surface(surface_id=surface_id, studio_state=studio_state)
        task = _build_task_response(
            task_id=task_id,
            state="input-required",
            text=studio_state["conversational_reply"],
            envelopes=envelopes,
            context_id=context_id,
        )
        return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": task})

    if method in ("message/stream", "tasks/sendSubscribe"):
        action_name, action_ctx, surface_id, text = _extract_action_from_message(params)
        explicit_session_id = params.get("sessionId")
        task_id = str(
            explicit_session_id
            or params.get("taskId")
            or params.get("id")
            or (params.get("message") or {}).get("taskId")
            or uuid.uuid4()
        )
        context_id = str(
            params.get("contextId")
            or (params.get("message") or {}).get("contextId")
            or task_id
        )
        prior_state = _get_stored_studio_state(context_id, task_id=task_id, surface_id=surface_id)

        # Determine whether this stream call should execute/attach to the storybook pipeline
        # vs. return an interactive Creative Studio turn.
        existing_active_session = bool(
            explicit_session_id
            and (
                explicit_session_id in SESSION_A2UI_QUEUES
                or explicit_session_id in _a2a_sessions
            )
            and action_name is None
            and not text
        )
        should_run_pipeline = (
            action_name == "generate_storybook"
            or (action_name is None and _is_explicit_generate_command(text))
            or existing_active_session
        )

        if not should_run_pipeline:
            return StreamingResponse(
                _stream_interactive_studio_turn(
                    rpc_id=rpc_id,
                    task_id=task_id,
                    context_id=context_id,
                    surface_id=surface_id,
                    user_text=text,
                    action_name=action_name,
                    action_ctx=action_ctx,
                    prior_state=prior_state,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Pipeline execution path: start or attach to storybook session
        session_id = str(explicit_session_id or "").strip()
        if not session_id or (
            session_id not in SESSION_A2UI_QUEUES and session_id not in _a2a_sessions
        ):
            merged_ctx = {**(prior_state or {}), **action_ctx}
            state = await _create_and_start_storybook_session(
                merged_ctx,
                session_id=session_id or None,
                studio_state=prior_state,
            )
            session_id = state.session_id
            title = state.config.source.title
            author = state.config.source.author
        else:
            st = _a2a_sessions.get(session_id)
            title = st.config.source.title if st else str(action_ctx.get("title") or "Storybook")
            author = st.config.source.author if st else str(action_ctx.get("author") or "")

        queue = SESSION_A2UI_QUEUES.get(session_id)
        return StreamingResponse(
            stream_session_a2ui_events(
                session_id=session_id,
                rpc_id=rpc_id,
                base_url=base_url,
                title=title,
                author=author,
                context_id=context_id,
                queue=queue,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if method == "tasks/get":
        task_id = params.get("id") or params.get("taskId")
        if not task_id:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "Missing task id"},
                },
            )
        state = _a2a_sessions.get(task_id) or gcs.load_pipeline_state(task_id)
        if state is None:
            return JSONResponse(
                status_code=404,
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32001, "message": f"Task {task_id} not found"},
                },
            )

        title = state.config.source.title
        author = state.config.source.author
        surface_id = f"storybook_run_{task_id}"
        if state.current_stage == "done":
            pdf_url = get_public_asset_url(task_id, "pdf", "storybook.pdf", base_url=base_url)
            envelopes = build_download_chip_update(
                surface_id=surface_id,
                pdf_url=pdf_url,
                title=title,
                author=author,
            )
            a2a_state = "completed"
            summary = f"🎉 Storybook '{title}' is complete.\n\n[📥 Download Completed Storybook PDF]({pdf_url})"  # noqa: E501
        elif state.current_stage in ("error", "cancelled"):
            err_detail = "; ".join(state.errors) if state.errors else "Pipeline failed"
            envelopes = build_stage_update(
                surface_id,
                "error",
                "failed",
                detail=err_detail,
                title=title,
                author=author,
            )
            a2a_state = "failed"
            summary = f"Storybook failed: {err_detail}"
        else:
            envelopes = build_stage_update(
                surface_id,
                state.current_stage,
                "in_progress",
                title=title,
                author=author,
            )
            a2a_state = "working"
            summary = f"Storybook '{title}' is in stage '{state.current_stage}'."

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
