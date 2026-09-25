# What this repository ships under `.claude/`

- `skills/security-sweep/SKILL.md` — how an operator runs the sweep, and what they check after.
- `workflows/security-sweep.js` — the sweep itself: recon, lanes, refuters, triage, report.
- `agents/sweep-*.md` — the five stage tool profiles below.

This directory holds **no `settings.json`**, and that is deliberate; see below.

## Stage tool profiles for the security sweep

Five subagent definitions, one per stage of `workflows/security-sweep.js`. The workflow asks for
them by name through `agentType`; nothing else in this repository refers to them. They live
beside the workflow rather than in this file's directory root because the harness reads every
Markdown file under `agents/` as a definition.

**They bind no session an operator starts in this checkout.** A subagent definition is a
profile that has to be asked for by name — it is not a project settings file, which applies to
every session in the directory whether it wants it or not. One of those was shipped for #21 and
reverted in #34 because it did exactly that, and `tests/test_agent_tooling_context.py` still
fails if one reappears.

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
| `sweep-report` | dedupe and the report | read, write, and a shell for two read-only `gh` listings |

Two things these profiles do not do, both of which are the operator's to close, and both of
which are why the post-run audit in `.claude/skills/security-sweep/SKILL.md` is not optional:

- **A tool list cannot say "read-only `gh`".** The report stage's dedupe is
  `gh issue list` and `gh pr list`, and the only way to run them is a shell that could equally
  run `gh issue close`. Its prompt names the two commands; the audit looks for write verbs.
- **A lane's shell can reach the network** whatever the web tools say, so "no WebFetch" bounds
  the tool, not the host.

Which profile a lane gets is the `web` flag on the lane in the workflow, and the reason is a
comment beside it.
