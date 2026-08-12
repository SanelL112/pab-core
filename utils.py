"""
utils.py — Shared utilities for rotation, backup, correlation, and safety.
"""
import os
import json
import time
import random
import shutil
import hashlib
import logging
import functools
import subprocess
import tempfile
import threading
import atexit
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import requests

from config import (
    BASE_DIR, CACHE_DIR, ARCHIVE_DIR, BACKUP_DIR, CHAT_HISTORY_DIR, LOG_DIR,
    NIGHTLY_QUEUE_FILE, STATE_DIR,
    STATE_FILE, CURATED_BRAIN_FILE, MEGA_INDEX_FILE,
    COMBINED_SUMMARIES_FILE, CORRELATION_GRAPH_FILE,
    MAX_COMBINED_SUMMARIES_CHARS, MAX_MEGA_INDEX_CHARS,
    MAX_CURATED_BRAIN_CHARS, MAX_SEEN_TASKS, MAX_CHAT_HISTORY_KB,
    BACKUP_FILES, BACKUP_RETENTION_DAYS, SANEL_CHAT_ID,
)
from bot.state import update_state

logger = logging.getLogger(__name__)


# ── Memory Bloat Fixes ───────────────────────────────────────────────────────
def rotate_file_if_needed(filepath: Path, max_chars: int, keep_chars: int = None):
    """Rotate a file if it exceeds max_chars. Keeps the last keep_chars."""
    if not filepath.exists():
        return
    try:
        content = filepath.read_text(encoding="utf-8")
        if len(content) <= max_chars:
            return
        keep = keep_chars or int(max_chars * 0.7)
        # Add rotation marker
        date_str = datetime.now().strftime("%Y-%m-%d")
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        archived = content[:-keep]
        rotated_path = ARCHIVE_DIR / f"{filepath.stem}_{date_str}{filepath.suffix}"
        _atomic_write_private_text(rotated_path, archived)
        # Keep only the recent portion
        new_content = f"[rotated older content to {rotated_path.name}]\n" + content[-keep:]
        _atomic_write_private_text(filepath, new_content)
        logger.info(f"Rotated {filepath.name}: {len(content)} -> {len(new_content)} chars")
    except Exception as e:
        logger.error(f"Rotation failed for {filepath}: {e}")


def enforce_all_rotations():
    """Call this periodically to keep files from growing unbounded."""
    rotate_file_if_needed(COMBINED_SUMMARIES_FILE, MAX_COMBINED_SUMMARIES_CHARS)
    rotate_file_if_needed(MEGA_INDEX_FILE, MAX_MEGA_INDEX_CHARS)
    rotate_file_if_needed(CURATED_BRAIN_FILE, MAX_CURATED_BRAIN_CHARS)

    # Rotate chat history files (per-topic)
    for f in CHAT_HISTORY_DIR.glob("chat_history_*.txt"):
        rotate_file_if_needed(f, MAX_CHAT_HISTORY_KB * 1024)

    # Cap state.json seen_tasks
    try:
        changed = False
        def cap_seen_tasks(state):
            nonlocal changed
            seen = state.get("seen_tasks", [])
            if len(seen) > MAX_SEEN_TASKS:
                state["seen_tasks"] = seen[-MAX_SEEN_TASKS:]
                changed = True
        update_state(cap_seen_tasks)
        if changed:
            logger.info("Capped seen_tasks in state.json")
    except Exception as e:
        logger.error(f"Failed to cap seen_tasks: {e}")


