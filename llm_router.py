"""
llm_router.py — Unified LLM dispatch with cost tracking and smart fallbacks.

SECURITY MODEL:
- Ollama and llama.cpp/RPC are local and may handle private data.
- agy/Gemini, OpenRouter, Opencode Zen, and Hack Club are cloud providers.
- Private/PII requests fail closed unless a caller explicitly chooses a cloud
  provider for a public request.  Scrubbing is defense in depth, not consent.

This module centralizes:
1. OpenRouter HTTP calls (replaces 4+ copies of `_call_or` across the codebase)
2. Cost tracking (Feature 10: /stats command)
3. Unified fallback chain: try primary → fallback → local failover
4. Response validation (replaces the sanity check LLM call)
"""
import os
import time
import json
import hashlib
import logging
import requests
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from config import (
    OPENROUTER_API_KEY, OR_DEFAULT_MODEL, OR_FALLBACK_MODEL, OR_THIRD_MODEL,
    COST_LOG_FILE, AGENTAPI_BIN, OLLAMA_URL, OLLAMA_ORANGEPI_URL,
    OPENCODE_ZEN_API_KEY, OPENCODE_ZEN_URL
)

logger = logging.getLogger(__name__)


class ProviderTrust(str, Enum):
    """Whether inference payloads leave owner-controlled machines."""

    LOCAL = "local"
    CLOUD = "cloud"


class Sensitivity(str, Enum):
    """Data classification used at an inference boundary."""

    PUBLIC = "public"
    PERSONAL = "personal"
    PII = "pii"


class InferenceStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID = "invalid"
    POLICY_BLOCKED = "policy_blocked"
    ERROR = "error"


@dataclass(frozen=True)
class InferenceResult:
    """Typed result so an error message can never masquerade as model output."""

    status: InferenceStatus
    text: str = ""
    provider: str = ""
    model: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is InferenceStatus.OK and bool(self.text.strip())

    @classmethod
    def success(cls, text: str, *, provider: str, model: str) -> "InferenceResult":
        return cls(InferenceStatus.OK, text.strip(), provider, model)


class InferenceUnavailable(RuntimeError):
    """Raised by strict call sites when no policy-compliant model is available."""


_CLOUD_PROVIDER_MARKERS = (
    "agy", "gemini", "openrouter", "opencode", "zen", "hackclub", "hack club",
)
_LOCAL_PROVIDER_MARKERS = ("ollama", "llama.cpp", "llamacpp", "rpc", "local")


def provider_trust(provider_or_model: str) -> ProviderTrust:
    """Classify unknown providers as cloud so new integrations fail safe."""

    value = (provider_or_model or "").strip().lower()
    if any(marker in value for marker in _CLOUD_PROVIDER_MARKERS):
        return ProviderTrust.CLOUD
    if any(marker in value for marker in _LOCAL_PROVIDER_MARKERS):
        return ProviderTrust.LOCAL
    return ProviderTrust.CLOUD


def _coerce_sensitivity(value: Sensitivity | str) -> Sensitivity:
    try:
        return value if isinstance(value, Sensitivity) else Sensitivity(value)
    except ValueError:
        # An unknown classification must not weaken the privacy boundary.
        return Sensitivity.PERSONAL


def _cloud_policy_allows(
    prompt: str,
    system_prompt: str,
    *,
    sensitivity: Sensitivity | str,
    cloud_consent: bool,
) -> bool:
    """Return true only for explicitly approved, non-PII cloud payloads."""

    if not cloud_consent or _coerce_sensitivity(sensitivity) is not Sensitivity.PUBLIC:
        return False
    from utils import check_pii

    user_safe, _, _ = check_pii(prompt or "")
    system_safe, _, _ = check_pii(system_prompt or "")
    return user_safe and system_safe

# ── OpenRouter Session (connection pooling) ──────────────────────────────────
import httpx
import atexit

_or_session = None
_or_client = None

def _get_client() -> httpx.Client:
    global _or_client
    if _or_client is None:
        # Configure timeouts: connect=10s, read=60s, write=10s, pool=5s
        timeout = httpx.Timeout(10.0, connect=10.0, read=60.0, write=10.0, pool=5.0)
        _or_client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://github.com/SanelL112/Antigravity-Based-Assistant-Bot",
                "X-Title": "Personal Assistant Bot",
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _or_client


def _cleanup_clients():
    """Synchronously close pooled clients for interpreter-exit fallback."""
    global _or_client, _or_session
    if _or_client is not None:
        try:
            _or_client.close()
        except Exception:
            pass
        _or_client = None
    if _or_session is not None:
        try:
            _or_session.close()
        except Exception:
            pass
        _or_session = None


async def cleanup_llm_clients() -> None:
    """Application-lifecycle cleanup hook used by PTB post_shutdown."""
    _cleanup_clients()


atexit.register(_cleanup_clients)


def _get_session() -> requests.Session:
    """Deprecated: use _get_client() for httpx instead."""
    global _or_session
    if _or_session is None:
        _or_session = requests.Session()
        _or_session.headers.update({
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://github.com/SanelL112/Antigravity-Based-Assistant-Bot",
            "X-Title": "Personal Assistant Bot",
        })
    return _or_session


