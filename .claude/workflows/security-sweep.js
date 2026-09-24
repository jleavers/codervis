export const meta = {
  name: 'security-sweep',
  description: 'Sweep the tree for security bugs, cluster them by root cause, check the tracker',
  phases: [
    { title: 'Recon', detail: 'one agent maps entry points, trust boundaries and secrets' },
    { title: 'Scan', detail: 'the selected threat-model lanes hunt in parallel' },
    { title: 'Verify', detail: 'an independent refuter per lane, hostile by default' },
    { title: 'Escalate', detail: 'a second refuter for confirmed critical and high findings' },
    { title: 'Triage', detail: 'cluster by root cause and name the invariant; find coverage gaps' },
    { title: 'Report', detail: 'dedupe against the tracker and write the report' },
  ],
}

if (!args || !args.runDir) {
  throw new Error('security-sweep needs args: {stamp, sha, repo, worktree, runDir, escalationCap}')
}
const { stamp, sha, repo, worktree, runDir } = args
const escalationCap = Number.isInteger(args.escalationCap) ? args.escalationCap : 3

// Which lane set to run. `baseline` is the four threat models a first sweep of this tree wants.
// When a run's completeness critic names surfaces nobody owned, add a second set here (issuebot
// calls its one `gaps`) rather than editing the baseline: the baseline is still what the next
// first-sweep-after-a-big-change wants. Keep new lanes threat-shaped -- a brief that is only a
// reading list produces coverage rather than attack paths, and coverage findings are the ones
// the refuters kill.
const laneSet = args.lanes || 'baseline'

// Optional prose naming what is already known and filed, so a lane does not spend itself
// re-deriving an issue that exists. Findings are still welcome where they go beyond it.
const known = args.known || ''

// --- schemas ---------------------------------------------------------------------------

const SEVERITIES = ['critical', 'high', 'medium', 'low']
const severityRank = (s) => {
  const i = SEVERITIES.indexOf(s)
  return i === -1 ? SEVERITIES.length : i
}
const str = { type: 'string' }
const sev = { type: 'string', enum: SEVERITIES }

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: str,
          dimension: str,
          file: str,
          line: { type: 'integer' },
          severity: sev,
          claim: str,
          why_it_matters: str,
          evidence: str,
          attack_path: str,
        },
        required: [
          'id', 'dimension', 'file', 'line', 'severity',
          'claim', 'why_it_matters', 'evidence', 'attack_path',
        ],
      },
    },
  },
  required: ['findings'],
}

const VERDICTS = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: str,
          refuted: { type: 'boolean' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          reasoning: str,
          corrected_severity: sev,
        },
        required: ['id', 'refuted', 'confidence', 'reasoning', 'corrected_severity'],
      },
    },
  },
  required: ['verdicts'],
}

const CLUSTERS = {
  type: 'object',
  properties: {
    clusters: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: str,
          root_cause: str,
          invariant: str,
          blast_radius: str,
          fix_shape: str,
          severity: sev,
          finding_ids: { type: 'array', items: str },
          dimensions: { type: 'array', items: str },
        },
        required: [
          'title', 'root_cause', 'invariant', 'blast_radius',
          'fix_shape', 'severity', 'finding_ids', 'dimensions',
        ],
      },
    },
    singletons: {
      type: 'array',
      items: {
        type: 'object',
        properties: { finding_id: str, why_unclustered: str, severity: sev },
        required: ['finding_id', 'why_unclustered', 'severity'],
      },
    },
  },
  required: ['clusters', 'singletons'],
}

const GAPS = {
  type: 'object',
  properties: {
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: { surface: str, why_it_matters: str, suggested_lane: str },
        required: ['surface', 'why_it_matters', 'suggested_lane'],
      },
    },
  },
  required: ['gaps'],
}

