# Browser Widget Toggles Implementation Plan

> **Archived — this work shipped.** Kept as a record of how the widget toggles were
> built (#2), not as work to do. Nothing here is a task list: the checkbox syntax was
> removed so no agent picks it up, and so were the package-cache prefixes its commands
> carried, which named a fixed directory in world-writable `/tmp` — a name another
> local principal can create and fill first; see "What repo-shipped agent text may
> say" in `AGENTS.md`. The five-provider shape below is also out of date: Gemini, Cursor and
> Copilot were removed in #13.

**Goal:** Add persistent, browser-local header toggles for all five provider widgets while changing `*_ENABLED` environment variables into first-visit defaults only.

**Architecture:** The FastAPI payload always reports live provider data plus an environment-derived `enabled` default. A small dependency-free JavaScript module owns localStorage validation, effective presentation state, and global status calculation; `app.js` wires those pure functions to the existing DOM and SSE stream. Provider clients remain unconditional so any locally disabled card can be restored immediately.

**Tech Stack:** Python 3.14, FastAPI, Jinja2, vanilla JavaScript, CSS, pytest, Node 24 built-in test runner, Docker Compose.

---

## File Structure

- Modify `app/main.py`: make all provider clients unconditional, add all five environment defaults to every payload section, and report defaults from `/healthz`.
- Create `app/static/widget-state.js`: pure browser-state parsing, persistence, presentation, and status-summary functions with CommonJS export support for Node tests.
- Modify `app/static/app.js`: connect checkbox changes, localStorage state, initial payload, SSE updates, and effective disabled presentation.
- Modify `app/templates/index.html`: render one accessible header toggle per provider, expose the initial payload, and load the state module before `app.js`.
- Modify `app/static/style.css`: style the compact slider and dim locally disabled cards while preserving an operable toggle.
- Modify `tests/test_main_payload.py`: replace startup-disable expectations with default-only payload and health behavior.
- Create `tests/test_widget_state.js`: test storage validation, disabled presentation, restoration, and global status with `node:test`.
- Modify `README.md`, `.env.example`, `docker-compose.yml`, and `CLAUDE.md`: document the new environment semantics and browser-local persistence.
- Modify `.gitignore`: ignore local visual-companion sessions under `.superpowers/`.

### Task 1: Make Environment Flags Payload Defaults

**Files:**
- Modify: `tests/test_main_payload.py`
- Modify: `app/main.py`

- **Step 1: Extend the quota stub to record calls and support health checks**

Replace `QuotaClientStub` in `tests/test_main_payload.py` with:

```python
class QuotaClientStub:
    credentials_path = Path("unused")

    def __init__(self, snapshot=None, error: Exception | None = None) -> None:
        self._snapshot = snapshot
        self._error = error
        self.calls = 0

    def get(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot

    def credentials_present(self) -> bool:
        return self.credentials_path.exists()
```

- **Step 2: Write the failing default-only provider test**

Delete the four tests that set `_codex`, `_cursor`, `_copilot`, or `_gemini` to
`None` and expect `source == "disabled"`. Add:

```python
def test_provider_defaults_do_not_suppress_live_clients(monkeypatch) -> None:
    monkeypatch.setattr(main, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(main, "CODEX_ENABLED", False)
    monkeypatch.setattr(main, "CURSOR_ENABLED", False)
    monkeypatch.setattr(main, "COPILOT_ENABLED", False)
    monkeypatch.setattr(main, "GEMINI_ENABLED", False)

    monkeypatch.setattr(
        main,
        "_claude_activity",
        ActivityStub(ClaudeActivitySnapshot(last_activity=None, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_codex_activity",
        ActivityStub(CodexActivitySnapshot(last_activity=None, data_root_exists=True)),
    )

    claude = QuotaClientStub(
        SimpleNamespace(
            five_hour=_window(10),
            seven_day=_window(20),
            subscription_type="max",
        )
    )
    codex = QuotaClientStub(
        SimpleNamespace(
            five_hour=_window(30),
            seven_day=_window(40),
            plan_type="pro",
        )
    )
    monkeypatch.setattr(main, "_live", claude)
    monkeypatch.setattr(main, "_codex", codex)

    cursor = QuotaClientStub(
        SimpleNamespace(
            requests=_named_window("requests", "Premium Requests (month)", 50),
            spend=_named_window("spend", "Usage-Based Spend (month)", 60),
            plan_type="pro",
        )
    )
    copilot = QuotaClientStub(
        SimpleNamespace(
            premium=_named_window("premium", "Premium Requests (month)", 70),
            secondary=_named_window("secondary", "Chat (month)", None),
            plan_type="individual",
        )
    )
    gemini = QuotaClientStub(
        SimpleNamespace(
            pro=_named_window("pro", "Pro Requests (day)", 80),
            flash=_named_window("flash", "Flash Requests (day)", 90),
            plan_type="pro",
        )
    )
    _stub_cursor(monkeypatch, snapshot=cursor._snapshot)
    _stub_copilot(monkeypatch, snapshot=copilot._snapshot)
    _stub_gemini(monkeypatch, snapshot=gemini._snapshot)
    monkeypatch.setattr(main, "_cursor", cursor)
    monkeypatch.setattr(main, "_copilot", copilot)
    monkeypatch.setattr(main, "_gemini", gemini)

    data = main._build_payload()

    for key in ("claude", "codex", "cursor", "copilot", "gemini"):
        assert data[key]["enabled"] is False
        assert data[key]["source"] == "live"
    assert [client.calls for client in (claude, codex, cursor, copilot, gemini)] == [
        1,
        1,
        1,
        1,
        1,
    ]
```

- **Step 3: Write the failing health-default test**

Add:

```python
def test_health_reports_configured_widget_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(main, "CODEX_ENABLED", True)
    monkeypatch.setattr(main, "CURSOR_ENABLED", False)
    monkeypatch.setattr(main, "COPILOT_ENABLED", True)
    monkeypatch.setattr(main, "GEMINI_ENABLED", False)

    activity = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(main, "_claude_activity", activity)
    monkeypatch.setattr(main, "_codex_activity", activity)
    monkeypatch.setattr(main, "_cursor_activity", activity)
    monkeypatch.setattr(main, "_copilot_activity", activity)
    monkeypatch.setattr(main, "_gemini_activity", activity)

    credential = tmp_path / "credential"
    credential.touch()
    clients = [QuotaClientStub() for _ in range(5)]
    for client in clients:
        client.credentials_path = credential
    monkeypatch.setattr(main, "_live", clients[0])
    monkeypatch.setattr(main, "_codex", clients[1])
    monkeypatch.setattr(main, "_cursor", clients[2])
    monkeypatch.setattr(main, "_copilot", clients[3])
    monkeypatch.setattr(main, "_gemini", clients[4])

    data = TestClient(main.app).get("/healthz").json()

    assert data["claude_enabled"] is False
    assert data["codex_enabled"] is True
    assert data["cursor_enabled"] is False
    assert data["copilot_enabled"] is True
    assert data["gemini_enabled"] is False
```

- **Step 4: Run the focused tests and verify RED**

Run:

```bash
uv run --with-requirements requirements-dev.txt \
  python -m pytest \
  tests/test_main_payload.py::test_provider_defaults_do_not_suppress_live_clients \
  tests/test_main_payload.py::test_health_reports_configured_widget_defaults -v
```

Expected: failures because Claude has no `enabled` field, false defaults still
short-circuit clients, and `/healthz` reports client existence.

- **Step 5: Make client construction unconditional**

In `app/main.py`, define all defaults and construct every client:

```python
CLAUDE_ENABLED = _enabled("CLAUDE_ENABLED")
CODEX_ENABLED = _enabled("CODEX_ENABLED")
CURSOR_ENABLED = _enabled("CURSOR_ENABLED")
COPILOT_ENABLED = _enabled("COPILOT_ENABLED")
GEMINI_ENABLED = _enabled("GEMINI_ENABLED")

app = FastAPI(title="Codervis")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

_live: LiveQuotaClient = client_from_env()
_claude_activity: ClaudeActivityReader = claude_activity_reader_from_env()
_codex: CodexLiveQuotaClient = codex_client_from_env()
_codex_activity: CodexActivityReader = codex_activity_reader_from_env()
_cursor: CursorLiveQuotaClient = cursor_client_from_env()
_cursor_activity: CursorActivityReader = cursor_activity_reader_from_env()
_copilot: CopilotLiveQuotaClient | CopilotBillingQuotaClient = copilot_client_from_env()
_copilot_activity: CopilotActivityReader = copilot_activity_reader_from_env()
_gemini: GeminiLiveQuotaClient = gemini_client_from_env()
_gemini_activity: GeminiActivityReader = gemini_activity_reader_from_env()
```

Remove the `_codex is None`, `_cursor is None`, `_copilot is None`, and
`_gemini is None` branches from their section builders.

- **Step 6: Add the configured default to every section result**

Add `"enabled": <PROVIDER>_ENABLED` to both the live and unavailable return
dictionaries in `_claude_section()`, `_codex_section()`, `_cursor_section()`,
`_copilot_section()`, and `_gemini_section()`. For example:

```python
return {
    "enabled": CLAUDE_ENABLED,
    "windows": [
        _window_dict(
            "five_hour",
            "5-Hour Window",
            live.five_hour.percent,
            live.five_hour.resets_at,
        ),
        _window_dict(
            "seven_day",
            "Weekly Window",
            live.seven_day.percent,
            live.seven_day.resets_at,
        ),
    ],
    "source": "live",
    "source_error": None,
    "subscription_type": live.subscription_type,
    "last_activity": last_activity,
    "data_root_exists": activity_snap.data_root_exists,
}
```

Use `CODEX_ENABLED`, `CURSOR_ENABLED`, `COPILOT_ENABLED`, and `GEMINI_ENABLED`
in the corresponding builders without changing their existing error
boundaries.

- **Step 7: Make health fields report defaults**

Replace the provider enablement and conditional credential expressions in
`healthz()` with:

```python
"claude_enabled": CLAUDE_ENABLED,
"claude_credentials_present": _live.credentials_path.exists(),
"codex_enabled": CODEX_ENABLED,
"codex_credentials_present": _codex.credentials_path.exists(),
"cursor_enabled": CURSOR_ENABLED,
"cursor_credentials_present": _cursor.credentials_path.exists(),
"copilot_enabled": COPILOT_ENABLED,
"copilot_credentials_present": _copilot.credentials_present(),
"gemini_enabled": GEMINI_ENABLED,
"gemini_credentials_present": _gemini.credentials_path.exists(),
```

Keep all existing data-root fields.

- **Step 8: Update existing payload assertions**

In `test_api_usage_returns_live_payload_without_scaling`, add:

```python
assert data["claude"]["enabled"] is True
```

Retain the existing `enabled is True` assertions for the other providers.

- **Step 9: Run server tests and verify GREEN**

Run:

```bash
uv run --with-requirements requirements-dev.txt \
  python -m pytest tests/test_main_payload.py -v
```

Expected: all payload and health tests pass.

- **Step 10: Commit server semantics**

```bash
git add app/main.py tests/test_main_payload.py
git commit -m "feat: make widget flags browser defaults"
```

### Task 2: Add A Pure Browser Widget-State Module

**Files:**
- Create: `tests/test_widget_state.js`
- Create: `app/static/widget-state.js`

- **Step 1: Write failing persistence and presentation tests**

Create `tests/test_widget_state.js`:

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");

const {
  PROVIDERS,
  STORAGE_KEY,
  defaultsFromPayload,
  loadSettings,
  mergeSettings,
  providerPresentation,
  saveSettings,
  summariseStatus,
} = require("../app/static/widget-state.js");

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
    value(key) {
      return values.get(key);
    },
  };
}

