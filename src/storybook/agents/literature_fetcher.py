"""Literature Fetcher — decides whether to use a URL or Gutenberg search."""

from google.adk.agents import LlmAgent

from storybook.config import settings
from storybook.tools.gutenberg import fetch_gutenberg_url, search_gutenberg

INSTRUCTION = """You are a literature fetcher for a children's storybook pipeline.

Given the session configuration (JSON in the user message), obtain the raw text of the
requested work:

1. If `source.gutenberg_url` is provided, call `fetch_gutenberg_url` with that URL directly.
2. If only `source.title` and/or `source.author` are provided, call `search_gutenberg`
   to find the best match, then call `fetch_gutenberg_url` with the download URL of the
   top result.

After fetching, return ONLY the raw text content. Do not summarize or modify it.
"""

literature_fetcher = LlmAgent(
    name="literature_fetcher",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[fetch_gutenberg_url, search_gutenberg],
    output_key="source_text",
)
