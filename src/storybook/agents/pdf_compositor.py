"""PDF Compositor — assembles page text + images into a storybook PDF via weasyprint."""

from __future__ import annotations

import base64
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

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


def compose_pdf(
    title: str,
    author: str,
    pages: list[str],
    image_bytes_list: list[bytes],
    target_age: str = "4-5",
) -> bytes:
    """
    Render a storybook PDF from page texts and illustration bytes.

    Args:
        title: Book title for the cover.
        author: Original author attribution.
        pages: List of page text strings (one per page).
        image_bytes_list: List of PNG bytes (one per page; may be shorter than pages).
        target_age: Used to set font size.

    Returns:
        PDF as bytes.
    """
    font_sizes = {"4-5": 20, "6-8": 16, "9-12": 13}
    font_size = font_sizes.get(target_age, 16)

    page_data = []
    for i, text in enumerate(pages):
        img_b64 = ""
        if i < len(image_bytes_list) and image_bytes_list[i]:
            img_b64 = base64.b64encode(image_bytes_list[i]).decode()
        # Convert newlines to <br> so line breaks render in the PDF (autoescape=False)
        page_data.append({"text": text.replace("\n", "<br>"), "image_b64": img_b64})

    html_content = _PAGE_TEMPLATE.render(
        title=title,
        author=author,
        pages=page_data,
        font_size=font_size,
    )

    return HTML(string=html_content).write_pdf()
