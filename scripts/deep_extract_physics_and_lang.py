#!/usr/bin/env python3
"""Deep Target Crawler for AP Physics 1 (Lang) and AP English Language (Jimenez).

Captures all assignments, WebAssign homework links, announcements, help session booking links,
module items, and embedded pages.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scrapers.assignment_calendar import AssignmentCalendarService
from scrapers.canvas_page_extractor import _fetch_external_link_text, extract_assignments_from_html
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("physics_lang_extractor")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "Canvas"


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("=" * 60)
    logger.info("⚡ DEEP EXTRACTOR: AP PHYSICS 1 & AP ENGLISH LANGUAGE")
    logger.info("=" * 60)

    try:
        client.connect()
        courses = client.get_favorite_courses()
        target_courses = [
            c for c in courses if any(k in (c.get("name") or "").lower() for k in ["physics", "lang", "jimenez"])
        ]

        logger.info("Found %d target courses: %s", len(target_courses), [c.get("name") for c in target_courses])

        for c in target_courses:
            cid = c.get("id")
            cname = c.get("name") or "Course"
            course_dir = OUT_DIR / sanitize(cname)
            course_dir.mkdir(parents=True, exist_ok=True)

            logger.info("\n" + "=" * 50)
            logger.info("🔍 EXTRACTING: %s (ID: %s)", cname, cid)
            logger.info("=" * 50)

            endpoints = [
                (f"https://forsyth.instructure.com/courses/{cid}", "Course Home"),
                (f"https://forsyth.instructure.com/courses/{cid}/assignments", "All Assignments & WebAssign"),
                (f"https://forsyth.instructure.com/courses/{cid}/announcements", "Course Announcements"),
                (f"https://forsyth.instructure.com/courses/{cid}/modules", "Course Modules"),
            ]

            # Also fetch all individual page URLs from API
            try:
                pages_api = client.get_paginated(f"/api/v1/courses/{cid}/pages?per_page=50", max_pages=3)
                for p in pages_api:
                    purl = p.get("url")
                    ptitle = p.get("title") or "Page"
                    if purl:
                        endpoints.append((f"https://forsyth.instructure.com/courses/{cid}/pages/{purl}", ptitle))
            except Exception:
                pass

            # Also fetch all assignments from API
            try:
                asgs_api = client.get_paginated(f"/api/v1/courses/{cid}/assignments?per_page=50", max_pages=3)
                for a in asgs_api:
                    a_html = a.get("html_url")
                    atitle = a.get("name") or "Assignment"
                    if a_html:
                        endpoints.append((a_html, f"Assignment: {atitle}"))
            except Exception:
                pass

            # Deduplicate endpoints
            seen = set()
            unique_endpoints = []
            for ep, title in endpoints:
                if ep not in seen:
                    seen.add(ep)
                    unique_endpoints.append((ep, title))

            logger.info("Scanning %d distinct pages, assignments, and modules for %s", len(unique_endpoints), cname)

            for idx, (url, title) in enumerate(unique_endpoints, start=1):
                logger.info("  [%d/%d] 👁️ %s -> %s", idx, len(unique_endpoints), title[:40], url)
                try:
                    try:
                        client.driver.get(url)
                    except Exception:
                        try:
                            client.driver.execute_script("window.stop();")
                        except Exception:
                            pass
                    time.sleep(1.5)

                    # Extract body text + iframes
                    main_text = client.driver.execute_script("return document.body ? document.body.innerText : '';") or ""
                    
                    # Extract external links
                    ext_links = client.driver.execute_script("""
                        const res = [];
                        for (const a of document.querySelectorAll('a[href]')) {
                            const h = a.href || '';
                            if (h.includes('docs.google.com') || h.includes('drive.google.com') || h.includes('cengage') || h.includes('webassign') || h.includes('calendly') || h.includes('forms.gle')) {
                                res.push({href: h, text: (a.innerText || '').trim()});
                            }
                        }
                        return res;
                    """) or []

                    ext_docs = []
                    for ext in ext_links[:5]:
                        h = ext.get("href", "")
                        label = ext.get("text", "Linked Resource")
                        if any(k in h for k in ["docs.google.com", "drive.google.com"]):
                            doc_t = _fetch_external_link_text(h)
                            if doc_t:
                                ext_docs.append(f"\n[EXTERNAL DOC: {label} ({h})]:\n{doc_t}\n")
                        else:
                            ext_docs.append(f"\n[EXTERNAL PORTAL LINK: {label} -> {h}]\n")

                    full_compiled = main_text + "\n" + "\n".join(ext_docs)

                    if len(full_compiled.strip()) > 30:
                        doc_file = course_dir / f"{sanitize(title)[:60]}.md"
                        doc_file.write_text(
                            f"# {cname}: {title}\n\n**Source URL**: {url}\n\n{full_compiled}\n",
                            encoding="utf-8",
                        )
                        logger.info("     ✓ Saved note: %s (%d chars)", doc_file.name, len(full_compiled))

                except Exception as e:
                    logger.debug("Failed extracting %s: %s", url, e)

        # Ingest and vectorize
        logger.info("\n🧠 Updating vector embeddings with Physics and Lang notes...")
        import subprocess
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)
        logger.info("✅ Deep extraction for AP Physics 1 and AP English Language complete!")

    finally:
        client.close()


if __name__ == "__main__":
    main()
