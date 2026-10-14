from __future__ import annotations

import json
import os
import posixpath
from datetime import timedelta
from pathlib import PurePosixPath

os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

from google.cloud import storage
from google.oauth2.credentials import Credentials as OAuth2Credentials

from storybook.api.auth import sanitize_email_for_gcs
from storybook.config import settings

_client: storage.Client | None = None
_scoped_user_email: str | None = None
_scoped_token: str | None = None

_LEGACY_DEFAULT_SAFE_USER = "legacy_at_storybook.local"


def set_scoped_credentials(
    user_email: str | None = None,
    token: str | None = None,
) -> None:
    """Configure the GCS client with a session-scoped user email and STS downscoped token."""
    global _client, _scoped_user_email, _scoped_token
    _scoped_user_email = user_email.strip().lower() if user_email else None
    _scoped_token = token.strip() if token else None
    if _scoped_token:
        creds = OAuth2Credentials(token=_scoped_token)
        _client = storage.Client(project=settings.gcp_project_id, credentials=creds)
    else:
        _client = None


def clear_scoped_credentials() -> None:
    """Reset any session-scoped GCS credentials back to ambient defaults."""
    global _client, _scoped_user_email, _scoped_token
    _scoped_user_email = None
    _scoped_token = None
    _client = None


def get_scoped_user_email() -> str | None:
    return _scoped_user_email


def _bucket() -> storage.Bucket:
    global _client
    if _client is None:
        if _scoped_token:
            creds = OAuth2Credentials(token=_scoped_token)
            _client = storage.Client(project=settings.gcp_project_id, credentials=creds)
        else:
            _client = storage.Client(project=settings.gcp_project_id)
    return _client.bucket(settings.gcs_artifacts_bucket)


def _validate_path_segments(session_id: str, parts: tuple[str, ...]) -> None:
    sid = session_id.strip()
    if not sid or ".." in sid or "/" in sid or "\\" in sid:
        raise ValueError(f"Invalid session_id for GCS path: {session_id!r}")
    for part in parts:
        if "\\" in part:
            raise ValueError(f"Invalid path separator in GCS blob path: {part!r}")
        for seg in part.split("/"):
            if seg in ("..", "."):
                raise ValueError(f"Path traversal detected in GCS blob path: {part!r}")


def _blob_path(
    session_id: str,
    *parts: str,
    user_email: str | None = None,
) -> str:
    _validate_path_segments(session_id, parts)
    effective_user = _scoped_user_email if user_email is None else user_email
    if effective_user:
        safe_user = sanitize_email_for_gcs(effective_user)
        base = str(PurePosixPath("users", safe_user, "sessions", session_id))
    else:
        # Fallback for un-scoped/legacy sessions
        base = str(PurePosixPath("sessions", session_id))

    if not parts:
        return base

    joined = str(PurePosixPath(base, *parts))
    normalized = posixpath.normpath(joined)
    if normalized != base and not normalized.startswith(base + "/"):
        raise ValueError(f"Path traversal outside session prefix is not allowed: {joined!r}")
    return normalized


def _can_fallback_to_legacy(user_email: str | None) -> bool:
    """Allow the configured legacy owner to read pre-existing legacy blobs under sessions/{id}/."""
    if _scoped_token:
        # Downscoped actor tokens are cryptographically confined to users/{user}/sessions/{id}/
        return False
    effective_user = _scoped_user_email if user_email is None else user_email
    if not effective_user:
        return False
    try:
        allowed_safe_users = {_LEGACY_DEFAULT_SAFE_USER}
        if settings.legacy_owner_email:
            allowed_safe_users.add(sanitize_email_for_gcs(settings.legacy_owner_email))
        return sanitize_email_for_gcs(effective_user) in allowed_safe_users
    except ValueError:
        return False


def write_text(
    session_id: str,
    *path_parts: str,
    content: str,
    user_email: str | None = None,
) -> str:
    """Write a text file to GCS and return its gs:// URI."""
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    blob = _bucket().blob(key)
    blob.upload_from_string(content, content_type="text/plain; charset=utf-8")
    return f"gs://{settings.gcs_artifacts_bucket}/{key}"


