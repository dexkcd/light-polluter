// Light rig: a session's page
(() => {
  const $ = (id) => document.getElementById(id);

  $("run-check").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    const sid = button.dataset.session;
    button.disabled = true;
    $("check-status").textContent = "Running the checks and plotting each run. This takes up to a minute…";
    const result = await Rig.post(`/sessions/${sid}/check`, {});
    button.disabled = false;
    if (!result.ok) { $("check-status").textContent = result.error || "The checks didn't run."; return; }
    $("check-status").textContent = "Done just now.";
    $("check-output").hidden = false;
    $("check-text").textContent = result.output;
    const plots = $("plots");
    plots.textContent = "";
    for (const name of result.plots) {
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.src = `/files/${name}?t=${Date.now()}`;
      img.alt = `Plot of ${name.replace(/_plot\.png$/, "")}: sensor counts and LED levels`;
      img.loading = "lazy";
      const cap = document.createElement("figcaption");
      cap.textContent = name.replace(`${sid}_`, "").replace(/_plot\.png$/, "").replace("_", " ");
      fig.append(img, cap);
      plots.append(fig);
    }
    $("check-output").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  $("fetch-backup").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    const sid = button.dataset.session;
    const status = $("backup-status");
    button.disabled = true;
    status.textContent = "Asking the Pi for its copy…";
    const reply = await Rig.post("/api/command", { action: "backup", session: sid });
    if (!reply.ok) { button.disabled = false; status.textContent = reply.error; return; }
    status.textContent = `The Pi is sending ${reply.runs} run${reply.runs === 1 ? "" : "s"}…`;
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const r = await fetch(`/api/backup/${sid}`).then((x) => x.json()).catch(() => ({}));
      const runs = r.result ? Object.values(r.result.runs) : [];
      if (runs.length >= reply.runs) {
        const added = { sensor: 0, led: 0, meta: 0 };
        for (const a of runs) for (const k in a) added[k] += a[k];
        status.textContent = added.sensor + added.led + added.meta
          ? `Filled in ${added.sensor} readings, ${added.led} LED events and ${added.meta} meta lines. Reloading…`
          : "Nothing was missing: the Pi's copy matches what's here.";
        if (added.sensor + added.led + added.meta) setTimeout(() => location.reload(), 1500);
        button.disabled = false;
        return;
      }
    }
    button.disabled = false;
    status.textContent = "The Pi's copy didn't finish arriving within a minute. Try again.";
  });
})();
