"""Hermetic tests for the OneNote scraper and spatial/vision extractor.

No network, no browser, no subprocess. Every Graph/token/LLM/vision boundary is
mocked. Covers: OAuth2 refresh + refresh-token rotation, 401 re-auth, page
listing pagination, content fetch, asset download, 2D spatial layout sorting
(including scrambled CSS coordinates), image-only + ink vision routing, and
malformed-date handling.
"""

from __future__ import annotations

import base64
import json

import pytest
import requests

import scrapers.onenote_scraper as onenote_scraper
import scrapers.onenote_page_extractor as onenote_extractor
from scrapers.onenote_scraper import (
    OneNoteAuthError,
    OneNoteClient,
    OneNoteRequestError,
    _iter_asset_urls,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, *, status_code=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def token_store(tmp_path):
    return tmp_path / "onenote_token.json"


@pytest.fixture
def client(token_store):
    return OneNoteClient(
        client_id="cid",
        client_secret="secret",
        refresh_token="rt-initial",
        token_path=token_store,
        graph_root="https://graph.example/v1.0",
    )


# ─────────────────────────────────────────────────────────────────────────────
# OAuth2
# ─────────────────────────────────────────────────────────────────────────────
def test_refresh_rotates_and_persists_refresh_token(client, token_store, monkeypatch):
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        return FakeResponse(
            json_data={
                "access_token": "at-1",
                "refresh_token": "rt-rotated",
                "expires_in": 3600,
            }
        )

    monkeypatch.setattr(requests, "post", fake_post)
    token = client._ensure_token()

    assert token == "at-1"
    assert client._refresh_token == "rt-rotated"  # rotation captured
    assert calls["data"]["grant_type"] == "refresh_token"
    assert calls["data"]["refresh_token"] == "rt-initial"

    # Persisted so the next process reuses the rotated token.
    stored = json.loads(token_store.read_text())
    assert stored["refresh_token"] == "rt-rotated"
    assert stored["access_token"] == "at-1"


def test_missing_refresh_token_raises(token_store, monkeypatch):
    c = OneNoteClient(client_id="cid", refresh_token="", token_path=token_store)
    with pytest.raises(OneNoteAuthError):
        c._ensure_token()


def test_token_endpoint_error_raises(client, monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse(status_code=400, text="bad_grant")
    )
    with pytest.raises(OneNoteAuthError):
        client._refresh_access_token()


def test_cached_token_avoids_extra_refresh(client, monkeypatch):
    posts = {"n": 0}

    def fake_post(*a, **k):
        posts["n"] += 1
        return FakeResponse(json_data={"access_token": "at", "expires_in": 3600})

    monkeypatch.setattr(requests, "post", fake_post)
    client._ensure_token()
    client._ensure_token()  # still valid -> no second refresh
    assert posts["n"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Graph requests
# ─────────────────────────────────────────────────────────────────────────────
def _prime_token(client, monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse(json_data={"access_token": "at", "expires_in": 3600})
    )


def test_list_pages_follows_pagination(client, monkeypatch):
    _prime_token(client, monkeypatch)
    pages_batches = [
        {
            "value": [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
            "@odata.nextLink": "https://graph.example/v1.0/me/onenote/pages?page=2",
        },
        {"value": [{"id": "3", "title": "C"}]},
    ]
    seen_urls = []

    def fake_request(method, url, headers=None, stream=False, timeout=None):
        seen_urls.append(url)
        assert headers["Authorization"] == "Bearer at"
        return FakeResponse(json_data=pages_batches[len(seen_urls) - 1])

    monkeypatch.setattr(requests, "request", fake_request)
    pages = client.list_pages(top=2)
    assert [p["id"] for p in pages] == ["1", "2", "3"]
    assert len(seen_urls) == 2
    assert seen_urls[1].endswith("page=2")  # nextLink followed verbatim


def test_401_triggers_single_reauth(client, monkeypatch):
    # Pre-seed a valid access token so the ONLY refresh is the 401-triggered one.
    client._access_token = "at-valid"
    client._access_expiry = 9_999_999_999.0
    states = {"count": 0}

    def fake_request(method, url, headers=None, stream=False, timeout=None):
        states["count"] += 1
        if states["count"] == 1:
            return FakeResponse(status_code=401, text="expired")
        return FakeResponse(json_data={"value": []})

    reauths = {"n": 0}

    def counting_refresh():
        reauths["n"] += 1
        client._access_token = "at-2"
        client._access_expiry = 9_999_999_999.0

    monkeypatch.setattr(client, "_refresh_access_token", counting_refresh)
    monkeypatch.setattr(requests, "request", fake_request)

    data = client._get_json("/me/onenote/pages")
    assert data == {"value": []}
    assert reauths["n"] == 1  # exactly one re-auth
    assert states["count"] == 2  # retried once


def test_get_page_html_includes_ink_flag(client, monkeypatch):
    _prime_token(client, monkeypatch)
    captured = {}

    def fake_request(method, url, headers=None, stream=False, timeout=None):
        captured["url"] = url
        return FakeResponse(text="<html><body>hi</body></html>")

    monkeypatch.setattr(requests, "request", fake_request)
    html = client.get_page_html("PID", include_ink=True)
    assert "hi" in html
    assert captured["url"].endswith("/pages/PID/content?includeInkML=true")


def test_download_page_assets_handles_partial_failure(client, monkeypatch):
    _prime_token(client, monkeypatch)
    body = (
        '<img data-fullres-src="https://graph.example/v1.0/me/onenote/resources/img1/$value" src="x"/>'
        '<object data="https://graph.example/v1.0/me/onenote/resources/file1/$value"></object>'
    )

    def fake_request(method, url, headers=None, stream=False, timeout=None):
        if "img1" in url:
            return FakeResponse(content=b"PNGDATA", headers={"Content-Type": "image/png"})
        raise OneNoteRequestError("boom on file1")

    monkeypatch.setattr(requests, "request", fake_request)
    assets = client.download_page_assets(body)
    kinds = {a["kind"] for a in assets}
    assert kinds == {"image", "attachment"}
    img = next(a for a in assets if a["kind"] == "image")
    assert img["data"] == b"PNGDATA" and img["content_type"] == "image/png"
    attach = next(a for a in assets if a["kind"] == "attachment")
    assert attach["data"] == b""  # failure degraded, not raised


def test_iter_asset_urls_prefers_fullres_and_skips_data_uri():
    body = (
        '<img data-fullres-src="https://x/full/$value" src="https://x/thumb/$value"/>'
        '<img src="data:image/png;base64,AAAA"/>'
        '<object data="https://x/doc.pdf"></object>'
    )
    urls = list(_iter_asset_urls(body))
    assert ("image", "https://x/full/$value") in urls
    assert ("attachment", "https://x/doc.pdf") in urls
    assert all(not u.startswith("data:") for _kind, u in urls)


# ─────────────────────────────────────────────────────────────────────────────
# 2D spatial layout parsing
# ─────────────────────────────────────────────────────────────────────────────
def test_spatial_layout_restores_reading_order_from_scrambled_coords():
    # Source order is deliberately shuffled; coordinates define the true order.
    html = """
    <div style="position:absolute;top:300px;left:40px">line3-left</div>
    <div style="position:absolute;top:10px;left:80px">line1-top</div>
    <div style="position:absolute;top:305px;left:600px">line3-right</div>
    <div style="position:absolute;top:150px;left:20px">line2-middle</div>
    """
    ordered = onenote_extractor.parse_spatial_layout(html)
    lines = ordered.splitlines()
    assert lines == ["line1-top", "line2-middle", "line3-left", "line3-right"]


def test_spatial_layout_row_band_groups_same_line_left_to_right():
    # top values 300 and 305 are within the row band -> same visual line.
    html = """
    <div style="position:absolute;top:305px;left:900px">z-end</div>
    <div style="position:absolute;top:300px;left:10px">a-start</div>
    """
    ordered = onenote_extractor.parse_spatial_layout(html)
    assert ordered.splitlines() == ["a-start", "z-end"]


def test_spatial_layout_appends_unpositioned_content():
    html = """
    <div style="position:absolute;top:10px;left:10px">positioned title</div>
    <p>a pasted paragraph with no coordinates</p>
    """
    ordered = onenote_extractor.parse_spatial_layout(html)
    assert "positioned title" in ordered
    assert "pasted paragraph" in ordered


# ─────────────────────────────────────────────────────────────────────────────
# Visual content detection & routing
# ─────────────────────────────────────────────────────────────────────────────
def test_detect_visual_content_flags():
    text_page = '<div style="position:absolute;top:1px;left:1px">Read chapter 4 by 9/1</div>'
    prof = onenote_extractor.detect_visual_content(text_page)
    assert prof["has_text"] and not prof["image_only"]

    img_page = '<img data-fullres-src="https://x/full/$value"/>'
    prof = onenote_extractor.detect_visual_content(img_page)
    assert prof["has_images"] and prof["image_only"] and not prof["has_text"]

    ink_page = '<div data-ink="true"><inkml:ink>...</inkml:ink></div>'
    prof = onenote_extractor.detect_visual_content(ink_page)
    assert prof["has_ink"] and prof["image_only"]


def test_text_page_routes_to_text_llm(monkeypatch):
    onenote_extractor._budget_remaining  # ensure symbol import
    monkeypatch.setattr(
        onenote_extractor,
        "_call_local_llm",
        lambda *a, **k: '[{"title":"Unit 2 Test","due_date":"9/4","task_type":"test"}]',
    )
    # Vision must NOT be called for a text page.
    monkeypatch.setattr(
        onenote_extractor,
        "_call_vision_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("vision should not run")),
    )
    page = {"id": "p1", "title": "Bio Notes", "links": {"oneNoteWebUrl": {"href": "https://onenote/p1"}}}
    html = '<div style="position:absolute;top:1px;left:1px">Unit 2 Test on 9/4</div>'
    rows = onenote_extractor.extract_tasks_from_page(page, html)
    assert len(rows) == 1
    assert rows[0]["due_date"] == "2026-09-04"
    assert rows[0]["task_type"] == "Test"
    assert rows[0]["source"] == "onenote"
    assert rows[0]["id"].startswith("onenote-")
    assert rows[0]["url"] == "https://onenote/p1"


def test_text_page_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setattr(onenote_extractor, "_call_local_llm", lambda *a, **k: "not json")
    page = {"id": "p2", "title": "Notes"}
    html = '<div style="position:absolute;top:1px;left:1px">Monday, 8/17 - U1Q1 quiz</div>'
    rows = onenote_extractor.extract_tasks_from_page(page, html)
    assert rows and rows[0]["due_date"] == "2026-08-17"


def test_image_only_page_routes_to_vision_with_snapshot(monkeypatch):
    captured = {}

    def fake_vision(image_bytes, prompt, timeout):
        captured["image"] = image_bytes
        return '[{"title":"Chem lab","due_date":"2026-10-05","task_type":"Project"}]'

    monkeypatch.setattr(onenote_extractor, "_call_vision_llm", fake_vision)
    # Text LLM must NOT run for an image-only page.
    monkeypatch.setattr(
        onenote_extractor,
        "_call_local_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("text llm should not run")),
    )

    page = {"id": "p3", "title": "Scanned"}
    html = '<img data-fullres-src="https://x/full/$value"/>'

    def render(page_meta, page_html):
        return b"FAKE_PNG_BYTES"

    rows = onenote_extractor.extract_tasks_from_page(page, html, render_snapshot=render)
    assert captured["image"] == b"FAKE_PNG_BYTES"
    assert len(rows) == 1
    assert rows[0]["title"] == "Chem lab"
    assert rows[0]["task_type"] == "Project"


def test_image_only_page_without_renderer_yields_nothing(monkeypatch):
    monkeypatch.setattr(
        onenote_extractor,
        "_call_vision_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no snapshot, no vision")),
    )
    page = {"id": "p4", "title": "Scanned"}
    html = '<img data-fullres-src="https://x/full/$value"/>'
    assert onenote_extractor.extract_tasks_from_page(page, html) == []


def test_vision_output_malformed_dates_are_dropped(monkeypatch):
    monkeypatch.setattr(
        onenote_extractor,
        "_call_vision_llm",
        lambda *a, **k: (
            '[{"title":"bad","due_date":"2026-02-30","task_type":"Test"},'
            ' {"title":"ok","due_date":"2026-05-01","task_type":"Reading"}]'
        ),
    )
    page = {"id": "p5", "title": "Scan"}
    html = '<img data-fullres-src="https://x/full/$value"/>'
    rows = onenote_extractor.extract_tasks_from_page(page, html, render_snapshot=lambda p, h: b"PNG")
    assert len(rows) == 1
    assert rows[0]["title"] == "ok" and rows[0]["due_date"] == "2026-05-01"


def test_vision_llm_base64_encodes_image(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["payload"] = json
        return FakeResponse(json_data={"response": "[]"})

    monkeypatch.setattr(requests, "post", fake_post)
    out = onenote_extractor._call_vision_llm(b"\x89PNG_BYTES", "prompt", 10.0)
    assert out == "[]"
    payload = sent["payload"]
    assert payload is not None
    assert payload["images"] == [base64.b64encode(b"\x89PNG_BYTES").decode("ascii")]
    assert payload["model"] == onenote_extractor._VISION_MODEL
    assert payload["options"]["temperature"] == 0.0


def test_empty_page_returns_empty():
    assert onenote_extractor.extract_tasks_from_page({"id": "x"}, "") == []
    assert onenote_extractor.parse_spatial_layout("") == ""
    prof = onenote_extractor.detect_visual_content("")
    assert not any(prof.values())