def write_json(
    session_id: str,
    *path_parts: str,
    data: dict,
    user_email: str | None = None,
) -> str:
    """Write a JSON file to GCS and return its gs:// URI."""
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    blob = _bucket().blob(key)
    blob.upload_from_string(json.dumps(data, indent=2), content_type="application/json")
    return f"gs://{settings.gcs_artifacts_bucket}/{key}"


def write_bytes(
    session_id: str,
    *path_parts: str,
    data: bytes,
    content_type: str,
    user_email: str | None = None,
) -> str:
    """Write binary data to GCS and return its gs:// URI."""
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    blob = _bucket().blob(key)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{settings.gcs_artifacts_bucket}/{key}"


def read_text(
    session_id: str,
    *path_parts: str,
    user_email: str | None = None,
) -> str:
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    try:
        return _bucket().blob(key).download_as_text()
    except Exception:
        if _can_fallback_to_legacy(user_email):
            legacy_key = _blob_path(session_id, *path_parts, user_email="")
            return _bucket().blob(legacy_key).download_as_text()
        raise


def read_bytes(
    session_id: str,
    *path_parts: str,
    user_email: str | None = None,
) -> bytes:
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    try:
        return _bucket().blob(key).download_as_bytes()
    except Exception:
        if _can_fallback_to_legacy(user_email):
            legacy_key = _blob_path(session_id, *path_parts, user_email="")
            return _bucket().blob(legacy_key).download_as_bytes()
        raise


def save_session_meta(
    session_id: str,
    data: dict,
    user_email: str | None = None,
) -> None:
    """Persist session metadata so it survives pod restarts."""
    write_json(session_id, "session.json", data=data, user_email=user_email)


def save_pipeline_state(
    session_id: str,
    state: object,
    user_email: str | None = None,
) -> str:
    """Persist full PipelineState checkpoint to state.json and session.json."""
    from storybook.models import PipelineState

    if isinstance(state, PipelineState):
        payload = state.model_dump(mode="json")
        # Keep source_text in original/source_text.txt once adapted to keep state.json compact
        if payload.get("spread_contents") and len(payload.get("source_text") or "") > 20000:
            payload["source_text"] = ""
    elif isinstance(state, dict):
        payload = state
    else:
        raise TypeError(f"Unsupported state type: {type(state)}")

    effective_user = (
        user_email
        if user_email is not None
        else (_scoped_user_email or payload.get("user_email"))
    )
    if effective_user:
        payload["user_email"] = effective_user

    uri = write_json(session_id, "state.json", data=payload, user_email=effective_user)
    meta = {
        "session_id": payload.get("session_id", session_id),
        "user_email": payload.get("user_email", effective_user or settings.dev_default_email),
        "config": payload.get("config", {}),
        "current_stage": payload.get("current_stage", "initializing"),
        "progress_pct": payload.get("progress_pct", 0),
        "pdf_gcs_uri": payload.get("pdf_gcs_uri", ""),
        "wide_pdf_gcs_uri": payload.get("wide_pdf_gcs_uri", ""),
        "trace_url": payload.get("trace_url", ""),
        "errors": payload.get("errors", []),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "adapted_from_source": payload.get("adapted_from_source", True),
    }
    save_session_meta(session_id, meta, user_email=effective_user)
    return uri


def load_pipeline_state(
    session_id: str,
    user_email: str | None = None,
):
    """Load PipelineState from state.json, falling back to session.json."""
    from storybook.models import PipelineState

    try:
        raw = json.loads(read_text(session_id, "state.json", user_email=user_email))
        return PipelineState.model_validate(raw)
    except Exception:
        try:
            raw = json.loads(read_text(session_id, "session.json", user_email=user_email))
            return PipelineState.model_validate(raw)
        except Exception:
            return None


def save_progress_events(
    session_id: str,
    events: list[dict],
    done: bool = False,
    user_email: str | None = None,
) -> str:
    """Persist progress event stream checkpoint to events.json."""
    return write_json(
        session_id,
        "events.json",
        data={"events": events, "done": done},
        user_email=user_email,
    )


