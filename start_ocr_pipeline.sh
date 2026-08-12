#!/bin/bash
set -euo pipefail

cd /home/sanel/personal-assistant-bot

echo "Starting historical export (Tesseract OCR) at $(date)..."
/home/sanel/personal-assistant-bot/venv/bin/python scrapers/historical_export.py

echo "Starting mega study guide builder at $(date)..."
/home/sanel/personal-assistant-bot/venv/bin/python run_builder.py

echo "Pipeline finished. Generated private artifacts are not committed or pushed automatically."
