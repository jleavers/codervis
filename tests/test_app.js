const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const WidgetState = require("../app/static/widget-state.js");
const appSource = fs.readFileSync(
  path.join(__dirname, "../app/static/app.js"),
  "utf8"
);

function renderResetIn(timeZone) {
  const reset = { dataset: {}, textContent: "" };
  const statusText = { textContent: "" };
  const status = {
    dataset: {},
    querySelector(selector) {
      return selector === ".status-text" ? statusText : null;
    },
  };
  const gauge = {
    dataset: {},
    style: { setProperty() {} },
    querySelector(selector) {
      if (selector === ".pct") return { textContent: "" };
      if (selector === ".pct-unit") return { textContent: "" };
      if (selector === ".val.resets") return reset;
      return null;
    },
  };
  const elements = {
    status,
    "gauge-claude-five_hour": gauge,
  };
  const document = {
    getElementById(id) {
      return elements[id] || null;
    },
    querySelectorAll(selector) {
      if (selector === ".resets") return [reset];
      return [];
    },
  };
  const localIntl = {
    DateTimeFormat: function DateTimeFormat(locales, options) {
      return new Intl.DateTimeFormat(locales || "en-GB", {
        ...options,
        timeZone,
      });
    },
  };
  const storage = {
    getItem() {
      return null;
    },
    setItem() {},
  };
  const window = {
    CodervisWidgetState: WidgetState,
    localStorage: storage,
    __INITIAL_PAYLOAD__: {
      claude: {
        enabled: true,
        source: "live",
        source_error: null,
        windows: [
          {
            name: "five_hour",
            percent: 25,
            resets_at: "2026-08-24T14:37:00Z",
          },
        ],
      },
    },
  };

  vm.runInNewContext(appSource, {
    Date,
    EventSource: class EventSource {},
    Intl: localIntl,
    console,
    document,
    setInterval() {},
    setTimeout() {},
    window,
  });

  return reset;
}

test("reset tooltip uses the browser-local date and time", () => {
  assert.equal(
    renderResetIn("Europe/London").title,
    "Monday, 24 August 2026 at 15:37 BST"
  );
  assert.equal(
    renderResetIn("America/New_York").title,
    "Monday, 24 August 2026 at 10:37 GMT-4"
  );
});
