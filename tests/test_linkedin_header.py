"""The LinkedIn header tool draws with the dashboard's own parts, and fetches nothing.

`tools/screenshots/linkedin_header.py` renders `docs/images/linkedin-header.png` from
`tools/screenshots/linkedin-header.html`, with a screenshot of the app served by `capture.py`'s
stubs in its window, and injects `colorFor()` through `social_preview.py`'s extraction. Like the
social preview, it only runs when someone regenerates the image, so these pin the parts a change
elsewhere could break quietly: the values it hands `capture`, the elements it measures on the
page, that neither the page nor the tool keeps a second colour ramp, and that the page reaches
nothing outside this tree. That extraction is itself pinned by `tests/test_social_preview.py`.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "screenshots"
TOOL = TOOLS / "linkedin_header.py"
PAGE = TOOLS / "linkedin-header.html"
CAPTURE = TOOLS / "capture.py"


def _tool():
    """The script as a module. It imports Playwright and `capture` only inside `main()`."""
    spec = importlib.util.spec_from_file_location("linkedin_header", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loading_the_tool_does_not_import_capture() -> None:
    """Importing `capture` repoints the data directories and swaps the app's clients for stubs.

    That is what the tool wants when it runs and what nothing else in a test session may have,
    so it has to stay inside `main()`.
    """
    already = {name for name in ("capture", "social_preview") if name in sys.modules}
    _tool()
    assert {name for name in ("capture", "social_preview") if name in sys.modules} == already


def test_the_stage_is_the_shape_capture_publishes() -> None:
    """`capture._publish()` unpacks five percentages; the tool's `STAGE` must be five, in range.

    Read from `capture.py`'s source rather than imported, for the reason the test above gives.
    """
    tree = ast.parse(CAPTURE.read_text(encoding="utf-8"))
    stages = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "STAGES" for target in node.targets)
    )
    stage = _tool().STAGE
    assert len(stage) == len(stages[0][0]), (len(stage), len(stages[0][0]))
    assert all(isinstance(p, float) and 0 <= p <= 100 for p in stage), stage


def test_the_elements_the_tool_measures_are_on_the_page() -> None:
    """The script sizes the dashboard from `#shot`'s width and `.window-view`'s height."""
    page = PAGE.read_text(encoding="utf-8")
    assert re.search(r'<img id="shot"', page), "linkedin-header.html has no <img id=\"shot\">"
    assert re.search(r'class="window-view"', page), "linkedin-header.html has no .window-view"
    tool = TOOL.read_text(encoding="utf-8")
    assert 'getElementById("shot")' in tool and 'querySelector(".window-view")' in tool


def test_neither_the_page_nor_the_tool_keeps_a_copy_of_the_colour_ramp() -> None:
    """One ramp, in app.js, found by one extraction, in `social_preview.py`."""
    page = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"function\s+colorFor\b|colorFor\s*=", page), (
        "linkedin-header.html defines colorFor itself instead of using the dashboard's"
    )
    tool = TOOL.read_text(encoding="utf-8")
    assert "social_preview.color_for_source()" in tool
    assert not re.search(r"function\s+colorFor|re\.compile", tool), (
        "linkedin_header.py looks for colorFor itself; use social_preview.color_for_source()"
    )


def test_the_page_loads_nothing_from_the_network() -> None:
    """Every `src` and `href` is a relative path to a file in this tree, and there is no other way in.

    The page is rendered on the host that holds both live tokens, and the picture has no reason
    to reach anywhere: what it draws is this repository's stylesheet, its own copy, and a
    screenshot the script hands it as a data URL.
    """
    page = PAGE.read_text(encoding="utf-8")
    references = re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", page)
    assert references == ["../../app/static/style.css"], references
    for reference in references:
        assert (PAGE.parent / reference).resolve().is_file(), f"{reference} does not exist"
    for route_out in (r"https?:", r"""["'(]\s*//""", r"@import", r"\burl\s*\(", r"\bfetch\s*\(", r"\bimport\s*\("):
        assert not re.search(route_out, page), f"linkedin-header.html matches {route_out!r}"
