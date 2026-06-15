"""Tools for fetching public-domain texts from Project Gutenberg."""

from __future__ import annotations

import re

import httpx

_GUTENBERG_SEARCH = "https://gutendex.com/books/"
_STRIP_HEADER_RE = re.compile(
    r"\*{3}\s*START OF (?:THIS |THE )?PROJECT GUTENBERG.*?\*{3}", re.DOTALL | re.IGNORECASE
)
_STRIP_FOOTER_RE = re.compile(
    r"\*{3}\s*END OF (?:THIS |THE )?PROJECT GUTENBERG.*", re.DOTALL | re.IGNORECASE
)


def search_gutenberg(query: str) -> list[dict]:
    """
    Search Project Gutenberg by title or author and return candidate books.

    Args:
        query: Free-text search query (title, author, or both).

    Returns:
        List of dicts with keys: id, title, authors, download_url.
    """
    resp = httpx.get(_GUTENBERG_SEARCH, params={"search": query}, timeout=15)
    resp.raise_for_status()
    results = []
    for book in resp.json().get("results", [])[:5]:
        txt_url = book.get("formats", {}).get("text/plain; charset=utf-8") or \
                  book.get("formats", {}).get("text/plain")
        if txt_url:
            results.append({
                "id": book["id"],
                "title": book["title"],
                "authors": [a["name"] for a in book.get("authors", [])],
                "download_url": txt_url,
            })
    return results


def fetch_gutenberg_url(url: str) -> str:
    """
    Download raw text from a Project Gutenberg URL and strip the boilerplate
    header and footer.

    Args:
        url: Direct URL to a plain-text Gutenberg file (e.g. https://www.gutenberg.org/files/NNNN/NNNN-0.txt)
             or a Gutenberg book page URL (e.g. https://www.gutenberg.org/ebooks/NNNN).

    Returns:
        Cleaned plain-text content of the book.
    """
    # Resolve ebook page URLs to the raw text file
    ebook_match = re.search(r"gutenberg\.org/ebooks/(\d+)", url)
    if ebook_match:
        book_id = ebook_match.group(1)
        meta = httpx.get(f"{_GUTENBERG_SEARCH}{book_id}/", timeout=15).json()
        formats = meta.get("formats", {})
        url = (
            formats.get("text/plain; charset=utf-8")
            or formats.get("text/plain")
            or url
        )

    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    text = resp.text

    # Strip Gutenberg header and footer
    if m := _STRIP_HEADER_RE.search(text):
        text = text[m.end():]
    if m := _STRIP_FOOTER_RE.search(text):
        text = text[: m.start()]

    return text.strip()
