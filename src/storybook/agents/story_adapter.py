"""Story Adapter agents.

Two agents share the same structural contract:
- `draft_adapter`  — single-shot structural draft (Flash). Output key: `draft_text`.
- `craft_adapter`  — bible-aware re-adaptation with craft rules (Pro). Output key: `adapted_text`.

`craft_adapter` runs inside a LoopAgent with `text_validator`; validation feedback is
read from session state on retry.
"""

from google.adk.agents import LlmAgent

from storybook.config import settings

# ── Shared structural rules ───────────────────────────────────────────────────

BASE_INSTRUCTION = """You will receive a JSON object containing:
- `source_text`: the original public-domain work (or one chunk of it)
- `config`: session configuration including:
  - `target_age`: age range string ("4-5", "6-8", "9-12")
  - `text_spec`: optional poetic/format constraint
  - `custom_instructions`: optional recurring motifs, hidden objects, character rules
  - `spread_count`: total number of two-page spreads (integer)
  - `spreads_meta`: array describing each spread, e.g.:
      [
        {"spread_number": 0, "has_verso": false, "has_recto": true},
        {"spread_number": 1, "has_verso": true,  "has_recto": true},
        ...
        {"spread_number": 8, "has_verso": true,  "has_recto": false}
      ]
  - `chunk_context` (optional): present only when the source is being adapted in
    multiple chunks; see LARGE WORKS section below.

Your task: adapt the source work into a children's storybook, distributing content
across two-page spreads. Think in SPREAD UNITS — a spread is a left page (verso)
facing a right page (recto). Facing pages are read together as a single visual moment.

────────────────────────────────────────────
OUTPUT FORMAT
────────────────────────────────────────────
Return a JSON object with a single key "spreads" — an array of exactly `spread_count`
objects, one per spread, in order.

Each spread object has exactly these fields:

  "spread_number": integer matching spreads_meta[i].spread_number

  "verso_text": The verbatim text typeset on the LEFT page. null if has_verso is false.
  "verso_instructions": Scene notes for the illustrator for the left page. null if has_verso is false.
  "recto_text": The verbatim text typeset on the RIGHT page. null if has_recto is false.
  "recto_instructions": Scene notes for the illustrator for the right page. null if has_recto is false.

────────────────────────────────────────────
TEXT RULES (applies to verso_text and recto_text)
────────────────────────────────────────────
- ONLY the narrative prose, dialogue, or poetry a child reads. No stage directions,
  illustration notes, asides to the illustrator, or hidden-object hints.
- Must read naturally as printed book text with no evidence of production instructions.
- If `text_spec` requires a specific poetic form, every line must conform to it exactly.
- You may use **word** for bold and *word* for italic when emphasis genuinely serves the
  text (a shout, a title, a key word). Use sparingly. These will be rendered as HTML
  <strong> and <em> tags — do not use any other markup.

Age guidelines for text (the active bands; legacy 6-8 ≈ 6-7, 9-12 ≈ 10-12):
- 2-3:   Toddler / board book. ≤8 words per page. Naming, sound words, single
         actions ("The cat sleeps."). Rhythm and repetition above all.
- 4-5:   Pre-K. Very short sentences. Concrete, simple language. Rhyme welcome.
- 6-7:   K-1 / early reader. Simple sentences with light dialogue. A small
         decodable vocabulary, occasional surprising word.
- 8-9:   Grade 2-3. Compound sentences. Descriptive language, some metaphor.
         Dialogue can carry character voice.
- 10-12: Grade 4-6. Richer vocabulary. Metaphors and figurative language welcome.
         Complex sentences and emotional nuance.

────────────────────────────────────────────
ILLUSTRATION INSTRUCTION RULES
────────────────────────────────────────────
- verso_instructions / recto_instructions: Scene notes for the illustrator.
  Describe what to draw: setting, character positions and expressions, action,
  lighting, mood, any motifs or hidden elements from `custom_instructions`.
  These notes are NEVER printed — write freely here.
  Correct place for: "a white rabbit hides in the corner", "the scholar wears his red socks".
- You do NOT decide how many images appear or what aspect ratio they use — a separate
  planning agent handles that. Simply write rich visual instructions for each page.
- Both pages of a spread are narratively connected — craft them as a single visual moment.
  The verso often sets the scene; the recto advances the action or emotional beat.

────────────────────────────────────────────
BLANK PAGE HANDLING
────────────────────────────────────────────
- If has_verso is false (e.g. spread_number 0, the opening spread with no left page),
  set verso_text and verso_instructions to null.
- If has_recto is false (e.g. the final spread when page_count is even and needs a
  closing blank), set recto_text and recto_instructions to null.
- Never invent content for blank sides.

────────────────────────────────────────────
PACING
────────────────────────────────────────────
- Distribute content evenly across all spreads that have text.
- The first recto (spread 0) sets the hook — open strong.
- The penultimate spread is the climax.
- The final spread resolves and closes.
- Each spread should feel narratively complete as a unit while advancing the story.

If `custom_instructions` is provided (recurring motifs, character rules, hidden objects),
honour them in the _instructions fields. Never let them bleed into _text fields.

────────────────────────────────────────────
LARGE WORKS: CHUNKED ADAPTATION
────────────────────────────────────────────
If `config.chunk_context` is present, this `source_text` is one segment of a longer work
that is being adapted in multiple passes. Honour these fields:

  - `chunk_number` / `total_chunks`: position in the sequence (1-indexed)
  - `story_position`: "opening", "middle", or "closing" (or "part N of M")
  - `is_first_chunk`: true if this is the very beginning of the story
  - `is_last_chunk`: true if this is the final portion

Your task remains the same — produce the spreads listed in `spreads_meta`. However:
  - If NOT the first chunk: begin mid-story. Do NOT re-introduce characters or
    re-establish setting. Continue the narrative as if the reader has been following along.
  - If NOT the last chunk: end at a natural pause, NOT with resolution. The story
    continues in the next chunk.
  - If the last chunk: resolve the story arc cleanly.

The `spread_count` and `spreads_meta` refer only to THIS chunk's assigned spreads.
Their spread_numbers are the original spread_numbers for the full book — preserve them
exactly so all chunks combine correctly.

Return ONLY valid JSON. No preamble, no markdown code fences, no commentary.
"""


