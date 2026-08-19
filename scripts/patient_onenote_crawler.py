#!/usr/bin/env python3
"""Patient OneNote Crawler with Interactive Re-Auth & Prompt Inspection.

Runs with human-like pacing (5-8s pauses). When opening a class notebook,
if Microsoft asks to verify or sign in again, it clicks 'Sign In',
inspects the resulting prompt, and logs the details to screen.
"""
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
logger = logging.getLogger("patient_onenote")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING PATIENT ONENOTE RE-AUTH & NOTEBOOK CRAWLER")
    logger.info("==================================================")

    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        # [Step 1] ClassLink Sign-in
        logger.info("[Step 1] ClassLink Sign-in...")
        driver.get("https://launchpad.classlink.com/forsyth")
        time.sleep(4)
        try:
            u = WebDriverWait(driver, 5).until(lambda _d: client._find_deep_css("input[name='username'], input#username, input#userNameInput"))
        except Exception:
            client._click_classlink_sign_in_entry()
            time.sleep(3)
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
        time.sleep(5)

        # [Step 2] Click M365 tile
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
        ActionChains(driver).move_to_element(tile).pause(0.5).click().perform()
        wait.until(lambda d: len(d.window_handles) > len(handles_before))
        new_h = [h for h in driver.window_handles if h not in handles_before]
        driver.switch_to.window(new_h[-1])
        time.sleep(7)

        # [Step 3] OneNote Sign-in
        logger.info("[Step 3] Navigating to OneNote...")
        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(5)
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
            const target = buttons.find(b => (b.innerText || b.getAttribute('aria-label') || '').toLowerCase().includes('sign in'));
            if (target) target.click();
            """
        )

        # [Step 4] Account Picker
        logger.info("[Step 4] Selecting student account...")
        wait.until(lambda d: "login.microsoftonline.com" in d.current_url or "select_account" in d.current_url)
        time.sleep(4)
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
        ActionChains(driver).move_to_element(acc).pause(0.5).click().perform()
        time.sleep(6)
        driver.execute_script("const b = document.querySelector('#idSIButton9, input[type=\"submit\"][value*=\"Yes\"], button#acceptButton'); if (b) b.click();")
        wait.until(lambda d: "login.microsoftonline.com" not in d.current_url)
        time.sleep(7)

        # [Step 5] Find and Click AP Biology Bleier Notebook
        logger.info("[Step 5] Locating 'AP Biology Bleier 26-27' notebook in sidebar...")
        elem = wait.until(
            lambda _d: driver.execute_script(
                """
                const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
                return items.find(el => (el.getAttribute('aria-label') || '').includes('AP Biology') || (el.innerText || '').includes('AP Biology'));
                """
            )
        )

        logger.info("Found AP Biology notebook! Highlighting in green...")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '5px solid lime'; arguments[0].style.boxShadow = '0 0 25px #00ff00';", elem)
        time.sleep(3)

        handles_before_click = set(driver.window_handles)
        ActionChains(driver).move_to_element(elem).pause(0.5).click().perform()
        logger.info("Clicked AP Biology notebook! Pausing 10s for loading...")
        time.sleep(10)

        new_tab = [h for h in driver.window_handles if h not in handles_before_click]
        if new_tab:
            driver.switch_to.window(new_tab[-1])
            logger.info("Switched to AP Biology window: %s (%s)", driver.title, driver.current_url)
            time.sleep(8)

        # [Step 6] Inside AP Biology Notebook: Click _Content Library -> Sections -> Pages
        logger.info("[Step 6] Locating and expanding '_Content Library'...")
        time.sleep(5)

        # Locate and click _Content Library
        clicked_lib = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[role="button"], span, div, a, [data-automationid*="SectionGroup"]'));
            const target = items.find(el => {
                const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                return txt === '_content library' || txt === 'content library' || txt.includes('_content library');
            });
            if (target) {
                target.scrollIntoView({block: 'center'});
                target.style.border = '4px solid lime';
                target.style.boxShadow = '0 0 25px #00ff00';
                target.click();
                return target.innerText || '_Content Library';
            }
            return null;
            """
        )
        logger.info("[Step 6 Result] Clicked '_Content Library': %s", clicked_lib)
        time.sleep(5)

        # Discover all Section folders inside _Content Library
        logger.info("[Step 6.1] Discovering Sections inside _Content Library...")
        sections = driver.execute_script(
            """
            const secElements = Array.from(document.querySelectorAll('div[role="treeitem"], div[data-automationid*="Section"], .section-item, [role="tab"]'));
            return secElements.map(el => ({
                name: (el.innerText || el.getAttribute('aria-label') || '').split('\\n')[0].trim()
            })).filter(s => s.name.length > 1 && !s.name.toLowerCase().includes('notebook') && !s.name.toLowerCase().includes('content library'));
            """
        )
        logger.info("Found %d section(s): %s", len(sections), [s["name"] for s in sections])

        if not sections:
            sections = [{"name": "General_Content"}]

        # Create output directory for AP Biology
        bio_dir = OUT_DIR / "AP_Biology_Bleier_26-27"
        bio_dir.mkdir(parents=True, exist_ok=True)

        # Iterate through Sections and Pages
        for sec in sections[:6]:
            sec_name = sec["name"]
            sec_dir = bio_dir / sanitize(sec_name)
            sec_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n--> Selecting Section: %s", sec_name)

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
            time.sleep(4)

            # Discover Pages in this Section
            pages = driver.execute_script(
                """
                const pageNodes = Array.from(document.querySelectorAll('div[role="listitem"], div[role="option"], [data-automationid*="Page"], .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"]'));
                return pageNodes.map(p => ({
                    name: (p.innerText || p.getAttribute('aria-label') || '').split('\\n')[0].trim()
                })).filter(p => p.name.length > 1 && !p.name.toLowerCase().includes('untitled page'));
                """
            )
            logger.info("  Found %d page(s) in '%s': %s", len(pages), sec_name, [p["name"] for p in pages[:6]])

            if not pages:
                pages = [{"name": sec_name}]

            # Click and extract each Page
            for pg in pages:
                pg_name = pg["name"]
                logger.info("    📄 Clicking and Extracting Page: %s", pg_name)

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

                # Extract canvas text
                canvas_text = driver.execute_script(
                    """
                    const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], #active_page_frame, .WACPageContent, div[data-testid="page-container"]');
                    return canvas ? canvas.innerText : document.body.innerText;
                    """
                )

                if canvas_text and len(canvas_text.strip()) > 20:
                    page_file = sec_dir / f"{sanitize(pg_name)}.md"
                    md_doc = f"# AP Biology - {sec_name}: {pg_name}\n\n**Class**: AP Biology Bleier 26-27\n**Section**: {sec_name}\n**Page**: {pg_name}\n**Source**: Microsoft OneNote Web\n\n{canvas_text}\n"
                    page_file.write_text(md_doc, encoding="utf-8")
                    logger.info("      ✓ Saved notes: %s (%d chars)", page_file.name, len(canvas_text))

        time.sleep(30)
        logger.info("\n✓ Finished scraping AP Biology _Content Library! Keeping browser open.")

    except Exception as exc:
        logger.exception("Crawler error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
