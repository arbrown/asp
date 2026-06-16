"""Text Validator — evaluates the structured story output against config constraints."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a rigorous children's literature validator.

The story adapter outputs a JSON object with a "pages" array. Each element has:
- "story_text": the verbatim text that will be printed on that page
- "page_instructions": illustrator notes that are never printed (do not validate these)

You also have access to the original config with `target_age`, `text_spec`, `page_count`.

Validate the story on these dimensions:

1. **Page count**: Does the array contain exactly `page_count` pages? Reject immediately
   if not — the adapter must produce the exact number requested.

2. **Story_text purity**: Each page's story_text must contain ONLY printable story content.
   Reject if any page's story_text contains:
   - Page numbers or labels ("Page 1:", "Scene:", chapter headings)
   - Illustration directions or notes to the artist
   - Hidden-object hints describing the illustration rather than the story
   - Stage directions, bracketed asides, or production meta-text
   - Anything that would look wrong typeset in a printed children's book

3. **Age appropriateness**: Is vocabulary, sentence length, and content suitable for
   `target_age`?

4. **Completeness**: Is there a clear narrative arc across all pages? Does each page have
   enough text to stand on its own?

5. **text_spec conformance** (only if `config.text_spec` is non-empty): Does each page's
   story_text strictly conform? Check meter, rhyme scheme, line count, and stanza form.
   Identify violations by page number and specific line.

If ALL checks pass, call `approve_text`.
If ANY check fails, call `reject_text` with specific, actionable feedback identifying
the exact page numbers and issues. Do not give vague feedback.
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