# ── BASH Safety (Fix #7) ─────────────────────────────────────────────────────
# STRICT ALLOWLIST: Only these exact command patterns are permitted.
# Each entry is a tuple: (base_command, allowed_args_pattern)
# args_pattern can be:
#   - [] : no arguments allowed
#   - ["<fixed_arg>"] : exactly these fixed arguments
#   - ["*"] : any single argument allowed (but still validated)
#   - ["<path>"] : any single path argument
#   - ["<n>", "<path>"] : two arguments (number then path)
ALLOWED_COMMAND_TEMPLATES = [
    # System info - read only, no args
    ("free", []),
    ("uptime", []),
    ("df", ["-h"]),
    ("ps", ["aux"]),
    ("top", ["-bn1"]),
    ("lscpu", []),
    ("lsblk", []),
    ("whoami", []),
    ("id", []),
    ("uname", ["-a"]),
    ("hostname", []),
    ("echo", ["*"]),
    ("printf", ["*"]),
    # File operations (read-only)
    ("cat", ["<path>"]),
    ("head", ["-n", "<n>", "<path>"]),
    ("tail", ["-n", "<n>", "<path>"]),
    ("wc", ["-l", "<path>"]),
    ("grep", ["-r", "<pattern>", "<path>"]),
    ("grep", ["-i", "<pattern>", "<path>"]),
    ("grep", ["<pattern>", "<path>"]),
    ("rg", ["<pattern>", "<path>"]),
    ("find", ["<path>", "-name", "<pattern>"]),
    ("ls", ["-la", "<path>"]),
    ("ls", ["<path>"]),
    ("stat", ["<path>"]),
    ("file", ["<path>"]),
    ("du", ["-sh", "<path>"]),
    ("diff", ["-u", "<path>", "<path>"]),
    ("sort", ["<path>"]),
    ("uniq", ["<path>"]),
    ("cut", ["-d", "<delim>", "-f", "<fields>", "<path>"]),
    ("tr", ["<set1>", "<set2>"]),
    # Network (read-only, no output redirection)
    ("ping", ["-c", "<n>", "<host>"]),
    ("nslookup", ["<host>"]),
    ("dig", ["<host>"]),
    ("host", ["<host>"]),
    ("ss", ["-tuln"]),
    ("netstat", ["-tuln"]),
    ("lsof", ["-i"]),
    # Git (read-only commands only)
    ("git", ["status"]),
    ("git", ["log", "--oneline", "-n", "<n>"]),
    ("git", ["diff"]),
    ("git", ["show", "<commit>"]),
    ("git", ["branch"]),
    ("git", ["branch", "-a"]),
    # Python/Node tools
    ("pip", ["list"]),
    ("pip", ["show", "<package>"]),
    ("npm", ["list"]),
    ("node", ["--version"]),
    ("npx", ["--version"]),
    # Archive/Compression (read-only)
    ("tar", ["-tzf", "<path>"]),
    ("tar", ["-tf", "<path>"]),
    ("gzip", ["-t", "<path>"]),
    ("gunzip", ["-t", "<path>"]),
    ("unzip", ["-l", "<path>"]),
    # Process management (read-only)
    ("kill", ["-0", "<pid>"]),  # only signal 0 (check existence)
    ("pkill", ["-0", "<pattern>"]),
    ("pgrep", ["<pattern>"]),
    ("pidof", ["<name>"]),
    # Ollama
    ("ollama", ["list"]),
    ("ollama", ["show", "<model>"]),
    # Agy
    ("agy", ["--version"]),
    # Pandoc
    ("pandoc", ["--version"]),
    # PDF tools
    ("pdftotext", ["<path>"]),
    ("pdfinfo", ["<path>"]),
    # Tesseract
    ("tesseract", ["<path>", "stdout"]),
]

# Patterns that are NEVER allowed (blocklist as safety net)
BLOCKED_PATTERNS = [
    'rm -rf /', 'mkfs', 'dd if=', ':(){', 'fork bomb',
    '> /dev/sda', 'chmod -R 777 /', 'shutdown', 'reboot',
    'init 0', 'poweroff', 'halt',
    'curl.*[|].*bash', 'wget.*[|].*sh',
    'sudo', 'su ', 'doas', 'passwd', 'chown', 'chmod',
    'mount', 'umount', 'fdisk', 'parted', 'mkfs',
    'iptables', 'ufw', 'firewall-cmd',
    'systemctl start', 'systemctl stop', 'systemctl restart', 'systemctl enable', 'systemctl disable',
    'service ', '/etc/init.d/',
    'reboot', 'poweroff', 'halt', 'shutdown',
    'crontab', 'at ', 'batch',
    'ssh', 'scp', 'rsync', 'sftp',
    'docker', 'podman', 'kubectl', 'helm',
    'chroot', 'pivot_root',
    '> /dev/', '> /proc/', '> /sys/',
    'tar --checkpoint', 'tar --checkpoint-action',
    '__import__', 'importlib', 'exec(', 'eval(', 'os.system',
]

