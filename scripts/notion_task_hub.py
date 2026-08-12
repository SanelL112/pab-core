#!/usr/bin/env python3
"""Safely preview or upgrade the Notion task tracker into a usable task hub.

Usage:
    venv/bin/python scripts/notion_task_hub.py cleanup
    venv/bin/python scripts/notion_task_hub.py cleanup --apply
    venv/bin/python scripts/notion_task_hub.py upgrade
    venv/bin/python scripts/notion_task_hub.py upgrade --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.notion_client import archive_stale_tasks, find_stale_tasks, upgrade_task_tracker_schema


RECOMMENDED_VIEWS = """\
Recommended Notion views to create after the upgrade:
  • Today — Status is not Done, Due date is today or earlier; sort Priority then Due date.
  • Next 7 days — Status is not Done, Due date is within the next week.
  • Inbox — Status is Not started; sort Priority then Last synced (newest first).
  • In progress — Status is In progress.
  • Completed — Status is Done; sort Due date or Last edited (newest first).
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("cleanup", "upgrade"))
    parser.add_argument("--apply", action="store_true", help="Perform the change; otherwise only preview it.")
    parser.add_argument("--overdue-days", type=int, default=7, help="Mark Not started dated tasks Done after this many overdue days.")
    parser.add_argument("--undated-age-days", type=int, default=60, help="Mark old undated Not started tasks Done after this many days.")
    args = parser.parse_args()

    if args.action == "cleanup":
        candidates = find_stale_tasks(
            max_age_days=max(0, args.undated_age_days),
            overdue_days=max(0, args.overdue_days),
        )
        heading = "Will mark Done" if args.apply else "Would mark Done"
        print(f"{heading}: {len(candidates)} task(s)")
        for task in candidates:
            print(f"- {task['title']} — {task['reason']}")
        if args.apply and candidates:
            completed = archive_stale_tasks(
                dry_run=False,
                max_age_days=max(0, args.undated_age_days),
                overdue_days=max(0, args.overdue_days),
            )
            print(f"Marked Done: {completed} task(s)")
        return 0

    changed = upgrade_task_tracker_schema(dry_run=not args.apply)
    action = "Added" if args.apply else "Would add"
    print(f"{action}: {', '.join(changed) if changed else 'no properties'}")
    print()
    print(RECOMMENDED_VIEWS.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