const DEDUPE = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          cluster_title: str,
          status: { type: 'string', enum: ['new', 'duplicate', 'related'] },
          issue_numbers: { type: 'array', items: { type: 'integer' } },
          reasoning: str,
        },
        required: ['cluster_title', 'status', 'issue_numbers', 'reasoning'],
      },
    },
    report_markdown: str,
  },
  required: ['verdicts', 'report_markdown'],
}

// --- shared prompt fragments -----------------------------------------------------------

// The one rule every agent in this sweep carries. This app's whole job is holding two live
// bearer tokens, and an agent that "just checks" the real credential file puts a live token
// into its own transcript and into this run directory. Reason about credentials from the
// code; prove behaviour with synthetic files.
const HANDS_OFF = `**Hands off the real credentials and the real endpoints.** Do not open, cat, grep, stat
or otherwise read the host's \`~/.claude/.credentials.json\`, \`~/.codex/auth.json\`, anything
else under \`~/.claude\` or \`~/.codex\`, or any \`*.log\` / \`latest\` debug capture outside the
worktree. Do not call claude.ai or chatgpt.com, and do not start the Docker stack. If you want
to demonstrate behaviour, run it against synthetic credential files and a stub server in a
temporary directory outside the worktree, with \`PYTHONDONTWRITEBYTECODE=1\` and pytest's
\`-p no:cacheprovider\` so nothing lands in the worktree. If a token-shaped string turns up in
the worktree or its history, cite its file, line and commit and quote at most its first six
characters -- never the whole value.`

const WHERE = `You are auditing the codervis repository at commit ${sha}, checked out read-only at:

    ${worktree}

Report every path repository-relative (\`app/quota.py\`), never absolute. Change nothing in the
worktree. You already have this repository's CLAUDE.md; use it for the layout and the provider
contracts rather than rediscovering them.

${HANDS_OFF}`

const writeBack = (name) => `

**Write this file before any other file you write, and before you return:**

    ${runDir}/${name}

Write exactly the object you are returning, pretty-printed. That file is this run's
crash-resistance record: if the session dies, the sweep resumes from what is on disk. A task
that also asks you for prose writes this JSON first and the prose second -- so that the two can
never disagree about what you concluded, and so that a crash between them costs the prose,
which can be regenerated, rather than the data, which cannot.`

// --- phase 1: recon --------------------------------------------------------------------

const reconPrompt = `${WHERE}

You are the recon pass for a security sweep. You find no vulnerabilities yourself; you build
the map the scanners will share, so that they name the same boundary the same way and their
findings can be clustered afterwards. Write Markdown to ${runDir}/01-surface-map.md and return
its full text.

Cover exactly four things, under these four headings:

## Entry points
Every place data enters the process, with file and line: every HTTP route (\`/\`,
\`/api/usage\`, \`/api/stream\`, \`/healthz\`, the \`/static\` mount), request headers and the
Host header, every environment variable read at import or call time, every file read under the
two bind-mounted data directories, both upstream HTTP responses (status, headers, redirects,
body), and the browser side -- \`window.__INITIAL_PAYLOAD__\`, SSE messages, \`localStorage\`.
For each, say who can influence it: anyone who can reach the published port, a web page open in
the operator's browser, the upstream vendor or whoever answers at the configured host, a local
process that can write under \`~/.claude\` or \`~/.codex\`, or the operator alone.

## Trust boundaries
Every place the provenance of data changes, named and located. The read-only bind mounts are
one. The "any failure -> \`LiveQuotaError\` / \`CodexLiveQuotaError\` -> \`unavailable\`" contract
is another. The activity readers' rule that they never read credentials (and, for Codex, never
read file contents) is a third. The container/host boundary and the browser's same-origin
policy are two more. For each, say what it asserts and what would breach it.

## Secrets
Every name that holds a credential or a credential-adjacent identifier (access tokens, account
ids, subscription/plan type), every place one is read, every header it is placed in, and every
place any part of one -- or an exception that might carry one -- is formatted into a string that
leaves the process: a response body, a log line, an SSE frame, a template.

## File inventory
A table of every tracked file in the tree with its line count, so a later pass can tell what
was never opened. Include the non-Python files: Dockerfile, docker-compose.yml, .dockerignore,
.gitignore, .env.example, .github/workflows/*, .github/dependabot.yml, requirements*.txt,
app/templates/*, app/static/*, README.md, CLAUDE.md, AGENTS.md, docs/**.

Be exhaustive and terse. This is a map, not an essay.`

