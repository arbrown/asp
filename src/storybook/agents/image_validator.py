"""Image Validator — multimodal check of a generated illustration."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

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

2. **Character consistency**: Do visible characters match their descriptions in
   `character_bible.characters`? Call out specific discrepancies (wrong hair color,
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
    return {"status": "approved"}


def reject_image(revised_prompt: str, tool_context: ToolContext) -> dict:
    """
    Signal that the image failed validation.

    Args:
        revised_prompt: The corrected image generation prompt to use on retry.
    """
    tool_context.state["revised_image_prompt"] = revised_prompt
    return {"status": "rejected", "revised_prompt": revised_prompt}


image_validator = LlmAgent(
    name="image_validator",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[approve_image, reject_image],
)
