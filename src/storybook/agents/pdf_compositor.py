"""PDF Compositor — assembles page text + images into a storybook PDF via weasyprint."""

from __future__ import annotations

import base64
import re
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

_FONT_SIZES = {"4-5": 20, "6-8": 16, "9-12": 13}


def _md_to_html(text: str) -> str:
    """Convert markdown emphasis to HTML tags. Bold processed before italic to avoid partial matches."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_', r'<em>\1</em>', text, flags=re.DOTALL)
    return text

_VALID_IMAGE_POSITIONS = {"top", "bottom", "background", "left", "right"}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _get_overlay_styles(
    text_treatment: str,
    text_color: str,
    bg_rgb: str,
    text_position: str = "bottom",
) -> dict:
    """Compute per-spread overlay CSS values from the planner's chosen treatment and position."""
    if text_position == "top":
        grad_dir = "to top"          # gradient is opaque at element top, fades downward
        position_css = "top: 0; left: 0; right: 0;"
        padding = "0.3in 0.5in 0.35in"
        page_justify = "flex-start"
    else:  # "bottom"
        grad_dir = "to bottom"       # gradient is opaque at element bottom, fades upward
        position_css = "bottom: 0; left: 0; right: 0;"
        padding = "0.35in 0.5in 0.3in"
        page_justify = "flex-end"

    if text_treatment == "gradient_light":
        backdrop = (
            f"linear-gradient({grad_dir}, rgba({bg_rgb},0) 0%,"
            f" rgba({bg_rgb},0.88) 40%, rgba({bg_rgb},0.97) 100%)"
        )
        text_inline = f"color: {text_color};"
        num_inline = ""
        css_block = (
            f".text-backdrop .page-text, .text-over-image .page-text"
            f" {{ color: {text_color}; }}"
        )
    elif text_treatment == "direct":
        backdrop = "transparent"
        text_inline = (
            f"color: {text_color};"
            " text-shadow: 0 1px 8px rgba(0,0,0,0.9), 0 0 24px rgba(0,0,0,0.6);"
        )
        num_inline = ""
        css_block = (
            f".text-backdrop .page-text, .text-over-image .page-text"
            f" {{ color: {text_color};"
            " text-shadow: 0 1px 8px rgba(0,0,0,0.9), 0 0 24px rgba(0,0,0,0.6); }"
        )
    else:  # "gradient_dark" — default, reliable for most images
        backdrop = (
            f"linear-gradient({grad_dir},"
            " rgba(0,0,0,0) 0%, rgba(0,0,0,0.72) 35%, rgba(0,0,0,0.91) 100%)"
        )
        text_inline = "color: #ffffff; text-shadow: 0 1px 4px rgba(0,0,0,0.6);"
        num_inline = "color: rgba(255,255,255,0.75);"
        css_block = (
            ".text-backdrop .page-text, .text-over-image .page-text"
            " { color: #ffffff; text-shadow: 0 1px 4px rgba(0,0,0,0.6); }\n"
            "  .text-backdrop .page-number, .text-over-image .page-number"
            " { color: rgba(255,255,255,0.75); }"
        )

    return {
        "overlay_backdrop": backdrop,
        "overlay_position_css": position_css,
        "overlay_padding": padding,
        "overlay_css_block": css_block,
        "overlay_box_inline": f"background: {backdrop}; {position_css} padding: {padding};",
        "overlay_text_inline": text_inline,
        "overlay_num_inline": num_inline,
        "overlay_page_justify": page_justify,
    }


def _build_page_css(layout_spec: dict) -> str:
    """Return layout-specific CSS rules for a single story page."""
    pos = layout_spec.get("image_position", "top")
    if pos not in _VALID_IMAGE_POSITIONS:
        pos = "top"

    bg = layout_spec.get("background_color", "#fffdf7")
    text_color = layout_spec.get("text_color", "#1a1a1a")
    accent = layout_spec.get("accent_color", "#2c1a0e")
    font = layout_spec.get("font_family", "Georgia, serif")

    base = f"""
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: {font};
    background: #e8e4dc;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    min-height: 100vh;
    padding: 1rem;
  }}
  .page-number {{
    margin-top: auto;
    padding-top: 0.3in;
    font-size: 10pt;
    color: {accent};
  }}
  .page-text {{
    font-size: {{{{ font_size }}}}pt;
    line-height: 1.7;
    text-align: center;
    color: {text_color};
    max-width: 6.5in;
  }}"""

    if pos == "bottom":
        return base + f"""
  .page {{
    width: 8.5in;
    min-height: 11in;
    background: {bg};
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0.5in;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  }}
  .illustration {{
    order: 2;
    width: 100%;
    max-height: 6.5in;
    object-fit: contain;
    border-radius: 8px;
    margin-top: 0.3in;
  }}
  .page-text {{ order: 1; }}"""

    elif pos == "background":
        r, g, b = _hex_to_rgb(bg)
        return base + f"""
  .page {{
    width: 8.5in;
    min-height: 11in;
    background: {bg};
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: flex-end;
    padding: 0;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  }}
  .illustration {{
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    z-index: 1;
  }}
  .text-backdrop {{
    position: relative;
    z-index: 2;
    width: 100%;
    background: linear-gradient(to bottom, rgba({r},{g},{b},0) 0%, rgba({r},{g},{b},0.88) 40%, rgba({r},{g},{b},0.97) 100%);
    padding: 0.35in 0.5in 0.3in;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.15in;
  }}"""

    elif pos in ("left", "right"):
        direction = "row" if pos == "left" else "row-reverse"
        return base + f"""
  .page {{
    width: 8.5in;
    min-height: 11in;
    background: {bg};
    display: flex;
    flex-direction: {direction};
    align-items: stretch;
    gap: 0.3in;
    padding: 0.5in;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  }}
  .illustration {{
    width: 50%;
    max-height: 100%;
    object-fit: contain;
    border-radius: 8px;
  }}
  .page-text {{
    width: 50%;
    display: flex;
    align-items: center;
    text-align: left;
  }}"""

    else:  # top (default)
        return base + f"""
  .page {{
    width: 8.5in;
    min-height: 11in;
    background: {bg};
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0.5in;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  }}
  .illustration {{
    order: 1;
    width: 100%;
    max-height: 6.5in;
    object-fit: contain;
    border-radius: 8px;
    margin-bottom: 0.3in;
  }}
  .page-text {{ order: 2; }}"""

