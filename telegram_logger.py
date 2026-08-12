"""Non-blocking, rate-limited Telegram alert logging."""
import logging
import queue
import threading
import time

import requests
from config import SANEL_CHAT_ID, TELEGRAM_BOT_TOKEN


class TelegramHandler(logging.Handler):
    """Queue log alerts so Handler.emit never performs network I/O."""

    _TELEGRAM_ERROR_FRAGMENTS = (
        "Can't parse entities",
        "can't find end of the entity",
        "bad request: can't parse",
        "Bad Request: can't parse",
        "entity starting at byte offset",
        "Telegram send failed",
    )

    def __init__(self, *, max_queue_size: int = 100, cooldown_seconds: float = 15.0):
        super().__init__()
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = SANEL_CHAT_ID
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=max_queue_size)
        self._cooldown_seconds = cooldown_seconds
        self._last_sent_by_fingerprint: dict[str, float] = {}
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def _is_telegram_feedback_loop(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return any(fragment in message for fragment in self._TELEGRAM_ERROR_FRAGMENTS)

    def emit(self, record: logging.LogRecord) -> None:
        if not self.token or not self.chat_id:
            return
        if self._is_telegram_feedback_loop(record):
            return
        if record.name.startswith(("urllib3", "httpx", "telegram", "apscheduler")):
            return

        try:
            from utils import sanitize_markdown, scrub_pii

            safe_entry = sanitize_markdown(scrub_pii(self.format(record)))
            fingerprint = safe_entry[:500]
            now = time.monotonic()
            if now - self._last_sent_by_fingerprint.get(fingerprint, 0.0) < self._cooldown_seconds:
                return
            self._last_sent_by_fingerprint[fingerprint] = now
            self._queue.put_nowait({
                "chat_id": self.chat_id,
                "text": f"⚙️ `{safe_entry}`",
                "parse_mode": "Markdown",
                "disable_notification": True,
            })
        except queue.Full:
            # Never block application logging because the alert sink is unhealthy.
            return
        except Exception:
            self.handleError(record)

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="telegram-log-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json=payload,
                    timeout=2,
                ).raise_for_status()
            except Exception:
                # Do not log here: logging failures must not recursively enqueue alerts.
                pass
            finally:
                self._queue.task_done()


def setup_telegram_logging() -> None:
    root_logger = logging.getLogger()
    if any(isinstance(handler, TelegramHandler) for handler in root_logger.handlers):
        return

    handler = TelegramHandler()
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.start()
    root_logger.addHandler(handler)
