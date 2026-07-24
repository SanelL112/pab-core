import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the bot directory
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Base Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── API Keys ──────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
SANEL_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))  # Alias for backward compatibility

CONVERSATION_ID = os.getenv("CONVERSATION_ID", "")
SUDO_PASSWORD = os.getenv("SUDO_PASSWORD", "")
GROUPME_TOKEN = os.getenv("GROUPME_TOKEN", "")
GROUPME_GROUP_ID = os.getenv("GROUPME_GROUP_ID", "")

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

# ── Feature Flags ─────────────────────────────────────────────────────────────
USE_COMPOSIO = os.getenv("USE_COMPOSIO", "True").lower() in ("true", "1", "yes", "t")

# ── Safe Roots ────────────────────────────────────────────────────────────────
# Allowed directories for read-only bash commands (e.g. cat, grep)
SAFE_BASH_ROOTS = [
    os.path.abspath(os.path.dirname(__file__))
]

# Directories containing application logs.  Keep this separate from the BASH
# read allowlist so observability configuration cannot broaden command access.
LOG_SCAN_DIRS = [
    Path(directory).expanduser()
    for directory in os.getenv("LOG_SCAN_DIRS", str(BASE_DIR / "logs")).split(os.pathsep)
    if directory
]

# Validate required environment variables at startup
_missing_vars = []
if not OPENROUTER_API_KEY:
    _missing_vars.append("OPENROUTER_API_KEY")
if not TELEGRAM_BOT_TOKEN:
    _missing_vars.append("TELEGRAM_BOT_TOKEN")
if TELEGRAM_CHAT_ID == 0:
    _missing_vars.append("TELEGRAM_CHAT_ID")
if not CONVERSATION_ID:
    _missing_vars.append("CONVERSATION_ID")

if _missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(_missing_vars)}. Please set them in .env file.")

# ── External Services ─────────────────────────────────────────────────────────
AGENTAPI_BIN = os.getenv("AGENTAPI_BIN", "/home/sanel/.local/bin/agy")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_LOCAL_URL = os.getenv("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
OLLAMA_ORANGEPI_URL = os.getenv("OLLAMA_ORANGEPI_URL", "http://10.10.10.2:11434")  # Orange Pi 5 via ethernet
OPENCODE_ZEN_API_KEY = os.getenv("OPENCODE_ZEN_API_KEY", "")
OPENCODE_ZEN_URL = os.getenv("OPENCODE_ZEN_URL", "https://opencode.ai/zen/v1")
OPENCODE_ZEN_MODEL = os.getenv("OPENCODE_ZEN_MODEL", "mimo-v2.5-free")
RESPONSE_TIMEOUT = 300  # seconds to wait for a reply
HACKCLUB_AI_API_KEY = os.getenv("HACKCLUB_AI_API_KEY", "")
HACKCLUB_AI_BASE_URL = os.getenv("HACKCLUB_AI_BASE_URL", "https://ai.hackclub.com/proxy/v1")

# ── OpenRouter Models ─────────────────────────────────────────────────────────
OR_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"  # Working, free, 1M context
OR_FALLBACK_MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"  # Smaller, free, less rate-limited
OR_THIRD_MODEL = "tencent/hy3:free"  # Backup free tier model

# ── File Paths ────────────────────────────────────────────────────────────────
TOKEN_PATH = BASE_DIR / "token.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
STATE_FILE = BASE_DIR / "state.json"
LATEST_DIGEST_FILE = BASE_DIR / "latest_digest.txt"
CURATED_BRAIN_FILE = BASE_DIR / "curated_brain.md"
MEGA_INDEX_FILE = BASE_DIR / "mega_index.md"
BOT_CONTEXT_FILE = BASE_DIR / "bot_context.txt"
NIGHTLY_QUEUE_FILE = BASE_DIR / "nightly_queue.json"
COST_LOG_FILE = BASE_DIR / "llm_cost_log.json"
CORRELATION_GRAPH_FILE = BASE_DIR / "correlation_graph.json"
COMBINED_SUMMARIES_FILE = CACHE_DIR / "combined_summaries.txt"
PDF_EXPORTS_FILE = CACHE_DIR / "pdf_exports.txt"
BACKUP_DIR = BASE_DIR / "backups"
ARCHIVE_DIR = BASE_DIR / "offline_archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# ── Orange Pi 5 NFS mounts ───────────────────────────────────────────────────
ORANGEPI_CACHE_DIR = Path("/mnt/orangepi/cache")
ORANGEPI_BACKUP_DIR = Path("/mnt/orangepi/backups")
import threading as _threading
def _safe_mkdir(path: Path) -> None:
    """mkdir with a 3s timeout — NFS mount can hang."""
    result = []

    def _do() -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            result.append(True)
        except OSError:
            pass

    t = _threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=3.0)
    # If it didn't finish in 3s, leave it for the call-site fallback

_safe_mkdir(ORANGEPI_CACHE_DIR)
_safe_mkdir(ORANGEPI_BACKUP_DIR)

# ── Rotation Limits ──────────────────────────────────────────────────────────
MAX_COMBINED_SUMMARIES_CHARS = 50_000      # ~7 days of digests
MAX_MEGA_INDEX_CHARS = 100_000
MAX_CURATED_BRAIN_CHARS = 50_000
MAX_SEEN_TASKS = 200
MAX_CHAT_HISTORY_KB = 50
DIGEST_INTERVAL_SECONDS = 14400            # 4 hours
WATCHDOG_INTERVAL_SECONDS = 1800           # 30 minutes

# ── RPC OOM Protection ────────────────────────────────────────────────────
# Minimum free RAM (MB) on server and Orange Pi before attempting RPC.
# If either machine has less, RPC is skipped and fallback is used.
RPC_SERVER_MIN_FREE_MB = int(os.getenv("RPC_SERVER_MIN_FREE_MB", "1500"))
RPC_WORKER_MIN_FREE_MB = int(os.getenv("RPC_WORKER_MIN_FREE_MB", "800"))

# Max RSS (MB) for llama-server RPC process before auto-kill + fallback.
RPC_SERVER_MAX_RSS_MB = int(os.getenv("RPC_SERVER_MAX_RSS_MB", "4000"))

# RPC timeouts (seconds).
RPC_STARTUP_TIMEOUT = int(os.getenv("RPC_STARTUP_TIMEOUT", "120"))
RPC_INFERENCE_TIMEOUT = int(os.getenv("RPC_INFERENCE_TIMEOUT", "600"))

# Fallback model chain when RPC fails:
#   1. RPC (primary) → 2. Solo 7B Ollama → 3. Cloud OpenRouter (free)
RPC_FALLBACK_OLLAMA_MODEL = os.getenv(
    "RPC_FALLBACK_OLLAMA_MODEL",
    "hf.co/Qwen/Qwen2-0.5B-Instruct-GGUF:latest"
)
RPC_FALLBACK_CLOUD_MODEL = os.getenv(
    "RPC_FALLBACK_CLOUD_MODEL",
    "nvidia/nemotron-3-ultra-550b-a55b:free"
)

# ── Backup ────────────────────────────────────────────────────────────────────
BACKUP_RETENTION_DAYS = 30
BACKUP_FILES = [
    "state.json", "curated_brain.md", "mega_index.md",
    "bot_context.txt", "latest_digest.txt", "correlation_graph.json",
    "llm_cost_log.json", "nightly_queue.json",
]
