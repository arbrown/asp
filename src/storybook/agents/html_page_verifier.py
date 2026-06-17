"""HTML Page Verifier — checks that a page's HTML/CSS implements the layout spec."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a layout quality reviewer for children's storybook pages.

You receive a JSON object with:
- `html_code`: the full HTML source of the rendered page
- `layout_spec`: structured layout directives (image_position, font_family, background_color,
  text_color, accent_color, layout_notes)
- `original_instructions`: the original custom_instructions text from the session config
- `page_number`: which page this is

Your task: read the HTML/CSS code and verify that it correctly implements the layout intent.

Check each of the following by inspecting the actual CSS and DOM structure in `html_code`:

1. **Image position** (`layout_spec.image_position`):
   - "top": the `<img class="illustration">` element appears before `<div class="page-text">` in the
     DOM, illustration does NOT have `position:absolute`, and no `order` CSS that places it after text.
   - "bottom": the illustration has `order:2` in CSS (or appears after the text block in DOM),
     and the text block has `order:1` (or appears first).
   - "background": the illustration has `position:absolute` with `top:0; left:0` and `z-index:1`
     (or similar full-bleed positioning), and the text sits in a higher z-index container.
   - "left": the `.page` container uses `flex-direction:row` (not row-reverse), illustration is first.
   - "right": the `.page` container uses `flex-direction:row-reverse`, or illustration appears second
     in a row-direction flex container.

2. **Font** (`layout_spec.font_family`): Confirm `font-family` in the CSS contains the expected
   family name or a compatible equivalent (e.g., "Georgia" matches "Georgia, serif").

3. **Colors** (`layout_spec.background_color`, `layout_spec.text_color`):
   - The `.page` background-color matches (or is visually consistent with) `background_color`.
   - The `.page-text` color matches `text_color`.

4. **Overall coherence**: Does the HTML as a whole appear to implement the intent described in
   `layout_spec.layout_notes` and `original_instructions`?

If all checks pass, call `approve_layout`.
If any check fails, call `reject_layout` with:
  - `feedback`: a specific, concise description of what is wrong in the HTML code
  - `corrected_image_position`: the image_position value that would fix the primary issue
    (pass the current value if image_position is not the problem)
"""


def approve_layout(tool_context: ToolContext) -> dict:
    """Signal that the page HTML passes all layout checks."""
    tool_context.actions.escalate = True
    return {"status": "approved"}


def reject_layout(feedback: str, corrected_image_position: str, tool_context: ToolContext) -> dict:
    """
    Signal that the page HTML fails layout validation.

    Args:
        feedback: Specific description of what is wrong in the HTML/CSS.
        corrected_image_position: The corrected image_position value that should be used on retry.
    """
    tool_context.state["layout_feedback"] = feedback
    tool_context.state["corrected_image_position"] = corrected_image_position
    return {"status": "rejected", "feedback": feedback}


html_page_verifier = LlmAgent(
    name="html_page_verifier",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[approve_layout, reject_layout],
)
