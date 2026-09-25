"""The execution context repo-shipped agent tooling runs in.

This repository ships text that agents execute -- the plans under
`docs/superpowers/plans/`, the security-sweep skill and its workflow -- and it ships it to a
host whose whole reason for existing is that it holds two live bearer tokens. Every one of
those documents used to make its own local choice about the environment it ran in, with no
rule to check the choice against, which is how one of them came to name a fixed directory in
world-writable `/tmp` as a package cache six times.

So the rule is one file the harness reads, not a paragraph each author restates:
`.claude/settings.json` confines shell commands and refuses reads of the host's secret stores.
These assert that file's shape, because a settings file is only an enforcement point while it
says what it is believed to say -- and assert that no shipped document has gone back to
carrying its own environment prefix or reads as work still to do.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / ".claude" / "settings.json"

# The two the container bind-mounts, and so the two an agent on this host can reach directly.
CREDENTIAL_STORES = ("~/.claude", "~/.codex")

# Secret stores that are nothing to do with codervis but sit on the same host, named because
# the sweep's own hands-off rule names them. The list is a backstop, not the boundary: the
# boundary is `blockReadsOutsideWorkingDirectories`, which needs no list.
OTHER_SECRET_STORES = ("~/.ssh", "~/.aws", "~/.config/gh", "~/.docker")

# The two undocumented endpoints the live clients call, which the suite must never reach.
LIVE_QUOTA_HOSTS = ("claude.ai", "chatgpt.com")

PLANS_DIR = ROOT / "docs" / "superpowers" / "plans"
WORKFLOW = ROOT / ".claude" / "workflows" / "security-sweep.js"

TEXT_SUFFIXES = {".md", ".js", ".json", ".yml", ".yaml", ".py", ".sh", ".toml", ".ini"}

# A fixed name under shared `/tmp`, which any other local principal can create and fill
# before the command that reads it runs. The pattern is deliberately blunt rather than a list
# of the spellings two plans happened to use: the next author will use a third.
FIXED_TMP_PATH = re.compile(r"(?<![\w/])/tmp/[\w.${}-]+")

# One exemption, named rather than pattern-dodged. `.github/workflows/ci.yml` builds synthetic
# credential files under a fixed `/tmp/codervis-ci`; a GitHub-hosted runner is single-principal
# so nothing else can get there first, and the fix (the runner's own temp directory) needs a
# token with `workflow` scope, which the agent that wrote this check does not have. Tracked as
# follow-up #30; delete this line with that change, not around it.
TMP_PATH_EXEMPT = (".github/workflows/ci.yml",)


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


@pytest.fixture(scope="module")
def settings() -> dict:
    assert SETTINGS_PATH.is_file(), (
        "the execution-context rule for agent tooling lives in .claude/settings.json"
    )
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _deny_rules(settings: dict) -> list[str]:
    return list(settings.get("permissions", {}).get("deny", []))


def test_shell_commands_are_confined(settings: dict) -> None:
    """The sandbox is what makes the rule bind a shell command rather than a file tool."""
    sandbox = settings.get("sandbox", {})
    assert sandbox.get("enabled") is True


def test_confinement_grants_nothing(settings: dict) -> None:
    """A settings file that tightens must not widen on the way past.

    `autoAllowBashIfSandboxed` defaults to true, so turning the sandbox on would otherwise
    run every command in this project without asking -- a grant this repository did not have
    before and is not asking for. And nothing here adds an allow rule: whatever an operator
    already granted is what an agent may still do.
    """
    sandbox = settings.get("sandbox", {})
    assert sandbox.get("autoAllowBashIfSandboxed") is False
    assert not settings.get("permissions", {}).get("allow"), (
        "this file exists to restrict; grants belong in the operator's own settings"
    )


def test_reads_outside_the_working_directories_are_blocked(settings: dict) -> None:
    """The allow-list form of the rule, which is the one that cannot forget a store."""
    permissions = settings.get("permissions", {})
    assert permissions.get("blockReadsOutsideWorkingDirectories") is True


def test_the_credential_stores_are_denied_by_name(settings: dict) -> None:
    """And the deny-list form, which survives an operator widening the working directories."""
    rules = " ".join(_deny_rules(settings))
    for store in CREDENTIAL_STORES + OTHER_SECRET_STORES:
        assert store in rules, f"no deny rule covers {store}"


def test_the_credential_stores_are_denied_to_sandboxed_commands(settings: dict) -> None:
    """`permissions.deny` binds the file tools; the credential layer binds the shell too."""
    files = settings.get("sandbox", {}).get("credentials", {}).get("files", [])
    denied = {entry.get("path") for entry in files if entry.get("mode") == "deny"}
    for store in CREDENTIAL_STORES + OTHER_SECRET_STORES:
        assert store in denied, f"sandboxed commands are not denied {store}"


def test_sandboxed_egress_cannot_reach_the_live_quota_endpoints(settings: dict) -> None:
    """The one allow-list in this file, and the two hosts that must never join it.

    `CLAUDE.md` and `AGENTS.md` both say the suite must not call the undocumented quota
    endpoints. Under the sandbox that stops being a promise: the hosts are simply not
    reachable, and adding them here would quietly take the enforcement back.
    """
    allowed = settings["sandbox"]["network"]["allowedDomains"]
    assert allowed, "an empty allow-list makes every sandboxed fetch a prompt"
    for host in LIVE_QUOTA_HOSTS:
        assert not any(host in entry for entry in allowed), (
            f"{host} is the live quota endpoint's host; it does not belong in the sandbox's "
            "allow-list"
        )


def test_no_shipped_document_names_a_fixed_path_in_shared_tmp() -> None:
    """The instance that made the rule necessary.

    A fixed name under world-writable `/tmp` is a directory any other local principal can
    pre-create and fill, and the command that reads it runs as the operator. Nothing in a
    shipped document should name one -- and under the rule above, nothing needs to: the
    environment a command runs in is the harness's business, not each document's.
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


def test_no_plan_reads_as_work_still_to_do() -> None:
    """A plan whose boxes are unticked is a plan an agent picks up and works through.

    Both of this repository's plans describe work that is over -- one shipped, one for
    providers that were deleted -- so neither has any business carrying an unticked box or
    telling a reader to execute it task by task.
    """
    plans = list(PLANS_DIR.rglob("*.md"))
    assert plans, f"no plans under {PLANS_DIR}; has this check outlived its subject?"

    live = [p for p in plans if "archive" not in p.relative_to(PLANS_DIR).parts]
    assert not live, f"a plan outside the archive reads as pending: {live}"

    for path in plans:
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(ROOT)
        assert "- [ ]" not in text, f"{name} still carries an unticked box"
        assert "REQUIRED SUB-SKILL" not in text, f"{name} still tells an agent to execute it"


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
