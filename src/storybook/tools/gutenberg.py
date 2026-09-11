"""Tools for fetching public-domain texts from Project Gutenberg."""

from __future__ import annotations

import contextlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from storybook.tracing import SpanContextManager

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
    """Search Project Gutenberg by title or author and return candidate books.

    Tries the third-party gutendex.com first; if it's unreachable, falls back
    to Gutenberg's own OPDS catalog at m.gutenberg.org.

    Args:
        query: Free-text search query (title, author, or both).

    Returns:
        List of dicts with keys: id, title, authors, download_url.
    """
    with SpanContextManager("gutenberg.search", attributes={"search.query": query}) as span:
        start_time = time.perf_counter()
        backend = "gutendex"
        try:
            resp = _get_with_retries(_GUTENBERG_SEARCH, params={"search": query}, timeout=30)
            results = []
            for book in resp.json().get("results", [])[:5]:
                formats = book.get("formats", {})
                txt_url = (
                    formats.get("text/plain; charset=utf-8")
                    or formats.get("text/plain")
                )
                if txt_url:
                    results.append({
                        "id": book["id"],
                        "title": book["title"],
                        "authors": [a["name"] for a in book.get("authors", [])],
                        "download_url": txt_url,
                    })
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning(
                "gutendex search failed (%s) — falling back to Gutenberg OPDS catalog",
                exc,
            )
            backend = "opds"
            results = _search_via_opds(query)

        duration_ms = (time.perf_counter() - start_time) * 1000
        if span is not None and hasattr(span, "set_attribute"):
            span.set_attribute("search.results_count", len(results))
            span.set_attribute("search.backend", backend)
            span.set_attribute("search.duration_ms", duration_ms)
        return results


def _search_via_opds(query: str) -> list[dict]:
    """Search Project Gutenberg's OPDS catalog (Atom XML) as a gutendex backup."""
    with SpanContextManager("gutenberg.search_opds", attributes={"search.query": query}) as span:
        start_time = time.perf_counter()
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
        duration_ms = (time.perf_counter() - start_time) * 1000
        if span is not None and hasattr(span, "set_attribute"):
            span.set_attribute("search.results_count", len(results))
            span.set_attribute("search.backend", "opds")
            span.set_attribute("search.duration_ms", duration_ms)
        return results


def _extract_book_id(url: str) -> str | None:
    """Pull the numeric book id from any flavor of Gutenberg URL we might see.

    Matches on path shape, not host, so mirror URLs (gutenberg.pglaf.org,
    aleph.gutenberg.org, etc.) parse the same way as the canonical host.
    """
    for pat in (r"/ebooks/(\d+)", r"/files/(\d+)/", r"/cache/epub/(\d+)/"):
        if m := re.search(pat, url):
            return m.group(1)
    return None


def _get_with_retries(
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 60,
    max_attempts: int = 3,
    mirror_index: int = 1,
    mirror_count: int = 1,
    span_name: str | None = None,
) -> httpx.Response:
    """GET a URL, retrying transport errors and 5xx responses with backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        attrs: dict[str, Any] = {
            "http.url": url,
            "http.mirror_index": mirror_index,
            "http.attempt": attempt,
            "http.timeout_sec": float(timeout),
        }
        cm = (
            SpanContextManager(span_name, attributes=attrs)
            if span_name
            else contextlib.nullcontext()
        )
        with cm as span:
            if span_name:
                log.info(
                    "Gutenberg download: trying mirror %d/%d — %s",
                    mirror_index,
                    mirror_count,
                    url,
                )
            start_time = time.perf_counter()
            try:
                resp = httpx.get(
                    url, params=params, follow_redirects=True, timeout=timeout
                )
                duration_s = time.perf_counter() - start_time
                duration_ms = duration_s * 1000
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("http.status_code", resp.status_code)
                    span.set_attribute("http.duration_ms", duration_ms)

                resp.raise_for_status()

                download_bytes = len(resp.content)
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("download.bytes", download_bytes)

                if span_name:
                    log.info(
                        "Gutenberg download succeeded from mirror %d/%d (%s) "
                        "(size: %d bytes in %.2fs)",
                        mirror_index,
                        mirror_count,
                        url,
                        download_bytes,
                        duration_s,
                    )
                return resp
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                duration_s = time.perf_counter() - start_time
                duration_ms = duration_s * 1000
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("http.status_code", exc.response.status_code)
                    span.set_attribute("http.duration_ms", duration_ms)
                    span.set_attribute("http.error", str(exc))
                    if hasattr(span, "record_exception"):
                        span.record_exception(exc)
                if span_name:
                    log.warning(
                        "Gutenberg download failed on mirror %d/%d (%s) after %.2fs: %s",
                        mirror_index,
                        mirror_count,
                        url,
                        duration_s,
                        exc,
                    )
                if exc.response.status_code < 500 or attempt == max_attempts:
                    raise
                log.warning(
                    "Gutenberg %d on %s (attempt %d/%d) — retrying",
                    exc.response.status_code,
                    url,
                    attempt,
                    max_attempts,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                duration_s = time.perf_counter() - start_time
                duration_ms = duration_s * 1000
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("http.duration_ms", duration_ms)
                    span.set_attribute("http.error", str(exc))
                    if hasattr(span, "record_exception"):
                        span.record_exception(exc)
                if span_name:
                    log.warning(
                        "Gutenberg download failed on mirror %d/%d (%s) after %.2fs: %s",
                        mirror_index,
                        mirror_count,
                        url,
                        duration_s,
                        exc,
                    )
                if attempt == max_attempts:
                    raise
                log.warning(
                    "Gutenberg transport error on %s (attempt %d/%d): %s — retrying",
                    url,
                    attempt,
                    max_attempts,
                    exc,
                )
        time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s
    raise last_exc if last_exc else RuntimeError("unreachable")


def _candidate_download_urls(url: str) -> list[str]:
    """Build an ordered list of URLs to try for a given Gutenberg text download.

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
            resp = _get_with_retries(
                candidate,
                mirror_index=i,
                mirror_count=len(candidates),
                span_name="gutenberg.download_attempt",
            )
            return resp.text
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            log.warning(
                "Gutenberg download failed on mirror %d/%d (%s) — %s",
                i,
                len(candidates),
                candidate,
                "trying next mirror" if i < len(candidates) else "no more mirrors",
            )
    raise last_exc if last_exc else RuntimeError("no Gutenberg download candidates")


def fetch_gutenberg_url(url: str) -> str:
    """Download raw text from a Project Gutenberg URL and strip boilerplate.

    Args:
        url: Direct URL to a plain-text Gutenberg file or a book page URL.

    Returns:
        Cleaned plain-text content of the book.
    """
    book_id = _extract_book_id(url) or ""
    with SpanContextManager(
        "gutenberg.fetch",
        attributes={"gutenberg.url": url, "gutenberg.book_id": book_id},
    ) as span:
        # Resolve ebook page URLs to the raw text file directly
        ebook_match = re.search(r"gutenberg\.org/ebooks/(\d+)", url)
        if ebook_match:
            book_id = ebook_match.group(1)
            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("gutenberg.book_id", book_id)
            url = f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt"
            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("gutenberg.url", url)

        text = _download_with_mirror_failover(url)

        # Strip Gutenberg header and footer
        if m := _STRIP_HEADER_RE.search(text):
            text = text[m.end():]
        if m := _STRIP_FOOTER_RE.search(text):
            text = text[: m.start()]

        return text.strip()
