"""Lossless nightly processing for queued study documents.

Each queue entry is leased before processing and acknowledged only after its
text has been durably appended to the private export.  A crash or transient
download failure leaves the item available for a later retry.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import time
import uuid

from bot.storage import AtomicJSONStore
from scrapers.batch_results import BatchResult, BatchStatus

import logging


logger = logging.getLogger(__name__)
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000
MAX_PDF_PAGES = 100
LEASE_SECONDS = 30 * 60
MAX_RETRY_ATTEMPTS = 5


def download_drive_file(file_id: str, output_path: str) -> bool:
    """Resolve the Google client only when a Drive item is actually claimed.

    Keeping this narrow adapter at the queue boundary makes the processor
    independently testable and prevents an optional Google dependency from
    becoming an import-time requirement for every nightly source.
    """
    from scrapers.google_scraper import download_drive_file as downloader

    return downloader(file_id, output_path)


def _queue_store() -> AtomicJSONStore[list]:
    from config import NIGHTLY_QUEUE_FILE

    return AtomicJSONStore(NIGHTLY_QUEUE_FILE, list)


def _dead_letter_store() -> AtomicJSONStore[list]:
    from config import NIGHTLY_DEAD_LETTER_FILE

    return AtomicJSONStore(NIGHTLY_DEAD_LETTER_FILE, list)


def _item_id(item: dict) -> str:
    # Do not include our own generated ``id`` here.  Queue normalization runs
    # both while claiming and while acknowledging; hashing a previously hashed
    # id produced a different key and left successful items leased forever.
    value = "\x1f".join(
        str(item.get(name) or "")
        for name in ("source", "file_id", "url", "title")
    )
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]


def _normalise_queue(queue: object) -> list[dict]:
    if not isinstance(queue, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for raw in queue:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["id"] = _item_id(item)
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        result.append(item)
    return result


def _claim_items(limit: int = 20) -> tuple[str, list[dict]]:
    lease_id = uuid.uuid4().hex
    now = time.time()
    claimed: list[dict] = []

    def mutate(queue: list) -> list:
        nonlocal claimed
        normalized = _normalise_queue(queue)
        for item in normalized:
            lease_until = float(item.get("lease_until", 0) or 0)
            if len(claimed) >= limit or (item.get("lease_id") and lease_until > now):
                continue
            item["lease_id"] = lease_id
            item["lease_until"] = now + LEASE_SECONDS
            item["updated_at"] = now
            claimed.append(dict(item))
        return normalized

    _queue_store().update(mutate)
    return lease_id, claimed


def _finish_items(lease_id: str, outcomes: dict[str, tuple[bool, str]]) -> None:
    """Ack successes and release failures owned by this worker only."""
    now = time.time()
    dead_letters: list[dict] = []

    def mutate(queue: list) -> list:
        remaining: list[dict] = []
        for item in _normalise_queue(queue):
            outcome = outcomes.get(item["id"])
            if item.get("lease_id") != lease_id or outcome is None:
                remaining.append(item)
                continue
            ok, detail = outcome
            if ok:
                continue
            item.pop("lease_id", None)
            item.pop("lease_until", None)
            item["attempt_count"] = min(int(item.get("attempt_count", 0) or 0) + 1, 1000)
            item["last_error"] = detail[:300]
            item["updated_at"] = now
            if item["attempt_count"] >= MAX_RETRY_ATTEMPTS:
                # Preserve metadata for review, but stop retrying a permanently
                # inaccessible document on every unattended nightly run.
                item["retryable"] = False
                item["dead_lettered_at"] = now
                dead_letters.append(item)
            else:
                remaining.append(item)
        return remaining

    _queue_store().update(mutate)
    if dead_letters:
        def append_dead_letters(existing: list) -> list:
            existing = existing if isinstance(existing, list) else []
            known = {str(item.get("id")) for item in existing if isinstance(item, dict)}
            for item in dead_letters:
                if str(item.get("id")) not in known:
                    existing.append(item)
                    known.add(str(item.get("id")))
            return existing

        _dead_letter_store().update(append_dead_letters)


def _append_export(title: str, text: str) -> None:
    from config import PDF_EXPORTS_FILE

    path = Path(PDF_EXPORTS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError("refusing to append export through a symlink")
    lock_path = path.with_name(f".{path.name}.lock")
    payload = (
        f"\n\n=== EXPORTED DOCUMENT: {title[:200]} ===\n"
        f"Captured: {datetime.now(timezone.utc).isoformat()}\n\n{text[:MAX_EXTRACTED_CHARS]}\n"
    ).encode("utf-8", "replace")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _extract_text(path: Path, suffix: str, title: str) -> str:
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        pages = reader.pages[:MAX_PDF_PAGES]
        text = "\n".join((page.extract_text() or "") for page in pages)
        if len(text.strip()) <= 50:
            # OCR is deliberately bounded; enormous scanned PDFs belong in a
            # dedicated offline ingestion job rather than the Telegram worker.
            import pytesseract
            from pdf2image import convert_from_path

            images = convert_from_path(path, first_page=1, last_page=min(len(reader.pages), 20))
            try:
                text = "\n".join(pytesseract.image_to_string(image) for image in images)
            finally:
                for image in images:
                    image.close()
        return text[:MAX_EXTRACTED_CHARS]
    if suffix == ".docx":
        from docx import Document

        return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)[:MAX_EXTRACTED_CHARS]
    return path.read_text(encoding="utf-8", errors="replace")[:MAX_EXTRACTED_CHARS]


def _process_item(item: dict) -> tuple[bool, str]:
    title = str(item.get("title") or "Untitled document")
    file_id = str(item.get("file_id") or "")
    source = str(item.get("source") or "google_drive")
    filename = str(item.get("filename") or title)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt"}:
        suffix = ".pdf"
    if not file_id:
        return False, "queue item has no file ID"

    fd, temporary = tempfile.mkstemp(prefix="pab-nightly-", suffix=suffix)
    os.close(fd)
    path = Path(temporary)
    try:
        if source == "canvas":
            from scrapers.canvas_scraper import download_canvas_file

            downloaded = download_canvas_file(file_id, str(path))
        else:
            downloaded = download_drive_file(file_id, str(path))
        if not downloaded:
            return False, "download failed"
        if not path.is_file() or path.stat().st_size <= 0:
            return False, "downloaded file is empty"
        if path.stat().st_size > MAX_FILE_BYTES:
            return False, f"download exceeds {MAX_FILE_BYTES // (1024 * 1024)}MB limit"
        text = _extract_text(path, suffix, title)
        if len(text.strip()) <= 50:
            return False, "document contained insufficient extractable text"
        _append_export(title, text)
        return True, f"exported {len(text)} characters"
    except Exception as exc:
        logger.warning("Nightly item %s failed: %s", item.get("id"), type(exc).__name__)
        return False, type(exc).__name__
    finally:
        try:
            path.unlink()
        except OSError:
            pass


async def run_nightly_job(bot=None, chat_id: int | None = None) -> BatchResult:
    """Process currently claimable queue entries without losing failed work."""
    lease_id, items = _claim_items()
    if not items:
        return BatchResult(BatchStatus.OK, detail="no queued documents")
    logger.info("Nightly processor claimed %d document(s)", len(items))
    raw = await asyncio.gather(*(asyncio.to_thread(_process_item, item) for item in items))
    outcomes = {item["id"]: outcome for item, outcome in zip(items, raw)}
    _finish_items(lease_id, outcomes)
    succeeded = sum(1 for ok, _detail in raw if ok)
    failed = len(raw) - succeeded
    if failed:
        dead_lettered = sum(
            1 for item, (ok, _detail) in zip(items, raw)
            if not ok and int(item.get("attempt_count", 0) or 0) + 1 >= MAX_RETRY_ATTEMPTS
        )
        retained = failed - dead_lettered
        detail = f"exported {succeeded}; retained {retained} for retry"
        if dead_lettered:
            detail += f"; quarantined {dead_lettered} after {MAX_RETRY_ATTEMPTS} failed attempts"
        return BatchResult(BatchStatus.ERROR, detail=detail)
    return BatchResult(BatchStatus.OK, detail=f"exported {succeeded} document(s)")


if __name__ == "__main__":
    result = asyncio.run(run_nightly_job())
    raise SystemExit(0 if result.ok else 1)
