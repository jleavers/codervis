# Browser Widget Toggles Design

> **Archived — this work shipped.** Kept as a record of how the widget toggles were
> designed (#2), not as work to do; its plan is archived beside it at
> `docs/superpowers/plans/archive/2026-06-08-browser-widget-toggles.md`. The five-provider
> shape below is out of date: Gemini, Cursor and Copilot were removed in #13 (a6d91b6).
> Its stated invariants are the design's, not this tree's, and were not all true when it was
> written: "OAuth tokens are never logged or returned" under "Error Handling" was contradicted
> by the CR/LF tail filed as #14, since closed and pinned by
> `tests/test_payload_contract.py::test_a_credential_in_a_header_never_reaches_the_payload`.
> So check any claim here against the code before relying on it. See "What repo-shipped agent
> text may say" in `AGENTS.md`.

## Goal

Add a small slider toggle to every provider card so a user can disable or
re-enable that widget from the dashboard without editing Docker Compose
environment variables.

Toggle choices must:

- apply only to the current browser profile;
- survive page reloads and container restarts;
- keep disabled cards visible but dimmed;
- support all five providers, including Claude Code;
- let the browser override an environment-derived default.

## Toggle Placement And Accessibility

Each provider header gets a compact slider toggle beside its source chip.
The control uses a native checkbox so it remains keyboard-operable and exposes
its checked state to assistive technology. Its accessible label includes the
provider name, for example `Enable Codex widget`.

The source chip and toggle remain grouped at the right side of the existing
provider header. The layout must continue to fit the current responsive card
grid without changing the gauge arrangement.

## Browser-Local State

The frontend stores one versioned object in `localStorage`, containing a
boolean for each provider:

```json
{
  "claude": true,
  "codex": true,
  "gemini": false,
  "cursor": true,
  "copilot": true
}
```

The exact storage key is application-specific and versioned so a future
incompatible settings shape can use a new key without misreading old data.

On first visit, or when no valid stored boolean exists for a provider, the
frontend initializes that provider from the server payload's `enabled` field.
After initialization, the stored browser choice wins over subsequent server
defaults.

Storage failures, malformed JSON, non-object values, and non-boolean provider
values must not break dashboard rendering. Invalid or missing entries fall
back to the corresponding server default.

## Environment Defaults

The existing `CODEX_ENABLED`, `CURSOR_ENABLED`, `COPILOT_ENABLED`, and
`GEMINI_ENABLED` variables change from server-side client kill switches to
first-visit browser defaults. Add `CLAUDE_ENABLED` with the same behavior and
a default of `true`.

All five quota clients are constructed regardless of these defaults, and the
server continues polling every provider. This is required so a browser can
re-enable any provider immediately without restarting the container.

The environment variables therefore no longer prevent credential reads or
upstream calls. Documentation must state this semantic change explicitly.
Users who leave a provider disabled by default still need a valid read-only
mount target, although missing credentials remain contained by the provider's
existing unavailable-state error handling.

## Server Payload

Every provider section includes:

- `enabled`: the environment-derived first-visit default;
- `source`: the provider's real current state, normally `live` or
  `unavailable`;
- the existing windows, plan, activity, and error fields.

The server no longer emits `source: "disabled"` because an environment flag
prevented client construction. Browser-local presentation owns the disabled
state. Existing provider failures continue to produce `source: "unavailable"`
without synthesized usage.

The health endpoint continues to report credential and data-root information.
Its provider `*_enabled` fields represent the configured browser defaults,
not whether a client exists. This keeps the field meaningful after all clients
become unconditional.

## Frontend Data Flow

The frontend maintains the latest server section for each provider separately
from the browser-local enabled state.

For each initial render and SSE update:

1. Read and validate saved toggle choices.
2. Fill missing choices from each section's `enabled` default.
3. Store the resulting complete settings object when possible.
4. Apply the latest gauge, plan, activity, source, and error data.
5. Apply the local enabled presentation to each card.

When a card is locally enabled, it displays the latest real server source,
error, and values.

When a card is locally disabled:

- it remains in the grid;
- the entire card is visibly dimmed;
- its toggle is unchecked;
- its source chip reads `disabled`;
- the underlying source error text and tooltip are hidden;
- the latest gauge and metadata values may remain rendered beneath the dimmed
  presentation so re-enabling is immediate.

Toggling a card updates the DOM and `localStorage` immediately. It does not
send a request to the server or reconnect the SSE stream.

## Global Status

The dashboard status summary considers only locally enabled providers.
An unavailable provider does not keep the global status stale after the user
switches that card off.

If every provider is locally disabled, the dashboard remains connected and
shows an OK state with text indicating that all widgets are off. Re-enabling a
provider immediately recomputes the summary from its latest server state.

## Error Handling

Provider client behavior remains unchanged:

- upstream, authentication, parsing, and credential failures are converted to
  the provider-specific live quota error;
- the payload reports `source: "unavailable"`;
- no quota is inferred from local activity;
- OAuth tokens are never logged or returned.

Browser storage failures are presentation failures only. The dashboard must
continue using server defaults in memory even when persistence is unavailable.

## Documentation

Keep `README.md`, `.env.example`, `docker-compose.yml`, and `CLAUDE.md` in sync:

- add `CLAUDE_ENABLED=true`;
- describe every `*_ENABLED` variable as a first-visit browser default;
- state that all providers are still polled regardless of the default;
- explain that header toggles are browser-local and persisted in
  `localStorage`;
- remove instructions that imply `*_ENABLED=false` stops client construction,
  credential access, or upstream polling.

## Tests

Add focused coverage for:

1. Every provider payload includes the environment-derived `enabled` default.
2. All provider clients remain constructed and callable when a default is
   false.
3. Health endpoint `*_enabled` fields report configured defaults.
4. Every provider header renders an accessible checkbox toggle.
5. Frontend state validation falls back for malformed storage and preserves
   valid per-provider booleans.
6. A locally disabled card displays `disabled`, dims the card, and suppresses
   its real error.
7. Re-enabling restores the latest real source and error without an additional
   server response.
8. The global status ignores locally disabled unavailable providers and
   handles the all-disabled case.

Use the repository's existing Python test suite for server and template
coverage. Frontend behavior should be exercised through a small deterministic
JavaScript test harness using the available Node runtime, without adding a
browser framework or production dependency.

## Success Criteria

The feature is complete when:

- all five cards have the approved header toggle;
- choices persist per browser across reloads and container restarts;
- disabled cards remain visible, dimmed, and labeled `disabled`;
- environment settings provide first-visit defaults only;
- any card can be re-enabled immediately;
- locally disabled unavailable providers do not affect global status;
- documentation reflects the new environment semantics;
- the full pytest suite, frontend behavior tests, and Python compilation
  checks pass.