// --- phase 2: the lanes ----------------------------------------------------------------

const BASELINE_LANES = [
  {
    key: 'tokens',
    title: 'where a bearer token can travel',
    brief: `codervis exists to hold two long-lived credentials -- Claude Code's
\`claudeAiOauth.accessToken\` and Codex's \`tokens.access_token\` plus \`account_id\` -- and to
present them to two undocumented endpoints. Your question is every place those values, or
anything derived from them, can end up other than the one intended request.

Cover, at minimum: \`CLAUDE_AI_HOST\` and \`CHATGPT_HOST\`, which re-point the bearer at whatever
they name -- is there any scheme or host check, and what does an \`http://\` value or a typo
cost; whether urllib's default redirect handling carries the \`Authorization\` and
\`ChatGPT-Account-Id\` headers to wherever a 3xx points, including a different host; the Codex
client's fall-through to \`USAGE_PATH_ALT\` on 401/403/404, which sends the same bearer a second
time; every \`LiveQuotaError\` / \`CodexLiveQuotaError\` message, since \`source_error\` is shipped
verbatim to the page, the SSE stream and \`/api/usage\` -- can any of them carry file contents,
a header, an upstream body or a token fragment; the \`raw\` payload kept on each snapshot; and
what \`/healthz\` reveals about which credentials exist.

Then history, not only the tree: \`git log -p\`, \`git log --all --full-history\` and
\`git rev-list --objects --all\` for token-shaped strings, \`.env\` files, \`*.log\` /
\`usage-debug.log\` / \`latest\` debug captures (the \`.gitignore\` comment says they may hold
OAuth tokens), and test fixtures. The Gemini, Cursor and Copilot providers were removed in
a6d91b6; read what they read and whether anything they committed -- fixtures, sample
responses, captured credentials -- is still reachable in history. The repository is private
today; assume it will not always be.

A credential that is real and live is \`critical\` however it got there.`,
  },
  {
    key: 'exposure',
    title: 'what anyone who can reach the port can read or do',
    brief: `The dashboard has no authentication, and docker-compose.yml publishes it on every
host interface by default. README "Security notes" acknowledges the LAN reach; your job is to
establish exactly what it gives away and who else can get it.

Start with the attackers: a machine on the same network; another container on the same Docker
host; and -- the one people forget -- any web page open in the operator's browser, which can
reach \`localhost:8765\` and, via DNS rebinding, read it as same-origin unless something checks
the \`Host\` header. Enumerate what each learns from \`/\`, \`/api/usage\`, \`/api/stream\` and
\`/healthz\`: plan type, reset times, \`last_activity\` (a presence signal: when the operator is at
the keyboard), \`source_error\` strings and the paths inside them, which credential files exist.

Then the rendering path: Jinja autoescape in \`app/templates/index.html\`, the
\`{{ data | tojson }}\` inside a \`<script>\` block, and every DOM write in \`app/static/app.js\`
and \`widget-state.js\` -- which fields are upstream- or file-controlled, and does any reach an
HTML, attribute or style sink unescaped. Then response headers: CSP, \`X-Frame-Options\` /
\`frame-ancestors\`, \`X-Content-Type-Options\`, and CORS -- present or absent, and what each
absence permits here. Then the SSE loop in \`main.py:stream()\`: what one client holding many
connections costs the server, and whether anything bounds it.

Severity is about this deployment's real reach, not about "unauthenticated" as a word: say who
the attacker is and what they walk away with.`,
  },
  {
    key: 'hostile-input',
    title: 'untrusted bytes reaching the parsers, and the unavailable contract',
    brief: `The project's load-bearing rule is that any failure in either live client becomes that
provider's \`unavailable\` state and nothing worse. Your attacker controls bytes the parsers
read and wants something worse: a 500 on every route, a dead SSE stream, one provider's fault
taking the other's card down, a wedged event loop, or a number on the dashboard that is not
the truth.

The inputs are: the two upstream responses (status, headers, redirect targets and body -- the
vendor, or whoever answers at an overridden host); the two credential files (a local process,
or a CLI update, can change their shape); Claude project transcripts under \`projects/**.jsonl\`,
which record tool output and fetched content, so part of their bytes are attacker-authored;
and the Codex \`sessions/\` and \`archived_sessions/\` trees.

Trace every exception that can leave \`LiveQuotaClient._fetch\`, \`CodexLiveQuotaClient._fetch\`,
\`ClaudeActivityReader.snapshot\` and \`CodexActivityReader.snapshot\` without being converted --
a top-level JSON array where an object is expected, a string where a dict is expected (in the
response and in the credential file), a read timeout or connection reset raised while reading
the body rather than at connect, an \`OverflowError\` from datetime arithmetic on an extreme
timestamp -- and follow it up through \`_claude_section\`, \`_codex_section\` and
\`_build_payload\`: the activity snapshots are taken outside the \`try\`. Compare the two
\`_float_field\` implementations: Claude's rejects booleans and non-finite values and Codex's
does not; follow a NaN or Infinity to every place a payload is serialised, since
\`JSONResponse\` and the SSE \`json.dumps\` do not treat it the same way. Then size and time: an
unbounded \`resp.read()\`, \`rglob\` over trees a local process can grow or fill with symlinks,
and whole-file reads of every changed transcript -- all of which run synchronously inside
\`async def\` routes, on the event loop, while holding a client lock.

For this lane in particular, \`attack_path\` must name the specific attacker-controlled input
and the specific line where it breaks the contract.`,
  },
  {
    key: 'deploy',
    title: 'what the image, the compose file and CI give a reader who copies them',
    brief: `Readers copy a small, working Docker dashboard wholesale, including the parts whose
safety depends on assumptions the author holds and never wrote down. Your question is what a
reader inherits by running this as documented, and what happens when they apply it somewhere
the author's assumptions do not hold.

Look at, at minimum: the Dockerfile's user (is there a \`USER\` line), base image pinning, and
what \`pip install\` trusts; docker-compose.yml's published port and bind address, its
\`restart\` policy, and the two bind mounts -- which carry the *whole* of \`~/.claude\` and
\`~/.codex\` (every transcript, history file and config) into a network-facing process that
needs one credential file and some timestamps; \`.env.example\` and its \`\${USERPROFILE}\`
defaults on a host where that is unset; \`.dockerignore\` against what a working checkout
actually holds (debug captures, \`.env\`, \`.claude/\`); and the pip, actions and docker update
policy in \`.github/dependabot.yml\`.

Then \`.github/workflows/ci.yml\`: job permissions, actions pinned by tag rather than digest,
the \`type=gha\` build cache written from \`pull_request\` builds and whether a fork can reach a
scope a \`main\` build reads, and any interpolation of attacker-controllable text into a
\`run:\` step. Then the dependency surface: \`requirements.txt\` pins exact versions without
hashes; \`requirements-dev.txt\` uses ranges, and its \`httpx\` requirement became \`httpx2\` in
3152556 -- establish what that package is, who publishes it, and whether it is what Starlette's
\`TestClient\` needs, rather than assuming either way.

Weight prose equally with code. A README, CLAUDE.md or AGENTS.md passage that teaches a pattern
by example without stating the boundary that makes it safe -- running \`claude --debug-file\`
to capture API traffic, say, or exposing the port -- is a finding in this lane, and its
\`file\` and \`line\` are the passage.`,
  },
]

