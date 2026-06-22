from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from storybook.models import SessionConfig


class CreateSessionRequest(BaseModel):
    config: SessionConfig


class SessionResponse(BaseModel):
    session_id: str
    current_stage: str
    progress_pct: int
    config: Optional[SessionConfig] = None
    pdf_signed_url: Optional[str] = None
    wide_pdf_url: Optional[str] = None
    trace_url: Optional[str] = None
    errors: list[str] = []
    resumable: bool = False


class ProgressEvent(BaseModel):
    event: str = "progress"
    stage: str
    pct: int
    message: Optional[str] = None
    spread: Optional[int] = None
    page: Optional[int] = None
    of: Optional[int] = None
    signed_url: Optional[str] = None
    session_id: Optional[str] = None
    attempt: Optional[int] = None
    reason: Optional[str] = None
