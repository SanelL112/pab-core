"""Browser-Backed Microsoft OneNote Web Scraper.

Connects to OneNote Online via an authenticated local browser profile
(reusing the existing Firefox ClassLink/School profile) to extract notebook sections,
pages, and lecture notes without requiring Azure App registration or tenant admin consent.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
ONENOTE_PAGES_DIR = _ROOT / "onenote_pages"
ACADEMIC_NOTES_DIR = _ROOT / "academic_notes"


def extract_onenote_html_to_markdown(html_content: str, title: str = "Untitled Note") -> str:
    """Convert raw OneNote Web HTML into clean, structured Markdown."""
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    # Remove script and style tags
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    # Extract tables into readable markdown tables
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text().strip()) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            table_md = "\n" + "\n".join(rows) + "\n"
            table.replace_with(table_md)

    text = soup.get_text(separator="\n")
    # Clean excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    md = f"# {title}\n\n{text}\n"
    return md


def save_extracted_page(notebook_name: str, section_name: str, page_title: str, content: str) -> Path:
    """Save an extracted OneNote page into the academic notes repository."""
    safe_nb = re.sub(r"[^\w\-_]", "_", notebook_name)
    safe_sec = re.sub(r"[^\w\-_]", "_", section_name)
    safe_title = re.sub(r"[^\w\-_]", "_", page_title)

    target_dir = ACADEMIC_NOTES_DIR / safe_nb / safe_sec
    target_dir.mkdir(parents=True, exist_ok=True)

    file_path = target_dir / f"{safe_title}.md"
    file_path.write_text(content, encoding="utf-8")
    logger.info("Saved OneNote page: %s", file_path)
    return file_path
