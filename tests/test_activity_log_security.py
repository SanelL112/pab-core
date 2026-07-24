import json
from unittest.mock import patch

import activity_log


def test_activity_log_redacts_free_text_and_queues_notifications(tmp_path, monkeypatch):
    log_path = tmp_path / "activity.jsonl"
    monkeypatch.setattr(activity_log, "LOG_PATH", str(log_path))

    with patch.object(activity_log, "_enqueue_telegram_notification") as enqueue:
        activity_log.log_event(
            "error",
            {
                "message": "student@example.com failed private homework",
                "error_type": "ValueError",
                "source": "ocr",
            },
            notify=True,
        )

    entry = json.loads(log_path.read_text())
    assert entry["details"]["message"] == "[REDACTED_UNSAFE_FIELD]"
    assert entry["details"]["error_type"] == "ValueError"
    assert "student@example.com" not in log_path.read_text()
    enqueue.assert_called_once()


def test_format_events_redacts_legacy_message_previews():
    rendered = activity_log.format_events([
        {"cat": "message", "time": "12:00:00", "details": {"preview": "private homework"}},
    ])

    assert "private homework" not in rendered
