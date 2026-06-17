"""Text Validator — evaluates the structured story output against config constraints."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a rigorous children's literature validator.

The story adapter outputs a JSON object with a "spreads" array. Each element has:
- "spread_number": integer (0-based spread index)
- "verso_text": the verbatim text printed on the LEFT page (null if this side is blank)
- "verso_instructions": illustrator notes for the left page (never printed — do not validate)
- "recto_text": the verbatim text printed on the RIGHT page (null if this side is blank)
- "recto_instructions": illustrator notes for the right page (never printed — do not validate)

You also have access to the original config with `target_age`, `text_spec`, `spread_count`.

Validate the story on these dimensions:

1. **Spread count**: Does the array contain exactly `spread_count` spreads? Reject immediately
   if not — the adapter must produce the exact count requested.

2. **Blank side compliance**: Spread 0 must have verso_text=null (it is the opening blank
   page). If spread_count indicates a trailing blank (the last spread), its recto_text must
   be null. Any non-null text on a blank side is an error.

3. **Text purity**: Each non-null verso_text and recto_text must contain ONLY printable
   story content. Reject if any text contains:
   - Page numbers or labels ("Page 1:", "Scene:", chapter headings)
   - Illustration directions or notes to the artist
   - Hidden-object hints or stage directions
   - Bracketed asides or production meta-text
   - Anything that would look wrong typeset in a printed children's book

4. **Age appropriateness**: Is vocabulary, sentence length, and content suitable for
   `target_age`?

5. **Completeness**: Is there a clear narrative arc across all spreads? Does each live side
   have enough text to stand on its own as a printed page?

6. **text_spec conformance** (only if `config.text_spec` is non-empty): Does each non-null
   text field strictly conform? Check meter, rhyme scheme, line count, and stanza form.
   Identify violations by spread_number and side (verso/recto) with the specific line.

If ALL checks pass, call `approve_text`.
If ANY check fails, call `reject_text` with specific, actionable feedback identifying
the exact spread numbers, sides, and issues. Do not give vague feedback.
"""


def approve_text(tool_context: ToolContext) -> dict:
    """Signal that the adapted text has passed all validation checks."""
    tool_context.actions.escalate = True
    return {"status": "approved"}


def reject_text(feedback: str, tool_context: ToolContext) -> dict:
    """
    Signal that the adapted text failed validation and must be revised.

    Args:
        feedback: Specific, actionable description of each violation found.
    """
    tool_context.state["validation_feedback"] = feedback
    return {"status": "rejected", "feedback": feedback}


text_validator = LlmAgent(
    name="text_validator",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    tools=[approve_text, reject_text],
)
