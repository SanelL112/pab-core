import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import log_scanner


def test_activity_log_scanner_supports_all_timestamp_formats(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    entries = [
        {"ts": now.timestamp(), "cat": "error", "details": {"kind": "numeric"}},
        {
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M:%S"),
            "cat": "critical",
            "details": {"kind": "date-time"},
        },
        {"timestamp": "not-a-date", "cat": "error", "details": {"kind": "invalid"}},
    ]
    (tmp_path / "activity_log.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n"
    )
    monkeypatch.setattr(log_scanner, "BASE_DIR", tmp_path)

    matches = log_scanner.scan_activity_log(hours=1)

    assert [match["message"] for match in matches] == [
        '[error] {"kind": "numeric"}',
        '[critical] {"kind": "date-time"}',
    ]
    assert matches[1]["timestamp"] == f"{now.date().isoformat()} {now.strftime('%H:%M:%S')}"


def test_activity_log_scanner_recognizes_structured_nightly_failures(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.timestamp(),
        "cat": "nightly",
        "details": {"phase": "indexing", "status": "failed"},
    }
    (tmp_path / "activity_log.jsonl").write_text(json.dumps(entry) + "\n")
    monkeypatch.setattr(log_scanner, "BASE_DIR", tmp_path)

    matches = log_scanner.scan_activity_log(hours=1)

    assert len(matches) == 1
    assert matches[0]["category"] == "NIGHTLY_FAIL"


def test_log_file_scanner_uses_configured_roots_and_all_matching_files(tmp_path, monkeypatch):
    import config

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "first.log").write_text("connection refused\n")
    (log_dir / "second.log").write_text("nightly failed\n")
    monkeypatch.setattr(config, "LOG_SCAN_DIRS", [log_dir])

    matches = log_scanner.scan_log_files(hours=1)

    assert {match["source"] for match in matches} == {"file:first.log", "file:second.log"}
    assert {match["category"] for match in matches} == {"NETWORK_ERR", "NIGHTLY_FAIL"}
