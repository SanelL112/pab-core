"""Privacy-preserving bridge between Telegram chat and inference providers.

Telegram messages, conversation history, digests, and retrieved notes are
personal data.  They therefore use owner-controlled Ollama/llama.cpp nodes by
default.  Cloud inference is possible only through an explicit per-call consent
flag and never receives the private corpus or conversation history.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from activity_log import log_llm_call
from bot.state import load_state
from config import BOT_CONTEXT_FILE, CHAT_HISTORY_DIR, LATEST_DIGEST_FILE, RESPONSE_TIMEOUT
from llm_router import (
    InferenceResult,
    InferenceStatus,
    Sensitivity,
    call_local_rpc_result,
    call_ollama_result,
    call_openrouter_result,
    is_valid_response,
)
from utils import check_pii

logger = logging.getLogger(__name__)

_TOPIC_RE = re.compile(r"^[a-z0-9_]{1,30}$")
_BASH_BLOCK_RE = re.compile(
    r"(?:<BASH>|\[BASH\])(.*?)(?:</BASH>|\[/BASH\])",
    flags=re.IGNORECASE | re.DOTALL,
)
_LOCAL_UNAVAILABLE_MESSAGE = (
    "⚠️ Local inference is currently unavailable. Your request was not sent "
    "to a cloud provider. Please try again in a moment."
)


async def _edit_status(context, chat_id: int, status_msg, text: str) -> None:
    if not context or not status_msg:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=text,
        )
    except Exception:
        # Progress updates are best effort and must not fail inference.
        logger.debug("Unable to update inference progress", exc_info=True)


def _read_private_context(path: str | os.PathLike, fallback: str, limit: int = 12_000) -> str:
    """Read a bounded tail of local context; never return filesystem errors."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return fallback
    if len(text) > limit:
        return "[older local context trimmed]\n" + text[-limit:]
    return text


def _history_path(chat_id: int, topic: str) -> Path:
    try:
        safe_chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        safe_chat_id = "0"
    safe_topic = topic if _TOPIC_RE.fullmatch(topic) else "general"
    return Path(CHAT_HISTORY_DIR) / f"chat_history_{safe_chat_id}_{safe_topic}.txt"


async def detect_topic(message: str, chat_id: int) -> str:
    """Classify a chat topic using only a local Ollama endpoint."""

    try:
        safe_chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        safe_chat_id = "0"
    existing_topics: list[str] = []
    prefix = f"chat_history_{safe_chat_id}_"
    for path in Path(CHAT_HISTORY_DIR).glob(f"{prefix}*.txt"):
        topic = path.stem[len(prefix):]
        if _TOPIC_RE.fullmatch(topic):
            existing_topics.append(topic)

    prompt = (
        "Classify the message into a short topic slug. Reuse one of these exact "
        f"slugs when appropriate: {', '.join(sorted(existing_topics)) or 'none'}. "
        "Otherwise create one or two lowercase words joined by underscores. "
        "Reply with the slug only.\n\nMessage: "
        f"{message}"
    )
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                call_ollama_result,
                prompt,
                model="hf.co/Qwen/Qwen2-0.5B-Instruct-GGUF:latest",
                timeout=25,
            ),
            timeout=30,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("Local topic classification timed out")
        return "general"
    except Exception as exc:
        logger.warning("Local topic classification failed: %s", type(exc).__name__)
        return "general"

    if not result.ok:
        return "general"
    topic = re.sub(r"[^a-z0-9_]", "", result.text.strip().lower().replace(" ", "_"))
    return topic if _TOPIC_RE.fullmatch(topic) else "general"


def _selected_model(chat_id: int) -> str:
    try:
        state = load_state()
        models = state.get("user_models", {}) if isinstance(state, dict) else {}
        value = models.get(str(chat_id), "auto") if isinstance(models, dict) else "auto"
        return value if isinstance(value, str) and value else "auto"
    except Exception:
        return "auto"


def _build_local_prompt(user_message: str, chat_id: int, topic: str) -> tuple[str, Path]:
    history_file = _history_path(chat_id, topic)
    history = _read_private_context(history_file, "", limit=4_000)
    brain = _read_private_context(BOT_CONTEXT_FILE, "No offline memory is available.")
    digest = _read_private_context(LATEST_DIGEST_FILE, "No recent digest is available.")

    retrieval_context = ""
    try:
        from scrapers.semantic_retrieval import get_context_for_prompt

        retrieved = get_context_for_prompt(user_message, top_k=5)
        if isinstance(retrieved, str) and "SEMANTIC RETRIEVAL" in retrieved:
            retrieval_context = retrieved[-12_000:]
    except Exception as exc:
        logger.warning("Local semantic retrieval failed: %s", type(exc).__name__)

    system = (
        "You are Sanel's private personal assistant. This prompt is being handled "
        "on owner-controlled local inference nodes. Be concise, clear, and honest. "
        "You cannot execute commands from your response. For server actions, direct "
        "the user to a vetted bot command such as /server. Never emit BASH tool tags.\n\n"
        f"Conversation topic: {topic}\n\n"
        f"Local memory:\n{brain}\n\n"
        f"Latest local digest:\n{digest}\n"
    )
    prompt = (
        f"{system}\n\nConversation history:\n{history}\n\n"
        f"Retrieved local notes:\n{retrieval_context or 'None'}\n\n"
        f"User: {user_message}"
    )
    return prompt, history_file


