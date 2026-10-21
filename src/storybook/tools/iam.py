"""Google Cloud STS Credential Access Boundary (CAB) downscoped token factory."""

from __future__ import annotations

import json
import os
import urllib.parse

os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

import google.auth
import google.auth.transport.requests
import httpx

from storybook.api.auth import sanitize_email_for_gcs

STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
STS_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
STS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _validate_identifier(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or ".." in cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "'" in cleaned
        or '"' in cleaned
    ):
        raise ValueError(f"Invalid {field_name} for STS CAB policy: {value!r}")
    return cleaned


def build_session_access_boundary(
    user_email: str,
    session_id: str,
    bucket_name: str,
) -> dict:
    """Build the STS Credential Access Boundary JSON payload for a user session.

    Confines `roles/storage.objectUser` on `bucket_name` strictly to objects under:
    `projects/_/buckets/{bucket_name}/objects/users/{safe_user}/sessions/{session_id}/`
    """
    safe_user = sanitize_email_for_gcs(user_email)
    safe_session = _validate_identifier(session_id, "session_id")
    safe_bucket = _validate_identifier(bucket_name, "bucket_name")

    object_prefix = (
        f"projects/_/buckets/{safe_bucket}/objects/users/{safe_user}/sessions/{safe_session}/"
    )
    return {
        "access_boundary": {
            "access_boundary_rules": [
                {
                    "available_resource": (
                        f"//storage.googleapis.com/projects/_/buckets/{safe_bucket}"
                    ),
                    "available_permissions": [
                        "inRole:roles/storage.objectUser",
                    ],
                    "availability_condition": {
                        "title": "SessionPrefixConfinement",
                        "expression": f"resource.name.startsWith('{object_prefix}')",
                    },
                }
            ]
        }
    }


# Alias for convenience
build_access_boundary = build_session_access_boundary


def mint_session_downscoped_token(
    user_email: str,
    session_id: str,
    bucket_name: str,
    lifetime_seconds: int = 1800,
) -> str:
    """Exchange backend default credentials for an STS CAB downscoped token.

    Target prefix:
    projects/_/buckets/{bucket_name}/objects/users/{user_email}/sessions/{session_id}/
    """
    cab_payload = build_session_access_boundary(
        user_email=user_email,
        session_id=session_id,
        bucket_name=bucket_name,
    )

    source_creds, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    if not source_creds.valid or not source_creds.token:
        auth_req = google.auth.transport.requests.Request()
        source_creds.refresh(auth_req)

    subject_token = source_creds.token
    if not subject_token:
        raise RuntimeError("Failed to obtain source access token from ambient credentials")

    form_data = {
        "grant_type": STS_GRANT_TYPE,
        "subject_token_type": STS_TOKEN_TYPE,
        "requested_token_type": STS_TOKEN_TYPE,
        "subject_token": subject_token,
        "options": urllib.parse.quote(json.dumps(cab_payload)),
    }

    resp = httpx.post(
        STS_TOKEN_URL,
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise RuntimeError(f"STS response did not include access_token: {body}")
    return str(access_token)
