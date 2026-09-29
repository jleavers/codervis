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

That is the dedupe pass and not the whole sweep. Five lanes read the GitHub side unfiltered,
for three reasons worth not blurring together. The `publication` lanes of the `gaps` and
`fixes` sets and the `public` set's `disclosure` lane (#89) audit what a stranger wrote, and a
listing filtered to maintainer-authored items is that evidence removed. The `public` set's
`outsiders` lane reads repository *state* — the settings, ruleset and collaborators that decide
what an account with no role can do once anyone can reach the repository — which exists on the
GitHub side and in no checkout. And the `unowned` set's `supply-chain` lane reads GitHub's copy
of the history, at the repository activity endpoint, because a force-pushed-over commit is
served by SHA long after a clone has stopped fetching it.

`.claude/skills/security-sweep/SKILL.md` says so beside the audit that stands behind them, and
is where the limit is written down too: the test reads what a brief *says*, so a lane reaching
GitHub without naming a command evades it, which is how `supply-chain` went unlisted until #91.
The list of five is also in `tests/test_agent_tooling_context.py`'s `GITHUB_SIDE_BY_DESIGN`,
off the workflow's source, and in `tests/test_sweep_relay.js`'s, off the prompt a stage is
really launched with. That first test also requires both documents to name every lane in the
set, so a new one is a change to both tests and to both documents rather than to the tests
alone (#91).

A tool list still cannot say "read-only `gh`" for any of them, so each of the five says it in
the brief instead (#85, #95, #96): the calls a lane may make are a named list in the workflow,
read-only on their face, and what it read goes in its `coverage` record. One list per lane
rather than one shared block, because a bound is a block of text and a lane that interpolates
another's name acquires the whole of that lane's reach. `public/outsiders` is the one lane sent
to a second repository, so `jleavers/issuebot` is named on its list and on no other's, which
`tests/test_agent_tooling_context.py` pins — that is what lets the audit tell a sent read from a
wandering one. Prose is what all of this is made of, which is why the audit has a second pass
that reads the record against the transcripts: the profile is not what makes it true.
`unowned/supply-chain` was the last one without a list — admitted to the allow-list by #91 with
the audit as the only thing behind it, and given one of its own by #96. It is where a `gh api`
entry first named the **path** it may ask for and not only its method: `gh api` reaches every
endpoint GitHub serves, so a bare `gh api -X GET` beside it is a deny-list of whatever the
author thought of. The three paths are the repository activity endpoint, the events window it
is compared against, and a commit by SHA; `git ls-remote origin` is the fourth entry, and the
`git clone --mirror` two of the other lanes may make is deliberately not among them. It is also
the one bound that closes the *web* route to the same surfaces, because this lane holds
`WebFetch` with no allow-list.

`public/outsiders` is written that way too since #102, and it is the lane the shape matters most
in: nothing on its list grants a *variable's* value, and GitHub serves one to anyone who can
read a public repository, so with a bare `gh api -X GET` on the list the closure had to be a
sentence in the bound naming `actions/variables` — a deny-list one level down, and
`environments/{name}/variables` is the endpoint it did not name. Its nine paths are the
repository object, Actions permissions and the default workflow token, branch protection,
collaborators, deploy keys, webhooks, private vulnerability reporting, and file contents for the
second repository; a variable is reached by `gh variable list --json name` and by nothing else,
and no path on the list returns a value, which is what closes the rest without naming one. The
`publication` lanes and `public/disclosure` keep the older shape, because their own entries —
`gh issue view`, `gh run view --log` — already bound what a bare `gh api -X GET` adds.

Which profile a lane gets is the `web` flag on the lane in the workflow, and the reason is a
comment beside it.

`Edit` is on the lane profiles and on no other, because a lane iterates on the probes and the
mutated copies it builds in its scratch directory — the `assurance` lane's brief is to remove
one rule at a time from a copy of `app/activity_gate.py` and re-run the gate tests. Recon and
the three later stages each write whole files and never revise one, so they hold `Write` alone.
Neither bounds where a file may be written: the prompts do that, and a shell could do either.
