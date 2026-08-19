#!/usr/bin/env python3
"""Deep Class Notebook Scraper for _Content Library and Student Sections (Lathiya, Sanel).

Iterates through:
- AP Biology Bleier 26-27
- AP Calculus AB 2026-2027
- AP Stat 25-26

Expands _Content Library & 'Lathiya, Sanel', clicks all Unit/Homework sections,
scrapes all individual lesson pages, and vectorizes into the database!
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
logger = logging.getLogger("class_deep_crawler")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING DEEP CLASS NOTEBOOK EXTRACTION")
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

        # Discovered class notebooks
        classes = ["AP Biology Bleier 26-27", "AP Calculus AB 2026-2027", "AP Stat 25-26"]

        for class_name in classes:
            logger.info("\n==================================================")
            logger.info("PROCESSING CLASS NOTEBOOK: %s", class_name)
            logger.info("==================================================")

            class_dir = OUT_DIR / sanitize(class_name)
            class_dir.mkdir(parents=True, exist_ok=True)

            elem = wait.until(
                lambda _d: driver.execute_script(
                    f"""
                    const items = Array.from(document.querySelectorAll('div[aria-label^="Open "][aria-label*="notebook"], div[role="button"][aria-label*="notebook"]'));
                    return items.find(el => (el.getAttribute('aria-label') || '').includes({json.dumps(class_name)}) || (el.innerText || '').includes({json.dumps(class_name)}));
                    """
                )
            )

            if not elem:
                logger.warning("Could not find notebook tile for %s", class_name)
                continue

            hb = set(driver.window_handles)
            ActionChains(driver).move_to_element(elem).pause(0.3).click().perform()
            time.sleep(10)

            nh = [h for h in driver.window_handles if h not in hb]
            if not nh:
                continue
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

            # Switch into WacFrame iframe
            wac_frames = driver.find_elements(By.CSS_SELECTOR, "iframe#WacFrame_OneNote_0, iframe[name*='WacFrame'], iframe")
            if wac_frames:
                driver.switch_to.frame(wac_frames[0])
                logger.info("Switched into WacFrame for %s", class_name)

            # Expand Section Groups: _Content Library and Lathiya, Sanel
            logger.info("Expanding Section Groups (_Content Library, Sanel Lathiya)...")
            driver.execute_script(
                """
                const groups = Array.from(document.querySelectorAll('div[role="treeitem"], div[role="button"], span, div, .sectionGroupItem'));
                groups.forEach(el => {
                    const txt = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
                    if (txt.includes('content library') || txt.includes('lathiya') || txt.includes('sanel')) {
                        el.style.border = '3px solid lime';
                        el.click();
                    }
                });
                """
            )
            time.sleep(5)

            # Enumerate all Sections inside the notebook
            sections = driver.execute_script(
                """
                const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[data-automationid*="Section"], .sectionItem, .section-item, [role="tab"], div.sectionName'));
                return items.map(s => ({
                    name: (s.innerText || s.getAttribute('aria-label') || '').split('\\n')[0].trim()
                })).filter(s => s.name.length > 1 && !['home', 'insert', 'draw', 'view', 'help', 'file'].includes(s.name.toLowerCase()));
                """
            )
            logger.info("Found %d sections: %s", len(sections), [s["name"] for s in sections])

            if not sections:
                sections = [{"name": "Course_Material"}]

            # Crawl each section and its pages
            for sec in sections:
                sec_name = sec["name"]
                sec_dir = class_dir / sanitize(sec_name)
                sec_dir.mkdir(parents=True, exist_ok=True)
                logger.info("  --> Selecting Section: %s", sec_name)

                driver.execute_script(
                    f"""
                    const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[data-automationid*="Section"], .sectionItem, .section-item, [role="tab"], div.sectionName'));
                    const match = items.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(sec_name)}));
                    if (match) {{
                        match.scrollIntoView({{block: 'center'}});
                        match.style.border = '2px solid cyan';
                        match.click();
                    }}
                    """
                )
                time.sleep(4)

                # Discover Pages
                pages = driver.execute_script(
                    """
                    const pageNodes = Array.from(document.querySelectorAll('div[role="listitem"], div[role="option"], [data-automationid*="Page"], .pageItem, .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"], div.pageTitle'));
                    return pageNodes.map(p => ({
                        name: (p.innerText || p.getAttribute('aria-label') || '').split('\\n')[0].trim()
                    })).filter(p => p.name.length > 1 && !p.name.toLowerCase().includes('untitled page'));
                    """
                )

                logger.info("    Found %d pages in '%s': %s", len(pages), sec_name, [p["name"] for p in pages[:5]])

                if not pages:
                    pages = [{"name": sec_name}]

                for pg in pages:
                    pg_name = pg["name"]
                    logger.info("      📄 Extracting Page: %s", pg_name)

                    driver.execute_script(
                        f"""
                        const pageNodes = Array.from(document.querySelectorAll('div[role="listitem"], div[role="option"], [data-automationid*="Page"], .pageItem, .page-item, a[data-automationid*="Page"], div[data-automationid*="PageList"] [role="row"], div.pageTitle'));
                        const match = pageNodes.find(el => (el.innerText || el.getAttribute('aria-label') || '').includes({json.dumps(pg_name)}));
                        if (match) {{
                            match.scrollIntoView({{block: 'center'}});
                            match.style.border = '3px solid lime';
                            match.click();
                        }}
                        """
                    )
                    time.sleep(4)

                    canvas_text = driver.execute_script(
                        """
                        const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], #active_page_frame, .WACPageContent, div[data-testid="page-container"]');
                        return canvas ? canvas.innerText : document.body.innerText;
                        """
                    )

                    if canvas_text and len(canvas_text.strip()) > 20:
                        page_file = sec_dir / f"{sanitize(pg_name)}.md"
                        md_doc = f"# {class_name} - {sec_name}: {pg_name}\n\n**Class**: {class_name}\n**Section**: {sec_name}\n**Page**: {pg_name}\n**Source**: Microsoft OneNote Web\n\n{canvas_text}\n"
                        page_file.write_text(md_doc, encoding="utf-8")
                        logger.info("        ✓ Saved: %s (%d chars)", page_file.name, len(canvas_text))

            # Close notebook tab and return to notebook list
            driver.switch_to.default_content()
            if len(driver.window_handles) > 1:
                driver.close()
                driver.switch_to.window(list(hb)[0])
                time.sleep(3)

        logger.info("\n✓ ALL CLASS NOTEBOOKS EXTRACTED! Re-indexing vector database...")
        subprocess.run([sys.executable, str(_ROOT / "scripts" / "ingest_academic_notes.py")], check=False)

        time.sleep(30)

    except Exception as exc:
        logger.exception("Crawler error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
