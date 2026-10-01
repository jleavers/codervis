"""Third-party code reaches this project by content hash, not by name and version range.

It is #104 for the build and a contributor's venv, and #108 for the one path that was out of
that issue's scope.

`requirements.txt` used to pin five packages and let pip resolve the other seventeen afresh on
every uncached build, with no hashes and no upper bound on `starlette`; `requirements-dev.txt`
gave ranges for a set of thirty-odd that a contributor runs as themselves, on the host that
holds both live tokens; and the base image was `python:3.14-slim` by tag, which is a label
upstream can move. Each of those is an upstream compromise away from import-time code in the
codervis process, which holds both bearer tokens and can read both mounted home trees.

So every artefact is fixed by content. What is pinned here is the *shape* that makes that
true, as allow-lists written in this file: which requirement files exist, what a line in a
lock may be, and what the `Dockerfile` may pass to pip. A check that looked for `--hash`
somewhere in the file, or for the absence of one bad flag, would pass over the next line
somebody adds -- which is what `AGENTS.md` means by naming what a file may carry rather than
what it may not.

#108 is the same argument one step further out, and it is why this file now reads "this
project" rather than "the build". The screenshot tool's `playwright` and `pillow` were taken by
bare name from a command a maintainer runs as themselves, on the host that holds both live
tokens and with nothing bounding where the import-time code of either can connect. That is a
rarer command and a friendlier principal than `docker compose up --build`, and an identical
shape, so it is a third input and a third lock rather than an exemption -- and every rule below
is written over `INPUTS` and `LOCKS`, so it arrived already covered.

What is *not* pinned here, because it cannot be witnessed without an index: that each lock is
complete. `pip install --require-hashes` is what establishes that, by refusing to install a
dependency the file does not name, and the `Dockerfile` line below is where it runs.

What is not pinned here at all, and is not an oversight: the three browser archives
`playwright install chromium` downloads after that lock is installed. Nothing upstream
publishes a digest for them, so there is no hash to require; `tools/screenshots/README.md` says
so in as many words rather than letting the silence read as coverage.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

#: `cwd` decides which repository the one git call below reads, so nothing in the environment
#: may: an exported `GIT_DIR` or `GIT_WORK_TREE` would point it at another checkout.
_GIT_ENV = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}

#: The six requirement files this repository has, and the relation between them: an input
#: names packages, its lock fixes them. A seventh file is a second place a version is decided.
#:
#: Three pairs, not two, since #108: `requirements-screenshots.in` names what
#: `tools/screenshots/capture.py` needs. That set was the last third-party code here taken by
#: bare name -- `uv run --with playwright --with pillow`, resolved afresh on every invocation,
#: on the host that holds both live tokens -- and #104 left it out of scope rather than fixing
#: it. It is a lock like the other two now, and every rule in this file reaches it because each
#: one is written over `INPUTS` and `LOCKS` rather than over two names.
INPUTS = {
    "requirements.in": "requirements.txt",
    "requirements-dev.in": "requirements-dev.txt",
    "requirements-screenshots.in": "requirements-screenshots.txt",
}
LOCKS = frozenset(INPUTS.values())

#: The flags the image's install may pass, and no others. `--require-hashes` is the one that
#: matters and the rest are stated so that, say, `--index-url` or `--trusted-host` arriving
#: beside it is a failure rather than a silent second source of code.
PERMITTED_PIP_ARGUMENTS = ("install", "--no-cache-dir", "--require-hashes", "-r")

#: Every tracked file whose installs of a lock must require hashes. Named here rather than
#: discovered, so that a *new* file telling somebody to install a lock is a failure until it is
#: added on purpose: the flag being on the image's install alone was the gap, not the image.
#: `tools/screenshots/README.md` joined this list in #108, by acquiring a lock to install.
INSTALL_SITES = frozenset(
    {
        "Dockerfile",
        "tools/screenshots/README.md",
        ".github/workflows/ci.yml",
        "README.md",
        "CONTRIBUTING.md",
        "CLAUDE.md",
        "AGENTS.md",
    }
)

#: Files that install a lock and are *not* held to the flag, each for a reason written here.
#: An exemption rather than a silent miss: the scan below finds these, so dropping one from this
#: list turns the pin red rather than quietly widening it.
#:
#: - The archived plans under `docs/superpowers/plans/archive/` are the record of work that is
#:   over, which `tests/test_agent_tooling_context.py` is what keeps true. Rewriting a finished
#:   plan's command would falsify the record rather than fix anything anyone runs.
#:
#: `tools/screenshots/README.md` was the third entry until #108 and is now in `INSTALL_SITES`
#: above, which is the whole of what that issue asked for on this side: the exemption was never
#: "this install is safe", it was "the packages it takes have nowhere to be pinned yet".
#:
#: Exempt from the *flag* is not invisible: `EXPECTED_UNFLAGGED_INSTALLS` below counts what each
#: of these carries, so one more cannot arrive unremarked.
EXEMPT_INSTALL_SITES = frozenset(
    {
        "docs/superpowers/plans/archive/2026-06-08-agy-1.0.6-compatibility.md",
        "docs/superpowers/plans/archive/2026-06-08-browser-widget-toggles.md",
    }
)

#: `tests/test_negative_controls.py` carries de-flagged installs as mutation *data* -- that is
#: how the controls prove these pins bite -- so it is not held to the flag either.
MUTATION_SOURCE = "tests/test_negative_controls.py"

#: How many installs of a lock each file outside `INSTALL_SITES` carries *without* the flag.
#: The exemptions above are whole-file, which on its own would let a genuinely dangerous install
#: be added to an exempt file and seen by nothing. This is what stops that: a new unhashed
#: install anywhere here fails until somebody changes a number and says why in the same change.
#: A count rather than a pattern, because none of these is a command this project asks anyone to
#: run -- what matters is that one cannot arrive quietly.
#:
#: `MUTATION_SOURCE` went from 2 to 4 in #110, in two steps worth keeping apart.
#: **2 to 3** is the fix: a `uv run --with-requirements` install was already in the file and
#: `commands()` could not see it, because its installer verb opened a string literal that was
#: glued to the assignment in front of it. So the 2 meant "every unhashed install in that file
#: whose verb the scan could reach", which is not what this table says it counts, and a de-flagged
#: install written that way -- verb opening a literal with syntax stuck to its front -- would not
#: have moved the number. A literal that opens its own token is *not* that shape: its quote is on
#: an end, so it was stripped and counted all along. **3 to 4** is that change's own widening
#: control, whose `after` carries a `pipenv install` -- mutation data in this same file, counted
#: like the rest of it.
#:
#: **4 to 6** in #113, and both are that shape again rather than a fix: the two widening controls
#: that change arrived with carry a `pipenv install` apiece, one naming its lock as a quoted
#: option value and one reached through a hyphenated dict key. Unlike #110, the widened scan
#: found no install already in the tree that it had been missing -- measured over the whole
#: tracked tree, the two plans below stayed at 5 and every other file at what it had. That is
#: worth stating rather than leaving to a reader to infer from an unchanged number: the blast
#: radius #113 reported was nil, so the two counts that moved are the controls' own, and a tree
#: whose numbers did not move is the evidence that nothing was quietly recategorised.
EXPECTED_UNFLAGGED_INSTALLS = {
    "docs/superpowers/plans/archive/2026-06-08-agy-1.0.6-compatibility.md": 5,
    "docs/superpowers/plans/archive/2026-06-08-browser-widget-toggles.md": 5,
    MUTATION_SOURCE: 6,
}

#: How an install of a lock may be *written*. Named forms rather than one pattern, because the
#: pin is exactly as wide as this list: `pip3`, `--requirement`, `-rfile` and `uv run
#: --with-requirements` each install a lock, and a regex written for `pip install ... -r file`
#: sees none of them. Keep this list ahead of what the tree contains, not level with it.
INSTALLERS = (
    ("pip", "install"),
    ("pip3", "install"),
    ("pip", "download"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("uv", "pip", "install"),
    ("uv", "run"),
    ("uv", "add"),
    ("pipenv", "install"),
    ("poetry", "add"),
)
#: A `python3.13 -m pip install` needs no entry of its own: the scan looks for an installer at
#: *every* token offset, so the `("pip", "install")` inside it matches.

#: The options that name a requirements file, in any of the spellings above.
REQUIREMENT_OPTIONS = ("-r", "--requirement", "--with-requirements")

#: What ends one command and begins the next. Without these, a hashed install chained by `&&` to
#: an unhashed one reads as a single command, and the flag on the first is credited to the
#: second. That is not hypothetical: it passed this pin's first draft with an unhashed install of
#: the dev lock documented in `README.md`. The lock names are left out of this comment on
#: purpose -- the scan reads this file too, and a real one here would be a finding about itself.
COMMAND_SEPARATORS = ("&&", "||", ";", "|")

#: The Python syntax a command written inside a string literal can be glued to, when the
#: installer verb is the literal's *first* token: `after="uv run …`, `after=("uv run …`,
#: `command=[rf'''uv run …`, `run(shlex.split("uv run …`. Stripping quotes off the *ends* of
#: the token does not reach that quote -- `after="uv` begins with `a`, so the opening quote is
#: on neither end -- and the scan looks for an installer at every token offset but never
#: inside one, so the verb went unseen entirely (#110).
#:
#: Matched as *glue and then a quote*, so that only syntax can be cut. What sits immediately
#: before the quote is one of ``= ( [ { , : +``, or a string prefix directly after one of them
#: (``after=f"uv run …``); ahead of that glue may run a chain of the same characters, their
#: closing halves, and the identifiers and quotes between them. The chain is greedy, so a token
#: holding several quotes is cut at the last glue-then-quote its own class can reach -- which is
#: what makes ``{"cmd":"uv run …`` reach the verb rather than stopping at `cmd`. What the chain
#: may not *begin* with is a `-`; see below.
#:
#: The glue is itself optional, so a literal opening the token is reached through its prefix
#: alone: ``f"pip install …`` is one Python string literal opening a command like any other. A
#: literal opening the token with *no* prefix needs nothing here -- its quote is on an end, which
#: is where `TOKEN_EDGES` already reached.
#:
#: What the glue requirement buys is that an apostrophe inside a word (``README's``, ``don't``)
#: and a lock named in prose (``requirements-dev.txt's hashes``) are left whole, since the
#: character before such a quote is an ordinary letter and not syntax. Cutting there would take
#: the rest of the token with it -- a lock's own name among it -- and so narrow this scan in
#: exchange for widening it. `test_a_word_holding_an_apostrophe_is_not_cut_at_it` is where that is
#: stated, on the tokens: `test_a_lock_named_in_prose_is_not_an_install` cannot see it, because a
#: line with no installer in it answers `[]` however the token was cut.
#:
#: That rule has a residue rather than no exception, and it is one or two letters wide: a word made
#: of nothing but prefix letters is read as a prefix, so ``F's``, ``rb's`` and ``ur's`` still cut to
#: `s`. `{0,2}` shortened the class from any such run -- ``Ruff's`` was in it -- rather than
#: emptying it. The only tokens of that shape in the tracked tree are the three examples on this
#: line, which name no installer and no lock; every other apostrophe the cut reaches belongs to a
#: real byte or f-string literal, where taking the prefix off is the right answer.
#:
#: The prefix is `{0,2}` and not `*` because two is as long as a string prefix gets: `r`, `b`,
#: `u`, `f` and `t` -- the last of those PEP 750's, so it needs the 3.14 the `Dockerfile` pins --
#: in at most two letters. Which *pairs* Python actually accepts is not checked, since `bf"…"` is
#: not a command either way; the bound is on the length, and that is what does the work. Left
#: unbounded, the class matched a *word* built only from those letters however long, so ``Ruff's``
#: cut to `s`: a run of prefix letters is not a prefix, and the residue above is what is left.
#:
#: `-` is in the chain, and the reason it was once in neither class is now stated where it
#: actually bites: `(?!-)`, which refuses a token that *begins* with one. That is the whole of
#: what the exclusion was protecting (#113). A token beginning `-` is an *option*, and
#: `named_target()` matches on the option's own name, so a cut that reached into one would hand
#: back a value with no option in front of it -- an install that finds its verb and loses its
#: lock. A `-` anywhere else in the chain is part of a name -- a dict key, a subscript, and in
#: this tree most often an HTML or CSS attribute (`aria-label="…`, `[data-state="…`) -- where no
#: option name can live, and stopping the chain there cost the literal behind it. The lookahead is not a second rule
#: smuggled in beside the widening: no token beginning `-` could match this pattern before
#: either -- the chain could not start with one, `-` is not a string prefix, and it is not a
#: quote -- so it states the old bound exactly and bounds the new reach to it.
#: `test_an_option_token_is_never_cut_into` is where that is pinned, on the tokens.
#:
#: A chain containing a closing bracket (`steps[0]="pip install …`) was a third such shape, for a
#: different reason -- the class held `(`, `[` and `{` but not their closing halves, so the chain
#: could not cross the `]` and the token did not match at all. Fixed in #110 by putting
#: `)`, `]` and `}` in the chain: a subscripted assignment is assignment syntax like any other.
#:
#: What the pattern still does not read is a literal reached through an operator the glue class
#: does not name: ``a*"pip install …`` and ``x-"pip install …`` are both unread, the second
#: because `-` is in the chain but not in the glue the chain has to end on. That is a bound and
#: not a noted gap -- the list of what is claimed is `COMMAND_SPELLINGS`, and a spelling that is
#: not in it is claimed nowhere. A call's *result* is not an example of it: ``fmt(verb)("pip
#: install …`` is read, since `fmt(verb)(` is identifiers and brackets ending on a `(`.
PYTHON_STRING_OPENS_A_COMMAND = re.compile(
    r"""^(?!-)(?:[\w.,+=(\[{:'")\]}-]*[,+=(\[{:])?(?i:[rbuft]){0,2}(?P<command>['"]{1,3}.+)$"""
)

#: What comes off the ends of a token. Brackets and braces sit here beside the quotes because
#: the syntax the pattern above cuts off the *front* of a command has a closing half on its
#: last token -- `command=["uv run … requirements.txt"]` ends in ``.txt"]`` -- and leaving that
#: on would find the verb and lose the lock, which reads as "no install" exactly as the whole
#: miss did. Measured against the tracked tree: adding them moves no count there today, so this
#: is the claim in `commands()` being made true rather than a number being changed.
TOKEN_EDGES = "`\"'.,;:()[]{}"

#: The options an install of a lock may carry, and no others -- the point `PERMITTED_PIP_ARGUMENTS`
#: makes for the image, made once more for every documented install. `--require-hashes` is
#: required on top of this; what the allow-list adds is that `--index-url`, `--extra-index-url`
#: or `--trusted-host` arriving beside it is a failure, since a documented command that points a
#: contributor's install at another index is the same defect as losing the hashes.
PERMITTED_INSTALL_OPTIONS = frozenset({"--require-hashes", "--no-cache-dir"})

#: `name:tag@sha256:...`. The tag is kept for a reader and for Dependabot to follow; the
#: digest is what the engine actually resolves.
BASE_IMAGE = re.compile(r"^FROM (?P<name>[a-z0-9._/-]+):(?P<tag>[\w.-]+)@sha256:[0-9a-f]{64}$")

#: A locked requirement: exactly one `==`, optionally under an environment marker, continued
#: onto its hash lines.
LOCKED = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9,._-]+\])?"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+*-]*)"
    r"(?: ; (?P<marker>[^\\]+?))?"
    r" \\$"
)
HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}( \\)?$")

