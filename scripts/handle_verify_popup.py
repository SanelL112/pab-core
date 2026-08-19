#!/usr/bin/env python3
"""Inspect and click the OneNote/SharePoint 'Verify / Sign In' popup modal."""
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

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config import get_setting
from scrapers.canvas_scraper import CanvasBrowserClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("handle_verify_popup")

DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("INSPECTING & HANDLING VERIFY / SIGN IN POPUP")
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
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].style.border = '4px solid lime';", elem)
        time.sleep(2)

        hb = set(driver.window_handles)
        ActionChains(driver).move_to_element(elem).pause(0.3).click().perform()
        time.sleep(10)

        nh = [h for h in driver.window_handles if h not in hb]
        if nh:
            driver.switch_to.window(nh[-1])
            time.sleep(8)

        logger.info("Current Window: %s (%s)", driver.title, driver.current_url)

        # [Step 5] Search for Verification / Sign-In Dialog in Top Doc & Iframes
        logger.info("[Step 5] Scanning for Verification Prompt or Modal...")
        modal_info = driver.execute_script(
            """
            const modals = Array.from(document.querySelectorAll('div[role="dialog"], div[role="alertdialog"], .ms-Dialog, .ms-Modal, iframe, div.modal, [id*="dialog"], [id*="modal"]'));
            const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]'));
            
            const verifyButtons = buttons.filter(b => {
                const txt = (b.innerText || b.getAttribute('aria-label') || b.value || '').toLowerCase();
                return txt.includes('sign in') || txt.includes('verify') || txt.includes('continue') || txt.includes('re-enter') || txt.includes('log in');
            });
            
            return {
                body_text: document.body.innerText.slice(0, 500),
                buttons_found: verifyButtons.map(b => b.innerText || b.getAttribute('aria-label') || b.value)
            };
            """
        )
        logger.info("Modal Inspection Results:\n%s", json.dumps(modal_info, indent=2))

        # Check inside all iframes
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        logger.info("Found %d iframes in notebook window", len(iframes))
        for i, frame in enumerate(iframes):
            try:
                driver.switch_to.frame(frame)
                frame_text = driver.execute_script("return document.body.innerText;")
                logger.info("  Iframe %d Text (%d chars): %s", i, len(frame_text), frame_text[:200].replace('\n', ' '))
                
                # Check for sign in inside iframe
                btn = driver.execute_script(
                    """
                    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'));
                    const target = buttons.find(b => {
                        const txt = (b.innerText || b.getAttribute('aria-label') || b.value || '').toLowerCase();
                        return txt.includes('sign in') || txt.includes('verify') || txt.includes('continue');
                    });
                    if (target) {
                        target.style.border = '5px solid yellow';
                        target.click();
                        return target.innerText || 'Sign in inside iframe';
                    }
                    return null;
                    """
                )
                if btn:
                    logger.info("  👉 Clicked verify button inside iframe %d: %s", i, btn)
                    time.sleep(5)
                driver.switch_to.default_content()
            except Exception as fe:
                logger.warning("  Could not inspect iframe %d: %s", i, fe)
                driver.switch_to.default_content()

        # Click top-level verify button if present
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]'));
            const target = buttons.find(b => {
                const txt = (b.innerText || b.getAttribute('aria-label') || b.value || '').toLowerCase();
                return txt.includes('sign in') || txt.includes('verify') || txt.includes('continue') || txt.includes('log in');
            });
            if (target) {
                target.style.border = '5px solid yellow';
                target.style.boxShadow = '0 0 30px yellow';
                target.click();
            }
            """
        )
        time.sleep(6)

        # Check if an account picker or password prompt opened
        logger.info("After clicking verify, current window count: %d", len(driver.window_handles))
        if len(driver.window_handles) > len(nh) + len(hb):
            driver.switch_to.window(driver.window_handles[-1])
            logger.info("Switched to popup window: %s (%s)", driver.title, driver.current_url)

        # Log what Microsoft is asking on screen
        current_screen_text = driver.execute_script("return document.body.innerText;")
        logger.info("\n==================================================")
        logger.info("WHAT MICROSOFT IS ASKING ON SCREEN:")
        logger.info("==================================================")
        logger.info("%s", current_screen_text[:1000])
        logger.info("==================================================")

        # Keep browser open on screen for 60 seconds
        time.sleep(60)

    except Exception as exc:
        logger.exception("Error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
