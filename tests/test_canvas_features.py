from datetime import datetime, timedelta, timezone

import scrapers.canvas_scraper as canvas_scraper
from scrapers.canvas_scraper import (
    _assignment_is_actionable,
    _canvas_link_target,
    _crawl_canvas_page_links,
    _follow_canvas_page_links,
    _get_canvas_pages,
    _get_upcoming_assignments,
    _is_recent_canvas_update,
    _parse_canvas_page_html,
    _submission_state,
    _supported_canvas_study_file,
)


def test_canvas_assignment_filter_rejects_old_due_work(monkeypatch):
    monkeypatch.setenv("CANVAS_ASSIGNMENT_OVERDUE_GRACE_DAYS", "7")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert not _assignment_is_actionable({"due_at": "2026-05-01T23:59:00Z"}, now)
    assert _assignment_is_actionable({"due_at": "2026-07-26T23:59:00Z"}, now)
    assert _assignment_is_actionable({"due_at": "2026-07-19T23:59:00Z"}, now)


def test_canvas_assignment_filter_keeps_only_recent_undated_items(monkeypatch):
    monkeypatch.setenv("CANVAS_NO_DUE_UPDATE_DAYS", "21")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert _assignment_is_actionable({"updated_at": "2026-07-10T00:00:00Z"}, now)
    assert not _assignment_is_actionable({"updated_at": "2026-02-13T00:00:00Z"}, now)


def test_canvas_content_filter_hides_old_pages_and_announcements(monkeypatch):
    monkeypatch.setenv("CANVAS_CONTENT_UPDATE_DAYS", "21")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert _is_recent_canvas_update({"updated_at": "2026-07-20T00:00:00Z"}, "updated_at", now=now)
    assert not _is_recent_canvas_update({"updated_at": "2026-02-13T00:00:00Z"}, "updated_at", now=now)


def test_canvas_action_plan_uses_completion_not_scores():
    now = datetime.now(timezone.utc)

    class FakeCanvas:
        def get_favorite_courses(self):
            return [{"id": "1", "name": "Physics"}]

        def get_paginated(self, _path, max_pages=1):
            return [
                {
                    "name": "Lab report", "due_at": (now - timedelta(days=1)).isoformat(),
                    "submission": {"missing": True},
                },
                {
                    "name": "Chapter quiz", "due_at": (now + timedelta(days=2)).isoformat(),
                    "submission": {},
                },
                {
                    "name": "Practice problems", "due_at": (now - timedelta(days=1)).isoformat(),
                    "submission": {"submitted_at": now.isoformat(), "score": 0},
                },
            ]

    result = _get_upcoming_assignments(FakeCanvas())

    assert "Lab report" in result and "**Missing**" in result
    assert "Chapter quiz" in result and "Due soon" in result
    assert "Practice problems" in result and "Recently completed" in result
    assert "score" not in result.lower()
    assert _submission_state({"submission": {"submitted_at": now.isoformat()}}, now) == "Completed"
    assert _supported_canvas_study_file({"display_name": "review.docx"})
    assert not _supported_canvas_study_file({"display_name": "lecture.mp4"})


def test_canvas_page_link_targets_stay_in_the_same_course():
    assert _canvas_link_target("/courses/42/pages/exam-review", "42") == (
        "page", "/api/v1/courses/42/pages/exam-review",
    )
    assert _canvas_link_target("/courses/42/assignments/99?module_item_id=3", "42") == (
        "assignment", "/api/v1/courses/42/assignments/99",
    )
    assert _canvas_link_target("/courses/42/files/123/download", "42") == (
        "file", "/api/v1/courses/42/files/123",
    )
    assert _canvas_link_target("/courses/7/pages/other-course", "42") is None
    assert _canvas_link_target("https://example.com/lesson", "42") is None