#: A package named by an input: a name, optional extras, and nothing that decides a version.
ASKED_FOR = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._-]+\])?$")


def canonical(name: str) -> str:
    """PEP 503: the one spelling `fastapi`, `FastAPI` and `typing_extensions` each have."""
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_lines(lock: str) -> list[str]:
    return (ROOT / lock).read_text(encoding="utf-8").splitlines()


class Pin(NamedTuple):
    """One locked requirement. The marker is part of the pin, not decoration: a package under
    `sys_platform != 'win32'` in one lock and unconditional in the other is a different
    *set* on the same machine, which a version-and-hash comparison passes over."""

    version: str
    marker: str | None
    hashes: tuple[str, ...]


def locked_packages(lock: str) -> dict[str, Pin]:
    """`{name: Pin}` for one lock, read by walking its lines in order."""
    packages: dict[str, Pin] = {}
    current: str | None = None
    hashes: list[str] = []
    for line in lock_lines(lock):
        match = LOCKED.match(line)
        if match:
            if current is not None:
                packages[current] = packages[current]._replace(hashes=tuple(hashes))
            current = canonical(match.group("name"))
            assert current not in packages, f"{current} is pinned twice in {lock}"
            packages[current] = Pin(match.group("version"), match.group("marker"), ())
            hashes = []
        elif HASH.match(line.strip()) and current is not None:
            hashes.append(line.strip().removeprefix("--hash=").removesuffix(" \\"))
    if current is not None:
        packages[current] = packages[current]._replace(hashes=tuple(hashes))
    return packages


