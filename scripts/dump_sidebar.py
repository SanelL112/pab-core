#!/usr/bin/env python3
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

os.environ["DISPLAY"] = ":2"
client = CanvasBrowserClient(headless=False, use_daemon=False)
client._start_browser()
driver = client.driver
wait = WebDriverWait(driver, 30)

try:
    # 1. ClassLink
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

    # 2. Click M365
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

    # 3. OneNote
    driver.get("https://onenote.cloud.microsoft/en-us/")
    time.sleep(4)
    driver.execute_script(
        """
        const buttons = Array.from(document.querySelectorAll('a, button, [role="button"], [data-bi-name*="signin"], [aria-label*="Sign in"], #hero-banner-sign-in'));
        const target = buttons.find(b => (b.innerText || b.getAttribute('aria-label') || '').toLowerCase().includes('sign in'));
        if (target) target.click();
        """
    )

    # 4. Account Picker
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

    # Dump full sidebar HTML
    sidebar_html = driver.execute_script(
        """
        const sidebar = document.querySelector('nav, aside, [role="navigation"], #leftPane, .left-pane, [data-automationid*="LeftNav"], #app-navigation');
        return sidebar ? sidebar.outerHTML : document.body.innerHTML;
        """
    )
    dom_file = Path("academic_notes/OneNote/sidebar_dom.html")
    dom_file.parent.mkdir(parents=True, exist_ok=True)
    dom_file.write_text(sidebar_html, encoding="utf-8")
    print(f"[OK] Dumped sidebar DOM ({len(sidebar_html)} bytes) to {dom_file}")

    # Keep browser open for 60 seconds on VNC screen
    time.sleep(60)

finally:
    driver.quit()
