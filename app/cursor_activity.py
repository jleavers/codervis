from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterator


# Relative paths under the Cursor data dir whose modification time tracks
# local activity. state.vscdb is touched constantly while Cursor is in use;
# the History/workspaceStorage dir mtimes update as edits accrue. We only
# stat these (file metadata) — we never read state.vscdb contents or the
# stored token; that is cursor_quota's job.
ACTIVITY_FILES = (("User", "globalStorage", "state.vscdb"),)
ACTIVITY_DIRS = (("User", "History"), ("User", "workspaceStorage"))


@dataclass
class CursorActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class CursorActivityReader:
    """Derives Cursor's local last-activity timestamp from safe file metadata.

    This intentionally stats known activity paths instead of reading their
    contents, and it never reads the stored auth token in state.vscdb.
    """

    def __init__(
        self,
        data_dir: str | Path,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._lock = Lock()
        self._cached: tuple[float, CursorActivitySnapshot] | None = None

    def snapshot(self) -> CursorActivitySnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._scan()
            self._cached = (now, snap)
            return snap

    def _scan(self) -> CursorActivitySnapshot:
        data_root_exists = self.data_dir.exists()
        last_activity: datetime | None = None
        if data_root_exists:
            for path in self._activity_paths():
                dt = _mtime(path)
                if dt is not None and (last_activity is None or dt > last_activity):
                    last_activity = dt
        return CursorActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _activity_paths(self) -> Iterator[Path]:
        for rel in ACTIVITY_FILES:
            path = self.data_dir.joinpath(*rel)
            if path.is_file():
                yield path
        for rel in ACTIVITY_DIRS:
            path = self.data_dir.joinpath(*rel)
            if path.is_dir():
                yield path


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def reader_from_env() -> CursorActivityReader:
    data_dir = os.environ.get("CURSOR_DATA_DIR", "/data/cursor")
    ttl = float(os.environ.get("CURSOR_ACTIVITY_CACHE_TTL_SECONDS", "5"))
    return CursorActivityReader(data_dir, cache_ttl_seconds=ttl)
