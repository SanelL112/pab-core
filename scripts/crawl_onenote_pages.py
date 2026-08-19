#!/usr/bin/env python3
"""Visual Deep Crawler for Microsoft OneNote on VNC Display :2.

Visually navigates notebooks, expands sections and pages, highlights elements
with glowing boxes so you can watch live on VNC, extracts all notes, and indexes them!
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

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("visual_onenote_scraper")

PROFILE_DIR = Path.home() / ".local" / "share" / "personal-assistant-bot" / "canvas-firefox-profile"
DISPLAY = os.environ.get("DISPLAY", ":2")
OUT_DIR = _ROOT / "academic_notes" / "OneNote"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-_.]", "_", name.strip()).strip("_") or "Untitled"


def main():
    os.environ["DISPLAY"] = DISPLAY
    options = Options()
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR))
    options.add_argument("--width=1366")
    options.add_argument("--height=768")

    logger.info("==================================================")
    logger.info("STARTING LIVE VISUAL ONENOTE CRAWLER ON DISPLAY :2")
    logger.info("==================================================")

    driver = webdriver.Firefox(options=options)
    wait = WebDriverWait(driver, 30)

    try:
        logger.info("[Step 1] Opening authenticated OneNote portal...")
        driver.get("https://onenote.cloud.microsoft/copilotnotebooks")
        time.sleep(6)

        # Ensure Left Navigation Sidebar is Expanded
        logger.info("[Step 2] Expanding Left Navigation Pane...")
        driver.execute_script(
            """
            const toggles = Array.from(document.querySelectorAll('button[aria-label*="Navigation"], button[aria-label*="Notebook"], #NavigationToggle, [data-automationid*="NavToggle"], button[title*="Navigation"]'));
            if (toggles.length > 0) {
                toggles[0].style.border = '4px solid yellow';
                toggles[0].click();
            }
            """
        )
        time.sleep(3)

        # Find all Notebook / Section items in the Left Tree
        logger.info("[Step 3] Discovering Notebooks and Sections on the left...")
        notebook_elements = driver.execute_script(
            """
            const items = Array.from(document.querySelectorAll(
                'div[role="treeitem"], div[role="listitem"], [data-automationid="DetailsRowCell"], .notebook-item, .ms-List-cell, [data-testid*="notebook"], button[role="treeitem"], a[role="treeitem"]'
            ));
            return items.map((el, idx) => {
                const title = (el.innerText || el.getAttribute('aria-label') || el.title || '').trim();
                return {
                    index: idx,
                    title: title,
                    is_clickable: Boolean(el.offsetParent)
                };
            }).filter(n => n.title.length > 2 && !n.title.toLowerCase().includes('sign in') && !n.title.toLowerCase().includes('feedback'));
            """
        )

        logger.info("Found %d interactive navigation item(s) in left panel:", len(notebook_elements))
        for nb in notebook_elements:
            logger.info("  📂 %s", nb["title"])

        # Click through sections/notebooks to extract notes
        logger.info("[Step 4] Visually extracting active pages and sections...")
        for item in notebook_elements[:10]:  # Crawl first batch of items
            try:
                title = item["title"]
                logger.info("--> Focusing item: %s", title)

                # Highlight element with glowing green box on screen
                driver.execute_script(
                    f"""
                    const items = Array.from(document.querySelectorAll('div[role="treeitem"], div[role="listitem"], [data-automationid="DetailsRowCell"], .notebook-item, .ms-List-cell, [data-testid*="notebook"], button[role="treeitem"], a[role="treeitem"]'));
                    const match = items.find(el => (el.innerText || el.getAttribute('aria-label') || el.title || '').trim() === {json.dumps(title)});
                    if (match) {{
                        match.scrollIntoView({{block: 'center'}});
                        match.style.border = '4px solid lime';
                        match.style.boxShadow = '0 0 15px #00ff00';
                        match.click();
                    }}
                    """
                )
                time.sleep(3)

                # Extract page content from canvas
                page_title = driver.title or title
                content = driver.execute_script(
                    """
                    const canvas = document.querySelector('#active_page, .OneNoteCanvas, #PageContent, main, [role="main"], #active_page_frame, .WACPageContent');
                    return canvas ? canvas.innerText : document.body.innerText;
                    """
                )

                if content and len(content.strip()) > 30:
                    clean_filename = f"{sanitize(title)}.md"
                    target_file = OUT_DIR / clean_filename
                    doc_md = f"# {title}\n\n**Source**: OneNote Web Canvas\n\n{content}\n"
                    target_file.write_text(doc_md, encoding="utf-8")
                    logger.info("  ✓ Saved note: %s (%d chars)", clean_filename, len(content))

            except Exception as item_err:
                logger.warning("Could not crawl item %s: %s", item.get("title"), item_err)

        time.sleep(5)
        logger.info("==================================================")
        logger.info("✓ Visual Crawl Complete! Files saved to academic_notes/OneNote/")
        logger.info("==================================================")

    except Exception as exc:
        logger.exception("Scraper encountered error: %s", exc)
    finally:
        # Keep open for a moment so user sees completion
        time.sleep(10)
        driver.quit()


if __name__ == "__main__":
    main()
