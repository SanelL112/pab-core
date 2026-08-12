"""Build one private study guide without publishing, staging, or pushing it."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import config
from scrapers.mega_study_builder import generate_mega_guide


def _safe_filename(topic: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", topic).strip("_").lower()
    if not cleaned:
        raise ValueError("topic must include letters or numbers")
    return cleaned[:80]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", nargs="?", default="Comprehensive SAT Exam Prep Guide")
    parser.add_argument("--docx", action="store_true", help="also convert with locally installed pandoc")
    args = parser.parse_args(argv)
    topic = " ".join(args.topic.split())
    if not 2 <= len(topic) <= 120:
        parser.error("topic must be 2–120 characters")

    config.initialize_runtime()
    filename = _safe_filename(topic) + "_study_guide"
    print(f"Building private study guide for: {topic}")
    result = generate_mega_guide(topic)
    if not result or len(result.strip()) < 80:
        print("Study-guide generation did not return publishable content.", file=sys.stderr)
        return 1

    markdown_path = config.PRIVATE_STUDY_GUIDES_DIR / f"{filename}.md"
    _atomic_write(markdown_path, result)
    print(f"Created {markdown_path}")

    if args.docx:
        document_path = config.PRIVATE_STUDY_GUIDES_DIR / f"{filename}.docx"
        completed = subprocess.run(
            ["pandoc", str(markdown_path), "-o", str(document_path)],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode:
            print("Pandoc conversion failed; the Markdown guide is still available.", file=sys.stderr)
            return 1
        document_path.chmod(0o600)
        print(f"Created {document_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
