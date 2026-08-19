#!/usr/bin/env python3
"""Targeted Scraper for Discovered OneNote Academic Class Notebooks.

Directly targets:
- AP Biology Bleier 26-27
- AP Calculus AB 2026-2027
- AP Stat 25-26
- All other notebooks listed under 'Show all Classic notebooks'

Extracts sections, pages, and canvas notes into academic_notes/OneNote/<Subject>/
and automatically triggers vector embedding indexing!
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

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("class_onenote_scraper")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING TARGETED CLASS NOTEBOOK EXTRACTION")
    logger.info("==================================================")

    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        # 1. Sign in via ClassLink
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

        # 2. Click M365 tile
        logger.info("[Step 2] Launching Microsoft 365...")
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

        # 3. OneNote
        logger.info("[Step 3] Navigating to OneNote...")
        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(4)
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
            const target = buttons.find(b => (b.innerText || b.getAttribute('aria-label') || '').toLowerCase().includes('sign in'));
            if (target) target.click();
            """
        )

        # 4. Account Picker
        logger.info("[Step 4] Selecting student account...")
        wait.until(lambda d: "login.microsoftonline.com" in d.current_url or "select_account" in d.current_url)
        time.sleep(3)
        acc = wait.until(
            lambda _d: driver.execute_script(
                """
                const tiles = Array.from(document.querySelectorAll('div[data-test-id="account-tile"], .table-row, #tilesHolder div[role="button"], div[role="option"], .tile-container, div.identity, div[aria-label*="@"], div.table, #tilesHolder div'));
                for (const t of tiles) {
                    const txt = (t.innerText || t.getAttribute('aria-label') || '').toLowerCase();
                    if (txt.includes('147416') || txt.includes('forsythk12') || (txt.length > 2 && !txt.includes('use another account'))) {
                        return t.closest('div[role="button"], .table-row, div[data-test-id="account-tile"]') || t;
                    }
                }
                return null;
                """
            )
        )
        ActionChains(driver).move_to_element(acc).pause(0.3).click().perform()
        time.sleep(5)
        driver.execute_script("const b = document.querySelector('#idSIButton9, input[type=\"submit\"][value*=\"Yes\"], button#acceptButton'); if (b) b.click();")
        wait.until(lambda d: "login.microsoftonline.com" not in d.current_url)
        time.sleep(6)

        # 5. Extract Discovered Class Notebooks
        logger.info("[Step 5] Discovering Class Notebooks in Sidebar...")

        notebook_buttons = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
            return items.map(el => ({
                label: el.getAttribute('aria-label') || '',
                title: (el.innerText || '').split('\\n')[0].trim()
            })).filter(n => n.title.length > 1);
            """
        )

        logger.info("Found %d targeted class notebook(s) on screen:", len(notebook_buttons))
        for nb in notebook_buttons:
            logger.info("  📚 %s (%s)", nb["title"], nb["label"])

        # Crawl each notebook deeply (Subfolders -> Sections -> Pages)
        for nb in notebook_buttons:
            title = nb["title"]
            label = nb["label"]
            logger.info("\n==================================================")
            logger.info("OPENING CLASS NOTEBOOK: %s", title)
            logger.info("==================================================")

            class_dir = OUT_DIR / sanitize(title)
            class_dir.mkdir(parents=True, exist_ok=True)

            elem = driver.execute_script(
                f"""
                const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
                const match = items.find(el => el.getAttribute('aria-label') === {json.dumps(label)} || (el.innerText || '').includes({json.dumps(title)}));
                if (match) {{
                    match.scrollIntoView({{block: 'center'}});
                    match.style.border = '4px solid lime';
                    match.style.boxShadow = '0 0 20px #00ff00';
                    return match;
                }}
                return null;
                """
            )

            if elem:
                handles_before_click = set(driver.window_handles)
                ActionChains(driver).move_to_element(elem).pause(0.3).click().perform()
                time.sleep(8)

                # If opened in new tab, switch
                new_tab = [h for h in driver.window_handles if h not in handles_before_click]
                if new_tab:
                    driver.switch_to.window(new_tab[-1])
                    logger.info("Switched to notebook window: %s", driver.title)
                    time.sleep(8)

                # [Step 5.1] Expand all Sub-Folders and Section Groups
                logger.info("[Step 5.1] Expanding all sub-folders and section groups on left panel...")
                driver.execute_script(
                    """
                    // Expand all collapsed section groups / folders
                    const expanders = Array.from(document.querySelectorAll(
                        'button[aria-expanded="false"], div[aria-expanded="false"], [data-automationid*="ExpandCollapse"], .expand-collapse-button, [aria-label*="Expand"]'
                    ));
                    expanders.forEach(b => {
                        b.style.border = '2px solid yellow';
                        b.click();
                    });
                    """
                )
                time.sleep(3)

                # [Step 5.2] Enumerate all Sections
                sections = driver.execute_script(
                    """
                    const secNodes = Array.from(document.querySelectorAll(
                        'div[role="treeitem"][aria-level="2"], div[role="treeitem"][aria-level="3"], div[data-automationid*="Section"], .section-item, [role="tab"][data-automationid*="Section"], div[role="treeitem"]:not([aria-expanded])'
                    ));
                    return secNodes.map(s => ({
                        name: (s.innerText || s.getAttribute('aria-label') || '').split('\\n')[0].trim()
                    })).filter(s => s.name.length > 1 && !s.name.toLowerCase().includes('notebook'));
                    """
                )
                logger.info("Discovered %d section(s) in %s: %s", len(sections), title, [s["name"] for s in sections[:5]])

                if not sections:
                    # Fallback generic section
                    sections = [{"name": "General"}]

                # [Step 5.3] Iterate Sections and Pages
                for sec in sections:
                    sec_name = sec["name"]
                    sec_dir = class_dir / sanitize(sec_name)
                    sec_dir.mkdir(parents=True, exist_ok=True)
                    logger.info("--> Navigating to Section: %s", sec_name)

                    # Click Section
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
                    time.sleep(3)

                    # Discover all Individual Pages in this Section
                    page_items = driver.execute_script(
                        """
                        const pages = Array.from(document.querySelectorAll(
                            'div[role="listitem"], div[role="option"], [data-automationid*="Page"], .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"]'
                        ));
                        return pages.map(p => ({
                            name: (p.innerText || p.getAttribute('aria-label') || '').split('\\n')[0].trim()
                        })).filter(p => p.name.length > 1 && !p.name.toLowerCase().includes('untitled page'));
                        """
                    )

                    logger.info("  Found %d page(s) in section '%s': %s", len(page_items), sec_name, [p["name"] for p in page_items[:5]])

                    if not page_items:
                        # Extract the current open page directly
                        page_items = [{"name": sec_name}]

                    # Click and scrape each Individual Page
                    for pg in page_items:
                        pg_name = pg["name"]
                        logger.info("    📄 Extracting Page: %s", pg_name)

                        # Highlight and click page
                        driver.execute_script(
                            f"""
                            const pages = Array.from(document.querySelectorAll('div[role="listitem"], div[role="option"], [data-automationid*="Page"], .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"]'));
                            const match = pages.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(pg_name)}));
                            if (match) {{
                                match.scrollIntoView({{block: 'center'}});
                                match.style.border = '3px solid lime';
                                match.style.boxShadow = '0 0 10px lime';
                                match.click();
                            }}
                            """
                        )
                        time.sleep(3)

                        # Extract rich page text from canvas
                        canvas_text = driver.execute_script(
                            """
                            const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], #active_page_frame, .WACPageContent, div[data-testid="page-container"]');
                            return canvas ? canvas.innerText : document.body.innerText;
                            """
                        )

                        if canvas_text and len(canvas_text.strip()) > 20:
                            page_file = sec_dir / f"{sanitize(pg_name)}.md"
                            md_doc = f"# {title} - {sec_name}: {pg_name}\n\n**Class**: {title}\n**Section**: {sec_name}\n**Page**: {pg_name}\n**Source**: Microsoft OneNote Web\n\n{canvas_text}\n"
                            page_file.write_text(md_doc, encoding="utf-8")
                            logger.info("      ✓ Saved notes: %s (%d chars)", page_file.name, len(canvas_text))

                # Return to main tab if notebook opened in a new tab
                if new_tab and len(driver.window_handles) > 1:
                    driver.close()
                    driver.switch_to.window(list(handles_before_click)[0])
                    time.sleep(3)

        logger.info("\n==================================================")
        logger.info("✓ ALL CLASS NOTEBOOKS EXTRACTED! Re-indexing vector DB...")
        logger.info("==================================================")

        # Trigger auto-ingest and vector embedding
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)

    except Exception as exc:
        logger.exception("Scraper error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
