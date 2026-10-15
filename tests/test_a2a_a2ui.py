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
    assert envelopes[0] == {"beginRendering": {"surfaceId": "studio_1", "root": "root"}}
    assert "surfaceUpdate" in envelopes[1]
    assert "dataModelUpdate" in envelopes[2]

    comps = {c["id"]: c["component"] for c in envelopes[1]["surfaceUpdate"]["components"]}
    assert comps["root"] == {"Card": {"child": "main-col"}}
    assert "explicitList" in comps["main-col"]["Column"]["children"]
    assert "MultipleChoice" in comps["art-picker"]
    assert comps["art-picker"]["MultipleChoice"]["selections"] == {
        "path": "/studio/artStyleSelections"
    }
    assert len(comps["art-picker"]["MultipleChoice"]["options"]) == 4
    assert "MultipleChoice" in comps["text-picker"]
    assert len(comps["text-picker"]["MultipleChoice"]["options"]) == 4
    assert "MultipleChoice" in comps["age-picker"]
    assert "MultipleChoice" in comps["pages-picker"]
    assert "TextField" in comps["custom-motif-input"]
    assert comps["generate-btn"]["Button"]["action"]["name"] == "generate_storybook"

    dm = envelopes[2]["dataModelUpdate"]
    assert dm["path"] == "/studio"
    dm_keys = {item["key"]: item for item in dm["contents"]}
    assert dm_keys["title"]["valueString"] == "Demons"
    assert dm_keys["author"]["valueString"] == "Fyodor Dostoevsky"
    assert dm_keys["ageSelections"]["valueMap"][0]["valueString"] == "8-10"
    assert dm_keys["pageCountSelections"]["valueMap"][0]["valueString"] == "16"

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
        assert len(frames) == 5
        # Frame 0: submitted (final: False)
        assert frames[0]["result"]["kind"] == "status-update"
        assert frames[0]["result"]["status"]["state"] == "submitted"
        assert frames[0]["result"]["final"] is False
        # Frame 1: working (final: False)
        assert frames[1]["result"]["status"]["state"] == "working"
        assert frames[1]["result"]["final"] is False
        # Frame 2: working with parts [text, beginRendering, surfaceUpdate, dataModelUpdate]
        working_parts = frames[2]["result"]["status"]["message"]["parts"]
        a2ui_parts = _extract_a2ui_parts(working_parts)
        assert len(a2ui_parts) == 3
        assert "beginRendering" in a2ui_parts[0]["data"]
        assert "surfaceUpdate" in a2ui_parts[1]["data"]
        assert "dataModelUpdate" in a2ui_parts[2]["data"]
        dm_contents = {
            item["key"]: item for item in a2ui_parts[2]["data"]["dataModelUpdate"]["contents"]
        }
        assert dm_contents["title"]["valueString"] == "Demons"
        assert "Dostoevsky" in dm_contents["author"]["valueString"]
        # Frame 3: artifact-update (lastChunk: True)
        assert frames[3]["result"]["kind"] == "artifact-update"
        assert frames[3]["result"]["lastChunk"] is True
        assert len(_extract_a2ui_parts(frames[3]["result"]["artifact"]["parts"])) == 3
        # Frame 4: completed (final: True)
        assert frames[4]["result"]["kind"] == "status-update"
        assert frames[4]["result"]["status"]["state"] == "completed"
        assert frames[4]["result"]["final"] is True

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
        assert refine_frames[-1]["result"]["final"] is True
        refine_a2ui = _extract_a2ui_parts(
            refine_frames[-1]["result"]["status"]["message"]["parts"]
        )
        refine_dm = {
            item["key"]: item
            for item in refine_a2ui[2]["data"]["dataModelUpdate"]["contents"]
        }
        assert refine_dm["title"]["valueString"] == "Demons"
        assert refine_dm["ageSelections"]["valueMap"][0]["valueString"] == "10-12"
        assert refine_dm["pageCountSelections"]["valueMap"][0]["valueString"] == "16"

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
        assert lucky_frames[-1]["result"]["final"] is True
        lucky_a2ui = _extract_a2ui_parts(
            lucky_frames[-1]["result"]["status"]["message"]["parts"]
        )
        lucky_dm = {
            item["key"]: item
            for item in lucky_a2ui[2]["data"]["dataModelUpdate"]["contents"]
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
            # Verify final markdown link + PDF URL in completed frame
            final_parts = frames[-1]["result"]["status"]["message"]["parts"]
            assert (
                "[📥 Download Completed Storybook PDF](https://pushkin-agent.duckdns.org/a2a/sessions/sess_demons/pdf)"  # noqa: E501
                in final_parts[0]["text"]
            )
            # Verify artifact-update frame was also emitted before completion
            assert any(f["result"].get("kind") == "artifact-update" for f in frames)
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