test("loadSettings preserves booleans and fills invalid entries from defaults", () => {
  const storage = memoryStorage({
    [STORAGE_KEY]: JSON.stringify({
      claude: false,
      codex: "false",
      cursor: true,
    }),
  });
  const defaults = {
    claude: true,
    codex: false,
    gemini: false,
    cursor: false,
    copilot: true,
  };

  assert.deepEqual(loadSettings(storage, defaults), {
    claude: false,
    codex: false,
    gemini: false,
    cursor: true,
    copilot: true,
  });
});

test("loadSettings survives malformed JSON and unavailable storage", () => {
  const defaults = Object.fromEntries(PROVIDERS.map((key) => [key, true]));
  const malformed = memoryStorage({ [STORAGE_KEY]: "{" });
  const unavailable = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("blocked");
    },
  };

  assert.deepEqual(loadSettings(malformed, defaults), defaults);
  assert.deepEqual(loadSettings(unavailable, defaults), defaults);
  assert.equal(saveSettings(unavailable, defaults), false);
});

test("mergeSettings retains choices and fills missing provider defaults", () => {
  assert.deepEqual(
    mergeSettings(
      { claude: false, codex: true },
      {
        claude: true,
        codex: false,
        gemini: false,
        cursor: true,
        copilot: false,
      }
    ),
    {
      claude: false,
      codex: true,
      gemini: false,
      cursor: true,
      copilot: false,
    }
  );
});

