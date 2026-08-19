#!/usr/bin/env python3
"""Dedicated ClassLink -> Microsoft 365 / OneNote Launcher on VNC Display :2."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("m365_onenote_launcher")

DISPLAY = os.environ.get("DISPLAY", ":2")
ACADEMIC_DIR = _ROOT / "academic_notes" / "OneNote"
ACADEMIC_DIR.mkdir(parents=True, exist_ok=True)


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("STARTING DIRECT CLASSLINK -> MICROSOFT 365 LAUNCHER")
    logger.info("==================================================")

    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 30)

    try:
        # [Step 1] ClassLink Sign-in (Deep CSS + Shadow DOM)
        logger.info("Opening ClassLink landing page...")
        driver.get("https://launchpad.classlink.com/forsyth")
        time.sleep(3)

        username = get_setting("CLASSLINK_USERNAME")
        password = get_setting("CLASSLINK_PASSWORD")

        username_selector = (
            "input[name='username'], input[name='user'], input#username, input#user, "
            "input#login, input#loginId, input[name='login'], input[name='loginId'], "
            "input#userNameInput, input[name='UserName'], input[name='userName'], "
            "input[name='identifier'], input[type='email'], input[autocomplete='username'], "
            "input[placeholder*='Username']"
        )
        password_selector = (
            "input[name='password'], input#password, input[type='password'], "
            "input#passwordInput, input[autocomplete='current-password']"
        )
        submit_selector = "button[type='submit'], input[type='submit'], #submitButton, #loginButton"

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

        logger.info("Submitted ClassLink form. Waiting for ADFS redirect...")
        # Wait for ADFS to return to LaunchPad
        wait.until(lambda d: "fcss-adfs.forsyth.k12.ga.us" not in urlsplit(d.current_url).netloc)
        time.sleep(4)
        logger.info("[Step 2] Successfully on ClassLink LaunchPad: %s", driver.current_url)

        handles_before = set(driver.window_handles)

        # [Step 3] Wait for LaunchPad apps to render and click 'OneDrive' / 'Outlook 365'
        logger.info("[Step 3] Waiting for ClassLink app grid to load...")
        time.sleep(3)

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
                        const target = el.closest('a, button, [role="button"], .app-icon, .app-tile') || el;
                        return target;
                    }
                }
                return null;
                """
            )
        )

        logger.info("[Step 3 Result] Located M365 App Tile on screen!")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '5px solid #00ff00'; arguments[0].style.boxShadow = '0 0 25px lime';", tile)
        time.sleep(1)

        # Click using pointer actions
        ActionChains(driver).move_to_element(tile).pause(0.3).click().perform()
        logger.info("Clicked M365 tile! Waiting for Microsoft 365 federated window...")

        # [Step 4] Wait for FCS ADFS window to complete and switch to Microsoft 365
        wait.until(lambda d: len(d.window_handles) > len(handles_before))
        new_handles = [h for h in driver.window_handles if h not in handles_before]
        if new_handles:
            driver.switch_to.window(new_handles[-1])
            logger.info("Switched to active M365 window: %s (%s)", driver.title, driver.current_url)

        # Wait for Microsoft 365 portal to settle
        time.sleep(8)
        logger.info("[Step 4 Status] Current M365 URL: %s (%s)", driver.title, driver.current_url)

        # [Step 5] Navigate to OneNote Notebooks portal
        logger.info("[Step 5] Navigating to OneNote Notebooks portal...")
        driver.get("https://onenote.cloud.microsoft/en-us/")
        time.sleep(4)

        # [Step 5.1] Click the 'Sign in' button on OneNote portal
        logger.info("[Step 5.1] Locating and clicking 'Sign in' button on OneNote page...")
        clicked_signin = driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
            const target = buttons.find(b => {
                const txt = (b.innerText || b.getAttribute('aria-label') || b.id || '').toLowerCase();
                return txt.includes('sign in') || txt.includes('signin') || txt.includes('log in');
            });
            if (target) {
                target.scrollIntoView({block: 'center'});
                target.style.border = '5px solid #00ff00';
                target.style.boxShadow = '0 0 25px yellow';
                target.click();
                return target.innerText || 'Sign In Button';
            }
            return null;
            """
        )
        logger.info("Clicked Sign In button: %s", clicked_signin)

        # [Step 5.2] Wait until Microsoft Account Selector URL loads, then click the account tile
        logger.info("[Step 5.2] Waiting for Microsoft Account Selector page to load...")
        wait.until(lambda d: "login.microsoftonline.com" in d.current_url or "select_account" in d.current_url)
        time.sleep(3)

        # Wait for the account tile element to appear
        account_tile = wait.until(
            lambda _d: driver.execute_script(
                """
                const tiles = Array.from(document.querySelectorAll('div[data-test-id="account-tile"], .table-row, #tilesHolder div[role="button"], div[role="option"], .tile-container, div.identity, div[aria-label*="@"], div.table, #tilesHolder div'));
                for (const tile of tiles) {
                    const text = (tile.innerText || tile.getAttribute('aria-label') || '').toLowerCase();
                    if (text.includes('147416') || text.includes('forsythk12') || (text.length > 2 && !text.includes('use another account'))) {
                        const target = tile.closest('div[role="button"], .table-row, div[data-test-id="account-tile"]') || tile;
                        return target;
                    }
                }
                return null;
                """
            )
        )

        logger.info("[Step 5.2] Located student account tile on screen! Highlighting and clicking...")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '5px solid #00ff00'; arguments[0].style.boxShadow = '0 0 25px yellow';", account_tile)
        time.sleep(1)
        ActionChains(driver).move_to_element(account_tile).pause(0.3).click().perform()
        logger.info("[Step 5.2 Result] Successfully CLICKED student account tile!")

        # Wait for redirect or "Stay signed in?" prompt
        time.sleep(5)

        # Check if 'Stay signed in?' prompt appears and click Yes (#idSIButton9)
        driver.execute_script(
            """
            const btn = document.querySelector('#idSIButton9, input[type="submit"][value*="Yes"], button#acceptButton');
            if (btn) {
                btn.style.border = '4px solid lime';
                btn.click();
            }
            """
        )

        # Wait until redirected back to OneNote portal authenticated
        wait.until(lambda d: "login.microsoftonline.com" not in d.current_url)
        time.sleep(6)
        logger.info("[Step 5 Status] Authenticated OneNote URL: %s (%s)", driver.title, driver.current_url)

        # [Step 6] Discover all Class Notebooks
        logger.info("[Step 6] Discovering Class Notebooks...")
        driver.execute_script(
            """
            const tabs = Array.from(document.querySelectorAll('button, a, [role="tab"]'));
            const classTab = tabs.find(t => (t.innerText || '').toLowerCase().includes('class'));
            if (classTab) classTab.click();
            """
        )
        time.sleep(4)

        notebooks = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('a[href*="onenote"], a[href*="sharepoint"], [data-automationid="DetailsRowCell"] a, .notebook-item, .ms-List-cell a, .ms-DetailsRow a')).map(el => ({
                title: (el.innerText || el.getAttribute('aria-label') || el.title || '').trim(),
                url: el.href || ''
            })).filter(n => n.title.length > 2 && (n.url.includes('onenote') || n.url.includes('sharepoint') || n.url.includes('office')));
            """
        )

        logger.info("Discovered %d notebook(s):", len(notebooks))
        for nb in notebooks:
            logger.info("  - 📓 %s -> %s", nb["title"], nb["url"][:80])

        meta_file = ACADEMIC_DIR / "discovered_notebooks.json"
        meta_file.write_text(json.dumps(notebooks, indent=2), encoding="utf-8")
        logger.info("Saved notebook list to: %s", meta_file)

        time.sleep(20)
        logger.info("✓ OneNote Sign-In & Crawl Complete! Keeping browser open on screen.")

    except Exception as exc:
        logger.exception("SSO Execution failed: %s", exc)


if __name__ == "__main__":
    main()
