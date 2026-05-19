from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterator


ACTIVITY_FILES = ("history.jsonl", "session_index.jsonl")
ACTIVITY_DIRS = ("sessions", "archived_sessions")


@dataclass
class CodexActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class CodexActivityReader:
    """Derives Codex's local last-activity timestamp from safe file metadata.

    This intentionally stats known activity/session files instead of reading
    their contents, and it never touches auth.json.
    """

    def __init__(
        self,
        data_dir: str | Path,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._lock = Lock()
        self._cached: tuple[float, CodexActivitySnapshot] | None = None

    def snapshot(self) -> CodexActivitySnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._scan()
            self._cached = (now, snap)
            return snap

    def _scan(self) -> CodexActivitySnapshot:
        data_root_exists = self.data_dir.exists()
        last_activity: datetime | None = None
        if data_root_exists:
            for path in self._activity_paths():
                dt = _mtime(path)
                if dt is not None and (last_activity is None or dt > last_activity):
                    last_activity = dt
        return CodexActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _activity_paths(self) -> Iterator[Path]:
        for name in ACTIVITY_FILES:
            path = self.data_dir / name
            if path.is_file():
                yield path

        for name in ACTIVITY_DIRS:
            root = self.data_dir / name
            if not root.is_dir():
                continue
            try:
                for path in root.rglob("*"):
                    if path.is_file():
                        yield path
            except OSError:
                continue


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def reader_from_env() -> CodexActivityReader:
    data_dir = os.environ.get("CODEX_DATA_DIR", "/data/codex")
    ttl = float(os.environ.get("CODEX_ACTIVITY_CACHE_TTL_SECONDS", "5"))
    return CodexActivityReader(data_dir, cache_ttl_seconds=ttl)