def _render_command_suggestions(text: str) -> str:
    """Display model-proposed commands as inert text; never execute them."""

    def replace(match: re.Match) -> str:
        proposed = match.group(1).strip()[:2_000]
        if not proposed:
            return ""
        return (
            "\n\nNo command was run automatically. Proposed command (review manually):\n"
            f"```sh\n{proposed}\n```"
        )

    return _BASH_BLOCK_RE.sub(replace, text)


def _append_history(history_file: Path, user_message: str, response: str) -> None:
    """Append private history with restrictive permissions and bounded growth."""

    history_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if history_file.is_symlink():
        logger.warning("Refusing to write chat history through a symlink")
        return

    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(history_file, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(f"User: {user_message}\nModel: {response}\n\n")
    finally:
        if fd >= 0:
            os.close(fd)

    try:
        if history_file.stat().st_size <= 50_000:
            return
        content = history_file.read_text(encoding="utf-8")
        temp_fd, temp_name = tempfile.mkstemp(dir=history_file.parent, suffix=".tmp")
        try:
            os.fchmod(temp_fd, 0o600)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                temp_fd = -1
                handle.write("[earlier messages trimmed]\n" + content[-40_000:])
            os.replace(temp_name, history_file)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    except (OSError, UnicodeError):
        logger.warning("Unable to rotate chat history", exc_info=True)


async def _run_local(prompt: str) -> InferenceResult:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                call_local_rpc_result,
                prompt=prompt,
                max_tokens=4_000,
                timeout=RESPONSE_TIMEOUT,
                allow_cloud=False,
                sensitivity=Sensitivity.PERSONAL,
            ),
            timeout=float(RESPONSE_TIMEOUT) + 2,
        )
    except (TimeoutError, asyncio.TimeoutError):
        return InferenceResult(
            InferenceStatus.TIMEOUT,
            provider="local-rpc",
            detail="Local inference deadline exceeded",
        )
    except Exception as exc:
        logger.warning("Local inference bridge failed: %s", type(exc).__name__)
        return InferenceResult(
            InferenceStatus.ERROR,
            provider="local-rpc",
            detail=type(exc).__name__,
        )


async def send_to_antigravity_and_wait(
    user_message: str,
    chat_id: int = 0,
    context=None,
    status_msg=None,
    *,
    cloud_consent: bool = False,
) -> str:
    """Generate a response while keeping personal chat data local by default.

    ``cloud_consent`` is deliberately keyword-only and applies to this call only.
    Even with consent, cloud receives only the current non-PII message; history,
    digests, memory, and semantic retrieval remain local.
    """

    if not isinstance(user_message, str) or not user_message.strip():
        return "⚠️ Please send a non-empty message."

    is_public, _, pii_types = check_pii(user_message)
    if not is_public:
        logger.info("PII detected (%s); enforcing local-only inference", ", ".join(pii_types))
        await _edit_status(context, chat_id, status_msg, "🛡️ Keeping this request on local models.")
    else:
        await _edit_status(context, chat_id, status_msg, "🛡️ Using private local inference.")

    topic = await detect_topic(user_message, chat_id)
    local_prompt, history_file = _build_local_prompt(user_message, chat_id, topic)
    selected_model = _selected_model(chat_id)

    result: InferenceResult
    if cloud_consent and is_public and selected_model.startswith("openrouter:"):
        # Explicit consent never authorizes disclosure of local history/corpus.
        model = selected_model.split(":", 1)[1]
        await _edit_status(context, chat_id, status_msg, "☁️ Using the approved cloud model for this message only.")
        result = await asyncio.to_thread(
            call_openrouter_result,
            model=model,
            prompt=user_message,
            task=f"chat-{topic}",
            max_tokens=4_000,
            timeout=RESPONSE_TIMEOUT,
            sensitivity=Sensitivity.PUBLIC,
            cloud_consent=True,
        )
    else:
        if selected_model.startswith(("openrouter:", "agy", "flash", "pro")):
            logger.info("Persistent cloud model preference ignored without per-request consent")
        result = await _run_local(local_prompt)
        log_llm_call("local-rpc", "chat", 0, is_local=True)

    if not result.ok or not is_valid_response(result.text):
        logger.warning("Inference did not produce usable output (status=%s)", result.status.value)
        return _LOCAL_UNAVAILABLE_MESSAGE

    response = _render_command_suggestions(result.text.strip())
    model_label = result.model or result.provider or "local"
    response += f"\n\n_(Generated by: `{model_label}`)_"

    try:
        _append_history(history_file, user_message, response)
    except OSError:
        logger.warning("Unable to persist chat history", exc_info=True)
    return response
