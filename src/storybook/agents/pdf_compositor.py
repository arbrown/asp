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
    background: rgba(255,253,247,0.88);
    padding: 0.3in 0.5in;
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

_PAGE_TEMPLATE = Template("""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {
    size: 8.5in 11in;
    margin: 0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Georgia', serif; background: #fffdf7; }

  .page {
    width: 8.5in;
    height: 11in;
    page-break-after: always;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0.5in;
  }

  /* Cover page */
  .page.cover {
    justify-content: center;
    background: #f5efe0;
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

  /* Story pages */
  .page.story .illustration {
    width: 100%;
    max-height: 6.5in;
    object-fit: contain;
    border-radius: 8px;
    margin-bottom: 0.3in;
  }
  .page.story .page-text {
    font-size: {{ font_size }}pt;
    line-height: 1.7;
    text-align: center;
    color: #1a1a1a;
    max-width: 6.5in;
  }
  .page-number {
    margin-top: auto;
    font-size: 10pt;
    color: #999;
  }
</style>
</head>
<body>

<!-- Cover -->
<div class="page cover">
  <div class="cover-title">{{ title }}</div>
  <div class="cover-author">Adapted from {{ author }}</div>
</div>

<!-- Story pages -->
{% for page in pages %}
<div class="page story">
  {% if page.image_b64 %}
  <img class="illustration"
       src="data:image/png;base64,{{ page.image_b64 }}"
       alt="Illustration for page {{ loop.index }}">
  {% endif %}
  <div class="page-text">{{ page.text }}</div>
  <div class="page-number">{{ loop.index }}</div>
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
) -> bytes:
    """
    Render a storybook PDF from StoryPage objects and illustration bytes.

    Args:
        title: Book title for the cover.
        author: Original author attribution.
        pages: List of StoryPage objects (only story_text is printed).
        image_bytes_list: List of PNG bytes (one per page; may be shorter than pages).
        target_age: Used to set font size.

    Returns:
        PDF as bytes.
    """
    font_size = _FONT_SIZES.get(target_age, 16)

    page_data = []
    for i, page in enumerate(pages):
        img_b64 = ""
        if i < len(image_bytes_list) and image_bytes_list[i]:
            img_b64 = base64.b64encode(image_bytes_list[i]).decode()
        # Use only story_text — page_instructions are never printed
        text = page.story_text if hasattr(page, "story_text") else str(page)
        page_data.append({"text": _md_to_html(text).replace("\n", "<br>"), "image_b64": img_b64})

    html_content = _PAGE_TEMPLATE.render(
        title=title,
        author=author,
        pages=page_data,
        font_size=font_size,
    )

    return HTML(string=html_content).write_pdf()
