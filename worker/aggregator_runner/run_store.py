"""Durable predict-run state with atomic writes and a per-run lock.

Writes go to a temporary file in the same directory, are fsynced, and are
renamed over the run file, so a reader never sees a torn file. A run is
processed only while its `.lock` sibling is held with an exclusive
non-blocking `flock`, so two runners on one host cannot both claim it.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def paths(self) -> list[Path]:
        if not self.runs_dir.exists():
            return []
        return sorted(path for path in self.runs_dir.glob("*.json") if not path.name.endswith(".tmp"))

    def load(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, path: Path, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @contextmanager
    def lock(self, path: Path) -> Iterator[bool]:
        """Yield True while holding the run's exclusive lock, False if another runner holds it."""

        lock_path = path.with_name(f"{path.stem}.lock")
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
