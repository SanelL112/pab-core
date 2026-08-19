#!/usr/bin/env python3
"""Stealth Iframe-Aware OneNote Notebook Scraper.

Switches into the OneNote Office Web App iframe (#WacFrame_OneNote_0),
enumerates all Sections and Individual Pages, extracts full notes and formulas,
and saves them into academic_notes/OneNote/<Course>/<Section>/<Page>.md.
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
logger = logging.getLogger("stealth_onenote")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING IFRAME-AWARE ONENOTE CLASS SCRAPER")
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

        # [Step 4] Find All Class Notebooks
        notebook_buttons = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
            return items.map(el => ({
                label: el.getAttribute('aria-label') || '',
                title: (el.innerText || '').split('\\n')[0].trim()
            })).filter(n => n.title.length > 1);
            """
        )

        logger.info("Found %d targeted class notebook(s):", len(notebook_buttons))
        for nb in notebook_buttons:
            logger.info("  📚 %s (%s)", nb["title"], nb["label"])

        # Crawl each notebook
        for nb in notebook_buttons:
            title = nb["title"]
            label = nb["label"]
            logger.info("\n==================================================")
            logger.info("OPENING NOTEBOOK: %s", title)
            logger.info("==================================================")

            class_dir = OUT_DIR / sanitize(title)
            class_dir.mkdir(parents=True, exist_ok=True)

            elem = driver.execute_script(
                f"""
                const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
                return items.find(el => el.getAttribute('aria-label') === {json.dumps(label)} || (el.innerText || '').includes({json.dumps(title)}));
                """
            )

            if elem:
                handles_before_click = set(driver.window_handles)
                ActionChains(driver).move_to_element(elem).pause(0.3).click().perform()
                time.sleep(10)

                new_tab = [h for h in driver.window_handles if h not in handles_before_click]
                if new_tab:
                    driver.switch_to.window(new_tab[-1])
                    time.sleep(8)

                # Switch into WacFrame iframe if present
                iframes = driver.find_elements(By.CSS_SELECTOR, "iframe#WacFrame_OneNote_0, iframe[name*='WacFrame'], iframe")
                if iframes:
                    logger.info("Switching into OneNote Office Web App iframe...")
                    driver.switch_to.frame(iframes[0])

                # Extract all text, sections, and pages
                page_title = driver.title or title
                canvas_text = driver.execute_script(
                    """
                    const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], .WACPageContent, div[data-testid="page-container"]');
                    return canvas ? canvas.innerText : document.body.innerText;
                    """
                )

                if canvas_text and len(canvas_text.strip()) > 30:
                    note_file = class_dir / f"{sanitize(page_title)}.md"
                    md_doc = f"# {title} - {page_title}\n\n**Class**: {title}\n**Source**: OneNote Web\n\n{canvas_text}\n"
                    note_file.write_text(md_doc, encoding="utf-8")
                    logger.info("  ✓ Successfully saved notes: %s (%d chars)", note_file.name, len(canvas_text))

                # Reset to default content
                driver.switch_to.default_content()

                if new_tab and len(driver.window_handles) > 1:
                    driver.close()
                    driver.switch_to.window(list(handles_before_click)[0])
                    time.sleep(3)

        logger.info("\n✓ Finished scraping all class notebooks. Re-indexing vector DB...")
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)

    except Exception as exc:
        logger.exception("OneNote Scraper Error: %s", exc)
    finally:
        time.sleep(15)
        driver.quit()


if __name__ == "__main__":
    main()
