from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from scrapers.assignment_calendar import Assignment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _load_dashboard_agent():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location("test_dashboard_agent_module", SCRIPTS_DIR / "dashboard_agent.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


def test_dashboard_keeps_unfinished_overdue_tasks_visible():
    dashboard_agent = _load_dashboard_agent()
    overdue_due = (date.today() - timedelta(days=5)).isoformat()
    assignment = Assignment(
        source="notion",
        external_id="overdue-task",
        title="Finish application",
        course="Notion",
        due_date=overdue_due,
        status="Not started",
    )

    with patch("scrapers.assignment_calendar.collect_assignments", return_value=[assignment]):
        rows = dashboard_agent.read_tasks()

    assert len(rows) == 1
    assert rows[0]["title"] == "Finish application"
    assert rows[0]["overdue"] is True
    assert rows[0]["due"] == f"{overdue_due} · overdue"
