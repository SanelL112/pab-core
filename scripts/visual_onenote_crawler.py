#!/usr/bin/env python3
"""Visual OneNote Crawler on VNC Display :2.

Runs a live, visible browser automation session in TigerVNC so you can watch
every step (ClassLink navigation, tile click, notebook discovery, and page extraction) in real time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("visual_onenote_crawler")

PROFILE_DIR = Path.home() / ".local" / "share" / "personal-assistant-bot" / "canvas-firefox-profile"
ACADEMIC_DIR = _ROOT / "academic_notes" / "OneNote"
ACADEMIC_DIR.mkdir(parents=True, exist_ok=True)

DISPLAY = os.environ.get("DISPLAY", ":2")


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name).strip("_")


def main():
    print("=" * 60)
    print(f"  VISUAL ONENOTE CRAWLER (RUNNING ON DISPLAY {DISPLAY})")
    print("=" * 60)
    print("  Watch the automation steps live in your VNC viewer!\n")

    from scrapers.canvas_scraper import CanvasBrowserClient

    os.environ["DISPLAY"] = DISPLAY
    client = CanvasBrowserClient(headless=False, use_daemon=False)
    logger.info("Starting Firefox on Display %s...", DISPLAY)
    client._start_browser()
    driver = client.driver
    assert driver is not None
    wait = WebDriverWait(driver, 25)

    try:
        # Step 1: Open ClassLink LaunchPad
        logger.info("[Step 1] Navigating to ClassLink LaunchPad...")
        driver.get("https://myapps.classlink.com/home")
        time.sleep(4)

        # Step 2: Locate and highlight M365 tile
        logger.info("[Step 2] Finding Microsoft 365 / OneDrive tile...")
        tile = driver.execute_script(
            """
            const tiles = Array.from(document.querySelectorAll('a, button, [role="button"], img, [aria-label], [title], .app-icon, .app-tile'));
            const match = tiles.find(el => {
                const txt = (el.innerText || el.getAttribute('aria-label') || el.title || '').toLowerCase();
                return txt.includes('onedrive') || txt.includes('outlook 365') || txt.includes('office 365');
            });
            if (match) {
                const target = match.closest('a, button, [role="button"], .app-icon, .app-tile') || match;
                target.style.border = '4px solid red';
                target.style.boxShadow = '0 0 15px yellow';
                return target;
            }
            return null;
            """
        )
        if tile:
            logger.info("Found M365 tile! Clicking...")
            handles_before = set(driver.window_handles)
            ActionChains(driver).move_to_element(tile).pause(0.5).click().perform()
            time.sleep(4)
            new_handles = [h for h in driver.window_handles if h not in handles_before]
            if new_handles:
                driver.switch_to.window(new_handles[-1])
        else:
            logger.info("Opening OneNote portal directly...")

        # Step 3: Navigate directly to School M365 OneNote Hub
        logger.info("[Step 3] Navigating to Microsoft 365 OneNote Hub (School Portal)...")
        driver.get("https://m365.cloud.microsoft/launch/onenote")
        time.sleep(8)

        if "login" in driver.current_url.lower() or "signin" in driver.current_url.lower():
            logger.info("Redirected to school login; navigating via Office.com student launcher...")
            driver.get("https://www.office.com/launch/onenote?auth=2")
            time.sleep(8)

        # Step 4: Check Class Notebooks and Recent Notebooks
        logger.info("[Step 4] Discovering Notebooks on screen...")
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
            return Array.from(document.querySelectorAll('a[href*="onenote"], a[href*="sharepoint"], a[data-automationid="DetailsRowCell"], .notebook-item, .ms-List-cell a')).map(el => ({
                title: (el.innerText || el.getAttribute('aria-label') || el.title || '').trim(),
                url: el.href || ''
            })).filter(n => n.title.length > 2 && (n.url.includes('onenote') || n.url.includes('sharepoint') || n.url.includes('office')));
            """
        )

        logger.info("Discovered %d notebook link(s) on screen:", len(notebooks))
        for nb in notebooks:
            logger.info("  - 📓 %s -> %s", nb["title"], nb["url"][:60])

        # Step 5: Save discovered notebooks to metadata
        meta_file = ACADEMIC_DIR / "discovered_notebooks.json"
        meta_file.write_text(json.dumps(notebooks, indent=2), encoding="utf-8")
        logger.info("Saved notebook discovery metadata to %s", meta_file)

        # Step 6: Extract page notes if inside an open notebook
        page_title = driver.title
        body_text = driver.execute_script(
            """
            const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main');
            return canvas ? canvas.innerText : document.body.innerText;
            """
        )
        if body_text and len(body_text) > 50:
            doc = f"# {page_title}\n\n{body_text}\n"
            (ACADEMIC_DIR / f"{sanitize(page_title)}.md").write_text(doc, encoding="utf-8")
            logger.info("Saved notebook page text for: %s", page_title)

        time.sleep(4)
        logger.info("\n✓ Visual OneNote Crawl Complete! Check your VNC screen.")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
