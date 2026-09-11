"""Image Generator — calls Nano Banana 2 (gemini-3.1-flash-image) via google-genai."""

from __future__ import annotations

from typing import Any

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


def generate_image(
    prompt: str,
    aspect_ratio: str = "1:1",
    attempt: int | None = None,
    reference_image: bytes | None = None,
) -> bytes:
    """Generate an illustration; wraps the call in an OpenTelemetry span with GenAI conventions."""
    from storybook.tracing import (
        GEN_AI_OPERATION_NAME,
        GEN_AI_REQUEST_MODEL,
        GEN_AI_SYSTEM,
        trace_agent_call,
    )

    span_attrs: dict[str, Any] = {
        "image.aspect_ratio": aspect_ratio,
        "image.model": settings.model_image,
        "image.has_reference": reference_image is not None,
        GEN_AI_SYSTEM: "gemini",
        GEN_AI_REQUEST_MODEL: settings.model_image,
        GEN_AI_OPERATION_NAME: "generate_content",
    }
    if attempt is not None:
        span_attrs["retry.attempt"] = attempt

    with trace_agent_call(
        "image.generate",
        model=settings.model_image,
        **span_attrs,
    ):
        return _generate_image(prompt, aspect_ratio, reference_image=reference_image)


def _generate_image(
    prompt: str,
    aspect_ratio: str = "1:1",
    reference_image: bytes | None = None,
) -> bytes:
    """Generate a single illustration using Nano Banana 2."""
    from storybook.tracing import set_span_token_usage

    guidance = _ASPECT_RATIO_GUIDANCE.get(aspect_ratio, "")
    full_prompt = f"[{guidance}] {prompt}" if guidance else prompt

    contents: list[Any] = []
    if reference_image:
        contents.append(
            types.Part(inline_data=types.Blob(mime_type="image/png", data=reference_image))
        )
    contents.append(full_prompt)

    client = _client()
    response = client.models.generate_content(
        model=settings.model_image,
        contents=contents if reference_image else full_prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        completion_tokens = getattr(usage, "candidates_token_count", None)
        set_span_token_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    candidates = response.candidates or []
    if candidates:
        candidate = candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason is not None:
            reason_str = str(finish_reason)
            if reason_str in (
                "FinishReason.NO_IMAGE",
                "NO_IMAGE",
                "FinishReason.IMAGE_PROHIBITED_CONTENT",
                "IMAGE_PROHIBITED_CONTENT",
            ):
                raise ImageContentPolicyError(f"Image model refused (finish_reason={reason_str})")
            if reason_str in ("FinishReason.MAX_TOKENS", "MAX_TOKENS"):
                raise ImageTokenLimitError("Image response truncated (finish_reason=MAX_TOKENS)")
        parts = candidate.content.parts if candidate.content else []
        for part in parts or []:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                return part.inline_data.data

    raise RuntimeError(f"No image returned by {settings.model_image}. Response: {response}")
