# Regenerating the README's image

`docs/images/usage-ramp.gif` is captured from the real app serving **fabricated data**: the
quota clients and activity readers are stubbed before the app starts, its data directories
point at an empty temporary directory, and every percentage, plan, reset time and activity
time is invented in `capture.py`. Nothing in it is anyone's usage, and no credential file is
read to make it.

Regenerate it when the layout or the colour ramp changes, or the image quietly stops
describing the dashboard.

## Once per machine

This tool's dependencies are `requirements-screenshots.in` in the repository root, resolved in
full and fixed by content hash in `requirements-screenshots.txt` — the runtime set, because
the capture serves the real app, plus `playwright` and `pillow`. It gets a venv of its own so
that neither joins a contributor's, and the install requires hashes exactly as every other
install in this repository does:

```bash
python -m venv tools/screenshots/.venv
. tools/screenshots/.venv/bin/activate
python -m pip install --require-hashes -r requirements-screenshots.txt
playwright install chromium
```

The last line puts Playwright's browser, about 150 MB, into `~/.cache/ms-playwright`. No root,
and it is the only step here that is not fixed by content — see below.

## Every time

```bash
. tools/screenshots/.venv/bin/activate
python tools/screenshots/capture.py
```

The script serves the app on `127.0.0.1:8099`, steps the stubbed usage through eight stages,
waits for each to arrive over SSE and for the meter's fill transition to settle, screenshots
the panel, and assembles the frames. It prints the size and exits non-zero if the file is over
500 KB, which is the limit it is asked to stay under so the repository stays small. The dials
are `--width` (900) and `--colours` (64): the panel is flat colour, so it quantises well and
loses more to resampling than to palette.

## The Chromium build is not fixed by content, and cannot be from here

`playwright install chromium` downloads a browser build from `cdn.playwright.dev` over TLS.
That artefact is **not** verified against a hash, and this repository has no way to make it be:

- **Which build is requested is fixed**, by the pinned `playwright` wheel. Its
  `driver/package/browsers.json` names the Chromium revision and version, and the wheel's own
  `--hash=sha256:` is what makes that file the one upstream published. So the download is not
  "whatever is newest": it is a named revision, and moving it takes a `playwright` bump that
  Dependabot writes and a reviewer reads.
- **What arrives is not.** That manifest carries a revision and a version and no digest of any
  kind, and Playwright's own installer checks none: there is no `--require-hashes` for a
  browser download, and nothing to give one. What stands behind the bytes is TLS to that CDN
  and nothing else.

So the blast radius of this step is the same as before #108 — code that runs as the maintainer,
on the host holding `~/.claude/.credentials.json` and `~/.codex/auth.json`, with no bound on
where it can connect — and it is narrower only in that it is one artefact from one named
revision rather than two packages resolved afresh. Two things follow, and both are the reader's
to weigh rather than something this file can settle:

- This is the one place the "everything by content hash" claim in `README.md`'s Security notes
  does not reach, and it is stated there as such.
- Whoever wants it closed anyway has one honest option: install the browser once on a machine
  that is not this one, record the digest of what arrived, and check it by hand on every later
  machine. Nothing in this repository automates that, because a digest this project recorded
  itself is a digest this project vouched for, which is a different claim from upstream's.

It is also the one step that can be skipped entirely: the browser is only needed when the image
is actually being regenerated, which is rare, and `capture.py` is the only thing here that uses
it.
