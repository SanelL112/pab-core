#!/usr/bin/env python3
"""
Log Error Scanner — scans all bot log sources for known error patterns.
Usage:
    python3 log_scanner.py                  # recent errors (last 24h)
    python3 log_scanner.py --all            # scan everything available
    python3 log_scanner.py --hours 72       # last 72 hours
    python3 log_scanner.py --json           # JSON output
    python3 log_scanner.py --follow         # tail and watch for new errors
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.absolute()

ERROR_PATTERNS = [
    # Telegram parse errors
    (r"can'?t parse entities", "TELEGRAM_PARSE"),
    (r"can'?t find end of the entity", "TELEGRAM_PARSE"),
    (r"Bad Request: can'?t parse", "TELEGRAM_PARSE"),
    (r"entity starting at byte offset", "TELEGRAM_PARSE"),
    (r"Telegram send failed", "TELEGRAM_FAIL"),
    # Resource warnings
    (r"ResourceWarning", "RESOURCE_WARN"),
    (r"unclosed.*SSLSocket", "SSL_LEAK"),
    # Auth / OAuth
    (r"CSRF Warning", "CSRF_WARN"),
    (r"mismatching_state", "CSRF_WARN"),
    (r"TokenError|token_expired|token revoked", "AUTH_FAIL"),
    (r"Insufficient Permission|quotaExceeded", "API_QUOTA"),
    # AI / LLM failures
    (r"Fallback to G1 Exception", "FALLBACK_FAIL"),
    (r"OpenRouter Exception", "OPENROUTER_FAIL"),
    (r"Ollama error", "OLLAMA_FAIL"),
    (r"AI hallucinated", "AI_HALLUCINATION"),
    (r"Recovery Agent", "RECOVERY_AGENT"),
    (r"All models failed", "ALL_MODELS_FAIL"),
    (r"timed out", "TIMEOUT"),
    # Runtime
    (r"Traceback \(most recent call last\)", "TRACEBACK"),
    (r"Error downloading|download failed", "DOWNLOAD_FAIL"),
    (r"Failed to build guide", "GUIDE_FAIL"),
    (r"Error during AI digest", "DIGEST_FAIL"),
    (r"Rate limit|rate_limit", "RATE_LIMIT"),
    (r"ConnectionError|Connection refused", "NETWORK_ERR"),
    (r"HTTPConnectionPool|ReadTimeout", "NETWORK_ERR"),
    # Discord / General
    (r"ERROR.*discord|discord.*error", "DISCORD_ERR"),
    (r"Watchdog.*error|watchdog.*fail", "WATCHDOG_ERR"),
    (r"Nightly sleep cycle error|nightly_fail|Nightly.*skipped|Nightly.*failed", "NIGHTLY_FAIL"),
]

def color(s: str, code: int) -> str:
    """Wrap string in terminal color code if stdout is a tty."""
    if sys.stdout.isatty():
        return f"\033[{code}m{s}\033[0m"
    return s

RED = 31
GREEN = 32
YELLOW = 33
CYAN = 36
BOLD = 1

SEVERITY_COLORS = {
    "TELEGRAM_PARSE": YELLOW,
    "TELEGRAM_FAIL": YELLOW,
    "RESOURCE_WARN": YELLOW,
    "SSL_LEAK": YELLOW,
    "CSRF_WARN": YELLOW,
    "AUTH_FAIL": RED,
    "API_QUOTA": RED,
    "FALLBACK_FAIL": CYAN,
    "OPENROUTER_FAIL": CYAN,
    "OLLAMA_FAIL": CYAN,
    "AI_HALLUCINATION": RED,
    "RECOVERY_AGENT": RED,
    "ALL_MODELS_FAIL": YELLOW,
    "TIMEOUT": CYAN,
    "TRACEBACK": RED,
    "DOWNLOAD_FAIL": YELLOW,
    "GUIDE_FAIL": YELLOW,
    "DIGEST_FAIL": YELLOW,
    "RATE_LIMIT": CYAN,
    "NETWORK_ERR": CYAN,
    "DISCORD_ERR": CYAN,
    "WATCHDOG_ERR": YELLOW,
    "NIGHTLY_FAIL": RED,
}

SEVERITY_EMOJI = {
    "TELEGRAM_PARSE": "📝",
    "TELEGRAM_FAIL": "📝",
    "RESOURCE_WARN": "🔋",
    "SSL_LEAK": "🔌",
    "CSRF_WARN": "🔑",
    "AUTH_FAIL": "🔒",
    "API_QUOTA": "🚫",
    "FALLBACK_FAIL": "🤖",
    "OPENROUTER_FAIL": "🌐",
    "OLLAMA_FAIL": "🦙",
    "AI_HALLUCINATION": "🤪",
    "RECOVERY_AGENT": "🩺",
    "ALL_MODELS_FAIL": "💀",
    "TIMEOUT": "⏰",
    "TRACEBACK": "🔥",
    "DOWNLOAD_FAIL": "⬇️",
    "GUIDE_FAIL": "📚",
    "DIGEST_FAIL": "📊",
    "RATE_LIMIT": "🐢",
    "NETWORK_ERR": "🌍",
    "DISCORD_ERR": "💬",
    "WATCHDOG_ERR": "👀",
    "NIGHTLY_FAIL": "🌙",
}


def scan_journal(hours: int = 24, service: str = "bot.service") -> list[dict]:
    """Scan journald logs for the bot service."""
    matches = []
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "--since", since, "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=30
        )
        for line in result.stdout.splitlines():
            for pattern, category in ERROR_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    matches.append({
                        "source": f"journal:{service}",
                        "timestamp": line[:25].strip(),
                        "category": category,
                        "message": line.strip(),
                    })
                    break
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        matches.append({
            "source": "journal",
            "timestamp": datetime.now().isoformat(),
            "category": "SCAN_ERR",
            "message": f"journalctl failed: {e}",
        })
    return matches


def scan_activity_log(hours: int = 24) -> list[dict]:
    """Scan activity_log.jsonl for error-level events."""
    matches = []
    log_path = BASE_DIR / "activity_log.jsonl"
    if not log_path.exists():
        return matches

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts_str = entry.get("timestamp", "")
        ts_num = entry.get("ts", None)
        date_str = entry.get("date", "")
        time_str = entry.get("time", "00:00:00")
        try:
            if ts_num is not None:
                entry_ts = datetime.fromtimestamp(ts_num, timezone.utc)
            elif ts_str:
                entry_ts = datetime.fromisoformat(ts_str)
            elif date_str:
                entry_ts = datetime.fromisoformat(f"{date_str}T{time_str}")
            else:
                entry_ts = datetime.fromtimestamp(0, timezone.utc)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.replace(tzinfo=timezone.utc)
            else:
                entry_ts = entry_ts.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            entry_ts = datetime.fromtimestamp(0, timezone.utc)

        if entry_ts < cutoff:
            continue

        cat = entry.get("cat", "")
        details = entry.get("details", {})
        msg = json.dumps(details, default=str)

        if cat in ("error", "critical", "nightly_fail"):
            display_ts = ts_str or f"{date_str} {time_str}".strip()
            if not display_ts and ts_num is not None:
                display_ts = entry_ts.isoformat()
            matches.append({
                "source": "activity_log",
                "timestamp": display_ts,
                "category": cat.upper(),
                "message": f"[{cat}] {msg[:300]}",
            })
        elif cat == "nightly" and details.get("status") in {"failed", "error"}:
            display_ts = ts_str or f"{date_str} {time_str}".strip() or entry_ts.isoformat()
            matches.append({
                "source": "activity_log",
                "timestamp": display_ts,
                "category": "NIGHTLY_FAIL",
                "message": f"[nightly] {msg[:300]}",
            })
        else:
            for pattern, category in ERROR_PATTERNS:
                if re.search(pattern, msg, re.IGNORECASE):
                    matches.append({
                        "source": "activity_log",
                        "timestamp": ts_str,
                        "category": category,
                        "message": msg[:300],
                    })
                    break
    return matches


def scan_chat_history(hours: int = 24) -> list[dict]:
    """Scan chat history files for error messages sent by the bot."""
    matches = []
    cutoff = datetime.now() - timedelta(hours=hours)
    for fpath in BASE_DIR.glob("chat_history_*.txt"):
        mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
        if mtime < cutoff:
            continue
        text = fpath.read_text(errors="replace")
        for pattern, category in ERROR_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                start = max(0, m.start() - 60)
                end = min(len(text), m.end() + 60)
                context = text[start:end].replace("\n", " ")
                matches.append({
                    "source": f"history:{fpath.name}",
                    "timestamp": datetime.fromtimestamp(fpath.stat().st_mtime).isoformat(),
                    "category": category,
                    "message": f"...{context}...",
                })
    return matches


def scan_log_files(hours: int = 24) -> list[dict]:
    """Scan loose .log files in configured directories."""
    matches = []
    cutoff = datetime.now() - timedelta(hours=hours)
    
    try:
        from config import LOG_SCAN_DIRS
        search_dirs = [Path(directory) for directory in LOG_SCAN_DIRS]
    except ImportError:
        search_dirs = [BASE_DIR]

    for search_dir in search_dirs:
        if not search_dir.exists(): continue
        for glob_pat in ["*.log", "*.log.*"]:
            for fpath in search_dir.glob(glob_pat):
                try:
                    mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
                    if mtime < cutoff:
                        continue
                    text = fpath.read_text(errors="replace")
                except Exception:
                    continue
                for line in text.splitlines():
                    for pattern, category in ERROR_PATTERNS:
                        if re.search(pattern, line, re.IGNORECASE):
                            matches.append({
                                "source": f"file:{fpath.name}",
                                "timestamp": datetime.fromtimestamp(fpath.stat().st_mtime).isoformat(),
                                "category": category,
                                "message": line.strip()[:300],
                            })
                            break
    return matches


def scan_command_audit(hours: int = 24) -> list[dict]:
    """Scan command_audit.log for failed commands."""
    matches = []
    fpath = BASE_DIR / "command_audit.log"
    if not fpath.exists():
        return matches
    cutoff = datetime.now() - timedelta(hours=hours)
    mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
    if mtime < cutoff:
        return matches

    for line in fpath.read_text(errors="replace").splitlines():
        if re.search(r"status.*fail|exit.*[1-9]\d*|error|FAILED", line, re.IGNORECASE):
            matches.append({
                "source": "command_audit",
                "timestamp": datetime.fromtimestamp(fpath.stat().st_mtime).isoformat(),
                "category": "CMD_FAIL",
                "message": line.strip()[:300],
            })
    return matches


def deduplicate(matches: list[dict]) -> list[dict]:
    """Remove duplicate messages within same category."""
    seen = set()
    unique = []
    for m in matches:
        key = (m["category"], m["message"][:120])
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def format_results(matches: list[dict], output_json: bool = False) -> str:
    """Format scan results for display."""
    if output_json:
        return json.dumps({"count": len(matches), "errors": matches}, indent=2)

    if not matches:
        return color("✅ No errors found in the scan period.", GREEN)

    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for m in matches:
        by_cat.setdefault(m["category"], []).append(m)

    lines = [
        color(f"📋 Log Scan Report — {len(matches)} issue(s) found", BOLD),
        "─" * 60,
    ]

    # Summary line
    cat_counts = "  ".join(
        f"{color(f'{emoji} {cat}: {len(items)}', SEVERITY_COLORS.get(cat, CYAN))}"
        for cat, items in sorted(by_cat.items())
        if (emoji := SEVERITY_EMOJI.get(cat, "❓"))
    )
    lines.append(cat_counts)
    lines.append("")

    for cat, items in sorted(by_cat.items()):
        emoji = SEVERITY_EMOJI.get(cat, "❓")
        clr = SEVERITY_COLORS.get(cat, CYAN)
        lines.append(color(f"{emoji}  {cat} ({len(items)})", clr | BOLD))
        for m in items[:5]:  # show max 5 per category
            ts = m.get("timestamp", "?")
            src = m.get("source", "?")
            msg = m["message"][:200]
            lines.append(f"    {color(ts, CYAN)} [{src}] {msg}")
        if len(items) > 5:
            lines.append(color(f"    ... and {len(items) - 5} more", YELLOW))
        lines.append("")

    lines.append("─" * 60)
    lines.append(f"Scanned: journal, activity_log, chat_history, *.log, command_audit")
    return "\n".join(lines)


def follow_mode(hours_initial: int = 24):
    """Watch logs and report new errors as they appear (simple poll)."""
    last_run = datetime.now()
    print(color("👀 Following logs for new errors (Ctrl+C to stop)...", CYAN))

    # Print initial snapshot
    matches = scan_all(hours_initial)
    print(format_results(matches))

    try:
        while True:
            time.sleep(30)
            fresh = scan_all(1)  # last 1 hour
            new_ones = [m for m in fresh if m not in matches]
            if new_ones:
                matches.extend(new_ones)
                print(f"\n{color('NEW ERRORS', RED | BOLD)} at {datetime.now().strftime('%H:%M:%S')}:")
                for m in new_ones[:10]:
                    emoji = SEVERITY_EMOJI.get(m["category"], "❗")
                    print(f"  {emoji} {color(m['category'], SEVERITY_COLORS.get(m['category'], CYAN))}: {m['message'][:200]}")
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print(color("\n👋 Stopped.", CYAN))


def scan_all(hours: int = 24) -> list[dict]:
    """Run all scanners and return deduplicated results."""
    matches = []
    matches.extend(scan_journal(hours))
    matches.extend(scan_activity_log(hours))
    matches.extend(scan_chat_history(hours))
    matches.extend(scan_log_files(hours))
    matches.extend(scan_command_audit(hours))
    return deduplicate(matches)


def main():
    parser = argparse.ArgumentParser(
        description="Bot Log Error Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--all", action="store_true", help="Scan all available logs (no time limit)")
    parser.add_argument("--hours", type=int, default=24, help="How far back to scan (hours, default 24)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--follow", "-f", action="store_true", help="Tail logs and report new errors")
    parser.add_argument("--source", choices=["journal", "activity_log", "chat_history", "log_files", "cmd_audit", "all"],
                        default="all", help="Only scan a specific source")
    args = parser.parse_args()

    hours = 0 if args.all else args.hours
    sources = {
        "journal": scan_journal,
        "activity_log": scan_activity_log,
        "chat_history": scan_chat_history,
        "log_files": scan_log_files,
        "cmd_audit": scan_command_audit,
    }

    if args.follow:
        follow_mode(hours if hours > 0 else 24)
        return

    if args.source == "all":
        matches = scan_all(hours if hours > 0 else 9999)
    else:
        fn = sources[args.source]
        matches = fn(hours if hours > 0 else 9999)
        matches = deduplicate(matches)

    print(format_results(matches, output_json=args.json))

    # Exit non-zero if any RED-level issues found
    red_cats = {cat for cat, clr in SEVERITY_COLORS.items() if clr == RED}
    if any(m["category"] in red_cats for m in matches):
        sys.exit(2)


if __name__ == "__main__":
    main()
