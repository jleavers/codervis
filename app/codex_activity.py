from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .budget import env_float, env_int


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

    No content is read, so there is no byte budget to set here; what is
    unbounded is the *walk*, over a tree someone else fills. ``max_files`` and
    ``scan_deadline_seconds`` bound it, and a truncated walk reports the
    timestamps already found.

    There is no TTL here any more: the refresher in ``app.main`` owns how often
    this runs, and caches a failure as well as a success.
    """

    def __init__(
        self,
        data_dir: str | Path,
        scan_deadline_seconds: float = 5.0,
        max_files: int = 20000,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.scan_deadline_seconds = scan_deadline_seconds
        self.max_files = max_files

    def snapshot(self) -> CodexActivitySnapshot:
        """One bounded scan. Called only from this source's refresher."""
        return self._scan()

    def _scan(self) -> CodexActivitySnapshot:
        deadline = time.monotonic() + self.scan_deadline_seconds
        data_root_exists = self.data_dir.exists()
        last_activity: datetime | None = None
        if data_root_exists:
            for path in self._activity_paths(deadline):
                dt = _mtime(path)
                if dt is not None and (last_activity is None or dt > last_activity):
                    last_activity = dt
        return CodexActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _activity_paths(self, deadline: float) -> Iterator[Path]:
        for name in ACTIVITY_FILES:
            path = self.data_dir / name
            if path.is_file():
                yield path

        # The budget is spent on every entry the walk *touches*, not on every
        # entry it yields: rglob("*") stats each one, so a tree of directories
        # with no files in it would otherwise cost the whole walk for free.
        seen = 0
        for name in ACTIVITY_DIRS:
            root = self.data_dir / name
            if not root.is_dir():
                continue
            try:
                for path in root.rglob("*"):
                    seen += 1
                    if seen > self.max_files or time.monotonic() >= deadline:
                        return
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
    return CodexActivityReader(
        data_dir,
        scan_deadline_seconds=env_float("ACTIVITY_SCAN_DEADLINE_SECONDS", 5.0),
        max_files=env_int("ACTIVITY_MAX_FILES", 20000),
    )