def _build_pdf_page_css(page_idx: int, layout_spec: dict, font_size: int) -> str:
    """Return namespaced CSS for one story page in the combined PDF document."""
    sel = f".story-page-{page_idx}"
    pos = layout_spec.get("image_position", "top")
    if pos not in _VALID_IMAGE_POSITIONS:
        pos = "top"
    bg = layout_spec.get("background_color", "#fffdf7")
    text_color = layout_spec.get("text_color", "#1a1a1a")
    accent = layout_spec.get("accent_color", "#2c1a0e")

    base = f"""
  {sel} .page-number {{
    margin-top: auto;
    padding-top: 0.3in;
    font-size: 10pt;
    color: {accent};
  }}
  {sel} .page-text {{
    font-size: {font_size}pt;
    line-height: 1.7;
    color: {text_color};
    max-width: 6.5in;
  }}"""

    if pos == "bottom":
        return base + f"""
  {sel} {{
    background: {bg};
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0.5in;
  }}
  {sel} .illustration {{
    order: 2;
    width: 100%;
    max-height: 6.5in;
    object-fit: contain;
    border-radius: 8px;
    margin-top: 0.3in;
  }}
  {sel} .page-text {{ order: 1; text-align: center; }}"""

    elif pos == "background":
        r, g, b = _hex_to_rgb(bg)
        return base + f"""
  {sel} {{
    background: {bg};
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: flex-end;
    padding: 0;
  }}
  {sel} .illustration {{
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    object-fit: cover;
    z-index: 1;
  }}
  {sel} .text-backdrop {{
    position: relative;
    z-index: 2;
    width: 100%;
    background: linear-gradient(to bottom, rgba({r},{g},{b},0) 0%, rgba({r},{g},{b},0.88) 40%, rgba({r},{g},{b},0.97) 100%);
    padding: 0.35in 0.5in 0.3in;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.15in;
  }}
  {sel} .page-text {{ text-align: center; }}"""

    elif pos in ("left", "right"):
        direction = "row" if pos == "left" else "row-reverse"
        return base + f"""
  {sel} {{
    background: {bg};
    display: flex;
    flex-direction: {direction};
    align-items: stretch;
    gap: 0.3in;
    padding: 0.5in;
  }}
  {sel} .illustration {{
    width: 50%;
    max-height: 100%;
    object-fit: contain;
    border-radius: 8px;
  }}
  {sel} .page-text {{
    width: 50%;
    display: flex;
    align-items: center;
    text-align: left;
  }}"""

    else:  # top (default)
        return base + f"""
  {sel} {{
    background: {bg};
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0.5in;
  }}
  {sel} .illustration {{
    order: 1;
    width: 100%;
    max-height: 6.5in;
    object-fit: contain;
    border-radius: 8px;
    margin-bottom: 0.3in;
  }}
  {sel} .page-text {{ order: 2; text-align: center; }}"""


_PDF_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {
    size: 8.5in 11in;
    margin: 0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: {{ font_family }}; }

  .page {
    width: 8.5in;
    height: 11in;
    page-break-after: always;
  }

  /* Cover */
  .cover {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    background: {{ cover_bg }};
  }
  .cover-title {
    font-size: 48pt;
    font-weight: bold;
    text-align: center;
    color: {{ text_color }};
    margin-bottom: 0.3in;
    line-height: 1.2;
  }
  .cover-author {
    font-size: 18pt;
    color: {{ accent_color }};
    text-align: center;
  }

  /* Per-page story styles (namespaced by .story-page-N) */
  {{ per_page_css }}
</style>
</head>
<body>

<div class="page cover">
  <div class="cover-title">{{ title }}</div>
  <div class="cover-author">Adapted from {{ author }}</div>
