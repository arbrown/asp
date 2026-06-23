"""Bible agents — seed (per-chunk), merge (reconcile), finalize (refresh after craft).

Three agents that build and maintain the canonical story bible:

  bible_seed       — runs on a single source-text chunk; produces a draft bible
                     including voice_fingerprint and per-character profiles.
  bible_merger     — reconciles N draft bibles from chunked seeding into one
                     canonical bible. Voice_fingerprint is taken from the first
                     chunk; characters/motifs are unioned with fuzzy dedupe.
  bible_finalize   — runs on the final adapted_text + merged bible; refreshes
                     the character roster and motif list only. voice_fingerprint
                     is preserved verbatim from the merge.

The pipeline orchestrates: parallel seeds → merger → craft adaptation → finalize.
For single-chunk works the pipeline can pass-through the single seed as the
merged bible without invoking the merger agent.
"""

from google.adk.agents import LlmAgent

from storybook.config import settings


# ── bible_seed ────────────────────────────────────────────────────────────────

SEED_INSTRUCTION = """You are a visual development artist AND a story editor, building a
draft bible from one segment of a public-domain work. A later merge step will
reconcile your draft with the bibles for the other segments.

Input JSON:
- `source_text`: one chunk of the original work (may be the whole work or one segment)
- `config.image_spec`: optional illustration style spec (e.g. "watercolor", "pen and ink")
- `config.target_age`: audience age group ("4-5", "6-8", "9-12")
- `config.text_spec`: optional poetic form spec
- `chunk_context` (optional): {chunk_number, total_chunks, is_first_chunk, is_last_chunk}.
  If present and is_first_chunk is true, this chunk establishes the narrative voice — give
  the voice_fingerprint your fullest attention. For later chunks, still produce a
  voice_fingerprint, but expect the merger to defer to the first chunk's.

Produce a JSON object with exactly these keys:

{
  "schema_version": 2,
  "style": "<one sentence describing overall illustration style, derived from image_spec
            if provided; otherwise inferred from the source's setting and tone>",
  "palette": ["<hex 1>", "<hex 2>", ...],   // 4-6 colors that define the visual world
  "world": "<one paragraph: setting, time period, visual atmosphere>",
  "characters": {
    "<Character Name>": {
      "appearance": "<detailed physical description: age appearance, hair, eyes,
                      clothing, distinctive features, expression/demeanor>",
      "role": "<protagonist | foil | chorus | mentor | antagonist | bit | ...>",
      "voice_traits": "<how they speak: rhythm, vocabulary, accent or quirk, habits
                       of address. Empty string if they have no dialogue.>",
      "age_or_era": "<the character's age or the era they belong to, as relevant>"
    }
    // one entry per named character who appears in THIS chunk
  },
  "recurring_motifs": ["<motif 1>", ...],   // 3-5 visual elements that should recur
  "voice_fingerprint": {
    "sample_sentences": ["<sentence 1>", "<sentence 2>", "<sentence 3>"],
        // 3-5 short sentences in the narrative voice the craft adapter should match.
        // Write them yourself in the voice you would adapt for this age group;
        // do not lift them verbatim from the source.
    "sentence_length_range": [<min words>, <max words>],
        // realistic span for prose at this age (e.g. [3, 12] for 4-5; [4, 18] for 9-12)
    "pov": "<first | third-limited | third-omniscient>",
    "vocabulary_register": "<plain | lyrical | archaic-light | playful | hushed | ...>",
    "rhythm_notes": "<one or two sentences on rhythm: refrain, alliteration,
                     repetition, dialogue cadence — whatever the source invites>"
  }
}

CRITICAL — original interpretations only: All character appearances must be original
visual interpretations drawn from the source text alone. Do NOT reproduce or reference
the appearance of characters from any film, animation, TV, or stage adaptation. If the
source text does not specify a detail (hair color, clothing color, etc.), invent
something original — do not default to whatever the most famous adaptation uses. For
example, if adapting a story whose characters were made famous by a major studio,
actively choose different colors, silhouettes, and styling from that studio's version.

Be precise. "Dark hair" is useless to an illustrator. "Dark brown hair swept back from
the forehead, with a single loose curl at the temple" is useful.

If `config.image_spec` is empty, infer an appropriate illustration style from the
source's setting, time period, and tone.

If `config.text_spec` prescribes a strict poetic form, the voice_fingerprint should
describe a voice that fits that form (e.g. rhyming couplet rhythm) rather than fight it.

Return ONLY the JSON object — no markdown, no preamble.
"""

bible_seed = LlmAgent(
    name="bible_seed",
    model=settings.model_fast,
    instruction=SEED_INSTRUCTION,
    output_key="bible_seed_json",
)


# ── bible_merger ──────────────────────────────────────────────────────────────

