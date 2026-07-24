import asyncio
import sys
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot.ai_bridge
from bot.ai_bridge import send_to_antigravity_and_wait
from llm_router import call_local_rpc


def _inline_run_in_executor(loop, executor, func, *args):
    """Resolve mocked executor work synchronously in unit tests.

    These tests replace every blocking dependency with a mock.  Running those
    mocks in a real executor adds no coverage and is unreliable in the local
    Python 3.13 runner after more than one submission.
    """
    future = loop.create_future()
    try:
        future.set_result(func(*args))
    except Exception as exc:
        future.set_exception(exc)
    return future


@pytest.mark.asyncio
async def test_ai_bridge_kwargs():
    # We want to mock out _do_call, call_opencode, call_hackclub, etc.
    with patch("bot.ai_bridge.load_state", return_value={"user_models": {}}), \
         patch("bot.ai_bridge.detect_topic", return_value="test_topic"), \
         patch("bot.ai_bridge.subprocess.run") as mock_run, \
         patch("llm_router.call_openrouter", side_effect=Exception("OpenRouter failed")), \
         patch("llm_router.call_opencode") as mock_opencode, \
         patch("llm_router.call_hackclub") as mock_hackclub, \
         patch("asyncio.BaseEventLoop.run_in_executor", new=_inline_run_in_executor):

        # Simulate OpenRouter failure to trigger fallbacks
        mock_opencode.return_value = "opencode_response"

        # Test the normal flow OpenRouter failure -> Opencode
        out = await send_to_antigravity_and_wait("hello", 0, None, None)
        assert out.startswith("opencode_response")
        mock_opencode.assert_called_once()
        kwargs = mock_opencode.call_args.kwargs
        assert "prompt" in kwargs
        assert "model" in kwargs

@pytest.mark.parametrize("classification", ["PRIVATE", "unexpected", None])
def test_call_local_rpc_private_never_uses_cloud(classification):
    # PRIVATE classification must never invoke call_openrouter.
    with patch("llm_router.call_llamacpp_rpc", return_value=""), \
         patch("llm_router.httpx.Client") as mock_client, \
         patch("llm_router.call_openrouter") as mock_openrouter:

         mock_client_instance = mock_client.return_value.__enter__.return_value
         mock_response = MagicMock()
         mock_response.status_code = 500
         mock_client_instance.post.return_value = mock_response

         res = call_local_rpc("test", classification=classification)
         assert "local inference unavailable" in res.lower() or "local inference" in res.lower() or "local" in res.lower()
         mock_openrouter.assert_not_called()

def test_call_local_rpc_dell_fallback():
    # Test Dell fallback order: Surface, Pi, Dell
    with patch("llm_router.call_llamacpp_rpc", return_value=""), \
         patch("llm_router.httpx.Client") as mock_client:

         # Need to mock the post calls
         # First post is Pi, second is Dell
         mock_client_instance = mock_client.return_value.__enter__.return_value

         def side_effect_post(url, **kwargs):
             mock_resp = MagicMock()
             if "10.10.10.2" in url:
                 mock_resp.status_code = 500
             elif "127.0.0.1:11434" in url or "localhost" in url:
                 mock_resp.status_code = 200
                 mock_resp.json.return_value = {"response": "dell_response"}
             else:
                 mock_resp.status_code = 500
             return mock_resp

         mock_client_instance.post.side_effect = side_effect_post

         res = call_local_rpc("test", classification="PRIVATE")
         assert res == "dell_response"

@pytest.mark.asyncio
async def test_pii_fail_closed():
    # Test that when PII is present and local models fail, we fail closed (no cloud)
    with patch("utils.check_pii", return_value=(False, "scrubbed", ["phone"])), \
         patch("bot.ai_bridge.load_state", return_value={"user_models": {}}), \
         patch("llm_router.call_ollama", side_effect=Exception("Local Pi down")), \
         patch("llm_router.call_openrouter") as mock_openrouter, \
         patch("asyncio.BaseEventLoop.run_in_executor", new=_inline_run_in_executor):

         out = await send_to_antigravity_and_wait("my phone is 555-1234", 0, None, None)
         assert "unavailable" in out.lower()
         assert "cloud fallback is disabled" in out.lower()
         mock_openrouter.assert_not_called()

def test_prompt_no_arbitrary_bash():
    # Test that the system prompt does not contain 'python3 -c' or 'root access'
    import bot.ai_bridge
    # We can inspect the source code of ai_bridge or run a regex over the file
    import os
    bridge_path = bot.ai_bridge.__file__
    with open(bridge_path, "r") as f:
        content = f.read()
        assert "python3 -c" not in content
        assert "root access" not in content.lower()
