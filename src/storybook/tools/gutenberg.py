"""Tools for fetching public-domain texts from Project Gutenberg."""

from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

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
    resp = _get_with_retries(_GUTENBERG_SEARCH, params={"search": query}, timeout=30)
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


def _extract_book_id(url: str) -> str | None:
    """Pull the numeric book id from any flavor of Gutenberg URL we might see."""
    for pat in (
        r"gutenberg\.org/ebooks/(\d+)",
        r"gutenberg\.org/files/(\d+)/",
        r"gutenberg\.org/cache/epub/(\d+)/",
    ):
        if m := re.search(pat, url):
            return m.group(1)
    return None


def _get_with_retries(
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 60,
    max_attempts: int = 3,
) -> httpx.Response:
    """GET a URL, retrying transport errors and 5xx responses with backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.get(url, params=params, follow_redirects=True, timeout=timeout)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code < 500 or attempt == max_attempts:
                raise
            log.warning(
                "Gutenberg %d on %s (attempt %d/%d) — retrying",
                exc.response.status_code, url, attempt, max_attempts,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise
            log.warning(
                "Gutenberg transport error on %s (attempt %d/%d): %s — retrying",
                url, attempt, max_attempts, exc,
            )
        time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s
    raise last_exc if last_exc else RuntimeError("unreachable")


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
        meta = _get_with_retries(f"{_GUTENBERG_SEARCH}{book_id}/", timeout=30).json()
        formats = meta.get("formats", {})
        url = (
            formats.get("text/plain; charset=utf-8")
            or formats.get("text/plain")
            or url
        )

    try:
        text = _get_with_retries(url).text
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
        book_id = _extract_book_id(url)
        if not book_id:
            raise
        cache_url = f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt"
        if cache_url == url:
            raise
        log.warning(
            "Gutenberg primary URL failed (%s) — falling back to cache URL %s",
            exc, cache_url,
        )
        text = _get_with_retries(cache_url).text

    # Strip Gutenberg header and footer
    if m := _STRIP_HEADER_RE.search(text):
        text = text[m.end():]
    if m := _STRIP_FOOTER_RE.search(text):
        text = text[: m.start()]

    return text.strip()