const LANE_SETS = { baseline: BASELINE_LANES }
const LANES = LANE_SETS[laneSet]
if (!LANES) {
  throw new Error(`unknown lane set ${laneSet}; expected one of ${Object.keys(LANE_SETS).join(', ')}`)
}

const scanPrompt = (lane) => `${WHERE}

Read ${runDir}/01-surface-map.md before anything else. It is the shared map: use its names for
entry points and boundaries, so that findings from ${LANES.length} lanes can be clustered afterwards.

Your lane is **${lane.key}** — ${lane.title}. Hunt only here. Another agent owns each of the
other lanes; a finding outside yours is their job, not a bonus.

${lane.brief}${known ? `\n\nAlready known in this tree, across every lane:\n\n${known}` : ''}

Rules that decide whether something is a finding at all:

- \`attack_path\` is required prose: who the attacker is, what they control, and the sequence
  by which they reach the line you are citing. If you cannot write it, you have not found a
  vulnerability — drop it.
- A claim that is true of Python, FastAPI, Docker or security in general, but which you cannot
  reach in THIS tree, is not a finding. Reaching it means citing the file and the line.
- \`evidence\` quotes the code or command output you are relying on, not a paraphrase of it.
- Severity: \`critical\` if it is exploitable now with real consequence; \`high\` if it is
  exploitable given a plausible precondition; \`medium\` if it weakens a defence without being
  exploitable on its own; \`low\` otherwise.
- Give each finding an \`id\` of \`${lane.key}-1\`, \`${lane.key}-2\`, and so on, and set
  \`dimension\` to \`${lane.key}\`.

Finding nothing is an acceptable result and much better than padding. Return an empty
\`findings\` array rather than a weak one.${writeBack(`02-findings-${lane.key}.json`)}`

