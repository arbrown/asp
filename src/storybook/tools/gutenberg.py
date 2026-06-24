"""Tools for fetching public-domain texts from Project Gutenberg."""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)

_GUTENBERG_SEARCH = "https://gutendex.com/books/"
# Tried in order whenever a download from the primary host fails. All three
# serve the same cache-path layout (/cache/epub/{id}/pg{id}.txt). aleph uses
# plain HTTP because its TLS certificate is misconfigured (no SAN for the
# subdomain); the content is public-domain text so an unencrypted fetch is
# acceptable as a last resort.
_GUTENBERG_MIRRORS = (
    "https://www.gutenberg.org",
    "https://gutenberg.pglaf.org",
    "http://aleph.gutenberg.org",
)
# Gutenberg's own OPDS search — fallback when gutendex.com (third-party) is down.
_GUTENBERG_OPDS_SEARCH = "https://m.gutenberg.org/ebooks/search.opds/"
_STRIP_HEADER_RE = re.compile(
    r"\*{3}\s*START OF (?:THIS |THE )?PROJECT GUTENBERG.*?\*{3}", re.DOTALL | re.IGNORECASE
)
_STRIP_FOOTER_RE = re.compile(
    r"\*{3}\s*END OF (?:THIS |THE )?PROJECT GUTENBERG.*", re.DOTALL | re.IGNORECASE
)


def search_gutenberg(query: str) -> list[dict]:
    """
    Search Project Gutenberg by title or author and return candidate books.

    Tries the third-party gutendex.com first; if it's unreachable, falls back
    to Gutenberg's own OPDS catalog at m.gutenberg.org.

    Args:
        query: Free-text search query (title, author, or both).

    Returns:
        List of dicts with keys: id, title, authors, download_url.
    """
    try:
        resp = _get_with_retries(_GUTENBERG_SEARCH, params={"search": query}, timeout=30)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning(
            "gutendex search failed (%s) — falling back to Gutenberg OPDS catalog",
            exc,
        )
        return _search_via_opds(query)

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


def _search_via_opds(query: str) -> list[dict]:
    """Search Project Gutenberg's OPDS catalog (Atom XML) as a gutendex backup."""
    resp = _get_with_retries(_GUTENBERG_OPDS_SEARCH, params={"query": query}, timeout=30)
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    results: list[dict] = []
    for entry in root.findall("atom:entry", ns)[:5]:
        urn = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        m = re.search(r"(\d+)$", urn)
        if not m:
            continue
        book_id = int(m.group(1))
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        authors = [
            (a.findtext("atom:name", default="", namespaces=ns) or "").strip()
            for a in entry.findall("atom:author", ns)
        ]
        txt_url: str | None = None
        for link in entry.findall("atom:link", ns):
            rel = link.get("rel", "")
            mime = link.get("type", "")
            if "acquisition" in rel and mime.startswith("text/plain"):
                txt_url = link.get("href")
                break
        # Fall back to the deterministic cache URL if no acquisition link is offered.
        if not txt_url:
            txt_url = f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt"
        results.append({
            "id": book_id,
            "title": title,
            "authors": authors,
            "download_url": txt_url,
        })
    return results


def _extract_book_id(url: str) -> str | None:
    """Pull the numeric book id from any flavor of Gutenberg URL we might see.

    Matches on path shape, not host, so mirror URLs (gutenberg.pglaf.org,
    aleph.gutenberg.org, etc.) parse the same way as the canonical host.
    """
    for pat in (
        r"/ebooks/(\d+)",
        r"/files/(\d+)/",
        r"/cache/epub/(\d+)/",
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


def _candidate_download_urls(url: str) -> list[str]:
    """
    Build an ordered list of URLs to try for a given Gutenberg text download.

    The primary URL is tried first as-is, then we rotate through known mirrors
    using the deterministic cache path (/cache/epub/{id}/pg{id}.txt) which is
    supported uniformly on every mirror.
    """
    urls: list[str] = [url]
    book_id = _extract_book_id(url)
    if not book_id:
        return urls
    for host in _GUTENBERG_MIRRORS:
        cache_url = f"{host}/cache/epub/{book_id}/pg{book_id}.txt"
        if cache_url != url and cache_url not in urls:
            urls.append(cache_url)
    return urls


def _download_with_mirror_failover(url: str) -> str:
    """Try `url`, then rotate through mirror cache URLs on failure."""
    candidates = _candidate_download_urls(url)
    last_exc: Exception | None = None
    for i, candidate in enumerate(candidates, start=1):
        try:
            log.info("Gutenberg download: trying source %d/%d — %s",
                     i, len(candidates), candidate)
            return _get_with_retries(candidate).text
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            log.warning(
                "Gutenberg download failed on %s (%s) — %s",
                candidate, exc,
                "trying next mirror" if i < len(candidates) else "no more mirrors",
            )
    raise last_exc if last_exc else RuntimeError("no Gutenberg download candidates")


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

    text = _download_with_mirror_failover(url)

    # Strip Gutenberg header and footer
    if m := _STRIP_HEADER_RE.search(text):
        text = text[m.end():]
    if m := _STRIP_FOOTER_RE.search(text):
        text = text[: m.start()]

    return text.strip()
