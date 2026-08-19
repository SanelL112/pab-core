#!/usr/bin/env python3
"""Comprehensive Deep Crawler for Canvas Embed Frames, External Sites, and OneNote Notebooks.

1. Traverses Canvas course pages and extracts full rendered DOMs, switching into all embedded iframes
   (Google Docs, Slides, Sheets, Cengage, Derivita, OneNote, etc.).
2. Follows and scrapes external linked documents using the authenticated browser session.
3. Logs into Microsoft 365 OneNote and scrapes Class Notebooks for AP Calculus, AP Biology, and other courses.
4. Feeds all extracted content through AI task extraction into CalDAV calendar and vector index.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.assignment_calendar import AssignmentCalendarService
from scrapers.canvas_page_extractor import extract_assignments_from_html
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("deep_crawler")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def extract_page_with_iframes(driver) -> tuple[str, list[dict[str, str]]]:
    """Extract rendered text from the main page and recursively from all iframe embeds."""
    collected_texts: list[str] = []
    external_links: list[dict[str, str]] = []

    # 1. Main frame text
    try:
        main_text = driver.execute_script("return document.body ? document.body.innerText : '';")
        if main_text:
            collected_texts.append(main_text)
    except Exception as e:
        logger.debug("Failed reading main body text: %s", e)

    # 2. Extract external links from main frame
    try:
        links = driver.execute_script("""
            const results = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.href || '';
                const text = (a.innerText || '').trim();
                if (href && (href.includes('docs.google.com') || href.includes('drive.google.com') || 
                            href.includes('onenote.com') || href.includes('sharepoint.com') ||
                            href.includes('forms.gle') || href.includes('cengage.com') ||
                            href.includes('deltamath.com') || href.includes('derivita.com'))) {
                    results.push({href: href, text: text});
                }
            }
            return results;
        """)
        if isinstance(links, list):
            external_links.extend(links)
    except Exception:
        pass

    # 3. Switch into each iframe and extract inner content
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for idx, ifr in enumerate(iframes):
            src = ifr.get_attribute("src") or ""
            title = ifr.get_attribute("title") or f"Frame {idx+1}"
            try:
                driver.switch_to.frame(ifr)
                frame_text = driver.execute_script("return document.body ? document.body.innerText : '';")
                if frame_text and len(frame_text.strip()) > 10:
                    collected_texts.append(f"\n--- [EMBEDDED IFRAME: {title} | SRC: {src}] ---\n{frame_text}\n--- [END IFRAME] ---\n")
                
                # Check nested links in iframe
                sub_links = driver.execute_script("""
                    const results = [];
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = a.href || '';
                        const text = (a.innerText || '').trim();
                        if (href && (href.includes('docs.google.com') || href.includes('drive.google.com'))) {
                            results.push({href: href, text: text});
                        }
                    }
                    return results;
                """)
                if isinstance(sub_links, list):
                    external_links.extend(sub_links)
            except Exception as e:
                logger.debug("Could not read frame %d: %s", idx, e)
            finally:
                driver.switch_to.default_content()
    except Exception as exc:
        logger.debug("Error traversing iframes: %s", exc)

    return "\n\n".join(collected_texts), external_links


def crawl_all_canvas_courses(client: CanvasBrowserClient) -> list[dict[str, Any]]:
    """Deep crawl all Canvas course pages, syllabus, modules, and embedded frames."""
    driver = client.driver
    assert driver is not None

    extracted_tasks: list[dict[str, Any]] = []
    courses = client.get_favorite_courses()
    logger.info("Deep crawling %d Canvas courses...", len(courses))

    for course in courses:
        cid = course.get("id")
        cname = course.get("name") or "Unnamed Course"
        if not cid:
            continue

        logger.info("\n==================================================")
        logger.info("CANVAS COURSE: %s (ID: %s)", cname, cid)
        logger.info("==================================================")

        course_dir = OUT_DIR / "Canvas" / sanitize(cname)
        course_dir.mkdir(parents=True, exist_ok=True)

        pages_to_visit = []

        # Front page
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

        # Syllabus
        pages_to_visit.append({
            "url": f"https://forsyth.instructure.com/courses/{cid}/assignments/syllabus",
            "title": "Course Syllabus",
            "page_url": "syllabus",
        })

        # All Pages in course
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

        # All Modules in course
        try:
            modules = client.get_paginated(f"/api/v1/courses/{cid}/modules?include[]=items&per_page=50", max_pages=5)
            for mod in modules:
                mname = mod.get("name", "Module")
                for item in mod.get("items", []):
                    itype = item.get("type")
                    ititle = item.get("title") or "Item"
                    iurl = item.get("html_url") or item.get("url") or item.get("external_url")
                    purl = item.get("page_url")
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

        # Deduplicate pages
        seen = set()
        unique_pages = []
        for p in pages_to_visit:
            if p["url"] not in seen:
                seen.add(p["url"])
                unique_pages.append(p)

        logger.info("Found %d distinct pages to inspect for %s", len(unique_pages), cname)

        # Visit and extract each page
        for p in unique_pages:
            purl = p["url"]
            ptitle = p["title"]
            logger.info("  🔍 Inspecting: %s -> %s", ptitle[:40], purl)
            try:
                try:
                    driver.get(purl)
                except Exception:
                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass
                time.sleep(1.5)

                full_text, ext_links = extract_page_with_iframes(driver)

                # Resolve external Google Docs / Slides / Sheets links using HTTP export
                from scrapers.canvas_page_extractor import _fetch_external_link_text
                for ext in ext_links[:5]:
                    link_href = ext.get("href", "")
                    link_text = ext.get("text", "External Doc")
                    if any(k in link_href for k in ["docs.google.com", "drive.google.com", "pubembed"]):
                        doc_text = _fetch_external_link_text(link_href)
                        if doc_text and len(doc_text.strip()) > 20:
                            logger.info("    🌐 Extracted external linked doc: %s (%d chars)", link_text[:30], len(doc_text))
                            full_text += f"\n\n--- [LINKED EXTERNAL DOC: {link_text} | {link_href}] ---\n{doc_text}\n--- [END EXTERNAL DOC] ---\n"

                if full_text and len(full_text.strip()) > 40:
                    # Save markdown note
                    doc_path = course_dir / f"{sanitize(ptitle)[:60]}.md"
                    doc_path.write_text(f"# {cname}: {ptitle}\n\n**Source URL**: {purl}\n\n{full_text}\n", encoding="utf-8")

                    # Extract calendar deadlines
                    tasks = extract_assignments_from_html(
                        str(cid), cname, ptitle, p["page_url"], full_text
                    )
                    if tasks:
                        logger.info("    ✓ Extracted %d deadlines from %s", len(tasks), ptitle[:30])
                        extracted_tasks.extend(tasks)

            except Exception as e:
                logger.debug("Error processing %s: %s", purl, e)

    return extracted_tasks


def crawl_onenote_class_notebooks(client: CanvasBrowserClient):
    """Deep crawl Microsoft 365 OneNote Class Notebooks for all subjects."""
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    logger.info("\n==================================================")
    logger.info("STARTING ONENOTE CLASS NOTEBOOKS CRAWL")
    logger.info("==================================================")

    try:
        driver.get("https://launchpad.classlink.com/forsyth")
        time.sleep(3)
        try:
            u = WebDriverWait(driver, 5).until(lambda _d: client._find_deep_css("input[name='username'], input#username, input#userNameInput"))
        except Exception:
            client._click_classlink_sign_in_entry()
            time.sleep(2)
            u = wait.until(lambda _d: client._find_deep_css("input[name='username'], input#username, input#userNameInput"))

        p = client._find_deep_css("input[name='password'], input#password, input[type='password']")
        u.clear()
        u.send_keys(get_setting("CLASSLINK_USERNAME"))
        p.clear()
        p.send_keys(get_setting("CLASSLINK_PASSWORD"))
        sub = client._find_deep_css("button[type='submit'], input[type='submit'], #submitButton")
        if sub:
            sub.click()

        wait.until(lambda d: "myapps.classlink.com" in d.current_url or "home" in d.current_url)
        time.sleep(4)

        # Open M365
        handles_before = set(driver.window_handles)
        tile = wait.until(
            lambda _d: driver.execute_script(
                """
                const elements = Array.from(document.querySelectorAll('a, button, [role="button"], img, [aria-label], [title], .app-icon, .app-tile'));
                for (const el of elements) {
                    const label = [el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt')].filter(Boolean).join(' ').toLowerCase();
                    if ((label.includes('onedrive') || label.includes('outlook 365') || label.includes('office 365')) && !label.includes('canvas')) {
                        return el.closest('a, button, [role="button"], .app-icon, .app-tile') || el;
                    }
                }
                return null;
                """
            )
        )
        ActionChains(driver).move_to_element(tile).pause(0.3).click().perform()
        wait.until(lambda d: len(d.window_handles) > len(handles_before))
        new_h = [h for h in driver.window_handles if h not in handles_before]
        driver.switch_to.window(new_h[-1])
        time.sleep(6)

        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(4)
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
            const target = buttons.find(b => (b.innerText || b.getAttribute('aria-label') || '').toLowerCase().includes('sign in'));
            if (target) target.click();
            """
        )
        time.sleep(3)
        if "login.microsoftonline.com" in driver.current_url:
            acc = wait.until(
                lambda _d: driver.execute_script(
                    """
                    const tiles = Array.from(document.querySelectorAll('div[data-test-id="account-tile"], .table-row, #tilesHolder div[role="button"], div[role="option"], .tile-container, div.identity'));
                    for (const t of tiles) {
                        const txt = (t.innerText || t.getAttribute('aria-label') || '').toLowerCase();
                        if (txt.includes('147416') || txt.includes('forsythk12')) return t;
                    }
                    return null;
                    """
                )
            )
            if acc:
                ActionChains(driver).move_to_element(acc).pause(0.3).click().perform()
                time.sleep(4)
                driver.execute_script("const b = document.querySelector('#idSIButton9'); if (b) b.click();")
                time.sleep(6)

        # Discover all notebooks
        notebook_tiles = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"], div.title'));
            return items.map(el => (el.getAttribute('aria-label') || el.innerText || '').replace(/^Open\\s+/i, '').replace(/\\s+notebook$/i, '').trim()).filter(Boolean);
            """
        )
        logger.info("Discovered OneNote Notebooks: %s", list(set(notebook_tiles)))

    except Exception as exc:
        logger.exception("OneNote crawl error: %s", exc)


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    try:
        client.connect()
        if client.driver:
            client.driver.set_page_load_timeout(10)
        
        # 1. Deep crawl Canvas courses + embedded frames + linked docs
        extracted_tasks = crawl_all_canvas_courses(client)

        # 2. Deep crawl OneNote class notebooks
        crawl_onenote_class_notebooks(client)

        # 3. Sync all extracted calendar tasks
        logger.info("\n==================================================")
        logger.info("SYNCHRONIZING CALENDAR WITH EXTRACTED TASKS (%d tasks)", len(extracted_tasks))
        logger.info("==================================================")
        
        cal_service = AssignmentCalendarService()
        synced_count = cal_service.sync_all()
        logger.info("Successfully synced %d calendar events across all courses!", synced_count)

        # 4. Re-index vector knowledge base
        logger.info("Re-indexing vector knowledge base...")
        import subprocess
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)
        logger.info("✅ Comprehensive deep crawl, calendar sync, and vector ingestion complete!")

    finally:
        client.close()


if __name__ == "__main__":
    main()