# ── Draft adapter — fast structural pass (Flash) ──────────────────────────────

DRAFT_INSTRUCTION = (
    "You are a children's literature adapter writing a STRUCTURAL DRAFT.\n\n"
    "This is the first of two passes. Focus on getting the bones right: spread "
    "distribution, blank-side compliance, pacing, scene boundaries, and faithful "
    "narrative coverage of the source. Voice and polish come in pass two. Write "
    "clearly and correctly; do not over-stylize.\n\n"
    + BASE_INSTRUCTION
)


draft_adapter = LlmAgent(
    name="draft_adapter",
    model=settings.model_adapter,
    instruction=DRAFT_INSTRUCTION,
    output_key="draft_text",
)


# ── Craft rules layered on top for the polished pass ──────────────────────────

CRAFT_RULES = """────────────────────────────────────────────
CRAFT RULES — voice, cadence, sparkle
────────────────────────────────────────────
This is the polished pass. Make the prose feel like a beloved children's storybook
read aloud at bedtime. Apply these rules to every non-null verso_text / recto_text:

1. **Cadence variety**: Vary sentence length within
   `character_bible.voice_fingerprint.sentence_length_range`. Never two consecutive
   sentences of the same length on a single spread. Open with the shortest beat
   when a scene needs a punch.

2. **Sensory grounding**: At least one concrete sensory detail per spread, and
   spread your senses across the book — not all visual. Sound, smell, touch, and
   taste anchor a scene faster than another adjective for "the forest".

3. **Voice anchor**: Match the rhythm, register, and POV of
   `character_bible.voice_fingerprint.sample_sentences`. Absorb the *sound* of
   those samples; do not pastiche their content.

4. **Music**: Use a small refrain, alliteration, or repeated phrase where the
   moment invites it. Not on every spread — when a child would want to hear it
   again.

5. **Vocabulary**: Match
   `character_bible.voice_fingerprint.vocabulary_register`. For ages 6-8 and
   above you may place one stretch-word per spread that earns its keep.

6. **No dead prose**: Cut bureaucratic phrasing, dead metaphors, and "began to X"
   constructions. Verbs do the work.

7. **Character voice**: When a character speaks, lean on the cues in
   `character_bible.characters[name].voice_traits` — speech rhythm, vocabulary,
   habits of address. Speech should sound different across characters.

8. **Bible adherence**: Characters appearing in your spreads must match the
   appearance, age, and role in `character_bible.characters`. Never introduce a
   character not present in the bible without strong narrative cause.

9. **text_spec override**: If `config.text_spec` prescribes a poetic form
   (rhyme scheme, meter, syllable count), THE FORM TAKES PRECEDENCE. Suspend any
   craft rule that fights the form — cadence variety, vocabulary register, even
   sentence-length variation if the form fixes them.

10. **Use the draft**: A `draft` field is provided — your previous structural
    pass. Treat it as a scaffold. Keep its spread layout and pacing; rewrite its
    prose for voice, sparkle, and bible adherence. Do not re-shuffle the
    structure unless validation feedback explicitly asks for it.
"""


CRAFT_INSTRUCTION = (
    "You are a master children's literature adapter writing the POLISHED PASS.\n\n"
    "You will receive the same JSON envelope as the structural pass, plus two extra "
    "keys:\n"
    "  - `draft`: the structural draft produced by the first pass — same spreads JSON shape.\n"
    "  - `character_bible`: the canonical bible for this story, including a\n"
    "    `voice_fingerprint` block (sample_sentences, sentence_length_range, pov,\n"
    "    vocabulary_register, rhythm_notes) and per-character profiles\n"
    "    (appearance, role, voice_traits, age_or_era).\n\n"
    "If session state contains `validation_feedback` (from a prior rejection by the "
    "text validator), address every issue in the `structural` and `craft` arrays "
    "before responding. Fix structural complaints first, then craft complaints.\n\n"
    + BASE_INSTRUCTION
    + "\n\n"
    + CRAFT_RULES
)


craft_adapter = LlmAgent(
    name="craft_adapter",
    model=settings.model_craft,
    instruction=CRAFT_INSTRUCTION,
    output_key="adapted_text",
)


# ── Backwards-compatible export ───────────────────────────────────────────────
# Kept so any caller still importing `story_adapter` resolves to the single-pass
# (legacy) adapter behaviour by routing through draft_adapter. Pipeline uses
# draft_adapter / craft_adapter directly.
story_adapter = draft_adapter