</div>

{% for page in pages %}
<div class="page story-page story-page-{{ loop.index }}">
  {% if page.image_b64 and page.image_position == "background" %}
  <img class="illustration"
       src="data:image/png;base64,{{ page.image_b64 }}"
       alt="Illustration for page {{ loop.index }}">
  <div class="text-backdrop">
    <div class="page-text">{{ page.text }}</div>
    <div class="page-number">{{ loop.index }}</div>
  </div>
  {% else %}
  {% if page.image_b64 %}
  <img class="illustration"
       src="data:image/png;base64,{{ page.image_b64 }}"
       alt="Illustration for page {{ loop.index }}">
  {% endif %}
  <div class="page-text">{{ page.text }}</div>
  <div class="page-number">{{ loop.index }}</div>
  {% endif %}
</div>
{% endfor %}

</body>
</html>
""")

_SINGLE_PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{{ layout_css }}
</style>
</head>
<body>
<div class="page">
  {% if image_b64 and image_position == "background" %}
  <img class="illustration"
       src="data:image/png;base64,{{ image_b64 }}"
       alt="Illustration for page {{ page_number }}">
  <div class="text-backdrop">
    <div class="page-text">{{ text }}</div>
    <div class="page-number">{{ page_number }}</div>
  </div>
  {% else %}
  {% if image_b64 %}
  <img class="illustration"
       src="data:image/png;base64,{{ image_b64 }}"
       alt="Illustration for page {{ page_number }}">
  {% endif %}
  <div class="page-text">{{ text }}</div>
  <div class="page-number">{{ page_number }}</div>
  {% endif %}
</div>
</body>
</html>
""")

