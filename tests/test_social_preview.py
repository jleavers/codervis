"""The social preview tool draws with the dashboard's own parts, and fetches nothing.

`tools/screenshots/social_preview.py` renders `docs/images/social-preview.png` from
`tools/screenshots/social-preview.html`, which links the dashboard's stylesheet, and injects
`colorFor()` read out of `app/static/app.js`. The tool itself only runs when someone regenerates
the image, which is rare, so these pin the parts of it that a change elsewhere could break
quietly: that the colour function can still be found where the script looks, that the page has
not grown a second copy of the ramp, and that it reaches nothing outside this tree.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "screenshots" / "social_preview.py"
PAGE = ROOT / "tools" / "screenshots" / "social-preview.html"


def _tool():
    """The script as a module. It imports Playwright only inside `main()`, so this needs none."""
    spec = importlib.util.spec_from_file_location("social_preview", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_colour_function_is_found_where_the_tool_looks_for_it() -> None:
    """A reformatted `colorFor` fails here, in CI, not on the day the image is regenerated.

    What is extracted has to be the whole function and only it: the declaration the page calls,
    the `{ hue, sat, lit }` it destructures, and braces that close where the function does.
    """
    source = _tool().color_for_source()
    assert source.startswith("function colorFor(percent) {\n"), source[:80]
    assert source.rstrip().endswith("}"), source[-80:]
    assert source.count("{") == source.count("}"), "the extraction cut the function short or ran past it"
    assert "return { hue, sat, lit };" in source, (
        "colorFor() no longer returns the {hue, sat, lit} the page and app.js both read"
    )


def test_the_page_keeps_no_copy_of_the_colour_ramp() -> None:
    """One ramp, in app.js. A copy here would go on drawing the old curve after a change."""
    page = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"function\s+colorFor\b|colorFor\s*=", page), (
        "social-preview.html defines colorFor itself instead of using the dashboard's"
    )


def test_the_page_loads_nothing_from_the_network() -> None:
    """Every `src` and `href` is a relative path to a file in this tree, and there is no other way in.

    The page is rendered on the host that holds both live tokens, and the picture has no reason
    to reach anywhere: what it draws is this repository's stylesheet and its own invented values.
    """
    page = PAGE.read_text(encoding="utf-8")
    references = re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", page)
    assert references == ["../../app/static/style.css"], references
    for reference in references:
        assert (PAGE.parent / reference).resolve().is_file(), f"{reference} does not exist"
    for route_out in (r"https?:", r"""["'(]\s*//""", r"@import", r"\burl\s*\(", r"\bfetch\s*\(", r"\bimport\s*\("):
        assert not re.search(route_out, page), f"social-preview.html matches {route_out!r}"
