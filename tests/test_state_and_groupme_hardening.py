"""Regression tests for the two silent-failure fixes.

1. ``seen_tasks`` is fuzzy-matched in full on every digest, so unbounded growth
   both costs O(n) and eventually causes a legitimate new task to collide with
   an ancient title and be dropped without a log line.
2. GroupMe returned a bare ``HTTPError`` for an expired token, which is
   indistinguishable from a transient outage in the logs.
"""
from __future__ import annotations

import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import state as state_module
from bot.state import MAX_SEEN_ALERTS, MAX_SEEN_TASKS, _normalize_state
from scrapers import groupme_scraper


# ── seen_tasks bounding ──────────────────────────────────────────────────────


def test_seen_tasks_is_capped_to_the_most_recent_window():
    raw = {"seen_tasks": [f"task {i}" for i in range(MAX_SEEN_TASKS + 250)]}
    result = _normalize_state(raw)["seen_tasks"]
    assert len(result) == MAX_SEEN_TASKS
    # The newest entries are the ones that matter for dedupe; oldest are dropped.
    assert result[-1] == f"task {MAX_SEEN_TASKS + 249}"
    assert "task 0" not in result


def test_legacy_md5_digests_are_discarded():
    """A 32-char hex digest can never fuzzy-match a real title, so it is dead weight."""
    raw = {"seen_tasks": ["a" * 32, "0123456789abcdef" * 2, "real task title"]}
    assert _normalize_state(raw)["seen_tasks"] == ["real task title"]


def test_sha256_digests_are_also_discarded():
    raw = {"seen_tasks": ["b" * 64, "keep me"]}
    assert _normalize_state(raw)["seen_tasks"] == ["keep me"]


def test_non_string_and_blank_entries_are_dropped():
    raw = {"seen_tasks": ["keep", "", "   ", None, 42, {"a": 1}, ["x"]]}
    assert _normalize_state(raw)["seen_tasks"] == ["keep"]


def test_entries_are_stripped_but_content_preserved():
    raw = {"seen_tasks": ["  physics homework  "]}
    assert _normalize_state(raw)["seen_tasks"] == ["physics homework"]


def test_seen_alerts_are_bounded_but_keep_their_hashes():
    """Alert keys are intentionally digests, so only growth is bounded."""
    digests = [f"{i:032x}" for i in range(MAX_SEEN_ALERTS + 40)]
    result = _normalize_state({"seen_alerts": digests})["seen_alerts"]
    assert len(result) == MAX_SEEN_ALERTS
    assert result[-1] == digests[-1]


def test_normalization_is_idempotent():
    raw = {"seen_tasks": [f"task {i}" for i in range(MAX_SEEN_TASKS + 10)] + ["c" * 32]}
    once = _normalize_state(raw)
    twice = _normalize_state(once)
    assert once["seen_tasks"] == twice["seen_tasks"]


def test_unrelated_keys_survive_normalization():
    raw = {"seen_tasks": ["a"], "custom_key": "value", "user_models": {"1": "m"}}
    result = _normalize_state(raw)
    assert result["custom_key"] == "value"
    assert result["user_models"] == {"1": "m"}


def test_non_list_seen_tasks_is_reset_not_crashed():
    assert _normalize_state({"seen_tasks": "not-a-list"})["seen_tasks"] == []


def test_state_root_must_be_an_object():
    with pytest.raises(ValueError):
        _normalize_state(["not", "a", "dict"])


def test_capped_state_round_trips_through_the_store(tmp_path, monkeypatch):
    """The cap must be applied on the durable write path, not just in memory."""
    import config

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(config, "STATE_FILE", state_file)

    overflow = [f"task {i}" for i in range(MAX_SEEN_TASKS + 100)] + ["d" * 32]
    state_module.save_state({"seen_tasks": overflow})
    reloaded = state_module.load_state()

    assert len(reloaded["seen_tasks"]) == MAX_SEEN_TASKS
    assert not any(len(entry) == 32 and all(c in "0123456789abcdef" for c in entry)
                   for entry in reloaded["seen_tasks"])


# ── GroupMe failure diagnosis ────────────────────────────────────────────────


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"HTTP {status}")
    error.response = response
    return error


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_logged_as_an_error_with_the_status(status, caplog):
    with caplog.at_level("ERROR"):
        message = groupme_scraper._describe_failure(_http_error(status), "messages")
    assert "Regenerate it" in message
    assert str(status) in caplog.text
    assert "expired" in caplog.text


def test_server_error_reports_the_status_but_stays_a_warning(caplog):
    with caplog.at_level("WARNING"):
        message = groupme_scraper._describe_failure(_http_error(500), "messages")
    assert message == "GroupMe is temporarily unavailable."
    assert "500" in caplog.text
    assert "Regenerate" not in message


def test_transport_error_without_a_response_still_names_the_exception(caplog):
    with caplog.at_level("WARNING"):
        message = groupme_scraper._describe_failure(requests.Timeout("slow"), "messages")
    assert message == "GroupMe is temporarily unavailable."
    assert "Timeout" in caplog.text


@pytest.mark.parametrize("token", ["", "   ", "your_groupme_token", "YOUR_GROUPME_TOKEN", "changeme"])
def test_placeholder_and_empty_tokens_are_treated_as_unconfigured(token, monkeypatch):
    """An unedited .env copy yields a non-empty token that 401s on every call."""
    import config

    monkeypatch.setattr(config, "GROUPME_TOKEN", token)
    assert groupme_scraper._access_token() is None


def test_real_token_is_returned_stripped(monkeypatch):
    import config

    monkeypatch.setattr(config, "GROUPME_TOKEN", "  abc123realtoken  ")
    assert groupme_scraper._access_token() == "abc123realtoken"


def test_unconfigured_token_short_circuits_before_any_network_call(monkeypatch):
    import config

    monkeypatch.setattr(config, "GROUPME_TOKEN", "your_groupme_token")

    def fail(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no HTTP request should be attempted without a token")

    monkeypatch.setattr(groupme_scraper.requests, "get", fail)

    for result in (groupme_scraper.get_latest_messages("102851186"),
                   groupme_scraper.get_groups()):
        assert "not configured" in result
        assert "dev.groupme.com" in result


def test_expired_token_surfaces_actionable_text_to_the_caller(monkeypatch):
    import config

    monkeypatch.setattr(config, "GROUPME_TOKEN", "expired-but-real-looking")

    response = requests.Response()
    response.status_code = 401
    monkeypatch.setattr(groupme_scraper.requests, "get", lambda *a, **k: response)

    result = groupme_scraper.get_latest_messages("102851186")
    assert "401" in result
    assert "GROUPME_ACCESS_TOKEN" in result


def test_invalid_group_id_is_still_rejected_when_a_token_exists(monkeypatch):
    import config

    monkeypatch.setattr(config, "GROUPME_TOKEN", "real-looking-token")
    assert groupme_scraper.get_latest_messages("not-a-number") == "Invalid GroupMe group ID."