_COVER_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Georgia', serif;
    background: #e8e4dc;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    min-height: 100vh;
    padding: 1rem;
  }
  .page {
    width: 8.5in;
    min-height: 11in;
    background: #f5efe0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    padding: 0.5in;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  }
  .cover-title {
    font-size: 48pt;
    font-weight: bold;
    text-align: center;
    color: #2c1a0e;
    margin-bottom: 0.3in;
    line-height: 1.2;
  }
  .cover-author {
    font-size: 18pt;
    color: #5c3d1e;
    text-align: center;
  }
</style>
</head>
<body>
<div class="page">
  <div class="cover-title">{{ title }}</div>
  <div class="cover-author">Adapted from {{ author }}</div>
</div>
</body>
</html>
""")


_SPREAD_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: {{ font_family }};
    background: #d0ccc4;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding: 1rem;
  }
  .spread {
    width: 17in;
    height: 11in;
    background: {{ background_color }};
    display: flex;
    position: relative;
    overflow: hidden;
    box-shadow: 0 4px 24px rgba(0,0,0,0.2);
  }
  .spread-page {
    width: 8.5in;
    height: 11in;
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 0.5in;
  }
  .verso-page { border-right: 1px solid rgba(0,0,0,0.08); }
  .portrait-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    position: absolute;
    top: 0; left: 0;
  }
  .full-bleed-img {
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    object-fit: cover;
    z-index: 1;
  }
  .page-text {
    font-size: {{ font_size }}pt;
    line-height: 1.7;
    color: {{ text_color }};
    text-align: center;
    position: relative;
    z-index: 3;
  }
  .page-number {
    font-size: 10pt;
    color: {{ accent_color }};
    position: relative;
    z-index: 3;
    margin-top: auto;
    padding-top: 0.2in;
  }
  .text-backdrop {
    position: absolute;
    {{ overlay_position_css }}
    z-index: 2;
    background: {{ overlay_backdrop }};
    padding: {{ overlay_padding }};
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.1in;
  }
  .text-over-image {
    position: absolute;
    {{ overlay_position_css }}
    z-index: 2;
    background: {{ overlay_backdrop }};
    padding: {{ overlay_padding }};
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.1in;
  }
  {{ overlay_css_block }}
  .gutter-line {
    position: absolute;
    top: 0; bottom: 0;
    left: 8.5in;
    width: 0;
    border-left: 2px solid rgba(0,0,0,0.06);
    z-index: 10;
  }
</style>
</head>
<body>
<div class="spread">

{% if coverage == "full" %}
  <img class="full-bleed-img" src="data:image/png;base64,{{ images[0] }}" alt="Spread illustration">
  <!-- verso side: blank or verso text with backdrop -->
  <div class="spread-page verso-page" style="justify-content:{{ overlay_page_justify }}; padding:0;">
    {% if verso_text %}
    <div class="text-backdrop">
      <div class="page-text">{{ verso_text }}</div>
      {% if verso_page_num %}<div class="page-number">{{ verso_page_num }}</div>{% endif %}
    </div>
    {% endif %}
  </div>
  <!-- recto side: recto text with backdrop -->
  <div class="spread-page" style="justify-content:{{ overlay_page_justify }}; padding:0;">
    {% if recto_text %}
    <div class="text-backdrop">
      <div class="page-text">{{ recto_text }}</div>
      {% if recto_page_num %}<div class="page-number">{{ recto_page_num }}</div>{% endif %}
    </div>
    {% endif %}
  </div>

{% elif coverage == "verso" %}
  <!-- verso: image fills left page; verso_text overlaid if present -->
  <div class="spread-page verso-page" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ images[0] }}" alt="Illustration">
    {% if verso_text %}
    <div class="text-over-image" style="{{ overlay_box_inline }}">
      <div class="page-text" style="{{ overlay_text_inline }}">{{ verso_text }}</div>
      {% if verso_page_num %}<div class="page-number" style="{{ overlay_num_inline }}">{{ verso_page_num }}</div>{% endif %}
    </div>
    {% elif verso_page_num %}
    <div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;">{{ verso_page_num }}</div>
    {% endif %}
  </div>
  <!-- recto: text column -->
  <div class="spread-page">
    {% if recto_text %}<div class="page-text">{{ recto_text }}</div>{% endif %}
    {% if recto_page_num %}<div class="page-number">{{ recto_page_num }}</div>{% endif %}
  </div>

{% elif coverage == "recto" %}
  <!-- verso: text column -->
  <div class="spread-page verso-page">
    {% if verso_text %}<div class="page-text">{{ verso_text }}</div>{% endif %}
    {% if verso_page_num %}<div class="page-number">{{ verso_page_num }}</div>{% endif %}
  </div>
  <!-- recto: image fills right page; recto_text overlaid if present -->
  <div class="spread-page" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ images[0] }}" alt="Illustration">
    {% if recto_text %}
    <div class="text-over-image" style="{{ overlay_box_inline }}">
      <div class="page-text" style="{{ overlay_text_inline }}">{{ recto_text }}</div>
      {% if recto_page_num %}<div class="page-number" style="{{ overlay_num_inline }}">{{ recto_page_num }}</div>{% endif %}
    </div>
    {% elif recto_page_num %}
    <div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;">{{ recto_page_num }}</div>
    {% endif %}
  </div>

{% elif coverage == "dual" %}
  <!-- verso: image with optional text overlay -->
  <div class="spread-page verso-page" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ images[0] }}" alt="Left illustration">
    {% if verso_text %}
    <div class="text-over-image">
      <div class="page-text">{{ verso_text }}</div>
      {% if verso_page_num %}<div class="page-number">{{ verso_page_num }}</div>{% endif %}
    </div>
    {% elif verso_page_num %}
    <div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;">{{ verso_page_num }}</div>
    {% endif %}
  </div>
  <!-- recto: image with optional text overlay -->
  <div class="spread-page" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ images[1] }}" alt="Right illustration">
    {% if recto_text %}
    <div class="text-over-image">
      <div class="page-text">{{ recto_text }}</div>
      {% if recto_page_num %}<div class="page-number">{{ recto_page_num }}</div>{% endif %}
    </div>
    {% elif recto_page_num %}
    <div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;">{{ recto_page_num }}</div>
    {% endif %}
  </div>

{% else %}  {# text-only #}
  <div class="spread-page verso-page">
    {% if verso_text %}<div class="page-text">{{ verso_text }}</div>{% endif %}
    {% if verso_page_num %}<div class="page-number">{{ verso_page_num }}</div>{% endif %}
  </div>
  <div class="spread-page">
    {% if recto_text %}<div class="page-text">{{ recto_text }}</div>{% endif %}
    {% if recto_page_num %}<div class="page-number">{{ recto_page_num }}</div>{% endif %}
  </div>
{% endif %}

<div class="gutter-line"></div>
</div>
</body>
</html>
""")

_WIDE_PDF_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page { size: 17in 11in; margin: 0; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: {{ font_family }}; }
  .spread-page {
    width: 17in;
    height: 11in;
    page-break-after: always;
    position: relative;
    overflow: hidden;
    background: {{ background_color }};
    display: flex;
  }
  .half-page {
    width: 8.5in;
    height: 11in;
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 0.5in;
  }
  .half-page.verso { border-right: 2px solid rgba(0,0,0,0.06); }
  .portrait-img { width: 100%; height: 100%; object-fit: cover; position: absolute; top:0; left:0; }
  .full-bleed-img { position: absolute; top:0; left:0; width:100%; height:100%; object-fit:cover; z-index:1; }
  .page-text {
    font-size: {{ font_size }}pt;
    line-height: 1.7;
    color: {{ text_color }};
    text-align: center;
    position: relative;
    z-index: 3;
  }
  .page-number { font-size: 10pt; color: {{ accent_color }}; position: relative; z-index:3; margin-top: auto; padding-top: 0.2in; }
  .text-backdrop { position: absolute; z-index:2; display: flex; flex-direction: column; align-items: center; gap: 0.1in; }
  .text-over-image { position: absolute; z-index:2; display: flex; flex-direction: column; align-items: center; gap: 0.1in; }
  .cover-half {
    display: flex; flex-direction: column; justify-content: center; align-items: center;
  }
  .cover-title { font-size: 42pt; font-weight: bold; text-align: center; color: {{ text_color }}; margin-bottom: 0.3in; line-height: 1.2; }
  .cover-author { font-size: 16pt; color: {{ accent_color }}; text-align: center; }
