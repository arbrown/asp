"""Spread Layout Verifier — checks that a spread's HTML implements the illustration plan correctly."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a layout quality reviewer for children's storybook spreads.
You are a multimodal agent — you will receive the HTML source AND the actual illustration image(s).

You receive a JSON text part with:
- `html_code`: the full HTML source of the rendered spread (17×11 wide, images stripped)
- `illustration_plan`: array of illustration entries, each with:
    { "image_index": 0|1, "coverage": "full"|"verso"|"recto", "aspect_ratio": "16:9"|"3:4"|"1:1" }
- `verso_text`: story text for the left page (may be null)
- `recto_text`: story text for the right page (may be null)
- `spread_number`: which spread this is

After the JSON text part you receive the actual illustration image(s). USE THEM to judge legibility.

────────────────────────────────────────────
WHAT TO CHECK (in order of importance)
────────────────────────────────────────────

1. LEGIBILITY — Is the text readable against the actual image? (MOST IMPORTANT)
   Look at the image AND the HTML. For text overlaid on an image, there are three treatments:
   - "gradient_dark": `.text-backdrop` has a dark gradient background — white text. Good default.
   - "gradient_light": `.text-backdrop` has a light/warm gradient — dark text. For dark images.
   - "direct": `.text-backdrop` has transparent background — colored text with drop shadow.

   REJECT only if there is a genuine legibility failure:
   - The bottom third of the image is bright, busy, or high-contrast AND the chosen treatment
     is "direct" or "gradient_light" — the text would be unreadable.
   - The image is very dark throughout AND "gradient_dark" is used — the dark gradient
     disappears into the dark image. "gradient_light" would be better.
   - The font size is clearly too small (below minimum for age group).
   - There is NO backdrop element at all for text sitting over an image.

   DO NOT reject because of a particular color choice if the text is actually readable.
   The planner makes intentional choices — only override them when legibility is genuinely broken.

2. STRUCTURAL — Does the image appear on the correct side?
   - "full": img spans the full spread (100% width)
   - "verso": img is on the left page (8.5in); text column on right
   - "recto": img is on the right page; text column on left
   - Two images ("dual"): one per page

3. PROPORTIONAL — Is the image scaled correctly?
   - Must use `object-fit: cover` or `object-fit: contain` — no distortion.

────────────────────────────────────────────
REJECTION THRESHOLD
────────────────────────────────────────────
Reject for:
- Actual legibility failure: text is genuinely unreadable against the image
- Image on wrong side (verso vs recto mismatch)
- No backdrop element at all for text over an image
- Visible distortion

Do NOT reject for:
- A non-dark-gradient treatment that is still readable (intentional planner choice)
- Minor padding differences
- Font choice, accent colors on non-overlay pages
- Small size differences within 10%

DEFAULT TO APPROVING when you are uncertain. Reserve rejection for clear, specific problems
you can name precisely with a concrete fix.

────────────────────────────────────────────
WHEN REJECTING — BE SPECIFIC AND STRUCTURED
────────────────────────────────────────────
`feedback`: exactly what is wrong and why it fails with the actual image content.

`suggested_fix`: plain English description of the remedy.

`css_overrides`: a JSON-serializable dict with any of these keys to fix the issue:
  - "text_treatment": "gradient_dark" — switch to dark gradient scrim with white text
                                        (use when image is bright/busy in the text zone)
  - "text_treatment": "gradient_light" — switch to light/warm gradient, dark text
                                         (use when image is very dark throughout)
  - "text_treatment": "direct" — no scrim, colored text with drop shadow
                                  (use when image has calm empty space for text)
  - "font_size_scale": 1.3 — multiply base font size by this factor (e.g. 1.2–1.5)

Example css_overrides: {"text_treatment": "gradient_dark"}

CRITICAL — YOU MUST CALL A TOOL. Do not write a prose verdict. Do not summarize your
conclusion in text. The pipeline ignores all text output; only tool calls are processed.
After reading the HTML and images, your VERY NEXT action must be a call to either
`approve_layout` or `reject_layout`. No exceptions.
"""


def approve_layout(tool_context: ToolContext) -> dict:
    """Signal that the spread HTML passes all layout checks."""
    tool_context.actions.escalate = True
    return {"status": "approved"}


def reject_layout(
    feedback: str,
    suggested_fix: str,
    css_overrides: dict,
    tool_context: ToolContext,
) -> dict:
    """
    Signal that the spread HTML fails layout validation.

    Args:
        feedback: Specific description of what is wrong (reference actual image content).
        suggested_fix: Plain English remedy.
        css_overrides: Dict of overrides to apply on re-render.
            Valid keys: text_treatment ("gradient_dark"|"gradient_light"|"direct"), font_size_scale (float).
    """
    tool_context.state["layout_feedback"] = feedback
    tool_context.state["layout_suggested_fix"] = suggested_fix
    tool_context.state["layout_css_overrides"] = css_overrides
    return {"status": "rejected", "feedback": feedback, "css_overrides": css_overrides}


html_page_verifier = LlmAgent(
    name="html_page_verifier",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[approve_layout, reject_layout],
)
