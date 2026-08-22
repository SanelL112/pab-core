"""Text pages carrying ink/images must fall through to the vision route
when text extraction yields nothing (regression: header-only pages with
handwritten content returned empty forever)."""
import json

from scrapers import onenote_page_extractor as ope


def _make_page():
    return {"id": "p1", "title": "Lesson 2", "links": {}}


def test_text_header_plus_ink_falls_through_to_vision(monkeypatch):
    html = (
        '<div style="position:absolute;top:10px;left:20px">Lesson 2 pages 7 - 15</div>'
        "<img src='data:image/png;base64,AAAA'>"
    )
    calls = {"text": 0, "vision": 0}

    def fake_local(prompt, system, timeout):
        calls["text"] += 1
        return "[]"

    def fake_vision(image_bytes, prompt, timeout):
        calls["vision"] += 1
        assert image_bytes == b"PNGDATA"
        return json.dumps([{"title": "Quiz 1", "due_date": "2026-09-02", "task_type": "Quiz"}])

    monkeypatch.setattr(ope, "_call_local_llm", fake_local)
    monkeypatch.setattr(ope, "_call_vision_llm", fake_vision)

    rows = ope.extract_tasks_from_page(
        _make_page(), html, render_snapshot=lambda page, h: b"PNGDATA"
    )

    assert calls["text"] == 1 and calls["vision"] == 1
    assert len(rows) == 1 and rows[0]["title"] == "Quiz 1"


def test_text_success_still_skips_vision(monkeypatch):
    html = '<div style="position:absolute;top:10px;left:20px">HW due 2026-09-01</div><img src="x.png">'
    monkeypatch.setattr(ope, "_call_local_llm", lambda p, s, t: json.dumps(
        [{"title": "HW", "due_date": "2026-09-01", "task_type": "Assignment"}]))
    monkeypatch.setattr(ope, "_call_vision_llm",
                        lambda *a: (_ for _ in ()).throw(AssertionError("vision must not run")))

    rows = ope.extract_tasks_from_page(_make_page(), html, render_snapshot=lambda p, h: b"X")
    assert len(rows) == 1 and rows[0]["title"] == "HW"


def test_header_date_rows_are_dropped():
    """Rows dated exactly on the page's creation header must be dropped —
    small models read that header as a deadline regardless of prompt."""
    html = (
        "<div>Wednesday, July 29, 2026 12:01 PM</div>"
        '<div style="position:absolute;top:40px;left:20px">Unit 1 Notes Packet</div>'
        "<img src='x.png'>"
    )
    monkeypatched_vision = json.dumps([
        {"title": "Fake from header", "due_date": "2026-07-29", "task_type": "Reading"},
        {"title": "Real quiz", "due_date": "2026-09-02", "task_type": "Quiz"},
    ])
    from scrapers import onenote_page_extractor as ope
    ope._call_local_llm = lambda *a: "[]"  # not used: no text route rows
    rows = ope.extract_tasks_from_page(
        {"id": "p", "title": "T", "links": {}}, html,
        render_snapshot=lambda p, h: b"PNG",
    ) if False else None
    # call through the vision path directly with monkeypatch
    import unittest.mock as mock
    with mock.patch.object(ope, "_call_local_llm", return_value="[]"), \
         mock.patch.object(ope, "_call_vision_llm", return_value=monkeypatched_vision):
        rows = ope.extract_tasks_from_page(
            {"id": "p", "title": "T", "links": {}}, html,
            render_snapshot=lambda p, h: b"PNG",
        )
    titles = [r["title"] for r in rows]
    assert "Fake from header" not in titles
    assert "Real quiz" in titles


def test_header_date_detector():
    from scrapers.onenote_page_extractor import _page_header_dates
    h = "<div>Wednesday, July 29, 2026 12:01 PM</div><p>due Friday, September 2, 2026</p>"
    assert _page_header_dates(h) == {"2026-07-29", "2026-09-02"}
