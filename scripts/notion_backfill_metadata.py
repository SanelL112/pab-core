#!/usr/bin/env python3
"""Backfill ``Source`` and ``Last synced`` on pre-upgrade Notion Tracker rows.

Rows created before the task-hub schema upgrade have empty ``Source`` and
``Last synced`` properties, so ``get_calendar_tasks`` falls back to labelling
them ``Notion``/``Notion`` and loses course attribution.

Attribution is only ever inferred from evidence already stored on the page:

* the ``Source: <name>`` / ``Course: <name>`` lines that ``add_task_to_notion``
  writes into the page body, and
* the ``Link`` URL's host (Canvas vs Google Classroom).

A row with no such evidence is labelled ``Manual``, which is truthful: it means
"we cannot attribute this automatically".  Nothing is invented, and no existing
non-empty property is ever overwritten.

``Last synced`` is set to the page's own creation date, not today, so the field
keeps its meaning of "when this row last matched an upstream source".

Dry run by default; pass --apply to write.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrapers import notion_client as nc  # noqa: E402

logger = logging.getLogger("notion_backfill")

# Hosts we can attribute with certainty.  Anything else stays unattributed.
HOST_SOURCES = {
    "instructure.com": "Canvas",
    "classroom.google.com": "Google Classroom",
    "mail.google.com": "Gmail",
    "groupme.com": "GroupMe",
}

# Must match the Source select options defined in notion_client.TASK_HUB_PROPERTIES.
VALID_SOURCES = {"Canvas", "Google Classroom", "Gmail", "GroupMe", "Manual"}

FALLBACK_SOURCE = "Manual"


def _plain_text(rich: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich or [])


def _source_from_host(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    for suffix, source in HOST_SOURCES.items():
        if host == suffix or host.endswith("." + suffix):
            return source
    return None


def _read_page_body(page_id: str) -> list[str]:
    """Return the page's paragraph lines, or [] when they cannot be read."""
    try:
        response = nc._rate_limited_request(
            "GET",
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=nc._notion_headers(),
            params={"page_size": 20},
            timeout=15,
        )
        response.raise_for_status()
        blocks = nc._json_object(response).get("results", [])
    except Exception as exc:  # pragma: no cover - network path
        logger.debug("Could not read body of %s: %s", page_id, exc)
        return []

    lines: list[str] = []
    for block in blocks:
        if block.get("type") != "paragraph":
            continue
        text = _plain_text(block.get("paragraph", {}).get("rich_text", []))
        lines.extend(line.strip() for line in text.splitlines() if line.strip())
    return lines


def _labelled_value(lines: list[str], label: str) -> str | None:
    prefix = f"{label}:".lower()
    for line in lines:
        if line.lower().startswith(prefix):
            value = line[len(prefix):].strip()
            if value:
                return value
    return None


def _fetch_all_rows() -> list[dict]:
    url = f"https://api.notion.com/v1/databases/{nc.DATABASE_ID}/query"
    headers = nc._notion_headers()
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = nc._rate_limited_request("POST", url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        body = nc._json_object(response)
        rows.extend(body.get("results", []))
        if not body.get("has_more"):
            return rows
        cursor = body.get("next_cursor")
        if not cursor:
            return rows


def _created_date(page: dict) -> str | None:
    raw = page.get("created_time", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return None


def plan_row(page: dict, *, read_body: bool = True) -> dict | None:
    """Return the patch needed for one row, or None when it is already complete."""
    props = page.get("properties", {})
    title_parts = props.get("Task name", {}).get("title", [])
    title = _plain_text(title_parts) or "(untitled)"

    has_source = bool((props.get("Source", {}).get("select") or {}).get("name"))
    has_synced = bool((props.get("Last synced", {}).get("date") or {}).get("start"))
    if has_source and has_synced:
        return None

    patch: dict = {}
    evidence = "none"

    if not has_source:
        link = props.get("Link", {}).get("url")
        source = _source_from_host(link)
        if source:
            evidence = f"link host ({source})"
        elif read_body:
            lines = _read_page_body(page["id"])
            body_source = _labelled_value(lines, "Source")
            if body_source:
                normalized = body_source.strip().title()
                if normalized == "Classroom":
                    normalized = "Google Classroom"
                if normalized in VALID_SOURCES:
                    source = normalized
                    evidence = f"page body ({source})"
            if source is None:
                source = _source_from_host(_labelled_value(lines, "Open task"))
                if source:
                    evidence = f"page body link ({source})"
        if source is None:
            source = FALLBACK_SOURCE
        patch["Source"] = {"select": {"name": source}}

    if not has_synced:
        created = _created_date(page)
        if created:
            patch["Last synced"] = {"date": {"start": created}}

    if not patch:
        return None
    return {"id": page["id"], "title": title, "properties": patch, "evidence": evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N rows needing a patch")
    parser.add_argument("--no-body", action="store_true", help="skip reading page bodies (faster, less accurate)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not nc.NOTION_API_KEY:
        logger.error("NOTION_API_KEY is not configured.")
        return 1

    schema = nc.get_task_tracker_schema(force_refresh=True)
    missing = [name for name in ("Source", "Last synced") if name not in schema]
    if missing:
        logger.error("Tracker is missing %s; run notion_task_hub.py --apply first.", ", ".join(missing))
        return 1

    rows = _fetch_all_rows()
    logger.info("Scanned %d row(s).", len(rows))

    plans = []
    for page in rows:
        plan = plan_row(page, read_body=not args.no_body)
        if plan:
            plans.append(plan)
            if args.limit and len(plans) >= args.limit:
                break

    if not plans:
        logger.info("Nothing to backfill — every row already has Source and Last synced.")
        return 0

    from collections import Counter

    sources = Counter(
        (plan["properties"].get("Source", {}).get("select") or {}).get("name", "(unchanged)")
        for plan in plans
    )
    logger.info("%d row(s) need a patch. Source assignment: %s", len(plans), dict(sources))

    if not args.apply:
        for plan in plans[:15]:
            fields = ", ".join(sorted(plan["properties"]))
            logger.info("  [DRY RUN] %-45s <- %s (evidence: %s)", plan["title"][:45], fields, plan["evidence"])
        if len(plans) > 15:
            logger.info("  ... and %d more", len(plans) - 15)
        logger.info("Re-run with --apply to write these changes.")
        return 0

    updated = failed = 0
    for plan in plans:
        try:
            response = nc._rate_limited_request(
                "PATCH",
                f"https://api.notion.com/v1/pages/{plan['id']}",
                headers=nc._notion_headers(),
                json={"properties": plan["properties"]},
                timeout=15,
            )
            response.raise_for_status()
            updated += 1
        except Exception as exc:
            logger.error("Failed to patch '%s': %s", plan["title"][:45], exc)
            failed += 1

    logger.info("Backfill complete: %d updated, %d failed.", updated, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
