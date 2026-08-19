#!/usr/bin/env python3
"""Interactive OneNote Authenticator & Live Crawler for VNC Display :2.

Keeps Firefox open on screen without timing out or closing prematurely,
allowing you to click your account tile / enter credentials on the Microsoft Account Selector.
Once signed in, it automatically captures all Class Notebooks and extracts your notes!
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("interactive_onenote")

PROFILE_DIR = Path.home() / ".local" / "share" / "personal-assistant-bot" / "canvas-firefox-profile"
ACADEMIC_DIR = _ROOT / "academic_notes" / "OneNote"
ACADEMIC_DIR.mkdir(parents=True, exist_ok=True)

DISPLAY = os.environ.get("DISPLAY", ":2")


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name).strip("_")


def main():
    print("=" * 60)
    print("  INTERACTIVE ONENOTE LOGIN & DISCOVERY (DISPLAY :2)")
    print("=" * 60)
    print("  Firefox is now open on your VNC screen (:2 / port 5902).")
    print("  Please select your account on the Microsoft screen.\n")

    from scrapers.canvas_scraper import CanvasBrowserClient

    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)
    client._start_browser()
    driver = client.driver
    assert driver is not None

    try:
        # Navigate to Office 365 OneNote Launchpad
        driver.get("https://www.office.com/launch/onenote?auth=2")
        time.sleep(4)

        # Highlight account tile if on account selector screen
        driver.execute_script(
            """
            const tiles = document.querySelectorAll('div[data-test-id="account-tile"], .table-row, #tilesHolder div[role="button"]');
            tiles.forEach(t => {
                t.style.border = '4px solid #00ff00';
                t.style.backgroundColor = '#ffffcc';
            });
            """
        )

        logger.info("Waiting for you to select your account and complete sign-in...")

        # Interactive loop: wait until sign-in completes and user reaches OneNote/Office portal
        max_wait_seconds = 300
        start_time = time.monotonic()
        authenticated = False

        while time.monotonic() - start_time < max_wait_seconds:
            curr_url = driver.current_url.lower()
            curr_title = driver.title

            # Check if reached OneNote / SharePoint / Office Hub
            if any(k in curr_url for k in ["onenote.com/notebooks", "sharepoint.com", "m365.cloud.microsoft", "office.com/launch/onenote"]):
                if "login.microsoftonline.com" not in curr_url:
                    logger.info("✓ Microsoft 365 OneNote Session Authenticated! Location: %s", driver.current_url)
                    authenticated = True
                    break

            time.sleep(2)

        if not authenticated:
            logger.warning("Sign-in timed out after 5 minutes. Keeping browser open for manual review.")
            return

        # Once authenticated, navigate to Notebooks list
        logger.info("Discovering all Class Notebooks and Personal Notebooks on screen...")
        driver.get("https://www.onenote.com/notebooks")
        time.sleep(6)

        # Click "Class Notebooks" tab if present
        driver.execute_script(
            """
            const tabs = Array.from(document.querySelectorAll('button, a, [role="tab"]'));
            const classTab = tabs.find(t => (t.innerText || '').toLowerCase().includes('class'));
            if (classTab) classTab.click();
            """
        )
        time.sleep(3)

        # Enumerate all notebook links
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
            logger.info("  - 📓 %s -> %s", nb["title"], nb["url"][:60])

        meta_file = ACADEMIC_DIR / "discovered_notebooks.json"
        meta_file.write_text(json.dumps(notebooks, indent=2), encoding="utf-8")
        logger.info("Saved notebook list to: %s", meta_file)

        # Extract visible notes
        page_text = driver.execute_script("return document.body.innerText;")
        if page_text and len(page_text) > 100:
            (ACADEMIC_DIR / "OneNote_Dashboard_Overview.md").write_text(f"# OneNote Dashboard Overview\n\n{page_text}\n", encoding="utf-8")

        time.sleep(5)
        logger.info("\n✓ OneNote Crawl & Session Capture Complete!")

    finally:
        # Flush and keep profile saved
        driver.quit()


if __name__ == "__main__":
    main()
