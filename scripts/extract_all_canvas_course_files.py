#!/usr/bin/env python3
"""Extract and Index All Embedded Canvas Files, PDFs, DOCXs, and Worksheets.

Especially targets AP Physics 1 (Lang) and AP English Language (Jimenez) where
the core curriculum, guided notes, reading packets, and problem sets are embedded as files.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scrapers.assignment_calendar import AssignmentCalendarService
from scrapers.canvas_page_extractor import extract_assignments_from_html
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("canvas_file_extractor")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "Canvas"
DOWNLOAD_CACHE = _ROOT / "academic_notes" / "downloaded_files"
DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def extract_text_from_file(file_path: Path) -> str:
    """Extract plain text from PDF, DOCX, or text files using pdftotext/python."""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        try:
            res = subprocess.run(
                ["pdftotext", "-layout", str(file_path), "-"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception as e:
            logger.debug("pdftotext failed on %s: %e", file_path.name, e)
    elif suffix in [".txt", ".md", ".csv"]:
        try:
            return file_path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            pass
    elif suffix in [".docx", ".doc"]:
        try:
            import docx
            doc = docx.Document(str(file_path))
            return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        except Exception:
            pass
    return ""


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("=" * 60)
    logger.info("📥 EXTRACTING ALL CANVAS EMBEDDED FILES & PDFS (LANG, PHYSICS, ETC.)")
    logger.info("=" * 60)

    try:
        client.connect()
        courses = client.get_favorite_courses()
        total_files_extracted = 0
        all_tasks: list[dict[str, Any]] = []

        for course in courses:
            cid = course.get("id")
            cname = course.get("name") or "Unnamed Course"
            if not cid:
                continue

            logger.info("\n" + "=" * 50)
            logger.info("📚 COURSE: %s (ID: %s)", cname, cid)
            logger.info("=" * 50)

            course_files_dir = OUT_DIR / sanitize(cname) / "files"
            course_files_dir.mkdir(parents=True, exist_ok=True)

            discovered_files: list[dict[str, Any]] = []
            seen_urls = set()

            # 1. Enumerate all Module items of type 'File'
            try:
                modules = client.get_paginated(f"/api/v1/courses/{cid}/modules?include[]=items&per_page=50", max_pages=5)
                for mod in modules:
                    mname = mod.get("name", "Module")
                    for item in mod.get("items", []):
                        if item.get("type") == "File":
                            f_id = item.get("content_id") or item.get("id")
                            f_title = item.get("title") or f"File_{f_id}"
                            f_url = item.get("url")
                            if f_url and f_url not in seen_urls:
                                seen_urls.add(f_url)
                                discovered_files.append({
                                    "title": f_title,
                                    "api_url": f_url,
                                    "module": mname,
                                    "id": str(f_id),
                                })
            except Exception as e:
                logger.debug("Failed fetching module files for %s: %s", cname, e)

            # 2. Enumerate Files API
            try:
                files_api = client.get_paginated(f"/api/v1/courses/{cid}/files?per_page=100", max_pages=5)
                for f in files_api:
                    f_name = f.get("display_name") or f.get("filename") or "Attachment"
                    f_url = f.get("url")
                    f_id = str(f.get("id") or "")
                    if f_url and f_url not in seen_urls:
                        seen_urls.add(f_url)
                        discovered_files.append({
                            "title": f_name,
                            "download_url": f_url,
                            "id": f_id,
                            "module": "Course Files",
                        })
            except Exception as e:
                logger.debug("Files API not accessible or empty for %s: %s", cname, e)

            logger.info("Found %d embedded files & attachments in %s", len(discovered_files), cname)

            for idx, item in enumerate(discovered_files, start=1):
                ftitle = item["title"]
                mname = item.get("module", "Files")
                download_url = item.get("download_url")

                # If we have api_url, fetch file metadata to get download url
                if not download_url and item.get("api_url"):
                    try:
                        f_meta = client.get_json(item["api_url"])
                        if isinstance(f_meta, dict):
                            download_url = f_meta.get("url")
                    except Exception:
                        pass

                if not download_url:
                    continue

                try:
                    local_target = DOWNLOAD_CACHE / f"{cid}_{sanitize(ftitle)}"
                    import requests
                    cookies = {c['name']: c['value'] for c in client.driver.get_cookies()} if client.driver else {}
                    resp = requests.get(download_url, cookies=cookies, headers={"User-Agent": "Mozilla/5.0"}, timeout=25, stream=True)
                    if resp.ok:
                        with open(local_target, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=16384):
                                f.write(chunk)

                    if local_target.is_file() and local_target.stat().st_size > 0:
                        text_content = extract_text_from_file(local_target)
                        if text_content and len(text_content.strip()) > 30:
                            # Save as academic note
                            md_file = course_files_dir / f"{sanitize(ftitle)[:60]}.md"
                            md_content = (
                                f"# {cname}: {ftitle}\n"
                                f"**Module / Folder**: {mname}\n"
                                f"**Original File**: {ftitle}\n"
                                f"**Course ID**: {cid}\n\n"
                                f"## Extracted Document Content\n\n"
                                f"{text_content}\n"
                            )
                            md_file.write_text(md_content, encoding="utf-8")
                            total_files_extracted += 1
                            logger.info("     ✓ Extracted text: %d characters -> %s", len(text_content), md_file.name)

                            # Check for date patterns in document
                            date_matches = re.findall(r"\b(?:due|quiz|test|exam|deadline|homework|hw)[:\s]+([A-Za-z]+ \d{1,2}|\d{1,2}/\d{1,2})", text_content, re.I)
                            if date_matches:
                                logger.info("     ✨ Found %d deadline references in %s", len(date_matches), ftitle[:30])

                except Exception as exc:
                    logger.debug("Failed extracting file %s: %s", ftitle, exc)

        logger.info("\n" + "=" * 60)
        logger.info("✅ Extracted text from %d embedded files & attachments across all courses!", total_files_extracted)
        logger.info("=" * 60)

        # Sync CalDAV Calendar
        if all_tasks:
            logger.info("Synchronizing calendar with %d newly extracted deadlines...", len(all_tasks))
            cal_service = AssignmentCalendarService()
            synced_count = cal_service.sync_all()
            logger.info("CalDAV synced with %d total events!", synced_count)

        # Re-index vector knowledge base
        logger.info("Re-indexing vector knowledge base...")
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)
        logger.info("🎉 All embedded PDFs, DOCXs, and worksheets extracted and indexed!")

    finally:
        client.close()


if __name__ == "__main__":
    main()