// --- phase 3: refutation ---------------------------------------------------------------

const verifyPrompt = (lane, findings) => `${WHERE}

You are an independent refuter. You did not write these findings, you have no stake in them,
and your job is to destroy the ones that do not survive contact with the code.

**Your default is refuted.** A finding survives only if you can follow its attack path in this
tree yourself and reach the same conclusion. If you are uncertain, it is refuted. If it is true
in general but you cannot reach it here, it is refuted. If its evidence does not say what the
finding claims it says, it is refuted.

Read the code. Do not reason from the finding's own text — it is a claim, not a source.

For each finding return a verdict carrying the same \`id\`:

- \`refuted\`: true or false.
- \`confidence\`: how sure you are of the verdict itself.
- \`reasoning\`: what you checked and what you concluded. For a refutation, say what is
  actually true instead.
- \`corrected_severity\`: the severity you would give it. You may downgrade a surviving finding
  rather than face a binary you would resolve by keeping it. Echo the original if you agree.

Findings to refute (lane ${lane.key}):

${JSON.stringify(findings, null, 2)}${writeBack(`03-verdicts-${lane.key}.json`)}`

const escalatePrompt = (finding) => `${WHERE}

One finding has already survived a refuter and is rated ${finding.severity}. Before it reaches
a human it gets a second, independent attempt at refutation, and you are it. You have not been
shown the first refuter's reasoning, deliberately.

**Your default is refuted**, on the same terms: follow the attack path in the code yourself, or
refute it. A finding this severe that turns out to be wrong is more expensive than one that is
missed, because it is the one that gets acted on.

Return a single verdict, inside the \`verdicts\` array, carrying this finding's \`id\`.

${JSON.stringify(finding, null, 2)}${writeBack(`03-escalated-${finding.id}.json`)}`

// --- phase 4: triage and the completeness critic ---------------------------------------