</style>
</head>
<body>

<!-- Cover spread: blank verso + title recto -->
<div class="spread-page">
  <div class="half-page verso"></div>
  <div class="half-page cover-half">
    <div class="cover-title">{{ title }}</div>
    <div class="cover-author">Adapted from {{ author }}</div>
  </div>
</div>

{% for s in spreads %}
<div class="spread-page">
{% if s.coverage == "full" %}
  <img class="full-bleed-img" src="data:image/png;base64,{{ s.images[0] }}" alt="Spread {{ s.spread_number }}">
  <div class="half-page verso" style="justify-content:{{ s.overlay_page_justify }}; padding:0;">
    {% if s.verso_text %}<div class="text-backdrop" style="{{ s.overlay_box_inline }}"><div class="page-text" style="{{ s.overlay_text_inline }}">{{ s.verso_text }}</div>{% if s.verso_page_num %}<div class="page-number" style="{{ s.overlay_num_inline }}">{{ s.verso_page_num }}</div>{% endif %}</div>{% endif %}
  </div>
  <div class="half-page" style="justify-content:{{ s.overlay_page_justify }}; padding:0;">
    {% if s.recto_text %}<div class="text-backdrop" style="{{ s.overlay_box_inline }}"><div class="page-text" style="{{ s.overlay_text_inline }}">{{ s.recto_text }}</div>{% if s.recto_page_num %}<div class="page-number" style="{{ s.overlay_num_inline }}">{{ s.recto_page_num }}</div>{% endif %}</div>{% endif %}
  </div>
{% elif s.coverage == "verso" %}
  <div class="half-page verso" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ s.images[0] }}" alt="Illustration">
    {% if s.verso_text %}<div class="text-over-image" style="{{ s.overlay_box_inline }}"><div class="page-text" style="{{ s.overlay_text_inline }}">{{ s.verso_text }}</div>{% if s.verso_page_num %}<div class="page-number" style="{{ s.overlay_num_inline }}">{{ s.verso_page_num }}</div>{% endif %}</div>{% elif s.verso_page_num %}<div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;z-index:3;">{{ s.verso_page_num }}</div>{% endif %}
  </div>
  <div class="half-page">
    {% if s.recto_text %}<div class="page-text">{{ s.recto_text }}</div>{% endif %}
    {% if s.recto_page_num %}<div class="page-number">{{ s.recto_page_num }}</div>{% endif %}
  </div>
{% elif s.coverage == "recto" %}
  <div class="half-page verso">
    {% if s.verso_text %}<div class="page-text">{{ s.verso_text }}</div>{% endif %}
    {% if s.verso_page_num %}<div class="page-number">{{ s.verso_page_num }}</div>{% endif %}
  </div>
  <div class="half-page" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ s.images[0] }}" alt="Illustration">
    {% if s.recto_text %}<div class="text-over-image" style="{{ s.overlay_box_inline }}"><div class="page-text" style="{{ s.overlay_text_inline }}">{{ s.recto_text }}</div>{% if s.recto_page_num %}<div class="page-number" style="{{ s.overlay_num_inline }}">{{ s.recto_page_num }}</div>{% endif %}</div>{% elif s.recto_page_num %}<div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;z-index:3;">{{ s.recto_page_num }}</div>{% endif %}
  </div>
{% elif s.coverage == "dual" %}
  <div class="half-page verso" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ s.images[0] }}" alt="Left illustration">
    {% if s.verso_text %}<div class="text-over-image" style="{{ s.overlay_box_inline }}"><div class="page-text" style="{{ s.overlay_text_inline }}">{{ s.verso_text }}</div>{% if s.verso_page_num %}<div class="page-number" style="{{ s.overlay_num_inline }}">{{ s.verso_page_num }}</div>{% endif %}</div>{% endif %}
  </div>
  <div class="half-page" style="padding:0;">
    <img class="portrait-img" src="data:image/png;base64,{{ s.images[1] }}" alt="Right illustration">
    {% if s.recto_text %}<div class="text-over-image" style="{{ s.overlay_box_inline }}"><div class="page-text" style="{{ s.overlay_text_inline }}">{{ s.recto_text }}</div>{% if s.recto_page_num %}<div class="page-number" style="{{ s.overlay_num_inline }}">{{ s.recto_page_num }}</div>{% endif %}</div>{% endif %}
  </div>
{% else %}
  <div class="half-page verso">
    {% if s.verso_text %}<div class="page-text">{{ s.verso_text }}</div>{% endif %}
    {% if s.verso_page_num %}<div class="page-number">{{ s.verso_page_num }}</div>{% endif %}
  </div>
  <div class="half-page">
    {% if s.recto_text %}<div class="page-text">{{ s.recto_text }}</div>{% endif %}
    {% if s.recto_page_num %}<div class="page-number">{{ s.recto_page_num }}</div>{% endif %}
  </div>
{% endif %}
</div>
{% endfor %}

