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


def signed_url(session_id: str, *path_parts: str, expiry_minutes: int = 60) -> str:
    """Return a short-lived signed URL for direct browser download."""
    import datetime
    key = _blob_path(session_id, *path_parts)
    blob = _bucket().blob(key)
    return blob.generate_signed_url(
        expiration=datetime.timedelta(minutes=expiry_minutes),
        method="GET",
        version="v4",
    )