def asked_for(source: str) -> set[str]:
    """The packages an input names, dropping its one `-r` line and its comments."""
    names = set()
    for raw in (ROOT / source).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        match = ASKED_FOR.match(line)
        assert match, f"{source} names {line!r}, which decides more than a package"
        names.add(canonical(match.group("name")))
    return names


def dockerfile_lines() -> list[str]:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()


# ─── the files ───────────────────────────────────────────────────────────────


#: What a requirement file is called, wherever in the tree it sits. The *basename* is matched
#: and the whole tracked tree is searched, which is the part #108 changed: this read
#: `name.startswith("requirements")` against repository-root paths, and said in a comment that a
#: nested `tools/x/requirements.txt` was not one of this repository's requirement files. That
#: was true when it was written and it was still a blind spot -- the one set of third-party code
#: this project had not pinned was the one belonging to a subdirectory, and the file that would
#: have pinned it is exactly the file this function would not have found. So the allow-list
#: below now has something to be an allow-list over: a file *named* like a requirement file,
#: anywhere in a clone, is either one of the six named there or a failure. Named like one is the
#: width of it, and worth being exact about: a `tools/x/deps.txt`, or a `pyproject.toml` with a
#: dependency table, would ship an unpinned set that neither this check nor the install scan
#: below would see. Both are a convention this repository does not use, so what stands behind
#: them is review -- which is why this comment says so rather than implying the check is wider.
REQUIREMENT_FILE = re.compile(r"^requirements[A-Za-z0-9._-]*\.(in|txt)$")


def tracked_requirement_files() -> set[str]:
    """The requirement files a clone gets, from anywhere in it.

    `git ls-files` rather than a glob, for the reason the other file checks in this suite read
    it: a developer's scratch `requirements-local.txt` is not something this repository ships,
    and failing on one would be a pin that bites the wrong person.
    """
    return {
        name for name in tracked_files() if REQUIREMENT_FILE.match(PurePosixPath(name).name)
    }


def test_the_repository_has_exactly_these_requirement_files() -> None:
    """A seventh would be a set nothing installs with hashes required.

    Exactly, in both directions: an unnamed file fails, and so does a named one that has gone
    missing, since "the lock was deleted" and "the lock is fine" must not be the same result.
    """
    assert tracked_requirement_files() == set(INPUTS) | LOCKS


@pytest.mark.parametrize("source", sorted(INPUTS))
def test_an_input_names_packages_and_decides_no_version(source: str) -> None:
    """One place decides a version, and it is the lock.

    A range here would be a second, and the two drift: a bump Dependabot writes into the lock
    leaves the input saying something else, and the next regeneration undoes the bump.
    """
    assert asked_for(source), f"{source} names no package"


@pytest.mark.parametrize("source", ["requirements-dev.in", "requirements-screenshots.in"])
def test_every_other_input_builds_on_the_runtime_one(source: str) -> None:
    """So a contributor, and the tool that takes the README's picture, run the code the image
    runs rather than a second resolution of it.

    The screenshot tool is in here because `capture.py` imports `app.main` and serves it: a
    picture taken against a different `starlette` is a picture of a different dashboard.
    """
    text = (ROOT / source).read_text(encoding="utf-8")
    assert "-r requirements.in" in text


