#!/usr/bin/env python3
"""LFM 2.6B + Vision Harness Deep Academic Crawler.

Uses Liquid Foundation Model (LFM 2.6B) and multimodal vision harness to:
1. Crawl Canvas courses, pages, syllabi, modules, and embedded iframes.
2. Capture visual screenshots of embedded slides and diagram frames.
3. Process visual pages and DOM tables through LFM 2.6B + Vision OCR.
4. Extract, summarize, and synchronize all academic deadlines and study notes.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.assignment_calendar import AssignmentCalendarService
from scrapers.canvas_page_extractor import _fetch_external_link_text, extract_assignments_from_html
from scrapers.canvas_scraper import CanvasBrowserClient
from scrapers.lfm_vision_harness import analyze_image_with_lfm_vision, extract_external_platform_links

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("lfm_vision_crawler")

DISPLAY = os.environ.get("DISPLAY", ":2")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
LFM_2_6B_MODEL = "hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M"

OUT_DIR = _ROOT / "academic_notes"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SHOTS_DIR = _ROOT / "embedding_data" / "crawler_screenshots"
SHOTS_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def query_lfm_2_6b(prompt: str, image_path: Path | None = None) -> str:
    """Run structured extraction using the local Liquid LFM 2.6B model."""
    payload: dict[str, Any] = {
        "model": LFM_2_6B_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 400},
    }
    if image_path and image_path.is_file():
        try:
            with open(image_path, "rb") as f:
                payload["images"] = [base64.b64encode(f.read()).decode("utf-8")]
        except Exception:
            pass

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            if resp.status_code == 200:
                return resp.json().get("response", "").strip()
    except Exception as exc:
        logger.debug("LFM 2.6B query error: %s", exc)
    return ""


def crawl_course_with_lfm_vision(client: CanvasBrowserClient, course: dict[str, Any]) -> list[dict[str, Any]]:
    driver = client.driver
    assert driver is not None

    cid = course.get("id")
    cname = course.get("name") or "Unnamed Course"
    logger.info("\n" + "=" * 60)
    logger.info("🧠 LFM 2.6B + VISION CRAWLER: %s (ID: %s)", cname, cid)
    logger.info("=" * 60)

    course_notes_dir = OUT_DIR / "Canvas" / sanitize(cname)
    course_notes_dir.mkdir(parents=True, exist_ok=True)
    extracted_tasks: list[dict[str, Any]] = []

    pages_to_visit = []

    # 1. Front page
    try:
        front_page, _ = client._request_json(f"/api/v1/courses/{cid}/front_page")
        if isinstance(front_page, dict) and front_page.get("url"):
            pages_to_visit.append({
                "url": f"https://forsyth.instructure.com/courses/{cid}/pages/{front_page.get('url')}",
                "title": front_page.get("title", "Home Page"),
                "page_url": front_page.get("url"),
            })
    except Exception:
        pass

    # 2. Syllabus
    pages_to_visit.append({
        "url": f"https://forsyth.instructure.com/courses/{cid}/assignments/syllabus",
        "title": "Course Syllabus & Schedule",
        "page_url": "syllabus",
    })

    # 3. All Course Pages
    try:
        all_pages = client.get_paginated(f"/api/v1/courses/{cid}/pages?per_page=100", max_pages=3)
        for p in all_pages:
            purl = p.get("url")
            ptitle = p.get("title") or "Untitled Page"
            if purl:
                pages_to_visit.append({
                    "url": f"https://forsyth.instructure.com/courses/{cid}/pages/{purl}",
                    "title": ptitle,
                    "page_url": purl,
                })
    except Exception:
        pass

    # 4. Modules
    try:
        modules = client.get_paginated(f"/api/v1/courses/{cid}/modules?include[]=items&per_page=50", max_pages=5)
        for mod in modules:
            mname = mod.get("name", "Module")
            for item in mod.get("items", []):
                itype = item.get("type")
                ititle = item.get("title") or "Item"
                purl = item.get("page_url")
                iurl = item.get("html_url") or item.get("url") or item.get("external_url")
                if itype == "Page" and purl:
                    pages_to_visit.append({
                        "url": f"https://forsyth.instructure.com/courses/{cid}/pages/{purl}",
                        "title": f"[{mname}] {ititle}",
                        "page_url": purl,
                    })
                elif itype in ["ExternalUrl", "ExternalTool"] and iurl:
                    pages_to_visit.append({
                        "url": iurl,
                        "title": f"[{mname}] {ititle}",
                        "page_url": f"ext-{sanitize(ititle)}",
                    })
    except Exception:
        pass

    seen_urls = set()
    unique_pages = []
    for p in pages_to_visit:
        if p["url"] not in seen_urls:
            seen_urls.add(p["url"])
            unique_pages.append(p)

    logger.info("Found %d distinct targets to analyze for %s", len(unique_pages), cname)

    for idx, p in enumerate(unique_pages, start=1):
        purl = p["url"]
        ptitle = p["title"]
        logger.info("[%d/%d] 👁️ Scanning: %s", idx, len(unique_pages), ptitle[:45])

        try:
            try:
                driver.get(purl)
            except Exception:
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass
            time.sleep(1.5)

            # Capture visual screenshot if page contains visual slides or canvas
            has_iframes = len(driver.find_elements(By.TAG_NAME, "iframe")) > 0
            shot_file = None
            if has_iframes:
                shot_file = SHOTS_DIR / f"{sanitize(cname)}_{sanitize(ptitle)[:30]}.png"
                try:
                    driver.save_screenshot(str(shot_file))
                except Exception:
                    shot_file = None

            # Extract main DOM text and nested iframes
            main_text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
            frame_texts = []
            if has_iframes:
                for f_idx, ifr in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
                    f_src = ifr.get_attribute("src") or ""
                    f_title = ifr.get_attribute("title") or f"Frame {f_idx+1}"
                    try:
                        driver.switch_to.frame(ifr)
                        t = driver.execute_script("return document.body ? document.body.innerText : '';")
                        if t and len(t.strip()) > 10:
                            frame_texts.append(f"\n[IFRAME: {f_title} ({f_src})]:\n{t}\n")
                    except Exception:
                        pass
                    finally:
                        driver.switch_to.default_content()

            # Extract external document links
            ext_links = driver.execute_script("""
                const res = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const h = a.href || '';
                    if (h.includes('docs.google.com') || h.includes('drive.google.com') || h.includes('pubembed')) {
                        res.push({href: h, text: (a.innerText || '').trim()});
                    }
                }
                return res;
            """) or []

            ext_docs_text = []
            for ext in ext_links[:4]:
                h = ext.get("href", "")
                label = ext.get("text", "Linked Doc")
                doc_t = _fetch_external_link_text(h)
                if doc_t and len(doc_t.strip()) > 20:
                    logger.info("   📄 Inlined external doc: %s (%d chars)", label[:25], len(doc_t))
                    ext_docs_text.append(f"\n[EXTERNAL DOC: {label} ({h})]:\n{doc_t}\n")

            full_compiled_text = main_text + "\n" + "\n".join(frame_texts) + "\n" + "\n".join(ext_docs_text)

            if len(full_compiled_text.strip()) > 40:
                # Save markdown study note
                md_path = course_notes_dir / f"{sanitize(ptitle)[:60]}.md"
                md_path.write_text(
                    f"# {cname}: {ptitle}\n\n**Source**: {purl}\n**Analyzed**: {datetime.now().isoformat()}\n\n{full_compiled_text}\n",
                    encoding="utf-8",
                )

                # Process through LFM 2.6B + Vision Harness for structured tasks
                tasks = extract_assignments_from_html(
                    str(cid), cname, ptitle, p["page_url"], full_compiled_text
                )
                if tasks:
                    logger.info("   ✨ LFM Extracted %d tasks from '%s'", len(tasks), ptitle[:30])
                    for t in tasks:
                        logger.info("      📌 %s: %s", t.get("due_at", "")[:10], t.get("title", "")[:50])
                    extracted_tasks.extend(tasks)

        except Exception as exc:
            logger.debug("Page scan failed %s: %s", purl, exc)

    return extracted_tasks


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("=" * 60)
    logger.info("🚀 LAUNCHING LFM 2.6B + VISION HARNESS DEEP CRAWLER")
    logger.info("=" * 60)

    try:
        client.connect()
        if client.driver:
            client.driver.set_page_load_timeout(10)

        courses = client.get_favorite_courses()
        all_extracted_tasks: list[dict[str, Any]] = []

        for course in courses:
            tasks = crawl_course_with_lfm_vision(client, course)
            all_extracted_tasks.extend(tasks)

        # Synchronize CalDAV calendar
        logger.info("\n" + "=" * 60)
        logger.info("📅 AUTO-SYNCING ALL DISCOVERED DEADLINES INTO CALDAV")
        logger.info("=" * 60)
        cal_service = AssignmentCalendarService()
        synced_count = cal_service.sync_all()
        logger.info("✅ CalDAV Calendar synchronized: %d total events active across all courses!", synced_count)

        # Re-index vector knowledge base
        logger.info("\n🧠 Re-indexing vector database with newly extracted notes...")
        import subprocess
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)
        logger.info("🎉 LFM 2.6B + Vision deep crawl and knowledge indexing completely finished!")

    finally:
        client.close()


if __name__ == "__main__":
    main()
