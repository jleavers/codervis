"""One background refresher per payload source.

The invariant this exists to hold: *all* I/O that feeds the payload runs here,
off the event loop, on a fixed cadence, and its outcome -- failure included --
is what request handlers read. Nothing a request can do changes how often a
source is read, how long a read may take, or how many bytes it may use.

Each refresher owns a single daemon thread. One thread per source means at
most one fetch of that source is ever in flight: a wedged read delays its own
source and nothing else, and it cannot pile threads up behind it the way a
shared executor would. The thread is a daemon because a read wedged in the
kernel cannot be cancelled from here, and must not be able to hold up process
exit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SourceSnapshot(Generic[T]):
    """The last outcome a refresher published.

    ``ok`` distinguishes the two; ``error`` carries what the source raised when
    it is false. A failure is published exactly as a success is, so a failing
    source is re-read on the cadence and not once per request.
    """

    ok: bool
    value: T | None
    error: Exception | None
    at: datetime | None

    @property
    def published(self) -> bool:
        """False only for the placeholder served before the first refresh."""
        return self.at is not None


_PENDING: SourceSnapshot = SourceSnapshot(ok=False, value=None, error=None, at=None)


class SourceRefresher(Generic[T]):
    """Runs one blocking ``fetch`` on a cadence and publishes what it returned."""

    def __init__(
        self,
        name: str,
        fetch: Callable[[], T],
        interval_seconds: float,
    ) -> None:
        self.name = name
        self._fetch = fetch
        self.interval_seconds = max(0.0, interval_seconds)
        self._lock = threading.Lock()
        self._snapshot: SourceSnapshot[T] = _PENDING
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> SourceSnapshot[T]:
        """The last published outcome. Never does I/O, never blocks on a read."""
        with self._lock:
            return self._snapshot

    def refresh_once(self) -> SourceSnapshot[T]:
        """Run one guarded fetch on the calling thread and publish the outcome.

        ``Exception`` is caught whole, not a list of expected types: an escape
        would kill the worker thread and leave the source frozen at its last
        snapshot forever. ``MemoryError`` -- the way an unbounded read ends --
        is an ``Exception``, and is exactly the case that must not do that.
        """
        try:
            published: SourceSnapshot[T] = SourceSnapshot(
                ok=True,
                value=self._fetch(),
                error=None,
                at=datetime.now(timezone.utc),
            )
        except Exception as e:  # noqa: BLE001 -- see docstring
            published = SourceSnapshot(
                ok=False, value=None, error=e, at=datetime.now(timezone.utc)
            )
        with self._lock:
            self._snapshot = published
        return published

    def start(self) -> None:
        """Start the refresher's thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"refresh:{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Ask the thread to finish the current cycle and stop waiting for it.

        The join is best-effort on purpose: a read wedged in the kernel would
        otherwise hold shutdown open for as long as it wants to. The thread is
        a daemon, so leaving it behind is safe.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            # A gap rather than a period: a fetch that overruns its interval
            # still cannot turn into a hot loop against the source.
            self._stop.wait(self.interval_seconds)


def wait_for_first_publish(
    refreshers: "list[SourceRefresher] | tuple[SourceRefresher, ...]",
    timeout: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until every refresher has published once, or ``timeout`` passes.

    Used once, at startup, so the first page load is not served from the
    placeholder. It is bounded and does not scale with requests, which is the
    property that matters: a slow source costs one bounded wait at boot, not a
    stall on every request.
    """
    deadline = time.monotonic() + timeout
    while True:
        if all(r.snapshot().published for r in refreshers):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.02)
