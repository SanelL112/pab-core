#!/usr/bin/env bash
# ==============================================================================
# Daily Academic Sync & Multimodal Deep Crawler Runner
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

export DISPLAY="${DISPLAY:-:2}"
export PYTHONUNBUFFERED=1

LOG_DIR="$HOME/.local/share/personal-assistant-bot/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_academic_sync_$(date +%Y%m%d_%H%M%S).log"

echo "============================================================" | tee -a "$LOG_FILE"
echo "🚀 STARTING DAILY ACADEMIC SYNC: $(date)" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"

# 1. Clean stale locks & orphan geckodriver processes
pkill -9 -f "firefox" 2>/dev/null || true
pkill -9 -f "geckodriver" 2>/dev/null || true
rm -f "$HOME/.local/share/personal-assistant-bot/canvas-firefox-profile/.parentlock" 2>/dev/null || true

# 2. Warm up local LFM 2.6B model in Ollama
echo "[1/4] Preloading LFM 2.6B model..." | tee -a "$LOG_FILE"
./venv/bin/python3 -c "
import requests
try:
    requests.post('http://127.0.0.1:11434/api/generate', json={
        'model': 'hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M',
        'prompt': 'Ready',
        'keep_alive': '60m',
        'options': {'num_predict': 2}
    }, timeout=60)
except Exception:
    pass
" 2>&1 | tee -a "$LOG_FILE" || true

# 3. Run LFM 2.6B + Vision Deep Crawler across all Canvas courses & embedded frames
echo "[2/4] Running LFM 2.6B + Vision Deep Crawler..." | tee -a "$LOG_FILE"
./venv/bin/python3 -u scripts/lfm_vision_deep_crawler.py 2>&1 | tee -a "$LOG_FILE"

# 4. Extract all embedded Canvas PDFs, Word docs, and worksheets
echo "[3/4] Extracting embedded Canvas course files & PDFs..." | tee -a "$LOG_FILE"
./venv/bin/python3 -u scripts/extract_all_canvas_course_files.py 2>&1 | tee -a "$LOG_FILE"

# 5. Final CalDAV Calendar & Vector Database Ingestion
echo "[4/4] Synchronizing CalDAV calendar & vector index..." | tee -a "$LOG_FILE"
./venv/bin/python3 -c "
from scrapers.assignment_calendar import AssignmentCalendarService
cal = AssignmentCalendarService()
cal.sync_all()
" 2>&1 | tee -a "$LOG_FILE"

./venv/bin/python3 -u scripts/ingest_academic_notes.py 2>&1 | tee -a "$LOG_FILE"

echo "============================================================" | tee -a "$LOG_FILE"
echo "✅ DAILY ACADEMIC SYNC COMPLETE: $(date)" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"