# ─── the locks ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("lock", sorted(LOCKS))
def test_every_line_of_a_lock_is_one_of_the_shapes_named_here(lock: str) -> None:
    """The allow-list, and the reason this file exists in the form it does.

    Named shapes rather than forbidden ones: `--index-url`, `--extra-index-url`,
    `--find-links`, `--trusted-host`, `-e` and a bare `-r` each turn a lock back into a name
    somebody else resolves, and a deny-list would be the ones whoever wrote it thought of.
    """
    for number, line in enumerate(lock_lines(lock), start=1):
        stripped = line.strip()
        permitted = (
            not stripped
            or stripped.startswith("#")
            or LOCKED.match(line) is not None
            or HASH.match(stripped) is not None
        )
        assert permitted, f"{lock}:{number} is not a pin, a hash or a comment: {line!r}"


@pytest.mark.parametrize("lock", sorted(LOCKS))
def test_every_requirement_in_a_lock_is_fixed_by_content(lock: str) -> None:
    packages = locked_packages(lock)
    assert packages, f"{lock} locks nothing"
    for name, pin in packages.items():
        assert pin.version, f"{lock} pins {name} to nothing"
        assert pin.hashes, (
            f"{lock} pins {name}=={pin.version} with no hash, so pip would take any file"
        )


@pytest.mark.parametrize("source,lock", sorted(INPUTS.items()))
def test_every_package_an_input_asks_for_is_locked(source: str, lock: str) -> None:
    missing = asked_for(source) - set(locked_packages(lock))
    assert not missing, f"{source} asks for {sorted(missing)}, which {lock} does not pin"


@pytest.mark.parametrize("source,lock", sorted(INPUTS.items()))
def test_a_lock_is_resolved_past_what_its_input_names(source: str, lock: str) -> None:
    """The transitive set is in the lock too, which is the whole point of locking it.

    Seventeen of the twenty-two packages in the image were named nowhere before #104, and
    those were the ones with no version decided at all.
    """
    assert set(locked_packages(lock)) > asked_for(source)


@pytest.mark.parametrize("lock", ["requirements-dev.txt", "requirements-screenshots.txt"])
def test_every_other_lock_agrees_with_the_runtime_lock_package_for_package(lock: str) -> None:
    """What the tests run against, and what the README's picture is taken against, is what the
    image runs -- artefact for artefact.

    Two independent resolutions would let CI pass against one `starlette` while the image
    installs another, which is the drift #20 is about arriving by a different route. Holding the
    third lock to it too is why its own header passes `--constraint requirements.txt`: a fresh
    resolve of the same input took a newer `fastapi` than the image has, which is precisely the
    drift, arriving through the one lock nobody would have thought to compare.
    """
    runtime = locked_packages("requirements.txt")
    other = locked_packages(lock)
    for name, pin in runtime.items():
        assert name in other, f"{name} is in the image and not in {lock}"
        assert other[name] == pin, f"{name} differs from the image: {other[name]} vs {pin}"


# ─── the image ───────────────────────────────────────────────────────────────


def test_the_image_installs_with_hashes_required() -> None:
    """Without `--require-hashes` the hashes in the file are decoration: pip would take
    whatever the index served for each pinned version."""
    installs = [line for line in dockerfile_lines() if line.startswith("RUN pip install")]
    assert len(installs) == 1, installs

    arguments = installs[0].removeprefix("RUN pip").split()
    assert arguments[-1] in LOCKS, f"the image installs {arguments[-1]}, which is not a lock"
    assert tuple(arguments[:-1]) == PERMITTED_PIP_ARGUMENTS, (
        "the image's install passes something this file does not name. Add it here on "
        f"purpose: {arguments[:-1]}"
    )


class Install(NamedTuple):
    """One install of a lock found in the tree: where it is, and what it passes."""

    path: str
    target: str
    options: frozenset[str]
    text: str


