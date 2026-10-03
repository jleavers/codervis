# Security policy

## Reporting a vulnerability

**Please report privately, through [GitHub's private vulnerability
reporting](https://github.com/jleavers/codervis/security/advisories/new).** The report becomes
a draft advisory only you and the maintainer can see, and it is the route to use rather than a
public issue.

**If that link does not offer you a form** — private reporting can be switched off — open an
issue titled `Security contact request` and put nothing else in it: no description, no file,
no hint of what you found. The maintainer will open a private advisory and invite you to it.
The tracker is public, so please do not describe the problem there, in the issue or in a
comment.

Include what you would want if you were fixing it: the commit, what an attacker can reach, and
the smallest sequence that shows it. A proof of concept is welcome but not required — a clear
description of the mechanism is worth more than a working exploit. **Check anything you paste
for tokens first**: the dashboard's own logs and payload never carry one, but a debug capture
of an upstream call can.

There is no bounty, and no guaranteed response time: this is a personal project. Expect an
acknowledgement within a few days.

## What is in scope

codervis runs beside two long-lived session tokens — `~/.claude/.credentials.json` and
`~/.codex/auth.json` — and displays what they are entitled to. The boundaries that matter are:

- **Where a token can go.** The dashboard's container has no route off the host except an
  allow-listing `CONNECT` proxy that admits `claude.ai`, `chatgpt.com` and whatever the
  operator adds to `EGRESS_ALLOW`, over HTTPS only. That holds on Docker Engine 28.0 or newer,
  which honours the `gateway_mode_ipv4: isolated` option that keeps the host off the
  dashboard's network. On an older engine the host keeps an address there that the container
  can reach, unless the operator adds the host firewall rule that [`docs/operations.md`'s
  "Check the egress bound"](docs/operations.md#check-the-egress-bound) gives, and
  `python -m app.egress check` is what says which a deployment has. A way to make the dashboard send a token anywhere else — a
  redirect, a host override, a compromised dependency, a route round the proxy — is in scope.
- **Where a token can be seen.** `source_error` and the log come from a fixed vocabulary
  (`app/degrade.py`), never from an exception's own text, because an exception raised while a
  request is being built carries the bearer. A token, or any part of one, reaching the payload,
  the log, `/healthz` or the page is in scope.
- **What the activity readers read.** Each reader reaches the filesystem only through
  `app/activity_gate.py`: its own allow-listed subtrees, the operation it was granted (`stat`
  alone for Codex), and no link followed out of the tree. A reader made to read, stat or reveal
  the existence of anything else — a credential file, a session's contents, a path outside the
  data root — is in scope, and so is a refusal that leaks the path it refused.
- **Who can read the dashboard.** There is no login, so `DASHBOARD_BIND` and
  `DASHBOARD_ALLOWED_HOSTS` are the whole of its access control. A read of the payload by a
  client the operator did not name — another container on the host, a LAN peer with the
  default bind, or a web page using DNS rebinding — is in scope.
- **What a peer can cost.** Everything that feeds the payload is bounded: the upstream body,
  the transcript scan and the credential read each have a byte cap, and every read that can
  take a deadline has one; the front door holds a fixed number of connections and times out a
  slow request head. A way to make a service grow without bound, or to stall a refresher
  without it going `unavailable`, is in scope.

## What is not

- An upstream endpoint changing shape or refusing the call. Both endpoints are undocumented,
  and a panel that reads `unavailable` when one moves is the designed outcome.
- Anything that needs the operator's own account on the host, or an attacker who already has
  the host. The credential files are bind-mounted read-only from a home directory; someone who
  can read that directory does not need the dashboard.
- Anyone the operator chose to serve. Widening `DASHBOARD_BIND` and `DASHBOARD_ALLOWED_HOSTS`
  serves the machines they name, and
  [`docs/security-model.md`](docs/security-model.md#security-notes) says what those machines
  can then read.
- Denial of service against your own dashboard from a client you allowed to reach it, within
  the bounds above.

[`docs/security-model.md`](docs/security-model.md) is the operator's view of these
boundaries, and the "Network boundary" section of [`CLAUDE.md`](CLAUDE.md) is the developer's,
with the tests that pin each one.
