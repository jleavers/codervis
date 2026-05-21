from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterator


# Relative paths under the Copilot config dir whose modification time tracks
# local activity. The editor integration rewrites these credential/config
# files when it refreshes the OAuth token, so their mtime is a coarse "recently
# used" signal. We only stat them (file metadata) — we never read their
# contents or the stored token; that is copilot_quota's job. Copilot keeps no
# per-session transcript here, so this is a weaker signal than the other agents.
ACTIVITY_FILES = (("apps.json",), ("hosts.json",), ("versions.json",))
ACTIVITY_DIRS = (("logs",),)


@dataclass
class CopilotActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class CopilotActivityReader:
    """Derives Copilot's local last-activity timestamp from safe file metadata.

    This intentionally stats known config/log paths instead of reading their
    contents, and it never reads the stored OAuth token in apps.json/hosts.json.
    """

    def __init__(
        self,
        data_dir: str | Path,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._lock = Lock()
        self._cached: tuple[float, CopilotActivitySnapshot] | None = None

    def snapshot(self) -> CopilotActivitySnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._scan()
            self._cached = (now, snap)
            return snap

    def _scan(self) -> CopilotActivitySnapshot:
        data_root_exists = self.data_dir.exists()
        last_activity: datetime | None = None
        if data_root_exists:
            for path in self._activity_paths():
                dt = _mtime(path)
                if dt is not None and (last_activity is None or dt > last_activity):
                    last_activity = dt
        return CopilotActivitySnapshot(
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


def reader_from_env() -> CopilotActivityReader:
    data_dir = os.environ.get("COPILOT_DATA_DIR", "/data/copilot")
    ttl = float(os.environ.get("COPILOT_ACTIVITY_CACHE_TTL_SECONDS", "5"))
    return CopilotActivityReader(data_dir, cache_ttl_seconds=ttl)