def tracked_files() -> list[str]:
    """Every path `git ls-files` reports, which is what a clone gets."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=_GIT_ENV,
    ).stdout
    return [entry for entry in listed.split("\0") if entry]


def commands(text: str) -> list[list[str]]:
    """The text as token lists, one per *command* rather than one per line.

    Backslash continuations are joined first, because a lock is routinely named on the line
    after the installer. A literal ``\\n`` is joined too, so that a command written inside a
    Python string -- which is how `tests/test_negative_controls.py` carries one -- tokenises as
    the command it is. Each line is then cut at `COMMAND_SEPARATORS`, so two installs chained on
    one line are two commands and neither is credited with the other's options.

    `TOKEN_EDGES` then comes off each token in one pass rather than in sequence, since
    `AGENTS.md` ends an install in ``.txt`.`` and the controls end one in ``.txt",``.
    Separators are replaced *before* that, because stripping would eat a bare ``;`` entirely
    and the cut would be lost.

    That pass is ends-only, which left the claim above true of every such command *except* the one
    whose literal is glued to syntax in front of it -- not simply the one whose verb opens the
    literal, since ``"pip3 install …"`` standing alone has its quote on an end and always
    tokenised fine. ``after="uv run …`` begins with `a`, so the opening quote was on neither end
    and the verb stayed glued to the assignment as ``after="uv``. An installer is looked for at
    every token offset but never *inside* one, so nothing matched at offset 0, and what followed
    depended on the command: where no shorter entry of `INSTALLERS` sits inside it the whole thing
    read as no install at all, and where one does -- ``after="python -m pip install …`` -- the
    install was found under the shorter verb instead (#110).
    `PYTHON_STRING_OPENS_A_COMMAND` is what cuts that --
    the syntax in front of the quote, and only syntax -- before the strip, which then takes the
    quote itself.

    So the claim holds where what precedes the literal is a chain of ``= ( [ { , : +``, the
    closing halves of those, identifiers, quotes and ``-``, and an optional string prefix --
    which is stated as a spelling apiece in `COMMAND_SPELLINGS` rather than left to this
    paragraph. It is deliberately *not* every way Python can reach a string, because "whatever
    syntax precedes it" would be the same defect as the claim it replaces -- wider than the code.
    The one character that chain may not begin with is ``-``, and that is a bound and not a gap:
    such a token is an *option*, `named_target()` finds a requirements file by the option's own
    name, and so a cut into one would find the verb and lose the lock (#113).

    The ends-only strip has its own residue one token further on, and it is not this function's
    to fix: a quoted option *value* keeps its opening quote, because that quote is in the middle
    of the token and the token is an option the cut above will not touch. `lock_named()` is where
    it comes off, and says so.
    """
    joined = text.replace("\\\n", " ").replace("\\n", " ")
    found: list[list[str]] = []
    for line in joined.splitlines():
        for separator in COMMAND_SEPARATORS:
            line = line.replace(separator, " \0 ")
        command: list[str] = []
        for token in line.split():
            if token == "\0":
                found.append(command)
                command = []
                continue
            if opened := PYTHON_STRING_OPENS_A_COMMAND.match(token):
                # A cut that leaves nothing behind has taken the whole token rather than the
                # syntax in front of a command, so it is not a cut (#113). `-` in the chain is
                # what made that reachable: the chain can now cross a hyphenated *lock* name to
                # a glue-then-quote behind it, and `["pip","install","-r","requirements-dev.txt",""]`
                # cut to `""]`, which strips to nothing -- an install the narrower chain found and
                # this one would have lost. `LOCK_TOKENS_LEFT_WHOLE` pins that direction.
                if opened.group("command").strip(TOKEN_EDGES):
                    token = opened.group("command")
            if stripped := token.strip(TOKEN_EDGES):
                command.append(stripped)
        found.append(command)
    return [command for command in found if command]


def lock_named(value: str) -> str:
    """The lock an option's value names: the basename, with `TOKEN_EDGES` off it.

    The strip is here as well as in `commands()` because that one is *ends-only* and an
    option's value is not on an end. `--requirement="requirements.txt"` loses its closing quote
    to that pass and keeps its opening one, which sits in the middle of the token; and the cut
    in `PYTHON_STRING_OPENS_A_COMMAND` deliberately never reaches a token beginning `-`, since
    that is an option and the option's own name is what the caller matches on. So before #113
    the quote survived both passes, a value with a quote still on its front was not a lock, and
    `pip install --requirement="…"` read as no install at all -- a command whose verb was found
    and whose lock was lost, which is the same silence as a verb nobody found. The lock's own
    name is left out of that example on purpose, as `COMMAND_SEPARATORS` leaves it out of its
    own: the scan reads this file, so a real one spelled here beside an installer verb would be
    an install of a lock in a file named in neither list -- a finding about itself.

    After the basename rather than before, so that the strip applies to the component that names
    the lock rather than to the whole value. The two orders agree wherever the punctuation is on
    an end of both, which is every shape above; they part where a quote sits *after* a directory
    separator, and a shell-quoted basename is the ordinary way to write one. For the value in
    `pip install -r "$DIR"/"<lock>"` -- `$DIR"/"<lock>` by the time this is called -- taking the
    basename first leaves `"<lock>` and the strip then reaches the quote, while stripping first
    reaches neither end of it and the basename keeps it.
    """
    return PurePosixPath(value).name.strip(TOKEN_EDGES)


def named_target(tokens: list[str], index: int) -> tuple[str | None, int]:
    """The requirements file the option at `index` names, and the index after its value.

    Handles `-r file`, `-rfile` and `--requirement=file`, each with the value quoted or bare,
    and strips any directory, so that `./requirements.txt` and `$PWD/requirements.txt` are the
    same lock. `lock_named()` is where the quote comes off, and says why it has to come off
    here; all three spellings go through it so that none of them can drift from the others.
    """
    token = tokens[index]
    for option in REQUIREMENT_OPTIONS:
        if token == option:
            if index + 1 >= len(tokens):
                return None, index + 1
            return lock_named(tokens[index + 1]), index + 2
        if token.startswith(f"{option}="):
            return lock_named(token[len(option) + 1 :]), index + 1
        # `-rfile`, which pip accepts for a short option.
        if option.startswith("-") and not option.startswith("--") and token.startswith(option):
            return lock_named(token[len(option) :]), index + 1
    return None, index + 1


def installs_in(text: str, path: str) -> list[Install]:
    """Every install of a lock in one file, taking one command at a time."""
    found = []
    for tokens in commands(text):
        verb = None
        start = 0
        for offset in range(len(tokens)):
            verb = next(
                (form for form in INSTALLERS if tuple(tokens[offset : offset + len(form)]) == form),
                None,
            )
            if verb is not None:
                start = offset
                break
        if verb is None:
            continue
        rest = tokens[start + len(verb) :]
        options: set[str] = set()
        targets = []
        index = 0
        while index < len(rest):
            target, step = named_target(rest, index)
            if target is not None:
                targets.append(target)
                index = step
                continue
            if rest[index].startswith("-"):
                options.add(rest[index].split("=", 1)[0])
            index += 1
        found.extend(
            Install(path, target, frozenset(options), " ".join(verb))
            for target in targets
            if target in LOCKS
        )
    return found


def installs_of_a_lock() -> dict[str, list[Install]]:
    """Every install of a lock in the tracked tree, by the file it sits in.

    The whole tree rather than `INSTALL_SITES`, because the point of that list is to be compared
    against what is really there. A file that cannot be decoded as UTF-8 or read at all is
    reported rather than skipped: "it installs nothing" and "nobody could look" are different
    answers, and the second one silently narrowed this pin.
    """
    found: dict[str, list[Install]] = {}
    unreadable = []
    for name in tracked_files():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a binary file installs nothing
        except OSError as exc:
            unreadable.append(f"{name}: {exc.__class__.__name__}")
            continue
        if installs := installs_in(text, name):
            found[name] = installs
    assert not unreadable, f"tracked files that could not be read: {unreadable}"
    return found


#: The runtime lock, for the cases below to name. Taken from `INPUTS` rather than written out,
#: for the reason those cases carry `{lock}` instead of a lock's name: this file is one of the
#: files the scan reads, so an installer verb spelled here beside a real lock name would be an
#: install of a lock in a file named in neither list -- a finding about itself. `COMMAND_SEPARATORS`
#: above leaves the names out of its comment for the same reason.
PROBE_LOCK = INPUTS["requirements.in"]

#: The spellings `commands()` claims to read, each with the installer it must yield. Stated here
#: rather than derived from the tree, so that this says what the scan is *for* rather than what it
#: currently happens to find. #110 was a docstring claiming one of these and a tree whose one
#: instance of it the scan could not see: `EXPECTED_UNFLAGGED_INSTALLS` had been measured against
#: the buggy scan, so the stated count agreed with the bug and nothing went red. A count derived
#: from behaviour cannot catch behaviour.
#: Every case is one command naming `PROBE_LOCK`, so that what varies is only the syntax in front
#: of the verb. The installers are spread across `INSTALLERS` rather than held constant, and every
#: verb length that list has -- two tokens, three and four -- appears, so that a cut which happened
#: to work for one length only would show up here. They repeat, since there are more spellings than
#: installers, and which case carries which is not load-bearing.
#:
#: These pin the cut from **both** sides. A cut too narrow loses the *verb*, which is #110. A cut
#: too wide loses the *lock*: "take whatever follows the last quote in the token" reads
#: `("requirements.txt")` as `)`, `commands()` drops it as empty, and an install with no target it
#: can name is no install at all -- the same silence, from the opposite mistake. Every case but
#: `bare` names its lock on a token that carries a closing quote or bracket, so that direction is
#: pinned throughout rather than in one place; `quoted target` is the one written for it alone.
COMMAND_SPELLINGS = (
    ("bare", "pip install -r {lock}", "pip install"),
    ("string at the line's start", '"pip3 install -r {lock}"', "pip3 install"),
    ("prefixed string, nothing before it", 'f"pip3 install -r {lock}"', "pip3 install"),
    ("assignment", 'after="uv run --with-requirements {lock}"', "uv run"),
    ("quoted target", 'pip install -r ("{lock}")', "pip install"),
    ("assignment and call", 'after=("pipenv install -r {lock}")', "pipenv install"),
    ("list of raw strings", 'command=[r"""poetry add -r {lock}"""]', "poetry add"),
    ("nested call", 'run(shlex.split("uv add -r {lock}"))', "uv add"),
    ("dict value", '{{"cmd":"uv pip install -r {lock}"}}', "uv pip install"),
    ("keyword after the verb", 'run("pip download -r {lock}", check=True)', "pip download"),
    # One apiece for the glue characters the cases above do not exercise as the character
    # *immediately* before the literal -- `,`, `+` and `{` -- so the list states the whole class
    # rather than the four spellings of it that happened to get written first.
    ("second argument", 'run(cmd,"pip install -r {lock}")', "pip install"),
    ("concatenation", 'prefix+"uv pip install -r {lock}"', "uv pip install"),
    ("set or dict literal", '{{"pipenv install -r {lock}"}}', "pipenv install"),
    # A subscripted assignment: the chain has to cross the `]` to reach the literal.
    ("subscripted assignment", 'steps[0]="poetry add -r {lock}"', "poetry add"),
    # The longest verb `INSTALLERS` names, so every length in that list is exercised here. It also
    # holds a shorter entry inside it -- `pip install` at offset 2 -- so it pins which one is
    # reported: `installs_in()` takes the *earliest* offset, and the four-token entry starts at 0.
    # This is the shape that was found under the wrong verb before #110 rather than not at all.
    ("module invocation", 'after="python -m pip install -r {lock}"', "python -m pip install"),
    # The option's value quoted, one case per option in `REQUIREMENT_OPTIONS` (#113). The verb is
    # reached in every one of these -- it is the *lock* that was lost, because `commands()` strips
    # ends and the opening quote of a value sits in the middle of the token. A command that finds
    # its installer and cannot name its requirements file reads as no install at all, which is the
    # same silence as a verb nobody found, so these belong beside the cases above rather than in a
    # list of their own. `lock_named()` is what reads them.
    ("quoted value, long option", 'pip install --requirement="{lock}"', "pip install"),
    ("quoted value, short option", "pip install -r'{lock}'", "pip install"),
    ("quoted value, uv's long option", 'uv run --with-requirements="{lock}"', "uv run"),
    # A quoted value *and* a literal the chain has to reach, so the two halves of #113 are pinned
    # composed and not only one at a time.
    (
        "quoted value inside a quoted command",
        'after="uv pip install --requirement=\'{lock}\'"',
        "uv pip install",
    ),
    # A glue chain containing a `-`. The chain crosses it; what still stops the cut is a token
    # that *begins* with one, which is the option tokens above and is `(?!-)` rather than a
    # character class. A hyphenated dict key and a hyphenated subscript are the two shapes, since
    # they are the two places a `-` really turns up in front of a literal.
    ("hyphenated dict key", '{{"pre-install":"pip install -r {lock}"}}', "pip install"),
    ("hyphenated subscript", 'd["a-b"]="pipenv install -r {lock}"', "pipenv install"),
    # A shell-quoted basename, which is the shape that decides the order of the two passes in
    # `lock_named()`: the value reaches it as `$DIR"/"<lock>`, with a quote after the separator.
    ('quoted basename', 'pip install -r "$DIR"/"{lock}"', "pip install"),
)

#: Option tokens the cut must hand back whole, as `(line, the token that must survive in it)`.
#: The other side of the `-` widening: `(?!-)` is what keeps an option's own name on the front of
#: its value, and `named_target()` matches on that name, so a token cut here would hand back a
#: value with no option in front of it and the install would lose its lock.
#:
#: Asserted on the tokens for the reason `TOKENS_LEFT_WHOLE` is: a line with no installer in it
#: reads as no install however badly the token was cut, and the spellings above cannot tell a
#: token that survived whole from one the strip happened to repair.
#:
#: The expected token carries the value's *opening* quote and not its closing one, which is not
#: an oversight in these cases but the whole of what #113 was: `TOKEN_EDGES` reaches the end of
#: the token and not the middle of it, so this is exactly what `commands()` yields and what
#: `lock_named()` is handed. Stating it rather than asserting a prefix, so that a cut which
#: trimmed the option down to something still beginning `-` would show up here too.
OPTION_TOKENS_LEFT_WHOLE = (
    ('pip install --requirement="{lock}"', '--requirement="{lock}'),
    ("pip install -r'{lock}'", "-r'{lock}"),
    ('uv run --with-requirements="{lock}"', '--with-requirements="{lock}'),
    # Not a requirements option at all, and the one whose loss `PERMITTED_INSTALL_OPTIONS` is
    # written against: a cut that ate this name would leave a second index behind unremarked.
    (
        'pip install --extra-index-url="https://elsewhere/simple"',
        '--extra-index-url="https://elsewhere/simple',
    ),
)

#: Installs a *wider* cut would lose, as `(line, the installer it must still yield)`. The other
#: direction of the `-` widening, and the one that is fail-open: `OPTION_TOKENS_LEFT_WHOLE` above
#: is the option's name, this is the lock's.
#:
#: The chain is greedy and ends on the last glue-then-quote it can reach, so crossing a `-` let
#: it reach *past* a hyphenated lock name to a quote behind it: the value token
#: `"<lock>",""]` was cut to `""]`, which strips to nothing, and the install had no target left
#: to name. An install the narrower chain found and the wider one lost. `commands()` refuses a
#: cut that leaves nothing behind, which is what keeps these.
#:
#: Asserted on the installs and not on the tokens, unlike the two lists above: here the loss
#: really is visible end to end, because the target is what goes missing rather than a token the
#: later strip repairs. Stated with the *dev* lock, since a `-` in the lock's own name is what
#: the shape needs -- which is why `COMMAND_SPELLINGS` cannot see it at all, every case there
#: formatting `PROBE_LOCK`, whose name has no hyphen.
INSTALLS_A_WIDER_CUT_WOULD_LOSE = (
    ('ARGS = ["pip", "install", "-r", "{lock}",""]', "pip install"),
    ('ARGS = ["pip", "install", "-r", "{lock}","."]', "pip install"),
    ('pip install -r {{"{lock}":""}}', "pip install"),
)

#: Prose that names a lock and is not a command at all. The weakest of the three directions and
#: stated as what it is: none of these carries an installer, so what it pins is that the scan does
#: not *invent* an install out of a sentence -- not that a wider cut would be caught here.
#: `COMMAND_SPELLINGS` above is what answers for too wide, by naming the lock on a token that also
#: carries a closing bracket or quote, and `TOKENS_LEFT_WHOLE` below for the apostrophe rule.
NOT_A_COMMAND = (
    "the {lock}'s hashes are what pip enforces",
    "README's {lock} is the one the image installs",
    "don't regenerate {lock} on its own",
)

#: Words the cut must hand back whole, as `(line, the token that must survive in it)`. Asserted on
#: the tokens rather than on the installs, because that is the level the mistake lives at: none of
#: these lines names an installer, so `installs_in()` answers `[]` however badly the token is cut
#: and `NOT_A_COMMAND` above cannot see the difference.
#:
#: `Ruff's` is the one that was wrong. The string prefix was `*` rather than `{0,2}`, and `Ruff`
#: is built only from prefix letters, so the pattern read it as `Ruff` + `'` + `s` and cut the
#: token to `s`. `.github/workflows/ci.yml` really does say "Start with Ruff's correctness rules",
#: so this is a token in the tree and not an invented one.
TOKENS_LEFT_WHOLE = (
    ("Start with Ruff's correctness rules", "Ruff's"),
    ("don't regenerate it", "don't"),
    ("README's own copy", "README's"),
    ("the {lock}'s hashes", "{lock}'s"),
)


@pytest.mark.parametrize(
    ("line", "verb"),
    [(line, verb) for _, line, verb in COMMAND_SPELLINGS],
    ids=[label for label, _, _ in COMMAND_SPELLINGS],
)
def test_an_install_is_found_however_its_command_is_written(line: str, verb: str) -> None:
    """`commands()` says a command written inside a Python string tokenises as the command it is,
    and until #110 that held only where the literal was not glued to syntax in front of it. Where
    it was, nothing matched at offset 0, and the command was read either as no install at all or
    -- where a shorter entry of `INSTALLERS` sits inside it, as `pip install` does inside
    ``after="python -m pip install …`` -- as an install by that shorter verb.

    An allow-list of spellings rather than the one case that was reported, for the reason
    `PERMITTED_INSTALL_OPTIONS` is an allow-list: the next miss is a spelling nobody has written
    down yet, and a test that re-checks only the spelling somebody already found says nothing
    about it.
    """
    found = installs_in(line.format(lock=PROBE_LOCK), "probe")
    assert [(install.text, install.target) for install in found] == [(verb, PROBE_LOCK)], (
        f"{line!r} did not read as one install of {PROBE_LOCK} by `{verb}`: {found}"
    )


@pytest.mark.parametrize("line", NOT_A_COMMAND)
def test_a_lock_named_in_prose_is_not_an_install(line: str) -> None:
    """That a sentence naming a lock is not read as an install.

    The weakest of the three directions, and worth saying why rather than leaving it to look like
    more than it is: these lines name no installer, so they read as no install however badly the
    token was cut. A *too wide* cut is caught by `COMMAND_SPELLINGS`, which names its lock on a
    token carrying a closing bracket or quote, and the apostrophe rule itself by
    `test_a_word_holding_an_apostrophe_is_not_cut_at_it`, which asserts on the tokens.
    """
    formatted = line.format(lock=PROBE_LOCK)
    assert installs_in(formatted, "probe") == [], formatted


@pytest.mark.parametrize(("line", "token"), TOKENS_LEFT_WHOLE)
def test_a_word_holding_an_apostrophe_is_not_cut_at_it(line: str, token: str) -> None:
    """A quote that is punctuation inside a word is not a literal opening a command, and cutting
    there costs the rest of the token -- which is a lock's own name often enough to matter. The
    only reason this is a test rather than a remark is `Ruff's`: a word built from nothing but
    string-prefix letters was read as a prefix and cut, so the rule had an exception in the tree
    while the comment claiming it did not."""
    tokens = [word for command in commands(line.format(lock=PROBE_LOCK)) for word in command]
    assert token.format(lock=PROBE_LOCK) in tokens, (
        f"{line!r} was cut into {tokens}, losing {token!r}"
    )


@pytest.mark.parametrize(("line", "verb"), INSTALLS_A_WIDER_CUT_WOULD_LOSE)
def test_an_install_a_wider_cut_would_lose_is_still_found(line: str, verb: str) -> None:
    """The fail-open direction of #113's widening, which the reported defect is the mirror of.

    `-` in the glue chain lets the cut reach past a hyphenated lock name to a quote behind it,
    and an install with no target it can name is no install at all -- the same silence as a lock
    behind a quote, from the opposite mistake. `commands()` refuses a cut that leaves nothing
    behind, and this is where that refusal is stated.

    Each of these reads as an install on the narrower chain, so a widening that dropped one would
    be a pin that got quieter: measured against the tracked tree the widening loses nothing, and
    this is what keeps that true of a tree that grows one of these shapes.
    """
    hyphenated = INPUTS["requirements-dev.in"]
    found = installs_in(line.format(lock=hyphenated), "probe")
    assert [(install.text, install.target) for install in found] == [(verb, hyphenated)], (
        f"{line!r} did not read as one install of {hyphenated} by `{verb}`: {found}"
    )


@pytest.mark.parametrize("lock", sorted(LOCKS))
@pytest.mark.parametrize(
    ("line", "verb"),
    [(line, verb) for _, line, verb in COMMAND_SPELLINGS],
    ids=[label for label, _, _ in COMMAND_SPELLINGS],
)
def test_every_spelling_reads_every_lock(line: str, verb: str, lock: str) -> None:
    """Every spelling above, against every lock rather than against `PROBE_LOCK` alone.

    `PROBE_LOCK` has no `-` in its name and two of the three locks do, which stopped mattering
    the moment `-` went into the glue chain: a hyphen inside the *lock* is a place the chain can
    now cross, so a cut that works for one name is no longer evidence about the others. The
    cross-product is cheap and says so directly.
    """
    found = installs_in(line.format(lock=lock), "probe")
    assert [(install.text, install.target) for install in found] == [(verb, lock)], (
        f"{line!r} did not read as one install of {lock} by `{verb}`: {found}"
    )


@pytest.mark.parametrize(("line", "token"), OPTION_TOKENS_LEFT_WHOLE)
def test_an_option_token_is_never_cut_into(line: str, token: str) -> None:
    """`-` is in the glue chain, and `(?!-)` is what keeps that from reaching an option.

    The two are one change: crossing a `-` is what lets `{"pre-install":"pip install …` reach
    its literal, and a token that *begins* with `-` is the one place crossing it would cost
    something, because `named_target()` finds a requirements file by the option's own name. A cut
    into `--requirement="…"` would hand back a value with no option in front of it, and the
    install would find its verb and lose its lock -- #113's own defect, reintroduced from the
    other side.

    On the tokens rather than on the installs, for `TOKENS_LEFT_WHOLE`'s reason: `installs_in()`
    would be repaired by the strip in some of these and silent in the rest, so neither direction
    of `COMMAND_SPELLINGS` can see which token the cut actually returned.
    """
    tokens = [word for command in commands(line.format(lock=PROBE_LOCK)) for word in command]
    assert token.format(lock=PROBE_LOCK) in tokens, (
        f"{line!r} was cut into {tokens}, losing the option {token!r}"
    )


def test_a_quoted_value_names_the_same_lock_as_a_bare_one() -> None:
    """A quote around an option's value makes no difference to the lock it names.

    `COMMAND_SPELLINGS` pins each quoting in one command apiece, which is one option and one
    lock per case. This is the cross-product instead: every option in `REQUIREMENT_OPTIONS`,
    every lock in `LOCKS`, both of Python's quote characters -- `-r'…'` was reported in one and
    `--requirement="…"` in the other -- and the bare value beside them, so that no pair is
    covered only by the case that happened to pick it.

    On `named_target()` rather than through `installs_in()`, because every spelling here needs an
    installer verb in front of it to be a command and only some of these options belong to any
    one verb: `pip install --with-requirements …` would be three quarters of this matrix spelled
    as commands nobody can run. The spellings are the three that function implements, each given
    the token `commands()` would really hand it.
    """
    for option in REQUIREMENT_OPTIONS:
        for lock in sorted(LOCKS):
            # `-r file`. A standalone value carries its quotes on both ends, so `commands()`
            # has already taken them and there is nothing left here for a quote to survive in.
            separated, step = named_target([option, lock], 0)
            assert (separated, step) == (lock, 2), f"{option} {lock} named {separated!r}"
            for quote in ("", "'", '"'):
                # `--requirement=file`, or `-rfile` for a short option -- the two spellings that
                # carry the value in the option's own token. Here the value's opening quote is in
                # the middle of that token and the ends-only strip in `commands()` cannot reach
                # it, which is the whole of #113.
                joined = (
                    f"{option}={quote}{lock}"
                    if option.startswith("--")
                    else f"{option}{quote}{lock}"
                )
                named, step = named_target([joined], 0)
                assert (named, step) == (lock, 1), f"{joined} named {named!r}"


def test_every_documented_install_of_a_lock_requires_hashes() -> None:
    """The image's install is the enforcing one, and it is not the only one anybody runs.

    `CONTRIBUTING.md`, `README.md`, `CLAUDE.md` and `AGENTS.md` each tell a contributor to
    install the dev lock on the host that holds both live tokens, and CI installs it twice. pip
    turns hash checking on by itself as soon as one requirement carries a `--hash`, so what the
    flag adds on those paths is the case where a lock has lost its hashes *altogether* -- which
    is exactly the regeneration mistake worth catching, and it would otherwise fail only in the
    image.

    The options are an allow-list and not a search for one good flag, for the reason
    `PERMITTED_INSTALL_OPTIONS` gives: `--require-hashes --index-url https://elsewhere/simple`
    carries the flag and is still a second source of code.
    """
    for name, installs in sorted(installs_of_a_lock().items()):
        if name in EXEMPT_INSTALL_SITES or name == MUTATION_SOURCE:
            continue
        for install in installs:
            assert "--require-hashes" in install.options, (
                f"{name} installs {install.target} without --require-hashes"
            )
            unnamed = install.options - PERMITTED_INSTALL_OPTIONS
            assert not unnamed, (
                f"{name}'s install of {install.target} passes {sorted(unnamed)}, which this "
                "file does not name. Add it here on purpose."
            )


def test_no_file_installs_a_lock_unless_it_is_named_here() -> None:
    """The safety direction. A file nobody listed telling somebody to install a lock is the
    image losing the flag, one step further from the build."""
    unnamed = set(installs_of_a_lock()) - INSTALL_SITES - EXEMPT_INSTALL_SITES - {MUTATION_SOURCE}
    assert not unnamed, (
        f"these files install a lock and are named in neither list: {sorted(unnamed)}"
    )


def test_every_file_named_here_still_installs_a_lock() -> None:
    """The bookkeeping direction, kept apart from the one above on purpose: the repair for this
    failure is to delete a name, and that is also how an unhashed install would go green."""
    found = set(installs_of_a_lock())
    assert INSTALL_SITES <= found, f"named but installing nothing: {sorted(INSTALL_SITES - found)}"
    assert EXEMPT_INSTALL_SITES <= found, (
        f"exempted but installing nothing -- drop the exemption: {sorted(EXEMPT_INSTALL_SITES - found)}"
    )


def test_no_unhashed_install_is_added_to_a_file_outside_the_bound() -> None:
    """The exemptions are whole-file, so this is what keeps them from being a hiding place.

    The archived plans are not held to the flag, and `tests/test_negative_controls.py` carries
    de-flagged installs as mutation data on purpose.
    None of them may grow one unnoticed: the counts are stated, so adding an install is a line a
    reviewer reads rather than nothing at all.
    """
    unflagged = {
        name: sum(1 for install in installs if "--require-hashes" not in install.options)
        for name, installs in installs_of_a_lock().items()
        if name not in INSTALL_SITES
    }
    assert unflagged == EXPECTED_UNFLAGGED_INSTALLS, (
        "the unhashed installs outside the bound are not the ones recorded here. If you added "
        f"one, say why in EXPECTED_UNFLAGGED_INSTALLS: {unflagged}"
    )


def test_the_image_names_its_base_by_content() -> None:
    """A tag is a name upstream can move, and it decides pip's version and the CA bundle too."""
    froms = [line for line in dockerfile_lines() if line.startswith("FROM ")]
    assert len(froms) == 1, froms
    match = BASE_IMAGE.match(froms[0])
    assert match, f"the base image is not pinned by digest: {froms[0]}"
    assert match.group("name") == "python"


