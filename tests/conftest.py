"""Session-wide safety boundaries for the test suite.

This file is imported before test collection, so application modules only ever
see disposable configuration roots and dummy credentials.  Accidental network
or process access fails loudly; individual tests may replace a specific call
with a mock when that boundary is part of the behavior under test.
"""
from __future__ import annotations

import asyncio
import atexit
import builtins
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="pab-pytest-", dir="/tmp")).resolve()


def _make_private_dir(name: str) -> Path:
    path = TEST_RUNTIME_ROOT / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


_config_root = _make_private_dir("config")
_runtime_root = _make_private_dir("runtime")
_state_root = _make_private_dir("state")
_log_root = _make_private_dir("logs")
_export_root = _make_private_dir("exports")
_xdg_root = _make_private_dir("xdg")

# Override rather than default these values: a developer shell may contain real
# credentials, and no test should inherit them.
_TEST_ENV = {
    "PAB_CONFIG_DIR": str(_config_root),
    "PAB_RUNTIME_DIR": str(_runtime_root),
    "PAB_STATE_DIR": str(_state_root),
    "PAB_LOG_DIR": str(_log_root),
    "PAB_EXPORT_DIR": str(_export_root),
    "PAB_ENV_FILE": str(_config_root / "intentionally-absent.env"),
    "ASSIGNMENT_CALENDAR_DATA_DIR": str(_runtime_root / "assignment-calendar"),
    "ORANGEPI_CACHE_DIR": str(_runtime_root / "orangepi-cache"),
    "ORANGEPI_BACKUP_DIR": str(_runtime_root / "orangepi-backups"),
    "XDG_CACHE_HOME": str(_xdg_root / "cache"),
    "XDG_CONFIG_HOME": str(_xdg_root / "config"),
    "XDG_DATA_HOME": str(_xdg_root / "data"),
    "XDG_STATE_HOME": str(_xdg_root / "state"),
    "PYTHON_DOTENV_DISABLED": "1",
    "TELEGRAM_BOT_TOKEN": "pytest-telegram-token",
    "TELEGRAM_CHAT_ID": "424242",
    "TELEGRAM_OWNER_USER_ID": "424242",
    "CONVERSATION_ID": "pytest-conversation",
    "OPENROUTER_API_KEY": "pytest-openrouter-key",
    "NOTION_API_KEY": "",
    "NOTION_DATABASE_ID": "",
    "GROUPME_TOKEN": "",
    "GROUPME_GROUP_ID": "",
    "USE_COMPOSIO": "false",
}
os.environ.update(_TEST_ENV)
sys.dont_write_bytecode = True

# Some legacy modules still call load_dotenv() directly.  Disable both dotenv
# entry points before any test module imports application code.
import dotenv  # noqa: E402


def _empty_dotenv(*_args: Any, **_kwargs: Any) -> bool:
    return False


dotenv.load_dotenv = _empty_dotenv
dotenv.dotenv_values = lambda *_args, **_kwargs: {}


class HermeticityError(RuntimeError):
    """Raised when a unit test attempts an undeclared external side effect."""


def _blocked_process(*_args: Any, **_kwargs: Any) -> Any:
    raise HermeticityError("subprocess execution is disabled in tests; mock the exact call")


def _blocked_socket(*_args: Any, **_kwargs: Any) -> Any:
    raise HermeticityError("socket access is disabled in tests; mock the exact client call")


# Install these guards before collection.  unittest.mock.patch still works on
# each symbol and is the required opt-in for a test exercising an integration.
class _BlockedPopen:
    """Popen-shaped guard that remains valid in runtime type annotations."""

    def __class_getitem__(cls, _item: Any) -> type["_BlockedPopen"]:
        return cls

    def __new__(cls, *_args: Any, **_kwargs: Any) -> "_BlockedPopen":
        raise HermeticityError("subprocess execution is disabled in tests; mock the exact call")


subprocess.Popen = _BlockedPopen
for _name in ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
    setattr(subprocess, _name, _blocked_process)
asyncio.create_subprocess_exec = _blocked_process
asyncio.create_subprocess_shell = _blocked_process
os.system = _blocked_process
os.popen = _blocked_process

socket.create_connection = _blocked_socket
socket.socket.connect = _blocked_socket
socket.socket.connect_ex = _blocked_socket
socket.socket.sendto = _blocked_socket


_PROTECTED_FILES = {
    (PROJECT_ROOT / name).resolve()
    for name in (
        ".env",
        "credentials.json",
        "token.json",
        "state.json",
        "activity_log.jsonl",
        "latest_digest.txt",
        "curated_brain.md",
        "mega_index.md",
        "bot_context.txt",
        "nightly_queue.json",
        "llm_cost_log.json",
        "correlation_graph.json",
    )
}
_PROTECTED_DIRS = tuple(
    (PROJECT_ROOT / name).resolve()
    for name in ("backups", "cache", "embedding_data", "offline_archive", "source_cache", "study_guides")
)
_real_builtin_open = builtins.open
_real_io_open = io.open


def _protected_runtime_path(value: Any) -> bool:
    if isinstance(value, int):
        return False
    try:
        path = Path(os.fspath(value)).expanduser().resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    return path in _PROTECTED_FILES or any(path == root or root in path.parents for root in _PROTECTED_DIRS)


def _guarded_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
    if _protected_runtime_path(file):
        raise HermeticityError(f"access to real runtime path is disabled: {file}")
    return _real_builtin_open(file, *args, **kwargs)


def _guarded_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
    if _protected_runtime_path(file):
        raise HermeticityError(f"access to real runtime path is disabled: {file}")
    return _real_io_open(file, *args, **kwargs)


builtins.open = _guarded_builtin_open
io.open = _guarded_io_open


@pytest.fixture(scope="session")
def test_runtime_root() -> Path:
    """Return the disposable root used by application configuration."""
    return TEST_RUNTIME_ROOT


@pytest.fixture(autouse=True)
def isolate_activity_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route legacy activity logging to the disposable runtime for every test."""
    import activity_log

    log_path = _log_root / "activity_log.jsonl"
    monkeypatch.setattr(activity_log, "BASE_DIR", str(_log_root), raising=False)
    monkeypatch.setattr(activity_log, "LOG_PATH", str(log_path), raising=False)
    monkeypatch.setattr(
        activity_log,
        "_send_telegram_notification",
        lambda _text: None,
        raising=False,
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Make silent dependency/import skips a CI failure."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter is not None else []
    if skipped:
        if reporter is not None:
            reporter.write_sep("!", f"{len(skipped)} skipped test(s) are not allowed in the hermetic suite")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _cleanup_runtime() -> None:
    shutil.rmtree(TEST_RUNTIME_ROOT, ignore_errors=True)


atexit.register(_cleanup_runtime)