_audit_log_path = LOG_DIR / "command_audit.log"
_rate_limit = {}  # chat_id -> [timestamps]

# Fixed diagnostic actions.  There are intentionally no caller-provided
# arguments or paths: even nominally read-only tools such as cat/ps can disclose
# service credentials, environment variables, private files, or command lines.
_DIAGNOSTIC_ACTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "health": (
        ("/usr/bin/uptime",),
        ("/usr/bin/free", "-h"),
        ("/usr/bin/df", "-h", "/"),
    ),
    "uptime": (("/usr/bin/uptime",),),
    "memory": (("/usr/bin/free", "-h"),),
    "disk": (("/usr/bin/df", "-h", "/"),),
    "services": (("/usr/bin/systemctl", "--failed", "--no-pager", "--no-legend"),),
    "ollama": (("/usr/local/bin/ollama", "list"),),
}
_DIAGNOSTIC_ALIASES = {
    "free": "memory",
    "free -h": "memory",
    "df -h": "disk",
    "df -h /": "disk",
    "systemctl --failed": "services",
    "ollama list": "ollama",
}


def _diagnostic_action(value: str) -> str | None:
    normalized = value.strip().lower()
    normalized = _DIAGNOSTIC_ALIASES.get(normalized, normalized)
    return normalized if normalized in _DIAGNOSTIC_ACTIONS else None


def _is_command_allowed(cmd: str) -> tuple[bool, str]:
    """Accept only a fixed diagnostic action or an exact legacy alias."""

    if not isinstance(cmd, str) or not cmd.strip():
        return False, "Empty diagnostic action"
    action = _diagnostic_action(cmd)
    if action is None:
        return False, "Only fixed diagnostics are allowed: health, uptime, memory, disk, services, ollama"
    return True, action


def _match_args(args: list[str], template_args: list[str]) -> bool:
    """
    Match actual arguments against template pattern.
    Template placeholders: <path>, <n>, <pattern>, <code>, <host>, <commit>, <package>, <script>, <delim>, <fields>, <set1>, <set2>, <pid>, <model>, <name>
    Special: '*' matches any single argument, [] means no args allowed.
    """
    if template_args == []:
        return len(args) == 0
    
    if template_args == ["*"]:
        return len(args) == 1
    
    # Every template describes the complete argv shape. Allowing a suffix after
    # a placeholder would let command-specific flags bypass the allowlist.
    if len(args) != len(template_args):
        return False
    
    # Simple matching: check fixed args match at their positions
    arg_idx = 0
    for tmpl_arg in template_args:
        if tmpl_arg.startswith('<') and tmpl_arg.endswith('>'):
            # Placeholder - skip one argument
            if arg_idx >= len(args):
                return False
            arg_idx += 1
        else:
            # Fixed argument - must match exactly
            if arg_idx >= len(args) or args[arg_idx] != tmpl_arg:
                return False
            arg_idx += 1
    
    return True


def run_bash_safely(cmd: str, chat_id: int = 0, timeout: int = 60) -> str:
    """Run one fixed, read-only diagnostic action without a shell.

    ``cmd`` is retained as the parameter name for API compatibility.  It is an
    action ID, not a shell command, and cannot carry caller-controlled options
    or filesystem paths.
    """

    # Rate limit: max 10 commands per minute per chat - use thread-safe version
    recent = get_rate_limit_timestamps(chat_id)
    if len(recent) >= 10:
        return "⛔ Rate limit exceeded (10 commands/min). Wait a bit."
    add_rate_limit_timestamp(chat_id)

    # Safety check - allowlist validation
    allowed, reason = _is_command_allowed(cmd)
    if not allowed:
        _audit_log(cmd, chat_id, "BLOCKED")
        return f"⛔ BLOCKED: {reason}"
    action = reason

    _audit_log(action, chat_id, "EXECUTED")
    command_timeout = min(max(int(timeout), 1), 30)
    safe_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    try:
        sections: list[str] = []
        for argv in _DIAGNOSTIC_ACTIONS[action]:
            result = subprocess.run(
                list(argv),
                shell=False,
                capture_output=True,
                text=True,
                timeout=command_timeout,
                stdin=subprocess.DEVNULL,
                cwd="/",
                env=safe_env,
                check=False,
            )
            output = (result.stdout + result.stderr).strip()
            if len(_DIAGNOSTIC_ACTIONS[action]) > 1:
                sections.append(f"$ {Path(argv[0]).name} {' '.join(argv[1:])}\n{output or '(no output)'}")
            else:
                sections.append(output or "(no output)")
        return "\n\n".join(sections)[:4000]
    except subprocess.TimeoutExpired:
        return f"⏱ Diagnostic timed out after {command_timeout}s"
    except FileNotFoundError:
        return "⛔ Diagnostic tool is not installed."
    except Exception as exc:
        logger.warning("Diagnostic action failed: %s", type(exc).__name__)
        return "⛔ Diagnostic failed."


