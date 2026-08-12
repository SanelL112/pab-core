#!/usr/bin/env python3
"""Telegram notification script for cron jobs.

Reads Telegram configuration through the application's private config loader.

Usage:
  python3 telegram_notify.py "your message here"
  python3 telegram_notify.py --health-check   # Runs full bot health check and sends report
"""
import sys
import subprocess
import requests
import shutil
from datetime import datetime
from pathlib import Path
import config

TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT_ID = str(config.SANEL_CHAT_ID)


def send_message(text: str, parse_mode: str = None) -> dict:
    """Send a message via Telegram Bot API."""
    if not TOKEN:
        return {'ok': False, 'error': 'Telegram notification is not configured'}

    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    payload = {
        'chat_id': CHAT_ID,
        'text': text,
    }
    if parse_mode:
        payload['parse_mode'] = parse_mode

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except requests.RequestException:
        return {'ok': False, 'error': 'Telegram request failed'}


def send_plain_message(text: str) -> dict:
    """Send a plain text message (no markdown parsing) - reliable for structured reports."""
    return send_message(text, parse_mode=None)


def run_cmd(args: list[str]) -> tuple[str, int]:
    """Run a fixed diagnostic command without invoking a shell."""
    try:
        result = subprocess.run(args, shell=False, capture_output=True, text=True, timeout=30, check=False)
        return result.stdout.strip(), result.returncode
    except (OSError, subprocess.SubprocessError):
        return "", 1


def journal_output(*, since: str, until: str | None = None) -> str:
    args = ["journalctl", "-u", "bot.service", "--since", since, "--no-pager"]
    if until:
        args.extend(["--until", until])
    output, _status = run_cmd(args)
    return output


def directory_size(path: Path) -> int:
    try:
        return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file() and not entry.is_symlink())
    except OSError:
        return 0


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    # Characters that MUST be escaped in MarkdownV2
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


def run_health_check() -> str:
    """Run the full bot health check and return formatted report."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M %Z')

    # CHECK 1: Bot Service Status
    bot_status, _ = run_cmd(['systemctl', 'is-active', 'bot.service'])
    bot_running = bot_status == 'active'

    # CHECK 2: Ollama Status
    ollama_status, _ = run_cmd(['systemctl', 'is-active', 'ollama'])
    ollama_running = ollama_status == 'active'

    # CHECK 3: Disk Space
    usage = shutil.disk_usage('/')
    disk_pct = int((usage.used / usage.total) * 100) if usage.total else 0

    # CHECK 4: Study Guides Size
    guide_bytes = directory_size(config.PRIVATE_STUDY_GUIDES_DIR)
    study_size = f"{guide_bytes / (1024 * 1024):.1f} MiB"

    # CHECK 5: Google Scope Warnings (last 4h)
    recent_logs = journal_output(since='4 hours ago')
    scope_count = sum('not all requested scopes' in line.lower() for line in recent_logs.splitlines())

    # CHECK 6: Crashes (last 4h)
    crash_markers = ('crash', 'traceback', 'fatal', 'segfault')
    crash_count = sum(any(marker in line.lower() for marker in crash_markers) for line in recent_logs.splitlines())

    # CHECK 7: Nightly Log Errors (last 50 lines)
    try:
        nightly_out = (config.LOG_DIR / 'nightly.log').read_text(encoding='utf-8', errors='replace')[-20_000:]
    except OSError:
        nightly_out = ''
    nightly_errors = 0
    if 'ERROR' in nightly_out or 'FAILED' in nightly_out.upper():
        nightly_errors = 1  # flag if any errors found

    # CHECK 8: Morning Digest (last 12h) + specific 7 AM check
    digest_sent = any('digest' in line.lower() for line in journal_output(since='12 hours ago').splitlines())

    # CHECK 8b: Specific 7 AM today check
    today_7am = journal_output(since='today 07:00', until='today 07:10').lower()
    digest_7am_fired = any(marker in today_7am for marker in ('digest', 'morning', 'send_morning'))

    # Build report - use plain text to avoid MarkdownV2 escaping issues
    status_bot = 'running' if bot_running else 'stopped'
    status_ollama = 'running' if ollama_running else 'stopped (warning)'
    status_disk = f'{disk_pct}% used' + (' (warning)' if disk_pct > 85 else '')

    report = f"Bot Health Check - {now}\n\n"
    report += f"Service Status:\n"
    report += f"- Telegram bot: {status_bot}\n"
    report += f"- Ollama: {status_ollama}\n\n"

    report += f"Recent Issues (last 4h):\n"
    report += f"- Google scope warnings: {scope_count}\n"
    report += f"- Crashes: {crash_count}\n"
    report += f"- Nightly log errors: {nightly_errors}\n\n"

    report += f"Disk: {status_disk}\n\n"
    report += f"Nightly Study Guides: {study_size}\n\n"

    issues = []
    if not ollama_running:
        issues.append("Ollama service inactive - nightly pipeline needs it. Fix: sudo systemctl start ollama")
    if scope_count > 5:
        issues.append(f"High Google scope warnings ({scope_count}) - monitor auth")
    if disk_pct > 85:
        issues.append(f"Disk at {disk_pct}% - cleanup needed")
    if crash_count > 0:
        issues.append(f"{crash_count} crash(es) detected - check journalctl")
    if nightly_errors:
        issues.append("Nightly log has errors - check nightly.log")
    if not digest_7am_fired:
        issues.append("Morning digest did NOT fire at 7:00 AM today - check JobQueue scheduling")

    if issues:
        report += "Issues found:\n"
        for issue in issues:
            report += f"- {issue}\n"
    else:
        report += "All clear - bot healthy, no crashes, nightly pipeline clean, disk OK, digest sent"

    return report


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 telegram_notify.py "your message here"')
        print('       python3 telegram_notify.py --health-check')
        sys.exit(1)

    if sys.argv[1] == '--health-check':
        message = run_health_check()
    else:
        message = ' '.join(sys.argv[1:])

    result = send_plain_message(message)
    print(result)
