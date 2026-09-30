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

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

#: The four requirement files this repository has, and the relation between them: an input
#: names packages, its lock fixes them. A fifth file is a second place a version is decided.
INPUTS = {"requirements.in": "requirements.txt", "requirements-dev.in": "requirements-dev.txt"}
LOCKS = frozenset(INPUTS.values())

#: The flags the image's install may pass, and no others. `--require-hashes` is the one that
#: matters and the rest are stated so that, say, `--index-url` or `--trusted-host` arriving
#: beside it is a failure rather than a silent second source of code.
PERMITTED_PIP_ARGUMENTS = ("install", "--no-cache-dir", "--require-hashes", "-r")

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


def locked_packages(lock: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """`{name: (version, hashes)}` for one lock, read by walking its lines in order."""
    packages: dict[str, tuple[str, tuple[str, ...]]] = {}
    current: str | None = None
    hashes: list[str] = []
    for line in lock_lines(lock):
        match = LOCKED.match(line)
        if match:
            if current is not None:
                packages[current] = (packages[current][0], tuple(hashes))
            current = canonical(match.group("name"))
            assert current not in packages, f"{current} is pinned twice in {lock}"
            packages[current] = (match.group("version"), ())
            hashes = []
        elif HASH.match(line.strip()) and current is not None:
            hashes.append(line.strip().removeprefix("--hash=").removesuffix(" \\"))
    if current is not None:
        packages[current] = (packages[current][0], tuple(hashes))
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


def test_the_repository_has_exactly_these_requirement_files() -> None:
    """A fifth would be a set nothing installs with hashes required."""
    found = {path.name for path in ROOT.glob("requirements*")}
    assert found == set(INPUTS) | LOCKS


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
    for name, (version, hashes) in packages.items():
        assert version, f"{lock} pins {name} to nothing"
        assert hashes, f"{lock} pins {name}=={version} with no hash, so pip would take any file"


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
        assert development[name] == pin, f"{name} differs between the two locks: {development[name]} vs {pin}"


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