def _audit_log(cmd: str, chat_id: int, status: str):
    """Write to audit log for accountability."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        ts = datetime.now(et).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        ts = datetime.now().isoformat()

    try:
        _audit_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _audit_log_path.is_symlink():
            return
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(_audit_log_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, f"[{ts}] chat={chat_id} status={status} action={cmd[:80]}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        logger.warning("Unable to write diagnostic audit log", exc_info=True)


# ── PII Scrubber (Security) ──────────────────────────────────────────────────
# Patterns that MUST NOT leave the server. Applied before any OpenRouter call.
import re as _re

# Email addresses
_EMAIL_RE = _re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
# Phone numbers (US format)
_PHONE_RE = _re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
# SSN
_SSN_RE = _re.compile(r'\b\d{3}-\d{2}-\d{4}\b')
# Credit card numbers (basic)
_CC_RE = _re.compile(r'\b(?:\d{4}[-\s]?){3}\d{4}\b')
# Dates of birth patterns
_DOB_RE = _re.compile(r'\b(?:0[1-9]|1[0-2])[/-](?:0[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b')
# Student ID patterns
_STUDENT_ID_RE = _re.compile(r'\b(?:student\s*id|sid|id\s*#?)\s*:?\s*\d{4,10}\b', _re.IGNORECASE)
# IP Addresses (IPv4)
_IP_RE = _re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')
# Home directories
_HOME_DIR_RE = _re.compile(r'/home/[a-zA-Z0-9_-]+/?')

# Known PII-safe replacement markers
_PII_REPLACEMENTS = {
    'email': '[EMAIL]',
    'phone': '[PHONE]',
    'ssn': '[SSN]',
    'cc': '[CARD]',
    'dob': '[DATE]',
    'student_id': '[ID]',
    'ip': '[IP_ADDRESS]',
    'home_dir': '[HOME_DIR]',
}


def scrub_pii(text: str, aggressive: bool = False) -> str:
    """
    Remove PII from text before sending to external (cloud) LLM APIs.

    Args:
        text: Input text that may contain PII
        aggressive: If True, also scrub names and other identifiers

    Returns:
        Scrubbed text safe for cloud processing
    """
    if not text:
        return text

    text = _EMAIL_RE.sub(_PII_REPLACEMENTS['email'], text)
    text = _PHONE_RE.sub(_PII_REPLACEMENTS['phone'], text)
    text = _SSN_RE.sub(_PII_REPLACEMENTS['ssn'], text)
    text = _CC_RE.sub(_PII_REPLACEMENTS['cc'], text)
    text = _DOB_RE.sub(_PII_REPLACEMENTS['dob'], text)
    text = _STUDENT_ID_RE.sub(_PII_REPLACEMENTS['student_id'], text)
    text = _IP_RE.sub(_PII_REPLACEMENTS['ip'], text)
    text = _HOME_DIR_RE.sub(_PII_REPLACEMENTS['home_dir'], text)

    if aggressive:
        # Also scrub proper names (simple heuristic: capitalized words not at sentence start)
        # This is lossy but safe for cloud processing
        text = _re.sub(
            r'(?<!^)(?<!\\. )(?<!\\n)\b([A-Z][a-z]+ [A-Z][a-z]+)\b',
            '[NAME]', text
        )

    return text


def check_pii(text: str) -> tuple:
    """
    Check if text is safe to send to cloud.
    Returns (is_safe, scrubbed_text, found_pii_types).
    """
    found_types = []
    if _EMAIL_RE.search(text):
        found_types.append('email')
    if _PHONE_RE.search(text):
        found_types.append('phone')
    if _SSN_RE.search(text):
        found_types.append('ssn')
    if _CC_RE.search(text):
        found_types.append('credit_card')
    if _DOB_RE.search(text):
        found_types.append('dob')
    if _STUDENT_ID_RE.search(text):
        found_types.append('student_id')

    scrubbed = scrub_pii(text)
    return (len(found_types) == 0, scrubbed, found_types)


def sanitize_markdown(text: str) -> str:
    """Escape Telegram MarkdownV1 control characters in dynamic/LLM-generated text.

    Unpaired ``_``, ``*``, `` ` ``, ``[``, ``]`` in AI output cause Telegram
    to reject the message with "Can't parse entities: can't find end of the
    entity".  Backslash-escaping these chars renders them literally in V1.

    Usage in f-strings::

        text = f"**Bold header**\\n{sanitize_markdown(llm_output)}"
    """
    # Order matters: backslash first so we don't double-escape our own escapes
    text = text.replace("\\", "\\\\")
    text = text.replace("_", "\\_")
    text = text.replace("*", "\\*")
    text = text.replace("`", "\\`")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text


# ── Backup System (Feature 8) ────────────────────────────────────────────────
def _atomic_write_private_text(path: Path, content: str) -> None:
    """Replace a private text file without a partial-write window."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _backup_members() -> list[tuple[Path, str]]:
    """Return only regular, private runtime files with safe archive names."""
    members: list[tuple[Path, str]] = []
    for filename in BACKUP_FILES:
        path = STATE_DIR / filename
        if path.is_file() and not path.is_symlink():
            members.append((path, f"state/{filename}"))
    for root, prefix, pattern in (
        (CHAT_HISTORY_DIR, "chat_history", "chat_history_*.txt"),
        (CACHE_DIR, "cache", "*.txt"),
    ):
        if not root.exists():
            continue
        for path in root.glob(pattern):
            if path.is_file() and not path.is_symlink():
                members.append((path, f"{prefix}/{path.name}"))
    return members


def create_backup() -> Optional[str]:
    """
    Create a timestamped backup of all critical state files.
    Returns the backup path or None on failure.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_DIR.chmod(0o700)
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_name = f"backup_{date_str}.tar.gz"
    backup_path = BACKUP_DIR / backup_name
    members = _backup_members()
    if not members:
        logger.warning("No files to backup")
        return None

    try:
        with tarfile.open(backup_path, mode="w:gz", dereference=False) as archive:
            for path, archive_name in members:
                archive.add(path, arcname=archive_name, recursive=False)
        backup_path.chmod(0o600)
        logger.info("Backup created: %s", backup_path.name)
        # Clean old backups
        cleanup_old_backups()
        return str(backup_path)
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        return None


def cleanup_old_backups():
    """Remove backups older than BACKUP_RETENTION_DAYS."""
    if not BACKUP_DIR.exists():
        return
    cutoff = time.time() - (BACKUP_RETENTION_DAYS * 86400)
    for f in BACKUP_DIR.glob("backup_*.tar.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            logger.info(f"Removed old backup: {f.name}")


def list_backups() -> list:
    """Return list of available backups, sorted newest first."""
    if not BACKUP_DIR.exists():
        return []
    backups = sorted(BACKUP_DIR.glob("backup_*.tar.gz"), reverse=True)
    result = []
    for b in backups:
        size_mb = b.stat().st_size / (1024 * 1024)
        date = b.stem.replace("backup_", "")
        result.append({"path": str(b), "date": date, "size_mb": round(size_mb, 1)})
    return result


def restore_backup(backup_path: str, dry_run: bool = True) -> str:
    """
    Restore from a backup. If dry_run, just list what would be restored.
    """
    try:
        p = Path(backup_path).resolve(strict=True)
        backup_root = BACKUP_DIR.resolve(strict=False)
    except OSError:
        return "❌ Backup not found."
    if backup_root not in p.parents or not p.is_file():
        return "❌ Backup path is not in the managed backup directory."

    # Restore is intentionally unavailable from bot code.  It requires an
    # operator-reviewed staging restore and an explicit operational approval.
    if not dry_run:
        return "⛔ Restore is disabled in the bot. Use an operator-reviewed staging restore."
    try:
        with tarfile.open(p, mode="r:gz") as archive:
            names = [member.name for member in archive.getmembers() if member.isfile()]
        files = names[:30]
        suffix = "+" if len(names) > len(files) else ""
        return f"📦 **Backup contents** ({len(names)} files):\n" + "\n".join(f"  {name}" for name in files) + suffix
    except (tarfile.TarError, OSError):
        return "❌ Backup cannot be read."


# ── Cross-Source Correlation Engine (Feature 6) ──────────────────────────────
def load_correlation_graph() -> dict:
    """Load the correlation graph (JSON)."""
    try:
        return json.loads(CORRELATION_GRAPH_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"nodes": {}, "edges": []}


def save_correlation_graph(graph: dict):
    """Persist the correlation graph."""
    CORRELATION_GRAPH_FILE.write_text(json.dumps(graph, indent=2))


def correlate_items(items: list) -> dict:
    """
    Build correlations across scraped items.

    items: list of {"source": "canvas", "title": "...", "": "...", "type": "assignment|announcement|email|message"}

    Returns updated graph.
    """
    graph = load_correlation_graph()

    # Extract key terms (simple: words > 3 chars, non-common)
    STOP_WORDS = {"assignment", "homework", "about", "this", "that", "with", "from",
                  "your", "have", "will", "please", "the", "and", "for", "you", "not",
                  "was", "are", "but", "can", "all", "had", "one", "our", "out",
                  "day", "get", "has", "him", "his", "how", "its", "may", "new", "now",
                  "old", "see", "way", "who", "did", "oil", "sit", "use", "than"}

    def extract_terms(text: str) -> set:
        words = text.lower().split()
        return {w.strip(".,!?():;[]'\"") for w in words if len(w) > 4 and w not in STOP_WORDS}

    # Add/update nodes
    for item in items:
        node_id = hashlib.md5(f"{item['source']}:{item['title']}".encode()).hexdigest()[:12]
        terms = extract_terms(item["title"] + " " + item.get("text", ""))

        if node_id not in graph["nodes"]:
            graph["nodes"][node_id] = {
                "source": item["source"],
                "title": item["title"],
                "terms": list(terms),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": item.get("type", "unknown"),
            }
        else:
            # Merge new terms
            existing = set(graph["nodes"][node_id]["terms"])
            graph["nodes"][node_id]["terms"] = list(existing | terms)

    # Build edges: nodes sharing 2+ terms are correlated
    nodes = graph["nodes"]
    graph["edges"] = []
    node_ids = list(nodes.keys())

    for i, nid_a in enumerate(node_ids):
        terms_a = set(nodes[nid_a]["terms"])
        for nid_b in node_ids[i+1:]:
            terms_b = set(nodes[nid_b]["terms"])
            shared = terms_a & terms_b
            if len(shared) >= 2:
                graph["edges"].append({
                    "source": nid_a,
                    "target": nid_b,
                    "shared_terms": list(shared),
                    "strength": len(shared),
                })

    save_correlation_graph(graph)
    return graph


def get_related_items(query: str, max_results: int = 5) -> list:
    """Find items correlated with a search query."""
    graph = load_correlation_graph()
    query_terms = set(w.lower() for w in query.split() if len(w) > 3)

    scored = []
    for node_id, node in graph.get("nodes", {}).items():
        node_terms = set(node.get("terms", []))
        shared = query_terms & node_terms
        if shared:
            scored.append((len(shared), node))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [node for _, node in scored[:max_results]]


def get_correlation_summary() -> str:
    """Format correlation stats for Telegram."""
    graph = load_correlation_graph()
    nodes = len(graph.get("nodes", {}))
    edges = len(graph.get("edges", {}))

    # Group by source
    sources = {}
    for node in graph.get("nodes", {}).values():
        s = node["source"]
        sources[s] = sources.get(s, 0) + 1

    lines = [
        f"⚙️ **Correlation Engine**",
        f"Nodes: {nodes} | Correlations: {edges}",
        "",
        "**By Source:**",
    ]
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        lines.append(f"  {src}: {count} items")
    return "\n".join(lines)


# ── Health Telemetry (Fix #6: /ping command) ────────────────────────────────
def get_health_status() -> str:
    """Generate health status report for /ping command."""
    import shutil as _shutil

    # Bot uptime from systemd
    uptime_str = "unknown"
    try:
        result = subprocess.run(
            ["systemctl", "show", "bot.service", "--property=ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            uptime_str = result.stdout.strip()
    except Exception:
        pass

    # Disk usage
    disk = _shutil.disk_usage("/")
    disk_pct = round(disk.used / disk.total * 100, 1)

    # Last digest time
    last_digest = "never"
    try:
        from config import LATEST_DIGEST_FILE
        mtime = os.path.getmtime(LATEST_DIGEST_FILE)
        age_min = int((time.time() - mtime) / 60)
        if age_min < 60:
            last_digest = f"{age_min}m ago"
        else:
            last_digest = f"{age_min // 60}h ago"
    except Exception:
        pass

    # Nightly queue size
    queue_size = 0
    try:
        qf = NIGHTLY_QUEUE_FILE
        if qf.exists():
            queue_size = len(json.loads(qf.read_text()))
    except Exception:
        pass

    # Rotating file sizes
    def file_size_str(path: Path) -> str:
        if not path.exists():
            return "missing"
        s = path.stat().st_size
        if s > 1024*1024:
            return f"{s/(1024*1024):.1f}MB"
        return f"{s/1024:.0f}KB"

    lines = [
        "🏥 **Bot Health**",
        f"Uptime: {uptime_str}",
        f"Disk: {disk_pct}% used ({disk.free/(1024**3):.1f}GB free)",
        f"Last digest: {last_digest}",
        f"Nightly queue: {queue_size} files",
        "**File Sizes:**",
        f"  State: {file_size_str(STATE_FILE)}",
        f"  Combined summaries: {file_size_str(COMBINED_SUMMARIES_FILE)}",
        f"  Mega index: {file_size_str(MEGA_INDEX_FILE)}",
        f"  Curated brain: {file_size_str(CURATED_BRAIN_FILE)}",
    ]
    return "\n".join(lines)


# ── Content-Hash Caching ─────────────────────────────────────────────────────
import hashlib as _hashlib

_PROCESSED_CACHE_PATH = CACHE_DIR / "processed_hashes.json"

def load_processed_cache() -> dict:
    """Load the cache of {source_hash: processed_at} entries."""
    try:
        return json.loads(_PROCESSED_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_processed_cache(cache: dict):
    """Persist the cache of {source_name: latest_hash} mapping."""
    _PROCESSED_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def content_hash(data: str) -> str:
    return _hashlib.md5(data.encode()).hexdigest()[:16]


def has_changed(source_name: str, data: str) -> bool:
    """Check if source data changed since last successful processing. Returns True if should reprocess."""
    cache = load_processed_cache()
    h = content_hash(data)
    if cache.get(source_name) == h:
        return False  # unchanged
    return True

def mark_processed(source_name: str, data: str):
    """Mark source data as processed to prevent reprocessing."""
    cache = load_processed_cache()
    h = content_hash(data)
    cache[source_name] = h
    # Keep only last 100 entries to prevent bloat
    if len(cache) > 100:
        cache = dict(list(cache.items())[-100:])
    save_processed_cache(cache)


# ── Retry Decorator ──────────────────────────────────────────────────────────
def retry(max_retries=3, base_delay=1.0, exceptions=(Exception,)):
    """Decorator for API calls with exponential backoff + jitter."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(f"{func.__name__} attempt {attempt+1} failed: {e}. Retry in {delay:.1f}s")
                        time.sleep(delay)
            return func(*args, **kwargs)  # last attempt, let it raise
        return wrapper
    return decorator


# ── Bounded LRU Caches ────────────────────────────────────────────────────────
from functools import lru_cache
import threading

# Global cache instances with size limits and thread safety
_rate_limit_cache = {}
_rate_limit_lock = threading.Lock()
_MAX_RATE_LIMIT_ENTRIES = 1000  # Max chat_ids to track

# Session caches with TTL
_cached_sessions = {}
_cached_sessions_lock = threading.Lock()
_MAX_SESSION_ENTRIES = 50

def get_rate_limit_timestamps(chat_id: int) -> list[float]:
    """Get recent timestamps for a chat_id, with automatic cleanup."""
    global _rate_limit_cache
    now = time.time()
    with _rate_limit_lock:
        # Clean old entries
        recent = [t for t in _rate_limit_cache.get(chat_id, []) if now - t < 60]
        _rate_limit_cache[chat_id] = recent
        # Enforce global size limit
        if len(_rate_limit_cache) > _MAX_RATE_LIMIT_ENTRIES:
            # Remove oldest entries
            all_entries = [(chat_id, ts) for chat_id, timestamps in _rate_limit_cache.items() for ts in timestamps]
            all_entries.sort(key=lambda x: x[1])
            to_remove = len(all_entries) - _MAX_RATE_LIMIT_ENTRIES + 100
            if to_remove > 0:
                for chat_id_rm, ts_rm in all_entries[:to_remove]:
                    if chat_id_rm in _rate_limit_cache:
                        try:
                            _rate_limit_cache[chat_id_rm].remove(ts_rm)
                            if not _rate_limit_cache[chat_id_rm]:
                                del _rate_limit_cache[chat_id_rm]
                        except ValueError:
                            pass
        return _rate_limit_cache.get(chat_id, [])

def add_rate_limit_timestamp(chat_id: int):
    """Add a timestamp for rate limiting."""
    with _rate_limit_lock:
        now = time.time()
        if chat_id not in _rate_limit_cache:
            _rate_limit_cache[chat_id] = []
        _rate_limit_cache[chat_id].append(now)

# Global session getters with cleanup
def get_httpx_client() -> httpx.Client:
    """Get or create a shared httpx client with connection pooling."""
    global _cached_sessions
    with _cached_sessions_lock:
        if 'httpx' not in _cached_sessions:
            # httpx.Timeout requires all four parameters or a single default
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
            _cached_sessions['httpx'] = httpx.Client(
                timeout=timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return _cached_sessions['httpx']

def get_async_httpx_client() -> httpx.AsyncClient:
    """Get or create a shared async httpx client with connection pooling."""
    global _cached_sessions
    with _cached_sessions_lock:
        if 'httpx_async' not in _cached_sessions:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
            _cached_sessions['httpx_async'] = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return _cached_sessions['httpx_async']

def get_requests_session() -> requests.Session:
    """Get or create a shared requests session."""
    import requests
    global _cached_sessions
    with _cached_sessions_lock:
        if 'requests' not in _cached_sessions:
            _cached_sessions['requests'] = requests.Session()
        return _cached_sessions['requests']

def _drain_cached_sessions() -> list[object]:
    """Detach cached clients once so shutdown paths cannot close them twice."""
    with _cached_sessions_lock:
        sessions = list(_cached_sessions.values())
        _cached_sessions.clear()
    return sessions


def _cleanup_caches() -> None:
    """Synchronous interpreter-exit fallback; never create async coroutines."""
    for session in _drain_cached_sessions():
        if hasattr(session, "aclose"):
            continue
        try:
            session.close()
        except Exception:
            pass
    with _rate_limit_lock:
        _rate_limit_cache.clear()


async def cleanup_async_caches() -> None:
    """Application-lifecycle cleanup for sync and async cached HTTP clients."""
    for session in _drain_cached_sessions():
        try:
            if hasattr(session, "aclose"):
                await session.aclose()
            else:
                session.close()
        except Exception:
            pass
    with _rate_limit_lock:
        _rate_limit_cache.clear()


import atexit
atexit.register(_cleanup_caches)
