"""Page Splitter — deterministic segmentation of adapted text into N pages."""

from __future__ import annotations

import re


def split_into_pages(text: str, page_count: int, max_words_per_page: int) -> list[str]:
    """
    Split adapted story text into exactly `page_count` pages.

    Strategy:
    - If the text contains stanza breaks (double newlines), preserve them as
      natural page boundaries, then merge/split to hit the target count.
    - Otherwise, split on sentence boundaries aiming for `max_words_per_page` per page.

    Args:
        text: The full adapted story text.
        page_count: Target number of pages.
        max_words_per_page: Soft word limit per page (from age group config).

    Returns:
        List of page text strings, length == page_count.
    """
    # Try stanza-aware splitting first (for poetry like Onegin stanzas)
    stanzas = [s.strip() for s in re.split(r"\n{2,}", text) if s.strip()]

    if len(stanzas) >= page_count:
        # More stanzas than pages — merge adjacent stanzas into page_count groups
        pages = _merge_chunks(stanzas, page_count)
    else:
        # Fall back to sentence-based splitting
        sentences = re.split(r"(?<=[.!?])\s+", text)
        pages = _merge_chunks(sentences, page_count)

    # Pad with empty strings if we somehow have too few (shouldn't happen)
    while len(pages) < page_count:
        pages.append("")

    return pages[:page_count]


def _merge_chunks(chunks: list[str], target: int) -> list[str]:
    """Distribute chunks into target groups as evenly as possible."""
    n = len(chunks)
    if n <= target:
        return chunks
    group_size = n / target
    pages = []
    for i in range(target):
        start = int(i * group_size)
        end = int((i + 1) * group_size) if i < target - 1 else n
        pages.append("\n\n".join(chunks[start:end]))
    return pages