const triagePrompt = (survivors) => `${WHERE}

You are the triage pass, and you are the reason this sweep exists. Below are the findings that
survived refutation. Your job is to stop them becoming a list of small patches.

A sweep that emits one issue per finding produces a round of local fixes, and those fixes are
the next sweep's findings, because nothing in the loop ever names the property that was
missing. So: **cluster by root cause, and name the invariant.**

You are forbidden from emitting one cluster per finding. Every cluster carries four things, and
a cluster missing any of them is not a cluster:

- \`root_cause\`: the single decision, or the single absence, that produced every finding in
  it. Not a category ("input validation") — a cause ("each parser trusts the JSON type of the
  upstream body at its own call site, with no single point that turns a shape mismatch into
  \`unavailable\`").
- \`invariant\`: the property which, enforced in one place, would make every finding in the
  cluster impossible. One sentence. This is the field that matters most. If you cannot state
  it, the cluster is either several clusters or it is nothing — decide which, and act on it.
- \`blast_radius\`: which threat model it lands in, and who is hurt — this operator's
  deployment, or a reader who copied the repository. Those have different urgencies and must
  not be blurred.
- \`fix_shape\`: WHERE the invariant would live. No diffs, no patches, no code. A diff in a
  security report is an invitation to apply it, and applying six diffs is the treadmill this
  field exists to prevent.

Set \`severity\` to the highest severity among the cluster's findings, \`finding_ids\` to every
id in it, and \`dimensions\` to the lanes they came from — a cluster spanning two lanes is
usually the most valuable kind.

A finding that genuinely resists clustering goes in \`singletons\`, with \`why_unclustered\`
saying what makes it isolated. Use this sparingly: it is the escape hatch for the one truly
standalone bug, and if most findings end up there you have not done the work.

Findings that survived refutation:

${JSON.stringify(survivors, null, 2)}

Return the JSON object and write nothing else. Do not also write a Markdown version: the report
pass renders the prose from exactly what you return, so a second representation written here
could only drift from it.${writeBack('04-clusters.json')}`

const criticPrompt = (allFindings, judged) => `${WHERE}

You are the completeness critic. ${LANES.length} scanners have finished, running the
\`${laneSet}\` lane set (${LANES.map((l) => l.key).join(', ')}). Your only question is: **what
was never looked at?**

Read ${runDir}/01-surface-map.md, and the raw findings below. Then find the holes:

- Files in the map's inventory that no finding cites and that no lane's brief plainly covers.
  Weight by what a file does, not by its size: a twenty-line file that decides what the
  container can reach matters more than a thousand-line plan document.
- Entry points in the map that no \`attack_path\` mentions.
- Surfaces that exist in the map but fall between the ${LANES.length} lanes, so that nobody
  owned them.

For each gap give \`surface\` (the file, route or boundary), \`why_it_matters\` (what could be
there), and \`suggested_lane\` — one of ${LANES.map((l) => `\`${l.key}\``).join(', ')}, or
\`new\` if it needs a lane none of these briefs would cover.

A gap that an earlier run already named and that this run still did not reach is worth naming
again, and worth saying so: a surface nobody has owned across two sweeps is a stronger signal
than a fresh one. Earlier runs, if any, are siblings of ${runDir}; read their \`04-gaps.md\`.

Do not audit the gaps yourself; naming them is the whole job. Your output seeds the next
sweep's briefs.

**Arithmetic you may state, and nothing beyond it.** Every number you put in your prose must
come from the two lists below or from a command you actually ran against the worktree. There
were **${allFindings.length}** findings this run, of which
**${judged.filter((v) => v.refuted).length}** were refuted by their lane's refuter (a separate
escalation pass may since have killed more, and you are not shown it). Do not recompute those
figures and do not state any other claim about verdicts: a surface that was examined and
cleared is not a gap, and getting that backwards is the one way this report misleads the next
sweep. If you want a coverage figure, derive it by running a command, and say which.

Findings produced this run, each with the refuter's verdict:

${JSON.stringify(
    allFindings.map((f) => {
      const v = judged.find((x) => x.id === f.id)
      return { ...f, verdict: v ? { refuted: v.refuted, reasoning: v.reasoning } : 'no verdict' }
    }),
    null,
    2,
  )}

Write your gaps as Markdown to ${runDir}/04-gaps.md and return the JSON object.`

