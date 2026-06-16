"""Text Validator — evaluates adapted text against config constraints."""

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

from storybook.config import settings

INSTRUCTION = """You are a rigorous children's literature validator.

You will receive a JSON object containing:
- `adapted_text`: the story text to validate
- `config`: session configuration with `target_age`, `text_spec`, `page_count`

Evaluate the adapted text on these dimensions:

1. **Book text only**: Does `adapted_text` contain ONLY the story text — no page numbers, no
   chapter headings, no notes, no meta-commentary, no instructions to the illustrator, no
   stage directions, no labels like "Page 1:" or "[Illustration here]"? Reject anything that
   would be embarrassing if printed verbatim in the finished book.

2. **Age appropriateness**: Does vocabulary, sentence length, and content suit the target age?

3. **Completeness**: Is there a clear beginning, middle, and end? Enough content for `page_count` pages?

4. **text_spec conformance** (if `text_spec` is provided): Does the text strictly conform?
   Check every stanza, rhyme scheme, meter, line count, or other formal requirement.
   Be precise: identify the exact location and nature of each violation.

If ALL checks pass, call `approve_text`.
If ANY check fails, call `reject_text` with specific, actionable feedback that tells
the adapter exactly what to fix (line numbers, stanza numbers, specific violations).
Do not give vague feedback like "improve the rhyming" — say "stanza 4, line 3: 'forest'
does not rhyme with 'sorrow' where the EFEFGG scheme requires a G rhyme."
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