def test_public_link_must_match_multiple_specific_source_terms(monkeypatch):
    monkeypatch.delenv("CANVAS_EXTERNAL_LINK_MIN_CONTEXT_MATCHES", raising=False)

    terms = {"photosynthesis", "chloroplast", "light-dependent"}
    assert canvas_scraper._is_contextual_external_text(
        "Photosynthesis happens in a chloroplast during light-dependent reactions.", terms
    )
    assert not canvas_scraper._is_contextual_external_text(
        "This general biology news story mentions photosynthesis once.", terms
    )


def test_canvas_page_links_are_read_and_included_in_page_data(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(canvas_scraper, "_fetch_contextual_external_page", lambda _href, _terms: ("", "", []))

    class FakeCanvas:
        def __init__(self):
            self.paths = []

        def get_favorite_courses(self):
            return [{"id": "42", "name": "Biology"}]

        def get_paginated(self, _path, max_pages=1):
            return [{"title": "Lab overview", "url": "lab-overview", "updated_at": now}]

        def get_json(self, path):
            self.paths.append(path)
            if path.endswith("/pages/lab-overview"):
                return {
                    "body": (
                        "<p>Read the safety instructions.</p>"
                        '<a href="/courses/42/assignments/99">Lab report</a>'
                        '<a href="https://example.com/ignored">External site</a>'
                    )
                }
            if path.endswith("/assignments/99"):
                return {"name": "Lab report", "due_at": "2026-08-14T23:59:00Z", "description": "<p>Submit your data.</p>"}
            raise AssertionError(path)

    canvas = FakeCanvas()
    result = _get_canvas_pages(canvas)

    assert "Read the safety instructions." in result
    assert "Assignment: Lab report (due 2026-08-14) — Submit your data." in result
    assert all("example.com" not in path for path in canvas.paths)

    text, links = _parse_canvas_page_html('<p>Hello <a href="/courses/42/pages/x">world</a></p>')
    assert text == "Hello world"
    assert links == [{"href": "/courses/42/pages/x", "label": "world"}]

    linked = _follow_canvas_page_links(canvas, "42", '<a href="/courses/42/assignments/99">Lab report</a>')
    assert linked == ["Assignment: Lab report (due 2026-08-14) — Submit your data."]


def test_canvas_link_crawler_recurses_without_cycles_and_keeps_contextual_public_leaves(monkeypatch):
    monkeypatch.setenv("CANVAS_PAGE_LINK_MAX_DEPTH", "5")
    monkeypatch.setenv("CANVAS_PAGE_LINK_MAX_ITEMS", "10")
    monkeypatch.setattr(
        canvas_scraper,
        "_fetch_contextual_external_page",
        lambda href, _terms: (
            href,
            "Public source (example.org): relevant biology reference",
            ["https://example.org/child-reference"],
        ) if href == "https://example.org/lab-safety" else (
            href,
            "Public source (example.org): relevant biology child reference",
            [],
        ) if href == "https://example.org/child-reference" else ("", "", []),
    )

    class FakeCanvas:
        def __init__(self):
            self.paths = []

        def get_json(self, path):
            self.paths.append(path)
            if path.endswith("/pages/first"):
                return {
                    "title": "First page",
                    "body": (
                        '<a href="/courses/42/pages/second">second</a>'
                        '<a href="https://example.org/lab-safety">reference</a>'
                    ),
                }
            if path.endswith("/pages/second"):
                return {
                    "title": "Second page",
                    "body": '<a href="/courses/42/assignments/99">assignment</a><a href="/courses/42/pages/first">cycle</a>',
                }
            if path.endswith("/assignments/99"):
                return {"name": "Lab report", "description": "<p>Analyze biology data.</p>"}
            raise AssertionError(path)

    canvas = FakeCanvas()
    crawled = _crawl_canvas_page_links(
        canvas,
        "42",
        '<a href="/courses/42/pages/first">first</a>',
        context_terms={"biology", "safety", "laboratory"},
    )

    assert [depth for depth, _description in crawled] == [1, 2, 2, 3, 3]
    assert any("First page" in description for _depth, description in crawled)
    assert any("Second page" in description for _depth, description in crawled)
    assert any("Lab report" in description for _depth, description in crawled)
    assert sum("Public source" in description for _depth, description in crawled) == 2
    assert canvas.paths.count("/api/v1/courses/42/pages/first") == 1


# ─────────────────────────────────────────────────────────────────────────────
# Canvas page extractor (scrapers/canvas_page_extractor.py)
# ─────────────────────────────────────────────────────────────────────────────
import pytest

import scrapers.canvas_page_extractor as page_extractor


@pytest.fixture(autouse=True)
def _isolate_extractor(monkeypatch):
    """Every extractor test runs offline with no disk cache and a fresh budget."""
    monkeypatch.setattr(page_extractor, "_CACHE_PATH", None, raising=False)
    # Disable embedded-doc network fetches unless a test opts back in.
    monkeypatch.setattr(page_extractor, "_fetch_external_link_text", lambda *a, **k: "")
    # Ample budget so chunk loop is never starved.
    page_extractor.reset_extraction_budget()
    yield


def _disable_llm(monkeypatch):
    """Force the deterministic heuristic path (no LLM, no network)."""
    monkeypatch.setattr(page_extractor, "_call_local_llm", lambda *a, **k: "")


def test_extractor_preserves_multi_column_table_as_markdown_rows(monkeypatch):
    _disable_llm(monkeypatch)
    html = """
    <div class="ic-app-header">Global Nav Home Dashboard Courses</div>
    <h2>Unit 1</h2>
    <table>
      <tr><th>Date</th><th>Topic</th><th>Assignment / Assessment</th></tr>
      <tr><td>8/17</td><td>Cells</td><td>U1Q1 quiz</td></tr>
      <tr><td>9/4</td><td>Energy</td><td>U1 Test</td></tr>
    </table>
    """
    compacted = page_extractor._parse_html_with_structure_and_links(html)
    assert "[TABLE DATA]" in compacted
    assert "Date | Topic | Assignment / Assessment" in compacted
    assert "Global Nav" not in compacted  # navigation chrome stripped

    rows = page_extractor.extract_assignments_from_html("42", "Biology", "Unit 1", "unit-1", html)
    titles = {r["title"] for r in rows}
    dates = {r["due_date"] for r in rows}
    assert "2026-08-17" in dates and "2026-09-04" in dates
    assert any("U1Q1" in t for t in titles)
    assert all(r["task_type"] in {"Test", "Project", "Reading", "Assignment"} for r in rows)
    assert all(r["official"] is False for r in rows)


def test_extractor_resolves_nested_iframe_embeds(monkeypatch):
    _disable_llm(monkeypatch)
    # Re-enable a *mocked* embed fetch that returns a daily-agenda snippet.
    monkeypatch.setattr(
        page_extractor,
        "_fetch_external_link_text",
        lambda url, **k: "Plans: Monday 8/25 - U2 Quiz on cell division" if "presentation" in url else "",
    )
    html = """
    <p>Daily agenda below:</p>
    <iframe title="Agenda" src="https://docs.google.com/presentation/d/e/2PACX-abc/pubembed?start=false"></iframe>
    """
    compacted = page_extractor._parse_html_with_structure_and_links(html)
    assert "Embedded Doc (Agenda)" in compacted
    assert "U2 Quiz" in compacted

    rows = page_extractor.extract_assignments_from_html("7", "Bio", "Agenda", "agenda", html)
    assert any(r["due_date"] == "2026-08-25" for r in rows)


def test_extractor_uses_llm_when_available_and_normalizes_output(monkeypatch):
    # LLM returns valid JSON with a messy date + lowercase type -> must normalize.
    monkeypatch.setattr(
        page_extractor,
        "_call_local_llm",
        lambda *a, **k: '[{"title": "Midterm", "due_date": "October 3, 2026", "task_type": "exam"}]',
    )
    html = "<p>See you at the midterm.</p>"
    rows = page_extractor.extract_assignments_from_html("1", "Physics", "Home", "home", html)
    assert len(rows) == 1
    assert rows[0]["due_date"] == "2026-10-03"
    assert rows[0]["task_type"] == "Test"  # "exam" -> Test
    assert rows[0]["id"].startswith("page-")


def test_extractor_falls_back_to_heuristic_on_malformed_llm_json(monkeypatch):
    # Model emits garbage; extractor must silently fall back to regex rules.
    monkeypatch.setattr(page_extractor, "_call_local_llm", lambda *a, **k: "Sure! Here you go: not json at all")
    html = "<p>Monday, 8/17 - U1Q1 quiz is due</p>"
    rows = page_extractor.extract_assignments_from_html("1", "Bio", "P", "p", html)
    assert rows, "heuristic fallback should still find the dated quiz"
    assert rows[0]["due_date"] == "2026-08-17"


def test_extractor_drops_malformed_dates(monkeypatch):
    monkeypatch.setattr(
        page_extractor,
        "_call_local_llm",
        lambda *a, **k: (
            '[{"title": "Bad month", "due_date": "2026-13-40", "task_type": "Test"},'
            ' {"title": "Not a date", "due_date": "TBD", "task_type": "Reading"},'
            ' {"title": "Good one", "due_date": "2026-09-15", "task_type": "Assignment"}]'
        ),
    )
    rows = page_extractor.extract_assignments_from_html("1", "Bio", "P", "p", "<p>text</p>")
    assert len(rows) == 1
    assert rows[0]["title"] == "Good one"
    assert rows[0]["due_date"] == "2026-09-15"


def test_extractor_chunks_long_pages_without_truncation(monkeypatch):
    # Two well-separated dated items far enough apart to land in different chunks.
    monkeypatch.setattr(page_extractor, "_CHUNK_CHARS", 400, raising=False)
    monkeypatch.setattr(page_extractor, "_CHUNK_OVERLAP", 20, raising=False)

    seen_chunks = []

    def fake_llm(prompt, system, timeout):
        seen_chunks.append(prompt)
        if "ALPHA" in prompt:
            return '[{"title": "Alpha quiz", "due_date": "2026-08-10", "task_type": "Test"}]'
        if "OMEGA" in prompt:
            return '[{"title": "Omega project", "due_date": "2026-11-20", "task_type": "Project"}]'
        return "[]"

    monkeypatch.setattr(page_extractor, "_call_local_llm", fake_llm)
    body = "ALPHA due soon. " + ("filler words here " * 40) + " OMEGA due later."
    html = f"<p>{body}</p>"
    rows = page_extractor.extract_assignments_from_html("1", "Bio", "Long", "long", html)
    dates = {r["due_date"] for r in rows}
    assert len(seen_chunks) >= 2, "long page must be processed in multiple chunks"
    assert "2026-08-10" in dates and "2026-11-20" in dates  # tail not truncated


def test_extractor_normalize_date_variants():
    n = page_extractor._normalize_date
    assert n("2026-08-17") == "2026-08-17"
    assert n("8/17") == "2026-08-17"
    assert n("8/17/26") == "2026-08-17"
    assert n("Aug 17") == "2026-08-17"
    assert n("August 17, 2025") == "2025-08-17"
    assert n("2026-13-40") is None
    assert n("garbage") is None
    assert n("") is None


def test_extractor_empty_html_returns_empty_list(monkeypatch):
    _disable_llm(monkeypatch)
    assert page_extractor.extract_assignments_from_html("1", "Bio", "P", "p", "") == []
    assert page_extractor.extract_assignments_from_html("1", "Bio", "P", "p", "<div></div>") == []

