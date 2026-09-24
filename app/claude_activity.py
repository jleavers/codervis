from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .budget import bounded_lines, env_float, env_int


@dataclass
class ClaudeActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class ClaudeActivityReader:
    """Reports Claude Code's local last activity from transcript timestamps.

    This reads only project transcript timestamp fields. It does not read
    .credentials.json, inspect usage fields, or compute quota statistics.

    Everything it reads is written by someone else -- anything that can write
    under ``~/.claude/projects``, including through a symlink -- so the scan is
    budgeted three ways: ``max_line_bytes`` per record, ``max_file_bytes`` per
    file, and ``scan_deadline_seconds`` (with ``max_files``) across the whole
    walk. Exceeding a budget truncates the scan and reports the timestamps
    already found; it never fails the section and never grows without bound.

    There is no TTL here any more. The refresher in ``app.main`` owns how often
    this runs; the per-file cache below is a different thing and stays, because
    it keys on (mtime, size) and is what keeps a steady-state scan cheap.
    """

    def __init__(
        self,
        data_dir: str | Path,
        max_line_bytes: int = 1024 * 1024,
        max_file_bytes: int = 16 * 1024 * 1024,
        scan_deadline_seconds: float = 5.0,
        max_files: int = 20000,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.max_line_bytes = max_line_bytes
        self.max_file_bytes = max_file_bytes
        self.scan_deadline_seconds = scan_deadline_seconds
        self.max_files = max_files
        self._file_cache: dict[Path, tuple[float, int, datetime | None]] = {}

    def snapshot(self) -> ClaudeActivitySnapshot:
        """One bounded scan. Called only from this source's refresher."""
        return self._scan()

    def _scan(self) -> ClaudeActivitySnapshot:
        deadline = time.monotonic() + self.scan_deadline_seconds
        data_root_exists = self.data_dir.exists()
        paths = self._iter_transcripts(deadline) if data_root_exists else []
        for stale in set(self._file_cache.keys()) - set(paths):
            self._file_cache.pop(stale, None)

        last_activity: datetime | None = None
        for path in paths:
            dt = self._last_activity_for(path)
            if dt is not None and (last_activity is None or dt > last_activity):
                last_activity = dt
            if time.monotonic() >= deadline:
                # Out of time. The per-file (mtime, size) cache makes the files
                # already done nearly free next time, so a scan cut short here
                # gets further on the next pass rather than losing the tail for
                # good. The max_files cut below is not like that -- it stops at
                # the same prefix every time -- which is why its default is set
                # far above any real tree.
                break
        return ClaudeActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _iter_transcripts(self, deadline: float) -> list[Path]:
        projects = self.data_dir / "projects"
        if not projects.is_dir():
            return []
        found: list[Path] = []
        try:
            for path in projects.rglob("*.jsonl"):
                if path.is_file():
                    found.append(path)
                # Per match, not per entry: rglob("*.jsonl") filters by name
                # without statting, so entries that do not match cost almost
                # nothing and the deadline is what bounds a huge tree of them.
                # (Codex's reader walks "*" and does have to count every entry.)
                if len(found) >= self.max_files or time.monotonic() >= deadline:
                    break
        except OSError:
            return found
        return found

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
            # Binary, so that the record boundary is found before anything is
            # decoded: a text-mode iterator would build the whole unterminated
            # record first, which is the case this is here to stop.
            with path.open("rb") as f:
                for record in bounded_lines(
                    f,
                    max_line_bytes=self.max_line_bytes,
                    max_file_bytes=self.max_file_bytes,
                ):
                    line = record.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
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
    return ClaudeActivityReader(
        data_dir,
        max_line_bytes=env_int("ACTIVITY_MAX_LINE_BYTES", 1024 * 1024),
        max_file_bytes=env_int("ACTIVITY_MAX_FILE_BYTES", 16 * 1024 * 1024),
        scan_deadline_seconds=env_float("ACTIVITY_SCAN_DEADLINE_SECONDS", 5.0),
        max_files=env_int("ACTIVITY_MAX_FILES", 20000),
    )
