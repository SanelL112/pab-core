#!/usr/bin/env python3
"""Navigate directly to AP Biology -> Click _Content Library -> Click Page -> Extract Notes."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("content_library_crawler")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote" / "AP_Biology_Bleier_26-27"
OUT_DIR.mkdir(parents=True, exist_ok=True)

NOTEBOOK_URL = "https://forsythk12org-my.sharepoint.com/:o:/r/personal/kbleier54_forsythk12_org/_layouts/15/doc2.aspx?sourcedoc=%7B1CE6594E-309A-4DB4-B1DB-7FD0841C66BD%7D&file=AP%20Biology%20Bleier%2026-27&action=edit&mobileredirect=true&wdorigin=Sharepoint"


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("CLICKING _CONTENT LIBRARY & EXTRACTING PAGES")
    logger.info("==================================================")

    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        # [Step 1] ClassLink Sign-in to ensure authenticated session
        logger.info("[Step 1] ClassLink Sign-in...")
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

        # [Step 2] Navigate directly to AP Biology Notebook URL
        logger.info("[Step 2] Opening AP Biology Notebook on SharePoint...")
        driver.get(NOTEBOOK_URL)
        time.sleep(12)
        logger.info("AP Biology Notebook Loaded: %s (%s)", driver.title, driver.current_url)

        # Helper function to find in document or iframes
        def execute_in_all_frames(js_code, *args):
            # Try top document
            res = driver.execute_script(js_code, *args)
            if res:
                return res
            # Try all iframes
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for frame in iframes:
                try:
                    driver.switch_to.frame(frame)
                    res = driver.execute_script(js_code, *args)
                    if res:
                        return res
                    # Try nested iframe
                    nested = driver.find_elements(By.TAG_NAME, "iframe")
                    for nframe in nested:
                        driver.switch_to.frame(nframe)
                        res = driver.execute_script(js_code, *args)
                        if res:
                            return res
                        driver.switch_to.parent_frame()
                    driver.switch_to.default_content()
                except Exception:
                    driver.switch_to.default_content()
            driver.switch_to.default_content()
            return None

        # [Step 3] Locate and click _Content Library
        logger.info("[Step 3] Finding and clicking '_Content Library'...")
        clicked_content_lib = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[role="button"], span, div, a'));
            const target = items.find(el => {
                const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                return txt === '_content library' || txt === 'content library' || txt.includes('_content library');
            });
            if (target) {
                target.scrollIntoView({block: 'center'});
                target.style.border = '4px solid lime';
                target.style.boxShadow = '0 0 25px #00ff00';
                target.click();
                return target.innerText || 'Content Library';
            }
            return null;
            """
        )
        logger.info("Clicked Content Library: %s", clicked_content_lib)
        time.sleep(5)

        # [Step 4] Enumerate all Sections inside _Content Library
        logger.info("[Step 4] Discovering Section tabs inside _Content Library...")
        sections = driver.execute_script(
            """
            const secElements = Array.from(document.querySelectorAll('div[role="treeitem"], div[data-automationid*="Section"], .section-item, [role="tab"]'));
            return secElements.map(el => ({
                name: (el.innerText || el.getAttribute('aria-label') || '').split('\\n')[0].trim()
            })).filter(s => s.name.length > 1 && !s.name.toLowerCase().includes('notebook') && !s.name.toLowerCase().includes('content library'));
            """
        )

        logger.info("Discovered %d section(s): %s", len(sections), [s["name"] for s in sections])

        # [Step 5] Iterate through Sections and Pages
        for sec in sections[:5]:  # Crawl first 5 sections
            sec_name = sec["name"]
            sec_dir = OUT_DIR / sanitize(sec_name)
            sec_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n--> Selecting Section: %s", sec_name)

            # Click section
            driver.execute_script(
                f"""
                const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[data-automationid*="Section"], .section-item, [role="tab"]'));
                const match = items.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(sec_name)}));
                if (match) {{
                    match.scrollIntoView({{block: 'center'}});
                    match.style.border = '3px solid cyan';
                    match.click();
                }}
                """
            )
            time.sleep(4)

            # Discover all Pages in this Section
            pages = driver.execute_script(
                """
                const pageNodes = Array.from(document.querySelectorAll('div[role="listitem"], div[role="option"], [data-automationid*="Page"], .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"]'));
                return pageNodes.map(p => ({
                    name: (p.innerText || p.getAttribute('aria-label') || '').split('\\n')[0].trim()
                })).filter(p => p.name.length > 1 && !p.name.toLowerCase().includes('untitled page'));
                """
            )

            logger.info("  Found %d page(s) in '%s': %s", len(pages), sec_name, [p["name"] for p in pages[:5]])

            # Click and extract each page
            for pg in pages:
                pg_name = pg["name"]
                logger.info("    📄 Clicking Page: %s", pg_name)

                driver.execute_script(
                    f"""
                    const pageNodes = Array.from(document.querySelectorAll('div[role="listitem"], div[role="option"], [data-automationid*="Page"], .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"]'));
                    const match = pageNodes.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(pg_name)}));
                    if (match) {{
                        match.scrollIntoView({{block: 'center'}});
                        match.style.border = '3px solid lime';
                        match.style.boxShadow = '0 0 15px lime';
                        match.click();
                    }}
                    """
                )
                time.sleep(4)

                # Extract rich canvas text
                canvas_text = driver.execute_script(
                    """
                    const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], #active_page_frame, .WACPageContent, div[data-testid="page-container"]');
                    return canvas ? canvas.innerText : document.body.innerText;
                    """
                )

                if canvas_text and len(canvas_text.strip()) > 30:
                    page_file = sec_dir / f"{sanitize(pg_name)}.md"
                    md_doc = f"# AP Biology - {sec_name}: {pg_name}\n\n**Class**: AP Biology Bleier 26-27\n**Section**: {sec_name}\n**Page**: {pg_name}\n**Source**: Microsoft OneNote Web\n\n{canvas_text}\n"
                    page_file.write_text(md_doc, encoding="utf-8")
                    logger.info("      ✓ Extracted: %s (%d chars)", page_file.name, len(canvas_text))

        time.sleep(15)
        logger.info("\n✓ Finished scraping _Content Library! Keeping browser open on screen.")

    except Exception as exc:
        logger.exception("Error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
