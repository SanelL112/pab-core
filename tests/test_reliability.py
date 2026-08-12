import os
import sys
import json
import asyncio
import threading
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import ai_processor
from llm_router import InferenceResult
from bot.state import load_state, update_state
from scrapers.nightly_processor import run_nightly_job
from scrapers.embedding_indexer import collect_sources
import main


async def _inline_to_thread(func, /, *args, **kwargs):
    """Run already-mocked callables inline to keep this unit test hermetic.

    The production handler uses ``asyncio.to_thread`` for blocking I/O.  The
    test mocks every such dependency, so creating worker threads adds no
    coverage and can leave executor threads behind in the Python 3.13 test
    runtime.
    """
    return func(*args, **kwargs)

@pytest.fixture(autouse=True)
def isolate_globals(tmp_path):
    orig_state = config.STATE_FILE
    config.STATE_FILE = tmp_path / "state.json"
    config.STATE_FILE.write_text(json.dumps({"seen_tasks": []}))
    import bot.state
    bot.state.STATE_FILE = config.STATE_FILE

    orig_cache = config.CACHE_DIR
    config.CACHE_DIR = tmp_path / "cache"
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ai_processor.CACHE_DIR = config.CACHE_DIR

    yield

    config.STATE_FILE = orig_state
    bot.state.STATE_FILE = orig_state
    config.CACHE_DIR = orig_cache
    ai_processor.CACHE_DIR = orig_cache

def test_update_state_concurrent(tmp_path):
    # Deterministic thread test for concurrent mutators
    barrier = threading.Barrier(2)

    def worker1():
        barrier.wait()
        def mutator1(s):
            s["key1"] = "val1"
        update_state(mutator1)

    def worker2():
        barrier.wait()
        def mutator2(s):
            s["key2"] = "val2"
        update_state(mutator2)

    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    state = load_state()
    assert state.get("key1") == "val1"
    assert state.get("key2") == "val2"
    assert "seen_tasks" in state

@pytest.mark.asyncio
async def test_nightly_queue_processor(tmp_path):
    queue_file = tmp_path / "nightly_queue.json"
    config.NIGHTLY_QUEUE_FILE = queue_file
    queue_file.write_text(json.dumps([
        {"title": "File1", "file_id": "id1", "filename": "file1.txt"},
        {"title": "File2", "file_id": "id2", "filename": "file2.txt"}
    ]))

    def mock_download(file_id, path):
        if file_id != "id2":
            return False
        Path(path).write_text("dummy text " * 10, encoding="utf-8")
        return True

    with patch("scrapers.nightly_processor.download_drive_file", side_effect=mock_download), \
         patch("scrapers.nightly_processor.asyncio.to_thread", new=_inline_to_thread):
        bot_mock = MagicMock()
        await run_nightly_job(bot_mock, 123)

    with open(queue_file, "r") as f:
        queue = json.load(f)
    assert len(queue) == 1
    assert queue[0]["file_id"] == "id1"
    assert "attempt_count" in queue[0]

def test_ai_processor_cache_consumer():
    # Behavioral test for DATA-01 cache directory usage
    data = "test content"
    with patch("llm_router.call_local_rpc_result", return_value=InferenceResult.success("summary", provider="local-rpc", model="test")) as mock_local, \
         patch("ai_processor.pi_classify_sync", return_value=None):
        ai_processor.process_source("test_src", data)

    cache_path = config.CACHE_DIR / "test_src_summary.txt"
    assert cache_path.exists()
    assert cache_path.read_text() == "summary"

