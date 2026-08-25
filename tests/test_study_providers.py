"""Provider chain: order, PII gate, and fall-through behavior."""
import json

import pytest

from scrapers import study_providers as sp


@pytest.fixture(autouse=True)
def _reset_provider_health():
    sp._FAILURES.clear()
    yield
    sp._FAILURES.clear()


def test_chain_order_and_fallthrough(monkeypatch):
    calls = []

    def make(name, text):
        def runner(prompt, system, max_tokens, timeout):
            calls.append(name)
            return text
        return runner

    monkeypatch.setattr(sp, "_PROVIDERS", {
        "openrouter": make("openrouter", ""),           # empty -> skip
        "opencode": make("opencode", "   "),            # blank -> skip
        "agy": make("agy", "chapter text"),
        "hackclub": make("hackclub", "should not run"),
    })
    text, provider = sp.generate_online("clean prompt", max_tokens=100)
    assert (text, provider) == ("chapter text", "agy")
    assert calls == ["openrouter", "opencode", "agy"]  # hackclub never called


def test_pii_block_refuses_send_when_residual_pii(monkeypatch):
    import utils

    sent = []

    def runner(prompt, system, max_tokens, timeout):
        sent.append(prompt)
        return "leaked"

    monkeypatch.setattr(sp, "_PROVIDERS", {"openrouter": runner})
    monkeypatch.setattr(utils, "check_pii", lambda t: (False, t, ["email"]))
    text, provider = sp.generate_online("contains residual pii", max_tokens=50)
    assert text == "" and provider == "pii-blocked" and not sent


def test_scrubbed_prompt_reaches_provider(monkeypatch):
    seen = {}

    def runner(prompt, system, max_tokens, timeout):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(sp, "_PROVIDERS", {"openrouter": runner})
    _, provider = sp.generate_online("email me at sanel@example.com please", max_tokens=50)
    assert provider == "openrouter"
    assert "sanel@example.com" not in seen["prompt"]