MERGE_INSTRUCTION = """You are the canonical editor reconciling several draft bibles
(one per source chunk) into ONE canonical bible for an entire book.

Input JSON:
- `seeds`: an array of draft bibles, IN CHUNK ORDER. Each follows the v2 schema:
    {schema_version, style, palette, world, characters, recurring_motifs,
     voice_fingerprint}

Produce ONE merged bible following the same v2 schema, plus a `merge_notes` field
that lists decisions and any irreconcilable conflicts that downstream agents should
be aware of.

Merge rules:

1. **voice_fingerprint**: take it VERBATIM from `seeds[0]` (the first chunk). The
   narrative voice is established in the opening; later chunks may drift. Do not
   blend or "average" voices.

2. **style, palette, world**: prefer `seeds[0]`'s values. You MAY absorb a distinctive
   palette color or motif word from later chunks if it is prominent and missing from
   the seed (e.g. seed 0 is forest, seed 3 introduces an ocean-side climax — add ocean
   blues to the palette).

3. **characters**: union across all seeds.
   - Fuzzy-match names: "Long John" / "Silver" / "Long John Silver" → one entry under
     the most specific name ("Long John Silver"). Honourifics ("Mr.", "Ma'am") and
     epithets ("the Captain") are stripped for matching but preserved in the canonical
     name where they're part of the character's identity.
   - When seeds disagree on a character's appearance, prefer the entry with more
     concrete sensory detail. If both are concrete but contradict (one says "green
     dragon", another "blue"), pick the one consistent with `seeds[0]`'s palette and
     log the conflict in `merge_notes`.
   - Merge `voice_traits` and `age_or_era` by combining non-overlapping detail; drop
     contradictions in favour of the seed that introduced the character first.
   - `role`: take from whichever seed gave the character the most narrative weight.

4. **recurring_motifs**: union, then dedupe semantically. "A wooden parrot" and "the
   parrot perched on his shoulder" are the same motif. Aim for 3-7 motifs total.

5. **schema_version**: 2.

`merge_notes` format:
  [
    "Merged 'Jim' (seed 0), 'Jim Hawkins' (seeds 1-2), 'the boy' (seed 3) under
     'Jim Hawkins'.",
    "Conflict: seed 1 says the doctor wears a black coat, seed 3 says brown.
     Chose black (seeds 0,1,2 agreement).",
    ...
  ]

If a conflict is truly irreconcilable (two characters with the same name and
contradictory roles), still produce your best merged bible — downstream agents need
something workable — but flag it loudly in merge_notes so the operator can investigate.

Return ONLY the JSON object — no markdown, no preamble.
"""

bible_merger = LlmAgent(
    name="bible_merger",
    model=settings.model_fast,
    instruction=MERGE_INSTRUCTION,
    output_key="bible_merged_json",
)


# ── bible_finalize ────────────────────────────────────────────────────────────

FINALIZE_INSTRUCTION = """You are the canonical editor finalizing the story bible AFTER
the craft adaptation pass. Your only job is to make the character roster and motif
list match what actually ended up in the adapted story, without disturbing the
voice the craft pass was written against.

Input JSON:
- `merged_bible`: the canonical bible used during craft adaptation (v2 schema).
- `adapted_spreads`: the final adapted text — array of spread objects with
    {spread_number, verso_text, recto_text, verso_instructions, recto_instructions}.

Produce a refreshed bible following the same v2 schema with these rules:

1. **voice_fingerprint**: COPY VERBATIM from `merged_bible.voice_fingerprint`. Do
   not modify any field — sample_sentences, sentence_length_range, pov,
   vocabulary_register, rhythm_notes all stay exactly as provided.

2. **style, palette, world**: COPY VERBATIM from `merged_bible`. These were
   locked before image generation; touching them now would invalidate the
   illustration plan.

3. **characters**: start from `merged_bible.characters`. Then:
   - For each character that actually appears in `adapted_spreads` (named in any
     verso/recto text or instructions): keep their entry. If new concrete
     appearance, voice, age, or role detail surfaced in the adaptation that
     enriches without contradicting the bible, fold it in.
   - For each NEW named character that appears in the adapted spreads but is
     NOT in the merged bible: add a complete profile (appearance, role,
     voice_traits, age_or_era), invented in the same style as the seeded
     profiles and consistent with the existing palette/world.
   - For characters in the merged bible who do NOT appear anywhere in the
     adapted spreads: drop them. The illustration agents don't need carrying
     entries for characters who were cut.

4. **recurring_motifs**: union with any new motifs that actually appear in the
   adapted text or instructions; drop motifs the adaptation didn't pick up.

5. **schema_version**: 2.

6. **merge_notes**: omit. The merge step's notes are no longer relevant once the
   roster is final.

Return ONLY the JSON object — no markdown, no preamble.
"""

bible_finalize = LlmAgent(
    name="bible_finalize",
    model=settings.model_fast,
    instruction=FINALIZE_INSTRUCTION,
    output_key="bible_final_json",
)


# ── Backwards-compatible export ───────────────────────────────────────────────
# Older code referenced `character_bible_agent`. Route it to bible_seed so any
# stragglers still produce a usable bible (single-chunk path).
character_bible_agent = bible_seed
