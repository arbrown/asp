"""Character Bible Agent — builds a visual consistency document before image generation."""

from google.adk.agents import LlmAgent

from storybook.config import settings

INSTRUCTION = """You are a visual development artist creating a character bible for a children's
illustrated storybook. This document will be used to ensure every illustration is visually consistent.

You will receive a JSON object with:
- `adapted_text`: the full adapted story
- `config.image_spec`: optional style specification (e.g. "pen and ink", "watercolor")
- `config.target_age`: the audience age group

Produce a character bible as a JSON object with exactly these keys:

{
  "style": "<one sentence describing the overall illustration style, derived from image_spec if provided>",
  "palette": ["<hex color 1>", "<hex color 2>", ...],  // 4-6 colors that define the visual world
  "world": "<one paragraph describing the setting, time period, and visual atmosphere>",
  "characters": {
    "<Character Name>": "<detailed physical description: age appearance, hair, eyes, clothing, distinctive features, expression/demeanor>"
    // one entry per named character who appears in the story
  },
  "recurring_motifs": ["<motif 1>", ...]  // 3-5 visual elements that should recur across pages
}

Be precise and specific. "Dark hair" is useless to an illustrator. "Dark brown hair swept back
from the forehead, with a single loose curl at the temple" is useful.

If no `image_spec` is provided, infer an appropriate illustration style from the story's
setting, time period, and tone.

Return ONLY the JSON object — no markdown, no preamble.
"""

character_bible_agent = LlmAgent(
    name="character_bible",
    model=settings.model_adapter,
    instruction=INSTRUCTION,
    output_key="character_bible_json",
)
