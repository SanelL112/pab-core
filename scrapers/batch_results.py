"""Typed outcomes and validation helpers for unattended batch jobs.

Batch pipelines must not infer success from ``bool(model_output)``.  Several
model adapters intentionally return human-readable warning strings on failure;
those strings are useful at an interactive boundary but are not valid artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class BatchStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    INVALID = "invalid"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BatchResult:
    status: BatchStatus
    text: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is BatchStatus.OK

    def __bool__(self) -> bool:
        """Keep ``if result`` useful while making it mean validated success."""
        return self.ok


_UNAVAILABLE_MARKERS = (
    "local inference unavailable",
    "cloud fallback disabled",
    "all models failed",
    "models failed to generate",
    "returned empty",
    "missing openrouter_api_key",
    "service unavailable",
    "temporarily unavailable",
)

_REFUSAL_PREFIXES = (
    "i cannot ",
    "i can't ",
    "i can’t ",
    "i am unable ",
    "i'm unable ",
    "i’m unable ",
    "i'm sorry",
    "i’m sorry",
    "i apologize",
    "as an ai",
)


def _coerce_output(value: Any) -> tuple[str, BatchStatus | None, str]:
    """Accept strings and typed router results without coupling to the router."""
    if value is None:
        return "", BatchStatus.EMPTY, "model returned no value"
    if isinstance(value, str):
        return value.strip(), None, ""

    status = getattr(value, "status", None)
    text = getattr(value, "text", "")
    detail = str(getattr(value, "detail", "") or getattr(value, "error", "") or "")
    if status is not None:
        normalized_status = str(getattr(status, "value", status)).lower()
        if normalized_status not in {"ok", "success", "completed"}:
            if "refus" in normalized_status:
                return str(text or "").strip(), BatchStatus.REFUSED, detail
            if normalized_status in {"empty", "no_content"}:
                return "", BatchStatus.EMPTY, detail
            return str(text or "").strip(), BatchStatus.UNAVAILABLE, detail or normalized_status
    return str(text or "").strip(), None, detail


def validate_generated_text(
    value: Any,
    *,
    min_chars: int = 20,
    required_markers: Iterable[str] = (),
) -> BatchResult:
    """Return ``OK`` only for content safe to publish as a batch artifact.

    ``required_markers`` are case-insensitive structural checks, such as the
    headings required in the curated-memory document.
    """
    text, forced_status, detail = _coerce_output(value)
    if forced_status is not None:
        return BatchResult(forced_status, detail=detail)
    if not text:
        return BatchResult(BatchStatus.EMPTY, detail="model returned empty text")

    normalized = " ".join(text.lower().split())
    if text.startswith(("⚠️", "❌")) or any(marker in normalized for marker in _UNAVAILABLE_MARKERS):
        return BatchResult(BatchStatus.UNAVAILABLE, detail=text[:240])

    beginning = normalized[:240]
    if any(beginning.startswith(prefix) for prefix in _REFUSAL_PREFIXES):
        return BatchResult(BatchStatus.REFUSED, detail=text[:240])
    if len(text) < min_chars:
        return BatchResult(
            BatchStatus.INVALID,
            detail=f"output too short ({len(text)} < {min_chars} characters)",
        )

    missing = [marker for marker in required_markers if marker.lower() not in normalized]
    if missing:
        return BatchResult(
            BatchStatus.INVALID,
            detail=f"missing required structure: {', '.join(missing)}",
        )
    return BatchResult(BatchStatus.OK, text=text)
