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
