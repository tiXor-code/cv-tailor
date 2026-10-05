const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["token", "adminBase"]).then(({ token, adminBase }) => {
  $("token").value = token || "";
  $("adminBase").value = adminBase || "";
});
$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ token: $("token").value.trim(), adminBase: $("adminBase").value.trim() });
  $("status").textContent = "Testing...";
  const res = await chrome.runtime.sendMessage({ type: "ping" });
  $("status").textContent = res && res.ok ? "Connected. Open a job from your Scout list." : `Not connected: ${res ? res.error : "no response"}`;
});

// ---- autopilot (on unless he turned it off) and "Run my list" ----
let paused = false;
let run = null;

function render() {
  $("pause").textContent = paused ? "Resume" : "Pause";
  const active = Boolean(run && run.active);
  $("run").hidden = active;
  $("stop").hidden = !active;
  const items = [];
  let text = "";
  if (active) {
    text = `Running: job ${Math.min(run.index + 1, run.jobs.length)} of ${run.jobs.length}`;
    if (run.paused) text += " (paused)";
    else if (run.phase === "pause") text += `. Next job in ${Math.max(0, Math.round((run.nextAt - Date.now()) / 1000))} s`;
  } else if (run && run.finished) {
    const applied = run.results.filter((r) => r.outcome === "applied");
    const parked = run.results.filter((r) => r.outcome === "parked");
    const left = run.jobs.length - run.results.length;
    text = run.jobs.length
      ? `Run ${run.stopped ? "stopped" : "finished"}: applied ${applied.length}, parked ${parked.length}${left ? `, ${left} not started` : ""}.`
      : "Your Scout list had nothing to apply to.";
    for (const r of run.results) {
      const name = `${r.company || r.id}${r.title ? ` - ${r.title}` : ""}`;
      items.push(r.outcome === "applied" ? `Applied: ${name}`
        : `Parked: ${name}: ${r.reason}${r.questions.length ? ` (${r.questions.join("; ")})` : ""}`);
    }
  }
  $("run-status").textContent = text;
  $("run-results").replaceChildren(...items.map((t) => {
    const li = document.createElement("li");
    li.textContent = t; // admin's job names and site labels: text only
    return li;
  }));
}

chrome.storage.local.get(["autopilot", "autopilotPaused", "runPauseMin", "runPauseMax", "scoutRun"]).then((g) => {
  $("autopilot").checked = g.autopilot !== false;
  $("pauseMin").value = g.runPauseMin || 20;
  $("pauseMax").value = g.runPauseMax || 40;
  paused = Boolean(g.autopilotPaused);
  run = g.scoutRun || null;
  render();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.autopilotPaused) paused = Boolean(changes.autopilotPaused.newValue);
  if (changes.scoutRun) run = changes.scoutRun.newValue || null;
  if (changes.autopilot) $("autopilot").checked = changes.autopilot.newValue !== false;
  render();
});
setInterval(() => { if (run && run.active) render(); }, 1000);

$("autopilot").addEventListener("change", () => chrome.storage.local.set({ autopilot: $("autopilot").checked }));
function savePause() {
  const lo = Math.max(1, Math.round(Number($("pauseMin").value) || 20));
  const hi = Math.max(lo, Math.round(Number($("pauseMax").value) || 40));
  chrome.storage.local.set({ runPauseMin: lo, runPauseMax: hi });
}
$("pauseMin").addEventListener("change", savePause);
$("pauseMax").addEventListener("change", savePause);

$("run").addEventListener("click", async () => {
  $("run").disabled = true;
  $("run-status").textContent = "Fetching your list...";
  const res = await chrome.runtime.sendMessage({ type: "run-start" });
  $("run").disabled = false;
  if (!res || !res.ok) $("run-status").textContent = `Could not start: ${res ? res.error : "no response"}`;
});
$("pause").addEventListener("click", () => chrome.runtime.sendMessage({ type: "run-pause", paused: !paused }));
$("stop").addEventListener("click", () => chrome.runtime.sendMessage({ type: "run-stop" }));
