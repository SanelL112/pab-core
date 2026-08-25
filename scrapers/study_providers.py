"""Free online LLM providers for study-guide generation.

Fallback order (user-specified): OpenRouter free models -> OpenCode CLI ->
agy CLI -> Hack Club AI.  Every provider only ever sees the PII-scrubbed
prompt: ``generate_online`` scrubs, then refuses to send anything that
``check_pii`` still flags.  When every provider fails, callers fall back to
the local inference chain (offline last resort).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PROVIDER_ORDER = ("openrouter", "opencode", "agy", "hackclub")

_OPENCODE_TIMEOUT = 90
_AGY_TIMEOUT = 240
_HACKCLUB_TIMEOUT = 120
_OPENROUTER_TIMEOUT = 300


def _run_openrouter(prompt: str, system_prompt: str, max_tokens: int, timeout: float) -> str:
    from config import OR_DEFAULT_MODEL, OR_FALLBACK_MODEL, OR_THIRD_MODEL
    from llm_router import Sensitivity, call_openrouter

    return call_openrouter(
        model=OR_DEFAULT_MODEL,
        prompt=prompt,
        task="study-guide",
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        fallback_chain=[OR_FALLBACK_MODEL, OR_THIRD_MODEL],
        timeout=timeout or _OPENROUTER_TIMEOUT,
        sensitivity=Sensitivity.PUBLIC,
        cloud_consent=True,
    )


def _run_opencode(prompt: str, system_prompt: str, max_tokens: int, timeout: float) -> str:
    full = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    result = subprocess.run(
        ["opencode", "run", full],
        capture_output=True,
        text=True,
        timeout=timeout or _OPENCODE_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        logger.debug("opencode exit %s: %s", result.returncode, result.stderr[:200])
        return ""
    lines = [ln for ln in result.stdout.splitlines() if not ln.strip().startswith(">")]
    return "\n".join(lines).strip()


def _run_agy(prompt: str, system_prompt: str, max_tokens: int, timeout: float) -> str:
    full = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    result = subprocess.run(
        ["agy", "-p", full],
        capture_output=True,
        text=True,
        timeout=timeout or _AGY_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        logger.debug("agy exit %s: %s", result.returncode, result.stderr[:200])
        return ""
    return result.stdout.strip()


def _run_hackclub(prompt: str, system_prompt: str, max_tokens: int, timeout: float) -> str:
    import requests

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    resp = requests.post(
        "https://ai.hackclub.com/chat/completions",
        json={"messages": messages, "max_tokens": max_tokens},
        timeout=timeout or _HACKCLUB_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.debug("Hack Club AI returned HTTP %s", resp.status_code)
        return ""
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    return (message.get("content") or "").strip()


_PROVIDERS = {
    "openrouter": _run_openrouter,
    "opencode": _run_opencode,
    "agy": _run_agy,
    "hackclub": _run_hackclub,
}


def generate_online(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 4000,
    timeout: float = 300,
    order: tuple = PROVIDER_ORDER,
) -> tuple[str, str]:
    """Generate via the free-provider fallback chain.

    Returns ``(text, provider_name)``.  Returns ``("", "")`` when the prompt
    still contains PII after scrubbing (never sent online) or when every
    provider fails — callers then fall back to local inference.
    """
    from utils import check_pii, scrub_pii

    scrubbed = scrub_pii(prompt)
    scrubbed_system = scrub_pii(system_prompt) if system_prompt else ""
    user_safe, _, _ = check_pii(scrubbed)
    system_safe, _, _ = check_pii(scrubbed_system)
    if not (user_safe and system_safe):
        logger.warning("Online generation refused: PII detected after scrubbing")
        return "", "pii-blocked"

    for name in order:
        runner = _PROVIDERS.get(name)
        if runner is None:
            continue
        if _FAILURES.get(name, 0) >= 2:
            logger.info("Skipping provider %s: %d consecutive failures this process", name, _FAILURES[name])
            continue
        try:
            text = runner(scrubbed, scrubbed_system, max_tokens, timeout)
        except Exception as exc:
            logger.warning("Study provider %s failed: %s", name, type(exc).__name__)
            continue
        if text and text.strip():
            _FAILURES[name] = 0
            logger.info("Study provider %s served %s chars", name, len(text))
            return text.strip(), name
        _FAILURES[name] = _FAILURES.get(name, 0) + 1
        logger.warning("Study provider %s returned empty; falling through", name)
    return "", ""


_FAILURES: dict[str, int] = {}


def provider_configured(name: str) -> bool:
    """Cheap liveness check for a provider's binary/endpoint."""
    if name in ("opencode", "agy"):
        from shutil import which

        return which(name) is not None
    return True
