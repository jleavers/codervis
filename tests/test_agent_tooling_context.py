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

DOCS_DIR = ROOT / "docs" / "superpowers"
PLANS_DIR = DOCS_DIR / "plans"
WORKFLOW = ROOT / ".claude" / "workflows" / "security-sweep.js"
SKILL = ROOT / ".claude" / "skills" / "security-sweep" / "SKILL.md"
AGENTS_DIR = ROOT / ".claude" / "agents"

# What each stage's agent may hold. The value is the exact `tools:` list its definition
# declares, in order, because "the triage pass has no shell" is the whole point of the file and
# a tool added to it is a decision, not a detail. `Bash` on the report stage is the one
# residual the tool layer cannot express: its dedupe is two read-only `gh` listings, and a
# shell that can run those can run `gh issue close` too, which is why SKILL.md's post-run audit
# looks for write verbs.
STAGE_TOOLS = {
    "sweep-recon": "Read, Glob, Grep, Bash, Write",
    "sweep-lane": "Read, Glob, Grep, Bash, Edit, Write",
    "sweep-lane-web": "Read, Glob, Grep, Bash, Edit, Write, WebFetch, WebSearch",
    "sweep-triage": "Read, Glob, Grep, Write",
    "sweep-report": "Read, Glob, Grep, Write, Bash",
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
#: named profile.
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
#: root. Root-relative rather than anchored under `.claude/`, because two of them are not
#: there: Claude Code reads the project-scoped MCP declaration from `.mcp.json` at the root,
#: and a plugin manifest from `.claude-plugin/`. A glob written as `mcp.json` under `.claude/`
#: matches nothing at all, which is the "register that covers every name the harness honours"
#: failing in exactly the way #78 is about.
HARNESS_CONFIG_GLOBS = (
    ".claude/settings.json",
    ".claude/settings.*.json",
    ".claude/hooks/**/*",
    ".mcp.json",
    ".claude-plugin/**/*",
)

#: The keys that make one of those files run something in every session started in this
#: checkout. `env` is beside `hooks` because it is the other key that reaches a command's
#: execution rather than describing what a session may do, and `mcpServers` because a declared
#: server is a process the harness starts.
SESSION_BINDING_KEYS = ("hooks", "env", "mcpServers")

#: Paths whose *location* is what binds, so there is no key to look for: a hook script is a
#: script, and a plugin manifest brings its own directory with it.
BINDING_BY_LOCATION = (".claude/hooks/", ".claude-plugin/")


def _harness_config_files() -> set[str]:
    """Whatever is present matching one of those, tracked or not."""
    return {
        path.relative_to(ROOT).as_posix()
        for pattern in HARNESS_CONFIG_GLOBS
        for path in ROOT.glob(pattern)
        if path.is_file()
    }


def _binds_the_session(path: Path) -> str | None:
    """The key by which a harness-config file runs something, or `None` if it declares none.

    Read rather than assumed, because `.gitignore` names `.claude/settings.local.json` as
    per-user session state: the harness writes one the first time an operator approves a
    permission in this checkout, and failing the suite on its *presence* would fail every
    developer for doing the thing AGENTS.md says is theirs to do. What the repository's rule
    is actually about is text that runs -- the `SessionStart` hook #78 names -- so that is
    what is looked for. A committed one of any shape is caught by the tracked-set equality
    above, which is where "the repository ships no agent configuration" really lives.
    """
    relative = path.relative_to(ROOT).as_posix()
    for directory in BINDING_BY_LOCATION:
        # A file the harness executes *because of where it sits* -- a hook script, a plugin
        # manifest -- is not JSON to inspect, and its content is beside the point.
        if relative.startswith(directory):
            return f"sits under {directory}, which the harness runs from"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A settings path that is not readable JSON is not something this test can clear, so
        # it says so rather than passing it over.
        return "is at a harness-configuration path and could not be read as JSON"
    if not isinstance(loaded, dict):
        return None
    for key in SESSION_BINDING_KEYS:
        if loaded.get(key):
            return key
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
        if path.relative_to(ROOT).parts[:1] == (".claude",)
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
    shipped = {path.relative_to(ROOT).as_posix() for path in _shipped_files()}
    committed = sorted(shipped & _harness_config_files())
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
    binding = {
        name: reason
        for name in sorted(_harness_config_files())
        if (reason := _binds_the_session(ROOT / name)) is not None
    }
    assert not binding, (
        f"{binding} runs something in every session started in this checkout. The operator's "
        "agent environment is theirs to configure (#21, #34, #35), and a file that merely "
        "records their own permission choices is part of that -- but a `hooks` or `env` key "
        "is a command, and belongs in their own settings rather than at this path."
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
    assert "Web" not in held["sweep-report"], "the report stage has acquired the web"
    assert "Web" not in held["sweep-lane"], (
        "the default lane profile has acquired the web; a lane whose brief needs it declares "
        "`web: true` and gets sweep-lane-web"
    )


def test_the_post_run_audit_looks_for_what_a_stage_still_holds() -> None:
    """Scoping is not the whole control, so the audit covers what the profiles cannot.

    A shell can reach the network whatever the web tools say, and the report stage's `gh` can
    write as well as list. SKILL.md's audit is what stands behind those, so it names them.
    """
    skill = SKILL.read_text(encoding="utf-8")

    # The prose checklist, which is the audit. Each of these words is what it tells the auditor
    # to look for, and the bare word `gh` cannot stand for the half that matters -- listing is
    # fine, writing is not -- because "through" and "high" contain it.
    for bullet in ("write** verb", "git push", "WebFetch", "connector", "docker exec"):
        assert bullet in skill, f"the post-run audit's checklist does not name {bullet!r}"

    # And the command the skill offers for it, scoped to the command: a token in the prose above
    # says the auditor was told to look, not that the one-liner looks. One probe per thing rather
    # than the alternation's exact spelling, so that reordering the verbs or splitting the grep in
    # two does not fail this -- what is pinned is the reach, not the regex.
    commands = [line for line in skill.splitlines() if line.startswith("grep -nE")]
    assert len(commands) == 1, f"expected one audit command in SKILL.md, found {len(commands)}"
    # Without its ERE escapes, so a probe reads as the thing looked for rather than as the
    # spelling: `\.credentials\.json` and `\bnc ` are what the command has to say.
    command = commands[0].replace("\\", "")
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
