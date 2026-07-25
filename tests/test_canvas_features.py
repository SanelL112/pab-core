from datetime import datetime, timedelta, timezone

from scrapers.canvas_scraper import (
    _assignment_is_actionable,
    _get_upcoming_assignments,
    _is_recent_canvas_update,
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
