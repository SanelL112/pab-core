#!/usr/bin/env python3
"""Inspect and complete the authentication in the Sign In popup window."""
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
logger = logging.getLogger("popup_auth_handler")

DISPLAY = os.environ.get("DISPLAY", ":2")


def main():
    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)

    logger.info("==================================================")
    logger.info("CONNECTING TO POPUP & COMPLETING VERIFICATION")
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
        time.sleep(6)

        # [Step 5] Click Sign-in inside Iframe
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            try:
                driver.switch_to.frame(frame)
                btn = driver.execute_script(
                    """
                    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'));
                    const target = buttons.find(b => (b.innerText || b.getAttribute('aria-label') || '').toLowerCase().includes('sign in'));
                    if (target) {
                        target.click();
                        return true;
                    }
                    return false;
                    """
                )
                if btn:
                    logger.info("Clicked Sign In button inside iframe!")
                    time.sleep(5)
                driver.switch_to.default_content()
            except Exception:
                driver.switch_to.default_content()

        time.sleep(5)

        # [Step 6] If a popup window opened, switch to it and complete account selection
        all_handles = driver.window_handles
        logger.info("Total open windows/tabs: %d", len(all_handles))
        for handle in all_handles:
            if handle not in hb and handle != notebook_window:
                driver.switch_to.window(handle)
                logger.info("--> Switched to Verification Popup: %s (%s)", driver.title, driver.current_url)

                # Check if account picker is on the popup
                account_tile = driver.execute_script(
                    """
                    const tiles = Array.from(document.querySelectorAll('div[data-test-id="account-tile"], .table-row, #tilesHolder div[role="button"], div[role="option"], .tile-container, div.identity, div[aria-label*="@"], div.table, #tilesHolder div'));
                    for (const t of tiles) {
                        const txt = (t.innerText || t.getAttribute('aria-label') || '').toLowerCase();
                        if (txt.includes('147416') || txt.includes('forsythk12') || (txt.length > 2 && !txt.includes('use another account'))) {
                            t.style.border = '4px solid lime';
                            t.click();
                            return t.innerText || 'Selected Account';
                        }
                    }
                    return null;
                    """
                )
                logger.info("Selected Account on Popup: %s", account_tile)
                time.sleep(5)

                # Click Yes on Stay Signed In if present
                driver.execute_script("const b = document.querySelector('#idSIButton9, input[type=\"submit\"][value*=\"Yes\"], button#acceptButton'); if (b) b.click();")
                time.sleep(5)

        # Return to notebook window
        driver.switch_to.window(notebook_window)
        logger.info("Returned to Notebook Window. Pausing 10s for full render...")
        time.sleep(10)

        # [Step 7] Click _Content Library
        logger.info("[Step 7] Locating and clicking '_Content Library'...")
        clicked = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[role="button"], span, div, a, [data-automationid*="SectionGroup"]'));
            const target = items.find(el => {
                const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                return txt === '_content library' || txt === 'content library' || txt.includes('_content library');
            });
            if (target) {
                target.scrollIntoView({block: 'center'});
                target.style.border = '5px solid lime';
                target.style.boxShadow = '0 0 25px lime';
                target.click();
                return target.innerText || '_Content Library';
            }
            return null;
            """
        )
        logger.info("Clicked _Content Library: %s", clicked)

        # Keep browser open on screen for review
        time.sleep(60)

    except Exception as exc:
        logger.exception("Error: %s", exc)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
