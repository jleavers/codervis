"""Render a LinkedIn header for codervis: the copy on the left, the dashboard on the right.

``docs/images/linkedin-header.png`` is the 1920x1080 header for a LinkedIn post about the
project, in the same layout as issuebot's. The page is ``linkedin-header.html`` beside this
file. It holds the copy and every layout number, and links ``app/static/style.css`` for the
palette, as ``social-preview.html`` does.

The picture in its window is the real app, served by ``capture.py``'s stubs: the same template,
SSE loop and ``app.js``, with the two quota clients and two activity readers swapped out before
the lifespan starts, and the data directories pointed at an empty temporary directory. The five
percentages are invented in ``STAGE`` below, and no credential file is read to make any of it.
The chip dots are coloured by ``colorFor()`` from ``app/static/app.js``, which this script
injects using ``social_preview.py``'s extraction, so there is still one colour ramp.

It runs in this tool's own venv, the one ``README.md`` beside this file sets up for
``capture.py``, and needs nothing that one does not already hold.
"""

from __future__ import annotations

import argparse
import base64
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PAGE = HERE / "linkedin-header.html"
DEFAULT_OUT = ROOT / "docs" / "images" / "linkedin-header.png"

#: The page is laid out at 1280x720 and rendered at 1.5x, giving the 1920x1080 LinkedIn
#: recommends for a post's cover image.
WIDTH, HEIGHT, SCALE = 1280, 720, 1.5
#: The width the dashboard is laid out at before the page scales it into the window: wide
#: enough for the two provider cards to sit side by side with the meters long.
DASHBOARD_WIDTH = 1100
#: Not a LinkedIn limit: the size this file is asked to stay under so the repository stays small.
SIZE_LIMIT = 1024 * 1024

#: (Claude 5-hour, Claude weekly, Claude Fable weekly, Codex 5-hour, Codex weekly), in percent,
#: in the order ``capture._publish()`` takes them. Invented, and chosen so the five meters
#: span the ramp from lime to coral. Nobody's usage.
STAGE = (41.3, 67.8, 22.5, 88.6, 54.2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linkedin_header.py", description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    # Imported here, so the module can be read and tested without the tool's venv, and because
    # importing `capture` is what points the environment at scratch and swaps the app's clients
    # for stubs. Both run as scripts from this directory, which puts it on `sys.path`.
    from playwright.sync_api import sync_playwright

    import capture
    import social_preview

    out: Path = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    capture._serve()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            header = browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=SCALE
            )
            # Assigned to `window` explicitly: Playwright does not run an init script at the
            # page's global scope, so a bare declaration would never reach the page.
            header.add_init_script(
                f"window.colorFor = (() => {{\n{social_preview.color_for_source()}return colorFor;\n}})();"
            )
            errors: list[str] = []
            header.on("pageerror", lambda error: errors.append(str(error)))
            header.goto(PAGE.as_uri(), wait_until="load")
            view = header.evaluate(
                """() => ({
                    width: document.getElementById("shot").getBoundingClientRect().width,
                    height: document.querySelector(".window-view").getBoundingClientRect().height,
                })"""
            )

            # The dashboard, laid out tall enough that once scaled into the window it fills it
            # to the bottom edge, as a browser's own viewport would.
            dashboard = browser.new_page(
                viewport={
                    "width": DASHBOARD_WIDTH,
                    "height": math.ceil(view["height"] * DASHBOARD_WIDTH / view["width"]),
                },
                device_scale_factor=2,
            )
            capture._publish(STAGE, datetime.now(timezone.utc))
            dashboard.goto(f"http://127.0.0.1:{capture.PORT}/", wait_until="networkidle")
            # Wait for the stage to have arrived over SSE, then for the meters' own 800 ms fill
            # transition to settle, so nothing is caught mid-slide.
            dashboard.wait_for_function(
                "expected => document.querySelector('#gauge-claude-five_hour .pct').textContent === expected",
                arg=f"{STAGE[0]:.1f}",
                timeout=10_000,
            )
            dashboard.wait_for_timeout(1000)
            shot = base64.b64encode(dashboard.screenshot()).decode("ascii")

            header.evaluate(
                """async src => {
                    const img = document.getElementById("shot");
                    img.src = src;
                    await img.decode();
                    await document.fonts.ready;
                }""",
                f"data:image/png;base64,{shot}",
            )
            if errors:
                print(f"linkedin_header: the page failed: {errors[0]}", file=sys.stderr)
                return 1
            header.screenshot(path=str(out))
        finally:
            browser.close()

    size = out.stat().st_size
    where = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"{where}: {round(WIDTH * SCALE)}x{round(HEIGHT * SCALE)}, {size / 1024:.0f} KB")
    if size > SIZE_LIMIT:
        print(f"linkedin_header: over {SIZE_LIMIT // 1024} KB", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
