from __future__ import annotations

import uuid
from typing import Optional

from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    gutenberg_url: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None


AGE_PARAMS: dict[str, dict] = {
    "4-5": {"max_words_per_page": 20, "reading_level": "Pre-K / Primer"},
    "6-8": {"max_words_per_page": 50, "reading_level": "Grade 1-2"},
    "9-12": {"max_words_per_page": 100, "reading_level": "Grade 3-5"},
}


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


class CharacterBible(BaseModel):
    style: str
    palette: list[str] = []
    world: str
    characters: dict[str, str] = {}
    recurring_motifs: list[str] = []


class ValidationResult(BaseModel):
    passed: bool
    feedback: str = ""


class PipelineState(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    config: SessionConfig

    source_text: str = ""
    adapted_text: str = ""
    pages: list[StoryPage] = []
    character_bible: Optional[CharacterBible] = None
    layout_spec: Optional[dict] = None
    image_gcs_uris: list[str] = []
    html_gcs_uris: list[str] = []
    pdf_gcs_uri: str = ""

    current_stage: str = "initializing"
    progress_pct: int = 0
    errors: list[str] = []