test("defaultsFromPayload accepts only boolean enabled fields", () => {
  assert.deepEqual(
    defaultsFromPayload({
      claude: { enabled: false },
      codex: { enabled: true },
      gemini: { enabled: "false" },
    }),
    {
      claude: false,
      codex: true,
      gemini: true,
      cursor: true,
      copilot: true,
    }
  );
});

test("disabled presentation hides source errors and enabled presentation restores them", () => {
  const section = {
    source: "unavailable",
    source_error: "credential expired",
  };

  assert.deepEqual(providerPresentation(section, false), {
    source: "disabled",
    sourceError: null,
  });
  assert.deepEqual(providerPresentation(section, true), {
    source: "unavailable",
    sourceError: "credential expired",
  });
});

test("status ignores disabled unavailable providers", () => {
  const payload = {
    claude: { source: "live", source_error: null },
    codex: { source: "unavailable", source_error: "expired" },
  };

  assert.deepEqual(
    summariseStatus(payload, {
      claude: true,
      codex: false,
      gemini: false,
      cursor: false,
      copilot: false,
    }),
    { state: "ok", text: "live" }
  );
});

test("status reports all widgets off", () => {
  const payload = {
    claude: { source: "unavailable", source_error: "expired" },
  };
  const settings = Object.fromEntries(PROVIDERS.map((key) => [key, false]));

  assert.deepEqual(summariseStatus(payload, settings), {
    state: "ok",
    text: "live · all widgets off",
  });
});
```

- **Step 2: Run the Node tests and verify RED**

Run:

```bash
node --test tests/test_widget_state.js
```

Expected: FAIL with `MODULE_NOT_FOUND` for `app/static/widget-state.js`.

- **Step 3: Implement the pure state module**

Create `app/static/widget-state.js`:

```javascript
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.CodervisWidgetState = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const PROVIDERS = ["claude", "codex", "gemini", "cursor", "copilot"];
  const STORAGE_KEY = "codervis.widget-enabled.v1";

  function defaultsFromPayload(payload) {
    return Object.fromEntries(
      PROVIDERS.map((key) => {
        const enabled = payload && payload[key] && payload[key].enabled;
        return [key, typeof enabled === "boolean" ? enabled : true];
      })
    );
  }

  function mergeSettings(settings, defaults) {
    const current =
      settings && typeof settings === "object" && !Array.isArray(settings)
        ? settings
        : {};
    const fallback =
      defaults && typeof defaults === "object" && !Array.isArray(defaults)
        ? defaults
        : {};
    return Object.fromEntries(
      PROVIDERS.map((key) => [
        key,
        typeof current[key] === "boolean"
          ? current[key]
          : typeof fallback[key] === "boolean"
            ? fallback[key]
            : true,
      ])
    );
  }

  function parseSettings(serialized) {
    if (typeof serialized !== "string") return {};
    try {
      const parsed = JSON.parse(serialized);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? parsed
        : {};
    } catch (_error) {
      return {};
    }
  }

  function saveSettings(storage, settings) {
    try {
      if (!storage) return false;
      storage.setItem(STORAGE_KEY, JSON.stringify(settings));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function loadSettings(storage, defaults) {
    let serialized = null;
    try {
      if (storage) serialized = storage.getItem(STORAGE_KEY);
    } catch (_error) {
      serialized = null;
    }
    const settings = mergeSettings(parseSettings(serialized), defaults);
    saveSettings(storage, settings);
    return settings;
  }

  function providerPresentation(section, enabled) {
    if (!enabled) {
      return { source: "disabled", sourceError: null };
    }
    return {
      source: section && section.source ? section.source : "unavailable",
      sourceError:
        section && section.source_error ? section.source_error : null,
    };
  }

  function summariseStatus(payload, settings) {
    const enabled = PROVIDERS.filter(
      (key) => payload && payload[key] && settings[key]
    );
    if (enabled.length === 0) {
      return { state: "ok", text: "live · all widgets off" };
    }

    const broken = enabled.find(
      (key) => payload[key].source === "unavailable"
    );
    if (broken) {
      const error = payload[broken].source_error || "";
      return {
        state: "stale",
        text: `${broken}: unavailable${error ? " · " + error : ""}`,
      };
    }

    if (enabled.every((key) => payload[key].source === "live")) {
      return { state: "ok", text: "live" };
    }
    return { state: "error", text: "error" };
  }

  return {
    PROVIDERS,
    STORAGE_KEY,
    defaultsFromPayload,
    loadSettings,
    mergeSettings,
    providerPresentation,
    saveSettings,
    summariseStatus,
  };
});
```

- **Step 4: Run the Node tests and verify GREEN**

Run:

```bash
node --test tests/test_widget_state.js
```

Expected: 7 tests pass.

- **Step 5: Commit the state module**

```bash
git add app/static/widget-state.js tests/test_widget_state.js
git commit -m "test: define browser widget state behavior"
```

### Task 3: Render Accessible Header Toggles

**Files:**
- Modify: `tests/test_main_payload.py`
- Modify: `app/templates/index.html`
- Modify: `app/static/style.css`

- **Step 1: Write the failing template test**

Add to `tests/test_main_payload.py`:

```python
def test_index_renders_accessible_widget_toggles(monkeypatch) -> None:
    def section(enabled: bool) -> dict:
        return {
            "enabled": enabled,
            "windows": [],
            "source": "live",
            "source_error": None,
            "subscription_type": None,
            "last_activity": None,
            "data_root_exists": True,
        }

    payload = {
        "claude": section(True),
        "codex": section(False),
        "gemini": section(True),
        "cursor": section(True),
        "copilot": section(True),
        "server_time": "2026-06-08T00:00:00+00:00",
    }
    monkeypatch.setattr(main, "_build_payload", lambda: payload)

    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    html = response.text
    assert html.count('class="widget-toggle-input"') == 5
    for key, title in (
        ("claude", "Claude Code"),
        ("codex", "Codex"),
        ("gemini", "Gemini Code Assist"),
        ("cursor", "Cursor"),
        ("copilot", "GitHub Copilot"),
    ):
        assert f'id="toggle-{key}"' in html
        assert f'data-provider="{key}"' in html
        assert f'aria-label="Enable {title} widget"' in html
    codex_start = html.index('id="provider-codex"')
    gemini_start = html.index('id="provider-gemini"')
    codex_card = html[codex_start:gemini_start]
    assert 'data-source="disabled"' in codex_card
    assert 'data-widget-enabled="false"' in codex_card
    assert html.index("/static/widget-state.js") < html.index("/static/app.js")
    assert "window.__INITIAL_PAYLOAD__" in html
