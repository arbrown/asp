"""Image Generator — calls Nano Banana 2 (gemini-3.1-flash-image) via google-genai."""

from __future__ import annotations

from google import genai
from google.genai import types

from storybook.config import settings


def _client() -> genai.Client:
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location="global",
    )


def generate_image(prompt: str) -> bytes:
    """
    Generate a single illustration using Nano Banana 2.

    Args:
        prompt: The image generation prompt (from IllustrationPrompter).

    Returns:
        Raw PNG image bytes.
    """
    client = _client()
    response = client.models.generate_content(
        model=settings.model_image,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            return part.inline_data.data

    raise RuntimeError(f"No image returned by {settings.model_image}. Response: {response}")
