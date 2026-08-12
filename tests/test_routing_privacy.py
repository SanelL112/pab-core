import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot.ai_bridge
from bot.ai_bridge import send_to_antigravity_and_wait
from llm_router import (
    InferenceResult,
    InferenceStatus,
    Sensitivity,
    call_local_rpc,
    call_local_rpc_result,
)


@pytest.fixture(autouse=True)
def disable_activity_logging(monkeypatch):
    """Routing tests must never append to the operator's activity feed."""
    monkeypatch.setattr(bot.ai_bridge, "log_llm_call", MagicMock())


async def _inline_to_thread(func, /, *args, **kwargs):
    """Run a mocked blocking dependency without creating an executor thread."""
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_ai_bridge_uses_typed_local_route():
    """Personal chat must use the typed local-only route by default."""
    with patch("bot.ai_bridge.load_state", return_value={"user_models": {}}), \
         patch("bot.ai_bridge.detect_topic", return_value="test_topic"), \
         patch(
             "bot.ai_bridge.call_local_rpc_result",
             return_value=InferenceResult.success(
                 "local response", provider="local-rpc", model="test-local"
             ),
         ) as mock_local, \
         patch("bot.ai_bridge.asyncio.to_thread", new=_inline_to_thread):
        out = await send_to_antigravity_and_wait("hello", 0, None, None)
        assert out.startswith("local response")
        kwargs = mock_local.call_args.kwargs
        assert "prompt" in kwargs
        assert kwargs["allow_cloud"] is False
        assert kwargs["sensitivity"] is Sensitivity.PERSONAL


def test_call_local_rpc_result_no_cloud():
    """An exhausted local chain yields a typed unavailable result, never cloud."""
    with patch("llm_router.call_llamacpp_rpc", return_value=""), \
         patch("llm_router.httpx.Client") as mock_client, \
         patch("llm_router.call_openrouter") as mock_openrouter:

         mock_client_instance = mock_client.return_value.__enter__.return_value
         mock_response = MagicMock()
         mock_response.status_code = 500
         mock_client_instance.post.return_value = mock_response

         result = call_local_rpc_result(prompt="test", allow_cloud=False)
         assert result.status is InferenceStatus.UNAVAILABLE
         assert result.text == ""
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

         res = call_local_rpc("test", allow_cloud=False)
         assert res == "dell_response"

@pytest.mark.asyncio
async def test_pii_fail_closed():
    """Even explicit cloud consent cannot override PII's local-only policy."""
    with patch("bot.ai_bridge.check_pii", return_value=(False, "scrubbed", ["phone"])), \
         patch("bot.ai_bridge.load_state", return_value={"user_models": {}}), \
         patch("bot.ai_bridge.detect_topic", return_value="test_topic"), \
         patch(
             "bot.ai_bridge.call_local_rpc_result",
             return_value=InferenceResult(InferenceStatus.UNAVAILABLE, provider="local-rpc"),
         ) as mock_local, \
         patch("bot.ai_bridge.call_openrouter_result") as mock_openrouter, \
         patch("bot.ai_bridge.asyncio.to_thread", new=_inline_to_thread):

         out = await send_to_antigravity_and_wait(
             "my phone is 555-1234", 0, None, None, cloud_consent=True
         )
         assert "unavailable" in out.lower()
         assert "not sent to a cloud provider" in out.lower()
         assert mock_local.call_args.kwargs["allow_cloud"] is False
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
