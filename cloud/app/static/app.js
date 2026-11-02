// Light rig: helpers shared by every page
const Rig = (() => {
  const PI_LABELS = {
    idle: "online", running: "running a session", stopping: "stopping the session",
    busy: "busy with a script started on it",
  };
  const KIND_LABELS = {
    dark: "Dark", settle: "Warm-up and baselines", clean: "Clean",
    low: "Pollution low", high: "Pollution high", check: "Pollution check",
  };

  function setPi(status) {
    const el = document.getElementById("pi-status");
    if (!el) return;
    const state = PI_LABELS[status.state] ? status.state : "offline";
    const who = document.body.dataset.mock === "1" ? "Mock Pi" : "Pi";
    el.dataset.state = state;
    el.textContent = `${who} ${PI_LABELS[state] || "offline"}`;
  }

  async function post(url, data) {
    try {
      const r = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}),
      });
      if (r.status === 401) return { ok: false, error: "You're logged out. Reload the page to log in." };
      try { return await r.json(); } catch { return { ok: false, error: `The app answered ${r.status}.` }; }
    } catch {
      return { ok: false, error: "Can't reach the app. Check your connection." };
    }
  }

  function minutes(m) {
    if (m < 1) return `${Math.round(m * 60)} s`;
    if (m < 60) return `${Math.round(m)} min`;
    const h = Math.floor(m / 60), rest = Math.round(m - h * 60);
    return rest ? `${h} h ${rest} min` : `${h} h`;
  }

  function clock(t) {
    return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function kindOf(name) {
    if (name === "dark") return "dark";
    if (/^(warmup|baseline)/.test(name)) return "settle";
    if (name === "pollution_check") return "check";
    if (/_low$/.test(name)) return "low";
    if (/_high$/.test(name)) return "high";
    return "clean";
  }

  // One segment per run, as wide as the run is long. With a position, runs before it are done,
  // the current one fills up as it goes, and the rest are faded.
  function timeline(el, rows, { position = 0, runStarted = null, now = Date.now() / 1000 } = {}) {
    el.textContent = "";
    el.classList.toggle("live-progress", position > 0);
    const total = rows.reduce((sum, r) => sum + r.minutes, 0) || 1;
    rows.forEach((r, i) => {
      const seg = document.createElement("div");
      seg.className = "seg";
      seg.dataset.kind = r.kind || kindOf(r.name);
      seg.style.flex = `${r.minutes} 1 0`;
      seg.title = `${r.number} ${r.name}, ${minutes(r.minutes)}`;
      if (position) {
        if (i + 1 < position) seg.classList.add("done");
        else if (i + 1 === position) {
          seg.classList.add("current");
          const done = runStarted ? Math.max(0, Math.min(1, (now - runStarted) / 60 / r.minutes)) : 0;
          seg.style.setProperty("--done", `${(done * 100).toFixed(1)}%`);
        } else seg.classList.add("upcoming");
      }
      if (r.minutes / total > 0.045) {
        const label = document.createElement("span");
        label.className = "seg-label";
        label.textContent = r.number;
        seg.append(label);
      }
      el.append(seg);
    });
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", `${rows.length} runs, ${minutes(total)} in all` +
      (position ? `, now on run ${position}` : ""));
  }

  function legend(el, rows) {
    const kinds = [...new Set(rows.map(r => r.kind || kindOf(r.name)))];
    el.textContent = "";
    for (const kind of Object.keys(KIND_LABELS)) {
      if (!kinds.includes(kind)) continue;
      const item = document.createElement("span");
      item.innerHTML = `<i class="k-${kind}"></i>`;
      item.append(KIND_LABELS[kind]);
      el.append(item);
    }
  }

  // Setup page: copy and reveal the env file
  document.addEventListener("click", async (e) => {
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      const pre = document.getElementById(copy.dataset.copy);
      try {
        await navigator.clipboard.writeText(pre.dataset.secret || pre.textContent);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Select and copy it by hand";
      }
      setTimeout(() => { copy.textContent = "Copy"; }, 2000);
    }
    const reveal = e.target.closest("[data-reveal]");
    if (reveal) {
      const pre = document.getElementById(reveal.dataset.reveal);
      if (!pre.dataset.masked) pre.dataset.masked = pre.textContent;
      const showing = pre.textContent === pre.dataset.secret;
      pre.textContent = showing ? pre.dataset.masked : pre.dataset.secret;
      reveal.textContent = showing ? "Show password" : "Hide password";
    }
  });

  return { setPi, post, minutes, clock, kindOf, timeline, legend };
})();
