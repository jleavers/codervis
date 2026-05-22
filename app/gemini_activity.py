from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterator


# Antigravity/Gemini local activity is spread across the shared ~/.gemini tree.
# We only stat known files/directories and never read OAuth token contents.
ACTIVITY_FILES = (
    ("antigravity-cli", "antigravity-oauth-token"),
    ("config", ".migrated"),
)
ACTIVITY_DIRS = (
    ("antigravity", "brain"),
    ("antigravity", "annotations"),
    ("config", "projects"),
)


@dataclass
class GeminiActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class GeminiActivityReader:
    """Derives Gemini/Antigravity last activity from safe file metadata."""

    def __init__(
        self,
        data_dir: str | Path,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._lock = Lock()
        self._cached: tuple[float, GeminiActivitySnapshot] | None = None

    def snapshot(self) -> GeminiActivitySnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._scan()
            self._cached = (now, snap)
            return snap

    def _scan(self) -> GeminiActivitySnapshot:
        data_root_exists = self.data_dir.exists()
        last_activity: datetime | None = None
        if data_root_exists:
            for path in self._activity_paths():
                dt = _mtime(path)
                if dt is not None and (last_activity is None or dt > last_activity):
                    last_activity = dt
        return GeminiActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _activity_paths(self) -> Iterator[Path]:
        for rel in ACTIVITY_FILES:
            path = self.data_dir.joinpath(*rel)
            if path.is_file():
                yield path

        for rel in ACTIVITY_DIRS:
            root = self.data_dir.joinpath(*rel)
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


def reader_from_env() -> GeminiActivityReader:
    data_dir = os.environ.get("GEMINI_DATA_DIR", "/data/gemini")
    ttl = float(os.environ.get("GEMINI_ACTIVITY_CACHE_TTL_SECONDS", "5"))
    return GeminiActivityReader(data_dir, cache_ttl_seconds=ttl)
