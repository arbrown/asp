from __future__ import annotations

import json
from pathlib import PurePosixPath

from google.cloud import storage

from storybook.config import settings

_client: storage.Client | None = None


def _bucket() -> storage.Bucket:
    global _client
    if _client is None:
        _client = storage.Client(project=settings.gcp_project_id)
    return _client.bucket(settings.gcs_artifacts_bucket)


def _blob_path(session_id: str, *parts: str) -> str:
    return str(PurePosixPath("sessions", session_id, *parts))


def write_text(session_id: str, *path_parts: str, content: str) -> str:
    """Write a text file to GCS and return its gs:// URI."""
    key = _blob_path(session_id, *path_parts)
    blob = _bucket().blob(key)
    blob.upload_from_string(content, content_type="text/plain; charset=utf-8")
    return f"gs://{settings.gcs_artifacts_bucket}/{key}"


def write_json(session_id: str, *path_parts: str, data: dict) -> str:
    """Write a JSON file to GCS and return its gs:// URI."""
    key = _blob_path(session_id, *path_parts)
    blob = _bucket().blob(key)
    blob.upload_from_string(json.dumps(data, indent=2), content_type="application/json")
    return f"gs://{settings.gcs_artifacts_bucket}/{key}"


def write_bytes(session_id: str, *path_parts: str, data: bytes, content_type: str) -> str:
    """Write binary data to GCS and return its gs:// URI."""
    key = _blob_path(session_id, *path_parts)
    blob = _bucket().blob(key)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{settings.gcs_artifacts_bucket}/{key}"


def read_text(session_id: str, *path_parts: str) -> str:
    key = _blob_path(session_id, *path_parts)
    return _bucket().blob(key).download_as_text()


def read_bytes(session_id: str, *path_parts: str) -> bytes:
    key = _blob_path(session_id, *path_parts)
    return _bucket().blob(key).download_as_bytes()


def save_session_meta(session_id: str, data: dict) -> None:
    """Persist session metadata so it survives pod restarts."""
    write_json(session_id, "session.json", data=data)


def load_all_session_meta() -> list[dict]:
    """Scan GCS and return metadata for all known sessions."""
    bucket = _bucket()
    results = []
    for blob in bucket.list_blobs(prefix="sessions/"):
        if blob.name.endswith("/session.json"):
            try:
                results.append(json.loads(blob.download_as_text()))
            except Exception:
                pass
    return results


def read_blob(session_id: str, *path_parts: str) -> tuple[bytes, str]:
    """Return (bytes, content_type) for a GCS object."""
    key = _blob_path(session_id, *path_parts)
    blob = _bucket().blob(key)
    blob.reload()
    data = blob.download_as_bytes()
    return data, blob.content_type or "application/octet-stream"


# ── Resume helpers ─────────────────────────────────────────────────────────────

def image_exists(session_id: str, page_number: int) -> bool:
    key = _blob_path(session_id, "images", f"page_{page_number:02d}.png")
    return _bucket().blob(key).exists()


def load_image_bytes(session_id: str, page_number: int) -> bytes:
    return read_bytes(session_id, "images", f"page_{page_number:02d}.png")


def html_exists(session_id: str, page_number: int) -> bool:
    key = _blob_path(session_id, "pages", f"page_{page_number:02d}.html")
    return _bucket().blob(key).exists()


def load_page_html(session_id: str, page_number: int) -> str:
    return read_text(session_id, "pages", f"page_{page_number:02d}.html")


def load_adapted_story(session_id: str) -> dict:
    return json.loads(read_text(session_id, "adapted", "story.json"))


def load_pages(session_id: str) -> list:
    """Return list[StoryPage] for all pages stored in GCS (JSON format)."""
    from storybook.models import StoryPage
    bucket = _bucket()
    prefix = _blob_path(session_id, "pages") + "/"
    blobs = sorted(bucket.list_blobs(prefix=prefix), key=lambda b: b.name)
    pages = [StoryPage(**json.loads(b.download_as_text())) for b in blobs if b.name.endswith(".json")]
    if not pages:
        raise ValueError(f"No page JSON files found in GCS for session {session_id}")
    return pages


def load_character_bible(session_id: str) -> dict:
    return json.loads(read_text(session_id, "character_bible.json"))
