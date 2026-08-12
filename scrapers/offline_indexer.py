"""Incrementally turn private historical exports into a validated local index."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from config import ARCHIVE_DIR as CONFIG_ARCHIVE_DIR, MEGA_INDEX_FILE
from scrapers.batch_results import BatchResult, BatchStatus, validate_generated_text


logger = logging.getLogger(__name__)

ARCHIVE_DIR = os.fspath(CONFIG_ARCHIVE_DIR)
OUTPUT_FILE = os.fspath(MEGA_INDEX_FILE)
PROGRESS_FILE = os.path.join(ARCHIVE_DIR, ".delta_index_progress.json")
LOCK_FILE = os.path.join(ARCHIVE_DIR, ".offline_indexer.lock")
CHUNK_SIZE = 8_000
PROGRESS_VERSION = 2


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE):
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    for start in range(0, len(text), chunk_size):
        yield text[start : start + chunk_size]


async def process_chunk(
    chunk: str,
    chunk_index: int,
    source_name: str,
    max_retries: int = 2,
) -> BatchResult:
    prompt = (
        "You are a highly meticulous academic data curator. Read the following chunk of raw data extracted from the student's learning platforms.\n"
        "Provide an exhaustive index of every file, document, announcement, and assignment actually present. Do not invent items.\n"
        "For each item include its exact title, contained information, study-guide usefulness, and what it reveals about the current learning topic.\n\n"
        f"SOURCE: {source_name}\n"
        f"DATA:\n{chunk}"
    )

    from llm_router import call_local_rpc

    attempts = max(1, min(int(max_retries), 3))
    last_result = BatchResult(BatchStatus.UNAVAILABLE, detail="local inference unavailable")
    for attempt in range(attempts):
        try:
            raw_result = await asyncio.to_thread(
                call_local_rpc,
                prompt=prompt,
                max_tokens=2048,
                temperature=0.1,
                timeout=120,
                allow_cloud=False,
            )
            last_result = validate_generated_text(raw_result, min_chars=80)
            if last_result.ok:
                return last_result
            # Explicit outages/refusals are deterministic and must not be
            # retried into the index as if they were content.
            if last_result.status in {BatchStatus.UNAVAILABLE, BatchStatus.REFUSED}:
                logger.warning(
                    "Offline indexing rejected chunk %s output (%s): %s",
                    chunk_index,
                    last_result.status.value,
                    last_result.detail,
                )
                return last_result
        except Exception as exc:
            last_result = BatchResult(BatchStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
            logger.warning(
                "Local inference error for chunk %s (attempt %s/%s): %s",
                chunk_index,
                attempt + 1,
                attempts,
                exc,
            )
        if attempt + 1 < attempts:
            await asyncio.sleep(5)
    return last_result


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _atomic_json(path: str, payload: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _load_progress(text: str) -> int:
    """Return a verified acknowledged prefix length, never a blind chunk count."""
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as stream:
            progress = json.load(stream)
        if progress.get("version") != PROGRESS_VERSION:
            return 0
        acknowledged = int(progress.get("acknowledged_chars", 0))
        if acknowledged < 0 or acknowledged > len(text):
            return 0
        if progress.get("prefix_sha256") != _sha256(text[:acknowledged]):
            return 0
        return acknowledged
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _save_progress(text: str, acknowledged_chars: int) -> None:
    _atomic_json(
        PROGRESS_FILE,
        {
            "version": PROGRESS_VERSION,
            "acknowledged_chars": acknowledged_chars,
            "prefix_sha256": _sha256(text[:acknowledged_chars]),
        },
    )


def _load_published_chunk_ids() -> set[str]:
    published: set[str] = set()
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("<!-- batch-chunk:") and line.rstrip().endswith(" -->"):
                    published.add(line[len("<!-- batch-chunk:") : -4].strip())
    except FileNotFoundError:
        pass
    return published


def _append_validated_chunk(chunk_id: str, heading: str, text: str) -> None:
    Path(OUTPUT_FILE).parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = f"\n\n<!-- batch-chunk:{chunk_id} -->\n{heading}\n\n{text}\n"
    fd = os.open(OUTPUT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        # fdopen owns the descriptor after construction.
        raise


def _atomic_write_text(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _consume_snapshot(delta_file: str, snapshot: str) -> bool:
    """Remove only bytes we processed, retaining exports appended mid-run."""
    try:
        with open(delta_file, "r", encoding="utf-8", errors="replace") as stream:
            current = stream.read()
    except OSError as exc:
        logger.error("Could not re-read delta before acknowledgement: %s", exc)
        return False
    if not current.startswith(snapshot):
        logger.warning("Delta changed non-append-only during indexing; preserving it unacknowledged")
        return False
    _atomic_write_text(delta_file, current[len(snapshot) :])
    _save_progress("", 0)
    return True


async def run_indexing() -> BatchResult:
    logger.info("Starting historical delta indexing")
    os.makedirs(ARCHIVE_DIR, mode=0o700, exist_ok=True)
    lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        delta_file = os.path.join(ARCHIVE_DIR, "delta_export.txt")
        if not os.path.exists(delta_file):
            return BatchResult(BatchStatus.OK, detail="no delta file")
        try:
            with open(delta_file, "r", encoding="utf-8", errors="replace") as stream:
                snapshot = stream.read()
        except OSError as exc:
            return BatchResult(BatchStatus.ERROR, detail=f"failed to read delta: {exc}")
        if not snapshot.strip():
            return BatchResult(BatchStatus.OK, detail="delta is empty")

        acknowledged = _load_progress(snapshot)
        published_ids = _load_published_chunk_ids()
        remaining_text = snapshot[acknowledged:]
        chunks = list(chunk_text(remaining_text))
        if not chunks:
            return BatchResult(BatchStatus.OK, detail="delta already acknowledged")

        for offset, chunk in enumerate(chunks):
            start = acknowledged + offset * CHUNK_SIZE
            end = start + len(chunk)
            chunk_id = _sha256(f"delta_export\0{chunk}")
            part_number = (start // CHUNK_SIZE) + 1
            logger.info("Processing delta chunk %s (%s/%s remaining)", chunk_id[:12], offset + 1, len(chunks))

            # If publication completed but the process died before progress
            # fsync, the stable marker makes replay an acknowledgement only.
            if chunk_id not in published_ids:
                result = await process_chunk(chunk, part_number, "delta_export")
                if not result.ok:
                    logger.warning(
                        "Delta preserved at character %s after %s result: %s",
                        start,
                        result.status.value,
                        result.detail,
                    )
                    return result
                _append_validated_chunk(
                    chunk_id,
                    f"## Source: Nightly Delta (Part {part_number})",
                    result.text,
                )
                published_ids.add(chunk_id)
            acknowledged = end
            _save_progress(snapshot, acknowledged)

        if not _consume_snapshot(delta_file, snapshot):
            return BatchResult(
                BatchStatus.INVALID,
                detail="all chunks published, but delta changed before acknowledgement",
            )
        logger.info("Delta indexing complete; processed input acknowledged")
        return BatchResult(BatchStatus.OK, detail=f"published {len(chunks)} chunks")
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    result = asyncio.run(run_indexing())
    raise SystemExit(0 if result.ok else 1)
