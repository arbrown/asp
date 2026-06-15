"""Image Validator — multimodal check of a generated illustration."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a quality control reviewer for children's storybook illustrations.

You will receive:
- The generated image (as an attachment)
- A JSON object with:
  - `image_prompt`: the prompt used to generate this image
  - `page_text`: the story text this image should illustrate
  - `character_bible`: the visual consistency document
  - `page_number`: current page number
  - `reference_image_available`: boolean — whether page 1 is available as style reference

Evaluate the image on three dimensions:

1. **Character consistency**: Do visible characters match their descriptions in
   `character_bible.characters`? Call out specific discrepancies (wrong hair color,
   wrong clothing, etc.).

2. **Style consistency**: Does the illustration match `character_bible.style`?
   If `reference_image_available` is true, does it match the reference image's
   visual style?

3. **Content appropriateness**: Is the image suitable for the target age group?
   No violence, no adult content, nothing scary or disturbing.

If all checks pass, call `approve_image`.
If any check fails, call `reject_image` with a revised prompt that fixes the specific issues.
The revised prompt should be the original prompt with corrections appended or substituted —
not a completely new prompt.
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
