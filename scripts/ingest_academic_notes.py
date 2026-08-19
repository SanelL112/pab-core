#!/usr/bin/env python3
"""Ingest and Vectorize Academic Notes (PDFs, Markdown, Word docs, OneNote exports).

Scans the `academic_notes/` folder, extracts text from any newly added PDFs or documents,
and triggers incremental vector indexing into the local nomic-embed-text database.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("academic_ingest")

ACADEMIC_DIR = _ROOT / "academic_notes"


def process_pdfs():
    """Extract text from any PDF files using pdftotext / tesseract."""
    ACADEMIC_DIR.mkdir(parents=True, exist_ok=True)
    pdf_files = list(ACADEMIC_DIR.glob("**/*.pdf"))
    logger.info("Found %d PDF(s) in academic_notes/", len(pdf_files))

    for pdf in pdf_files:
        txt_target = pdf.with_suffix(".txt")
        if txt_target.exists() and txt_target.stat().st_mtime >= pdf.stat().st_mtime:
            continue  # Up to date

        logger.info("Extracting text from: %s", pdf.name)
        try:
            res = subprocess.run(["pdftotext", str(pdf), str(txt_target)], check=False)
            if res.returncode == 0 and txt_target.exists() and txt_target.stat().st_size > 50:
                logger.info("Extracted %d bytes via pdftotext", txt_target.stat().st_size)
            else:
                # Try OCR if pdftotext produced empty text (scanned homework)
                logger.info("Running OCR on: %s", pdf.name)
                ocr_target = pdf.with_suffix("")
                subprocess.run(
                    ["tesseract", str(pdf), str(ocr_target), "--oem", "1", "-l", "eng"],
                    check=False,
                )
        except Exception as exc:
            logger.warning("Failed to extract %s: %s", pdf.name, exc)


async def main():
    print("=" * 60)
    print("  ACADEMIC NOTES INGESTION & VECTORIZATION PIPELINE")
    print("=" * 60)
    print(f"  Watched Directory: {ACADEMIC_DIR}\n")

    process_pdfs()

    from scrapers.embedding_indexer import build_index
    print("\n  Building Vector Embeddings (nomic-embed-text)...")
    success = await build_index()
    if success:
        print("\n✓ Ingestion & Vector Indexing Complete!")
    else:
        print("\n⚠️ Vector indexing completed with warnings.")


if __name__ == "__main__":
    asyncio.run(main())
