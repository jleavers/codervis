from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


@dataclass
class ClaudeActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class ClaudeActivityReader:
    """Reports Claude Code's local last activity from transcript timestamps.

    This reads only project transcript timestamp fields. It does not read
    .credentials.json, inspect usage fields, or compute quota statistics.
    """

    def __init__(
        self,
        data_dir: str | Path,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._file_cache: dict[Path, tuple[float, int, datetime | None]] = {}
        self._snapshot_cache: tuple[float, ClaudeActivitySnapshot] | None = None
        self._lock = Lock()

    def snapshot(self) -> ClaudeActivitySnapshot:
        with self._lock:
            now = time.monotonic()
            if (
                self._snapshot_cache
                and (now - self._snapshot_cache[0]) < self.cache_ttl_seconds
            ):
                return self._snapshot_cache[1]
            snap = self._scan()
            self._snapshot_cache = (now, snap)
            return snap

    def _scan(self) -> ClaudeActivitySnapshot:
        data_root_exists = self.data_dir.exists()
        paths = self._iter_transcripts() if data_root_exists else []
        for stale in set(self._file_cache.keys()) - set(paths):
            self._file_cache.pop(stale, None)

        last_activity = max(
            (dt for path in paths if (dt := self._last_activity_for(path)) is not None),
            default=None,
        )
        return ClaudeActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _iter_transcripts(self) -> list[Path]:
        projects = self.data_dir / "projects"
        if not projects.is_dir():
            return []
        try:
            return [path for path in projects.rglob("*.jsonl") if path.is_file()]
        except OSError:
            return []

    def _last_activity_for(self, path: Path) -> datetime | None:
        try:
            st = path.stat()
        except OSError:
            return None

        cached = self._file_cache.get(path)
        if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]

        last_activity = self._parse_last_activity(path)
        self._file_cache[path] = (st.st_mtime, st.st_size, last_activity)
        return last_activity

    def _parse_last_activity(self, path: Path) -> datetime | None:
        last_activity: datetime | None = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    dt = _timestamp_from_obj(obj)
                    if dt is not None and (last_activity is None or dt > last_activity):
                        last_activity = dt
        except OSError:
            return None
        return last_activity


def _timestamp_from_obj(obj: object) -> datetime | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    return _parse_ts(value)


def _parse_ts(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def reader_from_env() -> ClaudeActivityReader:
    data_dir = os.environ.get("CLAUDE_DATA_DIR", "/data/claude")
    ttl = float(os.environ.get("CLAUDE_ACTIVITY_CACHE_TTL_SECONDS", "5"))
    return ClaudeActivityReader(data_dir, cache_ttl_seconds=ttl)