</body>
</html>
""")

_PUBLISHING_PDF_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page { size: 8.5in 11in; margin: 0; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: {{ font_family }}; }
  .portrait-page {
    width: 8.5in;
    height: 11in;
    page-break-after: always;
    position: relative;
    overflow: hidden;
    background: {{ background_color }};
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 0.5in;
  }
  .portrait-img { width: 100%; height: 100%; object-fit: cover; position: absolute; top:0; left:0; }
  .full-bleed-img-left {
    /* shows left half of a wide image */
    position: absolute; top:0; left:0;
    width: 200%; height: 100%;
    object-fit: cover;
    object-position: left center;
    z-index: 1;
  }
  .full-bleed-img-right {
    /* shows right half of a wide image */
    position: absolute; top:0; right:0;
    width: 200%; height: 100%;
    object-fit: cover;
    object-position: right center;
    z-index: 1;
  }
  .page-text { font-size: {{ font_size }}pt; line-height: 1.7; color: {{ text_color }}; text-align: center; position: relative; z-index: 3; }
  .page-number { font-size: 10pt; color: {{ accent_color }}; position: relative; z-index: 3; margin-top: auto; padding-top: 0.2in; }
  .text-backdrop { position: absolute; z-index:2; display: flex; flex-direction: column; align-items: center; gap: 0.1in; }
  .text-over-image { position: absolute; z-index:2; display: flex; flex-direction: column; align-items: center; gap: 0.1in; }
  .cover-title { font-size: 42pt; font-weight: bold; text-align: center; color: {{ text_color }}; margin-bottom: 0.3in; line-height: 1.2; }
  .cover-author { font-size: 16pt; color: {{ accent_color }}; text-align: center; }
</style>
</head>
<body>

<!-- Cover page -->
<div class="portrait-page">
  <div class="cover-title">{{ title }}</div>
  <div class="cover-author">Adapted from {{ author }}</div>
</div>

{% for p in pages %}
<div class="portrait-page" style="{% if not p.has_content %}justify-content: center; align-items: center;{% endif %}">
{% if p.page_type == "full_verso" %}
  <img class="full-bleed-img-left" src="data:image/png;base64,{{ p.image }}" alt="Illustration">
  {% if p.text %}<div class="text-backdrop" style="{{ p.overlay_box_inline }}"><div class="page-text" style="{{ p.overlay_text_inline }}">{{ p.text }}</div>{% if p.page_num %}<div class="page-number" style="{{ p.overlay_num_inline }}">{{ p.page_num }}</div>{% endif %}</div>{% endif %}
{% elif p.page_type == "full_recto" %}
  <img class="full-bleed-img-right" src="data:image/png;base64,{{ p.image }}" alt="Illustration">
  {% if p.text %}<div class="text-backdrop" style="{{ p.overlay_box_inline }}"><div class="page-text" style="{{ p.overlay_text_inline }}">{{ p.text }}</div>{% if p.page_num %}<div class="page-number" style="{{ p.overlay_num_inline }}">{{ p.page_num }}</div>{% endif %}</div>{% endif %}
{% elif p.page_type == "image_only" %}
  <img class="portrait-img" src="data:image/png;base64,{{ p.image }}" alt="Illustration">
  {% if p.page_num %}<div class="page-number" style="position:absolute;bottom:0.2in;right:0.3in;z-index:3;">{{ p.page_num }}</div>{% endif %}
{% elif p.page_type == "image_with_text" %}
  <img class="portrait-img" src="data:image/png;base64,{{ p.image }}" alt="Illustration">
  {% if p.text %}<div class="text-over-image" style="{{ p.overlay_box_inline }}"><div class="page-text" style="{{ p.overlay_text_inline }}">{{ p.text }}</div>{% if p.page_num %}<div class="page-number" style="{{ p.overlay_num_inline }}">{{ p.page_num }}</div>{% endif %}</div>{% endif %}
{% else %}
  {% if p.text %}<div class="page-text">{{ p.text }}</div>{% endif %}
  {% if p.page_num %}<div class="page-number">{{ p.page_num }}</div>{% endif %}
{% endif %}
</div>
{% endfor %}

</body>
</html>
""")


def _spread_page_numbers(spread_number: int) -> tuple[int | None, int | None]:
    """Return (verso_page_num, recto_page_num) for a spread. Spread 0 has recto=1 only."""
    if spread_number == 0:
        return None, 1
    verso = spread_number * 2      # even pages are verso
    recto = spread_number * 2 + 1  # odd pages are recto
    return verso, recto


