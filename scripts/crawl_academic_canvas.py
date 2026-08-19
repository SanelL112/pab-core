#!/usr/bin/env python3
"""Deep Academic Canvas Crawler.

Extracts full lesson modules, syllabus bodies, lecture pages, and assignment rubrics
for all active academic courses from Canvas, saving them into `academic_notes/`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scrapers.canvas_scraper import CanvasBrowserClient, _parse_canvas_page_html
from scrapers.lfm_vision_harness import extract_external_platform_links, fetch_google_doc_or_slides_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("academic_canvas_crawler")

ACADEMIC_DIR = _ROOT / "academic_notes"


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name).strip("_")


def crawl_course(client: CanvasBrowserClient, course: dict[str, Any]):
    course_id = course.get("id")
    course_name = course.get("name") or f"Course_{course_id}"
    logger.info("==================================================")
    logger.info("Crawling Academic Course: %s (ID: %s)", course_name, course_id)
    logger.info("==================================================")

    course_folder = ACADEMIC_DIR / sanitize_filename(course_name)
    course_folder.mkdir(parents=True, exist_ok=True)

    # 1. Syllabus
    syllabus_body = course.get("syllabus_body")
    if not syllabus_body:
        try:
            detail, _ = client._request_json(f"/api/v1/courses/{course_id}?include[]=syllabus_body")
            syllabus_body = detail.get("syllabus_body") if isinstance(detail, dict) else None
        except Exception as exc:
            logger.debug("Could not fetch syllabus for %s: %s", course_name, exc)

    if syllabus_body:
        s_text, _ = _parse_canvas_page_html(syllabus_body)
        if len(s_text) > 20:
            (course_folder / "Syllabus.md").write_text(f"# {course_name} - Syllabus\n\n{s_text}\n", encoding="utf-8")
            logger.info("Saved Syllabus for %s (%d chars)", course_name, len(s_text))

    # 2. Modules & Lesson Items
    try:
        modules = client.get_paginated(f"/api/v1/courses/{course_id}/modules?include[]=items&per_page=50", max_pages=3)
        logger.info("Found %d module(s) in %s", len(modules), course_name)
        for mod in modules:
            mod_name = mod.get("name") or "Unnamed Module"
            mod_folder = course_folder / sanitize_filename(mod_name)
            mod_folder.mkdir(parents=True, exist_ok=True)

            items = mod.get("items") or []
            for item in items:
                title = item.get("title") or "Item"
                itype = item.get("type")
                page_url = item.get("page_url")
                html_url = item.get("html_url")

                if itype == "Page" and page_url:
                    try:
                        pdata = client.get_json(f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}")
                        body = pdata.get("body") if isinstance(pdata, dict) else ""
                        p_text, _ = _parse_canvas_page_html(body)
                        if len(p_text) > 30:
                            # Check for embedded Google Docs/Slides
                            ext_links = extract_external_platform_links(body)
                            ext_text = ""
                            for el in ext_links:
                                if el["type"] in ("Google Doc", "Google Slides"):
                                    fetched = fetch_google_doc_or_slides_text(el["url"])
                                    if fetched:
                                        ext_text += f"\n\n### Linked {el['type']}: {el['title']}\n{fetched}\n"

                            full_content = f"# {title}\n**Module:** {mod_name}\n\n{p_text}\n{ext_text}"
                            (mod_folder / f"{sanitize_filename(title)}.md").write_text(full_content, encoding="utf-8")
                            logger.info("  Saved Module Page: %s / %s", mod_name, title)
                    except Exception as exc:
                        logger.debug("Could not fetch page %s: %s", page_url, exc)
                elif itype == "ExternalUrl" and html_url:
                    ext_text = fetch_google_doc_or_slides_text(html_url)
                    if ext_text:
                        full_content = f"# {title}\n**Module:** {mod_name}\n**External URL:** {html_url}\n\n{ext_text}\n"
                        (mod_folder / f"{sanitize_filename(title)}.md").write_text(full_content, encoding="utf-8")
                        logger.info("  Saved External Doc: %s / %s", mod_name, title)
    except Exception as exc:
        logger.warning("Could not crawl modules for %s: %s", course_name, exc)

    # 3. Assignments & Rubrics
    try:
        assignments = client.get_paginated(
            f"/api/v1/courses/{course_id}/assignments?include[]=rubric&order_by=due_at&per_page=50",
            max_pages=2,
        )
        logger.info("Found %d assignment(s) in %s", len(assignments), course_name)
        assign_folder = course_folder / "Assignments"
        assign_folder.mkdir(parents=True, exist_ok=True)

        for a in assignments:
            aname = a.get("name") or "Assignment"
            due = a.get("due_at") or "No due date"
            desc_html = a.get("description") or ""
            desc_text, _ = _parse_canvas_page_html(desc_html)
            if len(desc_text) > 30:
                doc = f"# {aname}\n**Course:** {course_name}\n**Due Date:** {due}\n\n## Instructions\n\n{desc_text}\n"
                (assign_folder / f"{sanitize_filename(aname)}.md").write_text(doc, encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not crawl assignments for %s: %s", course_name, exc)


def main():
    print("=" * 60)
    print("  DEEP ACADEMIC CANVAS CRAWLER")
    print("=" * 60)
    client = CanvasBrowserClient(use_daemon=True)
    courses = client.get_favorite_courses()
    print(f"Found {len(courses)} enrolled courses in Canvas.\n")

    for course in courses:
        crawl_course(client, course)

    print("\n✓ Academic Canvas Crawl Complete!")


if __name__ == "__main__":
    main()
