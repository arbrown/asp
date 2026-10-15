"""A2UI v0.9 surface and component builders for Gemini Enterprise."""

from __future__ import annotations

from typing import Any

from storybook.models import ACTIVE_AGE_RANGES

GEMINI_ENTERPRISE_CATALOG_ID = (
    "https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json"
)
A2UI_MIME_TYPE = "application/json+a2ui"
A2UI_VERSION = "v0.9"

STAGE_LABELS: dict[str, str] = {
    "initializing": "Initializing storybook pipeline",
    "fetching": "Fetching & preparing classic source text",
    "adapting_text": "Adapting story for target age band",
    "planning_spreads": "Planning page spreads & visual layout",
    "extracting_bible": "Building character & world bible",
    "generating_image": "Illustrating storybook spreads",
    "image_generation": "Illustrating storybook spreads",
    "spread_image": "Illustrating storybook spreads",
    "composing_pdf": "Typesetting & composing print-ready PDF",
    "done": "Storybook complete",
    "error": "Pipeline encountered an error",
}


def wrap_a2ui_data_part(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap A2UI v0.9 message envelopes into an A2A DataPart."""
    data: dict[str, Any] = {
        "version": A2UI_VERSION,
        "envelopes": envelopes,
    }
    for env in envelopes:
        for k, v in env.items():
            if k == "version":
                continue
            if k not in data:
                data[k] = dict(v) if isinstance(v, dict) else v
            elif k == "updateComponents" and isinstance(data[k], dict) and isinstance(v, dict):
                merged_comps = list(data[k].get("components", []))
                existing_ids = {
                    c.get("id"): idx
                    for idx, c in enumerate(merged_comps)
                    if isinstance(c, dict) and "id" in c
                }
                for comp in v.get("components", []):
                    cid = comp.get("id") if isinstance(comp, dict) else None
                    if cid and cid in existing_ids:
                        merged_comps[existing_ids[cid]] = comp
                    else:
                        if cid:
                            existing_ids[cid] = len(merged_comps)
                        merged_comps.append(comp)
                data[k] = {**data[k], "components": merged_comps}
    return {
        "kind": "data",
        "mimeType": A2UI_MIME_TYPE,
        "metadata": {"mimeType": A2UI_MIME_TYPE},
        "data": data,
    }


def normalize_config_fields(raw: dict[str, Any] | None = None) -> dict[str, str]:
    """Normalize config or lucky output dict into canonical /config/* keys."""
    src = raw or {}
    age_range = str(src.get("age_range") or src.get("target_age") or "4-5")
    if age_range not in ACTIVE_AGE_RANGES:
        age_range = "4-5"
    return {
        "age_range": age_range,
        "title": str(src.get("title") or ""),
        "author": str(src.get("author") or ""),
        "style": str(src.get("style") or src.get("image_spec") or ""),
        "characters": str(src.get("characters") or src.get("text_spec") or ""),
        "freeform_notes": str(src.get("freeform_notes") or src.get("custom_instructions") or ""),
    }


def build_config_surface(
    surface_id: str = "storybook_config",
    defaults: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build A2UI v0.9 envelopes for the interactive Storybook configuration card."""
    cfg = normalize_config_fields(defaults)

    form_children = [
        "header_text",
        "age_select",
        "title_input",
        "author_input",
        "style_input",
        "characters_input",
        "notes_input",
        "actions_row",
    ]

    lucky_event = {
        "name": "im_feeling_lucky",
        "context": {
            "age_range": {"path": "/config/age_range"},
        },
    }

    generate_event = {
        "name": "generate_storybook",
        "context": {
            "age_range": {"path": "/config/age_range"},
            "title": {"path": "/config/title"},
            "author": {"path": "/config/author"},
            "style": {"path": "/config/style"},
            "characters": {"path": "/config/characters"},
            "freeform_notes": {"path": "/config/freeform_notes"},
        },
    }

    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": "Card",
            "child": "config_form_col",
            "children": form_children,
        },
        {
            "id": "config_form_col",
            "component": "Column",
            "children": form_children,
        },
        {
            "id": "header_text",
            "component": "Text",
            "text": "Storybook Agent — Configure Your Book",
            "variant": "h2",
        },
        {
            "id": "age_select",
            "component": "Select",
            "label": "Target Age Range",
            "value": {"path": "/config/age_range"},
            "options": [{"label": f"Ages {age}", "value": age} for age in ACTIVE_AGE_RANGES],
        },
        {
            "id": "title_input",
            "component": "TextField",
            "label": "Title",
            "value": {"path": "/config/title"},
        },
        {
            "id": "author_input",
            "component": "TextField",
            "label": "Author",
            "value": {"path": "/config/author"},
        },
        {
            "id": "style_input",
            "component": "TextField",
            "label": "Illustration Style",
            "value": {"path": "/config/style"},
        },
        {
            "id": "characters_input",
            "component": "TextField",
            "label": "Characters & Narrative Form",
            "value": {"path": "/config/characters"},
        },
        {
            "id": "notes_input",
            "component": "TextField",
            "label": "Freeform Notes",
            "value": {"path": "/config/freeform_notes"},
            "multiline": True,
        },
        {
            "id": "actions_row",
            "component": "Row",
            "children": ["lucky_btn", "generate_btn"],
        },
        {
            "id": "lucky_btn",
            "component": "Button",
            "label": "I'm Feeling Lucky",
            "variant": "secondary",
            "action": {
                "event": lucky_event,
                **lucky_event,
            },
        },
        {
            "id": "generate_btn",
            "component": "Button",
            "label": "Generate Storybook",
            "variant": "primary",
            "action": {
                "event": generate_event,
                **generate_event,
            },
        },
    ]

    return [
        {
            "version": A2UI_VERSION,
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": GEMINI_ENTERPRISE_CATALOG_ID,
            },
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": components,
            },
        },
        {
            "version": A2UI_VERSION,
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/config",
                "value": cfg,
                "paths": {f"/config/{k}": v for k, v in cfg.items()},
            },
        },
    ]


def build_lucky_data_model_update(
    surface_id: str = "storybook_config",
    lucky_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build an updateDataModel envelope populating /config/* from I'm Feeling Lucky."""
    cfg = normalize_config_fields(lucky_fields)
    return [
        {
            "version": A2UI_VERSION,
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/config",
                "value": cfg,
                "paths": {f"/config/{k}": v for k, v in cfg.items()},
            },
        }
    ]


def build_progress_surface_init(
    session_id: str,
    title: str | None = None,
) -> list[dict[str, Any]]:
    """Build createSurface + initial Canvas updateComponents for a running storybook session."""
    surface_id = f"storybook_run_{session_id}"
    heading = f"Storybook — {title}" if title else "Storybook Generation"
    status_text = (
        f"Initializing storybook pipeline for '{title}'..."
        if title
        else "Initializing storybook pipeline..."
    )

    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": "Canvas",
            "title": heading,
            "autoOpen": True,
            "child": "canvas_body",
            "children": ["stage_status_card", "spreads_column", "download_container"],
        },
        {
            "id": "canvas_body",
            "component": "Column",
            "children": ["stage_status_card", "spreads_column", "download_container"],
        },
        {
            "id": "stage_status_card",
            "component": "Card",
            "child": "stage_status_text",
            "children": ["stage_status_text"],
        },
        {
            "id": "stage_status_text",
            "component": "Text",
            "text": status_text,
            "variant": "body1",
        },
        {
            "id": "spreads_column",
            "component": "Column",
            "children": [],
        },
        {
            "id": "download_container",
            "component": "Column",
            "children": [],
        },
    ]

    return [
        {
            "version": A2UI_VERSION,
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": GEMINI_ENTERPRISE_CATALOG_ID,
            },
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": components,
            },
        },
    ]


def build_stage_update(
    session_id: str,
    stage: str,
    status: str,
    detail: str = "",
) -> list[dict[str, Any]]:
    """Build updateComponents envelope updating stage_status_text."""
    surface_id = f"storybook_run_{session_id}"
    label = STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize())
    text = f"{label} ({status})"
    if detail:
        text = f"{text} — {detail}"

    return [
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {
                        "id": "stage_status_text",
                        "component": "Text",
                        "text": text,
                        "stage": stage,
                        "status": status,
                    }
                ],
            },
        }
    ]


def build_spread_card_update(
    session_id: str,
    spread_index: int,
    image_url: str,
    caption: str = "",
    existing_spread_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build updateComponents adding a spread Card with Image + Text and updating spreads_column."""
    surface_id = f"storybook_run_{session_id}"
    card_id = f"spread_card_{spread_index}"
    img_id = f"spread_img_{spread_index}"
    caption_id = f"spread_caption_{spread_index}"

    spread_ids = list(existing_spread_ids or [])
    if card_id not in spread_ids:
        spread_ids.append(card_id)

    caption_text = caption or f"Spread {spread_index}"

    return [
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {
                        "id": card_id,
                        "component": "Card",
                        "children": [img_id, caption_id],
                    },
                    {
                        "id": img_id,
                        "component": "Image",
                        "url": image_url,
                        "altText": f"Spread {spread_index}",
                    },
                    {
                        "id": caption_id,
                        "component": "Text",
                        "text": caption_text,
                    },
                    {
                        "id": "spreads_column",
                        "component": "Column",
                        "children": spread_ids,
                    },
                ],
            },
        }
    ]


def build_download_chip_update(
    session_id: str,
    pdf_url: str,
    book_title: str = "Storybook",
) -> list[dict[str, Any]]:
    """Build updateComponents adding the final download PDF chip Button and completion status."""
    surface_id = f"storybook_run_{session_id}"
    clean_title = book_title or "Storybook"

    return [
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {
                        "id": "stage_status_text",
                        "component": "Text",
                        "text": f"Storybook complete — '{clean_title}' is ready to download.",
                        "stage": "done",
                        "status": "completed",
                    },
                    {
                        "id": "download_pdf_chip",
                        "component": "Button",
                        "label": f"Download {clean_title} (PDF)",
                        "icon": "download",
                        "variant": "chip",
                        "action": {
                            "openUrl": {"url": pdf_url},
                        },
                    },
                    {
                        "id": "download_container",
                        "component": "Column",
                        "children": ["download_pdf_chip"],
                    },
                ],
            },
        }
    ]
