import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
import ai_processor
import bot.state
from bot.state import load_state, update_state
from llm_router import InferenceResult
from scrapers.mega_study_builder import DEFAULT_OUTLINE, TokenBudget, _valid_outline, generate_mega_guide
from scrapers.nightly_processor import run_nightly_job


async def _inline_to_thread(func, /, *args, **kwargs):
    """Keep the queue unit test hermetic while production offloads blocking I/O."""
    return func(*args, **kwargs)


def test_cache_dir_centralization():
    assert ai_processor.CACHE_DIR == config.CACHE_DIR
    assert config.COMBINED_SUMMARIES_FILE.parent == config.CACHE_DIR
    assert config.PDF_EXPORTS_FILE.parent == config.CACHE_DIR


@pytest.mark.asyncio
async def test_nightly_queue_failure_handling(tmp_path, monkeypatch):
    queue_file = tmp_path / "nightly_queue.json"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    queue_file.write_text(json.dumps([
        {"title": "Fail1", "file_id": "1", "filename": "fail.txt"},
        {"title": "Success2", "file_id": "2", "filename": "success.txt"},
    ]))

    monkeypatch.setattr(config, "NIGHTLY_QUEUE_FILE", queue_file)
    monkeypatch.setattr(config, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(config, "PDF_EXPORTS_FILE", cache_dir / "pdf_exports.txt")

    def download(file_id: str, destination: str) -> bool:
        if file_id == "1":
            return False
        Path(destination).write_text("useful extracted content " * 5)
        return True

    with patch("scrapers.nightly_processor.download_drive_file", side_effect=download) as mocked_download, \
         patch("scrapers.nightly_processor.asyncio.to_thread", new=_inline_to_thread):
        await run_nightly_job(bot=None, chat_id=424242)

    remaining = json.loads(queue_file.read_text())
    assert mocked_download.call_count == 2
    assert [item["file_id"] for item in remaining] == ["1"]
    assert remaining[0]["attempt_count"] == 1
    assert remaining[0]["last_error"] == "download failed"
    assert "Success2" in (cache_dir / "pdf_exports.txt").read_text()


def test_update_state_uses_the_patched_state_module_path(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"seen_tasks": []}))
    monkeypatch.setattr(config, "STATE_FILE", state_file)
    # Older releases cached STATE_FILE in bot.state; current releases resolve
    # config.STATE_FILE dynamically.  Set both so the test cannot hit live state
    # across either side of that migration.
    monkeypatch.setattr(bot.state, "STATE_FILE", state_file, raising=False)

    def mutator(state):
        state["seen_tasks"].append("123")

    update_state(mutator)
    state = load_state()
    assert "123" in state["seen_tasks"]
    assert json.loads(state_file.read_text())["seen_tasks"] == ["123"]


def test_study_builder_keeps_private_context_local_and_bounds_its_plan(monkeypatch):
    """Guide generation must never fall back to a cloud model or unbound chapters."""
    assert _valid_outline(list(DEFAULT_OUTLINE)) == list(DEFAULT_OUTLINE)
    assert _valid_outline(list(DEFAULT_OUTLINE) + ["Chapter 11: Overflow"]) is None
    assert _valid_outline(["Practice"] * len(DEFAULT_OUTLINE)) is None

    budget = TokenBudget(limit=10)
    assert budget.reserve(7)
    assert not budget.reserve(4)
    assert budget.used == 7

    def local_result(**kwargs):
        if "raw JSON array" in kwargs["prompt"]:
            text = json.dumps(DEFAULT_OUTLINE)
        else:
            text = "# Local chapter\n" + ("useful study material " * 10)
        return InferenceResult.success(text, provider="local-rpc", model="test-local")

    monkeypatch.setattr("scrapers.mega_study_builder.search_youtube", lambda _topic: (None, ""))
    monkeypatch.setattr("scrapers.mega_study_builder.search_web_article", lambda _topic: (None, ""))
    monkeypatch.setattr("scrapers.mega_study_builder.search_images", lambda _topic: [])
    with patch("llm_router.call_local_rpc_result", side_effect=local_result) as mock_local:
        result = generate_mega_guide("Algebra")

    assert "Generated dynamically via a 10-part" in result
    assert mock_local.call_count == 21
    for call in mock_local.call_args_list:
        assert call.kwargs["allow_cloud"] is False
