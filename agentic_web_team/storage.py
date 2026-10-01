from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    pass


def ensure_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise StorageError(f"Refusing symlinked state directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


class JsonStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if self.path.is_symlink():
            raise StorageError(f"Refusing symlinked state file: {self.path}")
        if not self.path.is_file():
            return None
        try:
            if self.path.stat().st_size > 10_000_000:
                raise StorageError(f"State at {self.path} exceeds 10 MB")
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"Cannot read state at {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StorageError(f"State at {self.path} must be a JSON object")
        return data

    def save(self, data: dict[str, Any]) -> None:
        ensure_private_dir(self.path.parent)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        if len(payload.encode()) > 10_000_000:
            raise StorageError("Work state exceeds 10 MB")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, prefix=".state-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                os.chmod(stream.name, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StorageError(f"Cannot save state at {self.path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path

    def append(self, speaker: str, message: str) -> dict[str, str]:
        if len(message) > 100_000:
            raise StorageError("Conversation event exceeds 100,000 characters")
        event = {"speaker": speaker, "message": message}
        ensure_private_dir(self.path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            while payload:
                payload = payload[os.write(fd, payload) :]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return event

    def recent(self, limit: int = 80) -> list[dict[str, str]]:
        if self.path.is_symlink():
            raise StorageError(f"Refusing symlinked history file: {self.path}")
        if not self.path.is_file():
            return []
        events: deque[dict[str, str]] = deque(maxlen=limit)
        with self.path.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                stream.seek(0, os.SEEK_END)
                offset = max(0, stream.tell() - 1_000_000)
                stream.seek(offset)
                if offset:
                    stream.readline()
                for raw_line in stream:
                    try:
                        item = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("speaker"), str)
                        and isinstance(item.get("message"), str)
                    ):
                        events.append({"speaker": item["speaker"], "message": item["message"]})
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return list(events)


class WorkflowLease:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        ensure_private_dir(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