```

- **Step 2: Run the template test and verify RED**

Run:

```bash
uv run --with-requirements requirements-dev.txt \
  python -m pytest \
  tests/test_main_payload.py::test_index_renders_accessible_widget_toggles -v
```

Expected: FAIL because no toggle markup, initial payload, or state-module script
exists.

- **Step 3: Render first-visit presentation and toggle markup**

In `app/templates/index.html`, derive the first-visit display source and extend
the provider header:

```html
{% set display_source = prov.data.source if prov.data.enabled else "disabled" %}
<section class="provider"
         id="provider-{{ prov.key }}"
         data-source="{{ display_source }}"
         data-widget-enabled="{{ 'true' if prov.data.enabled else 'false' }}">
  <header class="provider-head">
    <h1 class="provider-title">{{ prov.title }}</h1>
    <div class="provider-source">
      <span id="source-{{ prov.key }}"
            class="source-chip"
            data-state="{{ display_source }}">{{ display_source }}</span>
      <label class="widget-toggle">
        <input id="toggle-{{ prov.key }}"
               class="widget-toggle-input"
               type="checkbox"
               data-provider="{{ prov.key }}"
               aria-label="Enable {{ prov.title }} widget"
               {% if prov.data.enabled %}checked{% endif %} />
        <span class="widget-toggle-track" aria-hidden="true"></span>
      </label>
    </div>
  </header>
