#!/bin/bash
# Bot Health Monitor — runs every 4 hours via systemd timer.
# Sends a dynamic health report to Telegram.
#
# Logs: journalctl -u bot-health.service
#
set -euo pipefail

BOT_DIR="/home/sanel/personal-assistant-bot"
TELEGRAM_TOKEN=$(grep TELEGRAM_BOT_TOKEN "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"')
CHAT_ID="8534649457"
PYTHON_BIN="$BOT_DIR/venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

# ── Gather health data ────────────────────────────────────────────────────────

# service_state <unit> — return the systemd active-state as a single word.
# `systemctl is-active` prints the state (active/activating/failed/...) to
# stdout but exits non-zero for anything other than "active". The old
# `$(... || echo unknown)` captured BOTH the real state AND "unknown" on a
# second line (hence the "activating\nunknown" garbage in reports). Capture
# stdout unconditionally and fall back to "unknown" only when it's empty.
service_state() {
    local s
    s=$(systemctl is-active "$1" 2>/dev/null)
    [ -n "$s" ] && echo "$s" || echo "unknown"
}

# For a service that may be mid-restart, re-check once after a short pause so
# a transient "activating"/"deactivating" doesn't fire a false DOWN alarm.
service_state_settled() {
    local s
    s=$(service_state "$1")
    if [ "$s" = "activating" ] || [ "$s" = "deactivating" ] || [ "$s" = "reloading" ]; then
        sleep 5
        s=$(service_state "$1")
    fi
    echo "$s"
}

# Bot service
BOT_ACTIVE=$(service_state_settled bot.service)
BOT_UPTIME=$(systemctl show bot.service -p ActiveEnterTimestamp --value 2>/dev/null)
[ -n "$BOT_UPTIME" ] || BOT_UPTIME="?"
BOT_PID=$(systemctl show bot.service -p MainPID --value 2>/dev/null)
[ -n "$BOT_PID" ] || BOT_PID="?"

# Ollama
OLLAMA_ACTIVE=$(service_state_settled ollama.service)
OLLAMA_MODELS=$(curl -sf --max-time 3 http://localhost:11434/api/tags 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('models',[])))" 2>/dev/null || echo "?")

# Disk
DISK_PCT=$(df -h / | tail -1 | awk '{print $5}' | tr -d '%')
DISK_FREE=$(df -h / | tail -1 | awk '{print $4}')

# Recent errors (last 4 hours)
# grep -c exits non-zero when there are 0 matches; the old `|| echo 0`
# appended a second "0" line (hence "Recent errors (4h): 0\n0"). Force a
# single clean integer with wc -l instead.
ERROR_COUNT=$( { journalctl -u bot.service --no-pager --since "4 hours ago" 2>/dev/null || true; } | { grep -icE "error|fail|traceback" || true; } | tr -d ' ')
[ -n "$ERROR_COUNT" ] || ERROR_COUNT="0"

# File sizes
STATE_KB=$(du -k "$BOT_DIR/state.json" 2>/dev/null | cut -f1 || echo "0")
SUMMARIES_KB=$(du -k "$BOT_DIR/cache/combined_summaries.txt" 2>/dev/null | cut -f1 || echo "0")
BRAIN_KB=$(du -k "$BOT_DIR/curated_brain.md" 2>/dev/null | cut -f1 || echo "0")
INDEX_KB=$(du -k "$BOT_DIR/mega_index.md" 2>/dev/null | cut -f1 || echo "0")

# Nightly queue
QUEUE_SIZE=$(python3 -c "import json; print(len(json.load(open('$BOT_DIR/nightly_queue.json'))))" 2>/dev/null || echo "0")

# Last digest
LAST_DIGEST="never"
if [ -f "$BOT_DIR/latest_digest.txt" ]; then
    LAST_DIGEST=$(stat -c %y "$BOT_DIR/latest_digest.txt" 2>/dev/null | cut -d. -f1 || echo "unknown")
fi

# Google scrapers
GOOGLE_TOKEN=$("$PYTHON_BIN" -c "
import json, os, sys
sys.path.insert(0, '$BOT_DIR')
os.chdir('$BOT_DIR')
tok = os.path.join('$BOT_DIR', 'token.json')
if not os.path.exists(tok):
    print('missing'); sys.exit()
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    d = json.load(open(tok))
    creds = Credentials.from_authorized_user_info(d, d.get('scopes'))
    if creds.valid:
        print('valid')
    elif creds.expired and creds.refresh_token:
        # Try a refresh so the check reflects whether the bot CAN recover on its
        # own (it does this per-call). Persist so downstream calls reuse it.
        try:
            creds.refresh(Request())
            open(tok, 'w').write(creds.to_json())
            print('valid')
        except Exception:
            print('expired')
    else:
        print('expired')
except Exception:
    print('error')
" 2>/dev/null || echo "error")

# ── Build report ──────────────────────────────────────────────────────────────

# Status emoji: active = ✅, transient (activating/deactivating/reloading) = ⏳
# (informational, no alarm), anything else = 🚨/⚠️.
status_emoji() {
    case "$1" in
        active) echo "✅" ;;
        activating|deactivating|reloading) echo "⏳" ;;
        *) echo "$2" ;;  # $2 = severity emoji for a genuinely bad state
    esac
}

