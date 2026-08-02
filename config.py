"""Application settings and filesystem layout.

Importing this module is intentionally side-effect free: it does not create
directories, start threads, or reject configuration for unrelated features.
The bot entry point calls :func:`initialize_runtime` and
:func:`validate_runtime_config` explicitly.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.getenv("PAB_CONFIG_DIR", str(BASE_DIR))).expanduser().resolve()
RUNTIME_DIR = Path(os.getenv("PAB_RUNTIME_DIR", str(BASE_DIR))).expanduser().resolve()
STATE_DIR = Path(os.getenv("PAB_STATE_DIR", str(RUNTIME_DIR))).expanduser().resolve()
LOG_DIR = Path(os.getenv("PAB_LOG_DIR", str(STATE_DIR / "logs"))).expanduser().resolve()
EXPORT_DIR = Path(os.getenv("PAB_EXPORT_DIR", str(RUNTIME_DIR / "exports"))).expanduser().resolve()

_DOTENV_PATH = Path(os.getenv("PAB_ENV_FILE", str(CONFIG_DIR / ".env"))).expanduser()
_FILE_ENV = dotenv_values(_DOTENV_PATH) if _DOTENV_PATH.is_file() else {}
_CONFIG_ERRORS: list[str] = []


def _value(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        value = _FILE_ENV.get(name, default)
    return str(value if value is not None else default)


def get_setting(name: str, default: str = "") -> str:
    """Read a non-secret feature setting from environment or the private config file.

    Legacy modules use this during their gradual migration away from direct
    ``load_dotenv`` calls.  It does not mutate ``os.environ``.
    """
    return _value(name, default)


def _integer(name: str, default: int) -> int:
    raw = _value(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        _CONFIG_ERRORS.append(f"{name} must be an integer")
        return default


def _boolean(name: str, default: bool) -> bool:
    raw = _value(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    _CONFIG_ERRORS.append(f"{name} must be a boolean")
    return default


# Credentials and owner identity.  TELEGRAM_CHAT_ID remains as a compatibility
# alias, while authorization should use TELEGRAM_OWNER_USER_ID.
OPENROUTER_API_KEY = _value("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN = _value("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _integer("TELEGRAM_CHAT_ID", 0)
TELEGRAM_OWNER_USER_ID = _integer("TELEGRAM_OWNER_USER_ID", TELEGRAM_CHAT_ID)
SANEL_CHAT_ID = TELEGRAM_CHAT_ID
CONVERSATION_ID = _value("CONVERSATION_ID")
GROUPME_TOKEN = _value("GROUPME_TOKEN") or _value("GROUPME_ACCESS_TOKEN")
GROUPME_GROUP_ID = _value("GROUPME_GROUP_ID")
NOTION_API_KEY = _value("NOTION_API_KEY")
NOTION_DATABASE_ID = _value("NOTION_DATABASE_ID")

# Retained only so older operator scripts fail gracefully while migrating away
# from password-in-process sudo.  Application code must not consume it.
SUDO_PASSWORD = _value("SUDO_PASSWORD")

# External services.
AGENTAPI_BIN = _value("AGENTAPI_BIN", str(Path.home() / ".local" / "bin" / "agy"))
OLLAMA_URL = _value("OLLAMA_URL", "http://localhost:11434")
OLLAMA_LOCAL_URL = _value("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
OLLAMA_ORANGEPI_URL = _value("OLLAMA_ORANGEPI_URL", "http://10.10.10.2:11434")
PI_CLASSIFIER_URL = _value("PI_CLASSIFIER_URL", "http://127.0.0.1:8080")
PI_CLASSIFIER_TOKEN = _value("PI_CLASSIFIER_TOKEN")
OPENCODE_ZEN_API_KEY = _value("OPENCODE_ZEN_API_KEY")
OPENCODE_ZEN_URL = _value("OPENCODE_ZEN_URL", "https://opencode.ai/zen/v1")
HACKCLUB_AI_API_KEY = _value("HACKCLUB_AI_API_KEY")
HACKCLUB_AI_BASE_URL = _value("HACKCLUB_AI_BASE_URL", "https://ai.hackclub.com/proxy/v1")
RESPONSE_TIMEOUT = _integer("RESPONSE_TIMEOUT", 300)
ENABLE_WEB_RESEARCH = _boolean("ENABLE_WEB_RESEARCH", False)
MAX_WEB_FETCH_BYTES = _integer("MAX_WEB_FETCH_BYTES", 1_000_000)
MAX_WEB_SOURCES = _integer("MAX_WEB_SOURCES", 3)
COMPOSIO_MAX_RESPONSE_BYTES = _integer("COMPOSIO_MAX_RESPONSE_BYTES", 1_000_000)

# Provider/model policy.
OR_DEFAULT_MODEL = _value("OR_DEFAULT_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
OR_FALLBACK_MODEL = _value("OR_FALLBACK_MODEL", "nvidia/nemotron-3-nano-30b-a3b:free")
OR_THIRD_MODEL = _value("OR_THIRD_MODEL", "tencent/hy3:free")

# Mutable/private paths.  Production units set PAB_* roots outside the checkout.
CACHE_DIR = RUNTIME_DIR / "cache"
TOKEN_PATH = CONFIG_DIR / "token.json"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
COMPOSIO_TOKEN_PATH = Path(_value("COMPOSIO_TOKEN_PATH", str(CONFIG_DIR / "mcp-tokens" / "composio.json"))).expanduser()
COMPOSIO_CANVAS_CACHE_FILE = STATE_DIR / "composio_canvas_courses_cache.json"
STATE_FILE = STATE_DIR / "state.json"
RUNTIME_DATABASE = STATE_DIR / "runtime.sqlite3"
LATEST_DIGEST_FILE = STATE_DIR / "latest_digest.txt"
CURATED_BRAIN_FILE = STATE_DIR / "curated_brain.md"
MEGA_INDEX_FILE = STATE_DIR / "mega_index.md"
BOT_CONTEXT_FILE = STATE_DIR / "bot_context.txt"
NIGHTLY_QUEUE_FILE = STATE_DIR / "nightly_queue.json"
COST_LOG_FILE = STATE_DIR / "llm_cost_log.json"
CORRELATION_GRAPH_FILE = STATE_DIR / "correlation_graph.json"
CHAT_HISTORY_DIR = STATE_DIR / "chat_history"
KNOWLEDGE_GAPS_DIR = STATE_DIR / "knowledge_gaps"
STUDY_DATABASE_DIR = STATE_DIR / "study_database"
IMPORTANT_EXTRACTS_FILE = STATE_DIR / "important_extracts.txt"
COMBINED_SUMMARIES_FILE = CACHE_DIR / "combined_summaries.txt"
PDF_EXPORTS_FILE = CACHE_DIR / "pdf_exports.txt"
BACKUP_DIR = STATE_DIR / "backups"
ARCHIVE_DIR = STATE_DIR / "offline_archive"
PRIVATE_STUDY_GUIDES_DIR = EXPORT_DIR / "study_guides"
PRIVATE_RESEARCH_DIR = EXPORT_DIR / "research"
ACTIVITY_LOG_FILE = LOG_DIR / "activity_log.jsonl"

# Optional Orange Pi mounts are never touched during import.
ORANGEPI_CACHE_DIR = Path(_value("ORANGEPI_CACHE_DIR", "/mnt/orangepi/cache"))
ORANGEPI_BACKUP_DIR = Path(_value("ORANGEPI_BACKUP_DIR", "/mnt/orangepi/backups"))

# Rotation and schedules.
MAX_COMBINED_SUMMARIES_CHARS = 50_000
MAX_MEGA_INDEX_CHARS = 100_000
MAX_CURATED_BRAIN_CHARS = 50_000
MAX_SEEN_TASKS = 200
MAX_CHAT_HISTORY_KB = 50
DIGEST_INTERVAL_SECONDS = _integer("DIGEST_INTERVAL_SECONDS", 14_400)
WATCHDOG_INTERVAL_SECONDS = _integer("WATCHDOG_INTERVAL_SECONDS", 1_800)

# Assignment calendar sync.
USE_COMPOSIO = _boolean("USE_COMPOSIO", True)
ASSIGNMENT_CALENDAR_TIMEZONE = _value("ASSIGNMENT_CALENDAR_TIMEZONE", "America/New_York")
ASSIGNMENT_CALENDAR_DATA_DIR = Path(_value(
    "ASSIGNMENT_CALENDAR_DATA_DIR",
    str(RUNTIME_DIR / "assignment-calendar"),
)).expanduser()
ASSIGNMENT_CALDAV_COLLECTION_URL = _value("ASSIGNMENT_CALDAV_COLLECTION_URL")
ASSIGNMENT_CALDAV_USERNAME = _value("ASSIGNMENT_CALDAV_USERNAME")
ASSIGNMENT_CALDAV_PASSWORD = _value("ASSIGNMENT_CALDAV_PASSWORD")
ASSIGNMENT_GOOGLE_CALENDAR_ID = _value("ASSIGNMENT_GOOGLE_CALENDAR_ID")
ASSIGNMENT_GOOGLE_CALENDAR_NAME = _value("ASSIGNMENT_GOOGLE_CALENDAR_NAME", "Assignments")
ASSIGNMENT_GOOGLE_CALENDAR_CREATE_TOOL = _value("ASSIGNMENT_GOOGLE_CALENDAR_CREATE_TOOL")
ASSIGNMENT_GOOGLE_CREATE_TOOL = _value("ASSIGNMENT_GOOGLE_CREATE_TOOL")
ASSIGNMENT_GOOGLE_UPDATE_TOOL = _value("ASSIGNMENT_GOOGLE_UPDATE_TOOL")
ASSIGNMENT_GOOGLE_DELETE_TOOL = _value("ASSIGNMENT_GOOGLE_DELETE_TOOL")
ASSIGNMENT_CALENDAR_DATABASE = ASSIGNMENT_CALENDAR_DATA_DIR / "assignment-calendar.sqlite3"

# RPC resource policy.
RPC_SERVER_MIN_FREE_MB = _integer("RPC_SERVER_MIN_FREE_MB", 1500)
RPC_WORKER_MIN_FREE_MB = _integer("RPC_WORKER_MIN_FREE_MB", 800)
RPC_SERVER_MAX_RSS_MB = _integer("RPC_SERVER_MAX_RSS_MB", 4000)
RPC_STARTUP_TIMEOUT = _integer("RPC_STARTUP_TIMEOUT", 120)
RPC_INFERENCE_TIMEOUT = _integer("RPC_INFERENCE_TIMEOUT", 600)

# Per-attempt ceiling for the Surface orchestrator inside the shared inference
# budget.  Measured cold-cache on the 3-node cluster: a digest-sized prompt
# (~1100 prompt tokens) costs ~66s of prompt eval at ~15 tok/s plus generation,
# for a 438s wall time.  The previous hardcoded 45s therefore timed out on 100%
# of digest calls and every digest was silently served by the 0.5B fallback.
#
# This MUST stay strictly below RPC_INFERENCE_TIMEOUT: call_local_rpc shares one
# deadline across Surface -> Pi Ollama -> Dell Ollama, and _try_ollama returns
# immediately when remaining() <= 0.  Setting it equal to the full budget would
# let a Surface timeout consume everything and silently disable both fallbacks.
RPC_SURFACE_TIMEOUT = _integer("RPC_SURFACE_TIMEOUT", 540)

# Reserve enough of the budget for at least one fallback node to answer.
_RPC_FALLBACK_RESERVE_SECONDS = 60
if RPC_SURFACE_TIMEOUT > RPC_INFERENCE_TIMEOUT - _RPC_FALLBACK_RESERVE_SECONDS:
    RPC_SURFACE_TIMEOUT = max(1, RPC_INFERENCE_TIMEOUT - _RPC_FALLBACK_RESERVE_SECONDS)

# Output ceiling for the digest assembly call.  Generation dominates wall time
# (371s of a 438s run), and an 8-task/13-topic digest used only ~900 tokens, so
# the old 6000 was headroom nobody consumed.
DIGEST_MAX_TOKENS = _integer("DIGEST_MAX_TOKENS", 2000)
RPC_FALLBACK_OLLAMA_MODEL = _value(
    "RPC_FALLBACK_OLLAMA_MODEL", "hf.co/Qwen/Qwen2-0.5B-Instruct-GGUF:latest"
)
RPC_FALLBACK_CLOUD_MODEL = _value("RPC_FALLBACK_CLOUD_MODEL", OR_DEFAULT_MODEL)

BACKUP_RETENTION_DAYS = 30
BACKUP_FILES = [
    "state.json", "curated_brain.md", "mega_index.md", "bot_context.txt",
    "latest_digest.txt", "correlation_graph.json", "llm_cost_log.json",
    "nightly_queue.json", "runtime.sqlite3",
]


def _private_directories() -> Iterable[Path]:
    return (
        CONFIG_DIR, COMPOSIO_TOKEN_PATH.parent, RUNTIME_DIR, STATE_DIR, LOG_DIR, EXPORT_DIR, CACHE_DIR,
        BACKUP_DIR, ARCHIVE_DIR, PRIVATE_STUDY_GUIDES_DIR, PRIVATE_RESEARCH_DIR,
        CHAT_HISTORY_DIR, KNOWLEDGE_GAPS_DIR, STUDY_DATABASE_DIR,
        ASSIGNMENT_CALENDAR_DATA_DIR,
    )


def initialize_runtime() -> None:
    """Create private runtime directories and harden existing secret files."""
    for directory in dict.fromkeys(_private_directories()):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass

    for path in (_DOTENV_PATH, TOKEN_PATH, CREDENTIALS_PATH, COMPOSIO_TOKEN_PATH):
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)


def validate_runtime_config(feature: str = "bot") -> None:
    """Validate only the settings required by the selected runtime feature."""
    errors = list(_CONFIG_ERRORS)
    requirements = {
        "bot": (
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
            ("TELEGRAM_OWNER_USER_ID", TELEGRAM_OWNER_USER_ID),
        ),
        "openrouter": (("OPENROUTER_API_KEY", OPENROUTER_API_KEY),),
        "notion": (
            ("NOTION_API_KEY", NOTION_API_KEY),
            ("NOTION_DATABASE_ID", NOTION_DATABASE_ID),
        ),
    }
    for name, value in requirements.get(feature, ()):
        if not value:
            errors.append(f"{name} is required for {feature}")
    if errors:
        raise ValueError("Invalid configuration: " + "; ".join(dict.fromkeys(errors)))
