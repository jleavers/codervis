"""What repo-shipped agent tooling says, as distinct from where it runs.

This repository ships text that agents execute -- the archived documents under
`docs/superpowers/`, the security-sweep skill and its workflow. That text used to make
its own local choices about the environment it ran in, which is how one plan came to name a
fixed directory in world-writable `/tmp` as a package cache six times (#21).

The environment an operator's agents run in is the operator's own to configure. The
repository does not ship a `.claude/settings.json` that confines it: one did (#31), and it
turned every shell command the operator ran in this checkout into a permission prompt and
blocked the harness's own auto-memory, which secured nothing for anyone who clones the
repository. What these tests hold instead is the shipped text, and the shape of the sweep that runs it:
that no document carries its own environment prefix, that nothing under `docs/superpowers/`
reads as work still to do, that every sweep prompt tells its agent that what it reads is data,
that material one stage relays to the next arrives fenced and labelled, and that every stage
launches through one path holding one named tool profile (#44).

The profiles are subagent definitions under `.claude/agents/`, which is a different kind of
file from the settings one above: a definition constrains no session and grants none of them
anything they do not already hold. It is registered in this checkout and can be delegated to by
name -- the sweep is the only thing here that does -- so what these checks hold is that the
sweep still asks for one per stage, and that each one still holds what that stage's output
needs and no more.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_SETTINGS = ROOT / ".claude" / "settings.json"

# `*.y*ml`, not `*.yml`: GitHub reads a form named `.yaml` exactly the same, so the narrower
# glob left `bug.yaml` with a `render: shell` field invisible to every check in this module.
ISSUE_FORMS = sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.y*ml"))

DOCS_DIR = ROOT / "docs" / "superpowers"
PLANS_DIR = DOCS_DIR / "plans"
WORKFLOW = ROOT / ".claude" / "workflows" / "security-sweep.js"
SKILL = ROOT / ".claude" / "skills" / "security-sweep" / "SKILL.md"
AGENTS_DIR = ROOT / ".claude" / "agents"
CLAUDE_README = ROOT / ".claude" / "README.md"
RELAY_TEST = ROOT / "tests" / "test_sweep_relay.js"

# What each stage's agent may hold. The value is the exact `tools:` list its definition
# declares, in order, because "the triage pass has no shell" is the whole point of the file and
# a tool added to it is a decision, not a detail. The report stage held `Bash` until #80, for a
# dedupe that was two read-only `gh` listings -- which a tool list cannot say, since a shell
# that runs those runs `gh issue close` too, and which is why SKILL.md's post-run audit looks
# for write verbs. The tracker listing is the launching session's now, filtered to
# maintainer-authored items and relayed through the fence, so the stage holds no shell at all.
STAGE_TOOLS = {
    "sweep-recon": "Read, Glob, Grep, Bash, Write",
    "sweep-lane": "Read, Glob, Grep, Bash, Edit, Write",
    "sweep-lane-web": "Read, Glob, Grep, Bash, Edit, Write, WebFetch, WebSearch",
    "sweep-triage": "Read, Glob, Grep, Write",
    "sweep-report": "Read, Glob, Grep, Write",
}

# What an archived document opens with. The plans carry this sentence; the two design specs
# beside them did not, which is how they outlived the archiving of their own plans and went on
# reading as designs someone had yet to implement (#46). Requiring it of every document here
# leaves no room for a live one, which is the state of this subtree and not a law of nature:
# a design that is genuinely outstanding belongs somewhere this check does not cover, or the
# check gets widened on purpose -- not a pasted header that makes it read as finished.
ARCHIVED_MARKER = "> **Archived —"

TEXT_SUFFIXES = {".md", ".js", ".json", ".yml", ".yaml", ".py", ".sh", ".toml", ".ini"}

# A fixed name under a world-writable shared directory, which any other local principal can
# create and fill before the command that reads it runs. The pattern is deliberately blunt
# rather than a list of the spellings two plans happened to use: the next author will use a
# third.
#
# Every such directory, not the one instance that was found. `/tmp` was the spelling #21 met,
# and so the only one this matched, which left `/var/tmp/<name>` and `/dev/shm/<name>` passing
# -- the same defect one directory over (#78). Each of these is mode 1777 on a stock image,
# and the sticky bit stops another principal *removing* the operator's file, not creating the
# name first and owning it when the operator's command opens it. `/private/tmp` is where macOS
# keeps the first.
SHARED_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/private/tmp")
FIXED_TMP_PATH = re.compile(
    # Longest first, so `/var/tmp/x` is reported whole rather than as the `/tmp/x` inside it.
    # The lookbehind is what keeps `/home/me/tmp/x` and a path inside a URL out.
    r"(?<![\w/])(?:"
    + "|".join(re.escape(shared) for shared in sorted(SHARED_DIRS, key=len, reverse=True))
    + r")/[\w.${}-]+"
)

# No exemptions. There was one, for the synthetic credential files `.github/workflows/ci.yml`
# used to build under a fixed `/tmp/codervis-ci`; #30 moved them to the runner's own temp
# directory and emptied this, which is the only way an exemption here is meant to end. A file
# that has to name such a path again gets a line and the reason, not a spelling the pattern
# above happens to miss.
TMP_PATH_EXEMPT: tuple[str, ...] = ()


def _shipped_files() -> list[Path]:
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
        # `cwd` decides which repository this reads, so nothing in the environment may: an
        # exported GIT_DIR or GIT_WORK_TREE -- a git hook's, a CI wrapper's -- would otherwise
        # answer for a tree nobody here named.
        env={name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
    ).stdout
    paths = [ROOT / name for name in listed.split("\0") if name]
    assert paths, "git ls-files returned nothing; is this a checkout?"
    return [path for path in paths if path.is_file()]


def _shipped_text_files() -> list[Path]:
    """The shipped files this module can scan as text."""
    return [path for path in _shipped_files() if path.suffix in TEXT_SUFFIXES]


# The sweep text that sends its agent to the GitHub side on purpose, and why. They read
# unfiltered and unfenced, with a shell, and SKILL.md and `.claude/README.md` say so beside the
# post-run audit that stands behind them. Anything else that acquires it is a decision, and
# this set is where the decision is argued for -- so the argument is per lane, because the four
# do not all have the same one, and a fifth is weighed against whichever of the two it claims.
#
# - **What a stranger wrote**, which a listing filtered to maintainer-authored items is exactly
#   the removal of. The `publication` lane of the `gaps` and `fixes` sets is sent to every
#   issue, comment, review comment and Actions run log, to find a credential that becomes
#   readable by anyone on the day this repository is public; `public/disclosure` (#89) asks
#   that of the change to public itself, over every reachable object and pull-request ref, the
#   tracker, and the Actions logs and artifacts. The dedupe pass's filtered listing cannot
#   stand in for any of it.
# - **Repository state, which exists only on the GitHub side.** `public/outsiders` (#89) is the
#   one lane here for this second reason rather than the first: its reads are GETs on the
#   repository object, the Actions permissions, the `main` ruleset, collaborators, deploy keys
#   and webhook hosts, and the *names* of secrets. None of that is stranger-written text, and
#   the filtered listing is not a poor substitute for it but no substitute at all -- it carries
#   no settings. A checkout cannot answer what an account with no role may do to the repository
#   once anyone can reach it.
# - **GitHub's copy of the history, which a clone does not hold.** `unowned/supply-chain`'s
#   last bullet is sent to the repository activity endpoint, because GitHub serves a
#   force-pushed-over commit by SHA after no ref names it and a clone has stopped fetching it;
#   what the lane is asking is how far back that reaches and what the retention is. The object
#   it is looking for is the one a checkout is missing by definition, so there is nowhere else
#   to ask. It was not in this set until #91 and should have been from the start: its brief
#   names no `gh` command and no Actions run, so the marker below never saw it, and being
#   invisible to a spelling check is not the same as not going.
#
# Both `public` briefs bound themselves to reads in their closing line, which is text and not a
# tool list, so the post-run audit stands behind them exactly as it does for the rest.
#
# The lanes that are not here and should not be: `public/cloner` runs the stack on a stranger's
# machine, `public/shipped-text` mutates a copy of the tree, and `unowned/assurance` mutates
# this one. None of them needs the GitHub side to do it.
GITHUB_SIDE_BY_DESIGN = {
    "gaps/publication",
    "fixes/publication",
    "public/disclosure",
    "public/outsiders",
    "unowned/supply-chain",
}

# How a brief says it: a `gh` subcommand, or GitHub's own name for the logs. Two markers rather
# than one, because the `fixes/publication` brief names no command at all -- it says "all issue
# and PR threads, #1 onwards" and leaves the agent to pick the call -- and a check keyed on
# `gh` alone would have pinned one of the two exceptions and left the other invisible, which is
# the failure mode this whole test exists to prevent.
#
# The character class is the third spelling, and it is load-bearing: `\bgh ` alone requires a
# space, and a brief writes the command in a code span -- `` \`gh\` listings `` -- far more
# often than bare. This reads the workflow's *source*, where that is `gh` followed by a
# backslash, so `public/disclosure` matched on one occurrence and it was the closing
# prohibition ("never pass `-X` ... to `` \`gh api\` ``") rather than the bullet that sends it
# to the Actions logs; its tracker bullet names no command at all, exactly as
# `fixes/publication`'s does not. A lane writing `` \`gh\` `` throughout would have matched
# nothing and gone green -- the #89 shape one layer down, caught here rather than in a run.
# (#89's own two lanes did not ship green; they shipped red, which is #91.) The backtick is in
# the class because
# `tests/test_sweep_relay.js` runs this same marker over the *rendered* prompt, where the
# escape is gone.
#
# The lookbehind is what keeps that widening from swallowing the whole sweep, and it is the
# one place the two readers genuinely differ. `\b` already excludes "through" and "high", but
# not a *path*: `HANDS_OFF` names `` \`~/.config/gh\` `` among the secret stores no stage may
# read, and `WHERE` puts it at the top of every prompt, outside the fence. This module never
# sees it -- it reads `${WHERE}` unexpanded -- but the relay test reads the rendered prompt,
# where every stage of every lane set would have matched and the property would have asserted
# nothing at all. So the marker excludes a `gh` that follows a word character, a `/` or a `.`,
# which is a command the moment it does not.
#
# The third alternative is a phrase rather than a command, and it is here because a brief can
# send an agent to the GitHub side without naming the tool it gets there with:
# `unowned/supply-chain` asks for the **repository activity endpoint** and names no `gh` at
# all. That is the honest shape of this marker and the reason it is not the whole control --
# it reads what a brief *says*, so "with the GitHub CLI", a bare `api.github.com` URL or
# "mirror-clone the remote" would each evade it. What it catches is a lane acquiring the
# GitHub side in the spelling lanes actually use; a lane that reaches it some other way is
# caught by a reviewer, and the set below is where the argument for each one is written so
# that a reviewer has something to check it against.
GITHUB_SIDE = re.compile(
    r"(?<![\w./])gh[ \\`]|Actions run|repository activity endpoint"
)


def _sweep_briefs(source: str) -> dict[str, str]:
    """Every prompt the workflow writes, and every lane brief it interpolates into one.

    A lane's `brief` is a template literal inside a `<SET>_LANES` array, not a `const
    <name>Prompt`, and `scanPrompt` renders it in the prompt's own voice -- so a check that
    reads only the prompt builders reads none of the text that actually reaches a scan agent.
    The keys are `<lane set>/<lane key>`, spelled as `LANE_SETS` and `args.lanes` spell them.
    """
    texts = {
        name: _expand_constants(source, _prompt_body(source, name))
        for name in re.findall(r"const (\w*[Pp]rompt\w*) = ", source)
    }
    sets = dict(re.findall(r"\n  (\w+): (\w+_LANES),", source))
    assert sets, "no lane sets found; has LANE_SETS moved?"
    for set_name, array in sets.items():
        body = re.search(rf"const {array} = \[(.*?)\n\]\n", source, re.S)
        assert body is not None, f"no array named {array}"
        lanes = re.findall(r"key: '([^']+)',(.*?)\n  \}", body.group(1), re.S)
        assert lanes, f"no lanes parsed out of {array}"
        for key, brief in lanes:
            texts[f"{set_name}/{key}"] = _expand_constants(source, brief)
    return texts


def _expand_constants(source: str, text: str) -> str:
    """Inline the module constants a prompt or a lane brief interpolates, as the agent gets them.

    `${PUBLICATION_READ_BOUND}` is a block of prose whose whole subject is which reads its lane
    may make, and `${PUBLICATION_READ_CALLS...}` renders the list of them. A check that read the
    text as written would see the two interpolations and none of what they say, so a prompt that
    acquired the GitHub side by carrying a shared constant would be invisible to the one check
    in this module that asks which of them reach it at all.

    An escaped `\\${NAME}` is left alone: three briefs quote `${USERPROFILE}` as the literal
    `.env.example` carries, and an interpolation is what that text is about rather than
    something it does. A name that resolves to neither a template nor an array constant is left
    written as it stands, for the same reason -- this returns text to read, not to evaluate.
    """
    unresolved: set[str] = set()
    for _ in range(4):
        found = [
            (whole, name)
            for whole, name in re.findall(r"(?<!\\)(\$\{([A-Z][A-Z0-9_]*)[^{}]*\})", text)
            if name not in unresolved
        ]
        if not found:
            break
        for whole, name in found:
            template = re.search(rf"const {name} = `((?:[^`\\]|\\.)*)`", source, re.S)
            if template is not None:
                text = text.replace(whole, template.group(1))
                continue
            array = re.search(rf"const {name} = \[([^\]]*)\]", source)
            if array is not None:
                text = text.replace(whole, "\n".join(re.findall(r"'([^']*)'", array.group(1))))
                continue
            unresolved.add(name)
    return text


def _between(text: str, opening: str, closing: str) -> str:
    """The span of ``text`` between two anchors, both of which must be there.

    A missing anchor is an error rather than an empty span: a check scoped to a passage that
    has been renamed away would otherwise pass by finding nothing to look at, which is the
    same defect as scoping it to the whole file.
    """
    start = text.index(opening)
    end = text.index(closing, start)
    return text[start:end]


def _const_body_list(source: str, name: str) -> list[str]:
    """The string literals of ``const <name> = ['a', 'b']``, which is not a template literal."""
    match = re.search(rf"const {name} = \[([^\]]*)\]", source)
    assert match is not None, f"no const {name} array"
    return re.findall(r"'([^']*)'", match.group(1))


def _const_body(source: str, name: str) -> str:
    """The template-literal body of ``const <name> = `...` ``, so a check can be scoped to it.

    These templates quote code spans, so the closing backtick is the first one that is not
    escaped -- `source.index("`")` would stop at ``\\`app/quota.py\\```.
    """
    start = source.index(f"const {name} = `") + len(f"const {name} = `")
    end = re.compile(r"(?<!\\)`").search(source, start)
    assert end is not None, f"const {name} has no closing backtick"
    return source[start : end.start()]


def _prompt_body(source: str, name: str) -> str:
    """The template-literal body of a prompt builder, ``const <name> = (args) => `...` ``.

    Four of the seven prompts take arguments and three do not, so the opening is matched up to
    the first backtick of the definition rather than assumed.
    """
    opening = re.search(rf"const {name} = [^`\n]*`", source)
    assert opening is not None, f"no prompt builder named {name}"
    end = re.compile(r"(?<!\\)`").search(source, opening.end())
    assert end is not None, f"const {name} has no closing backtick"
    return source[opening.end() : end.start()]


#: Every tracked file under `.claude/`, exactly. This is the register of what the repository
#: hands an agent's harness, and it is an equality rather than an absence check for the reason
#: #78 gives: "the repository ships no agent configuration" was pinned on the one file name
#: somebody had already seen, so `.claude/settings.local.json` -- which the harness reads
#: exactly as it reads `settings.json`, and which can carry a `SessionStart` hook that runs a
#: command in every session in the checkout -- landed with the suite green. So did every other
#: name the harness honours: a `hooks/` directory, an `mcp.json`, a `settings.*.json` for a
#: named profile, and any of them under a nested `.claude/` rather than this one.
#:
#: This is the half that answers for a *committed* file, of any shape at all, which is what
#: the rule is really about: the check below is about an operator's own working tree and so
#: permits the file the harness writes for them there.
#:
#: The five `sweep-*.md` profiles and the skill and workflow beside them are here because they
#: are the kind of file AGENTS.md draws the distinction about: a subagent definition constrains
#: no session and grants none of them anything they do not already hold. A settings file does
#: both, which is why none is listed and why adding one has to be a line in this test.
SHIPPED_AGENT_FILES = frozenset(
    {
        ".claude/README.md",
        ".claude/agents/sweep-lane-web.md",
        ".claude/agents/sweep-lane.md",
        ".claude/agents/sweep-recon.md",
        ".claude/agents/sweep-report.md",
        ".claude/agents/sweep-triage.md",
        ".claude/skills/security-sweep/SKILL.md",
        ".claude/workflows/security-sweep.js",
    }
)


#: The names the harness reads as its own configuration, as globs relative to the repository
#: root. Two of them are not under `.claude/` at all -- Claude Code reads the project-scoped
#: MCP declaration from `.mcp.json` and a plugin manifest from `.claude-plugin/` -- and a glob
#: written as `.claude/mcp.json` matches nothing whatever, which is a register that claims to
#: cover "every name the harness honours" failing in exactly the way #78 is about.
#:
#: Anchored at the root, because these are matched against the *working tree* and a walk that
#: descends is a walk into `.venv/`, `node_modules/` and this repository's own
#: `.claude/worktrees/`. A vendored package's `.mcp.json` is not read by anything -- Claude
#: Code resolves that name and `.claude-plugin/` at the project root -- and failing a
#: developer's suite on one is the same defect as failing them for their own
#: `settings.local.json`, which `_shipped_files()` above says in as many words this module
#: must not do. The *tracked* reach is the one that goes to any depth, just below, and it
#: reads `git ls-files` rather than walking.
HARNESS_CONFIG_GLOBS = (
    ".claude/settings.json",
    ".claude/settings.*.json",
    ".claude/hooks/**/*",
    ".mcp.json",
    ".claude-plugin/**/*",
)

#: What a harness settings file is *permitted* to carry, as an allow-list -- not a list of the
#: keys that happen to be dangerous.
#:
#: This was three bad key names for one round of review and that was the defect this whole
#: change exists to fix, one level down: `{"statusLine": {"type": "command", "command": ...}}`
#: runs an attacker-chosen command on every status-line render and was not among the three, and
#: neither were `apiKeyHelper`, `awsAuthRefresh`, `enableAllProjectMcpServers` or
#: `permissions.defaultMode`. The next key the harness gains will not be among them either.
#:
#: What an operator legitimately has here is the file the harness writes when they approve a
#: permission in this checkout, which `.gitignore` names as per-user session state. So that
#: shape is permitted and everything else is reported: `permissions` with the three lists in
#: it, and the schema pointer an editor adds.
#: This is narrower than everything the harness *may* write here, on purpose:
#: `enabledMcpjsonServers` and `enableAllProjectMcpServers` are approvals too, and each starts
#: a process for every session in the checkout, so they go in the operator's own
#: `~/.claude/` rather than at a path inside this repository.
PERMITTED_SETTINGS_KEYS = frozenset({"$schema", "permissions"})
PERMITTED_PERMISSION_KEYS = frozenset(
    # `additionalDirectories` is what `/add-dir` records, so it is one of the approvals this
    # shape is about. It widens what a session may *read and write*, not what it runs.
    {"allow", "deny", "ask", "additionalDirectories", "defaultMode"}
)
#: And the values `defaultMode` may take, as an allow-list for the same reason the keys are
#: one. It was a single refused string for one round -- and `acceptEdits`, which stops the
#: harness asking before any write in any session started here, went straight past it, as did
#: `"BYPASSPERMISSIONS"` and a trailing space. Both modes below leave the asking in place;
#: `acceptEdits` and `bypassPermissions` each take some of it away, which is a decision for
#: an operator's own settings and not for a file in this tree.
PERMITTED_DEFAULT_MODES = frozenset({"default", "plan"})

#: Paths whose *location* is what binds, so there is no shape to check: a hook script is a
#: script, and a plugin manifest brings its own directory with it. Nothing writes a file here
#: on an operator's behalf -- one is put there on purpose -- so any is reported.
BINDING_BY_LOCATION = ("/.claude/hooks/", "/.claude-plugin/")


def _harness_config_files() -> set[str]:
    """Whatever is present at the project's own harness paths, tracked or not."""
    return {
        path.relative_to(ROOT).as_posix()
        for pattern in HARNESS_CONFIG_GLOBS
        for path in ROOT.glob(pattern)
        if path.is_file()
    }


def _tracked_harness_config() -> set[str]:
    """The same names among the *tracked* files, at any depth.

    Any depth here and not above, because this reads `git ls-files`: a `.claude/` directory is
    honoured wherever it sits, so `app/.claude/settings.json` is the same file by another path
    -- and asking git rather than walking means an ignored `.venv` full of vendored packages
    cannot answer for this repository.
    """
    found: set[str] = set()
    for path in _shipped_files():
        relative = path.relative_to(ROOT)
        parts = relative.parts
        if ".claude" in parts:
            below = parts[parts.index(".claude") + 1 :]
            settings = (
                len(below) == 1
                and below[0].startswith("settings")
                and below[0].endswith(".json")
            )
            if settings or below[:1] == ("hooks",):
                found.add(relative.as_posix())
        if relative.name == ".mcp.json" or ".claude-plugin" in parts:
            found.add(relative.as_posix())
    return found


def _outside_the_permitted_shape(path: Path) -> str | None:
    """Why this harness-config file is more than an operator's own record, or `None`.

    Read rather than assumed, because `.gitignore` names `.claude/settings.local.json` as
    per-user session state: the harness writes one the first time an operator approves a
    permission in this checkout, and failing the suite on its mere *presence* would fail every
    developer for doing the thing AGENTS.md says is theirs to do.

    So what is checked is the shape, as an allow-list. Not a list of dangerous keys -- that was
    here for one round and `statusLine`, which runs a command on every render, was not on it,
    which is #78's own defect one level down. A committed file of any shape is caught by the
    tracked-set checks instead, which is where "the repository ships no agent configuration"
    really lives.
    """
    relative = "/" + path.relative_to(ROOT).as_posix()
    for directory in BINDING_BY_LOCATION:
        # A file the harness executes *because of where it sits* -- a hook script, a plugin
        # manifest -- has no JSON shape to check, and its content is beside the point.
        if directory in relative:
            return f"sits under {directory.strip('/')}, which the harness runs from"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A settings path that is not readable JSON is not something this test can clear, so
        # it says so rather than passing it over.
        return "is at a harness-configuration path and could not be read as JSON"
    if not isinstance(loaded, dict):
        return "is at a harness-configuration path and is not a JSON object"
    beyond = sorted(set(loaded) - PERMITTED_SETTINGS_KEYS)
    if beyond:
        return f"declares {beyond}, which is more than a record of approved permissions"
    permissions = loaded.get("permissions") or {}
    if not isinstance(permissions, dict):
        return "declares a `permissions` that is not an object"
    beyond = sorted(set(permissions) - PERMITTED_PERMISSION_KEYS)
    if beyond:
        return f"declares permissions.{beyond}"
    mode = permissions.get("defaultMode")
    if mode is not None and mode not in PERMITTED_DEFAULT_MODES:
        return f"sets permissions.defaultMode to {mode!r}"
    return None


def test_the_repository_ships_exactly_these_agent_facing_files() -> None:
    """What a clone hands the harness, stated here rather than inferred from one file's absence.

    A file added under `.claude/` is a change to what every session in this checkout inherits,
    on a host that holds two live tokens. Whether it is the benign kind -- another subagent
    profile -- or the kind that binds the operator is not something a test can tell from the
    name, so the rule is that it arrives here, in the same change, and a reviewer decides.
    """
    tracked = {
        path.relative_to(ROOT).as_posix()
        for path in _shipped_files()
        # Any depth, not just the root: a `.claude/` directory is honoured wherever it sits,
        # so `app/.claude/settings.json` is the same kind of file by another path.
        if ".claude" in path.relative_to(ROOT).parts
    }
    assert tracked == SHIPPED_AGENT_FILES, (
        "the set of files this repository ships under .claude/ has changed. Each one is read "
        "by the harness of every session started in a clone: list it here, in the same "
        "change, and say what it grants -- see \"What repo-shipped agent text may say\" in "
        "AGENTS.md."
    )


def test_the_repository_commits_no_harness_configuration_anywhere() -> None:
    """The other half of the same rule, for the names that do not live under `.claude/`.

    The equality above is anchored on that one directory, so a committed `.mcp.json` at the
    root -- which declares MCP servers for every session in the checkout, and so is the same
    kind of file as a settings one -- would be invisible to it. This reads the tracked set
    against `HARNESS_CONFIG_GLOBS`, wherever those names sit.

    Tracked rather than present, unlike the check below: a committed one is what a *clone*
    gets, and that is the thing this repository controls.
    """
    committed = sorted(_tracked_harness_config())
    assert not committed, (
        f"{committed} is committed and configures the harness of every session started in a "
        "clone. The operator's agent environment is theirs to configure (#21, #34, #35)."
    )


def test_the_repository_does_not_configure_the_operators_agent_environment() -> None:
    """A committed project settings file binds every session in the checkout, the operator's.

    One was added for #21 and reverted: with its sandbox on a host where the sandbox cannot
    start, every shell command became a prompt, and its `Read(~/.claude/**)` deny also
    refused the harness's own auto-memory, which no `!` rule could carve back out. A fix that
    wants to constrain agents belongs in the text they read or in the operator's own
    settings, not here. If a settings file is ever needed, it is a decision to make on
    purpose -- delete this test in the same change, and say why.

    Kept beside the equality above, and not folded into it, because it names the bound: the
    set check says "this file is not on the list" and this says why that particular file is
    not, and what happened when it was. It is also the half that catches an untracked one --
    a `settings.local.json` written into a checkout is not on any list `git ls-files` returns,
    and it binds the session all the same.
    """
    assert not PROJECT_SETTINGS.exists(), (
        f"{PROJECT_SETTINGS.relative_to(ROOT)} would bind the operator's own sessions"
    )
    beyond = {
        name: reason
        for name in sorted(_harness_config_files())
        if (reason := _outside_the_permitted_shape(ROOT / name)) is not None
    }
    assert not beyond, (
        f"{beyond}. The operator's agent environment is theirs to configure (#21, #34, #35), "
        "and a file recording the permissions they approved in this checkout is part of that "
        "-- but anything past that shape binds every session anyone starts here, and belongs "
        "in their own `~/.claude/` rather than at this path."
    )


def test_the_shared_directory_rule_covers_every_such_directory_and_not_one() -> None:
    """The pattern's own reach, stated here, because the pattern *is* the rule.

    A scanner is only as wide as what it matches, and nothing asked what this matched until a
    document naming `/var/tmp` went through it green. So each directory is asserted to be
    caught, and the near-misses beside it asserted not to be: a rule this blunt earns its keep
    only by being blunt in both directions.
    """
    # Written out again rather than iterated from `SHARED_DIRS`, which is the constant this
    # test exists to pin: a loop over it narrows when it narrows, and a scratch copy with
    # `/dev/shm` deleted from the tuple passed this file green.
    assert SHARED_DIRS == ("/tmp", "/var/tmp", "/dev/shm", "/private/tmp")
    for shared in ("/tmp", "/var/tmp", "/dev/shm", "/private/tmp"):
        assert FIXED_TMP_PATH.search(f"cache wheels under {shared}/codervis-cache first"), shared
        # The directory itself is not a fixed name *in* it; neither is one that merely ends
        # with the same characters, nor a path inside a URL.
        assert not FIXED_TMP_PATH.search(f"clean {shared} out"), shared
        assert not FIXED_TMP_PATH.search(f"under /home/me{shared}/cache"), shared
        assert not FIXED_TMP_PATH.search(f"https://example.test{shared}/cache"), shared
    # Reported whole, rather than as the shorter directory nested inside the longer one.
    assert FIXED_TMP_PATH.search("under /var/tmp/cache").group(0) == "/var/tmp/cache"


def test_no_shipped_document_names_a_fixed_path_in_shared_tmp() -> None:
    """The instance #21 found, and every directory of its kind (#78).

    A fixed name under a world-writable directory is one any other local principal can
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


def test_no_document_under_superpowers_reads_as_work_still_to_do() -> None:
    """A document whose work reads as outstanding is one an agent picks up and works through.

    Named for the subtree it reads, not for every shipped document: the check beside it really
    does read them all, and one name for two reaches is how the last one came to promise more
    than it looked at. Every document under `docs/superpowers/` describes work that is over --
    some shipped, some for providers that were deleted -- so none has any business carrying an
    unticked box, carrying the sub-skill marker that tells an agent to execute the document, or
    omitting the header that says which kind of finished it is. Prose can still read as work --
    both specs carry numbered steps under "Tests" -- and no substring check catches that; what
    answers for it is the header, up front, saying the work is over.

    The reach is the whole subtree, not `plans/` alone. It was `plans/` alone until #46, while
    this module's own docstring promised that no shipped document reads as pending: the two
    design specs under `specs/` were never archived when their plans were (#21), so they went
    on reading as live designs -- one of them describing a credential mount this tree does not
    have as one that "remains", and asserting that implementing it needs no Compose change.

    And the subject is what a clone gets, so it comes from the tracked files like every other
    check here, rather than from a walk of the worktree: an untracked scratch file a developer
    left under `docs/` is not this suite's business, and a committed one cannot escape by
    being something other than Markdown -- everything shipped there has to be a document this
    check can read, or the check would be narrower than it says again.
    """
    docs = [path for path in _shipped_files() if DOCS_DIR in path.parents]
    assert docs, f"no documents under {DOCS_DIR}; has this check outlived its subject?"
    unreadable = [path.relative_to(ROOT) for path in docs if path.suffix != ".md"]
    assert not unreadable, (
        f"shipped under {DOCS_DIR.relative_to(ROOT)} and not a Markdown document, so nothing "
        f"below reads it: {unreadable}. Make it one, or widen this check on purpose."
    )
    plans = [path for path in docs if PLANS_DIR in path.parents]
    assert plans, f"no plans under {PLANS_DIR}; has this check outlived its subject?"

    live = [path for path in plans if "archive" not in path.relative_to(PLANS_DIR).parts]
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


def _frontmatter(path: Path) -> dict[str, str]:
    """The `key: value` lines of a Markdown document's leading `---` block."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.relative_to(ROOT)} has no front matter"
    block = text.split("---\n", 2)[1]
    fields = {}
    for line in block.splitlines():
        if line and not line.startswith((" ", "\t")) and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_every_sweep_agent_launches_through_one_path() -> None:
    """One place decides what a stage is told, what it is handed and what it holds.

    Before #44 there were seven `agent()` call sites, each passing a label, a phase and a
    schema and nothing else: no call named a tool profile, so every stage ran with whatever the
    operator's session held, and each site interpolated the previous stage's output into its
    prompt in its own way. The check is structural rather than a count of good intentions --
    exactly one call to `agent()` exists, and it is the launcher's.
    """
    source = WORKFLOW.read_text(encoding="utf-8")

    calls = [
        (number, line)
        for number, line in enumerate(source.splitlines(), start=1)
        if re.search(r"(?<![\w.])agent\(", line)
    ]
    assert len(calls) == 1, f"every stage launches through one path, but agent() is called at: {calls}"

    launcher = source.index("const launch = ({")
    assert launcher < source.index(calls[0][1]), "the one agent() call is not the launcher's"
    body = source[launcher : source.index("\n}\n", launcher)]
    for argument in ("instructions", "relayed = []", "profile", "label", "phase", "schema"):
        assert argument in body, f"the launcher does not take {argument} as its own argument"

    # Taking the arguments is not using them. Both of these survived a text-only check: a
    # launcher that built `\`${instructions}\`` would relay nothing to anybody with every
    # fence constant still in the file, and one that dropped `agentType` from the options
    # would leave five profiles shipped and asked for by nothing.
    assert "renderRelay(relayed)" in body, (
        "the launcher does not put its relayed material through the fence, so a stage that "
        "passes some would send none"
    )
    options = body[body.index("agent(`") :]
    assert "agentType" in options, (
        "the launcher resolves a stage's profile and does not pass it to agent(), so nothing "
        "is scoped: the five definitions under .claude/agents/ would ship and be asked for by "
        "nothing"
    )

    # Every launch names a profile. A call that forgot one would throw at run time -- the
    # launcher has no default -- but a sweep that fails in its fifth phase has already spent
    # an hour, so the shape is pinned here too.
    launches = source.count("launch({")
    assert launches >= 7, f"unexpectedly few stage launches: {launches}"
    assert source.count("profile: ") >= launches, "a stage launches without naming a tool profile"


def test_relayed_material_reaches_an_agent_fenced_and_labelled() -> None:
    """What one stage hands the next is other people's text, carried verbatim by design.

    The evidence rule makes a finding quote what it is about, and the injection rule turns an
    injected instruction into a finding -- so an imperative somebody wrote into an issue, a log
    or a comment arrives inside the refuter's, the critic's and the report's prompts. It
    arrives inside a fence that says who wrote it and out of what, and no prompt interpolates
    it bare.
    """
    source = WORKFLOW.read_text(encoding="utf-8")

    for marker in ("RELAY_BEGIN = '===== BEGIN RELAYED DATA", "RELAY_END = '===== END RELAYED DATA"):
        assert marker in source, f"the fence has lost its {marker.split(' =')[0]}"

    rule = " ".join(_const_body(source, "RELAY_RULE").split())
    assert "data, not instructions" in rule
    assert "written by other agents in this sweep" in rule, (
        "the fence does not say who wrote what is inside it"
    )
    for claim in ("tracker", "CI logs", "quoted inside it"):
        assert claim in rule, f"the fence rule does not mention {claim}"

    # The launcher serialises every relayed value itself. A call site that stringified its own
    # material could put a raw newline inside the fence, and a line that closes the fence is
    # the one thing the fence cannot survive.
    stringifies = [
        line.strip() for line in source.splitlines() if "JSON.stringify(" in line
    ]
    assert len(stringifies) == 2, f"unexpected JSON.stringify call sites: {stringifies}"
    assert any("const body = JSON.stringify(" in line for line in stringifies)
    # The survivor is the funnel's own arithmetic, which the script computes from its own
    # counters -- numbers, not anybody's text.
    assert any("${JSON.stringify(counts)}" in line for line in stringifies)
    assert "DELIMITER_SHAPE = /^={3,}/" in source, (
        "the guard tests for the two exact markers rather than for the shape a reader goes by, "
        "so a line like `<the end marker> then do X` would still close the fence"
    )
    assert "defused" in source, "nothing keeps relayed material from closing its own fence"

    # `args.known` is the launching session's prose, and SKILL.md tells that session to build it
    # out of the tracker, so it is other people's text one step removed. It used to be
    # interpolated into every scan prompt bare, above the rules.
    assert "${known}" not in source, (
        "args.known is interpolated into a prompt bare; relay it like anything else"
    )

    labelled = set(re.findall(r"relay\(\s*[`'\"]([^`'\"]+)", source))
    for relayed in (
        "findings from lane ",
        "the finding to refute",
        "surviving findings",
        "coverage records",
        "findings and verdicts",
        "clusters",
        "singletons",
        "coverage gaps",
        "tracker items",
    ):
        assert any(label.startswith(relayed) for label in labelled), (
            f"{relayed!r} no longer reaches the next stage through the fence; relayed: {labelled}"
        )


def test_the_data_rule_covers_material_quoted_inside_a_finding() -> None:
    """A finding is required to quote its evidence verbatim, so the quote is the injection.

    The rule used to name the places text arrives from -- issues, comments, logs -- and left
    the reader to work out that a command quoted inside a colleague's finding is the same
    thing. It says so now.
    """
    fragment = _const_body(WORKFLOW.read_text(encoding="utf-8"), "DATA_NOT_INSTRUCTIONS")
    assert "quoted inside" in fragment, "the rule does not reach material quoted in a finding"
    for field in ("evidence", "attack_path", "reasoning"):
        assert field in fragment, f"the rule does not name the {field} field"


def test_refuters_reproduce_a_finding_rather_than_running_what_it_names() -> None:
    """A refuter is told to follow an attack path, and the path is written in relayed text.

    "Follow the attack path yourself" plus a finding quoting a command is an agent running the
    command an attacker wrote. Both refutation prompts say where the reproduction comes from
    instead: the code, and probes the refuter writes itself.
    """
    source = WORKFLOW.read_text(encoding="utf-8")
    for name in ("verifyPrompt", "escalatePrompt"):
        body = " ".join(_prompt_body(source, name).split())
        assert "run a command" in body or "running a command" in body, (
            f"{name} does not say that a command a finding names is not how to reproduce it"
        )
        assert "probes" in body and ("your own" in body or "you write" in body), (
            f"{name} does not tell its refuter that the probes are its own to write"
        )


def test_every_stage_holds_a_named_tool_profile_and_nothing_wider() -> None:
    """A stage that obeys injected text can reach only what that stage's output needs.

    The profiles are subagent definitions the workflow asks for by name. They are not the
    settings file above: they constrain no session an operator starts and grant none of them
    anything they do not already hold, and the test beside this one still fails if such a file
    appears.
    """
    source = WORKFLOW.read_text(encoding="utf-8")

    shipped = sorted(path.name for path in AGENTS_DIR.glob("*.md"))
    assert shipped == sorted(f"{name}.md" for name in STAGE_TOOLS), (
        f"the harness reads every Markdown file under {AGENTS_DIR.relative_to(ROOT)} as a "
        f"subagent definition, and these are not the five stage profiles: {shipped}"
    )

    held = {}
    for name, tools in STAGE_TOOLS.items():
        fields = _frontmatter(AGENTS_DIR / f"{name}.md")
        held[name] = fields.get("tools") or ""
        assert fields.get("name") == name, f"{name}.md declares a different name"
        assert held[name] == tools, (
            f"{name} holds {held[name]!r}, not {tools!r}. Widening a stage's tools is "
            f"a decision: say in the same change what an injected instruction could reach with it."
        )
        assert f"'{name}'" in source, f"nothing in the workflow launches {name}"

    # The three properties worth reading as sentences, off what the files say. The equality
    # above already ties each file to the table, so these add no reach -- they say which parts of
    # those tool lists are load-bearing, so that a change to one arrives with an explanation.
    assert "Bash" not in held["sweep-triage"], "the triage stage has acquired a shell"
    assert "Bash" not in held["sweep-report"], (
        "the report stage has acquired a shell; its dedupe reads a tracker listing relayed to "
        "it, and a shell is how it would go and fetch an unfiltered one itself (#80)"
    )
    assert "Web" not in held["sweep-report"], "the report stage has acquired the web"
    assert "Web" not in held["sweep-lane"], (
        "the default lane profile has acquired the web; a lane whose brief needs it declares "
        "`web: true` and gets sweep-lane-web"
    )


def test_no_sweep_stage_goes_and_reads_the_tracker() -> None:
    """Who wrote a tracker item is a bound on what may reach an agent; a preamble is not.

    The dedupe pass used to run `gh issue list` and `gh pr list` in its own shell, on the host
    that holds this dashboard's two live tokens, and the bodies it read carried no author and
    no fence (#80). Any GitHub account can open an issue on a public repository, edit its own
    and close it, so a stranger's self-closed "fixed" issue was enough to make a genuine new
    cluster read as a duplicate -- no disobedience needed, and so nothing in the prompt to
    disobey. The listing is the launching session's now, and this script's to filter.
    """
    source = WORKFLOW.read_text(encoding="utf-8")

    # Every prompt the workflow writes, and every lane brief it interpolates into one. The
    # briefs are the half worth saying out loud: they live in `LANE_SETS`, not in a `*Prompt*`
    # constant, and `scanPrompt` puts them in the prompt's own voice, outside the fence. A
    # check scoped to the prompts alone would have read as this whole property while the lane
    # briefs went unexamined.
    #
    # `gh` is on the `PATH` of every stage that holds a shell, so what is pinned is what the
    # text asks for, not what the tool lists allow.
    reaches_github = {
        name for name, body in _sweep_briefs(source).items() if GITHUB_SIDE.search(body)
    }
    assert reaches_github == GITHUB_SIDE_BY_DESIGN, (
        f"the sweep text that sends an agent to the GitHub side is {sorted(reaches_github)}, "
        f"not {sorted(GITHUB_SIDE_BY_DESIGN)}. Tracker and Actions text an agent reads with a "
        f"shell is bounded by nothing but the preamble (#80); a stage that needs it says so "
        f"here, and SKILL.md and .claude/README.md say the same to the operator."
    )

    # And they do say it, rather than the set being the only place it is written down. Both
    # documents claim a widening is a change to them as well, and until #91 nothing made that
    # true: a lane went green the moment the two allow-lists agreed, with the operator still
    # reading "the `publication` lanes of the `gaps` and `fixes` sets".
    #
    # SKILL.md is read between its two anchors rather than whole, and that is the difference
    # between a check and the appearance of one. It carries a per-set lane table naming every
    # lane of every set, so a whole-file search finds any real lane there whatever the
    # exception paragraph says -- the half would have passed for a lane nobody had argued for.
    # `.claude/README.md` has no such table and is read whole. What is checked is the lane's
    # own key, which is what a reader needs to know which lane is meant; neither half can bound
    # *where* within its span the name falls, so this fails the change that never went near the
    # paragraph rather than standing in for reading it. The key is also all it checks, so the
    # two `publication` lanes stand or fall together here -- a document naming one set's and
    # not the other's passes. Which set a `publication` lane belongs to is the reasons above,
    # and those are prose a reviewer reads.
    where_it_is_argued = {
        SKILL: _between(
            SKILL.read_text(encoding="utf-8"),
            "lanes still read the GitHub side",
            "\nAfter a run, audit",
        ),
        CLAUDE_README: CLAUDE_README.read_text(encoding="utf-8"),
    }
    for lane in sorted(GITHUB_SIDE_BY_DESIGN):
        key = lane.split("/", 1)[1]
        for doc, text in where_it_is_argued.items():
            assert key in text, (
                f"{doc.relative_to(ROOT)} does not name the `{key}` lane where it says which "
                f"lanes read the GitHub side, though GITHUB_SIDE_BY_DESIGN says it does. The "
                f"operator reads that passage to know which stages the post-run audit has to "
                f"stand behind, so a lane added to the set is argued for in both documents."
            )

    # The relay test runs this same marker over the rendered prompt, and two hand-kept copies
    # drifting apart is what #91 was. So the literal is asserted to be there rather than
    # promised in a comment: a narrowing on the JS side is otherwise silent, because that
    # assertion simply stops finding lanes and keeps passing.
    relay = RELAY_TEST.read_text(encoding="utf-8")
    assert f"const GITHUB_SIDE = /{GITHUB_SIDE.pattern}/" in relay, (
        f"{RELAY_TEST.relative_to(ROOT)} does not carry this module's marker verbatim. It "
        f"reads the prompt a stage is really launched with and this one reads the workflow's "
        f"source, so a marker true of only one of them pins half of this property."
    )

    # The filter is the script's, because the command that produces the listing is one line in
    # a skill document and the association is the whole of what makes an item trustworthy.
    associations = _const_body_list(source, "MAINTAINER_ASSOCIATIONS")
    assert associations == ["OWNER", "MEMBER", "COLLABORATOR"], (
        f"the maintainer associations are {associations}; anything wider admits an account "
        f"with no commit rights to this repository"
    )
    filterer = source[source.index("const maintainerAuthored = ") :]
    filterer = filterer[: filterer.index("\n}\n")]
    assert "MAINTAINER_ASSOCIATIONS.includes(item.authorAssociation)" in filterer, (
        "maintainerAuthored() does not check the association, so args.tracker reaches the "
        "dedupe pass however the launching session built it"
    )
    assert "String(item.author || AUTHOR_UNKNOWN)" in filterer, (
        "a relayed tracker item does not carry its author. An item whose author GitHub no "
        "longer has still has one recorded, because an empty string reads the same as a field "
        "nobody filled in"
    )
    assert "const AUTHOR_UNKNOWN = '(author unknown)'" in source, (
        "the sentinel for a deleted author is missing or is one an account could hold; "
        "`unknown` is a valid GitHub login, so a deleted author would read as a real account"
    )

    # The cap bounds records; this bounds bytes. One issue body can be 65,536 characters, so
    # 300 capped records is still a prompt of any size -- and the same text crosses the
    # launching session's own context on the way. Both halves are pinned, since either alone
    # leaves the other free to go.
    assert re.search(r"const TRACKER_BODY_CHARS = (\d+)\b", source), (
        "no per-body cap on the relayed listing"
    )
    body_chars = int(re.search(r"const TRACKER_BODY_CHARS = (\d+)\b", source).group(1))
    assert body_chars <= 8000, (
        f"TRACKER_BODY_CHARS is {body_chars}; a cap that large stops bounding the prompt, "
        f"which is the whole of what it is for"
    )
    assert "body: cut(String(item.body || ''))" in filterer, (
        "a relayed tracker item's body does not go through the cut, so the cap on how many "
        "items are relayed is the only bound and it does not bound bytes"
    )
    assert "body.slice(0, TRACKER_BODY_CHARS) + BODY_TRUNCATED" in filterer, (
        "the cut does not bound the body to TRACKER_BODY_CHARS, or does not mark where it cut"
    )
    # Counted and said, not only done. This module's own rule for the record cap is that a cut
    # the stage cannot see is a search it reports as whole, and a body that stops early is the
    # same cut on the other axis.
    assert "truncated += 1" in filterer, "a cut body is not counted"
    assert "the report must say that bodies were cut" in source, (
        "the dedupe pass is not told that bodies were cut, so it reports having read issues it "
        "only read the first part of"
    )

    # And it is applied: a listing that reached the relay unfiltered would leave every constant
    # above in place and change nothing about what the report stage reads.
    assert "maintainerAuthored(args.tracker)" in source, (
        "args.tracker is not put through the filter"
    )
    # Matched on every spelling of the read, not on the one the script happens to use:
    # `args['tracker']` and a destructure reach the same property and would have gone past a
    # fixed-substring check, which is the narrowing this whole module exists to catch.
    # Comments stripped first: this file is unusually comment-dense, and a comment that names
    # the property is not a read of it -- counting one would turn this red for prose.
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    reads = re.findall(r"""args\s*(?:\.\s*tracker\b|\[\s*['"]tracker)""", code)
    assert len(reads) == 1, (
        f"args.tracker is read {len(reads)} times; it reaches the relay only through "
        f"maintainerAuthored(), so a second read is a way round the filter"
    )
    assert re.search(r"\{[^}]*\btracker\b[^}]*\}\s*=\s*args\b", code) is None, (
        "args is destructured for `tracker`, which reads it without going through the filter"
    )
    assert "'tracker items'," in source, (
        "the tracker listing does not reach the dedupe pass as a labelled relayed block"
    )

    # The command that produces the listing names no repository literal. SKILL.md says twice
    # that `repo` is what `gh repo view` printed and never a literal, and nothing pinned it:
    # the first draft of this very change shipped `repos/jleavers/codervis/issues`. In a clone
    # or a fork -- which the skill supports, and #77 is about -- that deduped the swept tree's
    # clusters against a different project's tracker, so a real finding matched an upstream
    # issue and was suppressed as a duplicate. And `author_association` is relative to the
    # repository in the URL, so the maintainer filter would have been enforcing the wrong
    # repository's trust boundary under the right name.
    skill = SKILL.read_text(encoding="utf-8")
    listing = skill[skill.index("### The tracker listing") :]
    listing = listing[: listing.index("\n## ")]
    literals = re.findall(r"repos/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", listing)
    assert not literals, (
        f"the phase 0 tracker command names {literals!r} rather than the repository the sweep "
        f"resolved. The tracker it reads and the tree it audits have to be one repository, and "
        f"an author association means nothing without knowing which repository it is relative to"
    )
    assert "repos/{owner}/{repo}/issues" in listing or "repos/$REPO/issues" in listing, (
        "the phase 0 tracker command does not read the repository the sweep resolved. `gh`'s "
        "own `{owner}`/`{repo}` placeholders are the spelling to prefer -- they resolve from "
        "the current directory, so unlike a shell variable they do not need the fences of this "
        "document to run in one shell"
    )

    # And it asks for an order, because `TRACKER_CAP` keeps the front of the list. `/issues`
    # defaults to newest-first, which would have cut exactly the oldest issues -- the "reported
    # and fixed, or reported and forgotten" material the dedupe prompt says matters most. An
    # order left to a default is a decision nobody made.
    assert "-f sort=created -f direction=asc" in listing, (
        "the phase 0 tracker command does not ask for an order, so which items the workflow's "
        "cap keeps is GitHub's newest-first default -- which drops the oldest issues, the ones "
        "the dedupe pass is told matter most"
    )
    assert re.search(r'\(item\.get\("body"\) or ""\)\[:BODY_CHARS\]', listing), (
        "the phase 0 command does not cut issue bodies, so the whole of every maintainer issue "
        "crosses the launching session's context before the workflow's own cap can bound it"
    )
    skill_body_chars = re.search(r"^BODY_CHARS = (\d+)$", listing, re.M)
    assert skill_body_chars, "the phase 0 command declares no body cut"
    assert int(skill_body_chars.group(1)) == body_chars, (
        f"the phase 0 command cuts bodies at {skill_body_chars.group(1)} and the workflow at "
        f"{body_chars}. They are one bound written twice -- the command so the text never "
        f"reaches the launching session's context, the script because it is the enforcement "
        f"point -- and two numbers that may drift are not one bound"
    )


def test_no_issue_form_asks_a_stranger_for_an_executable_section() -> None:
    """A form any GitHub account fills in should not solicit input shaped as steps to run.

    Both forms ended in a `Validation` textarea rendered as a `shell` block, asking for "the
    commands that must pass" (#80). GitHub writes that into the issue body under `### Validation`
    as a fenced shell block, and an automated agent working this tracker reads a section of that
    name as steps to run -- which is what the body of this repository's own issue workflow says
    it does.
    """
    assert ISSUE_FORMS, "no issue forms found; has .github/ISSUE_TEMPLATE moved?"
    for form in ISSUE_FORMS:
        text = form.read_text(encoding="utf-8")
        where = form.relative_to(ROOT)
        # Allow-listed, not deny-listed: `render: python`, `render: yaml` and `render: console`
        # read as executable too, and a list of four forbidden spellings says nothing about the
        # fifth. `text` is the one permitted value -- GitHub writes it as a fenced block with no
        # language, which is a quoted log and not a section of steps. A form that wants another
        # argues for it here, in the test, rather than in a pull request nobody reads twice.
        rendered = {value for value in re.findall(r"^\s*render:\s*(\S+)", text, re.M)}
        assert rendered <= {"text"}, (
            f"{where} renders a field a stranger fills in as {sorted(rendered - {'text'})!r}; "
            f"GitHub writes that into the issue body as a fenced block of that language, and an "
            f"agent working this tracker reads one as steps to run"
        )
        # The name matters on its own: the section heading is what an agent reads for intent,
        # whatever the field renders as.
        for heading in ("label: Validation", "label: Test Plan", "label: Testing"):
            assert heading not in text, (
                f"{where} names a field a stranger fills in {heading.split(': ')[1]!r}; an agent "
                f"working this tracker treats a section of that name as steps to run"
            )


#: The lanes whose brief carries `PUBLICATION_READ_BOUND`, which is the decision #85 asked for
#: written where the lane reads it. It is a subset of `GITHUB_SIDE_BY_DESIGN` and not the same
#: set: a lane may reach the GitHub side for a reason of its own -- the `public` set asks what
#: GitHub will serve once this repository is public, not what is written in a comment -- while
#: this bound is the one written for a lane that goes looking for live values in text anyone
#: can write, and it is the lanes carrying *it* that this module holds to the call list below.
PUBLICATION_READ_BOUND_LANES = {"gaps/publication", "fixes/publication"}

#: Every GitHub-side read those lanes may make, spelled as the workflow spells them. This is an
#: allow-list stated here rather than read back out of the workflow, for the reason AGENTS.md
#: gives: a pin that asserts a module equals itself moves with the module. Adding an entry is a
#: decision, and the question to answer in the same change is what an injected instruction
#: could do with it -- a lane's shell holds the operator's own `gh`, so `gh issue comment`
#: added here is a stranger's text reaching the tracker under the operator's name.
#:
#: Two entries carry a reason of their own. `gh api` names its method because `gh api`'s
#: default is not fixed: it is `GET` until a field is added and `POST` afterwards, so
#: `gh api <path> -f body=...` is a write that names no method for a checker to see. And the
#: `git` reads are here because the lane's own brief requires a history scan of the GitHub-side
#: refs -- a list of `gh` calls alone, declared to be the only calls the lane may make, would
#: read as cancelling the mirror clone that finds a credential on a pull-request head nothing
#: points at any more.
PUBLICATION_READ_CALLS = [
    "gh issue list",
    "gh issue view",
    "gh pr list",
    "gh pr view",
    "gh run list",
    "gh run view --log",
    "gh api -X GET",
    "git ls-remote origin",
    "git clone --mirror",
]


def test_the_publication_lanes_bound_and_record_their_github_side_read() -> None:
    """The exception to #80 is bounded and recorded, rather than being a whole side of GitHub.

    #80 moved the dedupe pass off the tracker; the `publication` lanes stayed on it, because
    what they audit -- a credential in an issue, a token in an Actions log -- is precisely the
    text a maintainer-authored listing drops. #85 asked whether that could be narrowed, and the
    answer written into the workflow is that the lane keeps the shell and the *calls* are
    bounded instead: relaying the corpus would cut it exactly where a value might be, and would
    copy every candidate secret into a prompt, a journal and the launching session's context,
    which is what this lane's own "never write a candidate value down" rule forbids.

    So three things have to hold together, and each is a way the bound goes quiet: the lanes
    that carry it are the lanes that read the GitHub side, the list of calls is read-only, and
    the lane's `coverage` is what says what it read -- which is what an operator reads the
    transcripts against afterwards.
    """
    source = WORKFLOW.read_text(encoding="utf-8")

    # Matched on the brief as written, before `_expand_constants` inlines anything: what is
    # pinned here is which lanes interpolate the bound, not which lanes end up mentioning `gh`.
    carries = set()
    sets = dict(re.findall(r"\n  (\w+): (\w+_LANES),", source))
    for set_name, array in sets.items():
        body = re.search(rf"const {array} = \[(.*?)\n\]\n", source, re.S)
        assert body is not None, f"no array named {array}"
        for key, brief in re.findall(r"key: '([^']+)',(.*?)\n  \}", body.group(1), re.S):
            if "${PUBLICATION_READ_BOUND}" in brief:
                carries.add(f"{set_name}/{key}")
    assert carries == PUBLICATION_READ_BOUND_LANES, (
        f"the lanes carrying the bounded-read brief are {sorted(carries)}, not "
        f"{sorted(PUBLICATION_READ_BOUND_LANES)}. A lane that acquires it acquires a shell "
        f"pointed at the GitHub side; a lane that loses it keeps the shell and loses the bound"
    )
    assert PUBLICATION_READ_BOUND_LANES <= GITHUB_SIDE_BY_DESIGN, (
        f"{sorted(PUBLICATION_READ_BOUND_LANES - GITHUB_SIDE_BY_DESIGN)} carries the bound on "
        f"reading the GitHub side without being one of the lanes that does it on purpose"
    )

    calls = _const_body_list(source, "PUBLICATION_READ_CALLS")
    assert calls == PUBLICATION_READ_CALLS, (
        f"the reads the publication lanes may make are {calls}, not {PUBLICATION_READ_CALLS}. "
        f"Widening that list is a decision: say in the same change what an injected "
        f"instruction could reach with the call being added"
    )
    # And each entry is read-only on its face, so that a list somebody edits stays one: the
    # equality above is the allow-list, this is what the allow-list is allowed to contain.
    for call in calls:
        assert call.startswith(("gh ", "git ")), f"{call!r} is neither a `gh` nor a `git` read"
        verb = re.search(r"\b(create|edit|close|comment|merge|delete|push|fetch)\b", call)
        assert verb is None, f"{call!r} names the write verb {verb.group(1)!r}"
        # `--input` reads a request body from a file, which is a write however it is spelled.
        # Matched as a substring, because `\b` before a dash never fires -- the anchor needs a
        # word character on its left and there is always a space there.
        assert "--input" not in call, f"{call!r} sends a request body"
        methods = set(re.findall(r"-X (\w+)", call)) | set(re.findall(r"--method (\w+)", call))
        assert methods <= {"GET"}, f"{call!r} permits the method {sorted(methods - {'GET'})}"
        # An explicit method for `gh api`, because its default is not one: `gh api` is a `GET`
        # until a field is added and a `POST` after that, so an entry reading `gh api` alone
        # admits `gh api repos/{owner}/{repo}/issues/1/comments -f body=...` -- a comment
        # posted under the operator's name by the one lane whose whole input is a stranger's
        # text. This is the check that would have caught the first draft of this very list.
        if call.startswith("gh api"):
            assert methods == {"GET"}, (
                f"{call!r} does not name its method, and `gh api`'s default is whichever of "
                f"GET and POST the rest of the arguments imply"
            )

    # And the lane is handed the list, not only the constant: the brief interpolates two names,
    # and what the agent reads is what they render to.
    briefs = _sweep_briefs(source)
    for name in sorted(PUBLICATION_READ_BOUND_LANES):
        rendered = re.sub(r"\s+", " ", briefs[name])
        for call in calls:
            assert call in rendered, f"{name}'s brief does not name {call!r}"

    bound = _const_body(source, "PUBLICATION_READ_BOUND")
    # The repository the sweep resolved, never a literal -- `author_association` is relative to
    # the repository in the URL, and a lane sent at a different tracker audits a different
    # project's text while reporting on this one (the same defect SKILL.md's phase 0 carries).
    assert "${repo}" in bound, (
        "the bound does not name the repository the sweep resolved, so what it bounds is "
        "`gh`'s idea of the current repository rather than the tree being audited"
    )
    literals = re.findall(r"repos/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", bound)
    assert not literals, f"the bound names the repository literal(s) {literals!r}"

    # The coverage record is what says what was read, and it is the half an operator can check:
    # a bound nobody can audit after the fact is a sentence in a prompt. Each surface by name,
    # because "say what you read" is what the `gaps` lane's brief already implied and did not
    # ask for -- its record came back as prose about the tree with no count of the GitHub side.
    flat_bound = re.sub(r"\s+", " ", bound)
    for surface in (
        "issues",
        "PR threads",
        "comments",
        "review comments",
        "Actions runs",
        "refs and commits",
    ):
        assert surface in flat_bound, (
            f"the coverage record the bound asks for does not name {surface!r}"
        )
    assert "coverage" in flat_bound and "counts, not adjectives" in flat_bound, (
        "the bound does not require counts in `coverage`, which is what makes an empty result "
        "mean `clean` here rather than `never looked`"
    )

    # And the operator's copy of the list is the lane's copy. SKILL.md is where the decision is
    # recorded for whoever runs the sweep; a list that drifts from the workflow's is a bound the
    # operator would audit the transcripts against and find nothing wrong with.
    skill = SKILL.read_text(encoding="utf-8")
    decision = skill[skill.index("**Handing the two `publication` lanes") :]
    decision = decision[: decision.index("\n\nAfter a run")]
    flat = re.sub(r"\s+", " ", decision)
    for call in calls:
        head = call.split(" (")[0].split(",")[0].strip()
        assert head in flat, f"SKILL.md's record of the decision does not name {head!r}"
    assert "-X GET" in flat, "SKILL.md's record does not say which method `gh api` may use"
    assert "coverage" in flat, (
        "SKILL.md's record of the decision does not say that the lane's coverage record is "
        "what says what it read"
    )


def test_the_post_run_audit_looks_for_what_a_stage_still_holds() -> None:
    """Scoping is not the whole control, so the audit covers what the profiles cannot.

    A shell can reach the network whatever the web tools say, and `gh` is on the `PATH` of
    every stage that holds one -- the four lanes `GITHUB_SIDE_BY_DESIGN` names, which are sent
    to the GitHub side on purpose, included. SKILL.md's audit is what stands behind those, so
    it names them.
    """
    skill = SKILL.read_text(encoding="utf-8")

    # The prose checklist, which is the audit. Each of these words is what it tells the auditor
    # to look for, and the bare word `gh` cannot stand for the half that matters -- listing is
    # fine, writing is not -- because "through" and "high" contain it.
    for bullet in (
        "write** verb",
        "git push",
        "WebFetch",
        "connector",
        "docker exec",
        # For a lane sent to the GitHub side on purpose, the *read* is the exposure, and every
        # bullet above asks what an agent wrote or where it went instead (#85).
        "read on the GitHub side",
    ):
        assert bullet in skill, f"the post-run audit's checklist does not name {bullet!r}"

    # And the command the skill offers for it, scoped to the command: a token in the prose above
    # says the auditor was told to look, not that the one-liner looks. One probe per thing rather
    # than the alternation's exact spelling, so that reordering the verbs or splitting the grep in
    # two does not fail this -- what is pinned is the reach, not the regex.
    commands = [line for line in skill.splitlines() if line.startswith("grep ")]
    assert len(commands) == 2, f"expected two audit commands in SKILL.md, found {len(commands)}"
    # Which is which, by what each looks for rather than by the order they appear in: one pass
    # whose every hit is a thing to explain, and one for the GitHub-side read, which is expected
    # to print lines and is read against the lane's own coverage record. Keying on the position
    # would let the two swap roles and leave both halves of this test asserting about one.
    verb_passes = [line for line in commands if "docker exec" in line]
    read_passes = [line for line in commands if "docker exec" not in line]
    assert len(verb_passes) == 1 and len(read_passes) == 1, (
        f"the audit's two commands are not one of each: {commands}"
    )
    # The second pass reaches every `gh` surface the bounded lanes read, by subcommand, because
    # a pass that named `api` alone would leave `gh run view --log` -- the Actions logs, the
    # surface the bound exists for -- out of what the operator is shown.
    for probe in ("gh ", "api", "issue", "pr", "run", "ls-remote", "clone", "uniq"):
        assert probe in read_passes[0], (
            f"the audit's GitHub-side read pass does not look for {probe!r}"
        )
    # Without its ERE escapes, so a probe reads as the thing looked for rather than as the
    # spelling: `\.credentials\.json` and `\bnc ` are what the command has to say.
    command = verb_passes[0].replace("\\", "")
    for probe in (
        "docker exec",
        "create", "edit", "close", "comment", "merge", "delete",  # `gh`'s write verbs
        "-X", "--method", "POST", "PATCH", "PUT", "DELETE",  # and `gh api`'s
        "git push",
        "WebFetch",
        "curl",
        "nc ",
        "mcp__",  # a connector call
        # The secret stores the checklist above the command names.
        ".credentials.json",
        "auth.json",
        ".config/gh",
        ".ssh",
        ".docker",
        ".env",
        "printenv",
        "environ",
    ):
        assert probe in command, f"the audit command does not look for {probe!r}"


# ─── Who reviews the text an agent reads ─────────────────────────────────────

CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

#: Every path whose contents reach an agent before it acts. A change to one of these is a
#: change to what the next unattended session is told to do, on a host holding two live
#: tokens, so each needs a named reviewer. `.github/` is here because it owns CI and this file
#: itself; `.claude/` because it is the harness's own; `docs/superpowers/` because everything
#: in it is text written to be executed.
AGENT_FACING_PATHS = (
    "/CLAUDE.md",
    "/AGENTS.md",
    "/README.md",
    "/CONTRIBUTING.md",
    "/SECURITY.md",
    "/.claude/",
    "/.github/",
    "/docs/superpowers/",
)


def _codeowner_patterns() -> dict[str, str]:
    """Each rule in the file as `pattern -> owners`, comments and blank lines dropped."""
    rules: dict[str, str] = {}
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        # Split on any run of whitespace: CODEOWNERS separates a pattern from its owners
        # with spaces or tabs, and reading a tab-separated line as one token would report an
        # owned path as unowned.
        pattern, *rest = text.split()
        rules[pattern] = " ".join(rest)
    return rules


@pytest.mark.parametrize("path", AGENT_FACING_PATHS)
def test_every_agent_facing_path_has_a_named_reviewer(path: str) -> None:
    """`CODEOWNERS` named four of these and not the rest (#78).

    An imperative added to `README.md` or to a document under `docs/superpowers/` reaches the
    next session exactly as one added to `CLAUDE.md` does; what differed was only whether
    GitHub would put the change in front of someone. Stated as a list here rather than derived
    from the tree, because "which files an agent reads" is a judgement and not a glob.
    """
    rules = _codeowner_patterns()
    assert path in rules, (
        f"{path} is read by an agent before it acts and has no owner in .github/CODEOWNERS. "
        "Add it there, or take it off this list and say why it is no longer agent-facing."
    )
    assert rules[path], f"{path} is named in CODEOWNERS with no owner"


def test_every_agent_facing_path_is_one_that_exists() -> None:
    """So the list above cannot quietly become a list of names that match nothing.

    A `CODEOWNERS` pattern for a path that has been moved or deleted is owned by nobody, and
    the parametrized test above would go on passing on the strength of the stale line.
    """
    for path in AGENT_FACING_PATHS:
        target = ROOT / path.strip("/")
        assert target.exists(), path
        assert target.is_dir() == path.endswith("/"), path
