// Light rig: the live page
(() => {
  const $ = (id) => document.getElementById(id);
  const catalog = JSON.parse($("catalog").textContent);
  const WINDOW = 15 * 60;          // seconds of readings on the charts
  const STALE = 90;                // seconds without word from the Pi before it counts as offline

  let status = { state: "unknown" };
  let sensor = [];
  let led = [];
  let run = null;
  let charts = null;
  let drawPending = false;

  // ---------------- charts ----------------
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function makeCharts() {
    for (const id of ["chart-counts", "chart-leds"]) $(id).textContent = "";
    const width = $("chart-counts").clientWidth || 600;
    const font = `12px ${css("--font")}`;
    const axis = () => ({
      stroke: css("--ink-2"), font, grid: { stroke: css("--rule"), width: 1 }, ticks: { stroke: css("--rule"), width: 1 },
    });
    // clock times on the x axis, with seconds when the ticks are less than a minute apart
    const timeAxis = () => ({
      ...axis(), space: 70,
      values: (u, splits, ax, space, incr) => splits.map((t) => new Date(t * 1000).toLocaleTimeString([],
        incr < 60 ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" })),
    });
    const cursor = { sync: { key: "rig" }, points: { size: 6 } };
    const hooks = { setCursor: [showHover] };
    const counts = new uPlot({
      width, height: 220, cursor, hooks, legend: { show: false }, scales: { x: { time: true } },
      series: [{}, { label: "Sensor counts", stroke: css("--counts"), width: 2, points: { show: false } }],
      axes: [timeAxis(), { ...axis(), size: 56 }],
    }, [[], []], $("chart-counts"));
    const step = uPlot.paths.stepped({ align: 1 });
    const leds = new uPlot({
      width, height: 130, cursor, hooks, legend: { show: false }, scales: { x: { time: true }, y: { range: [0, 1.05] } },
      series: [
        {},
        { label: "Test LED", stroke: css("--test"), width: 2, paths: step, points: { show: false } },
        { label: "Pollution LED", stroke: css("--pollution"), width: 2, paths: step, points: { show: false } },
      ],
      axes: [timeAxis(), { ...axis(), size: 56, splits: () => [0, 0.5, 1], values: (u, v) => v.map((x) => x.toFixed(1)) }],
    }, [[], [], []], $("chart-leds"));
    charts = { counts, leds };
    draw();
  }

  // The value under the cursor, in the chart headings (the two charts share their times)
  function showHover(u) {
    if (!charts) return;
    const i = u.cursor.idx;
    const t = i == null ? null : charts.counts.data[0][i];
    if (t == null) { $("hover-counts").textContent = ""; $("hover-leds").textContent = ""; return; }
    const at = new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const count = charts.counts.data[1][i];
    $("hover-counts").textContent = count == null ? at : `${at}: ${count}`;
    $("hover-leds").textContent = `${at}: test ${levelText(charts.leds.data[1][i])}, pollution ${levelText(charts.leds.data[2][i])}`;
  }

  // Keep the last 15 minutes, plus each LED's last change before that, so its level is known throughout
  function trim(now) {
    const cutoff = now - WINDOW;
    sensor = sensor.filter((p) => p.t >= cutoff);
    const before = {};
    for (const e of led) if (e.t < cutoff && (!before[e.led] || e.t >= before[e.led].t)) before[e.led] = e;
    led = [...Object.values(before), ...led.filter((e) => e.t >= cutoff)];
  }

  function draw() {
    drawPending = false;
    if (!charts) return;
    const now = Date.now() / 1000;
    trim(now);
    const events = led.slice().sort((a, b) => a.t - b.t);
    const xs = [], ys = [], test = [], pollution = [];
    const level = { test: null, pollution: null };
    let i = 0, prev = null;
    for (const p of sensor) {
      while (i < events.length && events[i].t <= p.t) { level[events[i].led] = events[i].level; i++; }
      if (prev !== null && p.t - prev > 5) {  // a pause in the readings: break the lines
        xs.push(prev + 1); ys.push(null); test.push(null); pollution.push(null);
      }
      xs.push(p.t); ys.push(p.full); test.push(level.test); pollution.push(level.pollution);
      prev = p.t;
    }
    charts.counts.setData([xs, ys]);
    charts.leds.setData([xs, test, pollution]);
    $("chart-empty").hidden = xs.length > 0;
  }

  function drawSafely() {
    try { draw(); } catch (err) { console.error(err); }
  }

  function scheduleDraw() {
    if (!drawPending) { drawPending = true; setTimeout(() => requestAnimationFrame(drawSafely), 400); }
  }

  new ResizeObserver(() => {
    if (!charts) return;
    const width = $("chart-counts").clientWidth;
    charts.counts.setSize({ width, height: 220 });
    charts.leds.setSize({ width, height: 130 });
  }).observe($("chart-counts"));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { try { makeCharts(); } catch (err) { console.error(err); } });

  // ---------------- readout ----------------
  const levelText = (v) => (v == null ? "–" : v === 0 ? "off" : Number(v).toFixed(2));

  function showReading(p) {
    $("full").textContent = p.full;
    $("ir").textContent = p.ir ?? "–";
  }

  function showLevels() {
    const latest = { test: null, pollution: null };
    for (const e of led) latest[e.led] = e.level;
    $("test-level").textContent = levelText(latest.test);
    $("pollution-level").textContent = levelText(latest.pollution);
  }

  function updateNote() {
    const note = $("reading-note");
    const last = sensor[sensor.length - 1];
    note.classList.remove("warn");
    if (!last) { note.textContent = "Waiting for readings"; return; }
    const age = Date.now() / 1000 - last.t;
    if (last.saturated) { note.textContent = "Saturated: the sensor is at its limit"; note.classList.add("warn"); }
    else if (age > 10) note.textContent = `Last reading ${Rig.minutes(age / 60)} ago`;
    else note.textContent = `Run ${last.run.split("_").slice(1).join(" ").replace(/^(\d+) /, "$1, ")}`;
  }

  // ---------------- session state ----------------
  function effectiveState() {
    const age = Date.now() / 1000 - (status.t || 0);
    if (!["offline", "unknown"].includes(status.state) && age > STALE) return "offline";
    return status.state;
  }

  function applyStatus() {
    const state = effectiveState();
    Rig.setPi({ state });
    const running = state === "running" || state === "stopping";
    $("running").hidden = !running;
    $("busy").hidden = state !== "busy";
    $("start-form").hidden = running || state === "busy";

    if (running && status.job) renderRunning(state);
    if (state === "busy" && status.manual) $("busy-command").textContent = `${status.manual.command} (pid ${status.manual.pid})`;

    const offline = state === "offline" || state === "unknown";
    $("start").disabled = offline;
    $("start-hint").textContent = offline
      ? "The Pi is offline, so sessions can't start. When it's on and its agent is running, the header says Pi online."
      : "";
    renderLast(running);
  }

  function renderRunning(state) {
    const job = status.job;
    const now = Date.now() / 1000;
    $("running-title").textContent = state === "stopping" ? `Stopping the ${job.plan} session` : `Session running: ${job.plan}`;
    const row = job.rows[job.position - 1];
    const purpose = run && run.run === job.run ? run.purpose : null;
    const now_ = $("running-now");
    now_.textContent = "";
    if (row) {
      now_.append(`Run ${job.position} of ${job.total}: `);
      const strong = document.createElement("strong");
      strong.textContent = row.name;
      now_.append(strong);
      if (purpose) now_.append(`. ${purpose}.`);
    } else now_.textContent = "Getting the sensor and LEDs ready.";
    Rig.timeline($("timeline"), job.rows, { position: job.position, runStarted: job.run_started, now });
    Rig.legend($("timeline-legend"), job.rows);
    let left = 0;
    if (row) {
      const elapsed = job.run_started ? (now - job.run_started) / 60 : 0;
      left = Math.max(0, row.minutes - elapsed) + job.rows.slice(job.position).reduce((s, r) => s + r.minutes, 0);
    } else left = job.total_minutes;
    $("running-times").textContent = `Started ${Rig.clock(job.started)}, seed ${job.seed}. About ${Rig.minutes(left)} to go, ` +
      `done around ${Rig.clock(now + left * 60)}.` + (job.adopted ? " (Picked up again after the Pi agent restarted.)" : "");
    $("stop").disabled = state === "stopping";
    if (state === "stopping") resetConfirm("stop", "Stop session");
  }

  function renderLast(running) {
    const last = status.last;
    const el = $("last-result");
    el.textContent = "";
    if (running || !last) return;
    const words = { finished: "finished", "stopped early": "was stopped early", failed: "failed", ended: "ended",
                    "ended while the agent was off": "ended while the Pi agent was off" }[last.result] || last.result;
    el.append(`Last session (${last.plan}) ${words}` + (last.ended ? ` at ${Rig.clock(last.ended)}` : "") + ". ");
    if (last.session) {
      const a = document.createElement("a");
      a.href = `/sessions/${last.session}`;
      a.textContent = "See its data";
      el.append(a);
    }
    if (last.message && last.result === "failed") el.append(` The Pi said: ${last.message}`);
  }

  // ---------------- console ----------------
  function addLog(line) {
    const pre = $("console");
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.append(line + "\n");
    while (pre.childNodes.length > 400) pre.removeChild(pre.firstChild);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  // ---------------- live events ----------------
  function handle(ev) {
    switch (ev.type) {
      case "snapshot":
        status = ev.status; sensor = ev.sensor; led = ev.led; run = ev.run;
        $("console").textContent = "";
        ev.logs.forEach((l) => addLog(l.line));
        if (sensor.length) showReading(sensor[sensor.length - 1]);
        showLevels(); updateNote(); applyStatus(); scheduleDraw();
        break;
      case "sensor":
        sensor.push(ev); showReading(ev); updateNote(); scheduleDraw();
        break;
      case "led":
        led.push(ev); showLevels(); scheduleDraw();
        break;
      case "run":
        run = ev.run; applyStatus();
        break;
      case "status":
        status = ev.status; applyStatus();
        break;
      case "log":
        addLog(ev.line);
        break;
    }
  }

  function connect() {
    const source = new EventSource("/api/events");
    source.onmessage = (e) => handle(JSON.parse(e.data));
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) setTimeout(() => location.reload(), 3000);
    };
  }

  // ---------------- start form ----------------
  const form = $("start-form");
  const plan = () => form.querySelector('input[name="plan"]:checked').value;

  function renderRuns() {
    const p = catalog.plans.find((x) => x.name === plan());
    const list = $("run-list");
    list.textContent = "";
    for (const r of p.runs) {
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" checked data-run><span class="num"></span><span></span>`;
      label.querySelector("input").value = r.name;
      label.querySelector(".num").textContent = r.number;
      label.lastChild.textContent = r.name;
      list.append(label);
    }
  }

  function options() {
    const out = {};
    const value = (name) => form.elements[name].value.trim();
    if (value("seed")) out.seed = value("seed");
    if (value("time_scale")) out.time_scale = value("time_scale");
    const boxes = [...form.querySelectorAll("[data-run]")];
    const ticked = boxes.filter((b) => b.checked).map((b) => b.value);
    if (ticked.length < boxes.length) out.only = ticked;
    for (const field of form.querySelectorAll("[data-option]")) {
      const v = field.value.trim();
      if (!v) continue;
      out[field.name] = field.hasAttribute("data-list") ? v.split(/[\s,]+/).filter(Boolean) : v;
    }
    return out;
  }

  let previewOk = false;
  let previewTimer = null;
  let previewTotal = 0;

  async function preview() {
    resetConfirm("start", "Start session");
    const boxes = [...form.querySelectorAll("[data-run]")];
    if (!boxes.some((b) => b.checked)) {
      showPreviewError("Tick at least one run.");
      return;
    }
    const result = await Rig.post("/api/preview", { plan: plan(), options: options() });
    if (!result.ok) { showPreviewError(result.error); return; }
    previewOk = true;
    previewTotal = result.total_minutes;
    $("start-error").textContent = "";
    $("preview").hidden = false;
    Rig.timeline($("preview-timeline"), result.rows);
    Rig.legend($("preview-legend"), result.rows);
    let total = `${result.rows.length} run${result.rows.length === 1 ? "" : "s"}, about ${Rig.minutes(result.total_minutes)}.`;
    if (result.plan === "pollution-levels") {
      total += result.order_known
        ? ` Pollution duty order: ${result.pollution_order.join(", ")}.`
        : " The pollution duty blocks run in an order drawn from the new seed when the session starts.";
    }
    $("preview-total").textContent = total;
    const body = $("preview-rows");
    body.textContent = "";
    for (const r of result.rows) {
      const tr = document.createElement("tr");
      for (const [text, cls] of [[r.number, ""], [r.name, ""], [Rig.minutes(r.minutes), "num"], [r.test_led, ""], [r.pollution, ""]]) {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls) td.className = cls;
        tr.append(td);
      }
      tr.title = r.purpose;
      body.append(tr);
    }
  }

  function showPreviewError(message) {
    previewOk = false;
    $("start-error").textContent = message;
    $("preview").hidden = true;
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 300);
  }

  function resetConfirm(id, text) {
    const button = $(id);
    button.textContent = text;
    button.classList.remove("confirming");
    delete button.dataset.armed;
    $(`${id}-cancel`).hidden = true;
  }

  function arm(id, text) {
    const button = $(id);
    button.textContent = text;
    button.classList.add("confirming");
    button.dataset.armed = "1";
    $(`${id}-cancel`).hidden = false;
    button.focus();
  }

  form.addEventListener("change", (e) => {
    if (e.target.name === "plan") renderRuns();
    schedulePreview();
  });
  form.addEventListener("input", (e) => { if (e.target.matches("input:not([type=checkbox]):not([type=radio])")) schedulePreview(); });
  $("runs-all").addEventListener("click", () => { form.querySelectorAll("[data-run]").forEach((b) => { b.checked = true; }); schedulePreview(); });
  $("runs-none").addEventListener("click", () => { form.querySelectorAll("[data-run]").forEach((b) => { b.checked = false; }); schedulePreview(); });
  $("start-cancel").addEventListener("click", () => resetConfirm("start", "Start session"));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!previewOk) { await preview(); if (!previewOk) return; }
    const button = $("start");
    if (!button.dataset.armed) {
      arm("start", `Start now: ${plan()}, about ${Rig.minutes(previewTotal)}`);
      return;
    }
    button.disabled = true;
    button.textContent = "Starting…";
    const result = await Rig.post("/api/command", { action: "start", plan: plan(), options: options() });
    button.disabled = false;
    resetConfirm("start", "Start session");
    if (!result.ok) $("start-error").textContent = result.error;
  });

  // ---------------- stop ----------------
  async function stop(id, errorId, text) {
    const button = $(id);
    if (!button.dataset.armed) { arm(id, text); return; }
    button.disabled = true;
    const result = await Rig.post("/api/command", { action: "stop" });
    button.disabled = false;
    resetConfirm(id, id === "stop" ? "Stop session" : "Stop it");
    $(errorId).textContent = result.ok ? "" : result.error;
  }
  $("stop").addEventListener("click", () => stop("stop", "stop-error", "Stop now: end the current run early"));
  $("stop-cancel").addEventListener("click", () => resetConfirm("stop", "Stop session"));
  $("busy-stop").addEventListener("click", () => stop("busy-stop", "busy-error", "Stop it now"));
  $("busy-cancel").addEventListener("click", () => resetConfirm("busy-stop", "Stop it"));

  // ---------------- go ----------------
  renderRuns();
  preview();
  connect();  // first, so the page stays live even if the charts can't be drawn
  try {
    makeCharts();
  } catch (err) {
    charts = null;
    $("chart-empty").textContent = "The charts couldn't be drawn in this browser. The numbers on the left still update.";
    console.error(err);
  }
  setInterval(() => { updateNote(); applyStatus(); }, 1000);
})();
