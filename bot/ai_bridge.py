"""Privacy-preserving bridge between Telegram chat and inference providers.

Telegram messages, conversation history, digests, and retrieved notes are
personal data. They therefore use owner-controlled Ollama/llama.cpp nodes by
default. Cloud inference is possible only when data is verified non-PII and
explicitly approved (e.g. via smart auto-routing or command prefixes), and
never receives private files or unscrubbed local history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from activity_log import log_llm_call
from bot.state import load_state, update_state
from config import BOT_CONTEXT_FILE, CHAT_HISTORY_DIR, LATEST_DIGEST_FILE, RESPONSE_TIMEOUT
from llm_router import (
    InferenceResult,
    InferenceStatus,
    Sensitivity,
    call_agy_result,
    call_local_rpc_result,
    call_ollama_result,
    call_openrouter_result,
    is_valid_response,
)
from bot.smart_router import classify_query
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

MODE_PROMPTS = {
    "default": (
        "You are Sanel's private personal assistant. Be concise, clear, helpful, and well-structured.\n"
        "TELEGRAM FORMATTING RULES:\n"
        "• Telegram does NOT support LaTeX. Never use LaTeX syntax or dollar signs ($ or $$).\n"
        "• Write all formulas and equations using clean plain text and Unicode characters (e.g. KE = ½mv², x = 4, x² + y² = r², √(x), ∫ f(x) dx, Δt → 0, ≤, ≥, ≠, ±, ·, ×).\n"
        "• Use bold for headings/emphasis, and <pre> code blocks for scripts/data tables."
    ),
    "tutor": (
        "You are Sanel's Socratic study coach and tutor. Never give the final answer or solution "
        "immediately. Instead, guide the user step-by-step, ask intuitive leading questions, "
        "break complex concepts into digestible pieces, and praise active learning.\n"
        "TELEGRAM FORMATTING: Never use raw LaTeX or dollar signs ($ or $$). Write all math using clean Unicode symbols."
    ),
    "quick": (
        "You are a rapid-reference assistant. Deliver direct, minimal answers with zero "
        "conversational filler. Use concise bullet points, exact formulas, or code snippets only. "
        "Keep responses brief.\n"
        "TELEGRAM FORMATTING: Never use raw LaTeX or dollar signs ($ or $$). Write all math using clean Unicode symbols."
    ),
    "drill": (
        "You are an SAT and AP Exam drill coach. Generate challenging practice questions, "
        "point out common test traps, provide timed problem-solving strategies, and explain "
        "the fastest path to the correct solution.\n"
        "TELEGRAM FORMATTING: Never use raw LaTeX or dollar signs ($ or $$). Write all math using clean Unicode symbols."
    ),
}


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


def _session_history_path(chat_id: int) -> Path:
    try:
        safe_chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        safe_chat_id = "0"
    return Path(CHAT_HISTORY_DIR) / f"chat_history_{safe_chat_id}_session.json"


def clear_session(chat_id: int) -> bool:
    """Clear active conversation session memory."""
    path = _session_history_path(chat_id)
    try:
        if path.exists():
            path.unlink()
        return True
    except Exception as exc:
        logger.warning("Failed to clear session: %s", exc)
        return False


def get_session_turns(chat_id: int, max_turns: int = 8) -> list[dict]:
    """Retrieve recent conversation session turns."""
    path = _session_history_path(chat_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data[-max_turns:]
    except Exception:
        pass
    return []


def append_session_turn(chat_id: int, user_msg: str, bot_resp: str) -> None:
    """Append turn to active session window."""
    path = _session_history_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    turns = get_session_turns(chat_id, max_turns=20)
    clean_bot_resp = re.sub(r"\n\n_\(Generated by:.*?\)_", "", bot_resp).strip()
    turns.append({
        "user": user_msg[:2000],
        "assistant": clean_bot_resp[:3000],
        "ts": time.time(),
    })
    turns = turns[-12:]
    try:
        path.write_text(json.dumps(turns, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to write session turn: %s", exc)


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


def _selected_mode(chat_id: int) -> str:
    try:
        state = load_state()
        modes = state.get("user_modes", {}) if isinstance(state, dict) else {}
        value = modes.get(str(chat_id), "default") if isinstance(modes, dict) else "default"
        return value if isinstance(value, str) and value in MODE_PROMPTS else "default"
    except Exception:
        return "default"


def _is_code_request(text: str) -> bool:
    """Detect if prompt is primarily a coding/scripting task."""
    lowered = text.lower()
    code_keywords = (
        "def ", "class ", "function ", "import ", "const ", "let ", "var ",
        "python", "javascript", "bash", "shell", "sql", "regex", "script",
        "bug", "traceback", "syntax error", "refactor", "algorithm", "git",
    )
    return "```" in text or any(k in lowered for k in code_keywords)


def _is_heavy_reasoning(text: str) -> bool:
    """Detect if prompt benefits from a larger 70B+ model."""
    lowered = text.lower()
    reasoning_keywords = (
        "explain", "derive", "proof", "prove", "integral", "derivative",
        "calculus", "physics", "sat", "ap ", "essay", "thesis", "step-by-step",
        "solve", "compare and contrast", "why does", "how does",
    )
    return any(k in lowered for k in reasoning_keywords)


def _is_basic_or_local_query(text: str) -> bool:
    """Detect if a message is a simple greeting, quick conversion, or local inquiry."""
    clean = text.strip().lower()
    if len(clean.split()) <= 4:
        greetings = {"hi", "hello", "hey", "yo", "sup", "howdy", "good morning", "good evening", "thanks", "thank you"}
        if clean in greetings:
            return True
    basic_patterns = [
        r"^(?:what time|what date|what day|who are you|what can you do)",
        r"^(?:convert \d+|calculate \d+\s*[\+\-\*\/]\s*\d+)",
        r"^(?:my tasks|what do i have|schedule|digest|status|server)",
    ]
    return any(re.search(p, clean) for p in basic_patterns)


def _build_local_prompt(
    user_message: str,
    chat_id: int,
    topic: str,
    *,
    mode: str = "default",
    reply_context: str = "",
) -> tuple[str, Path]:
    history_file = _history_path(chat_id, topic)
    history = _read_private_context(history_file, "", limit=3_000)
    brain = _read_private_context(BOT_CONTEXT_FILE, "No offline memory is available.")
    digest = _read_private_context(LATEST_DIGEST_FILE, "No recent digest is available.")

    # Format active session dialogue for multi-turn continuity
    session_turns = get_session_turns(chat_id, max_turns=6)
    session_dialogue = ""
    if session_turns:
        dialogue_lines = [
            f"User: {turn['user']}\nAssistant: {turn['assistant']}"
            for turn in session_turns
        ]
        session_dialogue = "\n\n".join(dialogue_lines)

    retrieval_context = ""
    try:
        from scrapers.semantic_retrieval import get_context_for_prompt

        retrieved = get_context_for_prompt(user_message, top_k=5)
        if isinstance(retrieved, str) and "SEMANTIC RETRIEVAL" in retrieved:
            retrieval_context = retrieved[-12_000:]
    except Exception as exc:
        logger.warning("Local semantic retrieval failed: %s", type(exc).__name__)

    notion_tasks_context = ""
    try:
        from scrapers.notion_client import get_notion_tasks_summary
        notion_tasks_context = get_notion_tasks_summary(max_tasks=12)
    except Exception as exc:
        logger.debug("Local Notion tasks context unavailable: %s", type(exc).__name__)

    mode_instruction = MODE_PROMPTS.get(mode, MODE_PROMPTS["default"])
    system = (
        f"{mode_instruction}\n\n"
        "UNIFIED KNOWLEDGE BASE & RETRIEVAL:\n"
        "• You have direct semantic access to Sanel's unified notes across OneNote, Google Drive, Canvas, and classroom study guides in 'Retrieved local notes'.\n"
        "• When answering questions based on these notes, cite the source clearly (e.g. 'According to your OneNote notes...', 'From your Canvas Physics syllabus...').\n\n"
        "EXECUTION BOUNDARY:\n"
        "• You cannot execute commands directly from chat. For server actions, direct the user to a vetted bot command such as /server. Never emit BASH tool tags.\n\n"
        f"Conversation topic: {topic}\n\n"
        f"Local memory:\n{brain}\n\n"
        f"Active Notion tasks:\n{notion_tasks_context or 'None'}\n\n"
        f"Latest local digest:\n{digest}\n"
    )

    prompt_parts = [system]
    if session_dialogue:
        prompt_parts.append(f"Recent session dialogue:\n{session_dialogue}")
    elif history:
        prompt_parts.append(f"Conversation history:\n{history}")

    if retrieval_context:
        prompt_parts.append(f"Retrieved local notes:\n{retrieval_context}")

    if reply_context:
        prompt_parts.append(f"[In direct reply to message: \"{reply_context}\"]")

    prompt_parts.append(f"User: {user_message}")
    prompt = "\n\n".join(prompt_parts)
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
    force_route: str = "auto",
    reply_context: str = "",
) -> str:
    """Generate a response with privacy verification, smart auto-routing, and session continuity."""
    if not isinstance(user_message, str) or not user_message.strip():
        return "⚠️ Please send a non-empty message."

    is_public, _, pii_types = check_pii(user_message)
    selected_mode = _selected_mode(chat_id)
    selected_model = _selected_model(chat_id)
    decision = classify_query(user_message)
    active_mode = selected_mode if selected_mode != "default" else decision.mode

    # 1. PII Safety & Basic Question Enforcement
    if not is_public or decision.target_engine == "local" or force_route == "local" or selected_model in ("local", "ollama", "rpc"):
        route_to_cloud = False
        await _edit_status(context, chat_id, status_msg, "🏠 Answering with fast local model…")
    elif force_route in ("cloud", "code", "flash", "pro") or selected_model.startswith("openrouter:") or (cloud_consent and selected_model == "auto"):
        route_to_cloud = True
    else:
        route_to_cloud = False
        await _edit_status(context, chat_id, status_msg, "🛡️ Using private local inference.")

    topic = await detect_topic(user_message, chat_id)
    local_prompt, history_file = _build_local_prompt(
        user_message,
        chat_id,
        topic,
        mode=active_mode,
        reply_context=reply_context,
    )

    result: InferenceResult
    model_label = "local"

    if route_to_cloud:
        prefer_agy = (
            selected_model in ("auto", "flash", "pro", "gemini", "agy", "agy:flash", "agy:pro")
            or force_route in ("agy", "flash", "pro")
        ) and not selected_model.startswith("openrouter:")

        if prefer_agy:
            agy_alias = "pro" if (selected_model in ("pro", "agy:pro") or _is_heavy_reasoning(user_message)) else "flash"
            display_name = "Gemini 3.7 Flash" if agy_alias == "flash" else "Gemini 3.1 Pro"
            await _edit_status(context, chat_id, status_msg, f"☁️ Thinking with Google {display_name}…")
            try:
                result = await asyncio.to_thread(
                    call_agy_result,
                    prompt=local_prompt,
                    model=agy_alias,
                    timeout=120,
                    sensitivity=Sensitivity.PUBLIC,
                    cloud_consent=True,
                )
            except Exception as exc:
                logger.warning("AGY inference failed: %s", exc)
                result = InferenceResult(InferenceStatus.ERROR)

            if result.ok and is_valid_response(result.text):
                model_label = f"⚡ {result.model or display_name} (Google)"
            else:
                logger.info("AGY returned no usable output; falling back to OpenRouter/local")
                result = InferenceResult(InferenceStatus.UNAVAILABLE)

        if not result.ok:
            # Fallback or explicit OpenRouter request
            if force_route == "code" or _is_code_request(user_message):
                target_model = "qwen/qwen3-coder:free"
                display_name = "Qwen Coder"
            elif selected_model.startswith("openrouter:"):
                target_model = selected_model.removeprefix("openrouter:")
                display_name = target_model.split("/")[-1].replace(":free", "")
            elif _is_heavy_reasoning(user_message):
                target_model = "meta-llama/llama-3.3-70b-instruct:free"
                display_name = "Llama 3.3 70B"
            else:
                target_model = "meta-llama/llama-3.3-70b-instruct:free"
                display_name = "Llama 3.3 70B"

            await _edit_status(context, chat_id, status_msg, f"☁️ Querying {display_name}…")
            try:
                result = await asyncio.to_thread(
                    call_openrouter_result,
                    model=target_model,
                    prompt=user_message,
                    task=f"chat-{topic}",
                    max_tokens=4_000,
                    timeout=RESPONSE_TIMEOUT,
                    sensitivity=Sensitivity.PUBLIC,
                    cloud_consent=True,
                )
            except Exception as exc:
                logger.warning("OpenRouter inference failed: %s", exc)
                result = InferenceResult(InferenceStatus.ERROR)

            if result.ok and is_valid_response(result.text):
                model_label = f"⚡ {display_name} (Cloud)"
            if not result.ok:
                # Try Opencode Zen
                from llm_router import call_hackclub, call_opencode
                try:
                    logger.info("OpenRouter unavailable, trying Opencode Zen...")
                    oc_resp = await asyncio.to_thread(
                        call_opencode,
                        prompt=user_message,
                        model="mimo-v2.5-free",
                        task=f"chat-{topic}",
                        sensitivity=Sensitivity.PUBLIC,
                        cloud_consent=True,
                    )
                    if oc_resp and is_valid_response(oc_resp):
                        result = InferenceResult.success(oc_resp, provider="opencode", model="MiMo 2.5")
                        model_label = "⚡ MiMo 2.5 (Opencode)"
                except Exception as exc:
                    logger.warning("Opencode Zen inference failed: %s", exc)

            if not result.ok:
                # Try Hack Club AI proxy (Last resort online)
                try:
                    logger.info("Opencode unavailable, trying Hack Club AI (Last Resort)...")
                    hc_resp = await asyncio.to_thread(
                        call_hackclub,
                        prompt=user_message,
                        model="qwen/qwen3-32b",
                        task=f"chat-{topic}",
                        sensitivity=Sensitivity.PUBLIC,
                        cloud_consent=True,
                    )
                    if hc_resp and is_valid_response(hc_resp):
                        result = InferenceResult.success(hc_resp, provider="hackclub", model="Qwen 3 32B")
                        model_label = "⚡ Qwen 3 32B (HackClub)"
                except Exception as exc:
                    logger.warning("Hack Club AI inference failed: %s", exc)

            if not result.ok:
                # Final silent fallback to local cluster
                logger.info("All online providers exhausted; falling back to local cluster")
                await _edit_status(context, chat_id, status_msg, "🛡️ Falling back to private local cluster…")
                result = await _run_local(local_prompt)
                log_llm_call("local-rpc", "chat", 0, is_local=True)
                model_label = "🏠 Local Cluster (Fallback)"
    else:
        result = await _run_local(local_prompt)
        log_llm_call("local-rpc", "chat", 0, is_local=True)
        model_label = "🏠 Local Cluster (Private)"

    if not result.ok or not is_valid_response(result.text):
        logger.warning("Inference did not produce usable output (status=%s)", result.status.value)
        return _LOCAL_UNAVAILABLE_MESSAGE

    response = _render_command_suggestions(result.text.strip())
    response += f"\n\n_(Generated by: `{model_label}`)_"

    try:
        append_session_turn(chat_id, user_message, response)
        _append_history(history_file, user_message, response)
    except OSError:
        logger.warning("Unable to persist chat history", exc_info=True)

    return response
