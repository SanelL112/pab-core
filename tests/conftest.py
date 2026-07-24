"""Shared test configuration that keeps the suite independent of a local .env."""

import os


_TEST_ENV = {
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "TELEGRAM_BOT_TOKEN": "test-telegram-token",
    "TELEGRAM_CHAT_ID": "1",
    "CONVERSATION_ID": "test-conversation",
}

for _name, _value in _TEST_ENV.items():
    os.environ[_name] = _value  # OVERRIDE any real credentials from environment

import pytest
import socket

_SKIPPED_REPORTS = []


def pytest_addoption(parser):
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="treat skipped tests as a failing test run",
    )


def pytest_runtest_logreport(report):
    if report.skipped:
        _SKIPPED_REPORTS.append(report)


def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption("--fail-on-skip") and exitstatus == 0 and _SKIPPED_REPORTS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

@pytest.fixture(autouse=True)
def disable_network_calls(monkeypatch):
    """
    Block all network requests in tests by default.
    Tests that need network should explicitly mock the required modules.
    """
    def block_network(*args, **kwargs):
        raise RuntimeError("Network calls are disabled in tests!")
    
    # Preserve Unix-domain sockets: asyncio uses socketpair() for its local
    # event-loop wakeup pipe.  Internet socket families remain blocked.
    original_socket = socket.socket

    def guarded_socket(*args, **kwargs):
        family = kwargs.get("family", args[0] if args else socket.AF_INET)
        if family in (socket.AF_INET, socket.AF_INET6):
            return block_network(*args, **kwargs)
        return original_socket(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", guarded_socket)
    monkeypatch.setattr(socket, "create_connection", block_network)
    
    # Block requests/httpx if they try to bypass or are already imported
    try:
        import requests
        monkeypatch.setattr(requests, "get", block_network)
        monkeypatch.setattr(requests, "post", block_network)
        monkeypatch.setattr(requests.Session, "request", block_network)
        monkeypatch.setattr(requests.Session, "send", block_network)
    except ImportError:
        pass
        
    try:
        import httpx
        monkeypatch.setattr(httpx, "get", block_network)
        monkeypatch.setattr(httpx, "post", block_network)
        monkeypatch.setattr(httpx.Client, "request", block_network)
        monkeypatch.setattr(httpx.AsyncClient, "request", block_network)
        monkeypatch.setattr(httpx.Client, "send", block_network)
        monkeypatch.setattr(httpx.AsyncClient, "send", block_network)
    except ImportError:
        pass
