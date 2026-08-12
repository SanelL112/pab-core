#!/usr/bin/env python3
"""Compatibility entry point for the private, local-only study-guide builder.

The previous script independently loaded dotenv, sent private study material to
cloud providers, wrote into the checkout, and published a Telegram document.
Use ``run_builder`` instead: it keeps the guide in the private export root and
never stages, commits, pushes, or publishes it.
"""
from __future__ import annotations

from run_builder import main


if __name__ == "__main__":
    raise SystemExit(main())