```

Render the initial error only for an enabled default:

```html
<div class="provider-error" id="error-{{ prov.key }}">{% if prov.data.enabled %}{{ prov.data.source_error or '' }}{% endif %}</div>
```

Replace the script block at the bottom with:

```html
<script>
  window.__INITIAL_PAYLOAD__ = {{ data | tojson }};
</script>
<script src="/static/widget-state.js"></script>
<script src="/static/app.js"></script>
```

- **Step 4: Style the compact slider**

In `app/static/style.css`, change `.provider-head` alignment to `center`, add
spacing to `.provider-source`, and add:

```css
.provider-source {
  display: flex;
  align-items: center;
  gap: 8px;
}

.widget-toggle {
  position: relative;
  display: inline-flex;
  cursor: pointer;
}

.widget-toggle-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.widget-toggle-track {
  width: 30px;
  height: 17px;
  padding: 2px;
  border: 1px solid var(--ink-faint);
  border-radius: 999px;
  background: var(--rule);
  transition: border-color 180ms ease, background 180ms ease;
}

.widget-toggle-track::after {
  content: "";
  display: block;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--ink-faint);
  transition: transform 180ms ease, background 180ms ease, box-shadow 180ms ease;
}

.widget-toggle-input:checked + .widget-toggle-track {
  border-color: rgba(163,230,53,0.45);
  background: rgba(163,230,53,0.12);
}

