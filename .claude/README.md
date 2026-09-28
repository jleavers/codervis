# What this repository ships under `.claude/`

- `skills/security-sweep/SKILL.md` — how an operator runs the sweep, and what they check after.
- `workflows/security-sweep.js` — the sweep itself: recon, lanes, refuters, triage, report.
- `agents/sweep-*.md` — the five stage tool profiles below.

This directory holds **no `settings.json`**, and that is deliberate; see below.

## Stage tool profiles for the security sweep

Five subagent definitions, one per stage of `workflows/security-sweep.js`. The workflow asks for
them by name through `agentType`, and it is the only thing in this repository that asks for one
at all — `CLAUDE.md`, `AGENTS.md`, `SKILL.md` and three test files describe and pin them. This
description of them sits here rather than in `agents/` because the harness reads every Markdown
file under that directory as a definition, and a README is not one.

**They constrain no session, and they grant no session anything it does not already hold.**
Be exact about what shipping them does, because the next person deciding whether another file
under `agents/` is free will read this: each definition is registered in every session started
in this checkout, and its description appears in that session's list of subagents, so anything
in the checkout can delegate to one by name — which is why every description says it is not for
general delegation. A subagent also cannot exceed the permissions of the session that launched
it, so none of this adds reach. What it is *not* is a project settings file, which applies to
every session in the directory whether that session wants it or not: one of those was shipped
for #21 and reverted in #34 for doing exactly that, and
`tests/test_agent_tooling_context.py` still fails if one reappears.

What they do is narrow the sweep's own agents. Before #44 every stage launched with whatever
the launching session held: `gh` already authenticated as the operator, WebFetch and `curl` to
anywhere, every connected MCP connector. The triage pass needs to read files and write one
JSON object; it now holds that and no shell at all.

| Profile | Stage | Holds |
| --- | --- | --- |
| `sweep-recon` | recon | read, write, shell; no web |
| `sweep-lane` | a scan lane and its refuters | read, write, edit, shell; no web |
| `sweep-lane-web` | a lane whose brief sends it to vendor documentation or an advisory database | the above, plus WebFetch and WebSearch |
| `sweep-triage` | triage and the completeness critic | read and write; no shell, no web |
| `sweep-report` | dedupe and the report | read and write; no shell, no web |

**A lane's shell can reach the network** whatever the web tools say, so "no WebFetch" bounds
the tool, not the host. That is the operator's to close, and it is why the post-run audit in
`.claude/skills/security-sweep/SKILL.md` is not optional.

The report stage held a shell until #80, and the reason it no longer does is worth keeping:
a tool list cannot say "read-only `gh`". Its dedupe was `gh issue list` and `gh pr list`, and
the only way to run them was a shell that could equally run `gh issue close` — and what those
listings returned was issue bodies with no author, from a tracker any GitHub account can write
to. The listing is now the launching session's to fetch and filter to maintainer-authored
items, and the workflow relays it to the stage through the fence like every other hand-off.

That is the dedupe pass and not the whole sweep: the `publication` lanes of the `gaps` and
`fixes` sets read the GitHub side unfiltered, because what they are auditing is what a stranger
can make public. `.claude/skills/security-sweep/SKILL.md` says so beside the audit that stands
behind them.

A tool list still cannot say "read-only `gh`" for those two, so their brief says it instead
(#85): the calls they may make are a named list in the workflow, read-only and on the
repository the sweep resolved, and what they read goes in their `coverage` record. Prose is
what that bound is made of, which is why the audit has a second pass that reads the record
against the transcripts — the profile is not what makes it true.

Which profile a lane gets is the `web` flag on the lane in the workflow, and the reason is a
comment beside it.

`Edit` is on the lane profiles and on no other, because a lane iterates on the probes and the
mutated copies it builds in its scratch directory — the `assurance` lane's brief is to remove
one rule at a time from a copy of `app/activity_gate.py` and re-run the gate tests. Recon and
the three later stages each write whole files and never revise one, so they hold `Write` alone.
Neither bounds where a file may be written: the prompts do that, and a shell could do either.
