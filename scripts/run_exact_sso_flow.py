#!/usr/bin/env python3
"""Dedicated ClassLink -> Microsoft 365 / OneNote SSO Automation on Display :2."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import get_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("m365_sso")

PROFILE_DIR = Path.home() / ".local" / "share" / "personal-assistant-bot" / "canvas-firefox-profile"
DISPLAY = os.environ.get("DISPLAY", ":2")
ACADEMIC_DIR = _ROOT / "academic_notes" / "OneNote"
ACADEMIC_DIR.mkdir(parents=True, exist_ok=True)


def main():
    username = get_setting("CLASSLINK_USERNAME")
    password = get_setting("CLASSLINK_PASSWORD")

    os.environ["DISPLAY"] = DISPLAY
    options = Options()
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR))
    options.add_argument("--width=1366")
    options.add_argument("--height=768")

    logger.info("==================================================")
    logger.info("STARTING DIRECT CLASSLINK -> MICROSOFT 365 LAUNCHER")
    logger.info("==================================================")

    driver = webdriver.Firefox(options=options)
    wait = WebDriverWait(driver, 30)

    try:
        # [Step 1] Navigate to ClassLink Forsyth
        logger.info("[Step 1] Navigating to ClassLink portal...")
        driver.get("https://launchpad.classlink.com/forsyth")
        time.sleep(3)

        # If login form is present, fill and submit
        try:
            user_input = driver.find_element(By.CSS_SELECTOR, "input#username, input[name='username'], input[type='text']")
            pass_input = driver.find_element(By.CSS_SELECTOR, "input#password, input[name='password'], input[type='password']")
            submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], #submitButton, input[type='submit']")

            logger.info("Entering ClassLink credentials...")
            user_input.clear()
            user_input.send_keys(username)
            pass_input.clear()
            pass_input.send_keys(password)
            submit_btn.click()
            logger.info("Submitted ClassLink login form. Waiting for LaunchPad...")
        except Exception:
            logger.info("Already at login or form bypassed. Checking current location...")

        # [Step 2] Wait until LaunchPad home loads
        wait.until(lambda d: "myapps.classlink.com" in d.current_url or "home" in d.current_url)
        time.sleep(4)
        logger.info("[Step 2] Successfully on ClassLink LaunchPad: %s", driver.current_url)

        handles_before = set(driver.window_handles)

        # [Step 3] Click ONLY 'OneDrive' or 'Outlook 365 - Students' (DO NOT CLICK CANVAS)
        logger.info("[Step 3] Searching for Microsoft 365 App Tile (OneDrive / Outlook 365)...")
        tile_name = driver.execute_script(
            """
            const elements = Array.from(document.querySelectorAll('a, button, [role="button"], .app-icon, .app-tile, [aria-label], [title]'));
            const target = elements.find(el => {
                const label = [
                    el.innerText, el.getAttribute('aria-label'),
                    el.getAttribute('title'), el.getAttribute('alt')
                ].filter(Boolean).join(' ').toLowerCase();
                // Match Microsoft 365 tiles only
                return (label.includes('onedrive') || label.includes('outlook 365') || label.includes('office 365')) && !label.includes('canvas');
            });
            if (target) {
                target.scrollIntoView({block: 'center'});
                target.style.border = '5px solid #00ff00';
                target.style.boxShadow = '0 0 20px lime';
                const clickable = target.closest('a, button, [role="button"]') || target;
                clickable.click();
                return label || 'M365 Tile';
            }
            return null;
            """
        )
        logger.info("[Step 3 Result] Clicked Tile: %s", tile_name)

        # [Step 4] Wait for FCS ADFS window to complete and switch to Microsoft 365
        logger.info("[Step 4] Waiting for Microsoft 365 SAML/ADFS federated login...")
        time.sleep(8)

        new_handles = [h for h in driver.window_handles if h not in handles_before]
        if new_handles:
            driver.switch_to.window(new_handles[-1])
            logger.info("Switched to active M365 window: %s (%s)", driver.title, driver.current_url)

        # Wait for Microsoft 365 portal to settle
        time.sleep(6)
        logger.info("[Step 4 Status] Current M365 URL: %s", driver.current_url)

        # [Step 5] Navigate to OneNote within the newly minted Microsoft 365 session
        logger.info("[Step 5] Navigating to OneNote Notebooks portal...")
        driver.get("https://www.onenote.com/notebooks")
        time.sleep(8)
        logger.info("[Step 5 Status] Final Location: %s (%s)", driver.title, driver.current_url)

        # [Step 6] Discover all Class Notebooks
        logger.info("[Step 6] Discovering Class Notebooks...")
        driver.execute_script(
            """
            const tabs = Array.from(document.querySelectorAll('button, a, [role="tab"]'));
            const classTab = tabs.find(t => (t.innerText || '').toLowerCase().includes('class'));
            if (classTab) classTab.click();
            """
        )
        time.sleep(3)

        notebooks = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('a[href*="onenote"], a[href*="sharepoint"], [data-automationid="DetailsRowCell"] a, .notebook-item, .ms-List-cell a')).map(el => ({
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

        time.sleep(10)
        logger.info("✓ Microsoft 365 & OneNote SSO Complete! Keeping browser open on screen.")

    except Exception as exc:
        logger.exception("SSO Execution failed: %s", exc)


if __name__ == "__main__":
    main()
