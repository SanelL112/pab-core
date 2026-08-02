"""Tests for the RPC inference budget split.

``call_local_rpc`` runs one shared deadline across Surface -> Pi Ollama -> Dell
Ollama.  Whatever the Surface attempt consumes is gone, and ``_try_ollama``
returns immediately when ``remaining() <= 0``.  A Surface cap equal to the full
budget therefore silently disables both fallbacks: the call burns the entire
budget, returns "", and the digest degrades to the deterministic formatter.

These tests pin the invariant that the cap always leaves usable budget behind.
"""
from __future__ import annotations

import importlib

import config


class TestSurfaceTimeoutInvariant:
    def test_cap_is_below_the_full_budget(self):
        assert config.RPC_SURFACE_TIMEOUT < config.RPC_INFERENCE_TIMEOUT

    def test_cap_leaves_room_for_a_fallback_node(self):
        headroom = config.RPC_INFERENCE_TIMEOUT - config.RPC_SURFACE_TIMEOUT
        assert headroom >= 60, "a fallback node needs budget left to answer"

    def test_default_is_large_enough_for_a_measured_digest(self):
        # Cold-cache digest runs measured on the cluster: 108s, 127s, 148s, 438s.
        assert config.RPC_SURFACE_TIMEOUT >= 440

    def test_env_override_is_clamped(self, monkeypatch):
        # An operator setting the cap to the full budget must not starve fallbacks.
        monkeypatch.setenv("RPC_SURFACE_TIMEOUT", "600")
        monkeypatch.setenv("RPC_INFERENCE_TIMEOUT", "600")
        reloaded = importlib.reload(config)
        try:
            assert reloaded.RPC_SURFACE_TIMEOUT <= 540
            assert reloaded.RPC_SURFACE_TIMEOUT < reloaded.RPC_INFERENCE_TIMEOUT
        finally:
            monkeypatch.undo()
            importlib.reload(config)

    def test_absurd_override_still_leaves_a_positive_cap(self, monkeypatch):
        monkeypatch.setenv("RPC_SURFACE_TIMEOUT", "99999")
        reloaded = importlib.reload(config)
        try:
            assert reloaded.RPC_SURFACE_TIMEOUT > 0
            assert reloaded.RPC_SURFACE_TIMEOUT < reloaded.RPC_INFERENCE_TIMEOUT
        finally:
            monkeypatch.undo()
            importlib.reload(config)


class TestBudgetArithmetic:
    """Reproduce the split ``call_local_rpc`` performs, without touching the network."""

    @staticmethod
    def _split(cap: float, budget: float) -> tuple[float, float]:
        surface_allowance = min(budget, cap)
        # Worst case: Surface consumes its entire allowance before failing.
        return surface_allowance, max(0.0, budget - surface_allowance)

    def test_configured_cap_preserves_fallback_budget(self):
        _, remaining = self._split(config.RPC_SURFACE_TIMEOUT, config.RPC_INFERENCE_TIMEOUT)
        assert remaining > 0, "Pi/Dell fallbacks must still be reachable"

    def test_full_budget_cap_would_starve_fallbacks(self):
        """Documents why the cap is not simply RPC_INFERENCE_TIMEOUT."""
        _, remaining = self._split(600, 600)
        assert remaining == 0

    def test_old_hardcoded_cap_was_too_small(self):
        """45s timed out on every measured digest run."""
        allowance, _ = self._split(45, 600)
        assert allowance < 108, "45s is below the fastest observed digest (108s)"


class TestDigestMaxTokens:
    def test_default_is_bounded(self):
        assert config.DIGEST_MAX_TOKENS <= 2_000

    def test_default_fits_a_real_digest(self):
        # An 8-task / 13-topic digest used ~900 completion tokens.
        assert config.DIGEST_MAX_TOKENS >= 1_500

    def test_assembly_uses_the_config_value(self, monkeypatch):
        import ai_processor

        captured = {}

        def fake_inference(prompt, *, timeout, max_tokens):
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            return "✅ All caught up — no new actionable updates."

        monkeypatch.setattr(ai_processor, "_local_inference", fake_inference)
        ai_processor.assemble_digest({"canvas": "No canvas data available."})
        assert captured["max_tokens"] == config.DIGEST_MAX_TOKENS
        assert captured["max_tokens"] != 6_000, "the old unbounded ceiling must be gone"
