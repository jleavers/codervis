# Regenerating the README's image and the social preview

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

The last line puts Playwright's browser builds, about 150 MB, into `~/.cache/ms-playwright`.
It needs no root to download them; running Chromium afterwards needs the system libraries it
links against, which on a bare Linux host is `playwright install --with-deps chromium` and
*that* does use `apt` as root. It is the only step here not fixed by content — see below.

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

## The social preview

`docs/images/social-preview.png` is the 1280×640 card GitHub shows when the repository is
linked. It is drawn from the dashboard's own parts rather than from a copy of them:
`social-preview.html` links `app/static/style.css` for the palette, the wordmark and the meters,
and `social_preview.py` injects `colorFor()` from `app/static/app.js` before the page runs, so
each meter is the colour the dashboard would draw at that percentage. The four percentages are
invented in the page; the app is not served, nothing is read from `~/.claude` or `~/.codex`, and
the page loads nothing from the network. Regenerate it in the same venv, after a change to the
layout, the wordmark or the colour ramp:

```bash
. tools/screenshots/.venv/bin/activate
python tools/screenshots/social_preview.py
```

It prints the size and exits non-zero if the file is over GitHub's 1 MB limit. GitHub takes the
image only through the web interface, so a new one is uploaded by hand under the repository's
Settings, General, Social preview.

## The browser builds are not fixed by content, and cannot be from here

`playwright install chromium` is three downloads, not one. `playwright install --dry-run
chromium` prints them, and against the pinned `playwright==1.63.0` they are:

| Artefact | From |
| --- | --- |
| Chrome for Testing (`chromium-<rev>`) | `cdn.playwright.dev` |
| FFmpeg (`ffmpeg-<rev>`) | `cdn.playwright.dev`, with two named fallback hosts |
| Chrome Headless Shell (`chromium_headless_shell-<rev>`) | `cdn.playwright.dev` |

The third one is the one that matters most here: both scripts call `pw.chromium.launch()` with
Playwright's default `headless=True`, and since Playwright 1.49 that executes the headless shell
rather than Chromium proper. So the binary this tool actually runs is the one easiest to
overlook. None of the three is verified against a hash, and this repository has no way to make
one be:

- **Which builds are requested is fixed**, by the pinned `playwright` wheel. Its
  `driver/package/browsers.json` names the revision and version of each, and the wheel's own
  `--hash=sha256:` is what makes that file the one upstream published. So the downloads are not
  "whatever is newest": they are named revisions, and moving them takes a `playwright` bump
  that Dependabot writes and a reviewer reads.
- **What arrives is not.** That manifest carries a revision, a version and a title per browser
  and no digest of any kind, and Playwright's own installer checks none: there is no
  `--require-hashes` for a browser download, and nothing to give one. What stands behind the
  bytes is TLS to those hosts and nothing else.

So the blast radius of this step is the same as before #108 — code that runs as the maintainer,
on the host holding `~/.claude/.credentials.json` and `~/.codex/auth.json`, with no bound on
where it can connect — and it is narrower only in that the artefacts come from named revisions
rather than from packages resolved afresh. Two things follow, and both are the reader's to
weigh rather than something this file can settle:

- This is the one place the "everything by content hash" claim in `docs/security-model.md`'s
  Security notes does not reach, and it is stated there as such.
- Whoever wants it closed anyway has one honest option: install on a machine that is not this
  one, record the digest of **each** of the three archives that arrived, and check all of them
  by hand on every later machine — recording one and running the other two unverified is worse
  than recording none, because it reads as coverage. Nothing in this repository automates that,
  because a digest this project recorded itself is a digest this project vouched for, which is
  a different claim from upstream's.

It is also the one step that can be skipped entirely: the browsers are only needed when one of
the two images is actually being regenerated, which is rare, and the two scripts here are the
only things that use them.
