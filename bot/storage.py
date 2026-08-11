"""Durable, private JSON storage primitives.

The bot still has a number of JSON consumers, so this module provides a safe
compatibility layer while runtime data is gradually moved out of the source
checkout.  Writes are serialized across threads *and* processes, use an
``fsync`` + ``replace`` transaction, and retain one last-known-good backup.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


class StorageError(RuntimeError):
    """Base class for durable-storage failures."""


class StorageCorruptionError(StorageError):
    """Raised when neither the primary file nor its backup is valid JSON."""


class AtomicJSONStore(Generic[T]):
    """A small cross-process transactional JSON store.

    ``default_factory`` must return a fresh value.  A valid backup is used for
    reads if the primary file is truncated or malformed, but the corrupt
    primary is never silently overwritten by a default value.
    """

    def __init__(self, path: str | os.PathLike[str], default_factory: Callable[[], T]):
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.default_factory = default_factory
        self._thread_lock = threading.RLock()

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    @contextmanager
    def _locked(self, *, exclusive: bool):
        self._ensure_parent()
        with self._thread_lock:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    @staticmethod
    def _decode(path: Path) -> T:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise StorageCorruptionError(f"invalid JSON storage file: {path}") from exc

    def _read_unlocked(self) -> T:
        if not self.path.exists():
            return self.default_factory()
        try:
            return self._decode(self.path)
        except StorageCorruptionError as primary_error:
            if self.backup_path.exists():
                try:
                    return self._decode(self.backup_path)
                except StorageCorruptionError:
                    pass
            raise primary_error

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            AtomicJSONStore._fsync_directory(path.parent)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    def _write_unlocked(self, value: T) -> None:
        try:
            payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StorageError(f"value for {self.path} is not JSON serializable") from exc

        # Only promote a validated primary to the last-known-good backup.
        if self.path.exists():
            try:
                self._decode(self.path)
                previous = self.path.read_bytes()
            except (StorageCorruptionError, OSError):
                previous = None
            if previous is not None:
                self._atomic_bytes(self.backup_path, previous)

        self._atomic_bytes(self.path, payload)

    def read(self) -> T:
        with self._locked(exclusive=False):
            return deepcopy(self._read_unlocked())

    def write(self, value: T) -> None:
        with self._locked(exclusive=True):
            self._write_unlocked(deepcopy(value))

    def update(self, mutator: Callable[[T], object]) -> T:
        """Read, mutate, and commit while holding one exclusive file lock.

        Mutators may modify the value in place.  If they return a non-``None``
        value, that value is committed instead, which also supports immutable
        JSON roots.
        """
        with self._locked(exclusive=True):
            value = self._read_unlocked()
            replacement = mutator(value)
            if replacement is not None:
                value = replacement  # type: ignore[assignment]
            self._write_unlocked(value)
            return deepcopy(value)

