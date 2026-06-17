"""Spread Layout Verifier — checks that a spread's HTML implements the illustration plan correctly."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a layout quality reviewer for children's storybook spreads.

You receive a JSON object with:
- `html_code`: the full HTML source of the rendered spread (17×11 wide)
- `illustration_plan`: array of illustration entries, each with:
    { "image_index": 0|1, "coverage": "full"|"verso"|"recto", "aspect_ratio": "16:9"|"3:4"|"1:1" }
- `verso_text`: story text for the left page (may be null)
- `recto_text`: story text for the right page (may be null)
- `spread_number`: which spread this is

────────────────────────────────────────────
WHAT TO CHECK
────────────────────────────────────────────

1. STRUCTURAL — does each image appear on the correct side?
   - coverage "full": <img> or background spans the full spread width (100% or ~17in)
   - coverage "verso": <img> is positioned on the left half (left page, ~8.5in width)
   - coverage "recto": <img> is positioned on the right half (right page, ~8.5in width)

2. AESTHETIC — is text legible?
   - For "full" coverage: text must be overlaid on a calm section of the image.
     Look for a gradient overlay, a semi-opaque backdrop, or explicit bottom-third calm zone.
     If text is overlaid with no contrast treatment, REJECT.
   - For "verso"/"recto": the text column must be on the OPPOSITE side from the image.
     If text and image are both on the same side, REJECT.
   - Text must not overflow its container (look for `overflow:hidden` or adequate padding).

3. PROPORTIONAL — is the image scaled correctly?
   - Images must use `object-fit: cover` or `object-fit: contain` (not stretched).
   - If an image is stretched by independent width/height percentages that would distort
     its aspect ratio, REJECT and note the fix (add `object-fit: cover`).
   - Empty illustration_plan (text-only spread): no <img> tags should be present.

────────────────────────────────────────────
DEFAULTS AND THRESHOLDS
────────────────────────────────────────────
DEFAULT TO APPROVING. If you are uncertain, or if the layout broadly matches the intent,
call `approve_layout`. Only call `reject_layout` for a clear, unambiguous problem:
- wrong side (image on recto when coverage says verso)
- text completely unreadable due to no contrast treatment on a full-bleed image
- image visibly distorted (different scale on each axis)

Minor CSS details, font choices, and color values are outside scope — do NOT reject for those.

CRITICAL — YOU MUST CALL A TOOL. Do not write a prose verdict. Do not summarize your
conclusion in text. The pipeline ignores all text output; only tool calls are processed.
After reading the HTML, your VERY NEXT action must be a call to either `approve_layout` or
`reject_layout`. No exceptions.
"""


def approve_layout(tool_context: ToolContext) -> dict:
    """Signal that the spread HTML passes all layout checks."""
    tool_context.actions.escalate = True
    return {"status": "approved"}


def reject_layout(feedback: str, suggested_fix: str, tool_context: ToolContext) -> dict:
    """
    Signal that the spread HTML fails layout validation.

    Args:
        feedback: Specific description of what is wrong in the HTML/CSS.
        suggested_fix: Concrete CSS or structural change that would fix the issue.
    """
    tool_context.state["layout_feedback"] = feedback
    tool_context.state["layout_suggested_fix"] = suggested_fix
    return {"status": "rejected", "feedback": feedback, "suggested_fix": suggested_fix}


html_page_verifier = LlmAgent(
    name="html_page_verifier",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[approve_layout, reject_layout],
)
