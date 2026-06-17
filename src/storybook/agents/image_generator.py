"""Image Generator — calls Nano Banana 2 (gemini-3.1-flash-image) via google-genai."""

from __future__ import annotations

from google import genai
from google.genai import types

from storybook.config import settings


class ImageContentPolicyError(RuntimeError):
    """Raised when the image model refuses due to content policy (finish_reason=NO_IMAGE)."""


class ImageTokenLimitError(RuntimeError):
    """Raised when the image response was cut off (finish_reason=MAX_TOKENS)."""


def _client() -> genai.Client:
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location="global",
    )


_ASPECT_RATIO_GUIDANCE: dict[str, str] = {
    "16:9": "wide panoramic landscape composition, horizontal orientation, 16:9 aspect ratio",
    "3:4": "portrait composition, vertical orientation, 3:4 aspect ratio",
    "1:1": "square composition, 1:1 aspect ratio",
}


def generate_image(prompt: str, aspect_ratio: str = "1:1") -> bytes:
    """
    Generate a single illustration using Nano Banana 2.

    Args:
        prompt: The image generation prompt (from IllustrationPrompter).
        aspect_ratio: Target aspect ratio string — "16:9", "3:4", or "1:1".

    Returns:
        Raw PNG image bytes.

    Raises:
        ImageContentPolicyError: if the model refuses due to safety filters.
        RuntimeError: for any other failure.
    """
    guidance = _ASPECT_RATIO_GUIDANCE.get(aspect_ratio, "")
    full_prompt = f"[{guidance}] {prompt}" if guidance else prompt

    client = _client()
    response = client.models.generate_content(
        model=settings.model_image,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    candidates = response.candidates or []
    if candidates:
        candidate = candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason is not None:
            reason_str = str(finish_reason)
            if reason_str in (
                "FinishReason.NO_IMAGE", "NO_IMAGE",
                "FinishReason.IMAGE_PROHIBITED_CONTENT", "IMAGE_PROHIBITED_CONTENT",
            ):
                raise ImageContentPolicyError(f"Image model refused (finish_reason={reason_str})")
            if reason_str in ("FinishReason.MAX_TOKENS", "MAX_TOKENS"):
                raise ImageTokenLimitError("Image response truncated (finish_reason=MAX_TOKENS)")
        parts = candidate.content.parts if candidate.content else []
        for part in parts or []:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                return part.inline_data.data

    raise RuntimeError(f"No image returned by {settings.model_image}. Response: {response}")
