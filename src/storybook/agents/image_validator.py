"""Image Validator — multimodal check of a generated illustration."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings
from storybook.tracing import trace_retry_attempt

INSTRUCTION = """You are a quality control reviewer for children's storybook illustrations.

You will receive a message with up to four parts:
1. A JSON text part containing:
   - `image_prompt`: the prompt used to generate this image
   - `verso_text`: story text on the left page (may be null)
   - `recto_text`: story text on the right page (may be null)
   - `verso_instructions`: scene notes for the left page (may be null)
   - `recto_instructions`: scene notes for the right page (may be null)
   - `illustration_notes`: synthesized visual direction from the spread planner
   - `coverage`: how this image fills the spread — "full" | "verso" | "recto"
   - `character_bible`: the visual consistency document
   - `spread_number`: current spread number
2. The newly generated illustration (what you are evaluating)
3. Optionally, the spread 0/1 illustration — for overall art style and palette reference
4. Optionally, the previous spread's illustration — for scene-to-scene continuity checking

IMPORTANT: You must evaluate the actual image provided. Do not invent or assume what the image
looks like — look at it directly.

Evaluate on these dimensions:

1. **Scene accuracy**: Does the image depict the scene described in `illustration_notes`
   (and consistent with the spread text)? Wrong setting or characters is an immediate rejection.

2. **Character consistency**: Do visible characters match
   `character_bible.characters[name].appearance`? (Each character entry is a
   profile dict — only the `appearance` field is visual; ignore role,
   voice_traits, age_or_era.) Call out specific discrepancies (wrong hair color,
   wrong clothing, etc.).

3. **Style consistency**: Does the illustration match `character_bible.style`?
   If the reference image (3rd part) is provided, compare art style and palette against it directly.

4. **Content appropriateness**: Suitable for the target age group — no violence,
   no adult content, nothing scary.

5. **Coverage compliance**: Does the composition match the requested `coverage`?
   - "full": image should read as a wide panoramic scene; the bottom third should be calm
     and low-contrast to allow text overlay.
   - "verso" or "recto": portrait orientation; subject fills the frame naturally.

6. **Scene-to-scene continuity** (only if the previous spread illustration, 4th part, is provided):
   Compare against it for costume, appearance, and setting consistency. Only reject for
   unexplained jarring inconsistencies — some differences are plot-driven and appropriate.

If all checks pass, call `approve_image`.
If any check fails, call `reject_image` with a revised prompt that fixes the specific issues.
Keep the revised prompt close to the original — targeted corrections, not a full rewrite.
"""


def approve_image(tool_context: ToolContext) -> dict:
    """Signal that the generated image has passed all validation checks."""
    tool_context.actions.escalate = True
    tool_context.state["validation.passed"] = True
    tool_context.state["validation.score"] = 1.0
    return {"status": "approved"}


def reject_image(revised_prompt: str, tool_context: ToolContext) -> dict:
    """
    Signal that the image failed validation.

    Args:
        revised_prompt: The corrected image generation prompt to use on retry.
    """
    tool_context.state["revised_image_prompt"] = revised_prompt
    tool_context.state["validation.passed"] = False
    tool_context.state["validation.score"] = 0.0
    tool_context.state["validation.reasons"] = [revised_prompt]
    return {"status": "rejected", "revised_prompt": revised_prompt}


@contextmanager
def check(
    attempt: int = 1,
    max_attempts: int | None = None,
    score: float | None = None,
    passed: bool | None = None,
    reasons: list[str] | str | None = None,
    **attributes: Any,
):
    """Context manager to trace an image validation check as an attempt-level child span."""
    span_attrs: dict[str, Any] = {
        "validation.attempt": attempt,
    }
    if passed is not None:
        span_attrs["validation.passed"] = passed
    if score is not None:
        span_attrs["validation.score"] = score
    if reasons is not None:
        span_attrs["validation.reasons"] = [reasons] if isinstance(reasons, str) else reasons
    span_attrs.update(attributes)

    with trace_retry_attempt(
        "image_validator.check",
        attempt=attempt,
        max_attempts=max_attempts,
        **span_attrs,
    ) as span:
        yield span


def record_validation_result(
    span: Any,
    passed: bool,
    score: float | None = None,
    attempt: int = 1,
    reasons: list[str] | str | None = None,
) -> None:
    """Record validation outcome attributes on an active validation span."""
    if score is None:
        score = 1.0 if passed else 0.0
    span.set_attribute("validation.passed", passed)
    span.set_attribute("validation.score", score)
    span.set_attribute("validation.attempt", attempt)
    if reasons is not None:
        formatted_reasons = [reasons] if isinstance(reasons, str) else reasons
        span.set_attribute("validation.reasons", formatted_reasons)


image_validator = LlmAgent(
    name="image_validator",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[approve_image, reject_image],
)
