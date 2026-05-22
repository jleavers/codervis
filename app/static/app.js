(function () {
  const status = document.getElementById("status");
  const statusText = status.querySelector(".status-text");

  function setStatus(state, text) {
    status.dataset.state = state;
    statusText.textContent = text;
  }

  // lime (hue 90) at 0%, amber (45) at 60%, coral (5) at 100%
  function colorFor(percent) {
    const p = Math.max(0, Math.min(100, percent));
    let hue;
    if (p <= 60) {
      hue = 90 - (p / 60) * 45;
    } else {
      hue = 45 - ((p - 60) / 40) * 40;
    }
    const sat = 70 + (p / 100) * 15;
    const lit = 55 - (p / 100) * 5;
    return { hue, sat, lit };
  }

  function applyGauge(provider, w) {
    if (!w) return;
    const root = document.getElementById("gauge-" + provider + "-" + w.name);
    if (!root) return;
    const hasValue = w.percent !== null && w.percent !== undefined;
    root.dataset.hasValue = hasValue ? "1" : "0";

    const pctEl = root.querySelector(".pct");
    const unitEl = root.querySelector(".pct-unit");

    if (hasValue) {
      const { hue, sat, lit } = colorFor(w.percent);
      root.style.setProperty("--pct", w.percent);
      root.style.setProperty("--hue", hue.toFixed(1));
      root.style.setProperty("--sat", sat.toFixed(1) + "%");
      root.style.setProperty("--lit", lit.toFixed(1) + "%");
      pctEl.textContent = w.percent.toFixed(1);
      if (unitEl) unitEl.textContent = "%";
    } else {
      root.style.setProperty("--pct", 0);
      pctEl.textContent = "—";
      if (unitEl) unitEl.textContent = "";
    }

    const resets = root.querySelector(".val.resets");
    resets.dataset.iso = w.resets_at || "";
    resets.textContent = formatRelative(w.resets_at);

    const detail = document.getElementById("detail-" + provider + "-" + w.name);
    if (detail) {
      if (w.detail) {
        detail.querySelector(".val").textContent = w.detail;
        detail.removeAttribute("hidden");
      } else {
        detail.setAttribute("hidden", "");
      }
    }
  }

  function formatRelative(iso) {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "—";
    const delta = Math.round((t - Date.now()) / 1000);
    if (delta <= 0) return "now";
    const d = Math.floor(delta / 86400);
    const h = Math.floor((delta % 86400) / 3600);
    const m = Math.floor((delta % 3600) / 60);
    const s = delta % 60;
    if (d > 0) return `in ${d}d ${h}h`;
    if (h > 0) return `in ${h}h ${m}m`;
    if (m > 0) return `in ${m}m ${s}s`;
    return `in ${s}s`;
  }

  function paintRelativeFields() {
    document.querySelectorAll(".resets").forEach((el) => {
      el.textContent = formatRelative(el.dataset.iso);
    });
    document.querySelectorAll(".last-activity").forEach((el) => {
      el.textContent = formatAgo(el.dataset.iso);
    });
  }

  function formatAgo(iso) {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "—";
    const ago = Math.max(0, Math.round((Date.now() - t) / 1000));
    const d = Math.floor(ago / 86400);
    const h = Math.floor((ago % 86400) / 3600);
    const m = Math.floor((ago % 3600) / 60);
    const s = ago % 60;
    if (d > 0) return `${d}d ${h}h ago`;
    if (h > 0) return `${h}h ${m}m ago`;
    if (m > 0) return `${m}m ${s}s ago`;
    return `${s}s ago`;
  }

  function applyProvider(key, section) {
    if (!section) return;
    (section.windows || []).forEach((w) => applyGauge(key, w));

    const src = document.getElementById("source-" + key);
    if (src) {
      src.textContent = section.source;
      src.dataset.state = section.source;
      if (section.source_error) {
        src.title = section.source_error;
      } else {
        src.removeAttribute("title");
      }
    }

    const sub = document.getElementById("subscription-" + key);
    if (sub) sub.textContent = section.subscription_type || "—";

    const lastActivity = document.getElementById("last-activity-" + key);
    if (lastActivity) lastActivity.dataset.iso = section.last_activity || "";

    const errEl = document.getElementById("error-" + key);
    if (errEl) errEl.textContent = section.source_error || "";

    const provRoot = document.getElementById("provider-" + key);
    if (provRoot) provRoot.dataset.source = section.source;
  }

  const PROVIDERS = ["claude", "codex", "gemini", "cursor", "copilot"];

  function summariseStatus(payload) {
    const present = PROVIDERS.filter((k) => payload[k]);

    const broken = present.find((k) => payload[k].source === "unavailable");
    if (broken) {
      setStatus("stale", broken + ": unavailable · " + (payload[broken].source_error || ""));
      return;
    }

    const allOk = present.every((k) =>
      payload[k].source === "live" || payload[k].source === "disabled"
    );
    if (allOk) {
      const off = present.filter((k) => payload[k].source === "disabled");
      setStatus("ok", off.length ? "live · " + off.join(",") + " off" : "live");
      return;
    }
    setStatus("error", "error");
  }

  function apply(payload) {
    PROVIDERS.forEach((key) => applyProvider(key, payload[key]));

    paintRelativeFields();
    summariseStatus(payload);
  }

  // Apply initial server-rendered values.
  document.querySelectorAll(".gauge").forEach((g) => {
    if (g.dataset.hasValue === "0") return;
    const pct = parseFloat(g.dataset.percent || "0");
    const { hue, sat, lit } = colorFor(pct);
    g.style.setProperty("--pct", pct);
    g.style.setProperty("--hue", hue.toFixed(1));
    g.style.setProperty("--sat", sat.toFixed(1) + "%");
    g.style.setProperty("--lit", lit.toFixed(1) + "%");
  });
  paintRelativeFields();

  function connect() {
    setStatus("stale", "connecting…");
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
      try { apply(JSON.parse(ev.data)); }
      catch (e) { console.error(e); }
    };
    es.onerror = () => {
      setStatus("error", "reconnecting…");
      es.close();
      setTimeout(connect, 2000);
    };
  }
  connect();

  setInterval(paintRelativeFields, 1000);
})();