.widget-toggle-input:checked + .widget-toggle-track::after {
  transform: translateX(13px);
  background: var(--lime);
  box-shadow: 0 0 8px rgba(163,230,53,0.55);
}

.widget-toggle-input:focus-visible + .widget-toggle-track {
  outline: 2px solid var(--amber);
  outline-offset: 3px;
}

.provider[data-widget-enabled="false"] {
  background: rgba(255,255,255,0.008);
  border-color: rgba(82,92,102,0.55);
}

.provider[data-widget-enabled="false"] .provider-title,
.provider[data-widget-enabled="false"] .gauge,
.provider[data-widget-enabled="false"] .provider-foot {
  opacity: 0.45;
}
```

Keep the source chip and toggle fully readable and operable while the rest of
the card is dimmed. Remove `.provider[data-source="disabled"] .gauge` from the
existing unavailable selector because local dimming now uses
`data-widget-enabled`.

- **Step 5: Run template and server tests**

Run:

```bash
uv run --with-requirements requirements-dev.txt \
  python -m pytest tests/test_main_payload.py -v
```

Expected: all tests pass.

- **Step 6: Commit toggle markup and styling**

```bash
git add app/templates/index.html app/static/style.css tests/test_main_payload.py
git commit -m "feat: render provider widget toggles"
```

### Task 4: Wire Toggles Into Initial Paint And SSE Updates

**Files:**
- Modify: `app/static/app.js`
- Test: `tests/test_widget_state.js`

- **Step 1: Add a regression test for unavailable status restoration**

Append to `tests/test_widget_state.js`:

```javascript
test("status restores an unavailable provider when it is re-enabled", () => {
  const payload = {
    codex: { source: "unavailable", source_error: "expired" },
  };
  const settings = Object.fromEntries(PROVIDERS.map((key) => [key, false]));
  settings.codex = true;

  assert.deepEqual(summariseStatus(payload, settings), {
    state: "stale",
    text: "codex: unavailable · expired",
  });
});
```

- **Step 2: Run the Node tests and verify the regression passes**

Run:

```bash
node --test tests/test_widget_state.js
```

Expected: 8 tests pass. This locks the pure behavior before DOM integration.

- **Step 3: Initialize browser-local state from the initial payload**

At the top of `app/static/app.js`, after obtaining the status elements, add:

```javascript
  const WidgetState = window.CodervisWidgetState;
  const PROVIDERS = WidgetState.PROVIDERS;
  let latestPayload = window.__INITIAL_PAYLOAD__ || {};
  let storage = null;
  try {
    storage = window.localStorage;
  } catch (_error) {
    storage = null;
  }
  let widgetSettings = WidgetState.loadSettings(
    storage,
    WidgetState.defaultsFromPayload(latestPayload)
  );
