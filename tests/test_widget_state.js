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
    }),
  });
  const defaults = {
    claude: true,
    codex: true,
  };

  assert.deepEqual(loadSettings(storage, defaults), {
    claude: false,
    codex: true,
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
      { claude: false },
      {
        claude: true,
        codex: false,
      }
    ),
    {
      claude: false,
      codex: false,
    }
  );
});

test("defaultsFromPayload accepts only boolean enabled fields", () => {
  assert.deepEqual(
    defaultsFromPayload({
      claude: { enabled: false },
      codex: { enabled: "false" },
    }),
    {
      claude: false,
      codex: true,
    }
  );
  assert.deepEqual(defaultsFromPayload({}), { claude: true, codex: true });
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
