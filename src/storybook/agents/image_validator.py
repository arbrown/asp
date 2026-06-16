"""Image Validator — multimodal check of a generated illustration."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a quality control reviewer for children's storybook illustrations.

You will receive a message with up to four parts:
1. A JSON text part containing:
   - `image_prompt`: the prompt used to generate this image
   - `page_text`: the story text printed on this page
   - `page_instructions`: scene notes from the author (what to depict, hidden elements, motifs)
   - `character_bible`: the visual consistency document
   - `page_number`: current page number
   - `prev_page_text`: the previous page's story text, or null if not available
2. The newly generated illustration (what you are evaluating)
3. Optionally, the page 1 illustration — for overall art style and palette reference
4. Optionally, the previous page's illustration — for scene-to-scene continuity checking

IMPORTANT: You must evaluate the actual image provided. Do not invent or assume what the image
looks like — look at it directly.

Evaluate on these dimensions:

1. **Scene accuracy**: Does the image depict the scene described in `page_instructions`
   (and consistent with `page_text`)? Wrong setting or characters is an immediate rejection.

2. **Character consistency**: Do visible characters match their descriptions in
   `character_bible.characters`? Call out specific discrepancies (wrong hair color,
   wrong clothing, etc.).

3. **Style consistency**: Does the illustration match `character_bible.style`?
   If the page 1 reference image (3rd part) is provided, compare art style and palette against it directly.

4. **Content appropriateness**: Suitable for the target age group — no violence,
   no adult content, nothing scary.

5. **Page-to-page continuity** (only if the previous page illustration, 4th part, is provided):
   Compare this illustration against the previous page's illustration and `prev_page_text`.
   Characters should maintain consistent costumes and appearance unless the story text
   explicitly describes a change. Settings should be consistent unless the narrative has
   clearly moved to a new location. Use your judgment — some differences are plot-driven
   and appropriate. Only reject if there is an unexplained and jarring inconsistency
   (e.g., a character's costume changes with no story reason, or the scene contradicts
   what happened on the previous page).

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
