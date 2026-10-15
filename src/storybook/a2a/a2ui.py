"""A2UI surface and component builders for Gemini Enterprise (WASD)."""

from __future__ import annotations

from typing import Any

GEMINI_ENTERPRISE_CATALOG_ID = (
    "https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json"
)
A2UI_MIME_TYPE = "application/json+a2ui"
A2UI_VERSION = "v0.8"

STUDIO_AGE_OPTIONS: list[str] = ["4-5", "6-8", "8-10", "10-12"]
STUDIO_PAGE_OPTIONS: list[str] = ["12", "16", "24"]

STAGE_LABELS: dict[str, str] = {
    "initializing": "Initializing session",
    "resuming": "Resuming storybook pipeline",
    "fetching": "Fetching & preparing classic source text",
    "adapting_text": "Adapting story for target age band",
    "building_character_bible": "Creating Character Bible & reference sheet",
    "extracting_bible": "Creating Character Bible & reference sheet",
    "planning_spreads": "Planning page spreads & visual layout",
    "generating_image": "Illustrating storybook spreads",
    "image_generation": "Illustrating storybook spreads",
    "spread_image": "Illustrating storybook spreads",
    "image_retry": "Refining spread illustration",
    "composing_pdf": "Typesetting & composing print-ready PDF",
    "done": "Storybook complete",
    "error": "Pipeline encountered an error",
}

STAGE_ICONS: dict[str, str] = {
    "initializing": "✅",
    "resuming": "🔄",
    "fetching": "📚",
    "adapting_text": "✍️",
    "building_character_bible": "🎨",
    "extracting_bible": "🎨",
    "planning_spreads": "📐",
    "generating_image": "🖼️",
    "image_generation": "🖼️",
    "spread_image": "🖼️",
    "image_retry": "🔁",
    "composing_pdf": "📄",
    "done": "🎉",
    "error": "❌",
}


def format_breadcrumb_line(
    ev: dict[str, Any],
    default_pct: int | float | None = None,
) -> str:
    """Format a pipeline progress event dict as a single human-readable breadcrumb line."""
    stage = str(ev.get("stage") or "working")
    icon = STAGE_ICONS.get(stage, "⏳")
    raw_pct = ev.get("pct", default_pct)
    pct_str = f"[{int(raw_pct)}%] " if raw_pct is not None else ""

    raw_msg = str(
        ev.get("message") or ev.get("detail") or ev.get("caption") or ""
    ).strip()

    spread_zero = ev.get("spread")
    spread_one = ev.get("spread_index") or ev.get("spread_number") or ev.get("page_number")
    total_of = ev.get("of")
    of_str = f"/{total_of}" if total_of else ""

    if stage == "image_retry":
        if spread_zero is not None:
            sp_disp = int(spread_zero) + 1
        elif spread_one is not None:
            sp_disp = int(spread_one)
        else:
            sp_disp = 1
        attempt = ev.get("attempt") or 1
        msg = f"Refining Spread {sp_disp}{of_str} illustration (attempt {attempt})"
    elif stage in ("generating_image", "image_generation", "spread_image") and (
        spread_zero is not None or spread_one is not None
    ):
        sp_disp = (
            int(spread_one)
            if spread_one is not None
            else int(spread_zero) + 1  # type: ignore[arg-type]
        )
        low = raw_msg.lower()
        if low in ("", "start", "in_progress"):
            msg = f"Generating Spread {sp_disp}{of_str} illustration..."
        elif low == "prompt ready":
            msg = f"Drafted visual prompt for Spread {sp_disp}{of_str}"
        elif low == "image ready":
            msg = f"Generated Spread {sp_disp}{of_str} illustration"
        elif low == "done":
            msg = f"Verified & typeset Spread {sp_disp}{of_str}"
        elif low == "cached":
            msg = f"Loaded cached Spread {sp_disp}{of_str} illustration"
        elif f"spread {sp_disp}" in low or "reference" in low:
            msg = raw_msg
        else:
            msg = f"Spread {sp_disp}{of_str}: {raw_msg}"
    elif raw_msg:
        msg = raw_msg
    else:
        msg = STAGE_LABELS.get(stage, stage.replace("_", " ").title())

    return f"{icon} {pct_str}{msg}".strip()


def format_breadcrumb_trail(
    events: list[dict[str, Any] | str],
    max_items: int = 6,
) -> list[str]:
    """Build the last `max_items` deduplicated breadcrumb lines from progress events."""
    lines: list[str] = []
    last_pct: int | float | None = None
    for item in events:
        if isinstance(item, str):
            line = item.strip()
        elif isinstance(item, dict):
            if item.get("pct") is not None:
                last_pct = item.get("pct")
            line = format_breadcrumb_line(item, default_pct=last_pct)
        else:
            continue
        if not line:
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return lines[-max_items:]


def wrap_a2ui_part(envelope: dict[str, Any]) -> dict[str, Any]:
    """Wrap a single A2UI envelope dict into a WASD-compatible A2A DataPart."""
    return {
        "kind": "data",
        "mimeType": A2UI_MIME_TYPE,
        "metadata": {"mimeType": A2UI_MIME_TYPE},
        "data": envelope,
    }


