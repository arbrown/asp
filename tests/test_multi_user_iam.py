"""Tests for Multi-User OAuth, rqlite catalog isolation, and STS CAB downscoped GCS tokens."""

from __future__ import annotations

import base64
import json
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from google.api_core.exceptions import Forbidden, GoogleAPICallError

from storybook.api.auth import (
    AuthenticatedUser,
    get_current_user,
    sanitize_email_for_gcs,
)
from storybook.api.main import app
from storybook.api.routes import get_session, list_sessions_route
from storybook.config import settings
from storybook.db import store
from storybook.db.client import RqliteClient
from storybook.models import PipelineState, SessionConfig, SourceConfig
from storybook.runner import execute_session
from storybook.substrate.client import SubstrateClient
from storybook.tools import gcs, iam


def _make_state(
    session_id: str,
    title: str,
    user_email: str = "alice@example.com",
) -> PipelineState:
    return PipelineState(
        session_id=session_id,
        user_email=user_email,
        config=SessionConfig(source=SourceConfig(title=title, author="Test Author")),
        current_stage="done",
        progress_pct=100,
    )


def _make_jwt(email: str) -> str:
    raw_hdr = json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    raw_pay = json.dumps({"email": email, "sub": email}).encode()
    header = base64.urlsafe_b64encode(raw_hdr).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(raw_pay).decode().rstrip("=")
    return f"{header}.{payload}.signature"


# ── Test 1: STS Boundary Policy Generation ────────────────────────────────────


def test_sts_boundary_policy_generation():
    """Test 1: Verify CEL expression syntax, role permissions, and prefix escaping."""
    bucket = "storybook-artifacts-test1234"
    user_email = "alice@example.com"
    session_id = "test-sess-1"

    policy = iam.build_session_access_boundary(
        user_email=user_email,
        session_id=session_id,
        bucket_name=bucket,
    )

    assert "access_boundary" in policy
    rules = policy["access_boundary"]["access_boundary_rules"]
    assert len(rules) == 1
    rule = rules[0]

    assert rule["available_resource"] == f"//storage.googleapis.com/projects/_/buckets/{bucket}"
    assert rule["available_permissions"] == ["inRole:roles/storage.objectUser"]

    condition = rule["availability_condition"]
    assert condition["title"] == "SessionPrefixConfinement"
    expected_prefix = (
        f"projects/_/buckets/{bucket}/objects/users/alice_at_example.com/sessions/{session_id}/"
    )
    assert condition["expression"] == f"resource.name.startsWith('{expected_prefix}')"


