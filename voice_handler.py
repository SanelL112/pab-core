"""Local-only voice transcription.

Audio is personal data.  It is never sent to agy/Gemini or another cloud
provider, and failures are raised as a typed exception so error text cannot be
mistaken for a user prompt.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_WHISPER_MODEL = None
_WHISPER_MODEL_LOCK = threading.Lock()


class TranscriptionFailure(str, Enum):
    INVALID_FILE = "invalid_file"
    TOO_LARGE = "too_large"
    LOCAL_ENGINE_UNAVAILABLE = "local_engine_unavailable"


class TranscriptionUnavailable(RuntimeError):
    """Typed local transcription failure safe for the Telegram handler."""

    def __init__(self, reason: TranscriptionFailure):
        self.reason = reason
        super().__init__(reason.value)


def transcribe_voice(file_path: str) -> str:
    """Transcribe a voice message locally or raise ``TranscriptionUnavailable``."""

    path = Path(file_path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise TranscriptionUnavailable(TranscriptionFailure.INVALID_FILE) from exc
    if not path.is_file() or path.is_symlink():
        raise TranscriptionUnavailable(TranscriptionFailure.INVALID_FILE)
    if stat.st_size <= 0:
        raise TranscriptionUnavailable(TranscriptionFailure.INVALID_FILE)
    if stat.st_size > _MAX_AUDIO_BYTES:
        raise TranscriptionUnavailable(TranscriptionFailure.TOO_LARGE)

    text = _try_whisper_cpp(str(path))
    if text:
        return text

    text = _try_whisper_cli(str(path))
    if text:
        return text

    raise TranscriptionUnavailable(TranscriptionFailure.LOCAL_ENGINE_UNAVAILABLE)


def _try_whisper_cpp(file_path: str) -> str:
    """Try whisper.cpp with a bounded local ffmpeg conversion."""

    model_path = "/usr/local/share/whisper/ggml-base.en.bin"
    if not os.path.isfile(model_path):
        return ""
    binaries = ("/usr/local/bin/whisper-cpp", os.path.expanduser("~/.local/bin/whisper-cpp"))
    for binary in binaries:
        if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            continue
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                wav_path = os.path.join(tmpdir, "audio.wav")
                converted = subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", file_path,
                        "-ar", "16000", "-ac", "1", "-y", wav_path,
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if converted.returncode != 0 or not os.path.isfile(wav_path):
                    continue
                result = subprocess.run(
                    [binary, "-m", model_path, "-f", wav_path, "-l", "en"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    text = result.stdout.strip()
                    if len(text) > 3:
                        logger.info("whisper.cpp transcribed %s chars", len(text))
                        return text
        except (OSError, subprocess.SubprocessError):
            logger.warning("whisper.cpp transcription failed", exc_info=True)
    return ""


def _get_whisper_model():
    """Load the expensive Python Whisper model at most once per process."""

    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_MODEL_LOCK:
        if _WHISPER_MODEL is None:
            import whisper

            _WHISPER_MODEL = whisper.load_model("base", device="cpu")
    return _WHISPER_MODEL


def _try_whisper_cli(file_path: str) -> str:
    """Try cached openai-whisper inference on CPU."""

    try:
        result = _get_whisper_model().transcribe(file_path, language="en")
        text = result.get("text", "").strip() if isinstance(result, dict) else ""
        if text:
            logger.info("Python Whisper transcribed %s chars", len(text))
            return text
    except ImportError:
        logger.info("Python Whisper is not installed")
    except Exception as exc:
        logger.warning("Python Whisper failed: %s", type(exc).__name__)
    return ""
