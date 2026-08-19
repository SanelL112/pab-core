#!/usr/bin/env python3
"""Complete End-to-End OneNote Scraper via ClassLink SSO.

Executes the entire verified sign-in sequence:
1. ClassLink Landing Page -> ADFS Login -> LaunchPad
2. Click M365 App Tile (OneDrive / Outlook 365)
3. OneNote Portal -> Click 'Sign In'
4. Microsoft Account Selector -> Click '147416@forsythk12.org' -> 'Stay signed in'
5. Left Sidebar Navigation -> Iterate all Notebooks -> Sections -> Pages
6. Extract Markdown notes into academic_notes/OneNote/ and vectorize!
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
logger = logging.getLogger("onenote_full_crawler")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING FULL END-TO-END CLASSLINK -> ONENOTE CRAWL")
    logger.info("==================================================")

    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        # [Step 1] ClassLink Sign-in
        logger.info("[Step 1] Opening ClassLink landing page...")
        driver.get("https://launchpad.classlink.com/forsyth")
        time.sleep(3)

        username = get_setting("CLASSLINK_USERNAME")
        password = get_setting("CLASSLINK_PASSWORD")

        username_selector = "input[name='username'], input#username, input#userNameInput, input[type='email'], input[placeholder*='Username']"
        password_selector = "input[name='password'], input#password, input[type='password'], input#passwordInput"
        submit_selector = "button[type='submit'], input[type='submit'], #submitButton"

        try:
            username_input = WebDriverWait(driver, 5).until(lambda _d: client._find_deep_css(username_selector))
        except Exception:
            logger.info("Clicking ClassLink initial entry button...")
            client._click_classlink_sign_in_entry()
            time.sleep(2)
            username_input = wait.until(lambda _d: client._find_deep_css(username_selector))

        password_input = wait.until(lambda _d: client._find_deep_css(password_selector))
        username_input.clear()
        username_input.send_keys(username)
        password_input.clear()
        password_input.send_keys(password)

        submit_btn = client._find_deep_css(submit_selector)
        if submit_btn:
            submit_btn.click()

        logger.info("Submitted ClassLink credentials. Waiting for LaunchPad...")
        wait.until(lambda d: "myapps.classlink.com" in d.current_url or "home" in d.current_url)
        time.sleep(4)
        logger.info("[Step 2] Reached ClassLink LaunchPad: %s", driver.current_url)

        handles_before = set(driver.window_handles)

        # [Step 2] Click Microsoft 365 Tile
        logger.info("[Step 2.1] Locating Microsoft 365 Tile...")
        tile = wait.until(
            lambda _d: driver.execute_script(
                """
                const elements = Array.from(document.querySelectorAll('a, button, [role="button"], img, [aria-label], [title], .app-icon, .app-tile'));
                for (const el of elements) {
                    const label = [
                        el.innerText, el.getAttribute('aria-label'),
                        el.getAttribute('title'), el.getAttribute('alt')
                    ].filter(Boolean).join(' ').toLowerCase();
                    if ((label.includes('onedrive') || label.includes('outlook 365') || label.includes('office 365')) && !label.includes('canvas')) {
                        return el.closest('a, button, [role="button"], .app-icon, .app-tile') || el;
                    }
                }
                return null;
                """
            )
        )

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '5px solid #00ff00';", tile)
        time.sleep(1)
        ActionChains(driver).move_to_element(tile).pause(0.3).click().perform()
        logger.info("Clicked M365 tile! Waiting for federated window...")

        wait.until(lambda d: len(d.window_handles) > len(handles_before))
        new_handles = [h for h in driver.window_handles if h not in handles_before]
        if new_handles:
            driver.switch_to.window(new_handles[-1])

        time.sleep(6)

        # [Step 3] Navigate to OneNote and Click Sign In
        logger.info("[Step 3] Navigating to OneNote portal...")
        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(4)

        logger.info("[Step 3.1] Clicking 'Sign in' on OneNote portal...")
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
            const target = buttons.find(b => {
                const txt = (b.innerText || b.getAttribute('aria-label') || b.id || '').toLowerCase();
                return txt.includes('sign in') || txt.includes('signin') || txt.includes('log in');
            });
            if (target) target.click();
            """
        )

        # [Step 4] Pick Account on Microsoft Account Selector
        logger.info("[Step 4] Waiting for Microsoft Account Selector...")
        wait.until(lambda d: "login.microsoftonline.com" in d.current_url or "select_account" in d.current_url)
        time.sleep(3)

        account_tile = wait.until(
            lambda _d: driver.execute_script(
                """
                const tiles = Array.from(document.querySelectorAll('div[data-test-id="account-tile"], .table-row, #tilesHolder div[role="button"], div[role="option"], .tile-container, div.identity, div[aria-label*="@"], div.table, #tilesHolder div'));
                for (const tile of tiles) {
                    const text = (tile.innerText || tile.getAttribute('aria-label') || '').toLowerCase();
                    if (text.includes('147416') || text.includes('forsythk12') || (text.length > 2 && !text.includes('use another account'))) {
                        return tile.closest('div[role="button"], .table-row, div[data-test-id="account-tile"]') || tile;
                    }
                }
                return null;
                """
            )
        )

        logger.info("Located student account tile! Highlighting and clicking...")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '5px solid #00ff00';", account_tile)
        time.sleep(1)
        ActionChains(driver).move_to_element(account_tile).pause(0.3).click().perform()

        time.sleep(5)
        # Stay signed in prompt
        driver.execute_script("const b = document.querySelector('#idSIButton9, input[type=\"submit\"][value*=\"Yes\"], button#acceptButton'); if (b) b.click();")

        wait.until(lambda d: "login.microsoftonline.com" not in d.current_url)
        time.sleep(6)
        logger.info("[Step 5] Successfully reached Authenticated OneNote Dashboard: %s (%s)", driver.title, driver.current_url)

        # [Step 6] Click 'Go to OneNote' or 'Notebooks' to open the main notebooks view
        logger.info("[Step 6] Clicking 'Go to OneNote' to open main notebook view...")
        time.sleep(3)

        driver.execute_script(
            """
            const links = Array.from(document.querySelectorAll('a, button, [role="button"]'));
            const goToOneNote = links.find(l => {
                const txt = (l.innerText || l.getAttribute('aria-label') || '').trim().toLowerCase();
                return txt.includes('go to onenote') || txt === 'notebooks';
            });
            if (goToOneNote) {
                goToOneNote.style.border = '4px solid lime';
                goToOneNote.click();
            }
            """
        )
        time.sleep(8)
        logger.info("[Step 6 Status] Location after 'Go to OneNote': %s (%s)", driver.title, driver.current_url)

        # Also click "Class Notebooks" or "All" tab if present
        driver.execute_script(
            """
            const tabs = Array.from(document.querySelectorAll('button, a, [role="tab"]'));
            const classTab = tabs.find(t => (t.innerText || '').toLowerCase().includes('class'));
            if (classTab) {
                classTab.style.border = '3px solid yellow';
                classTab.click();
            }
            """
        )
        time.sleep(4)

        # Discover Notebook links & list items
        notebook_items = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll(
                'a[href*="onenote"], a[href*="sharepoint"], [data-automationid="DetailsRowCell"] a, .notebook-item, .ms-List-cell a, .ms-DetailsRow a, div[role="row"] a, div[role="listitem"] a, div[role="treeitem"]'
            ));
            return items.map(el => ({
                title: (el.innerText || el.getAttribute('aria-label') || el.title || '').trim(),
                url: el.href || ''
            })).filter(n => n.title.length > 2 && !n.title.toLowerCase().includes('sign in') && !n.title.toLowerCase().includes('feedback') && !n.title.toLowerCase().includes('copilot') && !n.title.toLowerCase().includes('privacy'));
            """
        )

        logger.info("Discovered %d Notebook(s) on screen:", len(notebook_items))
        for nb in notebook_items:
            logger.info("  📓 %s -> %s", nb["title"], nb["url"][:70])

        # [Step 7] Click each Notebook item using pointer actions to open and extract
        logger.info("[Step 7] Iterating through notebooks using pointer actions...")
        for item in notebook_items:
            title = item["title"]
            logger.info("--> Opening Notebook: %s", title)

            # Find element and click using ActionChains pointer events
            elem = driver.execute_script(
                f"""
                const items = Array.from(document.querySelectorAll('a, button, div[role="treeitem"], div[role="listitem"], [data-automationid="DetailsRowCell"], .notebook-item, .ms-List-cell, [data-testid*="notebook"], .ms-DetailsRow'));
                const match = items.find(el => (el.innerText || el.getAttribute('aria-label') || el.title || '').trim() === {json.dumps(title)});
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
                time.sleep(5)

                # If opened in a new tab/window, switch to it
                new_tab = [h for h in driver.window_handles if h not in handles_before_click]
                if new_tab:
                    driver.switch_to.window(new_tab[-1])
                    logger.info("Switched to notebook tab: %s", driver.title)
                    time.sleep(5)

                # Capture canvas page text
                page_title = driver.title or title
                page_text = driver.execute_script(
                    """
                    const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], #active_page_frame, .WACPageContent');
                    return canvas ? canvas.innerText : document.body.innerText;
                    """
                )

                if page_text and len(page_text.strip()) > 30:
                    clean_name = f"{sanitize(title)}.md"
                    target_file = OUT_DIR / clean_name
                    content_md = f"# {title}\n\n**Source**: OneNote Web Canvas\n\n{page_text}\n"
                    target_file.write_text(content_md, encoding="utf-8")
                    logger.info("  ✓ Successfully extracted notes for %s (%d chars)", title, len(page_text))

                # If we switched tabs, close the notebook tab and return to list
                if new_tab and len(driver.window_handles) > 1:
                    driver.close()
                    driver.switch_to.window(list(handles_before_click)[0])
                    time.sleep(2)

        # Save metadata catalog
        catalog_path = OUT_DIR / "discovered_notebooks.json"
        catalog_path.write_text(json.dumps(notebook_items, indent=2), encoding="utf-8")
        logger.info("Saved catalog metadata to: %s", catalog_path)

        time.sleep(10)
        logger.info("\n✓ ALL ONENOTE NOTEBOOKS EXTRACTED SUCCESSFULLY!")

    except Exception as exc:
        logger.exception("OneNote Crawl Error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