def test_sts_mint_session_downscoped_token_exchange():
    """Verify mint_session_downscoped_token exchanges ambient credentials via Google STS."""
    bucket = "storybook-artifacts-test1234"
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.token = "ambient-sa-token-xyz"

    captured_post: dict = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured_post["url"] = url
        captured_post["data"] = data
        captured_post["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"access_token": "downscoped-sts-token-123", "expires_in": 1800}
        return resp

    with patch("storybook.tools.iam.google.auth.default", return_value=(mock_creds, "test-proj")), \
         patch("storybook.tools.iam.httpx.post", side_effect=fake_post):
        token = iam.mint_session_downscoped_token(
            user_email="alice@example.com",
            session_id="sess-abc",
            bucket_name=bucket,
        )

    assert token == "downscoped-sts-token-123"
    assert captured_post["url"] == "https://sts.googleapis.com/v1/token"
    decoded_options = json.loads(urllib.parse.unquote(captured_post["data"]["options"]))
    rule = decoded_options["access_boundary"]["access_boundary_rules"][0]
    assert rule["available_permissions"] == ["inRole:roles/storage.objectUser"]
    assert (
        "projects/_/buckets/storybook-artifacts-test1234/objects/users/alice_at_example.com/sessions/sess-abc/"
        in rule["availability_condition"]["expression"]
    )


def test_sts_policy_rejects_injection_and_traversal():
    """Verify CEL policy builder rejects path traversal or quote injection."""
    with pytest.raises(ValueError):
        iam.build_session_access_boundary(
            user_email="../bob@example.com",
            session_id="sess-1",
            bucket_name="bucket",
        )
    with pytest.raises(ValueError):
        iam.build_session_access_boundary(
            user_email="alice@example.com",
            session_id="sess-1') || true || ('",
            bucket_name="bucket",
        )


# ── Test 2: Multi-Tenant Catalog Isolation (rqlite) ───────────────────────────


@pytest.fixture
async def isolated_rqlite(monkeypatch):
    """Provide a fresh in-memory SQLite-backed RqliteClient for store tests."""
    client = RqliteClient("sqlite:///:memory:")
    monkeypatch.setattr(store, "_client", client)
    await store.init_db()
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_multi_tenant_catalog_isolation_rqlite(isolated_rqlite):
    """Test 2: Verify list_sessions_route and get_session isolate sessions by user_email."""
    alice_email = "alice@example.com"
    bob_email = "bob@example.com"

    # Setup: Create 2 sessions for alice@ and 2 sessions for bob@
    alice_s1 = _make_state("alice-sess-1", "The Odyssey", user_email=alice_email)
    alice_s2 = _make_state("alice-sess-2", "Alice in Wonderland", user_email=alice_email)
    bob_s1 = _make_state("bob-sess-1", "Treasure Island", user_email=bob_email)
    bob_s2 = _make_state("bob-sess-2", "Don Quixote", user_email=bob_email)

    for st in (alice_s1, alice_s2, bob_s1, bob_s2):
        await store.upsert_session(st, user_email=st.user_email)

    # Execution: Call list_sessions_route with alice@ credentials
    alice_user = AuthenticatedUser(email=alice_email, auth_type="iap")
    alice_results = await list_sessions_route(user=alice_user)
    alice_ids = {r.session_id for r in alice_results}

    # Assert: Only the 2 sessions belonging to alice@ are returned
    assert len(alice_results) == 2
    assert alice_ids == {"alice-sess-1", "alice-sess-2"}

    # Verify Bob only sees Bob's 2 sessions
    bob_user = AuthenticatedUser(email=bob_email, auth_type="iap")
    bob_results = await list_sessions_route(user=bob_user)
    assert {r.session_id for r in bob_results} == {"bob-sess-1", "bob-sess-2"}

    # Assert: Calling get_session on Bob's session ID with Alice's user token returns 403 Forbidden
    with patch("storybook.api.routes.gcs.load_pipeline_state", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await get_session("bob-sess-1", user=alice_user)
        assert exc_info.value.status_code == 403

    # Also verify via FastAPI TestClient with IAP / Bearer headers
    with patch("storybook.api.routes.gcs.load_pipeline_state", return_value=None):
        http_client = TestClient(app)
        resp = http_client.get(
            "/api/v1/sessions/bob-sess-1",
            headers={"Authorization": f"Bearer {_make_jwt(alice_email)}"},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_legacy_sessions_accessible_to_owner(isolated_rqlite, monkeypatch):
    """Verify pre-existing legacy sessions in rqlite and GCS are accessible to the configured legacy owner."""
    monkeypatch.setattr(settings, "legacy_owner_email", "alice@example.com")
    monkeypatch.setattr(store, "LEGACY_OWNER_EMAIL", "alice@example.com")

    # Simulate a pre-migration row with 'legacy@storybook.local'
    legacy_state = _make_state("legacy-book-001", "Peter Pan", user_email="legacy@storybook.local")
    await isolated_rqlite.execute(
        "INSERT INTO sessions (session_id, user_email, status, created_at, updated_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        legacy_state.session_id,
        "legacy@storybook.local",
        "done",
        "2026-08-01T00:00:00Z",
        "2026-08-01T00:00:00Z",
        legacy_state.model_dump_json(),
    )

    alice_user = AuthenticatedUser(email="alice@example.com", auth_type="iap")
    bob_user = AuthenticatedUser(email="bob@example.com", auth_type="iap")

    # Owner can list and get the legacy session
    alice_list = await list_sessions_route(user=alice_user)
    assert any(s.session_id == "legacy-book-001" for s in alice_list)

    with patch("storybook.api.routes.gcs.load_pipeline_state", return_value=None):
        fetched = await get_session("legacy-book-001", user=alice_user)
        assert fetched.session_id == "legacy-book-001"

        # Another user cannot access the legacy session
        with pytest.raises(HTTPException) as exc_info:
            await get_session("legacy-book-001", user=bob_user)
        assert exc_info.value.status_code == 403


# ── Test 3: Downscoped Token GCS Access Enforcement ──────────────────────────


def test_downscoped_token_gcs_access_enforcement():
    """Test 3: Enforce CAB prefix confinement and reject cross-user or path-traversal writes."""
    bucket_name = settings.gcs_artifacts_bucket
    allowed_prefix = "users/alice_at_example.com/sessions/test-sess-1/"

    uploaded_blobs: dict[str, str] = {}

    class BoundaryEnforcingBlob:
        def __init__(self, key: str):
            self.name = key

        def upload_from_string(self, data, content_type: str = "") -> None:
            # Simulate GCS IAM CAB condition evaluation on prefix
            if not self.name.startswith(allowed_prefix):
                raise Forbidden(
                    f"403 POST https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o: "
                    f"Caller does not have storage.objects.create access to "
                    f"'{self.name}' (Credential Access Boundary SessionPrefixConfinement)."
                )
            uploaded_blobs[self.name] = data if isinstance(data, str) else data.decode("utf-8")

    mock_bucket = MagicMock()
    mock_bucket.blob.side_effect = lambda key: BoundaryEnforcingBlob(key)
    mock_storage_client = MagicMock()
    mock_storage_client.bucket.return_value = mock_bucket

    try:
        with patch("storybook.tools.gcs.storage.Client", return_value=mock_storage_client):
            # Setup: Mint and configure a downscoped token for alice@example.com / test-sess-1
            gcs.set_scoped_credentials(
                user_email="alice@example.com",
                token="sts-downscoped-token-alice-sess-1",
            )

            # Action A: Write to own session prefix -> 200 OK
            uri = gcs.write_json("test-sess-1", "state.json", data={"status": "ok"})
            expected_uri = (
                f"gs://{bucket_name}/users/alice_at_example.com/sessions/test-sess-1/state.json"
            )
            assert uri == expected_uri
            assert "users/alice_at_example.com/sessions/test-sess-1/state.json" in uploaded_blobs

            # Action B: Attempt to write to bob's session prefix -> 403 Forbidden
            with pytest.raises(GoogleAPICallError) as exc_info:
                gcs.write_json(
                    "test-sess-2",
                    "state.json",
                    data={"status": "unauthorized"},
                    user_email="bob@example.com",
                )
            assert isinstance(exc_info.value, Forbidden)
            assert "users/bob_at_example.com/sessions/test-sess-2/state.json" not in uploaded_blobs

            # Action C: Attempt a path traversal write:
            # users/alice_at_example.com/sessions/test-sess-1/../../bob_at_example.com/...
            with pytest.raises(ValueError, match="traversal"):
                gcs.write_json(
                    "test-sess-1",
                    "../../bob_at_example.com/sessions/test-sess-2/state.json",
                    data={"status": "traversal"},
                )
            with pytest.raises(ValueError, match="traversal"):
                gcs.write_json(
                    "test-sess-1",
                    "..",
                    "..",
                    "bob_at_example.com",
                    "state.json",
                    data={"status": "traversal"},
                )
    finally:
        gcs.clear_scoped_credentials()


def test_gcs_legacy_fallback_for_owner_without_modifying_existing_files(monkeypatch):
    """Verify legacy sessions/{id}/ files are readable by the configured legacy owner without moving them."""
    monkeypatch.setattr(settings, "legacy_owner_email", "alice@example.com")
    existing_blobs = {
        "sessions/legacy-123/state.json": json.dumps(
            _make_state("legacy-123", "Grimm Fairy Tales").model_dump(mode="json")
        ),
    }

    class FakeBlob:
        def __init__(self, key: str):
            self.name = key

        def download_as_text(self) -> str:
            if self.name not in existing_blobs:
                raise FileNotFoundError(self.name)
            return existing_blobs[self.name]

    mock_bucket = MagicMock()
    mock_bucket.blob.side_effect = lambda k: FakeBlob(k)
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    try:
        gcs.clear_scoped_credentials()
        with patch("storybook.tools.gcs._client", mock_client):
            # Legacy owner transparently reads legacy sessions/{id}/state.json
            state = gcs.load_pipeline_state("legacy-123", user_email="alice@example.com")
            assert state is not None
            assert state.session_id == "legacy-123"

            # bob@example.com cannot fall back to legacy sessions/{id}/state.json
            bob_state = gcs.load_pipeline_state("legacy-123", user_email="bob@example.com")
            assert bob_state is None
    finally:
        gcs.clear_scoped_credentials()


# ── Test 4: Substrate Runner Scoped Client Fallback ──────────────────────────


@pytest.mark.asyncio
async def test_substrate_runner_scoped_client_fallback(monkeypatch):
    """Test 4: Ensure runner starts with GCS_DOWNSCOPED_TOKEN and passes it to storage.Client."""
    sid = "runner-scoped-sess-1"
    test_token = "mock-sts-downscoped-token-for-runner"
    test_user = "alice@example.com"

    monkeypatch.setenv("GCS_DOWNSCOPED_TOKEN", test_token)
    monkeypatch.setenv("ASP_USER_EMAIL", test_user)

    state = _make_state(sid, "The Wind in the Willows", user_email=test_user)
    state.current_stage = "initializing"

    async def fake_run_pipeline(st, sink, resume=False):
        st.current_stage = "done"
        st.progress_pct = 100
        await sink.put({"stage": "done", "pct": 100})
        return st

    try:
        with patch("storybook.tools.gcs.storage.Client") as mock_storage_cls, \
             patch("storybook.runner.init_tracing"), \
             patch("storybook.runner.store.init_db", new_callable=AsyncMock), \
             patch("storybook.runner.gcs.load_pipeline_state", return_value=state), \
             patch("storybook.events.gcs.save_progress_events"), \
             patch("storybook.events.gcs.save_pipeline_state"), \
             patch("storybook.events.store.upsert_session", new_callable=AsyncMock), \
             patch("storybook.runner.run_pipeline", side_effect=fake_run_pipeline):
            exit_code = await execute_session(sid)

            assert exit_code == 0
            assert mock_storage_cls.called
            _, call_kwargs = mock_storage_cls.call_args
            passed_creds = call_kwargs.get("credentials")
            assert passed_creds is not None
            assert passed_creds.token == test_token
            assert gcs._blob_path(sid, "state.json") == (
                f"users/alice_at_example.com/sessions/{sid}/state.json"
            )
    finally:
        gcs.clear_scoped_credentials()


@pytest.mark.asyncio
async def test_substrate_client_passes_downscoped_token_to_local_subprocess(monkeypatch):
    """Verify SubstrateClient.create_actor injects GCS_DOWNSCOPED_TOKEN and ASP_USER_EMAIL."""
    monkeypatch.setattr(settings, "substrate_enabled", False)
    client = SubstrateClient()

    with patch("storybook.substrate.client.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        await client.create_actor(
            name="sess-local-1",
            user_email="alice@example.com",
            downscoped_token="sts-token-local-999",
        )

        assert mock_popen.called
        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env["ASP_USER_EMAIL"] == "alice@example.com"
        assert env["GCS_DOWNSCOPED_TOKEN"] == "sts-token-local-999"


@pytest.mark.asyncio
async def test_auth_dependency_iap_bearer_and_dev_modes():
    """Verify get_current_user parses IAP headers, Bearer JWTs, and Dev fallback."""
    # 1. IAP header
    u_iap = await get_current_user(
        x_goog_authenticated_user_email="accounts.google.com:alice@example.com",
    )
    assert u_iap.email == "alice@example.com"
    assert u_iap.auth_type == "iap"
    assert u_iap.safe_email == "alice_at_example.com"

    # 2. Bearer JWT
    jwt_tok = _make_jwt("bob@example.com")
    u_bearer = await get_current_user(authorization=f"Bearer {jwt_tok}")
    assert u_bearer.email == "bob@example.com"
    assert u_bearer.auth_type == "bearer"

    # 3. Dev mode with X-Dev-User-Email
    u_dev = await get_current_user(x_dev_user_email="charlie@example.com")
    assert u_dev.email == "charlie@example.com"
    assert u_dev.auth_type == "dev"
    assert sanitize_email_for_gcs(u_dev.email) == "charlie_at_example.com"
