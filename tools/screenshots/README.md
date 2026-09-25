# Regenerating the README's image

`docs/images/usage-ramp.gif` is captured from the real app serving **fabricated data**: the
quota clients and activity readers are stubbed before the app starts, its data directories
point at an empty temporary directory, and every percentage, plan, reset time and activity
time is invented in `capture.py`. Nothing in it is anyone's usage, and no credential file is
read to make it.

Regenerate it when the layout or the colour ramp changes, or the image quietly stops
describing the dashboard.

## Once per machine

Playwright's browser, about 150 MB into `~/.cache/ms-playwright`. No root, and nothing enters
this project's dependencies:

```bash
uv run --with playwright playwright install chromium
```

## Every time

```bash
uv run --with-requirements requirements.txt --with playwright --with pillow \
  python tools/screenshots/capture.py
```

The script serves the app on `127.0.0.1:8099`, steps the stubbed usage through eight stages,
waits for each to arrive over SSE and for the meter's fill transition to settle, screenshots
the panel, and assembles the frames. It prints the size and exits non-zero if the file is over
500 KB, which is the limit it is asked to stay under so the repository stays small. The dials
are `--width` (900) and `--colours` (64): the panel is flat colour, so it quantises well and
loses more to resampling than to palette.