def _build_spread_context(
    spread_number: int,
    verso_text: str | None,
    recto_text: str | None,
    illustration_plan: list[dict],
    image_bytes_by_index: dict[int, bytes],
    layout_spec: dict,
    font_size: int,
    text_treatment: str = "gradient_dark",
    text_position: str = "bottom",
) -> dict:
    """Build a template context dict describing one spread's layout and content."""
    bg = layout_spec.get("background_color", "#fffdf7")
    text_color = layout_spec.get("text_color", "#1a1a1a")
    r, g, b = _hex_to_rgb(bg)
    bg_rgb = f"{r},{g},{b}"
    verso_num, recto_num = _spread_page_numbers(spread_number)
    overlay = _get_overlay_styles(text_treatment, text_color, bg_rgb, text_position)

    images: dict[int, str] = {}
    for idx, data in image_bytes_by_index.items():
        if data:
            images[idx] = base64.b64encode(data).decode()

    coverage = "none"
    if len(illustration_plan) == 1:
        coverage = illustration_plan[0].get("coverage", "full")
    elif len(illustration_plan) == 2:
        coverage = "dual"

    return {
        "spread_number": spread_number,
        "coverage": coverage,
        "images": images,
        "verso_text": _md_to_html(verso_text).replace("\n", "<br>") if verso_text else None,
        "recto_text": _md_to_html(recto_text).replace("\n", "<br>") if recto_text else None,
        "verso_page_num": verso_num,
        "recto_page_num": recto_num,
        "font_size": font_size,
        "bg_rgb": bg_rgb,
        **overlay,
    }


def render_spread_html(
    spread_number: int,
    verso_text: str | None,
    recto_text: str | None,
    illustration_plan: list[dict],
    image_bytes_by_index: dict[int, bytes],
    layout_spec: dict,
    target_age: str = "4-5",
    css_overrides: dict | None = None,
    text_treatment: str = "gradient_dark",
    text_position: str = "bottom",
) -> str:
    """Render a two-page spread as a self-contained 17×11 HTML document with embedded images.

    css_overrides keys (from verifier feedback):
      font_size_scale: float — multiply base font size (e.g. 1.3)
      text_treatment: str — override the planner's chosen treatment
    """
    spec = layout_spec or {}
    overrides = css_overrides or {}
    applied_treatment = overrides.get("text_treatment", text_treatment)
    base_font_size = _FONT_SIZES.get(target_age, 16)
    font_size = int(base_font_size * overrides.get("font_size_scale", 1.0))
    bg = spec.get("background_color", "#fffdf7")
    r, g, b = _hex_to_rgb(bg)
    ctx = _build_spread_context(
        spread_number, verso_text, recto_text,
        illustration_plan, image_bytes_by_index, spec, font_size,
        text_treatment=applied_treatment,
        text_position=text_position,
    )
    return _SPREAD_TEMPLATE.render(
        font_family=spec.get("font_family", "Georgia, serif"),
        background_color=bg,
        text_color=spec.get("text_color", "#1a1a1a"),
        accent_color=spec.get("accent_color", "#2c1a0e"),
        bg_rgb=f"{r},{g},{b}",
        font_size=font_size,
        **{k: v for k, v in ctx.items() if k not in ("font_size", "bg_rgb")},
    )


def compose_spread_pdf_wide(
    title: str,
    author: str,
    spread_contexts: list[dict],
    layout_spec: dict,
    target_age: str = "4-5",
) -> bytes:
    """Render a landscape 17×11 storybook PDF — one page per spread."""
    spec = layout_spec or {}
    font_size = _FONT_SIZES.get(target_age, 16)
    bg = spec.get("background_color", "#fffdf7")
    r, g, b = _hex_to_rgb(bg)
    html_content = _WIDE_PDF_TEMPLATE.render(
        title=title,
        author=author,
        font_family=spec.get("font_family", "Georgia, serif"),
        background_color=bg,
        text_color=spec.get("text_color", "#1a1a1a"),
        accent_color=spec.get("accent_color", "#2c1a0e"),
        bg_rgb=f"{r},{g},{b}",
        font_size=font_size,
        spreads=spread_contexts,
    )
    return HTML(string=html_content).write_pdf()


def _build_publishing_pages(spread_contexts: list[dict]) -> list[dict]:
    """Convert spread contexts into a flat list of portrait page dicts for publishing PDF."""
    pages = []
    for ctx in spread_contexts:
        coverage = ctx["coverage"]
        images = ctx.get("images", {})
        verso_text = ctx.get("verso_text")
        recto_text = ctx.get("recto_text")
        verso_num = ctx.get("verso_page_num")
        recto_num = ctx.get("recto_page_num")
        overlay = {
            "overlay_box_inline": ctx.get("overlay_box_inline", ""),
            "overlay_text_inline": ctx.get("overlay_text_inline", ""),
            "overlay_num_inline": ctx.get("overlay_num_inline", ""),
        }

        # verso page
        if ctx.get("verso_page_num") is not None or verso_text:
            if coverage == "full":
                pages.append({"page_type": "full_verso", "image": images.get(0, ""),
                               "text": verso_text, "page_num": verso_num, "has_content": True, **overlay})
            elif coverage == "verso":
                pages.append({"page_type": "image_only", "image": images.get(0, ""),
                               "text": None, "page_num": verso_num, "has_content": True, **overlay})
            elif coverage == "dual":
                pages.append({"page_type": "image_with_text", "image": images.get(0, ""),
                               "text": verso_text, "page_num": verso_num, "has_content": True, **overlay})
            else:
                pages.append({"page_type": "text", "image": "",
                               "text": verso_text, "page_num": verso_num, "has_content": bool(verso_text), **overlay})

        # recto page
        if ctx.get("recto_page_num") is not None or recto_text:
            if coverage == "full":
                pages.append({"page_type": "full_recto", "image": images.get(0, ""),
                               "text": recto_text, "page_num": recto_num, "has_content": True, **overlay})
            elif coverage == "recto":
                pages.append({"page_type": "image_only", "image": images.get(0, ""),
                               "text": None, "page_num": recto_num, "has_content": True, **overlay})
            elif coverage == "dual":
                pages.append({"page_type": "image_with_text", "image": images.get(1, ""),
                               "text": recto_text, "page_num": recto_num, "has_content": True, **overlay})
            else:
                pages.append({"page_type": "text", "image": "",
                               "text": recto_text, "page_num": recto_num, "has_content": bool(recto_text), **overlay})

    return pages