```

Remove the later hard-coded `PROVIDERS` declaration.

- **Step 4: Apply the effective local presentation**

Replace `applyProvider()` with:

```javascript
  function applyProvider(key, section) {
    if (!section) return;
    (section.windows || []).forEach((w) => applyGauge(key, w));

    const enabled = widgetSettings[key];
    const presentation = WidgetState.providerPresentation(section, enabled);
    const src = document.getElementById("source-" + key);
    if (src) {
      src.textContent = presentation.source;
      src.dataset.state = presentation.source;
      if (presentation.sourceError) {
        src.title = presentation.sourceError;
      } else {
        src.removeAttribute("title");
      }
    }

    const sub = document.getElementById("subscription-" + key);
    if (sub) sub.textContent = section.subscription_type || "—";

    const lastActivity = document.getElementById("last-activity-" + key);
    if (lastActivity) lastActivity.dataset.iso = section.last_activity || "";

    const errEl = document.getElementById("error-" + key);
    if (errEl) errEl.textContent = presentation.sourceError || "";

    const provRoot = document.getElementById("provider-" + key);
    if (provRoot) {
      provRoot.dataset.source = presentation.source;
      provRoot.dataset.widgetEnabled = enabled ? "true" : "false";
    }

    const toggle = document.getElementById("toggle-" + key);
    if (toggle) toggle.checked = enabled;
  }
```

- **Step 5: Replace status calculation with the pure summary**

Replace `summariseStatus()` with:

```javascript
  function summariseStatus(payload) {
    const summary = WidgetState.summariseStatus(payload, widgetSettings);
    setStatus(summary.state, summary.text);
  }
```

- **Step 6: Merge defaults on every payload and persist toggle changes**

Replace `apply()` and the initial gauge-only paint block with:

```javascript
  function apply(payload) {
    latestPayload = payload;
    widgetSettings = WidgetState.mergeSettings(
      widgetSettings,
      WidgetState.defaultsFromPayload(payload)
    );
    WidgetState.saveSettings(storage, widgetSettings);
    PROVIDERS.forEach((key) => applyProvider(key, payload[key]));
    paintRelativeFields();
    summariseStatus(payload);
  }

  document.querySelectorAll(".widget-toggle-input").forEach((toggle) => {
    toggle.addEventListener("change", () => {
      widgetSettings[toggle.dataset.provider] = toggle.checked;
      WidgetState.saveSettings(storage, widgetSettings);
      apply(latestPayload);
    });
  });

  apply(latestPayload);
```

Keep the existing `connect()` and one-second relative-time interval. SSE
messages continue to call `apply(JSON.parse(ev.data))`; toggling does not
reconnect or call the server.

- **Step 7: Check JavaScript syntax and run behavior tests**

Run:

```bash
node --check app/static/widget-state.js
node --check app/static/app.js
node --test tests/test_widget_state.js
```

Expected: both syntax checks succeed and 8 tests pass.

- **Step 8: Commit browser integration**

```bash
git add app/static/app.js tests/test_widget_state.js
git commit -m "feat: persist browser widget choices"
```

### Task 5: Synchronize Docker And Documentation

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `.gitignore`

- **Step 1: Add the Claude default to Docker Compose**

In `docker-compose.yml`, add before `CODEX_ENABLED`:

```yaml
      CLAUDE_ENABLED: "${CLAUDE_ENABLED:-true}"
```

Retain all read-only provider mounts.

- **Step 2: Rewrite environment examples as first-visit defaults**

In `.env.example`, add:

```dotenv
# Initial widget state for browsers that have no saved dashboard choice.
# All providers are still polled; the dashboard toggle is persisted per
# browser in localStorage and overrides this default after the first visit.
CLAUDE_ENABLED=true
```

Keep each existing provider home path and set:

```dotenv
CODEX_ENABLED=true
CURSOR_ENABLED=true
COPILOT_ENABLED=true
GEMINI_ENABLED=true
```

Replace comments claiming a false value stops or safely bypasses a provider
with text stating that it only controls the first browser visit and does not
prevent credential reads or upstream calls.

- **Step 3: Update the README behavior and configuration table**

Add a paragraph after the SSE description:

```markdown
Each provider header has a browser-local toggle. Switching a widget off keeps
its card visible but dimmed, labels it `disabled`, and removes it from the
overall status summary. Choices are stored in browser `localStorage`, survive
container restarts, and do not affect other browsers.
```

Replace the caveat about setting `*_ENABLED=false` with:

```markdown
- The `*_ENABLED` variables only choose the initial toggle state for a browser
  with no saved preference. All provider clients are still constructed and
  polled, so these variables do not suppress credential reads or upstream
  calls.
