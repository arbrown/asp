from __future__ import annotations

import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceConfig(BaseModel):
    gutenberg_url: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None


AGE_PARAMS: dict[str, dict] = {
    # Active 5-bucket scheme (finer at the young end, mirrors publisher segments)
    "2-3":   {"max_words_per_page": 8,   "reading_level": "Toddler / board book"},
    "4-5":   {"max_words_per_page": 20,  "reading_level": "Pre-K / Primer"},
    "6-7":   {"max_words_per_page": 40,  "reading_level": "K-1 / early reader"},
    "8-9":   {"max_words_per_page": 70,  "reading_level": "Grade 2-3"},
    "10-12": {"max_words_per_page": 110, "reading_level": "Grade 4-6"},
    # Legacy keys retained so older sessions resume cleanly
    "6-8":   {"max_words_per_page": 50,  "reading_level": "Grade 1-2 (legacy)"},
    "9-12":  {"max_words_per_page": 100, "reading_level": "Grade 3-5 (legacy)"},
}

# Canonical list for UIs and prompts (excludes legacy)
ACTIVE_AGE_RANGES: list[str] = ["2-3", "4-5", "6-7", "8-9", "10-12"]


class SessionConfig(BaseModel):
    source: SourceConfig
    target_age: str = "4-5"
    page_count: int = 12
    language: str = "en"
    text_spec: Optional[str] = None
    image_spec: Optional[str] = None
    custom_instructions: Optional[str] = None

    @property
    def age_params(self) -> dict:
        return AGE_PARAMS.get(self.target_age, AGE_PARAMS["4-5"])


class StoryPage(BaseModel):
    story_text: str          # Verbatim text printed in the book
    page_instructions: str = ""  # Scene notes for the illustrator, never printed


class IllustrationEntry(BaseModel):
    image_index: int          # 0 or 1 within a spread
    coverage: str             # "full" | "verso" | "recto"
    aspect_ratio: str         # "16:9" | "3:4" | "1:1"
    illustration_notes: str


class SpreadContent(BaseModel):
    spread_number: int
    verso_text: Optional[str] = None
    verso_instructions: Optional[str] = None
    recto_text: Optional[str] = None
    recto_instructions: Optional[str] = None


class SpreadPlan(BaseModel):
    spread_number: int
    illustration_plan: list[IllustrationEntry] = []
    text_treatment: str = "gradient_dark"  # "gradient_dark" | "gradient_light" | "direct"
    text_position: str = "bottom"  # "top" | "bottom" — where text sits on image overlays


class CharacterProfile(BaseModel):
    model_config = ConfigDict(extra="allow")
    appearance: str = ""
    role: str = ""
    voice_traits: str = ""
    age_or_era: str = ""


class VoiceFingerprint(BaseModel):
    model_config = ConfigDict(extra="allow")
    sample_sentences: list[str] = []
    sentence_length_range: list[int] = [4, 14]
    pov: str = ""
    vocabulary_register: str = ""
    rhythm_notes: str = ""


class CharacterBible(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 2
    style: str = ""
    palette: list[str] = []
    world: str = ""
    characters: dict[str, CharacterProfile] = {}
    recurring_motifs: list[str] = []
    voice_fingerprint: VoiceFingerprint = Field(default_factory=VoiceFingerprint)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v1(cls, data: Any) -> Any:
        """Promote legacy v1 bibles (characters: dict[str, str], no voice_fingerprint)."""
        if not isinstance(data, dict):
            return data
        if data.get("schema_version", 1) >= 2:
            return data
        chars = data.get("characters") or {}
        if chars and all(isinstance(v, str) for v in chars.values()):
            data["characters"] = {name: {"appearance": desc} for name, desc in chars.items()}
        data.setdefault("voice_fingerprint", {})
        data["schema_version"] = 2
        return data


class ValidationResult(BaseModel):
    passed: bool
    feedback: str = ""


class PipelineState(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    config: SessionConfig

    source_text: str = ""
    draft_text: str = ""
    adapted_text: str = ""
    pages: list[StoryPage] = []
    character_bible: Optional[CharacterBible] = None
    layout_spec: Optional[dict] = None
    spread_contents: list[SpreadContent] = []
    spread_plans: list[SpreadPlan] = []
    image_gcs_uris: list[str] = []
    html_gcs_uris: list[str] = []
    pdf_gcs_uri: str = ""
    wide_pdf_gcs_uri: str = ""

    current_stage: str = "initializing"
    progress_pct: int = 0
    errors: list[str] = []
    trace_url: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