def compose_spread_pdf_publishing(
    title: str,
    author: str,
    spread_contexts: list[dict],
    layout_spec: dict,
    target_age: str = "4-5",
) -> bytes:
    """Render a portrait 8.5×11 storybook PDF — each spread split into verso + recto pages."""
    spec = layout_spec or {}
    font_size = _FONT_SIZES.get(target_age, 16)
    bg = spec.get("background_color", "#fffdf7")
    r, g, b = _hex_to_rgb(bg)
    portrait_pages = _build_publishing_pages(spread_contexts)
    html_content = _PUBLISHING_PDF_TEMPLATE.render(
        title=title,
        author=author,
        font_family=spec.get("font_family", "Georgia, serif"),
        background_color=bg,
        text_color=spec.get("text_color", "#1a1a1a"),
        accent_color=spec.get("accent_color", "#2c1a0e"),
        bg_rgb=f"{r},{g},{b}",
        font_size=font_size,
        pages=portrait_pages,
    )
    return HTML(string=html_content).write_pdf()


def render_page_html(
    page_number: int,
    story_text: str,
    image_bytes: bytes,
    target_age: str = "4-5",
    layout_spec: dict | None = None,
) -> str:
    """Render a single story page as a self-contained HTML document with embedded image."""
    spec = layout_spec or {}
    font_size = _FONT_SIZES.get(target_age, 16)
    img_b64 = base64.b64encode(image_bytes).decode() if image_bytes else ""
    layout_css = _build_page_css(spec).replace("{{ font_size }}", str(font_size))
    image_position = spec.get("image_position", "top")
    return _SINGLE_PAGE_TEMPLATE.render(
        page_number=page_number,
        text=_md_to_html(story_text).replace("\n", "<br>"),
        image_b64=img_b64,
        layout_css=layout_css,
        image_position=image_position,
    )


def render_cover_html(title: str, author: str) -> str:
    """Render the cover page as a self-contained HTML document."""
    return _COVER_TEMPLATE.render(title=title, author=author)


def compose_pdf(
    title: str,
    author: str,
    pages: list,
    image_bytes_list: list[bytes],
    target_age: str = "4-5",
    layout_spec: dict | None = None,
) -> bytes:
    """
    Render a storybook PDF from StoryPage objects and illustration bytes.

    Args:
        title: Book title for the cover.
        author: Original author attribution.
        pages: List of StoryPage objects (only story_text is printed).
        image_bytes_list: List of PNG bytes (one per page; may be shorter than pages).
        target_age: Used to set font size.
        layout_spec: Layout/typography spec from html_layout_extractor (page_layouts, fonts, colors).

    Returns:
        PDF as bytes.
    """
    spec = layout_spec or {}
    font_size = _FONT_SIZES.get(target_age, 16)
    page_layouts_list = spec.get("page_layouts", [])
    font = spec.get("font_family", "Georgia, serif")
    bg = spec.get("background_color", "#fffdf7")
    text_color = spec.get("text_color", "#1a1a1a")
    accent = spec.get("accent_color", "#2c1a0e")

    per_page_css = ""
    for i in range(1, len(pages) + 1):
        pos = page_layouts_list[i - 1] if i <= len(page_layouts_list) else "top"
        page_spec = {**spec, "image_position": pos}
        per_page_css += _build_pdf_page_css(i, page_spec, font_size)

    page_data = []
    for i, page in enumerate(pages, 1):
        img_b64 = ""
        if i <= len(image_bytes_list) and image_bytes_list[i - 1]:
            img_b64 = base64.b64encode(image_bytes_list[i - 1]).decode()
        text = page.story_text if hasattr(page, "story_text") else str(page)
        pos = page_layouts_list[i - 1] if i <= len(page_layouts_list) else "top"
        page_data.append({
            "text": _md_to_html(text).replace("\n", "<br>"),
            "image_b64": img_b64,
            "image_position": pos,
        })

    html_content = _PDF_TEMPLATE.render(
        title=title,
        author=author,
        pages=page_data,
        font_family=font,
        cover_bg=bg,
        text_color=text_color,
        accent_color=accent,
        per_page_css=per_page_css,
    )

    return HTML(string=html_content).write_pdf()