def load_progress_events(
    session_id: str,
    user_email: str | None = None,
) -> tuple[list[dict], bool]:
    """Load (events, done) from events.json."""
    try:
        raw = json.loads(read_text(session_id, "events.json", user_email=user_email))
        if isinstance(raw, dict):
            return list(raw.get("events", [])), bool(raw.get("done", False))
        if isinstance(raw, list):
            return raw, False
    except Exception:
        pass
    return [], False


def load_all_session_meta(user_email: str | None = None) -> list[dict]:
    """Scan GCS and return metadata for all known sessions (scoped to user if provided)."""
    bucket = _bucket()
    results = []
    seen_ids: set[str] = set()
    effective_user = _scoped_user_email if user_email is None else user_email

    prefixes: list[str] = []
    if effective_user:
        safe_user = sanitize_email_for_gcs(effective_user)
        prefixes.append(f"users/{safe_user}/sessions/")
        if _can_fallback_to_legacy(effective_user):
            prefixes.append("sessions/")
    else:
        prefixes.append("sessions/")

    for prefix in prefixes:
        for blob in bucket.list_blobs(prefix=prefix):
            if blob.name.endswith("/session.json"):
                try:
                    meta = json.loads(blob.download_as_text())
                    sid = meta.get("session_id")
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        results.append(meta)
                except Exception:
                    pass
    return results


def read_blob(
    session_id: str,
    *path_parts: str,
    user_email: str | None = None,
) -> tuple[bytes, str]:
    """Return (bytes, content_type) for a GCS object."""
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    try:
        blob = _bucket().blob(key)
        blob.reload()
        data = blob.download_as_bytes()
        return data, blob.content_type or "application/octet-stream"
    except Exception:
        if _can_fallback_to_legacy(user_email):
            legacy_key = _blob_path(session_id, *path_parts, user_email="")
            blob = _bucket().blob(legacy_key)
            blob.reload()
            data = blob.download_as_bytes()
            return data, blob.content_type or "application/octet-stream"
        raise


def generate_signed_url(
    session_id: str,
    *path_parts: str,
    user_email: str | None = None,
    expiration_seconds: int = 900,
) -> str:
    """Generate a V4 signed URL for reading a session artifact."""
    bucket = _bucket()
    key = _blob_path(session_id, *path_parts, user_email=user_email)
    blob = bucket.blob(key)
    if _can_fallback_to_legacy(user_email) and not blob.exists():
        legacy_key = _blob_path(session_id, *path_parts, user_email="")
        blob = bucket.blob(legacy_key)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=expiration_seconds),
        method="GET",
    )


# ── Resume helpers ─────────────────────────────────────────────────────────────

def image_exists(
    session_id: str,
    page_number: int,
    user_email: str | None = None,
) -> bool:
    key = _blob_path(session_id, "images", f"page_{page_number:02d}.png", user_email=user_email)
    if _bucket().blob(key).exists():
        return True
    if _can_fallback_to_legacy(user_email):
        legacy_key = _blob_path(session_id, "images", f"page_{page_number:02d}.png", user_email="")
        return _bucket().blob(legacy_key).exists()
    return False


def load_image_bytes(
    session_id: str,
    page_number: int,
    user_email: str | None = None,
) -> bytes:
    return read_bytes(session_id, "images", f"page_{page_number:02d}.png", user_email=user_email)


def html_exists(
    session_id: str,
    page_number: int,
    user_email: str | None = None,
) -> bool:
    key = _blob_path(session_id, "pages", f"page_{page_number:02d}.html", user_email=user_email)
    if _bucket().blob(key).exists():
        return True
    if _can_fallback_to_legacy(user_email):
        legacy_key = _blob_path(session_id, "pages", f"page_{page_number:02d}.html", user_email="")
        return _bucket().blob(legacy_key).exists()
    return False


def load_page_html(
    session_id: str,
    page_number: int,
    user_email: str | None = None,
) -> str:
    return read_text(session_id, "pages", f"page_{page_number:02d}.html", user_email=user_email)


def load_adapted_story(
    session_id: str,
    user_email: str | None = None,
) -> dict:
    return json.loads(read_text(session_id, "adapted", "story.json", user_email=user_email))