// --- phase 5: dedupe and report --------------------------------------------------------

const reportPrompt = (clusters, singletons, gaps, counts) => `${WHERE}

Two jobs, in order.

**First, dedupe.** For every cluster below, search the tracker of \`${repo}\` before it can be
proposed as new:

    gh issue list --repo ${repo} --state all --limit 200 --json number,title,state,labels,body
    gh pr list --repo ${repo} --state all --limit 100 --json number,title,state,body

Closed issues matter more than open ones here: what you are looking for is something already
reported and fixed, or reported and forgotten. Match on the invariant, not on wording — a
cluster is a duplicate when an existing issue would be closed by the same fix. Return, per
cluster, a \`status\` of \`new\`, \`duplicate\` or \`related\`, the \`issue_numbers\` you
matched (empty for \`new\`), and \`reasoning\`. An empty tracker is a valid answer: say so in
the report rather than implying a search found nothing to match.

Previous sweeps, if any, are siblings of ${runDir}. Read each one's \`06-filed.json\` if it
exists: a cluster that matches something filed there is that issue, not a new one.

**Second, compose the report** as Markdown and return it in \`report_markdown\`. Do not write
it to a file yourself -- the harness refuses report files from subagents -- and do not look
for another way to write it: the session that launched this sweep writes it to
\`${runDir}/report-${stamp}.md\`. Refer to it by that name if the report needs to mention
itself. A human reads this to decide what to file, so lead with what they must decide. In this
order:

1. A header: the swept commit \`${sha}\`, the stamp \`${stamp}\`, and the repository.
2. The funnel as a table — findings per lane, refuted, confirmed, escalated, clustered. The
   numbers are ${JSON.stringify(counts)}.
3. The clusters in severity order, numbered 1..N in the order you present them. Every reference
   to a cluster anywhere else in the report -- in the summary at the top especially -- uses that
   same number. A summary that says "file cluster 2" while section 2 is a different cluster is
   worse than no summary.
4. The singletons, with why each is unclustered, and a note that only \`critical\` singletons
   are proposed for filing.
5. The coverage gaps, verbatim from the critic — this is what the next sweep starts from.

The report must not contain a credential, even a redacted-looking one beyond six characters.

Clusters:
${JSON.stringify(clusters, null, 2)}

Singletons:
${JSON.stringify(singletons, null, 2)}

Coverage gaps:
${JSON.stringify(gaps, null, 2)}${writeBack('05-dedupe.json')}`

// --- the pipeline ----------------------------------------------------------------------

log(`sweeping ${repo} at ${sha}`)
log(`worktree ${worktree}`)
log(`artefacts ${runDir}`)

phase('Recon')
const surfaceMap = await agent(reconPrompt, { label: 'recon', phase: 'Recon' })
if (!surfaceMap) {
  throw new Error('recon produced no surface map; every later phase reads it')
}

const lanes = await pipeline(
  LANES,
  (lane) => agent(scanPrompt(lane), {
    label: `scan:${lane.key}`, phase: 'Scan', schema: FINDINGS,
  }),
  (scan, lane) => {
    // A scan agent that died returns null. Keep that distinct from "found nothing", or a
    // dead lane reads in the funnel as a clean one.
    if (!scan) return { lane, findings: [], verdicts: [], dead: true }
    const findings = scan.findings || []
    if (!findings.length) return { lane, findings, verdicts: [] }
    return agent(verifyPrompt(lane, findings), {
      label: `verify:${lane.key}`, phase: 'Verify', schema: VERDICTS,
    }).then((v) => ({ lane, findings, verdicts: v && v.verdicts ? v.verdicts : [] }))
  },
)

const ok = lanes.filter((r) => r && !r.dead)
const dead = LANES.filter((l) => !ok.some((r) => r.lane.key === l.key))
if (dead.length) log(`lanes that produced nothing usable: ${dead.map((l) => l.key).join(', ')}`)

