#!/usr/bin/env python3
"""Multi-Column OneNote Web Scraper:
1. Expands _Content Library in Sections Tree (Left Column).
2. Clicks each Sub-Section.
3. Finds the Pages Bar next to it (Middle Column).
4. Clicks through every individual Page one by one.
5. Extracts full lesson notes from Canvas (Right Column).
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

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pages_bar_crawler")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote" / "AP_Biology_Bleier_26-27"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING MULTI-COLUMN PAGES BAR ONENOTE SCRAPER")
    logger.info("==================================================")

    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        # [Step 1] ClassLink Sign-in
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

        # [Step 2] Click M365 tile
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

        # [Step 3] OneNote Sign-in
        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(4)
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
            const target = buttons.find(b => (b.innerText || b.getAttribute('aria-label') || '').toLowerCase().includes('sign in'));
            if (target) target.click();
            """
        )

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

        # [Step 4] Open AP Biology
        elem = wait.until(
            lambda _d: driver.execute_script(
                """
                const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
                return items.find(el => (el.getAttribute('aria-label') || '').includes('AP Biology') || (el.innerText || '').includes('AP Biology'));
                """
            )
        )
        hb = set(driver.window_handles)
        ActionChains(driver).move_to_element(elem).pause(0.3).click().perform()
        time.sleep(10)

        nh = [h for h in driver.window_handles if h not in hb]
        notebook_window = nh[-1]
        driver.switch_to.window(notebook_window)
        time.sleep(8)

        # Handle Verify Sign-in inside iframe if present
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            try:
                driver.switch_to.frame(frame)
                driver.execute_script(
                    """
                    const btn = Array.from(document.querySelectorAll('button, a, [role="button"]')).find(b => (b.innerText || '').toLowerCase().includes('sign in'));
                    if (btn) btn.click();
                    """
                )
                time.sleep(4)
                driver.switch_to.default_content()
            except Exception:
                driver.switch_to.default_content()

        # Switch to popup if opened
        for handle in driver.window_handles:
            if handle not in hb and handle != notebook_window:
                driver.switch_to.window(handle)
                driver.execute_script("const tile = Array.from(document.querySelectorAll('div[data-test-id=\"account-tile\"], .table-row, div.identity')).find(t => (t.innerText || '').includes('147416')); if (tile) tile.click();")
                time.sleep(4)
                driver.execute_script("const b = document.querySelector('#idSIButton9'); if (b) b.click();")
                time.sleep(4)

        driver.switch_to.window(notebook_window)
        time.sleep(8)

        # [Step 5] Switch into WacFrame iframe
        wac_frames = driver.find_elements(By.CSS_SELECTOR, "iframe#WacFrame_OneNote_0, iframe[name*='WacFrame'], iframe")
        if wac_frames:
            driver.switch_to.frame(wac_frames[0])
            logger.info("Switched into WacFrame!")

        # [Step 6] Locate and expand _Content Library
        logger.info("[Step 6] Expanding '_Content Library' in Left Column...")
        driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[role="button"], span, div, a, .sectionGroupItem'));
            const target = items.find(el => {
                const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                return txt === '_content library' || txt === 'content library' || txt.includes('content library');
            });
            if (target) {
                target.scrollIntoView({block: 'center'});
                target.style.border = '4px solid lime';
                target.style.boxShadow = '0 0 25px #00ff00';
                target.click();
            }
            """
        )
        time.sleep(5)

        # [Step 7] Find Sub-Sections under _Content Library
        sub_sections = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll(
                'div[role="treeitem"]:not([aria-expanded="true"]), div[data-automationid*="Section"], .sectionItem, .section-item, [role="tab"], div.sectionName, div[aria-level="2"], div[aria-level="3"]'
            ));
            return items.map(s => ({
                name: (s.innerText || s.getAttribute('aria-label') || '').split('\\n')[0].trim()
            })).filter(s => s.name.length > 1 && !['home', 'insert', 'draw', 'view', 'help', 'file', 'content library', '_content library', 'lathiya, sanel'].includes(s.name.toLowerCase()));
            """
        )

        logger.info("Discovered %d sub-section(s) under _Content Library: %s", len(sub_sections), [s["name"] for s in sub_sections])

        if not sub_sections:
            sub_sections = [{"name": "Unit_Material"}]

        # [Step 8] Iterate Sub-Sections and Click Pages in the Pages Bar
        for sec in sub_sections:
            sec_name = sec["name"]
            sec_dir = OUT_DIR / sanitize(sec_name)
            sec_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n==================================================")
            logger.info("--> SELECTING SUB-SECTION: %s", sec_name)
            logger.info("==================================================")

            # Click sub-section
            driver.execute_script(
                f"""
                const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[data-automationid*="Section"], .sectionItem, .section-item, [role="tab"], div.sectionName, span'));
                const match = items.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(sec_name)}));
                if (match) {{
                    match.scrollIntoView({{block: 'center'}});
                    match.style.border = '3px solid cyan';
                    match.click();
                }}
                """
            )
            time.sleep(4)

            # [Step 8.1] Query the Pages Bar Column (Middle Column next to sections)
            page_elements = driver.execute_script(
                """
                // Query pages list container and rows
                const pageRows = Array.from(document.querySelectorAll(
                    '#pageList div[role="option"], #pageList div[role="listitem"], .pageRow, .page-item, [data-automationid*="PageItem"], div[role="listbox"] div[role="option"], div.pages-pane div[role="option"], a.pageRow, div[role="row"][data-automationid*="Page"]'
                ));
                
                if (pageRows.length === 0) {
                    // Fallback to any page-like item
                    const fallback = Array.from(document.querySelectorAll('div[role="option"], div[role="listitem"], div.pageTitle'));
                    return fallback.map(p => ({
                        name: (p.innerText || p.getAttribute('aria-label') || '').split('\\n')[0].trim()
                    })).filter(p => p.name.length > 1 && !p.name.toLowerCase().includes('untitled page'));
                }

                return pageRows.map(p => ({
                    name: (p.innerText || p.getAttribute('aria-label') || '').split('\\n')[0].trim()
                })).filter(p => p.name.length > 1);
                """
            )

            logger.info("  Found %d page(s) in Pages Bar for '%s': %s", len(page_elements), sec_name, [p["name"] for p in page_elements[:8]])

            if not page_elements:
                page_elements = [{"name": sec_name}]

            # [Step 8.2] Click through every individual page in the Pages Bar
            for i, pg in enumerate(page_elements):
                pg_name = pg["name"]
                logger.info("    [%d/%d] 📄 Clicking Page in Bar: '%s'", i+1, len(page_elements), pg_name)

                driver.execute_script(
                    f"""
                    const pageRows = Array.from(document.querySelectorAll(
                        '#pageList div[role="option"], #pageList div[role="listitem"], .pageRow, .page-item, [data-automationid*="PageItem"], div[role="listbox"] div[role="option"], div.pages-pane div[role="option"], a.pageRow, div[role="row"][data-automationid*="Page"], div[role="option"], div[role="listitem"]'
                    ));
                    const match = pageRows.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(pg_name)}));
                    if (match) {{
                        match.scrollIntoView({{block: 'center'}});
                        match.style.border = '4px solid lime';
                        match.style.boxShadow = '0 0 20px lime';
                        match.click();
                    }}
                    """
                )
                time.sleep(4)

                # Extract canvas notes
                canvas_text = driver.execute_script(
                    """
                    // Check inside canvas frame or main page content
                    const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], .WACPageContent, div[data-testid="page-container"], #active_page_frame');
                    if (canvas) {
                        return canvas.innerText;
                    }
                    return document.body.innerText;
                    """
                )

                if canvas_text and len(canvas_text.strip()) > 30:
                    clean_title = sanitize(pg_name)
                    page_file = sec_dir / f"{clean_title}.md"
                    md_doc = f"# AP Biology - {sec_name}: {pg_name}\n\n**Class**: AP Biology Bleier 26-27\n**Section**: {sec_name}\n**Page**: {pg_name}\n**Source**: Microsoft OneNote Web Canvas\n\n{canvas_text}\n"
                    page_file.write_text(md_doc, encoding="utf-8")
                    logger.info("      ✓ Successfully extracted page notes: %s (%d chars)", page_file.name, len(canvas_text))

        driver.switch_to.default_content()
        logger.info("\n==================================================")
        logger.info("✓ ALL PAGES IN _CONTENT LIBRARY EXTRACTED SUCCESSFULLY!")
        logger.info("==================================================")

        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)
        time.sleep(30)

    except Exception as exc:
        logger.exception("Crawler error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
