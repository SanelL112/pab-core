"""Retired historical-export entry point.

Bulk re-exporting Google, Canvas, and Gmail history mixed old personal data
with current operational state, relied on a legacy Canvas API token, and wrote
unbounded archives.  The leased nightly processor is the supported bounded
ingestion path.  This module intentionally performs no network or file I/O.
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def run_all_exports() -> None:
    """Retained for callers; historical bulk export is intentionally disabled."""
    logger.warning("Historical bulk export is disabled; use the nightly ingestion queue instead.")


if __name__ == "__main__":
    run_all_exports()