# ── Cost Tracking (Feature 10) ───────────────────────────────────────────────
def load_cost_log() -> dict:
    try:
        with open(COST_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": "", "total_usd": 0.0, "calls": [], "by_model": {}}


def save_cost_log(data: dict):
    """Atomic write for cost log (prevents corruption on crash)."""
    import tempfile
    try:
        fd, tmp_path = tempfile.mkstemp(dir=COST_LOG_FILE.parent, suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, COST_LOG_FILE)
    except Exception:
        with open(COST_LOG_FILE, "w") as f:
            json.dump(data, f, indent=2)


def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost. Free models and local = $0."""
    free_models = {
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "openrouter/owl-alpha:free",
        "tencent/hy3:free",
    }
    paid_rates = {
        "openrouter/owl-alpha": (0.01, 0.03),  # ($/1K input, $/1K output)
    }
    if model in free_models:
        return 0.0
    rates = paid_rates.get(model, (0.0, 0.0))
    return (input_tokens / 1000 * rates[0]) + (output_tokens / 1000 * rates[1])


def log_call(model: str, task: str, prompt: str, result: str, duration_s: float):
    """Log an LLM call for cost tracking."""
    in_tokens = estimate_tokens(prompt)
    out_tokens = estimate_tokens(result)
    cost = estimate_cost_usd(model, in_tokens, out_tokens)

    log = load_cost_log()
    today = time.strftime("%Y-%m-%d")
    if log.get("date") != today:
        log = {"date": today, "total_usd": 0.0, "calls": [], "by_model": {}}

    log["total_usd"] += cost
    log.setdefault("calls", []).append({
        "time": time.strftime("%H:%M:%S"),
        "model": model,
        "task": task,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cost_usd": round(cost, 6),
        "duration_s": round(duration_s, 2),
    })
    # Keep last 500 calls to prevent bloat
    if len(log["calls"]) > 500:
        log["calls"] = log["calls"][-500:]

    log.setdefault("by_model", {})
    log["by_model"].setdefault(model, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0})
    log["by_model"][model]["calls"] += 1
    log["by_model"][model]["tokens_in"] += in_tokens
    log["by_model"][model]["tokens_out"] += out_tokens
    log["by_model"][model]["cost"] += cost

    save_cost_log(log)
    # The kiosk needs only the current operational route; prompts and model
    # output are deliberately excluded from this side-channel.
    try:
        from bot.dashboard_state import record_route
        if model == "llamacpp-rpc":
            record_route(model, "surface-rpc")
        elif model.startswith(("qwen", "llama", "hf.co/")):
            record_route(model, "local")
        else:
            record_route(model, "cloud")
    except Exception:
        pass


def get_cost_summary():
    log = load_cost_log()
    today = time.strftime("%Y-%m-%d")

    if log.get("date") != today:
        return "📊 **Cost Dashboard**\nNo activity today yet."

    lines = [
        f"📊 **Cost Dashboard ({today})**",
        f"Total: `${log['total_usd']:.4f}` | {len(log.get('calls', []))} calls",
        "",
        "**By Model:**",
    ]
    for model, stats in log.get("by_model", {}).items():
        lines.append(f"  `{model}`: {stats['calls']} calls, "
                     f"{stats['tokens_in'] + stats['tokens_out']:,} tokens, "
                     f"${stats['cost']:.4f}")
    return "\n".join(lines)


# ── Response Validation (replaces sanity check LLM call) ─────────────────────
FAIL_PHRASES = ["i cannot", "i'm sorry", "i don't know", "as an ai", "unable to", "i apologize"]


def is_valid_response(text: str) -> bool:
    """Heuristic response validation — no LLM call needed."""
    if not text or len(text.strip()) < 5:
        return False
    # Detect system prompt regurgitation
    if text.startswith("You are a powerful personal assistant") or \
       text.startswith("You are Sanel"):
        return False
    # Short refusals
    if len(text) < 100 and any(p in text.lower()[:50] for p in FAIL_PHRASES):
        return False
    return True


# ── OpenRouter Unified Caller ────────────────────────────────────────────────
def call_openrouter(
    model: str,
    prompt: str,
    task: str = "general",
    max_tokens: int = 4000,
    system_prompt: str = "",
    fallback_chain: list = None,
    timeout: int | float = 120,
    stream_to_status=None,   # optional: (context, chat_id, status_msg) for streaming edits
    *,
    sensitivity: Sensitivity | str = Sensitivity.PUBLIC,
    cloud_consent: bool = False,
) -> str:
    """
    Unified OpenRouter caller with retry, fallback, cost tracking.

    This is a cloud boundary.  The caller must provide ``cloud_consent=True``
    for a public, non-PII request.  Scrubbing remains defense in depth and is
    never used to turn private data into an implicitly approved request.

    Args:
        model: OpenRouter model ID (e.g. "openrouter/owl-alpha")
        prompt: The user message
        task: Label for cost tracking (e.g. "study-guide", "watchdog", "chat")
        system_prompt: Optional system message
        fallback_chain: List of fallback model IDs if primary fails
        timeout: HTTP timeout in seconds
        stream_to_status: Optional (context, chat_id, status_msg) for live typing updates

    Returns:
        Generated text string
    """
    if not _cloud_policy_allows(
        prompt,
        system_prompt,
        sensitivity=sensitivity,
        cloud_consent=cloud_consent,
    ):
        logger.warning("Cloud inference blocked by data/consent policy for task=%s", task)
        return ""

    # Defense in depth for an already-approved public payload.
    from utils import scrub_pii
    scrubbed_prompt = scrub_pii(prompt)
    if scrubbed_prompt != prompt:
        logger.info(f"scrubbed PII from {task} prompt")
    scrubbed_system = scrub_pii(system_prompt) if system_prompt else ""

    chain = [model] + (fallback_chain or [])
    total_budget = max(0.1, float(timeout))
    deadline = time.monotonic() + total_budget

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    for i, m in enumerate(chain):
        if remaining() <= 0:
            logger.warning("Cloud inference deadline exhausted for task=%s", task)
            break
        start = time.monotonic()
        # Brief delay between fallback attempts to let rate limits cool down
        if i > 0:
            delay = min(3 * i, 10, remaining())
            if delay <= 0:
                break
            logger.info(f"Waiting {delay}s before trying fallback {m}...")
            time.sleep(delay)
        try:
            result = _do_call(m, scrubbed_prompt, task, max_tokens, scrubbed_system, remaining(),
                              stream_to_status if i == 0 else None)
            duration = time.monotonic() - start

            if is_valid_response(result):
                log_call(m, task, scrubbed_prompt, result, duration)
                return result
            else:
                logger.warning(f"Model {m} returned invalid response, trying fallback...")
                log_call(m, task, scrubbed_prompt, "(invalid)", duration)
                continue

        except Exception as e:
            duration = time.monotonic() - start
            logger.warning(f"Model: {e} ({duration:.1f}s)")
            log_call(m, task, scrubbed_prompt, f"(error: {e})", duration)
            continue

    # ── Opencode Zen cross-provider fallback ──
    # OpenRouter free models share one rate-limit bucket. When 429'd,
    # Opencode Zen models are a separate provider with their own rate limits.
    # with their own rate limits.
    try:
        from config import OPENCODE_ZEN_API_KEY
        if OPENCODE_ZEN_API_KEY and remaining() > 0:
            logger.info("OpenRouter chain exhausted — falling back to Opencode Zen")
            return call_opencode(
                # The provider no longer accepts hy3-free; use the documented
                # default model rather than attempting a known-invalid request.
                model="mimo-v2.5-free",
                prompt=scrubbed_prompt,
                task=task,
                max_tokens=max_tokens,
                system_prompt=scrubbed_system,
                timeout=remaining(),
                sensitivity=Sensitivity.PUBLIC,
                cloud_consent=True,
            )
    except Exception as e:
        logger.warning(f"Opencode Zen fallback also failed: {e}")

    # ── Hack Club AI (another provider, separate rate-limit bucket) ──
    try:
        from config import HACKCLUB_AI_API_KEY
        if HACKCLUB_AI_API_KEY and remaining() > 0:
            logger.info("Previous providers exhausted — falling back to Hack Club AI")
            return call_hackclub(
                model="qwen/qwen3-32b",
                prompt=scrubbed_prompt,
                task=task,
                max_tokens=min(max_tokens, 8000),
                system_prompt=scrubbed_system,
                timeout=remaining(),
                sensitivity=Sensitivity.PUBLIC,
                cloud_consent=True,
            )
    except Exception as e:
        logger.warning(f"Hack Club AI fallback also failed: {e}")

    return ""


def call_openrouter_result(**kwargs) -> InferenceResult:
    """Typed OpenRouter entry point for new and security-sensitive callers."""

    prompt = kwargs.get("prompt", "")
    system_prompt = kwargs.get("system_prompt", "")
    sensitivity = kwargs.get("sensitivity", Sensitivity.PUBLIC)
    cloud_consent = kwargs.get("cloud_consent", False)
    model = kwargs.get("model", "openrouter")
    if not _cloud_policy_allows(
        prompt,
        system_prompt,
        sensitivity=sensitivity,
        cloud_consent=cloud_consent,
    ):
        return InferenceResult(
            InferenceStatus.POLICY_BLOCKED,
            provider="openrouter",
            model=model,
            detail="Cloud consent or public-data classification missing",
        )
    try:
        text = call_openrouter(**kwargs)
    except (httpx.TimeoutException, TimeoutError) as exc:
        return InferenceResult(
            InferenceStatus.TIMEOUT, provider="openrouter", model=model, detail=str(exc)
        )
    except Exception as exc:
        logger.warning("OpenRouter inference failed: %s", type(exc).__name__)
        return InferenceResult(
            InferenceStatus.ERROR, provider="openrouter", model=model, detail=type(exc).__name__
        )
    if is_valid_response(text):
        return InferenceResult.success(text, provider="openrouter", model=model)
    return InferenceResult(
        InferenceStatus.UNAVAILABLE, provider="openrouter", model=model,
        detail="No provider returned a valid response",
    )


def _do_call(
    model: str,
    prompt: str,
    task: str,
    max_tokens: int,
    system_prompt: str,
    timeout: int,
    stream_to_status: Optional[tuple],
) -> str:
    """Execute a single OpenRouter API call. Supports streaming with live status updates."""
    # Use the pooled client from module level (connection reuse)
    client = _get_client()
    from utils import scrub_pii

    # SECURITY: Scrub PII from all cloud-bound prompts
    scrubbed_prompt = scrub_pii(prompt)
    if scrubbed_prompt != prompt:
        logger.info(f"scrubbed PII from {task} prompt")
    if system_prompt:
        scrubbed_system = scrub_pii(system_prompt)
    else:
        scrubbed_system = ""

    messages = []
    if scrubbed_system:
        messages.append({"role": "system", "content": scrubbed_system})
    messages.append({"role": "user", "content": scrubbed_prompt})

    # Streaming path (for chat responses where we show typing)
    if stream_to_status:
        return _streaming_call(client, model, messages, task, max_tokens, timeout, stream_to_status)
    else:
        # Non-streaming (simpler, for everything else)
        # Convert int timeout to httpx.Timeout if needed
        if isinstance(timeout, int):
            call_timeout = httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=5.0)
        else:
            call_timeout = timeout

        resp = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
            timeout=call_timeout,
        )
        if resp.status_code == 200:
            choice = resp.json()["choices"][0]
            text = choice["message"]["content"].strip()
            # Detect truncation
            if choice.get("finish_reason") == "length":
                text += "\n\n⚠️ Response was truncated due to output length limits."
            return text
        else:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

def _streaming_call(client, model, messages, task, max_tokens, timeout, stream_to_status):
    """Stream an OpenRouter response with a hard end-to-end deadline."""
    # stream_to_status is (context, chat_id, status_msg, loop)
    context, chat_id, status_msg, main_loop = stream_to_status
    full_response = ""
    current_thought = ""
    in_thought = False
    last_edit = 0

    budget = max(0.1, float(timeout))
    deadline = time.monotonic() + budget
    stream_timeout = httpx.Timeout(
        connect=min(10.0, budget), read=budget, write=min(10.0, budget), pool=min(5.0, budget)
    )

    try:
        with client.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": True,
            },
            timeout=stream_timeout,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"OpenRouter HTTP {resp.status_code}")

            for line in resp.iter_lines():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"OpenRouter total deadline exceeded after {budget:.1f}s")
                if isinstance(line, bytes):
                    # Compatibility with simple test doubles; httpx itself yields str.
                    line = line.decode("utf-8", errors="replace")
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].lstrip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue
                content = delta.get("content", "")
                if isinstance(content, str) and content:
                    full_response += content

                    if "<thought>" in full_response and "</thought>" not in full_response:
                        in_thought = True
                        current_thought = full_response.split("<thought>")[-1]
                    elif "</thought>" in full_response:
                        in_thought = False

                    now = time.time()
                    if now - last_edit > 1.5 and status_msg and context:
                        last_edit = now
                        try:
                            import asyncio
                            from bot.ui import render_assistant_text
                            coro = None
                            if in_thought:
                                disp = current_thought[-400:].strip()
                                coro = context.bot.edit_message_text(
                                    chat_id=chat_id, message_id=status_msg.message_id,
                                    text=render_assistant_text(disp, title="Thinking"), parse_mode="HTML"
                                )
                            else:
                                final_text = full_response.split("</thought>")[-1] if "</thought>" in full_response else full_response
                                disp = final_text[-800:].strip()
                                if disp:
                                    coro = context.bot.edit_message_text(
                                        chat_id=chat_id, message_id=status_msg.message_id,
                                        text=render_assistant_text(disp, title="Drafting"), parse_mode="HTML"
                                    )
                            if coro:
                                try:
                                    asyncio.run_coroutine_threadsafe(coro, main_loop)
                                except Exception:
                                    pass # Ignore if loop is dead or coro fails
                        except Exception:
                            pass
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"OpenRouter streaming timeout after {budget:.1f}s") from exc

    if "</thought>" in full_response:
        return full_response.split("</thought>")[-1].strip()
    return full_response.strip()


# ── Local LLM Wrappers (PII-safe, never leaves server) ──────────────────────
def call_ollama(prompt: str, model: str = "hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:latest",
                timeout: int | float = 30, url: str | None = None) -> str:
    """Call local Ollama model. Safe for PII — runs entirely on your server.

    Args:
        prompt: The prompt to send
        model: Model name
        timeout: Read timeout in seconds
        url: Ollama server URL (defaults to OLLAMA_URL from config, or OLLAMA_ORANGEPI_URL if model starts with 'qwen2.5:3b' or 'qwen2:0.5b')

    Behavior:
        * Connects with a tight 2 s connect timeout (LAN/VPN target should fail fast).
        * Trailing-slash-safe URL join via rstrip('/').
        * If the call was auto-routed to OLLAMA_ORANGEPI_URL and the response is empty
          (timeout / connection error / non-200), transparently retry once against
          OLLAMA_URL (the local box) so the bot doesn't silently return "" to callers
          that would otherwise treat that as a real answer.
    """
    # Auto-select Orange Pi 5 for qwen2.5:3b and qwen2:0.5b models
    if url is None:
        if model.startswith("qwen2.5:3b") or model.startswith("qwen2:0.5b") or "350M" in model or "350m" in model:
            url = OLLAMA_ORANGEPI_URL
        else:
            url = OLLAMA_URL

    budget = max(0.1, float(timeout))
    deadline = time.monotonic() + budget

    def _attempt(target_url: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        try:
            import httpx
            # Tight connect timeout for LAN/VPN; all fallbacks share one deadline.
            httpx_timeout = httpx.Timeout(
                connect=min(2.0, remaining), read=remaining,
                write=min(10.0, remaining), pool=min(5.0, remaining),
            )
            resp = httpx.post(
                f"{target_url.rstrip('/')}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.0}},
                timeout=httpx_timeout,
            )
            if resp.status_code == 200:
                return resp.json().get("response", "").strip()
            return ""
        except httpx.TimeoutException:
            logger.error(f"Ollama call to {target_url} timed out within {budget:.1f}s budget")
            return ""
        except Exception as e:
            logger.error(f"Ollama call to {target_url} failed: {e}")
            return ""

    result = _attempt(url)
    # Transparent fallback: if we tried the Orange Pi and got nothing, retry on local Ollama.
    if not result and url == OLLAMA_ORANGEPI_URL and OLLAMA_ORANGEPI_URL != OLLAMA_URL:
        logger.warning(
            f"Orange Pi Ollama at {url} returned empty; falling back to local Ollama at {OLLAMA_URL}"
        )
        result = _attempt(OLLAMA_URL)
    return result


def call_ollama_result(
    prompt: str,
    model: str = "hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:latest",
    timeout: int | float = 30,
    url: str | None = None,
) -> InferenceResult:
    """Typed local inference entry point."""

    try:
        text = call_ollama(prompt=prompt, model=model, timeout=timeout, url=url)
    except (httpx.TimeoutException, TimeoutError) as exc:
        return InferenceResult(
            InferenceStatus.TIMEOUT, provider="ollama", model=model, detail=str(exc)
        )
    except Exception as exc:
        logger.warning("Ollama inference failed: %s", type(exc).__name__)
        return InferenceResult(
            InferenceStatus.ERROR, provider="ollama", model=model, detail=type(exc).__name__
        )
    if text.strip():
        return InferenceResult.success(text, provider="ollama", model=model)
    return InferenceResult(
        InferenceStatus.UNAVAILABLE, provider="ollama", model=model,
        detail="Local model returned no content",
    )


# agy no longer accepts short aliases ("flash"/"pro"); it wants the full
# display-name model IDs (see `agy models`). Map our internal short names to
# the current valid identifiers. Update this table if `agy models` changes.
AGY_MODEL_ALIASES = {
    "flash": "Gemini 3.7 Flash (Low)",
    "flash-low": "Gemini 3.7 Flash (Low)",
    "flash-med": "Gemini 3.7 Flash (Medium)",
    "flash-high": "Gemini 3.7 Flash (High)",
    "flash-3.5": "Gemini 3.5 Flash (Low)",
    "pro": "Gemini 3.1 Pro (Low)",
    "pro-high": "Gemini 3.1 Pro (High)",
    "claude": "Claude Sonnet 4.6 (Thinking)",
    "gpt-oss": "GPT-OSS 120B (Medium)",
}


def _resolve_agy_model(model: str) -> str:
    """Map an internal short alias to agy's current display-name model ID.

    Passes through anything already containing a space (a real display name),
    so callers can specify an exact model if they want one.
    """
    if model and (" " in model or model.startswith("openrouter:")):
        return model
    return AGY_MODEL_ALIASES.get(model, AGY_MODEL_ALIASES["flash"])


def call_agy_local(
    prompt: str,
    model: str = "flash",
    timeout: int | float = 180,
    *,
    sensitivity: Sensitivity | str = Sensitivity.PERSONAL,
    cloud_consent: bool = False,
) -> str:
    """Compatibility wrapper for the cloud-hosted agy/Gemini CLI.

    The historical name is retained for imports, but agy is *not* local.  It is
    disabled by default and may only receive an explicitly approved public,
    non-PII prompt.  The CLI is never granted unattended tool permissions.
    """

    if not _cloud_policy_allows(
        prompt,
        "",
        sensitivity=sensitivity,
        cloud_consent=cloud_consent,
    ):
        logger.warning("agy/Gemini request blocked by cloud data policy")
        return ""

    import re
    import subprocess

    target_model = _resolve_agy_model(model)
    try:
        result = subprocess.run(
            [AGENTAPI_BIN, "--model", target_model, "--print", prompt],
            capture_output=True,
            text=True,
            timeout=max(0.1, float(timeout)),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (subprocess.TimeoutExpired, TimeoutError):
        logger.warning("agy/Gemini request timed out")
        return ""
    except Exception as exc:
        logger.warning("agy/Gemini request failed: %s", type(exc).__name__)
        return ""

    if result.returncode != 0:
        logger.warning("agy/Gemini exited with status %s", result.returncode)
        return ""
    clean = re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', result.stdout or "")
    return clean.replace('\r\n', '\n').replace('\r', '\n').strip()


def call_agy_result(
    prompt: str,
    model: str = "flash",
    timeout: int | float = 180,
    *,
    sensitivity: Sensitivity | str = Sensitivity.PUBLIC,
    cloud_consent: bool = True,
) -> InferenceResult:
    """Execute inference via Google Gemini / agy harness and return InferenceResult."""
    text = call_agy_local(
        prompt=prompt,
        model=model,
        timeout=timeout,
        sensitivity=sensitivity,
        cloud_consent=cloud_consent,
    )
    if text and is_valid_response(text):
        target_name = _resolve_agy_model(model)
        return InferenceResult.success(text, provider="agy", model=target_name)
    return InferenceResult(
        InferenceStatus.UNAVAILABLE,
        provider="agy",
        detail="AGY CLI harness returned empty or failed",
    )


# ── RPC Cluster (Surface orchestrator + Dell + Pi workers) ────────────────
# Topology:
#   Surface Pro (10.0.0.47:8080) — runs llama-server with RPC orchestration
#     → Dell E5430 (10.10.10.1:50052) — ggml-rpc-server worker
#     → Orange Pi 5 (10.42.0.139:50052) — ggml-rpc-server worker
#
# Bot sends HTTP requests to Surface's OpenAI-compatible API.
# Surface handles RPC layer splitting between workers internally.
#
# For direct llama-cli (non-Surface path), models live here:
RPC_GGUF_MODEL_DIR = os.path.dirname(os.getenv(
    "RPC_GGUF_PATH",
    "/home/sanel/models/qwen2.5-7b-instruct-q4_K_M.gguf"
))
RPC_GGUF_7B = "/home/sanel/models/qwen2.5-7b-instruct-q4_K_M.gguf"
RPC_GGUF_QWYTHOS_9B = "/home/sanel/models/Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M.gguf"
RPC_GGUF_QWEN35_9B = "/home/sanel/models/Qwen3.5-9B-Q4_K_M.gguf"
RPC_GGUF_DEFAULT = RPC_GGUF_7B  # best speed/reliability balance
# RPC worker
RPC_WORKER_URL = os.getenv("RPC_WORKER_URL", "10.10.10.2:50052")
# llama-cli binary (RPC-enabled build)
LLAMACPP_CLI = "/home/sanel/llama.cpp-build/build/bin/llama-cli"
LLAMACPP_LD_PATH = "/home/sanel/llama.cpp-build/build/bin"


def call_local_rpc(
    prompt: str,
    system_prompt: str = "",
    model_path: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: int | float = 120,
    allow_cloud: bool = False,
    *,
    sensitivity: Sensitivity | str = Sensitivity.PERSONAL,
    cloud_consent: bool = False,
) -> str:
    """
    Primary local inference path — tries cluster nodes in order.

    Fallback chain:
      1. Surface llama-server (10.0.0.47:8080) — primary, short timeout
      2. Pi Ollama (10.10.10.2:11434) — fast local backup
      3. Dell local Ollama
      4. Cloud only when both ``allow_cloud`` and explicit public-data consent
         are supplied.  The default is local-only.

    Args:
        prompt: The user prompt
        system_prompt: Optional system message
        model_path: Ignored (Surface server has its own model loaded)
        max_tokens: Max output tokens
        temperature: 0.0 = deterministic
        timeout: Max seconds to wait
        allow_cloud: Whether to fallback to cloud if local paths fail.

    Returns:
        Generated text, or an empty string if all policy-compliant paths fail.
    """
    budget = max(0.1, float(timeout))
    deadline = time.monotonic() + budget

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    # Cap the Surface attempt so a stall cannot eat the whole shared budget and
    # starve the Pi/Dell fallbacks below (see config.RPC_SURFACE_TIMEOUT).
    from config import RPC_SURFACE_TIMEOUT

    surface_timeout = min(remaining(), float(RPC_SURFACE_TIMEOUT))

    logger.info("call_local_rpc: trying Surface orchestrator API (%s)", LLAMACPP_RPC_URL)
    result = ""
    if surface_timeout > 0:
        result = call_llamacpp_rpc(
            prompt=prompt,
            model_path=model_path,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout=surface_timeout,
            temperature=temperature,
        )
    if result and result.strip():
        logger.info(f"call_local_rpc: Surface API returned {len(result)} chars")
        return result

    from config import OLLAMA_LOCAL_URL, OLLAMA_ORANGEPI_URL, RPC_FALLBACK_OLLAMA_MODEL, RPC_SMALL_OLLAMA_MODEL

    def _try_ollama(target_url: str, model: str, label: str) -> str:
        attempt_budget = remaining()
        if attempt_budget <= 0:
            return ""
        logger.info("call_local_rpc: trying %s", label)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    connect=min(3.0, attempt_budget), read=attempt_budget,
                    write=min(10.0, attempt_budget), pool=min(5.0, attempt_budget),
                )
            ) as client:
                resp = client.post(
                    f"{target_url.rstrip('/')}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"num_predict": max_tokens, "temperature": temperature},
                    },
                )
            if resp.status_code != 200:
                logger.warning("call_local_rpc: %s returned HTTP %s", label, resp.status_code)
                return ""
            payload = resp.json()
            text = payload.get("response", "") if isinstance(payload, dict) else ""
            return text.strip() if isinstance(text, str) else ""
        except Exception as exc:
            logger.warning("call_local_rpc: %s failed: %s", label, type(exc).__name__)
            return ""

    result = _try_ollama(
        OLLAMA_ORANGEPI_URL,
        RPC_SMALL_OLLAMA_MODEL,
        "Pi Ollama",
    )
    if result:
        return result

    result = _try_ollama(OLLAMA_LOCAL_URL, RPC_FALLBACK_OLLAMA_MODEL, "Dell Ollama")
    if result:
        return result

    if allow_cloud and remaining() > 0:
        logger.warning("call_local_rpc: local paths exhausted; evaluating cloud policy")
        from config import RPC_FALLBACK_CLOUD_MODEL
        return call_openrouter(
            model=RPC_FALLBACK_CLOUD_MODEL,
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout=remaining(),
            sensitivity=sensitivity,
            cloud_consent=cloud_consent,
        )

    logger.warning("call_local_rpc: all local paths exhausted; no cloud request made")
    return ""


def call_local_rpc_result(**kwargs) -> InferenceResult:
    """Typed wrapper around the local cluster fallback chain."""

    # New typed callers are local-only unless they deliberately override both
    # cloud gates.  This avoids an innocent omission turning into data egress.
    kwargs.setdefault("allow_cloud", False)
    try:
        text = call_local_rpc(**kwargs)
    except (httpx.TimeoutException, TimeoutError) as exc:
        return InferenceResult(
            InferenceStatus.TIMEOUT, provider="local-rpc", detail=str(exc)
        )
    except Exception as exc:
        logger.warning("Local RPC inference failed: %s", type(exc).__name__)
        return InferenceResult(
            InferenceStatus.ERROR, provider="local-rpc", detail=type(exc).__name__
        )
    if text.strip():
        return InferenceResult.success(text, provider="local-rpc", model="local-fallback-chain")
    return InferenceResult(
        InferenceStatus.UNAVAILABLE,
        provider="local-rpc",
        model="local-fallback-chain",
        detail="All local inference nodes were unavailable",
    )


# ── RPC (llama-server HTTP path) ────────────────────────────────────────────
# Surface Pro runs llama-server (OpenAI-compatible) with LFM2.5-2.6B-Q8_0.
# Network: Dell (10.10.10.1, wired) → Pi (10.10.10.2, end1) → Surface
# (10.42.0.1, usb0 — reachable from the Dell via Pi ip_forward). The Pi also
# runs ggml-rpc-server on :50052 as an RPC worker.
LLAMACPP_RPC_URL = os.getenv("LLAMACPP_RPC_URL", "http://10.42.0.1:8080")
RPC_MODEL_PATH = os.getenv(
    "RPC_MODEL_PATH",
    "/home/sanel/.ollama/models/blobs/sha256-*qwen2.5*7b*"
)


def call_llamacpp_rpc(
    prompt: str,
    model_path: str = None,
    system_prompt: str = "",
    max_tokens: int = 4000,
    timeout: int | float = 600,
    temperature: float = 0.0,
) -> str:
    """
    Call Surface tablet's llama-server (OpenAI-compatible API) that internally uses
    RPC to split inference across the cluster (Dell + Orange Pi 5).

    The Surface tablet (10.0.0.47:8080) runs llama-server with --rpc flags pointing
    to the Dell (10.10.10.1:50052) and Pi (10.42.0.139:50052) as RPC workers.
    This function sends the HTTP request — the Surface handles all RPC orchestration.

    Use this for OVERNIGHT BATCH TASKS and watchdog inference.
    For interactive use, prefer call_ollama() with a local model.

    The llama-server must already be running on the Surface (systemd service).

    Args:
        prompt: The user prompt
        model_path: Ignored — the Surface's server has its own model loaded
        system_prompt: Optional system message
        max_tokens: Max output tokens
        timeout: HTTP timeout (longer for RPC inference over WiFi)
        temperature: 0.0 = deterministic, higher = creative

    Returns:
        Generated text, or empty string on failure.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    budget = max(0.1, float(timeout))
    start = time.monotonic()
    try:
        import httpx
        with httpx.Client(
            timeout=httpx.Timeout(
                connect=min(10.0, budget), read=budget,
                write=min(10.0, budget), pool=min(10.0, budget),
            ),
        ) as client:
            resp = client.post(
                f"{LLAMACPP_RPC_URL.rstrip('/')}/v1/chat/completions",
                json={
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": False,
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                logger.info("RPC: Surface returned malformed response")
                return ""
            message = choices[0].get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else ""
            text = content.strip() if isinstance(content, str) else ""
            if not text:
                # Reasoning models (LFM2.5-2.6B) spend early tokens in
                # reasoning_content before answering; a tight max_tokens can
                # exhaust the budget there. Return the reasoning trace rather
                # than failing — callers scanning for JSON still find the draft.
                reasoning = message.get("reasoning_content", "") if isinstance(message, dict) else ""
                text = reasoning.strip() if isinstance(reasoning, str) else ""
            if not text:
                return ""
            duration = time.monotonic() - start
            tok_est = len(text) // 4
            logger.info(
                f"RPC inference: {len(text)} chars in {duration:.1f}s "
                f"(~{tok_est / max(duration, 0.1):.0f} tok/s)"
            )
            return text
        else:
            # Surface unavailable is NON-fatal: call_local_rpc falls back to
            # the Pi Ollama next. Log at INFO so it doesn't get forwarded to
            # Telegram every 30 min as a scary "⚙️ RPC: ..." alert.
            logger.info(f"RPC: Surface HTTP {resp.status_code} (falling back to Pi)")
            return ""
    except httpx.TimeoutException:
        logger.info(f"RPC: Surface timed out after {budget:.1f}s (falling back to Pi)")
        return ""
    except Exception as e:
        logger.info(f"RPC: Surface unreachable ({e}) — falling back to Pi")
        return ""


def is_rpc_server_healthy() -> bool:
    """Check if the RPC-enabled llama-server is running and responding."""
    try:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
            resp = client.get(f"{LLAMACPP_RPC_URL.rstrip('/')}/health")
            return resp.status_code == 200
    except Exception:
        return False


def start_rpc_llama_server(model_path: str = None, port: int = 8080,
                           ngl: int = 99, ctx_size: int = 4096,
                           threads: int = 2) -> bool:
    """
    Start llama-server with RPC to Orange Pi. Blocking — runs in foreground.
    Use for manual testing. For production, use the systemd service.

    Args:
        model_path: Path to GGUF model
        port: HTTP listen port
        ngl: Number of GPU layers (set high to force layer offload via RPC)
        ctx_size: Context window size
        threads: CPU threads on server side

    Returns:
        True if server started successfully (never returns if successful — blocks).
    """
    import subprocess as _sp
    import glob as _glob

    path = model_path or RPC_MODEL_PATH
    if "*" in path:
        matches = sorted(_glob.glob(path), key=os.path.getsize, reverse=True)
        if not matches:
            logger.error(f"RPC start: no model found matching {path}")
            return False
        path = matches[0]

    if not os.path.exists(path):
        logger.error(f"RPC start: model not found at {path}")
        return False

    cmd = [
        "llama-server",
        "-m", path,
        "--rpc", RPC_WORKER_URL,
        "--host", "127.0.0.1",
        "--port", str(port),
        "-ngl", str(ngl),
        "-c", str(ctx_size),
        "-t", str(threads),
    ]
    logger.info(f"Starting RPC llama-server: {' '.join(cmd)}")
    try:
        # subprocess.run() blocks until the server exits — this is
        # intentional for foreground/manual use. For background use,
        # run the scripts/start-rpc-overnight.sh script or systemd service.
        _sp.run(cmd, check=True)
        # Unreachable unless server crashes/exits — treat as failure.
        logger.error("RPC llama-server exited unexpectedly.")
        return False
    except FileNotFoundError:
        logger.error("llama-server not found. Install: brew install llama.cpp  or  apt install llama.cpp")
        return False
    except Exception as e:
        logger.error(f"Failed to start RPC server: {e}")
        return False


def stop_rpc_llama_server() -> bool:
    """Kill any running llama-server process on this machine."""
    import subprocess as _sp
    try:
        _sp.run(
            ["pkill", "-f", "llama-server"],
            capture_output=True, timeout=10
        )
        logger.info("RPC llama-server stopped.")
        return True
    except Exception as e:
        logger.error(f"Failed to stop RPC server: {e}")
        return False


def get_free_memory_mb() -> int:
    """Return available system memory in MB (Linux only)."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


def _get_surface_free_mb() -> int:
    """Free RAM (MB) on the Surface orchestrator via SSH. -1 if unreachable.

    The Surface is the node that actually LOADS the model into RAM, so it's the
    one that OOMs — not the Dell (which only runs a ggml-rpc-server worker that
    holds a fraction of the layers). This is the memory check that matters."""
    surface_host = LLAMACPP_RPC_URL.replace("http://", "").replace("https://", "").split(":")[0]
    surface_user = os.getenv("SURFACE_SSH_USER", "sanel-lathiya")
    try:
        import subprocess as _sp
        result = _sp.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             f"{surface_user}@{surface_host}",
             "awk '/MemAvailable:/{print int($2/1024)}' /proc/meminfo"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except Exception:
        pass
    return -1


def check_rpc_memory_ok(server_min_mb: int = 1500, worker_min_mb: int = 800) -> tuple[bool, str]:
    """
    Check if cluster nodes have enough free RAM for inference.

    In the 3-node topology:
      - Orchestrator = Surface (10.0.0.47) — runs llama-server and LOADS THE MODEL.
        This is the node that OOMs, so its free RAM is the primary gate.
      - Worker = Dell (this machine) — runs ggml-rpc-server; holds only offloaded
        layers, so a lower bar applies.
      - Worker = Orange Pi (10.10.10.2) — runs ggml-rpc-server, checked via SSH.

    Returns:
        (ok, reason) — ok=True if all reachable nodes have sufficient RAM.
    """
    # RPC_WORKER_URL is defined at module level.
    #
    # IMPORTANT: llama-web is a PERSISTENT service — the model is loaded once and
    # stays resident. Worker nodes (Dell, Pi) run ggml-rpc-server and legitimately
    # hold their share of the model's layers, so their "free RAM" is LOW precisely
    # when the cluster is healthy and serving. Vetoing on low worker RAM would
    # wrongly block a working cluster. OOM risk lives at model-LOAD time, which is
    # now guarded in cluster_manager.validate_model (size + quant checks).
    #
    # So the runtime gate is: the Surface orchestrator must not be thrashing. The
    # actual serving readiness is confirmed separately by is_rpc_server_healthy().
    # Worker RAM is reported for observability only, never used to veto.
    surface_free = _get_surface_free_mb()
    if surface_free == -1:
        logger.info("check_rpc_memory_ok: Surface RAM unreadable (SSH) — relying on health check")
    elif surface_free < server_min_mb:
        return False, f"Surface RAM too low ({surface_free}MB free, need {server_min_mb}MB) — orchestrator thrashing"

    # Observability only (NOT a veto): workers holding model layers is healthy.
    dell_free = get_free_memory_mb()
    pi_free = None
    worker_host = RPC_WORKER_URL.split(":")[0]
    try:
        import subprocess as _sp
        result = _sp.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", f"root@{worker_host}",
             "awk '/MemAvailable:/{print int($2/1024)}' /proc/meminfo"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            pi_free = int(result.stdout.strip())
    except Exception:
        pass

    surface_s = f"Surface: {surface_free}MB" if surface_free != -1 else "Surface: n/a"
    dell_s = f"Dell(worker): {dell_free}MB" if dell_free != -1 else "Dell: n/a"
    pi_s = f"Pi(worker): {pi_free}MB" if pi_free is not None else "Pi: unreachable"
    return True, f"{surface_s}, {dell_s}, {pi_s}"


def call_llamacpp_rpc_with_fallback(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 4000,
    task: str = "overnight",
    timeout: int = 600,
    skip_cloud_fallback: bool = True,
    *,
    sensitivity: Sensitivity | str = Sensitivity.PERSONAL,
    cloud_consent: bool = False,
) -> str:
    """
    Call Surface orchestrator API with full OOM protection and fallback chain.

    Fallback order:
      1. Surface API (primary) — llama-server on 10.0.0.47:8080, RPCs to Dell + Pi
      2. Solo Ollama on Dell (qwen2:0.5b or configured fallback model)
      3. Cloud API via OpenRouter (free tier) — skipped if skip_cloud_fallback=True

    Each step validates memory + server health before attempting.

    Args:
        prompt: The user prompt
        system_prompt: Optional system message
        max_tokens: Max output tokens
        task: Label for cost tracking
        timeout: Per-request timeout (seconds)
        skip_cloud_fallback: If True, skip the cloud API fallback (step 3).
            Use when caller has its own cloud fallback (e.g., Mega Study Builder).

    Returns:
        Generated text, or empty string if all fallbacks fail.
    """
    from config import (
        RPC_SERVER_MIN_FREE_MB, RPC_WORKER_MIN_FREE_MB,
        RPC_FALLBACK_OLLAMA_MODEL, RPC_FALLBACK_CLOUD_MODEL,
        RPC_INFERENCE_TIMEOUT
    )

    # ── Step 1: Check if RPC is viable (memory check) ─────────────────────
    mem_ok, mem_reason = check_rpc_memory_ok(
        RPC_SERVER_MIN_FREE_MB, RPC_WORKER_MIN_FREE_MB
    )

    if mem_ok and is_rpc_server_healthy():
        logger.info(f"RPC: attempting inference (RAM: {mem_reason})")
        rpc_start = time.time()
        try:
            result = call_llamacpp_rpc(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                timeout=RPC_INFERENCE_TIMEOUT,
            )
            rpc_duration = time.time() - rpc_start
            if result and len(result.strip()) > 20:
                logger.info(f"RPC: success ({len(result)} chars, {rpc_duration:.1f}s)")
                log_call("llamacpp-rpc", task, prompt, result, rpc_duration)
                return result
            logger.warning("RPC: returned empty or too-short response, falling back")
        except Exception as e:
            logger.warning(f"RPC: call failed ({e}), falling back")
    else:
        logger.warning(f"RPC: skipping — {mem_reason}")

    # ── Step 2: Fallback to local Ollama (solo, no RPC) ─────────────────
    logger.info(f"Falling back to local Ollama ({RPC_FALLBACK_OLLAMA_MODEL})...")
    try:
        result = call_ollama(
            prompt=prompt,
            model=RPC_FALLBACK_OLLAMA_MODEL,
            timeout=300 if isinstance(timeout, int) else 300,
        )
        if result and len(result.strip()) > 10:
            logger.info(f"Ollama fallback: success ({len(result)} chars)")
            log_call(RPC_FALLBACK_OLLAMA_MODEL, task, prompt, result, 0)
            return result
        logger.warning("Ollama fallback: returned empty, trying cloud...")
    except Exception as e:
        logger.warning(f"Ollama fallback: failed ({e}), trying cloud...")

    # ── Step 3: Final fallback to cloud API (OpenRouter free tier) ───────
    if skip_cloud_fallback:
        logger.info("Cloud fallback skipped (caller has own cloud strategy)")
        return ""

    logger.info(f"Final fallback to cloud ({RPC_FALLBACK_CLOUD_MODEL})...")
    try:
        result = call_openrouter(
            model=RPC_FALLBACK_CLOUD_MODEL,
            prompt=prompt,
            task=task,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            fallback_chain=[OR_FALLBACK_MODEL],
            timeout=120 if isinstance(timeout, int) else 120,
            sensitivity=sensitivity,
            cloud_consent=cloud_consent,
        )
        if result:
            logger.info(f"Cloud fallback: success ({len(result)} chars)")
            return result
    except Exception as e:
        logger.error(f"Cloud fallback: failed ({e})")

    logger.error("All RPC fallbacks exhausted — returning empty")
    return ""


# ── Opencode Zen (separate provider from OpenRouter) ────────────────────────
def call_opencode(
    prompt: str,
    model: str = "mimo-v2.5-free",
    system_prompt: str = "",
    max_tokens: int = 4000,
    task: str = "general",
    timeout: int = 120,
    temperature: float = 0.0,
    *,
    sensitivity: Sensitivity | str = Sensitivity.PUBLIC,
    cloud_consent: bool = False,
) -> str:
    """
    Call Opencode Zen API — a separate provider from OpenRouter.
    Requires OPENCODE_ZEN_API_KEY to be set.

    Models available (free tier):
      - "mimo-v2.5-free"  (MiMo 2.5)
      - "hy3-free"        (Hy3)
      - "nemotron-3-ultra-free"

    Args:
        prompt: The user message
        model: Model ID (e.g. "mimo-v2.5-free", "hy3-free")
        system_prompt: Optional system message
        max_tokens: Max output tokens
        task: Label for cost tracking
        timeout: HTTP timeout in seconds
        temperature: 0.0 = deterministic, higher = creative

    Returns:
        Generated text, or empty string on failure.
    """
    if not _cloud_policy_allows(
        prompt,
        system_prompt,
        sensitivity=sensitivity,
        cloud_consent=cloud_consent,
    ):
        logger.warning("Opencode Zen request blocked by cloud data policy")
        return ""

    if not OPENCODE_ZEN_API_KEY:
        logger.error("Opencode Zen: no API key set (OPENCODE_ZEN_API_KEY)")
        return ""

    # SECURITY: Scrub PII from cloud-bound prompts
    from utils import scrub_pii
    scrubbed_prompt = scrub_pii(prompt)
    if scrubbed_prompt != prompt:
        logger.info(f"scrubbed PII from {task} prompt")

    messages = []
    if system_prompt:
        scrubbed_system = scrub_pii(system_prompt)
        messages.append({"role": "system", "content": scrubbed_system})
    messages.append({"role": "user", "content": scrubbed_prompt})

    start = time.time()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=5.0),
            headers={
                "Authorization": f"Bearer {OPENCODE_ZEN_API_KEY}",
                "Content-Type": "application/json",
            },
        ) as client:
            resp = client.post(
                f"{OPENCODE_ZEN_URL.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            duration = time.time() - start
            log_call(model, task, prompt, text, duration)
            logger.info(
                f"Opencode Zen ({model}): {len(text)} chars in {duration:.1f}s"
            )
            return text
        else:
            logger.error(f"Opencode Zen: HTTP {resp.status_code}: {resp.text[:200]}")
            return ""
    except httpx.TimeoutException:
        logger.error(f"Opencode Zen: timed out after {timeout}s")
        return ""
    except Exception as e:
        logger.error(f"Opencode Zen: call failed: {e}")
        return ""


def is_opencode_healthy() -> bool:
    """Check if Opencode Zen API is reachable with the configured key."""
    if not OPENCODE_ZEN_API_KEY:
        return False
    try:
        # Call the models endpoint as a lightweight health check
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(
                f"{OPENCODE_ZEN_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {OPENCODE_ZEN_API_KEY}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


# ── Utility ──────────────────────────────────────────────────────────────────
def response_cache_key(model: str, prompt: str) -> str:
    return hashlib.md5(f"{model}:{prompt}".encode()).hexdigest()


def is_ollama_healthy(url: str | None = None) -> bool:
    """Check if Ollama is running at the given URL."""
    target = url or OLLAMA_URL
    try:
        resp = requests.get(f"{target}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def is_orangepi_ollama_healthy() -> bool:
    """Check if Orange Pi 5 Ollama is running."""
    return is_ollama_healthy(OLLAMA_ORANGEPI_URL)


def is_agy_healthy(model: str = "flash") -> bool:
    """Check if agy CLI is functional."""
    import subprocess
    try:
        result = subprocess.run(
            [AGENTAPI_BIN, "--model", _resolve_agy_model(model), "--print", "Say OK"],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0 and len(result.stdout.strip()) > 0
    except Exception:
        return False


# ── Hack Club AI (separate provider via ai.hackclub.com) ─────────────────────
# Shared OpenRouter proxy with a $25.26/24h shared pool. Reserved as a LAST
# resort fallback after all free-tier providers (OpenRouter + Opencode Zen) fail.


def call_hackclub(
    prompt: str,
    model: str = "qwen/qwen3-32b",
    system_prompt: str = "",
    max_tokens: int = 4000,
    task: str = "general",
    timeout: int = 120,
    temperature: float = 0.0,
    *,
    sensitivity: Sensitivity | str = Sensitivity.PUBLIC,
    cloud_consent: bool = False,
) -> str:
    """
    Call Hack Club AI (ai.hackclub.com) — an OpenRouter-like shared proxy.
    Uses the shared pool ($25.26/day budget). Use sparingly; only as a last resort.

    SECURITY: PII is scrubbed before sending to the cloud API.

    Models (tested):
      - "qwen/qwen3-32b"       — default, cheap, fast (default)
      - "qwen/qwen3-235b-a22b" — stronger, for study guides

    Args:
        prompt: The user message
        model: Model ID
        system_prompt: Optional system message
        max_tokens: Max output tokens
        task: Label for cost tracking
        timeout: HTTP timeout in seconds
        temperature: 0.0 = deterministic, higher = creative

    Returns:
        Generated text, or empty string on failure.
    """
    from config import HACKCLUB_AI_API_KEY, HACKCLUB_AI_BASE_URL

    if not _cloud_policy_allows(
        prompt,
        system_prompt,
        sensitivity=sensitivity,
        cloud_consent=cloud_consent,
    ):
        logger.warning("Hack Club AI request blocked by cloud data policy")
        return ""

    if not HACKCLUB_AI_API_KEY:
        logger.error("Hack Club AI: no API key set (HACKCLUB_AI_API_KEY)")
        return ""

    # SECURITY: Scrub PII from cloud-bound prompts
    from utils import scrub_pii
    scrubbed_prompt = scrub_pii(prompt)
    if scrubbed_prompt != prompt:
        logger.info(f"scrubbed PII from {task} prompt")

    messages = []
    if system_prompt:
        scrubbed_system = scrub_pii(system_prompt)
        messages.append({"role": "system", "content": scrubbed_system})
    messages.append({"role": "user", "content": scrubbed_prompt})

    start = time.time()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=5.0),
            headers={
                "Authorization": f"Bearer {HACKCLUB_AI_API_KEY}",
                "Content-Type": "application/json",
            },
        ) as client:
            resp = client.post(
                f"{HACKCLUB_AI_BASE_URL.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            raw_content = data["choices"][0]["message"].get("content")
            if raw_content is None:
                # Some models (e.g. deepseek-style reasoning) put output in reasoning field
                raw_content = data["choices"][0]["message"].get("reasoning", "")
            text = raw_content.strip() if raw_content else ""
            duration = time.time() - start
            log_call(model, task, scrubbed_prompt, text, duration)
            logger.info(
                f"Hack Club AI ({model}): {len(text)} chars in {duration:.1f}s"
            )
            return text
        else:
            logger.error(f"Hack Club AI: HTTP {resp.status_code}: {resp.text[:200]}")
            return ""
    except httpx.TimeoutException:
        logger.error(f"Hack Club AI: timed out after {timeout}s")
        return ""
    except Exception as e:
        logger.error(f"Hack Club AI: call failed: {e}")
        return ""


# ── Health Check on Import ─────────────────────────────────────────────────
# Removed to speed up import time. Health checks should be called lazily.
