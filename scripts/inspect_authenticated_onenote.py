#!/usr/bin/env python3
"""Inspect authenticated OneNote DOM to capture all notebooks, sections, and pages."""
from __future__ import annotations

import json
import logging
import os
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
logger = logging.getLogger("inspect_onenote")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)
    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        logger.info("Opening ClassLink landing page...")
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

        wait.until(lambda d: "myapps.classlink.com" in d.current_url or "home" in d.current_url)
        time.sleep(4)
        handles_before = set(driver.window_handles)

        # Locate and click M365 tile
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

        wait.until(lambda d: len(d.window_handles) > len(handles_before))
        new_handles = [h for h in driver.window_handles if h not in handles_before]
        if new_handles:
            driver.switch_to.window(new_handles[-1])

        time.sleep(6)

        # Open OneNote
        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(4)

        # Click Sign In on OneNote
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

        wait.until(lambda d: "login.microsoftonline.com" in d.current_url or "select_account" in d.current_url)
        time.sleep(3)

        # Pick account
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

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '5px solid #00ff00';", account_tile)
        time.sleep(1)
        ActionChains(driver).move_to_element(account_tile).pause(0.3).click().perform()

        time.sleep(5)
        # Stay signed in click
        driver.execute_script("const b = document.querySelector('#idSIButton9, input[type=\"submit\"][value*=\"Yes\"], button#acceptButton'); if (b) b.click();")

        wait.until(lambda d: "login.microsoftonline.com" not in d.current_url)
        time.sleep(6)
        logger.info("Successfully on authenticated OneNote portal: %s (%s)", driver.title, driver.current_url)

        # Discover all interactive items in the left panel and main container
        items = driver.execute_script(
            """
            const allElements = Array.from(document.querySelectorAll('a, button, div[role="treeitem"], div[role="listitem"], [data-automationid], [data-testid], .ms-List-cell, [role="tab"], .notebook-item'));
            return allElements.map(el => {
                const text = (el.innerText || el.getAttribute('aria-label') || el.title || '').trim();
                const href = el.href || '';
                const role = el.getAttribute('role') || el.tagName.toLowerCase();
                return { text, href, role };
            }).filter(item => item.text.length > 1 && item.text.length < 100);
            """
        )

        logger.info("Discovered %d DOM items on screen:", len(items))
        for it in items:
            if any(term in it["text"].lower() for term in ["bio", "calc", "comp", "lang", "notebook", "physics", "class", "recent", "my"]):
                logger.info("  📌 [%s] %s -> %s", it["role"], it["text"], it["href"][:60])

        out_path = OUT_DIR / "dom_inspection.json"
        out_path.write_text(json.dumps(items, indent=2), encoding="utf-8")
        logger.info("Saved DOM inventory to: %s", out_path)

        time.sleep(20)

    except Exception as exc:
        logger.exception("Inspection error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