def test_has_changed_suffix_detection():
    # Behavioral test for DATA-03: proving suffix-only changes after 1000 chars are detected
    data1 = "a" * 1500 + "suffix1"
    data2 = "a" * 1500 + "suffix2"

    with patch("llm_router.call_local_rpc_result", return_value=InferenceResult.success("summary1", provider="local-rpc", model="test")) as mock_local, \
         patch("ai_processor.pi_classify_sync", return_value=None):
        ai_processor.process_source("test_src2", data1)
        assert mock_local.call_count == 1

        # Second call with suffix2 should trigger re-processing since suffix changed
        mock_local.return_value = InferenceResult.success("summary2", provider="local-rpc", model="test")
        ai_processor.process_source("test_src2", data2)
        assert mock_local.call_count == 2

        cache_path = config.CACHE_DIR / "test_src2_summary.txt"
        assert cache_path.read_text() == "summary2"

def test_nightly_import_side_effects():
    # Behavioral test for DATA-04: import nightly_processor without side effects
    import importlib
    import scrapers.nightly_processor as nightly_processor

    with patch("logging.getLogger"):
        reloaded = importlib.reload(nightly_processor)
        # No side effects like os.system or exit should happen during import
        assert hasattr(reloaded, "run_nightly_job")

@pytest.mark.asyncio
async def test_notion_failure_behavior():
    # Behavioral test for Notion failure
    context = MagicMock()
    context.job.chat_id = 123
    context.bot.send_message = AsyncMock()

    # Mock AI response to return a new task
    ai_result = {
        "tasks": [{"title": "test task", "source": "test", "due_date": None}],
        "digest": "test digest",
        "topics": []
    }

    # Run with Notion failure (returns False)
    with patch("ai_processor.process_all_sources", return_value=ai_result) as mock_process, \
         patch("scrapers.notion_client.add_task_to_notion", return_value=False) as mock_notion, \
         patch("main.USE_COMPOSIO", False), \
         patch("main.is_sleep_window", return_value=False, create=True), \
         patch("scrapers.canvas_scraper.get_all_canvas_data", return_value=""), \
         patch("scrapers.google_scraper.get_classroom_assignments", return_value=""), \
         patch("scrapers.google_scraper.get_classroom_announcements", return_value=""), \
         patch("scrapers.google_scraper.get_unread_emails", return_value=""), \
         patch("scrapers.groupme_scraper.get_latest_messages", return_value=""), \
         patch("scrapers.google_scraper.get_recent_google_docs", return_value=""), \
         patch("main.asyncio.to_thread", side_effect=_inline_to_thread), \
         patch("main.correlate_items", return_value={}), \
         patch("main.open", mock_open(), create=True):

        await main._check_updates_impl(context)

        assert mock_process.call_count == 1, "process_all_sources was not called!"
        mock_notion.assert_called_once()
        state = load_state()
        assert "test task" not in state.get("seen_tasks", [])

    # Run again with Notion success (returns truthy page id)
    ai_result2 = {
        "tasks": [{"title": "test task 2", "source": "test", "due_date": None}],
        "digest": "test digest 2",
        "topics": []
    }
    with patch("ai_processor.process_all_sources", return_value=ai_result2), \
         patch("scrapers.notion_client.add_task_to_notion", return_value="page_id_123") as mock_notion2, \
         patch("main.USE_COMPOSIO", False), \
         patch("main.is_sleep_window", return_value=False, create=True), \
         patch("scrapers.canvas_scraper.get_all_canvas_data", return_value=""), \
         patch("scrapers.google_scraper.get_classroom_assignments", return_value=""), \
         patch("scrapers.google_scraper.get_classroom_announcements", return_value=""), \
         patch("scrapers.google_scraper.get_unread_emails", return_value=""), \
         patch("scrapers.groupme_scraper.get_latest_messages", return_value=""), \
         patch("scrapers.google_scraper.get_recent_google_docs", return_value=""), \
         patch("main.asyncio.to_thread", side_effect=_inline_to_thread), \
         patch("main.correlate_items", return_value={}), \
         patch("main.open", mock_open(), create=True):

        await main._check_updates_impl(context)

        mock_notion2.assert_called_once()
        state = load_state()
        assert "test task 2" in state.get("seen_tasks", [])
