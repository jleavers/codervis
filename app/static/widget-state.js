(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.CodervisWidgetState = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const PROVIDERS = ["claude", "codex"];
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