BOT_STATUS_EMOJI=$(status_emoji "$BOT_ACTIVE" "🚨")
OLLAMA_STATUS_EMOJI=$(status_emoji "$OLLAMA_ACTIVE" "⚠️")

# Only treat a service as truly down/degraded when it's neither active nor
# in a transient restart state — avoids false alarms during a normal restart.
is_bad_state() {
    case "$1" in
        active|activating|deactivating|reloading) return 1 ;;
        *) return 0 ;;
    esac
}

# Clean up the Ollama model count for display.
if [ "$OLLAMA_ACTIVE" = "active" ] && [ "$OLLAMA_MODELS" = "?" ]; then
    OLLAMA_MODELS_DISPLAY="count unavailable"
else
    OLLAMA_MODELS_DISPLAY="$OLLAMA_MODELS models loaded"
fi

REPORT=$(cat << ENDREPORT
🤖 **Bot Health Check** — $(date '+%Y-%m-%d %H:%M %Z')

**Services:**
• Telegram bot: $BOT_STATUS_EMOJI $BOT_ACTIVE (since $(echo $BOT_UPTIME | cut -d' ' -f2-3))
• Ollama: $OLLAMA_STATUS_EMOJI $OLLAMA_ACTIVE ($OLLAMA_MODELS_DISPLAY)

**System:**
• Disk: ${DISK_PCT}% used (${DISK_FREE} free)
• Google token: $GOOGLE_TOKEN
• Recent errors (4h): $ERROR_COUNT

**Data:**
• State: ${STATE_KB}KB
• Summaries: ${SUMMARIES_KB}KB
• Curated brain: ${BRAIN_KB}KB
• Mega index: ${INDEX_KB}KB
• Nightly queue: $QUEUE_SIZE files
• Last digest: $LAST_DIGEST

$([ "$ERROR_COUNT" -gt 0 ] && echo "⚠️ $ERROR_COUNT errors in last 4 hours — check journalctl -u bot.service")
$([ "$GOOGLE_TOKEN" = "expired" ] && echo "⚠️ Google token expired — run google_auth_setup.py")
$([ "$GOOGLE_TOKEN" = "missing" ] && echo "🚨 No token.json found!")
$(is_bad_state "$OLLAMA_ACTIVE" && echo "⚠️ Ollama is not running — nightly pipeline will fail")
$(is_bad_state "$BOT_ACTIVE" && echo "🚨 Bot is DOWN — check systemctl status bot.service")
ENDREPORT
)

# ── Send to Telegram ──────────────────────────────────────────────────────────

curl -sf --max-time 10 -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  -d "text=${REPORT}" \
  -d "parse_mode=Markdown" \
  > /dev/null 2>&1 && echo "$(date -Iseconds) Health report sent OK" || echo "$(date -Iseconds) Failed to send health report"
