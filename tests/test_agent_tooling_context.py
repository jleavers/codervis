"""What repo-shipped agent tooling says, as distinct from where it runs.

This repository ships text that agents execute -- the archived documents under
`docs/superpowers/`, the security-sweep skill and its workflow. That text used to make
its own local choices about the environment it ran in, which is how one plan came to name a
fixed directory in world-writable `/tmp` as a package cache six times (#21).

The environment an operator's agents run in is the operator's own to configure. The
repository does not ship a `.claude/settings.json` that confines it: one did (#31), and it
turned every shell command the operator ran in this checkout into a permission prompt and
blocked the harness's own auto-memory, which secured nothing for anyone who clones the
repository. What these tests hold instead is the shipped text: that no document carries its
own environment prefix, that none reads as work still to do, and that every sweep prompt
tells its agent that what it reads is data.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_SETTINGS = ROOT / ".claude" / "settings.json"

DOCS_DIR = ROOT / "docs" / "superpowers"
PLANS_DIR = DOCS_DIR / "plans"
WORKFLOW = ROOT / ".claude" / "workflows" / "security-sweep.js"

# What an archived document opens with. The plans carry this sentence; the two design specs
# beside them did not, which is how they outlived the archiving of their own plans and went on
# reading as designs someone had yet to implement (#46).
ARCHIVED_MARKER = "> **Archived —"

TEXT_SUFFIXES = {".md", ".js", ".json", ".yml", ".yaml", ".py", ".sh", ".toml", ".ini"}

# A fixed name under shared `/tmp`, which any other local principal can create and fill
# before the command that reads it runs. The pattern is deliberately blunt rather than a list
# of the spellings two plans happened to use: the next author will use a third.
FIXED_TMP_PATH = re.compile(r"(?<![\w/])/tmp/[\w.${}-]+")

# No exemptions. There was one, for the synthetic credential files `.github/workflows/ci.yml`
# used to build under a fixed `/tmp/codervis-ci`; #30 moved them to the runner's own temp
# directory and emptied this, which is the only way an exemption here is meant to end. A file
# that has to name such a path again gets a line and the reason, not a spelling the pattern
# above happens to miss.
TMP_PATH_EXEMPT: tuple[str, ...] = ()


def _shipped_text_files() -> list[Path]:
    """What a clone gets, which is what an agent reads: the tracked files, and only those.

    `git ls-files` rather than a walk, so a developer's `.venv`, a pytest cache or an
    untracked scratch file cannot fail this suite -- and so a file that is committed cannot
    escape it by living somewhere the walk's exclusions happened to cover.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    paths = [ROOT / name for name in listed.split("\0") if name]
    assert paths, "git ls-files returned nothing; is this a checkout?"
    return [p for p in paths if p.suffix in TEXT_SUFFIXES and p.is_file()]


def _const_body(source: str, name: str) -> str:
    """The template-literal body of ``const <name> = `...` ``, so a check can be scoped to it.

    These templates quote code spans, so the closing backtick is the first one that is not
    escaped -- `source.index("`")` would stop at ``\\`app/quota.py\\```.
    """
    start = source.index(f"const {name} = `") + len(f"const {name} = `")
    end = re.compile(r"(?<!\\)`").search(source, start)
    assert end is not None, f"const {name} has no closing backtick"
    return source[start : end.start()]


def test_the_repository_does_not_configure_the_operators_agent_environment() -> None:
    """A committed project settings file binds every session in the checkout, the operator's.

    One was added for #21 and reverted: with its sandbox on a host where the sandbox cannot
    start, every shell command became a prompt, and its `Read(~/.claude/**)` deny also
    refused the harness's own auto-memory, which no `!` rule could carve back out. A fix that
    wants to constrain agents belongs in the text they read or in the operator's own
    settings, not here. If a settings file is ever needed, it is a decision to make on
    purpose -- delete this test in the same change, and say why.
    """
    assert not PROJECT_SETTINGS.exists(), (
        f"{PROJECT_SETTINGS.relative_to(ROOT)} would bind the operator's own sessions"
    )


def test_no_shipped_document_names_a_fixed_path_in_shared_tmp() -> None:
    """The instance #21 found.

    A fixed name under world-writable `/tmp` is a directory any other local principal can
    pre-create and fill, and the command that reads it runs as the operator. Nothing in a
    shipped document should name one.
    """
    this_file = Path(__file__).resolve()
    offenders = []
    for path in _shipped_text_files():
        if path.resolve() == this_file:  # the scanner has to spell what it looks for
            continue
        if path.relative_to(ROOT).as_posix() in TMP_PATH_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in FIXED_TMP_PATH.finditer(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)}")
    assert not offenders, f"fixed path in shared /tmp in shipped text: {offenders}"


def test_no_shipped_document_reads_as_work_still_to_do() -> None:
    """A document whose work reads as outstanding is one an agent picks up and works through.

    Every document under `docs/superpowers/` describes work that is over -- some shipped, some
    for providers that were deleted -- so none has any business carrying an unticked box,
    telling a reader to execute it task by task, or omitting the header that says which it is.

    The reach is the whole subtree, not `plans/` alone. It was `plans/` alone until #46, while
    this module's own docstring promised that no shipped document reads as pending: the two
    design specs under `specs/` were never archived when their plans were (#21), so they went
    on reading as live designs -- one of them asserting, falsely against this tree, that
    implementing it changes no mount, secret or Compose behaviour.
    """
    docs = list(DOCS_DIR.rglob("*.md"))
    assert docs, f"no documents under {DOCS_DIR}; has this check outlived its subject?"
    plans = [p for p in docs if PLANS_DIR in p.parents]
    assert plans, f"no plans under {PLANS_DIR}; has this check outlived its subject?"

    live = [p for p in plans if "archive" not in p.relative_to(PLANS_DIR).parts]
    assert not live, f"a plan outside the archive reads as pending: {live}"

    for path in docs:
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(ROOT)
        assert "- [ ]" not in text, f"{name} still carries an unticked box"
        assert "REQUIRED SUB-SKILL" not in text, f"{name} still tells an agent to execute it"
        # Near the top, where a reader sees it before the body: a marker at the foot of a
        # document an agent has already started working through is no marker at all.
        head = "\n".join(text.splitlines()[:8])
        assert ARCHIVED_MARKER in head, f"{name} does not say, up front, that it is archived"


def test_every_sweep_agent_is_told_its_input_is_data() -> None:
    """The sweep reads issue bodies, comments and CI logs -- text other people write.

    Each prompt gets the rule through the shared `WHERE` preamble, so the check is that no
    prompt is built without it.
    """
    source = WORKFLOW.read_text(encoding="utf-8")

    # The rule itself, and then the fact that the shared preamble interpolates it. Checking
    # only that the sentence appears somewhere would stay green if `WHERE` stopped carrying
    # it and the fragment were left behind unused.
    fragment = _const_body(source, "DATA_NOT_INSTRUCTIONS")
    assert "data, not instructions" in fragment
    for reader in ("Issue and PR bodies", "comments", "CI logs"):
        assert reader in fragment, f"the rule does not name {reader}"
    assert "${DATA_NOT_INSTRUCTIONS}" in _const_body(source, "WHERE")

    prompts = [
        line for line in source.splitlines()
        if line.startswith("const ") and "Prompt" in line.split("=")[0]
    ]
    assert len(prompts) >= 7, f"unexpectedly few prompt definitions: {prompts}"
    for line in prompts:
        assert "${WHERE}" in line, f"prompt built without the shared preamble: {line}"