def load_pages(
    session_id: str,
    user_email: str | None = None,
) -> list:
    """Return list[StoryPage] for all pages stored in GCS (JSON format)."""
    from storybook.models import StoryPage

    bucket = _bucket()
    prefix = _blob_path(session_id, "pages", user_email=user_email) + "/"
    blobs = sorted(bucket.list_blobs(prefix=prefix), key=lambda b: b.name)
    pages = [
        StoryPage(**json.loads(b.download_as_text()))
        for b in blobs
        if b.name.endswith(".json")
    ]
    if not pages and _can_fallback_to_legacy(user_email):
        legacy_prefix = _blob_path(session_id, "pages", user_email="") + "/"
        blobs = sorted(bucket.list_blobs(prefix=legacy_prefix), key=lambda b: b.name)
        pages = [
            StoryPage(**json.loads(b.download_as_text()))
            for b in blobs
            if b.name.endswith(".json")
        ]
    if not pages:
        raise ValueError(f"No page JSON files found in GCS for session {session_id}")
    return pages


def load_character_bible(
    session_id: str,
    user_email: str | None = None,
) -> dict:
    """Load the character bible, upgrading legacy v1 layouts to v2 on the fly."""
    data = json.loads(read_text(session_id, "character_bible.json", user_email=user_email))
    if isinstance(data, dict) and data.get("schema_version", 1) < 2:
        chars = data.get("characters") or {}
        if chars and all(isinstance(v, str) for v in chars.values()):
            data["characters"] = {name: {"appearance": desc} for name, desc in chars.items()}
        data.setdefault("voice_fingerprint", {})
        data["schema_version"] = 2
    return data


# ── Spread helpers ─────────────────────────────────────────────────────────────

def spread_image_exists(
    session_id: str,
    spread_number: int,
    img_index: int = 0,
    user_email: str | None = None,
) -> bool:
    key = _blob_path(
        session_id,
        "images",
        f"spread_{spread_number:02d}_img{img_index}.png",
        user_email=user_email,
    )
    if _bucket().blob(key).exists():
        return True
    if _can_fallback_to_legacy(user_email):
        legacy_key = _blob_path(
            session_id,
            "images",
            f"spread_{spread_number:02d}_img{img_index}.png",
            user_email="",
        )
        return _bucket().blob(legacy_key).exists()
    return False


def load_spread_image_bytes(
    session_id: str,
    spread_number: int,
    img_index: int = 0,
    user_email: str | None = None,
) -> bytes:
    return read_bytes(
        session_id,
        "images",
        f"spread_{spread_number:02d}_img{img_index}.png",
        user_email=user_email,
    )


def spread_html_exists(
    session_id: str,
    spread_number: int,
    user_email: str | None = None,
) -> bool:
    key = _blob_path(
        session_id,
        "spreads",
        f"spread_{spread_number:02d}.html",
        user_email=user_email,
    )
    if _bucket().blob(key).exists():
        return True
    if _can_fallback_to_legacy(user_email):
        legacy_key = _blob_path(
            session_id,
            "spreads",
            f"spread_{spread_number:02d}.html",
            user_email="",
        )
        return _bucket().blob(legacy_key).exists()
    return False


def load_spread_html(
    session_id: str,
    spread_number: int,
    user_email: str | None = None,
) -> str:
    return read_text(
        session_id,
        "spreads",
        f"spread_{spread_number:02d}.html",
        user_email=user_email,
    )


def load_spread_contents(
    session_id: str,
    user_email: str | None = None,
) -> list:
    """Return list[SpreadContent] for all spreads stored as JSON in GCS."""
    from storybook.models import SpreadContent

    bucket = _bucket()
    prefix = _blob_path(session_id, "spreads", user_email=user_email) + "/"
    blobs = sorted(bucket.list_blobs(prefix=prefix), key=lambda b: b.name)
    spreads = [
        SpreadContent(**json.loads(b.download_as_text()))
        for b in blobs
        if b.name.endswith(".json")
    ]
    if not spreads and _can_fallback_to_legacy(user_email):
        legacy_prefix = _blob_path(session_id, "spreads", user_email="") + "/"
        blobs = sorted(bucket.list_blobs(prefix=legacy_prefix), key=lambda b: b.name)
        spreads = [
            SpreadContent(**json.loads(b.download_as_text()))
            for b in blobs
            if b.name.endswith(".json")
        ]
    return spreads