const allFindings = ok.flatMap((r) => r.findings)
const verdictFor = new Map()
for (const r of ok) {
  for (const v of r.verdicts) verdictFor.set(v.id, v)
}

const unjudged = allFindings.filter((f) => !verdictFor.has(f.id))
if (unjudged.length) {
  log(`${unjudged.length} finding(s) got no verdict and are dropped as unconfirmed: ${unjudged.map((f) => f.id).join(', ')}`)
}

const confirmed = allFindings
  .filter((f) => {
    const v = verdictFor.get(f.id)
    return v ? !v.refuted : false
  })
  .map((f) => {
    const v = verdictFor.get(f.id)
    return { ...f, severity: v.corrected_severity || f.severity, verified_by: v.reasoning }
  })

log(`${allFindings.length} found, ${confirmed.length} survived refutation`)

phase('Escalate')
const candidates = confirmed
  .filter((f) => f.severity === 'critical' || f.severity === 'high')
  .sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || a.id.localeCompare(b.id))
const taken = candidates.slice(0, escalationCap)
const skipped = candidates.slice(escalationCap)
if (skipped.length) {
  log(`escalation cap ${escalationCap}: ${taken.length} of ${candidates.length} critical/high finding(s) get a second refuter; ${skipped.length} do not (${skipped.map((f) => f.id).join(', ')})`)
}

const second = taken.length
  ? await parallel(taken.map((f) => () => agent(escalatePrompt(f), {
    label: `escalate:${f.id}`, phase: 'Escalate', schema: VERDICTS,
  })))
  : []

const killed = new Set()
for (const r of second.filter(Boolean)) {
  for (const v of r.verdicts || []) {
    if (v.refuted) killed.add(v.id)
  }
}
if (killed.size) log(`second refuter killed: ${[...killed].join(', ')}`)
const survivors = confirmed.filter((f) => !killed.has(f.id))

phase('Triage')
const [clusterResult, gapResult] = await parallel([
  () => (survivors.length
    ? agent(triagePrompt(survivors), { label: 'triage', phase: 'Triage', schema: CLUSTERS })
    : Promise.resolve({ clusters: [], singletons: [] })),
  () => agent(criticPrompt(allFindings, [...verdictFor.values()]), {
    label: 'critic', phase: 'Triage', schema: GAPS,
  }),
])
if (!survivors.length) log('nothing survived refutation; triage skipped, the critic still runs')

const clusters = clusterResult && clusterResult.clusters ? clusterResult.clusters : []
const singletons = clusterResult && clusterResult.singletons ? clusterResult.singletons : []
const gaps = gapResult && gapResult.gaps ? gapResult.gaps : []

const counts = {
  by_lane: Object.fromEntries(ok.map((r) => [r.lane.key, r.findings.length])),
  found: allFindings.length,
  refuted: allFindings.length - confirmed.length,
  confirmed: confirmed.length,
  escalated: taken.length,
  killed_on_escalation: killed.size,
  survivors: survivors.length,
  clusters: clusters.length,
  singletons: singletons.length,
}

phase('Report')
const dedupe = (clusters.length || singletons.length)
  ? await agent(reportPrompt(clusters, singletons, gaps, counts), {
    label: 'dedupe-report', phase: 'Report', schema: DEDUPE,
  })
  : null
if (!dedupe) log('nothing survived to report; the run directory still holds every raw finding and 04-gaps.md')

return {
  stamp,
  sha,
  repo,
  counts,
  clusters,
  singletons,
  gaps,
  dedupe: dedupe && dedupe.verdicts ? dedupe.verdicts : [],
  // Subagents may not write report files, so the report comes back as text and the launching
  // session writes it to report_path (see SKILL.md, phase 6).
  report_path: dedupe && dedupe.report_markdown ? `${runDir}/report-${stamp}.md` : null,
  report_markdown: dedupe && dedupe.report_markdown ? dedupe.report_markdown : null,
  run_dir: runDir,
}
