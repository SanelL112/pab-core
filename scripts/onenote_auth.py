#!/usr/bin/env python3
"""One-time OneNote authorization + smoke test (device-code flow).

Usage
-----
1. Register a PUBLIC client app in Azure (Entra):
     Azure Portal -> App registrations -> New registration
       - Supported account types: "Personal Microsoft accounts" (or both)
       - Authentication -> Advanced -> Allow public client flows: YES
       - API permissions -> Microsoft Graph -> Delegated -> Notes.Read
         (add offline_access; Grant admin consent if it's an org account)
     Copy the Application (client) ID.

2. Put it in the environment (or pab-dev/.env):
     export ONENOTE_CLIENT_ID=<application-client-id>
   For a single-tenant org account also set:
     export ONENOTE_TOKEN_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
     export ONENOTE_DEVICE_CODE_ENDPOINT=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/devicecode

3. Run:
     python scripts/onenote_auth.py           # interactive login, saves token
     python scripts/onenote_auth.py --list     # after login: list notebooks/sections/pages
     python scripts/onenote_auth.py --extract   # fetch recent pages and extract tasks

The refresh token is stored at CONFIG_DIR/onenote_token.json (0600). After the
one-time login, all downstream runs refresh silently — no more prompts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scrapers.onenote_scraper import OneNoteAuthError, OneNoteClient  # noqa: E402


def _login(client: OneNoteClient) -> None:
    def prompt(uri: str, code: str, message: str) -> None:
        print("\n" + "=" * 60)
        print("  OPEN THIS URL AND ENTER THE CODE TO AUTHORIZE ONENOTE")
        print("=" * 60)
        print(f"  URL : {uri}")
        print(f"  CODE: {code}")
        if message:
            print(f"\n  ({message})")
        print("=" * 60)
        print("  Waiting for sign-in... (Ctrl-C to abort)\n")

    client.device_code_login(on_prompt=prompt)
    print("✓ Authorized. Refresh token saved to:", client.token_path)


def _list(client: OneNoteClient) -> None:
    pages = client.list_pages(top=25, max_pages=2)
    print(f"Found {len(pages)} OneNote page(s):\n")
    for p in pages:
        print(f"  [{p.get('lastModifiedDateTime', '?')[:10]}] {p.get('title', 'Untitled')}")
        print(f"      id={p.get('id', '')}")


def _extract(client: OneNoteClient) -> None:
    from scrapers.canvas_page_extractor import reset_extraction_budget
    from scrapers.onenote_page_extractor import extract_tasks_from_page

    reset_extraction_budget()
    pages = client.list_pages(top=25, max_pages=2)
    print(f"Extracting tasks from {len(pages)} page(s)...\n")
    all_tasks: list[dict] = []
    for p in pages:
        page_id = p.get("id", "")
        if not page_id:
            continue
        try:
            html = client.get_page_html(page_id, include_ink=True)
        except OneNoteAuthError as exc:
            print(f"  auth error on {p.get('title')}: {exc}")
            continue
        tasks = extract_tasks_from_page(p, html)
        for t in tasks:
            print(f"  [{t['task_type']:10}] {t['due_date']}  {t['title'][:60]}  ({p.get('title', '')[:25]})")
        all_tasks.extend(tasks)
    print(f"\nTotal OneNote tasks extracted: {len(all_tasks)}")
    print(json.dumps(all_tasks, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(description="OneNote device-code auth + smoke test")
    ap.add_argument("--list", action="store_true", help="list pages (requires prior login)")
    ap.add_argument("--extract", action="store_true", help="fetch + extract tasks (requires prior login)")
    args = ap.parse_args()

    client = OneNoteClient()
    try:
        if args.list:
            _list(client)
        elif args.extract:
            _extract(client)
        else:
            _login(client)
    except OneNoteAuthError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
