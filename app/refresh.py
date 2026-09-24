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


class SourceStale(Exception):
    """The last refresh of a source is too old to keep reporting it as live.

    Raised by nothing: it is the ``error`` a stale snapshot carries, so that a
    handler reports the same ``unavailable`` state it would for a failed fetch.
    A read that hangs with no deadline of its own -- the credential file, one
    transcript file -- stops the source's thread advancing without ever failing
    it, and without this the last good percentages would be served as ``live``
    forever, with nothing in the payload saying how old they are.
    """


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

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """How long ago this was published, or None before the first publish."""
        if self.at is None:
            return None
        return ((now or datetime.now(timezone.utc)) - self.at).total_seconds()


_PENDING: SourceSnapshot = SourceSnapshot(ok=False, value=None, error=None, at=None)

# No source is read more often than this, whatever it is asked for.
MIN_INTERVAL_SECONDS = 0.01

# How far a source may fall behind its own cadence before what it last
# published stops counting as live. Deliberately slack: this is meant to catch
# a thread that has stopped advancing altogether, not a cycle that ran long, so
# a slow-but-working source must never flap to `unavailable`. The grace term is
# what keeps the fast activity cadences (5 s) from doing exactly that.
STALE_AFTER_INTERVALS = 3.0
STALE_GRACE_SECONDS = 30.0


class SourceRefresher(Generic[T]):
    """Runs one blocking ``fetch`` on a cadence and publishes what it returned."""

    def __init__(
        self,
        name: str,
        fetch: Callable[[], T],
        interval_seconds: float,
        stale_after_seconds: float | None = None,
    ) -> None:
        self.name = name
        self._fetch = fetch
        # A floor rather than just a non-negative: a zero interval is a hot
        # loop against the source by construction, which is the thing this
        # class exists to make impossible.
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, interval_seconds)
        self.stale_after_seconds = (
            max(
                STALE_AFTER_INTERVALS * self.interval_seconds,
                self.interval_seconds + STALE_GRACE_SECONDS,
            )
            if stale_after_seconds is None
            else stale_after_seconds
        )
        self._lock = threading.Lock()
        self._snapshot: SourceSnapshot[T] = _PENDING
        # Each thread gets its own stop flag, created in start(). A single
        # shared Event would let a later start() clear the flag of a thread
        # that stop() failed to join, reviving it -- two live threads, both
        # fetching, only the newer one stoppable.
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def snapshot(self) -> SourceSnapshot[T]:
        """The last published outcome, exactly as it was published.

        Never does I/O and never blocks on a read. Handlers should use
        ``current()`` instead; this is the raw record, for the refresher's own
        tests and for anything that needs to see through the staleness rule.
        """
        with self._lock:
            return self._snapshot

    def current(self) -> SourceSnapshot[T]:
        """What a handler should serve: the last outcome, unless it is stale.

        A success that stopped being refreshed becomes a failure carrying
        ``SourceStale``, so the provider degrades to ``unavailable`` rather
        than showing old numbers as current. ``value`` is dropped with it, so
        there is no path by which a stale reading reaches the payload.

        A *failure* is left alone: it is already ``unavailable``, and its own
        error says more about what is wrong than its age does.
        """
        snapshot = self.snapshot()
        if not snapshot.ok:
            return snapshot
        age = snapshot.age_seconds()
        if age is None or age <= self.stale_after_seconds:
            return snapshot
        return SourceSnapshot(
            ok=False,
            value=None,
            error=SourceStale(
                f"last refresh was {age:.0f}s ago, over the "
                f"{self.stale_after_seconds:.0f}s limit for this source"
            ),
            at=snapshot.at,
        )

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
        """Start the refresher's thread. Idempotent, and refuses to double up.

        If a previous thread was asked to stop but is still wedged in a read,
        this does nothing: it is already stopping, and a second thread would
        double the source's call volume for as long as the first took to die.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        stop = threading.Event()
        self._stop = stop
        self._thread = threading.Thread(
            target=self._run, args=(stop,), name=f"refresh:{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Ask the thread to finish the current cycle and stop waiting for it.

        The join is best-effort on purpose: a read wedged in the kernel would
        otherwise hold shutdown open for as long as it wants to. The thread is
        a daemon, so leaving it behind is safe -- and the reference is kept, so
        a later start() can see it is still alive and decline to add a second.
        """
        if self._stop is not None:
            self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if not thread.is_alive():
                self._thread = None

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh_once()
            # A gap rather than a period: a fetch that overruns its interval
            # still cannot turn into a hot loop against the source.
            stop.wait(self.interval_seconds)


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
