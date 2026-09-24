"""Budgets for the reads that feed the payload.

Every payload-feeding read is somebody else's to lengthen: the upstream
response body is written by the vendor (or by whatever an operator's
``CLAUDE_AI_HOST``/``CHATGPT_HOST`` points at), and a transcript file is
written by whoever can write under ``~/.claude``. urllib's timeout applies to
one socket operation, so a sender that keeps trickling is never cut off, and
nothing at all bounds how many bytes a read may accumulate.

This module holds the two bounds that fix that -- a total deadline and a byte
cap -- plus the tolerant environment parsing the callers share. Each caller
converts ``BudgetExceeded`` into its own error type so the "any failure ->
LiveQuotaError / CodexLiveQuotaError -> unavailable" contract still holds.
"""

from __future__ import annotations

import os
import time
from typing import BinaryIO, Iterator

CHUNK_BYTES = 64 * 1024


class BudgetExceeded(Exception):
    """A read ran past its total deadline or its byte cap."""


def deadline_in(seconds: float) -> float:
    """A monotonic instant ``seconds`` from now."""
    return time.monotonic() + seconds


def remaining(deadline: float, cap: float) -> float:
    """Seconds left before ``deadline``, never more than ``cap``.

    Raised rather than returned as zero, so a caller cannot accidentally pass
    a non-positive timeout to urllib (which reads that as "no timeout").
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise BudgetExceeded("total deadline exceeded")
    return min(cap, left)


def read_capped(resp, *, max_bytes: int, deadline: float) -> bytes:
    """Read a response body, giving up at ``max_bytes`` or at ``deadline``.

    ``read1`` is preferred where the response offers it, because it returns
    what has arrived rather than blocking until the full chunk is there: that
    is what keeps the deadline check from being reached late. The deadline is
    therefore honoured to within one socket timeout, not exactly.
    """
    read = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise BudgetExceeded("upstream body was still arriving at the deadline")
        # One byte past the cap, so that a body of exactly max_bytes is
        # accepted and the first byte beyond it is detected without ever
        # holding more than the cap plus one byte.
        chunk = read(min(CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise BudgetExceeded(f"upstream body exceeded {max_bytes} bytes")
        chunks.append(chunk)


def bounded_lines(
    stream: BinaryIO,
    *,
    max_line_bytes: int,
    max_file_bytes: int,
) -> Iterator[bytes]:
    """Yield newline-terminated records without ever building an unbounded one.

    A file with no newline and no end -- a symlink to an endless device, a
    writer that never terminates a record -- would otherwise grow one line
    until ``MemoryError``. Here a record longer than ``max_line_bytes`` is
    dropped and the reader skips to the next newline, so one hostile record
    costs the rest of the file nothing, and no more than ``max_file_bytes`` is
    read from any one file.
    """
    buf = bytearray()
    read_total = 0
    skipping = False
    while read_total < max_file_bytes:
        chunk = stream.read(min(CHUNK_BYTES, max_file_bytes - read_total))
        if not chunk:
            break
        read_total += len(chunk)
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            record = bytes(buf[:nl])
            del buf[: nl + 1]
            if skipping:
                skipping = False  # the tail of a record already given up on
            elif len(record) <= max_line_bytes:
                yield record
            # else: a complete record over the cap. Dropped here as well as
            # below, because a chunk can contain one whole -- the pending-buffer
            # check alone would let anything shorter than a chunk straight past.
        if len(buf) > max_line_bytes:
            # A record still arriving that is already over the cap: give up on
            # it now, and skip to the next newline rather than keep building.
            buf.clear()
            skipping = True
    if buf and not skipping:
        yield bytes(buf)


def env_float(name: str, default: float, *, fallback: str | None = None) -> float:
    """A positive float from the environment; the default for anything else.

    Deliberately tolerant: a typo in an operator's ``.env`` should not stop the
    dashboard from starting, it should leave the shipped budget in place.
    """
    for key in (name, fallback):
        if key is None:
            continue
        raw = os.environ.get(key)
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return default


def env_int(name: str, default: int, *, fallback: str | None = None) -> int:
    """A positive int from the environment; the default for anything else."""
    for key in (name, fallback):
        if key is None:
            continue
        raw = os.environ.get(key)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return default