def wrap_a2ui_parts(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap a list of A2UI envelopes into separate A2A DataParts for WASD."""
    return [wrap_a2ui_part(env) for env in envelopes]


def wrap_a2ui_data_part(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap A2UI envelopes into a single A2A DataPart (also exposing merged keys)."""
    merged: dict[str, Any] = {"envelopes": envelopes}
    for env in envelopes:
        for k, v in env.items():
            if k not in merged:
                merged[k] = v
            elif k == "surfaceUpdate" and isinstance(merged[k], dict) and isinstance(v, dict):
                prev_comps = list(merged[k].get("components", []))
                by_id = {
                    c.get("id"): idx
                    for idx, c in enumerate(prev_comps)
                    if isinstance(c, dict) and "id" in c
                }
                for comp in v.get("components", []):
                    cid = comp.get("id") if isinstance(comp, dict) else None
                    if cid and cid in by_id:
                        prev_comps[by_id[cid]] = comp
                    else:
                        if cid:
                            by_id[cid] = len(prev_comps)
                        prev_comps.append(comp)
                merged[k] = {**merged[k], "components": prev_comps}
    return {
        "kind": "data",
        "mimeType": A2UI_MIME_TYPE,
        "metadata": {"mimeType": A2UI_MIME_TYPE},
        "data": merged,
    }


def build_message_parts(text: str, envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build [text_part, *a2ui_parts] for Gemini Enterprise (WASD) messages and artifacts."""
    return [{"kind": "text", "text": text}, *wrap_a2ui_parts(envelopes)]


def default_studio_options_for_book(
    title: str = "Demons",
    author: str = "Fyodor Dostoevsky",
) -> dict[str, Any]:
    """Return rich, literary default Creative Studio options tailored to a given work."""
    clean_title = (title or "Demons").strip()
    clean_author = (author or "Fyodor Dostoevsky").strip()
    is_russian_or_dostoevsky = any(
        tok in f"{clean_title} {clean_author}".lower()
        for tok in ("demon", "dostoev", "dostev", "crime", "karamazov", "idiot", "pushkin", "gogol")
    )

    if is_russian_or_dostoevsky:
        art_style_options = [
            {
                "label": "19th-Century Russian Woodblock — Stark carbon ink, birch-bark grain & lantern ochre",  # noqa: E501
                "value": (
                    "19th-century Russian lubok and woodblock print illustration with dramatic "
                    "carbon-black ink hatching, weathered birch-bark paper textures, and warm "
                    "amber lantern highlights."
                ),
            },
            {
                "label": "Ivan Bilibin Fairy-Tale Gouache — Ornate Slavic borders & jewel-toned watercolor",  # noqa: E501
                "value": (
                    "Golden-Age Russian fairy-tale illustration in the style of Ivan Bilibin, "
                    "featuring intricate Slavic folk borders, hand-inked architectural details, "
                    "and luminous jewel-toned gouache washes."
                ),
            },
            {
                "label": "Edward Gorey Gothic Crosshatch — Wry Victorian pen-and-ink & theatrical shadows",  # noqa: E501
                "value": (
                    "Wry gothic picture-book illustration inspired by Edward Gorey, with meticulous "  # noqa: E501
                    "pen-and-ink crosshatching, expressive wide-eyed characters in oversized coats, "  # noqa: E501
                    "and atmospheric fog-draped provincial streets."
                ),
            },
            {
                "label": "Chagall Constructivist Dreamscape — Floating silhouettes & expressive gouache",  # noqa: E501
                "value": (
                    "Poetic Russian avant-garde picture-book gouache inspired by Marc Chagall, "
                    "blending tilted provincial rooftops, expressive jewel-toned brushwork, and "
                    "whimsical floating visual metaphors."
                ),
            },
        ]
        text_style_options = [
            {
                "label": "Lemony Snicket Deadpan Satire — Wry narrator & vocabulary asides ('a word which here means...')",  # noqa: E501
                "value": (
                    "Wry, conspiratorial Lemony Snicket-style narrator who gently unpacks big "
                    "philosophical ideas and salon gossip with deadpan humor, parenthetical "
                    "definitions, and warm moral clarity for young readers."
                ),
            },
            {
                "label": "Krylov Fable & Samovar Storytelling — Cozy Russian fireside voice with rhythmic refrain",  # noqa: E501
                "value": (
                    "Warm Russian fireside storyteller voice inspired by Ivan Krylov's fables, "
                    "turning provincial rumors and runaway ideas into a lively parable about "
                    "listening carefully and thinking for yourself."
                ),
            },
            {
                "label": "Roald Dahl Mischievous Comedy — Fast-paced, theatrical & delightfully eccentric",  # noqa: E501
                "value": (
                    "Playful, theatrical Roald Dahl-inspired narration full of vivid character "
                    "quirks, comic overreactions at the town gala, and brisk rhythmic sentences."
                ),
            },
            {
                "label": "Kate DiCamillo Philosophical Heart — Lyrical, empathetic chapter-book prose",  # noqa: E501
                "value": (
                    "Luminous, big-hearted prose in the spirit of Kate DiCamillo, treating young "
                    "readers as thoughtful philosophers exploring friendship, truth, and courage."
                ),
            },
        ]
        suggested_motif = (
            "Include a tiny brass samovar and a mischievous paper weathervane that spins faster "
            "whenever a character spreads a wild rumor through the provincial town."
        )
        recommended_age = "8-10"
        recommended_pages = "12"
        conversational_reply = (
            f"Adapting **{clean_title}** by **{clean_author}** into an illustrated children's "
            f"storybook is a wonderfully rich creative challenge! At its core, the novel is a "
            f"whirlwind comedy of manners about how a sleepy provincial town gets swept up in "
            f"grandiose rumors, theatrical personalities, and contagious ideas before a chaotic "
            f"literary fête — which translates brilliantly into a wry picture-book fable about "
            f"critical thinking and Independent spirit.\n\n"
            f"For the visual direction, we can lean into **19th-Century Russian Woodblock** prints "
            f"with lantern-lit shadows, ornate **Ivan Bilibin Fairy-Tale Gouache** borders, "
            f"**Edward Gorey Gothic Crosshatch** comedy, or a **Chagall Dreamscape**. For the "
            f"narrative voice, a **Lemony Snicket Deadpan Satire** or **Krylov Fireside Fable** "
            f"lets us keep {clean_author}'s psychological wit accessible and delightful for young readers.\n\n"  # noqa: E501
            f"Use the **Storybook Creative Studio** card below to pick your favorite art style, "
            f"text style, target age band, and length — or tell me in chat how you'd like to "
            f"adjust the options before clicking **🚀 Generate Storybook on GKE**!"
        )
    else:
        art_style_options = [
            {
                "label": "Mid-Century Risograph & Gouache — Warm grain, bold shapes & vintage ink",
                "value": (
                    f"Mid-century picture-book gouache and Risograph print illustration for {clean_title}, "  # noqa: E501
                    "with warm paper grain, expressive character silhouettes, and rich layered inks."  # noqa: E501
                ),
            },
            {
                "label": "Golden-Age Watercolor & Ink — Delicate pen linework & luminous washes",
                "value": (
                    f"Classic Golden-Age storybook illustration for {clean_title} with hand-inked "
                    "pen contours, atmospheric watercolor washes, and intricate period details."
                ),
            },
            {
                "label": "Woodblock & Linocut Folk Print — Bold carved textures & lantern-lit palette",  # noqa: E501
                "value": (
                    f"Hand-carved woodblock and linocut print illustration for {clean_title} "
                    "featuring dramatic textured Relief lines and earthy ochre, indigo, and crimson tones."  # noqa: E501
                ),
            },
            {
                "label": "Whimsical Theatrical Diorama — Layered paper-cut stage & cinematic lighting",  # noqa: E501
                "value": (
                    f"Layered theatrical paper-cut diorama illustration for {clean_title} with "
                    "dramatic stage lighting, deep perspective, and tactile handmade textures."
                ),
            },
        ]
        text_style_options = [
            {
                "label": "Lemony Snicket Wry Narrator — Clever vocabulary asides & conspiratorial wit",  # noqa: E501
                "value": (
                    f"Wry, conspiratorial Lemony Snicket-style narrator adapting {clean_title} "
                    "with deadpan humor, parenthetical vocabulary definitions, and warm insight."
                ),
            },
            {
                "label": "Classic Fireside Read-Aloud — Lyrical cadence, vivid sensory detail & warmth",  # noqa: E501
                "value": (
                    f"Timeless fireside read-aloud prose for {clean_title} with musical cadence, "
                    "rich sensory imagery, and clear emotional arcs on every spread."
                ),
            },
            {
                "label": "Roald Dahl Playful Wit — Brisk pacing, eccentric charm & lively dialogue",
                "value": (
                    f"Playful, high-energy narration for {clean_title} in the spirit of Roald Dahl, "  # noqa: E501
                    "spotlighting eccentric character details and snappy dialogue."
                ),
            },
            {
                "label": "Kate DiCamillo Philosophical Tale — Empathetic, luminous & big-hearted",
                "value": (
                    f"Luminous, philosophical storytelling for {clean_title} in the spirit of "
                    "Kate DiCamillo, inviting young readers into the big questions of the story."
                ),
            },
        ]
        suggested_motif = (
            f"Weave a recurring visual motif from {clean_title} into the corner of every spread "
            f"that subtly reflects the protagonist's journey."
        )
        recommended_age = "6-8"
        recommended_pages = "12"
        conversational_reply = (
            f"Adapting **{clean_title}** by **{clean_author}** as an illustrated storybook opens up "  # noqa: E501
            f"wonderful visual and narrative possibilities! We can distill the dramatic heart of "
            f"{clean_author}'s work into unforgettable picture-book spreads while keeping the voice "  # noqa: E501
            f"engaging and age-appropriate.\n\n"
            f"Below I've curated four distinct **Visual Art Styles** and four **Narrative & Text Styles** "  # noqa: E501
            f"tailored to *{clean_title}*. Pick the combination that sparks your imagination in the "  # noqa: E501
            f"**Storybook Creative Studio** card below, ask me to refine any of the directions in chat, "  # noqa: E501
            f"or click **🚀 Generate Storybook on GKE** when you're ready to launch!"
        )

    return {
        "conversational_reply": conversational_reply,
        "title": clean_title,
        "author": clean_author,
        "art_style_options": art_style_options,
        "text_style_options": text_style_options,
        "selected_art_style": art_style_options[0]["value"],
        "selected_text_style": text_style_options[0]["value"],
        "recommended_age": recommended_age,
        "recommended_pages": recommended_pages,
        "suggested_motif": suggested_motif,
        "ready_to_generate": False,
    }


def _coerce_age_band(age: Any) -> str:
    raw = str(age or "6-8").strip()
    if raw in STUDIO_AGE_OPTIONS:
        return raw
    mapping = {
        "2-3": "4-5",
        "4-5": "4-5",
        "6-7": "6-8",
        "6-8": "6-8",
        "8-9": "8-10",
        "8-10": "8-10",
        "9-12": "10-12",
        "10-12": "10-12",
    }
    return mapping.get(raw, "6-8")


def _coerce_page_count_str(pages: Any) -> str:
    raw = str(pages or "12").strip().split()[0]
    if raw in STUDIO_PAGE_OPTIONS:
        return raw
    try:
        val = int(raw)
        if val <= 14:
            return "12"
        if val <= 20:
            return "16"
        return "24"
    except Exception:
        return "12"


def _resolve_option_value(
    candidate: str,
    options: list[dict[str, str]],
    fallback: str,
) -> str:
    cand = (candidate or "").strip()
    if not cand:
        return fallback
    for opt in options:
        if cand == opt.get("value") or cand == opt.get("label"):
            return opt["value"]
    for opt in options:
        if cand.lower() in opt.get("label", "").lower() or cand.lower() in opt.get("value", "").lower():  # noqa: E501
            return opt["value"]
    return cand


def normalize_config_fields(
    raw: dict[str, Any] | None = None,
    studio_state: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Normalize config fields from button action context + stored studio state."""
    raw = raw or {}
    base_title = str(
        raw.get("title")
        or (studio_state or {}).get("title")
        or "Demons"
    ).strip()
    base_author = str(
        raw.get("author")
        or (studio_state or {}).get("author")
        or "Fyodor Dostoevsky"
    ).strip()

    fallback_studio = studio_state or default_studio_options_for_book(base_title, base_author)
    art_options = fallback_studio.get("art_style_options") or []
    text_options = fallback_studio.get("text_style_options") or []

    default_art = str(
        fallback_studio.get("selected_art_style")
        or (art_options[0]["value"] if art_options else "Mid-century gouache picture-book illustration.")  # noqa: E501
    )
    default_text = str(
        fallback_studio.get("selected_text_style")
        or (text_options[0]["value"] if text_options else "Warm, wry read-aloud narrator.")
    )

    raw_art = str(raw.get("art_style") or raw.get("image_spec") or raw.get("style") or "").strip()
    raw_text = str(raw.get("text_style") or raw.get("text_spec") or raw.get("characters") or "").strip()  # noqa: E501

    art_style = _resolve_option_value(raw_art, art_options, default_art)
    text_style = _resolve_option_value(raw_text, text_options, default_text)

    age_range = _coerce_age_band(
        raw.get("age_range")
        or raw.get("target_age")
        or fallback_studio.get("recommended_age")
        or "6-8"
    )
    page_count = _coerce_page_count_str(
        raw.get("page_count")
        or fallback_studio.get("recommended_pages")
        or "12"
    )
    custom_instructions = str(
        raw.get("custom_instructions")
        or raw.get("freeform_notes")
        or raw.get("suggested_motif")
        or fallback_studio.get("suggested_motif")
        or ""
    ).strip()

    return {
        "title": base_title,
        "author": base_author,
        "art_style": art_style,
        "image_spec": art_style,
        "style": art_style,
        "text_style": text_style,
        "text_spec": text_style,
        "characters": text_style,
        "age_range": age_range,
        "target_age": age_range,
        "page_count": page_count,
        "custom_instructions": custom_instructions,
        "freeform_notes": custom_instructions,
    }


def ensure_studio_state(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ensure a studio_state dict has all required options and selected defaults."""
    raw = raw or {}
    title = str(raw.get("title") or "Demons").strip()
    author = str(raw.get("author") or "Fyodor Dostoevsky").strip()
    defaults = default_studio_options_for_book(title, author)

    art_opts = raw.get("art_style_options")
    if not isinstance(art_opts, list) or len(art_opts) == 0:
        art_opts = defaults["art_style_options"]
    else:
        normalized_art: list[dict[str, str]] = []
        for idx, item in enumerate(art_opts[:4]):
            if isinstance(item, dict):
                lbl = str(item.get("label") or item.get("value") or f"Style Option {idx + 1}").strip()  # noqa: E501
                val = str(item.get("value") or item.get("label") or lbl).strip()
                normalized_art.append({"label": lbl, "value": val})
            elif isinstance(item, str) and item.strip():
                normalized_art.append({"label": item.strip()[:80], "value": item.strip()})
        while len(normalized_art) < 4:
            normalized_art.append(defaults["art_style_options"][len(normalized_art)])
        art_opts = normalized_art

    text_opts = raw.get("text_style_options")
    if not isinstance(text_opts, list) or len(text_opts) == 0:
        text_opts = defaults["text_style_options"]
    else:
        normalized_text: list[dict[str, str]] = []
        for idx, item in enumerate(text_opts[:4]):
            if isinstance(item, dict):
                lbl = str(item.get("label") or item.get("value") or f"Narrative Option {idx + 1}").strip()  # noqa: E501
                val = str(item.get("value") or item.get("label") or lbl).strip()
                normalized_text.append({"label": lbl, "value": val})
            elif isinstance(item, str) and item.strip():
                normalized_text.append({"label": item.strip()[:80], "value": item.strip()})
        while len(normalized_text) < 4:
            normalized_text.append(defaults["text_style_options"][len(normalized_text)])
        text_opts = normalized_text

    selected_art = str(
        raw.get("selected_art_style")
        or raw.get("art_style")
        or raw.get("image_spec")
        or raw.get("style")
        or art_opts[0]["value"]
    ).strip()
    # Make sure selected_art is present in art_opts so the MultipleChoice chip highlights cleanly
    if not any(selected_art == o["value"] for o in art_opts):
        matched = _resolve_option_value(selected_art, art_opts, "")
        if any(matched == o["value"] for o in art_opts):
            selected_art = matched
        else:
            art_opts[0] = {"label": selected_art[:72], "value": selected_art}

    selected_text = str(
        raw.get("selected_text_style")
        or raw.get("text_style")
        or raw.get("text_spec")
        or raw.get("characters")
        or text_opts[0]["value"]
    ).strip()
    if not any(selected_text == o["value"] for o in text_opts):
        matched_t = _resolve_option_value(selected_text, text_opts, "")
        if any(matched_t == o["value"] for o in text_opts):
            selected_text = matched_t
        else:
            text_opts[0] = {"label": selected_text[:72], "value": selected_text}

    recommended_age = _coerce_age_band(
        raw.get("recommended_age") or raw.get("age_range") or raw.get("target_age") or defaults["recommended_age"]  # noqa: E501
    )
    recommended_pages = _coerce_page_count_str(
        raw.get("recommended_pages") or raw.get("page_count") or defaults["recommended_pages"]
    )
    suggested_motif = str(
        raw.get("suggested_motif")
        or raw.get("custom_instructions")
        or raw.get("freeform_notes")
        or defaults["suggested_motif"]
    ).strip()
    conversational_reply = str(
        raw.get("conversational_reply") or defaults["conversational_reply"]
    ).strip()

    return {
        "conversational_reply": conversational_reply,
        "title": title,
        "author": author,
        "art_style_options": art_opts,
        "text_style_options": text_opts,
        "selected_art_style": selected_art,
        "selected_text_style": selected_text,
        "recommended_age": recommended_age,
        "recommended_pages": recommended_pages,
        "suggested_motif": suggested_motif,
        "ready_to_generate": bool(raw.get("ready_to_generate", False)),
    }


def build_studio_surface(
    surface_id: str = "storybook_studio",
    studio_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the 3 WASD A2UI envelopes (beginRendering, surfaceUpdate, dataModelUpdate) for the Creative Studio."""  # noqa: E501
    st = ensure_studio_state(studio_state)
    title = st["title"]
    author = st["author"]
    art_options = st["art_style_options"]
    text_options = st["text_style_options"]
    selected_art = st["selected_art_style"]
    selected_text = st["selected_text_style"]
    selected_age = st["recommended_age"]
    selected_pages = str(st["recommended_pages"])
    motif = st["suggested_motif"]

    components: list[dict[str, Any]] = [
        {"id": "root", "component": {"Card": {"child": "main-col"}}},
        {
            "id": "main-col",
            "component": {
                "Column": {
                    "children": {
                        "explicitList": [
                            "header",
                            "subtitle",
                            "div1",
                            "art-heading",
                            "art-picker",
                            "text-heading",
                            "text-picker",
                            "age-heading",
                            "age-picker",
                            "pages-heading",
                            "pages-picker",
                            "div2",
                            "title-input",
                            "author-input",
                            "custom-motif-input",
                            "div3",
                            "actions-row",
                        ]
                    }
                }
            },
        },
        {
            "id": "header",
            "component": {
                "Text": {
                    "text": {"literalString": f"🎨 Storybook Creative Studio: {title} by {author}"},
                    "usageHint": "h2",
                }
            },
        },
        {
            "id": "subtitle",
            "component": {
                "Text": {
                    "text": {
                        "literalString": (
                            "Choose your visual art style, narrative voice, target age band, and length "  # noqa: E501
                            "below — or ask in chat to refine any direction before generating on GKE."  # noqa: E501
                        )
                    },
                    "usageHint": "body",
                }
            },
        },
        {"id": "div1", "component": {"Divider": {}}},
        {
            "id": "art-heading",
            "component": {
                "Text": {
                    "text": {"literalString": "1. Visual Art Style (image_spec)"},
                    "usageHint": "h3",
                }
            },
        },
        {
            "id": "art-picker",
            "component": {
                "MultipleChoice": {
                    "selections": {"path": "/studio/artStyleSelections"},
                    "options": [
                        {"label": {"literalString": opt["label"]}, "value": opt["value"]}
                        for opt in art_options
                    ],
                    "maxAllowedSelections": 1,
                }
            },
        },
        {
            "id": "text-heading",
            "component": {
                "Text": {
                    "text": {"literalString": "2. Narrative & Text Style (text_spec)"},
                    "usageHint": "h3",
                }
            },
        },
        {
            "id": "text-picker",
            "component": {
                "MultipleChoice": {
                    "selections": {"path": "/studio/textStyleSelections"},
                    "options": [
                        {"label": {"literalString": opt["label"]}, "value": opt["value"]}
                        for opt in text_options
                    ],
                    "maxAllowedSelections": 1,
                }
            },
        },
        {
            "id": "age-heading",
            "component": {
                "Text": {
                    "text": {"literalString": "3. Target Age Band"},
                    "usageHint": "h3",
                }
            },
        },
        {
            "id": "age-picker",
            "component": {
                "MultipleChoice": {
                    "selections": {"path": "/studio/ageSelections"},
                    "options": [
                        {"label": {"literalString": f"Ages {age}"}, "value": age}
                        for age in STUDIO_AGE_OPTIONS
                    ],
                    "maxAllowedSelections": 1,
                }
            },
        },
        {
            "id": "pages-heading",
            "component": {
                "Text": {
                    "text": {"literalString": "4. Book Length"},
                    "usageHint": "h3",
                }
            },
        },
        {
            "id": "pages-picker",
            "component": {
                "MultipleChoice": {
                    "selections": {"path": "/studio/pageCountSelections"},
                    "options": [
                        {"label": {"literalString": f"{p} pages"}, "value": p}
                        for p in STUDIO_PAGE_OPTIONS
                    ],
                    "maxAllowedSelections": 1,
                }
            },
        },
        {"id": "div2", "component": {"Divider": {}}},
        {
            "id": "title-input",
            "component": {
                "TextField": {
                    "label": {"literalString": "5. Book Title"},
                    "text": {"path": "/studio/title"},
                }
            },
        },
        {
            "id": "author-input",
            "component": {
                "TextField": {
                    "label": {"literalString": "6. Author"},
                    "text": {"path": "/studio/author"},
                }
            },
        },
        {
            "id": "custom-motif-input",
            "component": {
                "TextField": {
                    "label": {"literalString": "7. Custom Instructions / Recurring Visual Motif"},
                    "text": {"path": "/studio/customInstructions"},
                }
            },
        },
        {"id": "div3", "component": {"Divider": {}}},
        {
            "id": "lucky-btn-label",
            "component": {
                "Text": {"text": {"literalString": "🎲 I'm Feeling Lucky (Surprise Book)"}}
            },
        },
        {
            "id": "lucky-btn",
            "component": {
                "Button": {
                    "child": "lucky-btn-label",
                    "primary": False,
                    "action": {
                        "name": "im_feeling_lucky",
                        "context": [
                            {"key": "age_range", "value": {"path": "/studio/ageSelections"}},
                            {"key": "page_count", "value": {"path": "/studio/pageCountSelections"}},
                        ],
                    },
                }
            },
        },
        {
            "id": "shuffle-btn-label",
            "component": {
                "Text": {"text": {"literalString": "🔄 Shuffle New Style Ideas"}}
            },
        },
        {
            "id": "shuffle-btn",
            "component": {
                "Button": {
                    "child": "shuffle-btn-label",
                    "primary": False,
                    "action": {
                        "name": "shuffle_styles",
                        "context": [
                            {"key": "title", "value": {"path": "/studio/title"}},
                            {"key": "author", "value": {"path": "/studio/author"}},
                            {"key": "age_range", "value": {"path": "/studio/ageSelections"}},
                            {"key": "page_count", "value": {"path": "/studio/pageCountSelections"}},
                            {"key": "custom_instructions", "value": {"path": "/studio/customInstructions"}},  # noqa: E501
                        ],
                    },
                }
            },
        },
        {
            "id": "generate-btn-label",
            "component": {
                "Text": {"text": {"literalString": "🚀 Generate Storybook on GKE"}}
            },
        },
        {
            "id": "generate-btn",
            "component": {
                "Button": {
                    "child": "generate-btn-label",
                    "primary": True,
                    "action": {
                        "name": "generate_storybook",
                        "context": [
                            {"key": "title", "value": {"path": "/studio/title"}},
                            {"key": "author", "value": {"path": "/studio/author"}},
                            {"key": "art_style", "value": {"path": "/studio/artStyleSelections"}},
                            {"key": "text_style", "value": {"path": "/studio/textStyleSelections"}},
                            {"key": "age_range", "value": {"path": "/studio/ageSelections"}},
                            {"key": "page_count", "value": {"path": "/studio/pageCountSelections"}},
                            {"key": "custom_instructions", "value": {"path": "/studio/customInstructions"}},  # noqa: E501
                        ],
                    },
                }
            },
        },
        {
            "id": "actions-row",
            "component": {
                "Row": {
                    "children": {
                        "explicitList": ["lucky-btn", "shuffle-btn", "generate-btn"]
                    }
                }
            },
        },
    ]

    return [
        {"beginRendering": {"surfaceId": surface_id, "root": "root"}},
        {"surfaceUpdate": {"surfaceId": surface_id, "components": components}},
        {
            "dataModelUpdate": {
                "surfaceId": surface_id,
                "path": "/studio",
                "contents": [
                    {"key": "title", "valueString": title},
                    {"key": "author", "valueString": author},
                    {"key": "customInstructions", "valueString": motif},
                    {
                        "key": "artStyleSelections",
                        "valueMap": [{"key": "0", "valueString": selected_art}],
                    },
                    {
                        "key": "textStyleSelections",
                        "valueMap": [{"key": "0", "valueString": selected_text}],
                    },
                    {
                        "key": "ageSelections",
                        "valueMap": [{"key": "0", "valueString": selected_age}],
                    },
                    {
                        "key": "pageCountSelections",
                        "valueMap": [{"key": "0", "valueString": selected_pages}],
                    },
                ],
            }
        },
    ]


def build_config_surface(
    surface_id: str = "storybook_studio",
    defaults: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Alias for build_studio_surface for backwards compatibility."""
    return build_studio_surface(surface_id=surface_id, studio_state=defaults)


def build_lucky_data_model_update(
    surface_id: str = "storybook_studio",
    lucky_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build updated Creative Studio surface envelopes for an I'm Feeling Lucky / Shuffle turn."""
    return build_studio_surface(surface_id=surface_id, studio_state=lucky_fields)


def build_run_surface(
    surface_id: str,
    title: str = "Storybook",
    author: str = "",
    stage: str = "initializing",
    status: str = "in_progress",
    detail: str = "",
    pct: int | float | None = None,
    spreads: list[dict[str, Any]] | None = None,
    pdf_url: str | None = None,
    trace_url: str | None = None,
    breadcrumbs: list[str] | str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build WASD A2UI envelopes (beginRendering + surfaceUpdate) for live pipeline execution."""
    stage_label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())
    pct_str = f" ({int(pct)}%)" if pct is not None else ""
    status_badge = "✅" if status == "completed" or stage == "done" else ("❌" if status == "failed" else "⏳")  # noqa: E501
    header_title = f"📖 Storybook Studio Run: {title}" + (f" by {author}" if author else "")
    stage_line = f"{status_badge} {stage_label}{pct_str}" + (f" — {detail}" if detail else "")

    resolved_session_id = session_id or (
        surface_id[len("storybook_run_") :]
        if surface_id.startswith("storybook_run_")
        else surface_id
    )

    if isinstance(breadcrumbs, list):
        breadcrumb_text = "\n".join(str(b).strip() for b in breadcrumbs if str(b).strip())
    elif isinstance(breadcrumbs, str):
        breadcrumb_text = breadcrumbs.strip()
    else:
        breadcrumb_text = ""

    if not breadcrumb_text:
        breadcrumb_text = format_breadcrumb_line(
            {
                "stage": stage,
                "pct": pct if pct is not None else (100 if stage == "done" else 5),
                "message": detail,
            }
        )

    col_children: list[str] = [
        "run-header",
        "run-stage",
        "breadcrumbs-header",
        "breadcrumbs-log",
    ]
    components: list[dict[str, Any]] = [
        {"id": "root", "component": {"Card": {"child": "main-col"}}},
        {
            "id": "run-header",
            "component": {
                "Text": {
                    "text": {"literalString": header_title},
                    "usageHint": "h2",
                }
            },
        },
        {
            "id": "run-stage",
            "component": {
                "Text": {
                    "text": {"literalString": stage_line},
                    "usageHint": "h3",
                }
            },
        },
        {
            "id": "breadcrumbs-header",
            "component": {
                "Text": {
                    "text": {"literalString": "🧭 Live Activity Breadcrumbs"},
                    "usageHint": "h4",
                }
            },
        },
        {
            "id": "breadcrumbs-log",
            "component": {
                "Text": {
                    "text": {"literalString": breadcrumb_text},
                    "usageHint": "body",
                }
            },
        },
    ]

    if trace_url:
        col_children.append("run-trace")
        components.append(
            {
                "id": "run-trace",
                "component": {
                    "Text": {
                        "text": {"literalString": f"Session Trace: {trace_url}"},
                        "usageHint": "caption",
                    }
                },
            }
        )

    col_children.append("run-div")
    components.append({"id": "run-div", "component": {"Divider": {}}})

    for sp in spreads or []:
        raw_idx = sp.get("spread_index")
        idx = int(raw_idx) if raw_idx is not None else 1
        sp_title = str(
            sp.get("title")
            or ("Character & Style Reference" if idx == 0 else f"Spread {idx}")
        )
        sp_img = str(sp.get("image_url") or "")
        sp_text = str(sp.get("caption") or sp.get("page_text") or "")

        card_id = f"spread-card-{idx}"
        scol_id = f"spread-col-{idx}"
        stitle_id = f"spread-title-{idx}"
        simg_id = f"spread-img-{idx}"
        scaption_id = f"spread-caption-{idx}"

        if card_id not in col_children:
            col_children.append(card_id)
        s_children = [stitle_id]
        if sp_img:
            s_children.append(simg_id)
        if sp_text:
            s_children.append(scaption_id)

        components.extend(
            [
                {"id": card_id, "component": {"Card": {"child": scol_id}}},
                {
                    "id": scol_id,
                    "component": {"Column": {"children": {"explicitList": s_children}}},
                },
                {
                    "id": stitle_id,
                    "component": {
                        "Text": {"text": {"literalString": sp_title}, "usageHint": "h3"}
                    },
                },
            ]
        )
        if sp_img:
            components.append(
                {
                    "id": simg_id,
                    "component": {
                        "Image": {
                            "url": {"literalString": sp_img},
                            "usageHint": "largeFeature",
                        }
                    },
                }
            )
        if sp_text:
            components.append(
                {
                    "id": scaption_id,
                    "component": {
                        "Text": {"text": {"literalString": sp_text}, "usageHint": "body"}
                    },
                }
            )

    if stage != "done" and status != "completed":
        col_children.append("check-status-btn")
        components.extend(
            [
                {
                    "id": "check-status-btn-label",
                    "component": {
                        "Text": {"text": {"literalString": "🔄 Check Latest Progress"}}
                    },
                },
                {
                    "id": "check-status-btn",
                    "component": {
                        "Button": {
                            "child": "check-status-btn-label",
                            "primary": False,
                            "action": {
                                "name": "check_status",
                                "context": [
                                    {
                                        "key": "session_id",
                                        "value": {"literalString": resolved_session_id},
                                    },
                                ],
                            },
                        }
                    },
                },
            ]
        )

    if pdf_url:
        col_children.extend(["pdf-div", "pdf-summary", "download-btn"])
        components.extend(
            [
                {"id": "pdf-div", "component": {"Divider": {}}},
                {
                    "id": "pdf-summary",
                    "component": {
                        "Text": {
                            "text": {
                                "literalString": (
                                    f"🎉 Your storybook '{title}' is ready! "
                                    f"Download the print-ready PDF at: {pdf_url}"
                                )
                            },
                            "usageHint": "body",
                        }
                    },
                },
                {
                    "id": "download-btn-label",
                    "component": {
                        "Text": {"text": {"literalString": "📥 Download Completed Storybook PDF"}}
                    },
                },
                {
                    "id": "download-btn",
                    "component": {
                        "Button": {
                            "child": "download-btn-label",
                            "primary": True,
                            "action": {
                                "name": "download_pdf",
                                "context": [
                                    {"key": "pdf_url", "value": {"literalString": pdf_url}},
                                ],
                            },
                        }
                    },
                },
            ]
        )

    components.insert(
        1,
        {
            "id": "main-col",
            "component": {"Column": {"children": {"explicitList": col_children}}},
        },
    )

    return [
        {"beginRendering": {"surfaceId": surface_id, "root": "root"}},
        {"surfaceUpdate": {"surfaceId": surface_id, "components": components}},
    ]


def build_progress_surface_init(
    surface_id: str,
    title: str = "Storybook",
    author: str = "",
    trace_url: str | None = None,
    breadcrumbs: list[str] | str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build initial progress surface envelopes for a running storybook session."""
    return build_run_surface(
        surface_id=surface_id,
        title=title,
        author=author,
        stage="initializing",
        status="in_progress",
        detail="Initializing session",
        pct=5,
        trace_url=trace_url,
        breadcrumbs=breadcrumbs or ["✅ [5%] Initializing session"],
        session_id=session_id,
    )


def build_stage_update(
    surface_id: str,
    stage: str,
    status: str = "in_progress",
    detail: str = "",
    pct: int | float | None = None,
    title: str = "Storybook",
    author: str = "",
    spreads: list[dict[str, Any]] | None = None,
    breadcrumbs: list[str] | str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build stage progress update envelopes in WASD A2UI format."""
    return build_run_surface(
        surface_id=surface_id,
        title=title,
        author=author,
        stage=stage,
        status=status,
        detail=detail,
        pct=pct,
        spreads=spreads,
        breadcrumbs=breadcrumbs,
        session_id=session_id,
    )


def build_spread_card_update(
    surface_id: str,
    spread_index: int,
    image_url: str,
    caption: str = "",
    title: str = "",
    existing_spread_ids: list[str] | None = None,
    all_spreads: list[dict[str, Any]] | None = None,
    book_title: str = "Storybook",
    book_author: str = "",
    pct: int | float | None = None,
    stage: str = "generating_image",
    detail: str = "",
    breadcrumbs: list[str] | str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build A2UI envelopes adding/updating a spread card with an Image component."""
    default_title = "Character & Style Reference" if spread_index == 0 else f"Spread {spread_index}"
    spread_entry = {
        "spread_index": spread_index,
        "title": title or default_title,
        "image_url": image_url,
        "caption": caption or f"Illustrated {default_title.lower()}",
    }
    spreads = list(all_spreads) if all_spreads else [spread_entry]
    return build_run_surface(
        surface_id=surface_id,
        title=book_title,
        author=book_author,
        stage=stage,
        status="in_progress",
        detail=detail or f"Generated {default_title}",
        pct=pct,
        spreads=spreads,
        breadcrumbs=breadcrumbs,
        session_id=session_id,
    )


def build_download_chip_update(
    surface_id: str,
    pdf_url: str,
    title: str = "Storybook",
    author: str = "",
    spreads: list[dict[str, Any]] | None = None,
    breadcrumbs: list[str] | str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build final completed A2UI surface envelopes with spreads and PDF download action."""
    return build_run_surface(
        surface_id=surface_id,
        title=title,
        author=author,
        stage="done",
        status="completed",
        detail="All spreads illustrated and typeset into PDF",
        pct=100,
        spreads=spreads,
        pdf_url=pdf_url,
        breadcrumbs=breadcrumbs,
        session_id=session_id,
    )
