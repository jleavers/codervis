"""Third-party code reaches the build by content hash, not by name and version range (#104).

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

What is *not* pinned here, because it cannot be witnessed without an index: that each lock is
complete. `pip install --require-hashes` is what establishes that, by refusing to install a
dependency the file does not name, and the `Dockerfile` line below is where it runs.
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

#: The four requirement files this repository has, and the relation between them: an input
#: names packages, its lock fixes them. A fifth file is a second place a version is decided.
INPUTS = {"requirements.in": "requirements.txt", "requirements-dev.in": "requirements-dev.txt"}
LOCKS = frozenset(INPUTS.values())

#: The flags the image's install may pass, and no others. `--require-hashes` is the one that
#: matters and the rest are stated so that, say, `--index-url` or `--trusted-host` arriving
#: beside it is a failure rather than a silent second source of code.
PERMITTED_PIP_ARGUMENTS = ("install", "--no-cache-dir", "--require-hashes", "-r")

#: Every tracked file whose installs of a lock must require hashes. Named here rather than
#: discovered, so that a *new* file telling somebody to install a lock is a failure until it is
#: added on purpose: the flag being on the image's install alone was the gap, not the image.
INSTALL_SITES = frozenset(
    {
        "Dockerfile",
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
#: - `tools/screenshots/` installs the runtime lock through `uv run --with-requirements`, and in
#:   the same command takes `playwright` and `pillow` by bare name. That is #108, filed rather
#:   than fixed here, and README's Security notes name it as the exception; it is no part of the
#:   image or of the test set.
#: - The archived plans under `docs/superpowers/plans/archive/` are the record of work that is
#:   over, which `tests/test_agent_tooling_context.py` is what keeps true. Rewriting a finished
#:   plan's command would falsify the record rather than fix anything anyone runs.
#:
#: Exempt from the *flag* is not invisible: `EXPECTED_UNFLAGGED_INSTALLS` below counts what each
#: of these carries, so one more cannot arrive unremarked.
EXEMPT_INSTALL_SITES = frozenset(
    {
        "tools/screenshots/README.md",
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
EXPECTED_UNFLAGGED_INSTALLS = {
    "tools/screenshots/README.md": 1,
    "docs/superpowers/plans/archive/2026-06-08-agy-1.0.6-compatibility.md": 5,
    "docs/superpowers/plans/archive/2026-06-08-browser-widget-toggles.md": 5,
    MUTATION_SOURCE: 2,
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


def tracked_requirement_files() -> set[str]:
    """The requirement files a clone gets.

    `git ls-files` rather than a glob, for the reason the other file checks in this suite read
    it: a developer's scratch `requirements-local.txt` is not something this repository ships,
    and failing on one would be a pin that bites the wrong person.
    """
    # `startswith`, which is what the `requirements*` pathspec this replaced matched: a
    # nested `tools/x/requirements.txt` was not one of this repository's requirement files
    # then and is not now.
    return {name for name in tracked_files() if name.startswith("requirements")}


def test_the_repository_has_exactly_these_requirement_files() -> None:
    """A fifth would be a set nothing installs with hashes required."""
    assert tracked_requirement_files() == set(INPUTS) | LOCKS


@pytest.mark.parametrize("source", sorted(INPUTS))
def test_an_input_names_packages_and_decides_no_version(source: str) -> None:
    """One place decides a version, and it is the lock.

    A range here would be a second, and the two drift: a bump Dependabot writes into the lock
    leaves the input saying something else, and the next regeneration undoes the bump.
    """
    assert asked_for(source), f"{source} names no package"


def test_the_dev_input_builds_on_the_runtime_one() -> None:
    """So a contributor runs the code the image runs, rather than a second resolution of it."""
    text = (ROOT / "requirements-dev.in").read_text(encoding="utf-8")
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


def test_the_dev_lock_agrees_with_the_runtime_lock_package_for_package() -> None:
    """What the tests run against is what the image runs, artefact for artefact.

    Two independent resolutions would let CI pass against one `starlette` while the image
    installs another, which is the drift #20 is about arriving by a different route.
    """
    runtime = locked_packages("requirements.txt")
    development = locked_packages("requirements-dev.txt")
    for name, pin in runtime.items():
        assert name in development, f"{name} is in the image and not in a contributor's venv"
        assert development[name] == pin, (
            f"{name} differs between the two locks: {development[name]} vs {pin}"
        )


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

    Backticks, quotes and sentence punctuation come off each token in one pass rather than in
    sequence, since `AGENTS.md` ends an install in ``.txt`.`` and the controls end one in
    ``.txt",``. Separators are replaced *before* that, because stripping would eat a bare ``;``
    entirely and the cut would be lost.
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
            elif stripped := token.strip("`\"'.,;:()"):
                command.append(stripped)
        found.append(command)
    return [command for command in found if command]


def named_target(tokens: list[str], index: int) -> tuple[str | None, int]:
    """The requirements file the option at `index` names, and the index after its value.

    Handles `-r file`, `-rfile` and `--requirement=file`, and strips any directory, so that
    `./requirements.txt` and `$PWD/requirements.txt` are the same lock.
    """
    token = tokens[index]
    for option in REQUIREMENT_OPTIONS:
        if token == option:
            if index + 1 >= len(tokens):
                return None, index + 1
            return PurePosixPath(tokens[index + 1]).name, index + 2
        if token.startswith(f"{option}="):
            return PurePosixPath(token[len(option) + 1 :]).name, index + 1
        # `-rfile`, which pip accepts for a short option.
        if option.startswith("-") and not option.startswith("--") and token.startswith(option):
            return PurePosixPath(token[len(option) :]).name, index + 1
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

    `tools/screenshots/` and the archived plans are not held to the flag, and
    `tests/test_negative_controls.py` carries de-flagged installs as mutation data on purpose.
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


def test_dependabot_follows_both_the_lock_and_the_base_image() -> None:
    """A pin nobody moves becomes an old artefact with a known hash, which is its own problem.

    `pip` covers both locks because they sit in the directory it watches; `docker` is what
    moves the digest above when the tag does.
    """
    configuration = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    watched = {
        (update["package-ecosystem"], update["directory"]) for update in configuration["updates"]
    }
    assert ("pip", "/") in watched
    assert ("docker", "/") in watched
