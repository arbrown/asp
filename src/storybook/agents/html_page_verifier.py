"""HTML Page Verifier — checks that a page's HTML/CSS implements the layout spec."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a layout quality reviewer for children's storybook pages.

You receive a JSON object with:
- `html_code`: the full HTML source of the rendered page
- `layout_spec`: structured layout directives (image_position, font_family, background_color, etc.)
- `original_instructions`: the original custom_instructions text
- `page_number`: which page this is

Your ONLY job is to verify that the HTML/CSS implements the correct `image_position`.
Do NOT check fonts, colors, or overall coherence — those values are generated directly from the
spec and cannot be wrong.

Read the CSS rules for `.illustration` and `.page` and apply these pass/fail rules:

"top"
  PASS: `.illustration` has `order: 1` (and `.page-text` has `order: 2`), OR the <img> simply
        appears before the text in DOM with no absolute positioning.
  FAIL: `.illustration` has `position: absolute`, or its `order` value is larger than the text's.

"bottom"
  PASS: `.illustration` has `order: 2` (a value larger than the text element's order value).
  FAIL: `.illustration` appears first with no `order` override.

"background"
  PASS: `.illustration` has `position: absolute`.
  FAIL: no `position: absolute` on the illustration.

"left"
  PASS: `.page` has `flex-direction: row` (not row-reverse).
  FAIL: column layout or row-reverse.

"right"
  PASS: `.page` has `flex-direction: row-reverse`.
  FAIL: column layout or plain row.

DEFAULT TO APPROVING. If you are uncertain, or if the layout broadly matches the intent,
call `approve_layout`. Only call `reject_layout` for a clear, unambiguous structural mismatch
— e.g. the spec says "background" but the illustration has no `position: absolute`.

CRITICAL — YOU MUST CALL A TOOL. Do not write a prose verdict. Do not summarize your
conclusion in text. The pipeline ignores all text output; only tool calls are processed.
After reading the CSS, your VERY NEXT action must be a call to either `approve_layout` or
`reject_layout`. No exceptions.
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
