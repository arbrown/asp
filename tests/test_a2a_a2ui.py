"""Tests for Gemini Enterprise (WASD) A2A + A2UI Creative Studio & Pipeline Streaming."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from storybook.a2a.a2ui import (
    A2UI_MIME_TYPE,
    GEMINI_ENTERPRISE_CATALOG_ID,
    build_download_chip_update,
    build_spread_card_update,
    build_studio_surface,
    wrap_a2ui_parts,
)
from storybook.a2a.router import _studio_contexts
from storybook.a2a.sink import A2UIEventSink, get_signed_or_api_url
from storybook.api.main import app


def _extract_a2ui_parts(parts: list[dict]) -> list[dict]:
    out = []
    for part in parts:
        if (
            part.get("kind") == "data"
            and part.get("metadata", {}).get("mimeType") == A2UI_MIME_TYPE
        ):
            out.append(part)
    assert out, f"No WASD A2UI parts with metadata.mimeType={A2UI_MIME_TYPE} found in {parts}"
    return out


def _parse_sse_frames(raw_text: str) -> list[dict]:
    frames = []
    for line in raw_text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: ") :]))
    return frames


def test_agent_card_discovery():
    client = TestClient(app)
    for path in ("/.well-known/agent.json", "/a2a/.well-known/agent.json"):
        resp = client.get(path)
        assert resp.status_code == 200
        card = resp.json()
        assert card["capabilities"]["streaming"] is True
        exts = card["capabilities"]["extensions"]
        assert any("a2ui" in e["uri"] for e in exts)
        assert GEMINI_ENTERPRISE_CATALOG_ID in exts[0]["params"]["supportedCatalogIds"]


def test_wasd_a2ui_surface_builders_unit():
    envelopes = build_studio_surface(
        surface_id="studio_1",
        studio_state={
            "title": "Demons",
            "author": "Fyodor Dostoevsky",
            "recommended_age": "8-10",
            "recommended_pages": "16",
        },
    )
    assert len(envelopes) == 3
    assert "surfaceUpdate" in envelopes[0]
    assert "dataModelUpdate" in envelopes[1]
    assert envelopes[2] == {"beginRendering": {"surfaceId": "studio_1", "root": "root"}}

    comps = {c["id"]: c["component"] for c in envelopes[0]["surfaceUpdate"]["components"]}
    assert comps["root"] == {"Card": {"child": "main-col"}}
    assert "explicitList" in comps["main-col"]["Column"]["children"]
    assert "MultipleChoice" in comps["art-picker"]
    assert comps["art-picker"]["MultipleChoice"]["selections"] == {
        "path": "/studio/artStyleSelections"
    }
    assert comps["art-picker"]["MultipleChoice"]["filterable"] is True
    assert comps["art-picker"]["MultipleChoice"]["maxAllowedSelections"] == 1
    assert len(comps["art-picker"]["MultipleChoice"]["options"]) == 4
    assert comps["art-custom-input"]["TextField"]["text"] == {"path": "/studio/customArtStyle"}
    assert comps["art-custom-input"]["TextField"]["textFieldType"] == "shortText"

    assert "MultipleChoice" in comps["text-picker"]
    assert comps["text-picker"]["MultipleChoice"]["filterable"] is True
    assert comps["text-picker"]["MultipleChoice"]["maxAllowedSelections"] == 1
    assert len(comps["text-picker"]["MultipleChoice"]["options"]) == 4
    assert comps["text-custom-input"]["TextField"]["text"] == {"path": "/studio/customTextStyle"}
    assert comps["text-custom-input"]["TextField"]["textFieldType"] == "shortText"

    assert comps["age-picker"]["MultipleChoice"]["variant"] == "chips"
    assert comps["age-picker"]["MultipleChoice"]["maxAllowedSelections"] == 1
    assert comps["pages-picker"]["MultipleChoice"]["variant"] == "chips"
    assert comps["pages-picker"]["MultipleChoice"]["maxAllowedSelections"] == 1

    assert comps["title-input"]["TextField"]["textFieldType"] == "shortText"
    assert comps["author-input"]["TextField"]["textFieldType"] == "shortText"
    assert comps["custom-motif-input"]["TextField"]["textFieldType"] == "shortText"
    assert comps["generate-btn"]["Button"]["action"]["name"] == "generate_storybook"

    dm = envelopes[1]["dataModelUpdate"]
    assert dm["path"] == "/studio"
    dm_keys = {item["key"]: item for item in dm["contents"]}
    assert dm_keys["title"]["valueString"] == "Demons"
    assert dm_keys["author"]["valueString"] == "Fyodor Dostoevsky"
    assert dm_keys["customArtStyle"]["valueString"] == ""
    assert dm_keys["customTextStyle"]["valueString"] == ""
    assert json.loads(dm_keys["artStyleSelections"]["valueString"]) == [
        comps["art-picker"]["MultipleChoice"]["options"][0]["value"]
    ]
    assert json.loads(dm_keys["textStyleSelections"]["valueString"]) == [
        comps["text-picker"]["MultipleChoice"]["options"][0]["value"]
    ]
    assert dm_keys["ageSelections"]["valueString"] == '["8-10"]'
    assert dm_keys["pageCountSelections"]["valueString"] == '["16"]'

    parts = wrap_a2ui_parts(envelopes)
    assert len(parts) == 3
    for p in parts:
        assert p["kind"] == "data"
        assert p["metadata"]["mimeType"] == A2UI_MIME_TYPE

    # Check spread card & download surface builders
    spread_envs = build_spread_card_update(
        surface_id="run_1",
        spread_index=1,
        image_url="https://pushkin-agent.duckdns.org/a2a/sessions/s1/images/spread_1.png",
        caption="Stepan Trofimovich踱s through the provincial garden.",
        book_title="Demons",
        book_author="Fyodor Dostoevsky",
    )
    assert "beginRendering" in spread_envs[0]
    spread_comps = {
        c["id"]: c["component"] for c in spread_envs[1]["surfaceUpdate"]["components"]
    }
    assert spread_comps["spread-img-1"]["Image"]["url"]["literalString"].endswith(
        "/a2a/sessions/s1/images/spread_1.png"
    )
    assert spread_comps["spread-img-1"]["Image"]["usageHint"] == "largeFeature"

    dl_envs = build_download_chip_update(
        surface_id="run_1",
        pdf_url="https://pushkin-agent.duckdns.org/a2a/sessions/s1/pdf",
        title="Demons",
        author="Fyodor Dostoevsky",
    )
    dl_comps = {c["id"]: c["component"] for c in dl_envs[1]["surfaceUpdate"]["components"]}
    assert "download-btn" in dl_comps


def test_signed_url_and_fallback():
    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-v4-url"
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    with patch("storybook.a2a.sink.gcs._bucket", return_value=mock_bucket):
        url = get_signed_or_api_url("sess123", "pdf", "storybook.pdf", base_url="https://example.com")  # noqa: E501
        assert url == "https://storage.googleapis.com/signed-v4-url"

    with patch("storybook.a2a.sink.gcs._bucket", side_effect=RuntimeError("no credentials")):
        fallback = get_signed_or_api_url(
            "sess123", "image", "spread_01_img0.png", base_url="https://example.com"
        )
        assert fallback == "https://example.com/a2a/sessions/sess123/images/spread_01_img0.png"


def test_message_stream_brainstorming_and_buttons_without_starting_actor():
    """Verify message/stream for natural language brainstorming and studio buttons returns A2UI Card and final: True without starting a Substrate actor."""  # noqa: E501
    client = TestClient(app)
    _studio_contexts.clear()

    with (
        patch(
            "storybook.a2a.router._create_and_start_storybook_session",
            new_callable=AsyncMock,
        ) as mock_start_actor,
        patch(
            "google.genai.Client",
            side_effect=RuntimeError("offline unit test"),
        ),
    ):
        # 1. Natural language brainstorming turn on message/stream (exactly how WASD calls A2A)
        stream_resp = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "turn-1",
                "method": "message/stream",
                "params": {
                    "contextId": "ctx-dostoevsky-1",
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "text",
                                "text": "Help me come up with some good storybook art styles and clever text styles for adapting Demons by Dostevsky",  # noqa: E501
                            }
                        ],
                    },
                },
            },
        )
        assert stream_resp.status_code == 200
        # Must NOT have launched a Substrate actor session!
        mock_start_actor.assert_not_called()

        frames = _parse_sse_frames(stream_resp.text)
        assert len(frames) == 3
        # Frame 0: submitted (final: False)
        assert frames[0]["result"]["kind"] == "status-update"
        assert frames[0]["result"]["status"]["state"] == "submitted"
        assert frames[0]["result"]["final"] is False
        # Frame 1: working with parts [text, surfaceUpdate, dataModelUpdate, beginRendering]
        assert frames[1]["result"]["status"]["state"] == "working"
        assert frames[1]["result"]["final"] is False
        working_parts = frames[1]["result"]["status"]["message"]["parts"]
        a2ui_parts = _extract_a2ui_parts(working_parts)
        assert len(a2ui_parts) == 3
        assert "surfaceUpdate" in a2ui_parts[0]["data"]
        assert "dataModelUpdate" in a2ui_parts[1]["data"]
        assert "beginRendering" in a2ui_parts[2]["data"]
        turn1_surface_id = a2ui_parts[2]["data"]["beginRendering"]["surfaceId"]
        assert turn1_surface_id.startswith("storybook-studio-")
        dm_contents = {
            item["key"]: item for item in a2ui_parts[1]["data"]["dataModelUpdate"]["contents"]
        }
        assert dm_contents["title"]["valueString"] == "Demons"
        assert "Dostoevsky" in dm_contents["author"]["valueString"]
        # Frame 2: completed (final: True) with NO duplicate message or artifact-update
        assert not any(f["result"].get("kind") == "artifact-update" for f in frames)
        assert frames[2]["result"]["kind"] == "status-update"
        assert frames[2]["result"]["status"]["state"] == "completed"
        assert frames[2]["result"]["final"] is True
        assert "message" not in frames[2]["result"]["status"]

        # 2. Follow-up refinement in the same contextId (adjusting age band to 10-12)
        refine_resp = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "turn-2",
                "method": "message/stream",
                "params": {
                    "contextId": "ctx-dostoevsky-1",
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "text",
                                "text": "Give me darker woodcut options and make it for ages 10-12 and 16 pages",  # noqa: E501
                            }
                        ],
                    },
                },
            },
        )
        assert refine_resp.status_code == 200
        mock_start_actor.assert_not_called()
        refine_frames = _parse_sse_frames(refine_resp.text)
        assert len(refine_frames) == 3
        assert refine_frames[-1]["result"]["final"] is True
        assert "message" not in refine_frames[-1]["result"]["status"]
        refine_a2ui = _extract_a2ui_parts(
            refine_frames[1]["result"]["status"]["message"]["parts"]
        )
        turn2_surface_id = refine_a2ui[2]["data"]["beginRendering"]["surfaceId"]
        assert turn2_surface_id != turn1_surface_id
        refine_dm = {
            item["key"]: item
            for item in refine_a2ui[1]["data"]["dataModelUpdate"]["contents"]
        }
        assert refine_dm["title"]["valueString"] == "Demons"
        assert refine_dm["ageSelections"]["valueString"] == '["10-12"]'
        assert refine_dm["pageCountSelections"]["valueString"] == '["16"]'

        # 3. Clicking 'shuffle_styles' button on message/stream
        shuffle_resp = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "turn-3",
                "method": "message/stream",
                "params": {
                    "contextId": "ctx-dostoevsky-1",
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "action": {
                                        "name": "shuffle_styles",
                                        "context": [
                                            {"key": "title", "value": "Demons"},
                                            {"key": "author", "value": "Fyodor Dostoevsky"},
                                        ],
                                    }
                                },
                            }
                        ],
                    },
                },
            },
        )
        assert shuffle_resp.status_code == 200
        mock_start_actor.assert_not_called()
        shuffle_frames = _parse_sse_frames(shuffle_resp.text)
        assert shuffle_frames[-1]["result"]["final"] is True

        # 4. Clicking 'im_feeling_lucky' button on message/stream
        mock_lucky = {
            "title": "The Wind in the Willows",
            "author": "Kenneth Grahame",
            "target_age": "6-8",
            "page_count": 12,
            "image_spec": "Edwardian riverbank watercolor and ink.",
            "text_spec": "Cozy fireside animal adventure narration.",
            "custom_instructions": "Include a wicker picnic basket on every spread.",
        }
        with patch("storybook.a2a.router.api_routes._generate_lucky", return_value=mock_lucky):
            lucky_resp = client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": "turn-4",
                    "method": "message/stream",
                    "params": {
                        "contextId": "ctx-dostoevsky-1",
                        "message": {
                            "role": "user",
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {
                                        "userAction": {
                                            "name": "im_feeling_lucky",
                                            "context": [
                                                {"key": "age_range", "value": {"0": "6-8"}},
                                            ],
                                        }
                                    },
                                }
                            ],
                        },
                    },
                },
            )
        assert lucky_resp.status_code == 200
        mock_start_actor.assert_not_called()
        lucky_frames = _parse_sse_frames(lucky_resp.text)
        assert len(lucky_frames) == 3
        assert lucky_frames[-1]["result"]["final"] is True
        assert "message" not in lucky_frames[-1]["result"]["status"]
        lucky_a2ui = _extract_a2ui_parts(
            lucky_frames[1]["result"]["status"]["message"]["parts"]
        )
        lucky_dm = {
            item["key"]: item
            for item in lucky_a2ui[1]["data"]["dataModelUpdate"]["contents"]
        }
        assert lucky_dm["title"]["valueString"] == "The Wind in the Willows"


@pytest.mark.asyncio
async def test_generate_storybook_streams_spreads_and_pdf_download():
    """Verify clicking generate_storybook starts the session, streams spread Image cards, and emits the final PDF link."""  # noqa: E501
    sink = A2UIEventSink(
        session_id="sess_demons",
        base_url="https://pushkin-agent.duckdns.org",
        title="Demons",
        author="Fyodor Dostoevsky",
    )

    await sink.put({"stage": "fetching", "status": "in_progress", "detail": "Loading text", "pct": 15})  # noqa: E501
    await sink.put(
        {
            "stage": "generating_image",
            "status": "completed",
            "spread_index": 1,
            "filename": "spread_1.png",
            "caption": "Spread 1: Stepan Trofimovich and Varvara Petrovna debate in the salon.",
        }
    )
    await sink.put(
        {
            "stage": "generating_image",
            "status": "completed",
            "spread_index": 2,
            "filename": "spread_2.png",
            "caption": "Spread 2: Lanterns flicker along the rainy provincial street.",
        }
    )
    await sink.put({"stage": "done", "pct": 100})

    from storybook.a2a.router import SESSION_A2UI_QUEUES

    SESSION_A2UI_QUEUES["sess_demons"] = sink.queue
    try:
        client = TestClient(app)
        with (
            patch("storybook.a2a.router.ate.create_actor", new_callable=AsyncMock),
            patch("storybook.a2a.router.gcs.save_pipeline_state"),
            patch("storybook.a2a.router.gcs.save_progress_events"),
        ):
            resp = client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": "gen-stream-1",
                    "method": "message/stream",
                    "params": {
                        "sessionId": "sess_demons",
                        "contextId": "ctx-demons",
                        "message": {
                            "role": "user",
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {
                                        "action": {
                                            "name": "generate_storybook",
                                            "context": [
                                                {"key": "title", "value": "Demons"},
                                                {"key": "author", "value": "Fyodor Dostoevsky"},
                                                {
                                                    "key": "art_style",
                                                    "value": {
                                                        "0": "19th-century Russian woodblock print."
                                                    },
                                                },
                                                {
                                                    "key": "text_style",
                                                    "value": {
                                                        "0": "Lemony Snicket deadpan satire."
                                                    },
                                                },
                                                {"key": "age_range", "value": {"0": "8-10"}},
                                                {"key": "page_count", "value": {"0": "12"}},
                                                {
                                                    "key": "custom_instructions",
                                                    "value": "Brass samovar motif",
                                                },
                                            ],
                                        }
                                    },
                                }
                            ],
                        },
                    },
                },
            )
            assert resp.status_code == 200
            frames = _parse_sse_frames(resp.text)
            assert frames[0]["result"]["status"]["state"] == "submitted"
            assert frames[-1]["result"]["status"]["state"] == "completed"
            assert frames[-1]["result"]["final"] is True

            # Verify spread Image URLs were streamed
            all_raw = resp.text
            assert (
                "https://pushkin-agent.duckdns.org/a2a/sessions/sess_demons/images/spread_1.png"
                in all_raw
            )
            assert (
                "https://pushkin-agent.duckdns.org/a2a/sessions/sess_demons/images/spread_2.png"
                in all_raw
            )
            # Verify final markdown link + PDF URL in the working frame right before completed
            assert "message" not in frames[-1]["result"]["status"]
            assert not any(f["result"].get("kind") == "artifact-update" for f in frames)
            final_parts = frames[-2]["result"]["status"]["message"]["parts"]
            assert (
                "[📥 Download Completed Storybook PDF](https://pushkin-agent.duckdns.org/a2a/sessions/sess_demons/pdf)"  # noqa: E501
                in final_parts[0]["text"]
            )
            assert "Live Activity Breadcrumbs" not in all_raw
    finally:
        SESSION_A2UI_QUEUES.pop("sess_demons", None)


def test_a2a_session_asset_proxy_routes():
    client = TestClient(app)
    with patch("storybook.a2a.router.gcs.load_image", side_effect=[None, b"\x89PNG-fake"]):
        r_img = client.get("/a2a/sessions/s1/images/spread_1.png")
        assert r_img.status_code == 200
        assert r_img.content == b"\x89PNG-fake"
        assert r_img.headers["content-type"] == "image/png"

    with patch("storybook.a2a.router.gcs.load_pdf", return_value=b"%PDF-1.7-fake"):
        r_pdf = client.get("/a2a/sessions/s1/pdf")
        assert r_pdf.status_code == 200
        assert r_pdf.content == b"%PDF-1.7-fake"
        assert r_pdf.headers["content-type"] == "application/pdf"


def test_a2a_session_image_spread_00_img0_and_regex():
    """Verify spread_00_img0.png and reference.png are served while invalid filenames return 400."""
    client = TestClient(app)
    with patch("storybook.a2a.router.gcs.load_image", return_value=b"\x89PNG-spread00"):
        r0 = client.get("/a2a/sessions/s1/images/spread_00_img0.png")
        assert r0.status_code == 200
        assert r0.content == b"\x89PNG-spread00"

    with patch("storybook.a2a.router.gcs.load_image", return_value=b"\x89PNG-ref"):
        r_ref = client.get("/a2a/sessions/s1/images/reference.png")
        assert r_ref.status_code == 200
        assert r_ref.content == b"\x89PNG-ref"

    r_bad = client.get("/a2a/sessions/s1/images/evil.exe")
    assert r_bad.status_code == 400


@pytest.mark.asyncio
async def test_stream_session_a2ui_events_realtime_breadcrumbs_and_spread_00_img0():
    """Verify stream_session_a2ui_events emits real-time breadcrumbs from events.json, streams spread_00_img0.png with story text, and calls ate.stop_actor on completion."""  # noqa: E501
    from storybook.a2a.sink import stream_session_a2ui_events
    from storybook.models import PipelineState, SessionConfig, SourceConfig, SpreadContent

    fake_state = PipelineState(
        session_id="sess_breadcrumbs",
        config=SessionConfig(source=SourceConfig(title="Demons", author="Fyodor Dostoevsky")),
        current_stage="generating_image",
        progress_pct=62,
        spread_contents=[
            SpreadContent(
                spread_number=0,
                recto_text="In a quiet provincial town, Stepan Trofimovich adjusted his cravat.",
            ),
            SpreadContent(
                spread_number=1,
                verso_text="Rain drummed on the cobblestones outside Varvara Petrovna's salon.",
                recto_text="Nikolai Stavrogin arrived on the evening train.",
            ),
        ],
    )

    progress_batches = [
        (
            [
                {"stage": "initializing", "pct": 5, "message": "Initializing session", "seq": 0},
                {
                    "stage": "fetching",
                    "pct": 12,
                    "message": 'Fetching literature: Found "Demons" on Project Gutenberg',
                    "seq": 1,
                },
                {
                    "stage": "adapting_text",
                    "pct": 25,
                    "message": "Adapting text into 24 pages for ages 8-10",
                    "seq": 2,
                },
            ],
            False,
        ),
        (
            [
                {"stage": "initializing", "pct": 5, "message": "Initializing session", "seq": 0},
                {
                    "stage": "fetching",
                    "pct": 12,
                    "message": 'Fetching literature: Found "Demons" on Project Gutenberg',
                    "seq": 1,
                },
                {
                    "stage": "adapting_text",
                    "pct": 25,
                    "message": "Adapting text into 24 pages for ages 8-10",
                    "seq": 2,
                },
                {
                    "stage": "building_character_bible",
                    "pct": 40,
                    "message": "Creating Character Bible & reference sheet",
                    "seq": 3,
                },
                {
                    "stage": "generating_image",
                    "pct": 62,
                    "spread": 1,
                    "of": 6,
                    "message": "Generating Spread 2 illustration (Russian folk art)...",
                    "seq": 4,
                },
                {"stage": "done", "pct": 100, "message": "Storybook complete", "seq": 5},
            ],
            True,
        ),
    ]
    image_batches = [
        ["reference.png", "spread_00_img0.png"],
        ["reference.png", "spread_00_img0.png", "spread_01_img0.png"],
    ]

    with (
        patch("storybook.a2a.sink.gcs.load_progress_events", side_effect=progress_batches),
        patch("storybook.a2a.sink.gcs.list_session_images", side_effect=image_batches),
        patch("storybook.a2a.sink.gcs.load_pipeline_state", return_value=fake_state),
        patch("storybook.a2a.sink.ate.stop_actor", new_callable=AsyncMock) as mock_stop_actor,
    ):
        raw_frames: list[str] = []
        async for sse in stream_session_a2ui_events(
            session_id="sess_breadcrumbs",
            rpc_id="rpc-bc-1",
            base_url="https://pushkin-agent.duckdns.org",
            title="Demons",
            author="Fyodor Dostoevsky",
            poll_interval=0.01,
            heartbeat_interval=0.05,
        ):
            raw_frames.append(sse)

        mock_stop_actor.assert_awaited_with(
            name="sess_breadcrumbs",
            atespace="asp",
        )

    combined = "".join(raw_frames)
    assert "spread_00_img0.png" in combined
    assert "spread_01_img0.png" in combined
    assert "reference.png" in combined
    assert "Stepan Trofimovich adjusted his cravat" in combined
    assert "Nikolai Stavrogin arrived on the evening train" in combined
    assert "Project Gutenberg" in combined
    assert "Creating Character Bible & reference sheet" in combined
    assert "check_status" in combined

    assert "Live Activity Breadcrumbs:" not in combined
    parsed = _parse_sse_frames(combined)
    assert parsed[0]["result"]["status"]["state"] == "submitted"
    assert parsed[-1]["result"]["status"]["state"] == "completed"
    assert parsed[-1]["result"]["final"] is True
    assert "message" not in parsed[-1]["result"]["status"]
    for f in parsed:
        msg = f["result"].get("status", {}).get("message")
        if msg and f["result"]["status"].get("state") == "working":
            txt = msg["parts"][0].get("text", "")
            if "is complete!" not in txt:
                assert "\n" not in txt


def test_user_edited_fields_override_prior_studio_state_on_generate():
    """Verify user-edited title, author, custom_instructions, custom_art_style, and custom_text_style override prior studio state."""  # noqa: E501
    client = TestClient(app)
    _studio_contexts.clear()
    _studio_contexts["ctx-override-1"] = {
        "title": "Demons",
        "author": "Fyodor Dostoevsky",
        "selected_art_style": "Original preset art style",
        "selected_text_style": "Original preset text style",
        "recommended_age": "6-8",
        "recommended_pages": "12",
        "suggested_motif": "Original motif",
    }

    with (
        patch("storybook.a2a.router.ate.create_actor", new_callable=AsyncMock),
        patch("storybook.a2a.router.gcs.save_pipeline_state"),
        patch("storybook.a2a.router.gcs.save_progress_events"),
    ):
        resp = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "gen-override-1",
                "method": "message/send",
                "params": {
                    "sessionId": "sess_override_1",
                    "contextId": "ctx-override-1",
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "action": {
                                        "name": "generate_storybook",
                                        "context": [
                                            {"key": "title", "value": "Crime and Punishment"},
                                            {"key": "author", "value": "F. M. Dostoevsky"},
                                            {
                                                "key": "art_style",
                                                "value": '["Original preset art style"]',
                                            },
                                            {
                                                "key": "custom_art_style",
                                                "value": (
                                                    "Custom St. Petersburg charcoal & gold leaf"
                                                ),
                                            },
                                            {
                                                "key": "text_style",
                                                "value": '["Original preset text style"]',
                                            },
                                            {
                                                "key": "custom_text_style",
                                                "value": "Custom detective fable narration",
                                            },
                                            {"key": "age_range", "value": '["10-12"]'},
                                            {"key": "page_count", "value": '["24"]'},
                                            {
                                                "key": "custom_instructions",
                                                "value": (
                                                    "Yellow wallpaper & pawnbroker watch motif"
                                                ),
                                            },
                                        ],
                                    }
                                },
                            }
                        ],
                    },
                },
            },
        )
        assert resp.status_code == 200
        from storybook.a2a.router import _a2a_sessions

        st = _a2a_sessions["sess_override_1"]
        assert st.config.source.title == "Crime and Punishment"
        assert st.config.source.author == "F. M. Dostoevsky"
        assert st.config.image_spec == "Custom St. Petersburg charcoal & gold leaf"
        assert st.config.text_spec == "Custom detective fable narration"
        assert st.config.target_age == "10-12"
        assert st.config.page_count == 24
        assert st.config.custom_instructions == "Yellow wallpaper & pawnbroker watch motif"
        assert _studio_contexts["ctx-override-1"]["title"] == "Crime and Punishment"
        assert _studio_contexts["ctx-override-1"]["author"] == "F. M. Dostoevsky"


@pytest.mark.asyncio
async def test_reclaim_finished_actors_and_error_reporting():
    """Verify _reclaim_finished_actors stops finished sessions and create_actor failures mark session as error."""  # noqa: E501
    from storybook.a2a.router import (
        _a2a_sessions,
        _create_and_start_storybook_session,
        _reclaim_finished_actors,
    )
    from storybook.models import PipelineState, SessionConfig, SourceConfig

    done_session = PipelineState(
        session_id="done-sess-1",
        config=SessionConfig(source=SourceConfig(title="Alice", author="Lewis Carroll")),
        current_stage="done",
    )
    err_session = PipelineState(
        session_id="err-sess-2",
        config=SessionConfig(source=SourceConfig(title="Bob", author="Author")),
        current_stage="error",
    )
    running_session = PipelineState(
        session_id="run-sess-3",
        config=SessionConfig(source=SourceConfig(title="Running", author="Author")),
        current_stage="generating_image",
    )

    with (
        patch(
            "storybook.a2a.router.store.list_sessions",
            new=AsyncMock(return_value=[done_session, err_session, running_session]),
        ),
        patch("storybook.a2a.router.ate.stop_actor", new_callable=AsyncMock) as mock_stop,
    ):
        reclaimed = await _reclaim_finished_actors()
        assert reclaimed >= 2
        stopped_names = {call.kwargs["name"] for call in mock_stop.await_args_list}
        assert "done-sess-1" in stopped_names
        assert "err-sess-2" in stopped_names
        assert "run-sess-3" not in stopped_names

    # Verify RESOURCE_EXHAUSTED triggers reclaim + retry, and persistent failure sets stage="error"
    saved_events: list[tuple] = []

    def _capture_events(sid, evs, done=False):
        saved_events.append((sid, evs, done))
        return "gs://fake/events.json"

    with (
        patch(
            "storybook.a2a.router.store.list_sessions",
            new=AsyncMock(return_value=[done_session]),
        ),
        patch("storybook.a2a.router.ate.stop_actor", new_callable=AsyncMock) as mock_stop2,
        patch(
            "storybook.a2a.router.ate.create_actor",
            new=AsyncMock(
                side_effect=RuntimeError("StatusCode.RESOURCE_EXHAUSTED: no free workers available")
            ),
        ) as mock_create,
        patch("storybook.a2a.router.gcs.save_pipeline_state"),
        patch("storybook.a2a.router.gcs.save_progress_events", side_effect=_capture_events),
        patch("storybook.a2a.router.api_routes._persist_session", new_callable=AsyncMock),
    ):
        failed_state = await _create_and_start_storybook_session(
            {"title": "Demons", "author": "Dostoevsky"}
        )
        assert len(failed_state.session_id) == 36  # Full UUID
        assert failed_state.current_stage == "error"
        assert any("RESOURCE_EXHAUSTED" in e for e in failed_state.errors)
        assert mock_create.await_count == 2  # initial attempt + 1 retry after reclaim
        assert mock_stop2.await_count >= 2
        assert saved_events[-1][2] is True
        assert saved_events[-1][1][0]["stage"] == "error"
        _a2a_sessions.pop(failed_state.session_id, None)
