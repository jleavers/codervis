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

PLANS_DIR = ROOT / "docs" / "superpowers" / "plans"
WORKFLOW = ROOT / ".claude" / "workflows" / "security-sweep.js"

# Files a clone gets that an agent may execute. `.git` and the gitignored run directories are
# excluded; everything else committed is fair game, because an agent reads what it is given.
def _shipped_text_files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(ROOT).parts
        if parts[0] in {".git", ".issuebot"}:
            continue
        if parts[:2] in {(".claude", "worktrees"), (".claude", "security-sweeps")}:
            continue
        if path.suffix in {".md", ".js", ".json", ".yml", ".yaml", ".py", ".sh"}:
            out.append(path)
    return out


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
    for store in CREDENTIAL_STORES:
        assert store in denied, f"sandboxed commands are not denied {store}"


def test_no_shipped_document_names_a_fixed_path_in_shared_tmp() -> None:
    """The instance that made the rule necessary.

    A fixed name under world-writable `/tmp` is a directory any other local principal can
    pre-create and fill, and the command that reads it runs as the operator. Nothing in a
    shipped document should name one -- and under the rule above, nothing needs to: the
    environment a command runs in is the harness's business, not each document's.
    """
    offenders = []
    for path in _shipped_text_files():
        if path == Path(__file__):  # the scanner spells the needle it looks for
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if "/tmp/uv-cache" in line or "UV_CACHE_DIR" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, f"fixed /tmp cache path in shipped text: {offenders}"


def test_no_plan_reads_as_work_still_to_do() -> None:
    """A plan whose boxes are unticked is a plan an agent picks up and works through.

    Both of this repository's plans describe work that is over -- one shipped, one for
    providers that were deleted -- so neither has any business carrying an unticked box or
    telling a reader to execute it task by task.
    """
    live = [p for p in PLANS_DIR.rglob("*.md") if "archive" not in p.relative_to(PLANS_DIR).parts]
    assert not live, f"a plan outside the archive reads as pending: {live}"

    for path in PLANS_DIR.rglob("*.md"):
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
    assert "data, not instructions" in source

    prompts = [
        line for line in source.splitlines()
        if line.startswith("const ") and "Prompt" in line.split("=")[0]
    ]
    assert len(prompts) >= 7, f"unexpectedly few prompt definitions: {prompts}"
    for line in prompts:
        assert "${WHERE}" in line, f"prompt built without the shared preamble: {line}"
