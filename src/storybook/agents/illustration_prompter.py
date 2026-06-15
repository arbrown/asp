"""Illustration Prompter — crafts a detailed, bible-consistent image prompt for one page."""

from google.adk.agents import LlmAgent

from storybook.config import settings

INSTRUCTION = """You are an expert art director writing image generation prompts for
children's storybook illustrations.

You will receive a JSON object with:
- `page_text`: the text content of the current page
- `page_number`: current page number (1-based)
- `total_pages`: total number of pages
- `character_bible`: the visual consistency document (style, palette, characters, motifs)
- `config.image_spec`: optional additional style instructions
- `is_first_page`: boolean — first page sets the visual standard for all subsequent pages

Your task: write a single, comprehensive image generation prompt for this page's illustration.

Rules:
1. Describe the scene depicted in `page_text` concretely — what is happening, who is present,
   what is the setting.
2. Include the relevant character descriptions from `character_bible.characters` verbatim
   for any character who appears in this scene.
3. Include the `character_bible.style` and relevant palette colors.
4. Reference 1-2 `character_bible.recurring_motifs` if they fit naturally.
5. Specify: children's illustration, age-appropriate, no violence, no adult content.
6. Keep the prompt under 300 words. Dense and specific beats long and vague.
7. Do NOT include text, words, or letters in the image.

If this is the first page, add: "This is the establishing illustration — set the definitive
visual style for the entire book."

Return ONLY the image prompt text. No preamble, no JSON.
"""

illustration_prompter = LlmAgent(
    name="illustration_prompter",
    model=settings.model_fast,
    instruction=INSTRUCTION,
    output_key="image_prompt",
)