# ─── what keeps it current ───────────────────────────────────────────────────


def _watched() -> set[tuple[str, str]]:
    configuration = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    return {
        (update["package-ecosystem"], update["directory"]) for update in configuration["updates"]
    }


def test_dependabot_follows_both_the_lock_and_the_base_image() -> None:
    """A pin nobody moves becomes an old artefact with a known hash, which is its own problem.

    `pip` covers the locks because they sit in the directory it watches; `docker` is what
    moves the digest above when the tag does.
    """
    watched = _watched()
    assert ("pip", "/") in watched
    assert ("docker", "/") in watched


def test_dependabot_watches_the_directory_every_lock_actually_sits_in() -> None:
    """Every lock, not "the locks" as a figure of speech.

    Dependabot's pip updater reads the requirement files of the directories it is told about
    and no others, so a lock is followed because of where it sits. Asserting `("pip", "/")`
    alone was enough while all of them were at the root and says nothing once one is not --
    and a lock nobody updates is #104's own argument running backwards: an artefact pinned by
    content, with a known vulnerability, that every install is now guaranteed to fetch.
    """
    watched = {directory for ecosystem, directory in _watched() if ecosystem == "pip"}
    for lock in sorted(LOCKS):
        parent = PurePosixPath(lock).parent
        directory = "/" if parent == PurePosixPath(".") else f"/{parent}"
        assert directory in watched, (
            f"{lock} sits in {directory}, which Dependabot's pip updater does not watch. "
            "Add a directory entry for it."
        )
