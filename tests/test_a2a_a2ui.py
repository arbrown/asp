"""Tests for Gemini Enterprise A2A + A2UI v0.9 integration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from storybook.a2a.a2ui import (
    A2UI_MIME_TYPE,
    GEMINI_ENTERPRISE_CATALOG_ID,
    build_config_surface,
    build_download_chip_update,
    build_lucky_data_model_update,
    build_progress_surface_init,
    build_spread_card_update,
    build_stage_update,
)
from storybook.a2a.sink import A2UIEventSink, get_signed_or_api_url
from storybook.api.main import app
from storybook.events import EventSink
from storybook.models import ACTIVE_AGE_RANGES, PipelineState, SessionConfig, SourceConfig


def _find_a2ui_part(parts: list[dict]) -> dict:
    for part in parts:
        if part.get("mimeType") == A2UI_MIME_TYPE or (
            part.get("metadata", {}).get("mimeType") == A2UI_MIME_TYPE
        ):
            return part
    raise AssertionError(f"No A2UI DataPart found in parts: {parts}")


def test_agent_card_discovery():
    client = TestClient(app)
    resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()

    assert card["name"] == "Storybook Agent"
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is False

    extensions = card["capabilities"].get("extensions", [])
    assert any(
        GEMINI_ENTERPRISE_CATALOG_ID in ext.get("params", {}).get("supportedCatalogIds", [])
        for ext in extensions
    )

    skill_ids = {s["id"] for s in card.get("skills", [])}
    assert {"configure_storybook", "im_feeling_lucky", "generate_storybook"} <= skill_ids


def test_a2ui_surface_builders_unit():
    # 1. Config surface
    envelopes = build_config_surface("storybook_config", defaults={"age_range": "6-7"})
    assert len(envelopes) == 3
    assert envelopes[0]["createSurface"] == {
        "surfaceId": "storybook_config",
        "catalogId": GEMINI_ENTERPRISE_CATALOG_ID,
    }
    comps = {c["id"]: c for c in envelopes[1]["updateComponents"]["components"]}
    assert comps["root"]["component"] == "Card"
    assert comps["header_text"]["text"] == "Storybook Agent — Configure Your Book"
    assert comps["age_select"]["component"] == "Select"
    assert comps["age_select"]["value"] == {"path": "/config/age_range"}
    option_values = [o["value"] for o in comps["age_select"]["options"]]
    assert option_values == ACTIVE_AGE_RANGES

    bound_paths = {c["value"]["path"] for c in comps.values() if c["component"] == "TextField"}
    assert bound_paths == {
        "/config/title",
        "/config/author",
        "/config/style",
        "/config/characters",
        "/config/freeform_notes",
    }

    assert comps["lucky_btn"]["component"] == "Button"
    assert comps["lucky_btn"]["action"]["name"] == "im_feeling_lucky"
    assert comps["generate_btn"]["component"] == "Button"
    assert comps["generate_btn"]["action"]["name"] == "generate_storybook"
    assert set(comps["generate_btn"]["action"]["context"].keys()) == {
        "age_range",
        "title",
        "author",
        "style",
        "characters",
        "freeform_notes",
    }

    # 2. Lucky data model update
    lucky_env = build_lucky_data_model_update(
        "storybook_config",
        {
            "title": "Treasure Island",
            "author": "Robert Louis Stevenson",
            "target_age": "8-9",
            "image_spec": "Woodblock nautical cutaways",
            "text_spec": "Jim Hawkins and Long John Silver in sea-shanty cadence",
            "custom_instructions": "Include a compass rose motif",
        },
    )
    dm = lucky_env[0]["updateDataModel"]
    assert dm["surfaceId"] == "storybook_config"
    assert dm["value"] == {
        "age_range": "8-9",
        "title": "Treasure Island",
        "author": "Robert Louis Stevenson",
        "style": "Woodblock nautical cutaways",
        "characters": "Jim Hawkins and Long John Silver in sea-shanty cadence",
        "freeform_notes": "Include a compass rose motif",
    }

    # 3. Progress Canvas surface init, stage, spread card, and download chip
    init_env = build_progress_surface_init("sess-123", title="Treasure Island")
    assert init_env[0]["createSurface"]["surfaceId"] == "storybook_run_sess-123"
    init_comps = {c["id"]: c for c in init_env[1]["updateComponents"]["components"]}
    assert init_comps["root"]["component"] == "Canvas"
    assert init_comps["root"]["autoOpen"] is True
    assert "stage_status_card" in init_comps
    assert "stage_status_text" in init_comps
    assert "spreads_column" in init_comps
    assert "download_container" in init_comps

    stage_env = build_stage_update("sess-123", "adapting_text", "35%", "Adapted 6 spreads")
    stage_comp = stage_env[0]["updateComponents"]["components"][0]
    assert stage_comp["id"] == "stage_status_text"
    assert "35%" in stage_comp["text"]

    spread_env = build_spread_card_update(
        "sess-123",
        spread_index=0,
        image_url="https://example.com/spread_0.png",
        caption="Cover Spread",
    )
    spread_comps = {c["id"]: c for c in spread_env[0]["updateComponents"]["components"]}
    assert spread_comps["spread_card_0"]["component"] == "Card"
    assert spread_comps["spread_img_0"]["url"] == "https://example.com/spread_0.png"
    assert spread_comps["spreads_column"]["children"] == ["spread_card_0"]

    dl_env = build_download_chip_update(
        "sess-123",
        pdf_url="https://example.com/storybook.pdf",
        book_title="Treasure Island",
    )
    dl_comps = {c["id"]: c for c in dl_env[0]["updateComponents"]["components"]}
    assert dl_comps["download_pdf_chip"]["action"]["openUrl"]["url"] == (
        "https://example.com/storybook.pdf"
    )


def test_signed_url_and_fallback():
    # Verify V4 signed URL when GCS blob.generate_signed_url succeeds
    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-pdf-url"
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    with patch("storybook.tools.gcs._bucket", return_value=mock_bucket):
        url = get_signed_or_api_url("sess-42", "pdf", "storybook.pdf", base_url="http://test")
        assert url == "https://storage.googleapis.com/signed-pdf-url"
        kwargs = mock_blob.generate_signed_url.call_args.kwargs
        assert kwargs["version"] == "v4"
        assert kwargs["response_disposition"] == 'attachment; filename="storybook.pdf"'

    # Verify fallback to API route when local ADC lacks signing credentials
    mock_blob.generate_signed_url.side_effect = RuntimeError("No private key on ADC")
    with patch("storybook.tools.gcs._bucket", return_value=mock_bucket):
        pdf_fallback = get_signed_or_api_url(
            "sess-42", "pdf", "storybook.pdf", base_url="http://testserver"
        )
        assert pdf_fallback == "http://testserver/a2a/sessions/sess-42/pdf"

        img_fallback = get_signed_or_api_url(
            "sess-42", "image", "spread_01_img0.png", base_url="http://testserver"
        )
        assert img_fallback == (
            "http://testserver/a2a/sessions/sess-42/images/spread_01_img0.png"
        )


def test_a2a_message_send_config_and_lucky():
    client = TestClient(app)

    # 1. Initial prompt returns config surface
    resp = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "I want to make a storybook"}],
                }
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "req-1"
    task = body["result"]
    assert task["kind"] == "task"
    a2ui_part = _find_a2ui_part(task["status"]["message"]["parts"])
    assert a2ui_part["data"]["createSurface"]["catalogId"] == GEMINI_ENTERPRISE_CATALOG_ID
    comp_ids = {c["id"] for c in a2ui_part["data"]["updateComponents"]["components"]}
    assert {"age_select", "lucky_btn", "generate_btn", "title_input", "author_input"} <= comp_ids

    # 2. I'm Feeling Lucky action populates /config/* via updateDataModel
    fake_lucky = {
        "title": "The Wind in the Willows",
        "author": "Kenneth Grahame",
        "target_age": "6-7",
        "page_count": 12,
        "image_spec": "Edwardian pen-and-ink with riverbank watercolor washes",
        "text_spec": "Mole, Ratty, and Mr. Toad in cozy pastoral chapters",
        "custom_instructions": "Highlight the picnic basket and rowboat details",
    }
    with patch("storybook.api.routes._generate_lucky", return_value=fake_lucky):
        lucky_resp = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "req-2",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "action": {
                                        "name": "im_feeling_lucky",
                                        "surfaceId": "storybook_config",
                                        "context": {"age_range": "4-5"},
                                    }
                                },
                            }
                        ],
                    }
                },
            },
        )
    assert lucky_resp.status_code == 200
    lucky_task = lucky_resp.json()["result"]
    lucky_part = _find_a2ui_part(lucky_task["status"]["message"]["parts"])
    dm = lucky_part["data"]["updateDataModel"]
    assert dm["path"] == "/config"
    assert dm["value"]["title"] == "The Wind in the Willows"
    assert dm["value"]["author"] == "Kenneth Grahame"
    assert dm["value"]["age_range"] == "4-5"
    assert "Edwardian pen-and-ink" in dm["value"]["style"]
    assert "Mole, Ratty" in dm["value"]["characters"]
    assert "picnic basket" in dm["value"]["freeform_notes"]


@pytest.mark.asyncio
async def test_a2ui_event_sink_and_message_stream():
    inner_events: list[dict | None] = []

    class DummyInnerSink:
        async def put(self, item):
            inner_events.append(item)

    sink = A2UIEventSink(
        session_id="sess-stream-1",
        inner=DummyInnerSink(),
        book_title="Peter Pan",
        base_url="http://testserver",
    )
    assert isinstance(sink, EventSink)

    with patch("storybook.tools.gcs._bucket", side_effect=RuntimeError("offline")):
        await sink.put({"stage": "fetching", "pct": 10, "message": "Fetched Gutenberg text"})
        await sink.put(
            {
                "stage": "generating_image",
                "pct": 55,
                "spread": 0,
                "of": 6,
                "message": "image ready",
            }
        )
        await sink.put({"stage": "done", "pct": 100})
        await sink.put(None)

    assert len(inner_events) == 4
    collected = []
    while not sink.queue.empty():
        ev = await sink.queue.get()
        if ev is not None:
            collected.append(ev)

    # Expect: 1) init Canvas, 2) fetching stage, 3) spread 0 image card, 4) final download chip
    assert len(collected) == 4
    init_part = _find_a2ui_part(collected[0]["status"]["message"]["parts"])
    assert init_part["data"]["createSurface"]["surfaceId"] == "storybook_run_sess-stream-1"

    spread_part = _find_a2ui_part(collected[2]["status"]["message"]["parts"])
    spread_comps = {c["id"]: c for c in spread_part["data"]["updateComponents"]["components"]}
    assert "spread_card_0" in spread_comps
    assert spread_comps["spread_img_0"]["url"].endswith(
        "/a2a/sessions/sess-stream-1/images/spread_00_img0.png"
    )

    final_ev = collected[3]
    assert final_ev["final"] is True
    assert final_ev["status"]["state"] == "completed"
    final_part = _find_a2ui_part(final_ev["status"]["message"]["parts"])
    final_comps = {c["id"]: c for c in final_part["data"]["updateComponents"]["components"]}
    assert final_comps["download_pdf_chip"]["action"]["openUrl"]["url"].endswith(
        "/a2a/sessions/sess-stream-1/pdf"
    )

    # Verify POST /a2a message/stream SSE endpoint end-to-end
    simulated_gcs_events = [
        {"stage": "adapting_text", "pct": 30, "message": "Adapting chapters"},
        {"stage": "generating_image", "pct": 60, "spread": 0, "message": "image ready"},
        {"stage": "generating_image", "pct": 80, "spread": 1, "message": "image ready"},
        {"stage": "done", "pct": 100},
    ]

    with (
        patch("storybook.api.routes._persist_session", new=AsyncMock()),
        patch("storybook.substrate.ate.create_actor", new=AsyncMock(return_value={"name": "s1"})),
        patch("storybook.tools.gcs.save_progress_events"),
        patch(
            "storybook.tools.gcs.load_progress_events",
            return_value=(simulated_gcs_events, True),
        ),
        patch("storybook.tools.gcs._bucket", side_effect=RuntimeError("offline")),
    ):
        client = TestClient(app)
        with client.stream(
            "POST",
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "stream-1",
                "method": "message/stream",
                "params": {
                    "sessionId": "sess-e2e-99",
                    "pollInterval": 0.01,
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "action": {
                                        "name": "generate_storybook",
                                        "context": {
                                            "age_range": "6-7",
                                            "title": "Peter Pan",
                                            "author": "J. M. Barrie",
                                            "style": "Golden Age watercolor",
                                            "characters": "Peter, Wendy, Captain Hook",
                                            "freeform_notes": "Neverland map endpapers",
                                        },
                                    }
                                },
                            }
                        ],
                    },
                },
            },
        ) as response:
            assert response.status_code == 200
            frames = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    assert len(frames) >= 4
    first_result = frames[0]["result"]
    first_a2ui = _find_a2ui_part(first_result["status"]["message"]["parts"])
    assert first_a2ui["data"]["createSurface"]["surfaceId"] == "storybook_run_sess-e2e-99"

    # Check that both spread_card_0 and spread_card_1 were streamed
    all_comp_ids = set()
    for frame in frames:
        part = _find_a2ui_part(frame["result"]["status"]["message"]["parts"])
        for comp in part["data"].get("updateComponents", {}).get("components", []):
            all_comp_ids.add(comp["id"])

    assert {"spread_card_0", "spread_card_1", "download_pdf_chip"} <= all_comp_ids
    last_result = frames[-1]["result"]
    assert last_result["final"] is True
    assert last_result["status"]["state"] == "completed"

    # Also test tasks/get for a completed session
    completed_state = PipelineState(
        session_id="sess-e2e-99",
        config=SessionConfig(source=SourceConfig(title="Peter Pan", author="J. M. Barrie")),
        current_stage="done",
        progress_pct=100,
        pdf_gcs_uri="gs://bucket/sessions/sess-e2e-99/final/storybook.pdf",
    )
    with (
        patch(
            "storybook.api.routes._load_session_state",
            new=AsyncMock(return_value=completed_state),
        ),
        patch("storybook.tools.gcs._bucket", side_effect=RuntimeError("offline")),
    ):
        get_resp = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "get-1",
                "method": "tasks/get",
                "params": {"id": "sess-e2e-99"},
            },
        )
        assert get_resp.status_code == 200
        get_task = get_resp.json()["result"]
        assert get_task["status"]["state"] == "completed"
        get_part = _find_a2ui_part(get_task["status"]["message"]["parts"])
        get_comps = {c["id"]: c for c in get_part["data"]["updateComponents"]["components"]}
        assert "download_pdf_chip" in get_comps


def test_a2a_session_asset_proxy_routes():
    client = TestClient(app)

    # Also verify /a2a/.well-known/agent.json and https host forwarding
    card_resp = client.get(
        "/a2a/.well-known/agent.json",
        headers={"Host": "pushkin-agent.duckdns.org"},
    )
    assert card_resp.status_code == 200
    assert card_resp.json()["url"] == "https://pushkin-agent.duckdns.org/a2a"

    # Valid spread image and reference image
    with patch("storybook.tools.gcs.load_image", return_value=b"\x89PNG-fake-image"):
        img_resp = client.get("/a2a/sessions/sess-12345678/images/spread_00_img0.png")
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"] == "image/png"
        assert img_resp.headers["cache-control"] == "public, max-age=86400"
        assert img_resp.content == b"\x89PNG-fake-image"

        ref_resp = client.get("/a2a/sessions/sess-12345678/images/reference.png")
        assert ref_resp.status_code == 200

        # Invalid filename rejected with 404
        bad_resp = client.get("/a2a/sessions/sess-12345678/images/other.png")
        assert bad_resp.status_code == 404

    # Missing image returns 404
    with patch("storybook.tools.gcs.load_image", return_value=None):
        missing_img = client.get("/a2a/sessions/sess-12345678/images/spread_01_img0.png")
        assert missing_img.status_code == 404

    # Valid PDF download
    with patch("storybook.tools.gcs.load_pdf", return_value=b"%PDF-1.4-fake"):
        pdf_resp = client.get("/a2a/sessions/12345678-abcd/pdf")
        assert pdf_resp.status_code == 200
        assert pdf_resp.headers["content-type"] == "application/pdf"
        assert (
            pdf_resp.headers["content-disposition"]
            == 'attachment; filename="storybook-12345678.pdf"'
        )
        assert pdf_resp.content == b"%PDF-1.4-fake"

    # Missing PDF returns 404
    with patch("storybook.tools.gcs.load_pdf", return_value=None):
        missing_pdf = client.get("/a2a/sessions/12345678-abcd/pdf")
        assert missing_pdf.status_code == 404

