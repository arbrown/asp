from __future__ import annotations

import base64
import json
import re

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel

from storybook.config import settings

_SAFE_CHARS_RE = re.compile(r"[^a-z0-9._-]")


def sanitize_email_for_gcs(email: str) -> str:
    """Sanitize an email address for safe use in GCS object prefixes.

    Replaces '@' with '_at_' and strips/replaces delimiter or unsafe characters.
    Raises ValueError if the input is empty or contains path traversal sequences.
    """
    cleaned = email.strip().lower()
    if not cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Invalid email for GCS path: {email!r}")
    cleaned = cleaned.replace("@", "_at_")
    cleaned = _SAFE_CHARS_RE.sub("_", cleaned)
    return cleaned


def _parse_iap_email(raw_header: str) -> str:
    """Parse GKE IAP header 'accounts.google.com:user@domain' -> 'user@domain'."""
    val = raw_header.strip()
    if ":" in val:
        _, val = val.split(":", 1)
    email = val.strip().lower()
    if not email or ".." in email or "/" in email or "\\" in email:
        raise HTTPException(status_code=401, detail="Invalid IAP user email header")
    return email


def _parse_bearer_token(auth_header: str) -> str:
    """Extract user email from a Bearer JWT or token."""
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")

    # 1. Standard 3-segment JWT (header.payload.signature)
    segments = token.split(".")
    if len(segments) == 3:
        try:
            payload_b64 = segments[1]
            padding = "=" * (-len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
            payload = json.loads(payload_bytes.decode("utf-8"))
            email = (payload.get("email") or payload.get("sub") or "").strip().lower()
            if email:
                return email
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Malformed Bearer JWT") from exc

    # 2. Direct email bearer token (used in tests or internal service calls)
    if "@" in token and " " not in token and "/" not in token and ".." not in token:
        return token.lower()

    raise HTTPException(status_code=401, detail="Unable to extract email from Bearer token")


class AuthenticatedUser(BaseModel):
    email: str
    auth_type: str  # "iap", "bearer", or "dev"

    @property
    def safe_email(self) -> str:
        return sanitize_email_for_gcs(self.email)


async def get_current_user(
    request: Request = None,  # type: ignore[assignment]
    x_goog_authenticated_user_email: str | None = Header(None),
    authorization: str | None = Header(None),
    x_dev_user_email: str | None = Header(None),
) -> AuthenticatedUser:
    """Extract and verify user identity.

    1. In production behind GKE IAP:
       Parses header: 'accounts.google.com:user@example.com' -> 'user@example.com'
    2. Bearer token fallback (if direct OAuth JWT is used).
    3. Dev mode fallback:
       If settings.dev_auth_enabled is True, read 'X-Dev-User-Email' or default to
       settings.dev_default_email ('dev@storybook.local').
    """
    # Resolve headers from Request object if Header defaults weren't injected
    if not isinstance(x_goog_authenticated_user_email, str):
        x_goog_authenticated_user_email = (
            request.headers.get("x-goog-authenticated-user-email") if request else None
        )
    if not isinstance(authorization, str):
        authorization = request.headers.get("authorization") if request else None
    if not isinstance(x_dev_user_email, str):
        x_dev_user_email = request.headers.get("x-dev-user-email") if request else None

    # 1. GKE IAP header
    if x_goog_authenticated_user_email and x_goog_authenticated_user_email.strip():
        email = _parse_iap_email(x_goog_authenticated_user_email)
        return AuthenticatedUser(email=email, auth_type="iap")

    # 2. Bearer token fallback
    if authorization and authorization.strip():
        email = _parse_bearer_token(authorization)
        return AuthenticatedUser(email=email, auth_type="bearer")

    # 3. Dev mode fallback
    if settings.dev_auth_enabled:
        dev_email = x_dev_user_email
        if not dev_email and request is not None and hasattr(request, "cookies"):
            dev_email = request.cookies.get("X-Dev-User-Email")
        email = (dev_email or settings.dev_default_email or "dev@local").strip().lower()
        return AuthenticatedUser(email=email, auth_type="dev")

    raise HTTPException(status_code=401, detail="Authentication required")


__all__ = [
    "AuthenticatedUser",
    "Depends",
    "get_current_user",
    "sanitize_email_for_gcs",
]
