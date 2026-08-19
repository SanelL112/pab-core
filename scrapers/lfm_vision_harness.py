"""Liquid Foundation Model (LFM 2.6B) Vision & External Resource Extraction Harness.

Crawls Canvas pages, OneNote notes, and academic documents to:
1. Detect external resources (Google Docs, Google Slides, OneNote links, Cengage, Canva).
2. Resolve embedded diagrams, homework images, and lecture slide screenshots via
   local LFM 2.6B / Ollama multimodal vision inference (with Tesseract OCR fallback).
3. Save structured markdown summaries into `academic_notes/` for vector embedding.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
ACADEMIC_NOTES_DIR = _ROOT / "academic_notes"
EXTRACTED_PAGES_DIR = _ROOT / "extracted_pages"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
LFM_2_6B_MODEL = os.getenv("LFM_2_6B_MODEL", "hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M")


def extract_external_platform_links(html_content: str) -> list[dict[str, str]]:
    """Find all external academic links and embedded iframes."""
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    found_resources: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    # 1. Iframes (Embedded Google Slides, Docs, Forms, Videos)
    for ifr in soup.find_all("iframe"):
        src = str(ifr.get("src", "") or "").strip()
        title = str(ifr.get("title", "Embedded Document") or "Embedded Document").strip()
        if src and src not in seen_urls:
            seen_urls.add(src)
            found_resources.append({"type": "iframe", "title": title, "url": src})

    # 2. Links to third-party academic portals
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", "") or "").strip()
        label = a.get_text(strip=True) or href
        if not href or href in seen_urls or href.startswith(("#", "javascript:", "mailto:")):
            continue

        low = href.lower()
        platform = None
        if "docs.google.com/document" in low:
            platform = "Google Doc"
        elif "docs.google.com/presentation" in low:
            platform = "Google Slides"
        elif "docs.google.com/forms" in low or "forms.gle" in low:
            platform = "Google Form"
        elif "onenote" in low or "sharepoint.com" in low or "1drv.ms" in low:
            platform = "OneNote / OneDrive"
        elif "cengage" in low or "deltamath" in low or "quizlet" in low or "canva.com" in low:
            platform = "Educational Platform"

        if platform:
            seen_urls.add(href)
            found_resources.append({"type": platform, "title": label, "url": href})

    return found_resources


def fetch_google_doc_or_slides_text(url: str, timeout: float = 10.0) -> str:
    """Fetch public Google Docs or Google Slides text export."""
    export_url = None
    doc_match = re.search(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", url)
    if doc_match:
        export_url = f"https://docs.google.com/document/d/{doc_match.group(1)}/export?format=txt"
    else:
        slide_match = re.search(r"docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)", url)
        if slide_match:
            export_url = f"https://docs.google.com/presentation/d/{slide_match.group(1)}/export/txt"

    if not export_url:
        return ""

    try:
        req = urllib.request.Request(
            export_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            return content.strip()
    except Exception as exc:
        logger.debug("Could not fetch export for %s: %s", url, exc)
        return ""


def analyze_image_with_lfm_vision(image_path: str | Path, prompt: str = "Transcribe all text, formulas, and diagrams accurately:") -> str:
    """Pass an image or slide diagram through local LFM 2.6B / OCR."""
    img_path = Path(image_path)
    if not img_path.is_file():
        return ""

    # 1. Try local OCR first for fast text extraction
    ocr_text = ""
    try:
        res = subprocess.run(
            ["tesseract", str(img_path), "stdout", "--oem", "1", "-l", "eng"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            ocr_text = res.stdout.strip()
    except Exception as exc:
        logger.debug("Tesseract OCR fallback failed: %s", exc)

    # 2. Query LFM 2.6B on Ollama for visual analysis
    try:
        with open(img_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")

        with httpx.Client(timeout=httpx.Timeout(45.0)) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": LFM_2_6B_MODEL,
                    "prompt": f"{prompt}\n\nOCR hint:\n{ocr_text[:500]}",
                    "images": [b64_img],
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 350},
                },
            )
            if resp.status_code == 200:
                out = resp.json().get("response", "").strip()
                if out:
                    return out
    except Exception as exc:
        logger.debug("LFM 2.6B vision inference failed: %s", exc)

    return ocr_text


def process_and_save_external_resource(
    course_name: str,
    resource_title: str,
    resource_url: str,
    raw_content: str,
) -> Path:
    """Save an extracted external academic resource into `academic_notes/`."""
    safe_course = re.sub(r"[^\w\-_]", "_", course_name)
    safe_title = re.sub(r"[^\w\-_]", "_", resource_title)[:60]

    target_dir = ACADEMIC_NOTES_DIR / safe_course / "external_resources"
    target_dir.mkdir(parents=True, exist_ok=True)

    file_path = target_dir / f"{safe_title}.md"
    body = (
        f"# {resource_title}\n"
        f"**Course:** {course_name}\n"
        f"**Source URL:** {resource_url}\n\n"
        f"## Content / Lesson Notes\n\n"
        f"{raw_content}\n"
    )
    file_path.write_text(body, encoding="utf-8")
    logger.info("Saved academic resource: %s", file_path)
    return file_path