```

Add `CLAUDE_ENABLED` to the configuration table and describe all five enable
variables as “First-visit browser widget default” with default `true`.
Update the dashboard behavior section so `disabled` is described as a local
presentation state rather than a server source.

- **Step 4: Update agent implementation guidance**

In `CLAUDE.md`, replace each provider-specific “falsy means client is `None`”
note with one shared invariant:

```markdown
`CLAUDE_ENABLED`, `CODEX_ENABLED`, `CURSOR_ENABLED`, `COPILOT_ENABLED`, and
`GEMINI_ENABLED` are first-visit browser defaults only. All live clients are
constructed unconditionally. Browser-local choices live in versioned
`localStorage`; disabling a card must not stop SSE updates or change provider
error handling.
```

Document `app/static/widget-state.js` as the pure state/presentation module and
retain `app/static/app.js` as the single gauge-color and DOM-update path.

- **Step 5: Verify stale semantics are gone**

Run:

```bash
rg -n \
  'client kill switch|_codex is None|_cursor is None|_copilot is None|_gemini is None|source: "disabled"|Set .*_ENABLED=false if you do not use' \
  README.md CLAUDE.md .env.example docker-compose.yml app/main.py \
  tests/test_main_payload.py
```

Expected: no stale server-disable documentation or implementation matches.
CSS, JavaScript, and JavaScript-test references to the browser presentation
string `disabled` are allowed and should be reviewed separately.

- **Step 6: Commit configuration and documentation**

```bash
git add .gitignore .env.example docker-compose.yml README.md CLAUDE.md
git commit -m "docs: explain browser widget defaults"
```

### Task 6: Full Verification

**Files:**
- Verify all modified files.

- **Step 1: Run the JavaScript checks**

```bash
node --check app/static/widget-state.js
node --check app/static/app.js
node --test tests/test_widget_state.js
```

Expected: syntax checks succeed and all 8 Node tests pass.

- **Step 2: Run the full Python suite**

```bash
uv run --with-requirements requirements-dev.txt \
  python -m pytest
```

Expected: all pytest tests pass without reading host credentials or calling
upstream quota endpoints.

- **Step 3: Run Python compilation checks**

```bash
uv run python -m py_compile \
  app/main.py app/quota.py app/claude_activity.py \
  app/codex_quota.py app/codex_activity.py \
  app/cursor_quota.py app/cursor_activity.py \
  app/copilot_quota.py app/copilot_activity.py \
  app/gemini_quota.py app/gemini_activity.py
```

Expected: command exits successfully with no output.

- **Step 4: Validate Compose and whitespace**

```bash
docker compose config --quiet
git diff --check
git status --short
```

Expected: Compose validation and whitespace checks succeed. Status contains
only intentional implementation-plan or feature changes not already committed.

- **Step 5: Perform a manual dashboard smoke test**

Run:

```bash
docker compose up --build -d
curl -fsS http://localhost:8765/healthz
curl -fsS http://localhost:8765/api/usage
```

In the browser, verify:

1. Every provider header has a keyboard-focusable slider.
2. Switching a card off dims it and changes its chip to `disabled`.
3. An unavailable card stops affecting the top status while off.
4. Reloading the page and restarting the container preserve the browser choice.
5. Switching the card on restores the latest real source and values.

Then stop the stack:

```bash
docker compose down
```

- **Step 6: Commit any verification-only fixes**

If verification required source changes, stage only those files and commit:

```bash
git add app tests README.md CLAUDE.md .env.example docker-compose.yml
git commit -m "fix: complete widget toggle verification"
```

If no source changes were required, do not create an empty commit.
