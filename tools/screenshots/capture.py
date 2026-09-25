"""Capture the README's GIF from the dashboard serving fabricated data.

``docs/images/usage-ramp.gif``  the dashboard once per stage as usage climbs from a few percent
to nearly all of it, so the meters' colour ramp (lime, amber, coral) is what the picture shows.

The real app is served -- its template, its SSE loop, its ``app.js`` colour function -- but its
two quota clients and two activity readers are swapped for stubs before the lifespan starts,
so no credential file is read and no upstream call is made. The data directories are pointed
at an empty temporary directory as well, so even the module-level clients constructed at
import are looking at nothing. Every percentage, plan name, reset time and activity time is
invented in ``STAGES`` below.

Playwright and Pillow are ephemeral here -- ``uv run --with playwright --with pillow`` -- so
neither joins the project's dependencies for the sake of a picture. The file must stay small
enough to belong in a repository: ``--width`` and ``--colours`` are the two dials if a redesign
pushes it over. See ``README.md`` beside this file for the whole procedure.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
IMAGES = ROOT / "docs" / "images"

# Nothing below this point reads the operator's data directories: the app's module-level
# clients are constructed from these variables, and they are replaced before anything calls
# them anyway. The SSE tick is the app's minimum, so a stage is on the page within a second.
_SCRATCH = tempfile.mkdtemp(prefix="codervis-capture-")
os.environ.update(
    {
        "CLAUDE_DATA_DIR": _SCRATCH,
        "CODEX_DATA_DIR": _SCRATCH,
        "REFRESH_INTERVAL_SECONDS": "1",
        "QUOTA_REFRESH_INTERVAL_SECONDS": "3600",
        "CLAUDE_ACTIVITY_REFRESH_INTERVAL_SECONDS": "3600",
        "CODEX_ACTIVITY_REFRESH_INTERVAL_SECONDS": "3600",
        "STARTUP_REFRESH_WAIT_SECONDS": "0",
    }
)
sys.path.insert(0, str(ROOT))

from app import main  # noqa: E402  (after the environment is set)
from app.claude_activity import ClaudeActivitySnapshot  # noqa: E402
from app.codex_activity import CodexActivitySnapshot  # noqa: E402

PORT = 8099
SIZE_LIMIT = 500 * 1024

# (claude 5h, claude weekly, claude fable weekly, codex 5h, codex weekly), in percent, and how
# long the frame holds. The last two stages linger so the coral end of the ramp is what a
# reader is left looking at.
STAGES = (
    ((3.2, 9.5, 4.1, 5.8, 12.0), 1400),
    ((18.7, 22.4, 11.6, 24.3, 27.5), 900),
    ((34.1, 36.8, 20.9, 41.6, 39.2), 900),
    ((49.6, 48.3, 31.7, 57.9, 51.4), 900),
    ((63.8, 59.1, 44.2, 72.4, 62.7), 900),
    ((78.2, 70.6, 58.5, 85.1, 74.3), 900),
    ((90.5, 81.9, 71.3, 94.7, 85.6), 1200),
    ((97.4, 90.2, 83.8, 99.1, 93.0), 2600),
)


class _Scripted:
    """A quota client or activity reader whose answer is whatever the capture last set."""

    credentials_path = Path(_SCRATCH) / "unused"
    data_dir = Path(_SCRATCH)
    timeout_seconds = 1.0
    total_deadline_seconds = 1.0
    scan_deadline_seconds = 1.0

    def __init__(self) -> None:
        self.value = None

    def get(self):
        return self.value

    def snapshot(self):
        return self.value


def _window(percent: float, resets_at: datetime):
    return SimpleNamespace(percent=percent, resets_at=resets_at)


def _publish(stage: tuple[float, ...], now: datetime) -> None:
    c5, c7, cf, x5, x7 = stage
    claude.value = SimpleNamespace(
        five_hour=_window(c5, now + timedelta(hours=2, minutes=41)),
        seven_day=_window(c7, now + timedelta(days=3, hours=6)),
        seven_day_fable=_window(cf, now + timedelta(days=3, hours=6)),
        subscription_type="max",
    )
    codex.value = SimpleNamespace(
        five_hour=_window(x5, now + timedelta(hours=1, minutes=12)),
        seven_day=_window(x7, now + timedelta(days=4, hours=19)),
        plan_type="pro",
    )
    claude_activity.value = ClaudeActivitySnapshot(
        last_activity=now - timedelta(minutes=2), data_root_exists=True
    )
    codex_activity.value = CodexActivitySnapshot(
        last_activity=now - timedelta(minutes=14), data_root_exists=True
    )
    for source in main._SOURCES:
        source.refresh_once()


claude = _Scripted()
codex = _Scripted()
claude_activity = _Scripted()
codex_activity = _Scripted()
main._live = claude
main._codex = codex
main._claude_activity = claude_activity
main._codex_activity = codex_activity


def _serve() -> threading.Thread:
    import uvicorn

    config = uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("the app did not start")
        time.sleep(0.05)
    return thread


def _capture(width: int, colours: int, out: Path) -> None:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    now = datetime.now(timezone.utc)
    frames: list[Image.Image] = []
    durations: list[int] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        _publish(STAGES[0][0], now)
        page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        panel = page.locator("main.panel")
        for stage, hold in STAGES:
            _publish(stage, now)
            # The SSE tick carries the stage to the page; wait for it to have arrived, then
            # for the meter's own 800 ms fill transition to settle, so no frame is mid-slide.
            page.wait_for_function(
                "expected => document.querySelector('#gauge-claude-five_hour .pct').textContent === expected",
                arg=f"{stage[0]:.1f}",
                timeout=10_000,
            )
            page.wait_for_timeout(1000)
            frames.append(Image.open(io.BytesIO(panel.screenshot())).convert("RGB"))
            durations.append(hold)
        browser.close()

    quantised = [f.quantize(colors=colours, method=Image.Quantize.MEDIANCUT) for f in frames]
    quantised[0].save(
        out,
        save_all=True,
        append_images=quantised[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    size = out.stat().st_size
    print(f"{out.relative_to(ROOT)}: {len(frames)} frames, {size / 1024:.0f} KB")
    if size > SIZE_LIMIT:
        raise SystemExit(f"{out.name} is over {SIZE_LIMIT // 1024} KB; try a smaller --width")


def main_() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=900)
    parser.add_argument("--colours", type=int, default=64)
    parser.add_argument("--out", type=Path, default=IMAGES / "usage-ramp.gif")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _serve()
    _capture(args.width, args.colours, args.out)


if __name__ == "__main__":
    main_()
