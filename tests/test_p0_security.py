import os
import sys
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# ---------------------------------------------------------
# Test Scope 1: utils.py Python execution policy
# ---------------------------------------------------------
from utils import _is_command_allowed, run_bash_safely

def test_python_execution_policy():
    allowed, _ = _is_command_allowed("python3 -c 'print(1)'")
    assert not allowed, "python3 -c execution should be blocked"


@pytest.mark.parametrize(
    "command",
    [
        "find /tmp -name '*.txt' -exec id \\;",
        "tar -tzf archive.tar.gz --checkpoint=1",
        "awk 'BEGIN { system(\"id\") }' /tmp/input.txt",
    ],
)
def test_command_templates_reject_trailing_or_script_arguments(command):
    allowed, _ = _is_command_allowed(command)
    assert not allowed

def test_python_bash_tag_execution_behavior():
    with patch("utils.subprocess.run") as mock_run:
        out = run_bash_safely("python3 -c 'print(1)'", chat_id=123)
        mock_run.assert_not_called()
        assert "not in allowlist" in out.lower() or "blocked" in out.lower() or "error" in out.lower() or "not allowed" in out.lower()

def test_path_containment_in_bash(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SAFE_BASH_ROOTS", [str(tmp_path)])

    allowed_file = tmp_path / "allowed.txt"
    allowed_file.write_text("safe")
    allowed, reason = _is_command_allowed(f"cat {allowed_file}")
    assert allowed is True, f"Should allow cat inside safe root: {reason}"

    # Outside safe roots
    allowed, reason = _is_command_allowed("cat /etc/passwd")
    assert allowed is False
    assert "outside safe roots" in reason.lower()

    # Traverse outside
    allowed, reason = _is_command_allowed(f"cat {tmp_path}/../../../../etc/passwd")
    assert allowed is False
    assert "outside safe roots" in reason.lower()

    for system_path in ("/proc/self/environ", "/sys/kernel", "/dev/null"):
        allowed, reason = _is_command_allowed(f"cat {system_path}")
        assert allowed is False
        assert "system device/proc" in reason.lower()

    for sensitive_name in (".env", "credentials.json", "state.json", "app_token.txt"):
        sensitive_file = tmp_path / sensitive_name
        sensitive_file.write_text("private")
        allowed, reason = _is_command_allowed(f"cat {sensitive_file}")
        assert allowed is False
        assert "sensitive file" in reason.lower()

    linked_file = tmp_path / "outside-link"
    linked_file.symlink_to("/etc/passwd")
    allowed, reason = _is_command_allowed(f"cat {linked_file}")
    assert allowed is False
    assert "symlink" in reason.lower()

# ---------------------------------------------------------
# Test Scope 2: Telegram Authentication Security
# ---------------------------------------------------------
from bot.security import require_auth
from config import SANEL_CHAT_ID

@pytest.mark.asyncio
async def test_require_auth_fails_closed_without_update():
    mock_func = AsyncMock(return_value="success")
    decorated = require_auth(mock_func)

    result = await decorated(None, MagicMock())  # type: ignore[arg-type]

    assert result is None
    mock_func.assert_not_called()


@pytest.mark.asyncio
async def test_require_auth_fails_closed_unresolved_update():
    mock_func = AsyncMock(return_value="success")
    decorated = require_auth(mock_func)

    update = MagicMock()
    update.effective_chat = None
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    result = await decorated(update, MagicMock())
    assert result is None
    update.message.reply_text.assert_called()
    mock_func.assert_not_called()

@pytest.mark.asyncio
async def test_main_start_schedules_owner_chat():
    from main import start
    update = MagicMock()
    update.effective_chat.id = SANEL_CHAT_ID
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.job_queue.run_repeating = MagicMock()

    await start(update, context)

    context.job_queue.run_repeating.assert_called()
    assert context.job_queue.run_repeating.call_args[1].get('chat_id') == SANEL_CHAT_ID

# ---------------------------------------------------------
# Test Scope 3: Cluster Manager API Security
# ---------------------------------------------------------
from surface.cluster_manager import Handler

def create_mock_handler(path="/api/status", headers=None, body=None):
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.headers = headers or {}
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.rfile = MagicMock()
    if body:
        handler.rfile.read.return_value = json.dumps(body).encode('utf-8')
    return handler

def test_api_root_reachable_without_auth():
    handler = create_mock_handler(path="/")
    handler.do_GET()
    handler.send_response.assert_called_with(200)

def test_api_rejects_when_server_token_is_unset(monkeypatch):
    monkeypatch.delenv("CLUSTER_MANAGER_API_TOKEN", raising=False)
    handler = create_mock_handler(headers={"Authorization": "Bearer any-value"})

    handler.do_GET()

    handler.send_response.assert_called_with(401)


def test_api_security_unauthorized_and_valid_auth(monkeypatch):
    monkeypatch.setenv('CLUSTER_MANAGER_API_TOKEN', 'valid_token')

    # Unauthorized (no token)
    handler = create_mock_handler()
    handler.do_GET()
    handler.send_response.assert_called_with(401)
    handler.send_header.assert_any_call('Content-type', 'application/json')

    # Unauthorized (wrong token)
    handler = create_mock_handler(headers={'Authorization': 'Bearer wrong_token'})
    handler.do_GET()
    handler.send_response.assert_called_with(401)

    # Valid auth
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_res = MagicMock()
        mock_res.status = 200
        mock_urlopen.return_value = mock_res
        handler = create_mock_handler(headers={'Authorization': 'Bearer valid_token'})
        handler.do_GET()
        handler.send_response.assert_called_with(200)

def test_api_security_download_traversal_and_non_hf(monkeypatch):
    monkeypatch.setenv('CLUSTER_MANAGER_API_TOKEN', 'valid_token')
    headers = {'content-length': '100', 'Authorization': 'Bearer valid_token'}

    # Traversal reject
    handler = create_mock_handler(path="/api/download", headers=headers, body={
        "url": "https://huggingface.co/model/resolve/main/model.gguf",
        "filename": "../model.gguf"
    })
    handler.do_POST()
    handler.send_response.assert_called_with(400)

    # Non-HF reject
    handler = create_mock_handler(path="/api/download", headers=headers, body={
        "url": "http://malicious.com/model.gguf",
        "filename": "model.gguf"
    })
    handler.do_POST()
    handler.send_response.assert_called_with(400)

def test_api_security_download_valid_accepts(monkeypatch):
    monkeypatch.setenv('CLUSTER_MANAGER_API_TOKEN', 'valid_token')
    headers = {'content-length': '100', 'Authorization': 'Bearer valid_token'}

    handler = create_mock_handler(path="/api/download", headers=headers, body={
        "url": "https://huggingface.co/model/resolve/main/model.gguf",
        "filename": "model.gguf"
    })
    with patch("subprocess.Popen") as mock_popen:
        handler.do_POST()
        handler.send_response.assert_called_with(200)
        mock_popen.assert_called_once()
