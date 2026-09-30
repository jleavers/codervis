# Contributing

Thanks for looking. This is a personal project run by one maintainer, so the honest
expectation is: issues and small pull requests are welcome, and a large one is worth raising as
an issue first rather than writing on spec.

[`AGENTS.md`](AGENTS.md) is the binding set of rules in this repository, and it applies to
people as well as to agents. What follows is the practical version.

## Getting set up

Python 3.13 or newer, Node 24 for the JavaScript tests, and Docker with Compose v2 for the
container stack and the egress check. On Windows, use WSL.

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest                          # hermetic: no network, no credential reads
ruff check --select E4,E7,E9,F .          # what the lint job runs
node --test 'tests/test_*.js'
```

`tests/test_compose_topology.py` renders `docker-compose.yml` with `docker compose config`,
which needs the Docker CLI but no daemon, and skips where Docker is not installed. The egress
bound is checked from inside a running stack:

```bash
docker compose up --build -d
docker compose exec codervis python -m app.egress check
docker compose down
```

The day-to-day commands and the architecture are in [`CLAUDE.md`](CLAUDE.md), which is
written for an agent working in this repository but is the best map of the code for a person
too.

## One rule above the others

**Never read a credential file to check something.** Every command in this repository runs on
the host that holds `~/.claude/.credentials.json` and `~/.codex/auth.json`, the two live tokens
the dashboard exists to display. The tests use temporary directories and stubbed clients, and
they must stay that way; a test that needs a credential-shaped file writes its own. The same
goes for the live endpoints: nothing in the test suite calls them, and nothing you run to
reproduce a bug should either. If you need to see what an endpoint returns, `docker compose
logs codervis` and `/api/usage` show you what the dashboard made of it, and that is the layer
the code is written against.

## Pull requests

Everything reaches `main` through a pull request. Without write access to this repository you
will be working from a fork, which comes to the same thing: push your branch there and open the
pull request across. With write access, push a branch here and open one — `main` carries a
ruleset that refuses a direct push, a force push and a deletion, so there is nothing to
remember.

A pull request needs one approving review to merge, and nobody can approve their own, so a
contributor's pull request waits for the maintainer's. The maintainer's own pull requests have
no second reviewer to wait for: a repository admin may merge a pull request without the
approval, and GitHub records that as a bypass of the rule. The bypass covers pull requests only,
so a direct push is refused to an admin too. Either way, review is a person reading the diff,
and the approval is where that is recorded.

- Say what changed and why. A reviewer reading the diff alone should not have to guess the
  motivation.
- Keep the tests green, and add one for behaviour you change. Most of this repository's tests
  pin a decision rather than a line of code, and the comment explaining *why* is as much the
  point as the assertion.
- Prose in the documentation and in comments explains the reasoning, not just the mechanism.
  That is deliberate: much of this code exists because a subtler approach was wrong, and the
  note saying so is what stops it coming back.

## Conventions worth knowing before you trip over them

**Both providers, or an explicit skip.** `tests/test_payload_contract.py` runs the Claude and
Codex clients through one matrix of transport faults, hostile bodies and hostile credential
files. A case added there runs for both; a shape one provider cannot have is skipped
explicitly for the other, so the gap shows in the test report rather than quietly testing a
single client.

**Assert on what a reader touched, not on what it returned.** A reader that opens a credential
file returns the same timestamp as one that does not. `tests/test_activity_readers.py`
therefore asserts the gate's own record of admitted and refused paths, and the audit hook in
`tests/conftest.py` records every file open and directory listing the process makes. CPython
raises no audit event for a stat, so stats are pinned structurally instead:
`tests/test_reader_filesystem_surface.py` fails if either reader names a filesystem API at all.
New reader I/O goes through `app/activity_gate.py`, never round it.

**Failure text comes from a fixed vocabulary.** `source_error` in the payload is one of the
strings in `app/degrade.py`, never `str(exc)`: an exception raised while a request is being
built carries the bearer token. The same rule covers the log.

**Every read of something someone else wrote has a byte cap, and a deadline unless it
provably cannot take one.** The knobs are the "Read budgets" table in `README.md`, and a new
one goes there, in `.env.example` and in `docker-compose.yml`.

**Dependencies are fixed by content, and one place decides a version.** `requirements.in`
and `requirements-dev.in` name the packages; `requirements.txt` and `requirements-dev.txt`
are those resolved in full, every package pinned to one version and to a `sha256` of the
artefact, and are what anything actually installs. A range in an input would be a second place
a version is decided, so the inputs carry names and nothing else. To add or drop a package,
edit the input and regenerate both locks with the command written in each lock's header —
they are regenerated together, because a contributor's venv and the image have to be the same
set of artefacts. The image installs with `pip install --require-hashes`, which refuses a
lock that has lost a hash and refuses a package the lock does not name, so an incomplete
regeneration fails the Docker build rather than resolving something fresh. The same flag is on
each of the other installs this repository spells out -- the venv above, CI's two jobs, and the
commands in `README.md`, `CLAUDE.md` and `AGENTS.md` -- so a lock that has lost its hashes
*altogether* fails at each of them rather than only in the image. That last part is the only
part the flag adds outside the image: pip turns hash checking on by itself as soon as one
requirement carries a `--hash`, so a lock that has lost *one* hash already fails without it.
`tests/test_dependency_lock.py` scans the tracked tree for installs of a lock, requires the
flag and allows no other option beside it -- a second `--index-url` is a second source of
code, hashes or not -- and fails if a file it does not name installs one. Two tracked places are
exempt there, each named with its reason: `tools/screenshots/`, which is #108, and the archived
plans under `docs/superpowers/plans/archive/`, which are a record of finished work rather than
instructions anybody follows. The locks are compiled `--universal` against
`--python-version 3.13`, which is the floor CI runs and not the target: the image is Python
3.14, and a universal resolve is what makes one set of artefacts serve both.

**The image in the README is generated, not screenshotted by hand.** It is captured from the
real app serving fabricated data, so nobody's plan tier, usage or activity ends up in a public
file. If a change moves the layout or the colour ramp, regenerate it:
[`tools/screenshots/README.md`](tools/screenshots/README.md).

## Reporting a security issue

Not here — see [`SECURITY.md`](SECURITY.md). Use GitHub's private vulnerability reporting,
and do not describe what you found in a public issue or comment.

## Licence

Contributions are accepted under the [Apache License 2.0](LICENSE), the licence this project is
released under.
