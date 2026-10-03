"""Render the repository's social preview from the dashboard's own styles and colour ramp.

``docs/images/social-preview.png`` is the 1280x640 card GitHub shows when the repository is
linked. It is uploaded by hand under Settings, General, Social preview, because GitHub offers
no other way to set it; this script is how the file is made, so that it can be made again.

The page is ``social-preview.html`` beside this file. It links ``app/static/style.css`` for the
palette, the wordmark and the meters, and this script injects ``colorFor()`` from
``app/static/app.js`` before the page runs, so each meter is exactly the colour the dashboard
would draw at that percentage, and a change to either file shows up here on the next render.
The four percentages are invented in the page. Nothing is anyone's usage, no credential file is
read, the app is not served, and the page loads nothing from the network.

It runs in this tool's own venv, the one ``README.md`` beside this file sets up for
``capture.py``, and needs nothing that one does not already hold.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PAGE = HERE / "social-preview.html"
APP_JS = ROOT / "app" / "static" / "app.js"
DEFAULT_OUT = ROOT / "docs" / "images" / "social-preview.png"

#: GitHub's recommended size for a social preview; it accepts as little as 640x320.
WIDTH, HEIGHT = 1280, 640
#: GitHub refuses a social preview image over 1 MB.
GITHUB_LIMIT_BYTES = 1024 * 1024

#: `colorFor` as app.js declares it: a two-space-indented function inside the module's IIFE,
#: closed by a brace at the same indent. Matched rather than imported, because app.js runs
#: against a live page and an EventSource the moment it loads.
COLOR_FOR = re.compile(r"^  function colorFor\(percent\) \{\n.*?^  \}$", re.M | re.S)


def color_for_source(app_js: Path = APP_JS) -> str:
    """The dashboard's `colorFor()`, as a top-level function declaration."""
    match = COLOR_FOR.search(app_js.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(
            f"social_preview: {app_js.relative_to(ROOT)} no longer declares "
            "`  function colorFor(percent) {` where this script looks for it. Update "
            "COLOR_FOR rather than copying the function here: a copy is a second colour ramp."
        )
    return textwrap.dedent(match.group(0)) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="social_preview.py", description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    # Imported here, so the module can be read and its helpers tested without the tool's venv.
    from playwright.sync_api import sync_playwright

    out: Path = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
            )
            # Assigned to `window` explicitly: Playwright does not run an init script at the
            # page's global scope, so a bare declaration would never reach the page.
            page.add_init_script(
                f"window.colorFor = (() => {{\n{color_for_source()}return colorFor;\n}})();"
            )
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(PAGE.as_uri(), wait_until="load")
            page.evaluate("async () => { await document.fonts.ready; }")
            if errors:
                print(f"social_preview: the page failed: {errors[0]}", file=sys.stderr)
                return 1
            page.screenshot(path=str(out))
        finally:
            browser.close()

    size = out.stat().st_size
    where = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"{where}: {WIDTH}x{HEIGHT}, {size / 1024:.0f} KB")
    if size > GITHUB_LIMIT_BYTES:
        print(f"social_preview: over GitHub's {GITHUB_LIMIT_BYTES // 1024} KB limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
