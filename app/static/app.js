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

  function applyGauge(name, w) {
    const root = document.getElementById("gauge-" + name);
    if (!root) return;
    const { hue, sat, lit } = colorFor(w.percent);
    root.style.setProperty("--pct", w.percent);
    root.style.setProperty("--hue", hue.toFixed(1));
    root.style.setProperty("--sat", sat.toFixed(1) + "%");
    root.style.setProperty("--lit", lit.toFixed(1) + "%");
    root.querySelector(".pct").textContent = w.percent.toFixed(1);
    const resets = root.querySelector(".val.resets");
    resets.dataset.iso = w.resets_at || "";
    resets.textContent = formatRelative(w.resets_at);
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
    const la = document.getElementById("last-activity");
    if (la && la.dataset.iso) {
      const t = new Date(la.dataset.iso).getTime();
      if (!Number.isNaN(t)) {
        const ago = Math.max(0, Math.round((Date.now() - t) / 1000));
        const h = Math.floor(ago / 3600);
        const m = Math.floor((ago % 3600) / 60);
        const s = ago % 60;
        la.textContent = h ? `${h}h ${m}m ago` : m ? `${m}m ${s}s ago` : `${s}s ago`;
      }
    }
  }

  function apply(payload) {
    applyGauge("five_hour", payload.five_hour);
    applyGauge("seven_day", payload.seven_day);

    const src = document.getElementById("source");
    src.textContent = payload.source;
    src.dataset.state = payload.source;

    const sub = document.getElementById("subscription");
    sub.textContent = payload.subscription_type || "—";

    const la = document.getElementById("last-activity");
    la.dataset.iso = payload.last_activity || "";

    paintRelativeFields();

    if (payload.source === "live") {
      setStatus("ok", "live");
    } else if (payload.source === "fallback") {
      setStatus("stale", "fallback · " + (payload.source_error || "live unavailable"));
    } else {
      setStatus("error", payload.source_error || "error");
    }
  }

  // Apply initial server-rendered values.
  document.querySelectorAll(".gauge").forEach((g) => {
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
