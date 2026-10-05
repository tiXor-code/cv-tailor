// Scout Fill -- background service worker.
// The only component that talks to admin.teodorlutoiu.com, with the personal
// extension key from chrome.storage.local (never synced, never in the page).
// Content scripts ask it for a fixed set of things; anything else is refused.
// It also drives "Run my list": one tab, one job at a time (see below).

const DEFAULT_BASE = "https://admin.teodorlutoiu.com";
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

async function config() {
  const { token, adminBase } = await chrome.storage.local.get(["token", "adminBase"]);
  return { token: token || "", base: (adminBase || DEFAULT_BASE).replace(/\/+$/, "") };
}

async function api(path, init = {}) {
  const { token, base } = await config();
  if (!token) throw new Error("no-key: open the Scout Fill options and paste your key");
  const res = await fetch(base + path, {
    ...init,
    headers: { ...(init.headers || {}), authorization: `Bearer ${token}` },
  });
  if (res.status === 401) throw new Error("bad-key: the key was refused; copy it again from admin /scout/extension");
  if (!res.ok) throw new Error(`admin ${res.status}`);
  return res;
}

function job(msg) {
  if (!DATE_RE.test(String(msg.date || "")) || !ID_RE.test(String(msg.id || ""))) throw new Error("bad-job");
  return { date: msg.date, id: msg.id };
}

async function postPark(body) {
  return (await api("/api/scout/ext/park", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })).json();
}

// ------------------------------------------------------------ run my list ----
// Teodor, 2026-10-05: "I don't want to click anything." One dedicated tab works
// through his list, best first: open a job's form, let autopilot fill and
// submit (or park), pause 20-40 s, next. The state lives in storage.local so a
// sleeping service worker or a page load never loses it; alarms wake it.
const RUN_KEY = "scoutRun";
const WATCH_ALARM = "scout-run-watch";
const NEXT_ALARM = "scout-run-next";
const HELLO_MS = 90 * 1000;    // no content script by then: a site it can't run on
const JOB_MS = 8 * 60 * 1000;  // one job never takes longer than this

let lock = Promise.resolve();
function locked(fn) {
  const p = lock.then(fn, fn);
  lock = p.catch(() => {});
  return p;
}
async function getRun() {
  return (await chrome.storage.local.get(RUN_KEY))[RUN_KEY] || null;
}
async function putRun(run) {
  await chrome.storage.local.set({ [RUN_KEY]: run });
}
async function pauseRange() {
  const got = await chrome.storage.local.get(["runPauseMin", "runPauseMax"]);
  const num = (v, d) => (Number.isFinite(Number(v)) && Number(v) >= 1 ? Math.min(Number(v), 3600) : d);
  const lo = num(got.runPauseMin, 20);
  return [lo, Math.max(lo, num(got.runPauseMax, 40))];
}
function queuedJob(raw) {
  try {
    const { date, id } = job(raw || {});
    const url = String(raw.form_url || "").slice(0, 2000);
    if (!url.startsWith("https://")) throw new Error("not an https page");
    new URL(url);
    const str = (v, n) => String(v || "").slice(0, n);
    return { date, id, company: str(raw.company, 200), title: str(raw.title, 300), source: str(raw.source, 50), form_url: url };
  } catch {
    return null;
  }
}
function jobUrl(j) {
  const u = new URL(j.form_url);
  u.hash = `scout-fill=${j.date}~${j.id}`;
  return u.href;
}

async function startRun() {
  const old = await getRun();
  if (old && old.active) return old;
  const data = await (await api("/api/scout/ext/queue")).json();
  const seen = new Set();
  const jobs = [];
  for (const raw of (Array.isArray(data && data.jobs) ? data.jobs : []).slice(0, 200)) {
    const j = queuedJob(raw);
    if (!j || seen.has(`${j.date}~${j.id}`)) continue;
    seen.add(`${j.date}~${j.id}`);
    jobs.push(j);
  }
  const run = { active: jobs.length > 0, paused: false, finished: jobs.length === 0, stopped: false, jobs, index: 0,
    results: [], phase: jobs.length ? "job" : "done", current: null, nextAt: 0, tabId: null, startedAt: Date.now() };
  await chrome.storage.local.set({ autopilotPaused: false });
  if (!jobs.length) {
    await putRun(run);
    return run;
  }
  await openJob(run);
  chrome.alarms.create(WATCH_ALARM, { periodInMinutes: 0.5 });
  return run;
}

async function openJob(run) {
  const j = run.jobs[run.index];
  run.phase = "job";
  run.nextAt = 0;
  run.current = { date: j.date, id: j.id, startedAt: Date.now(), hello: false };
  await putRun(run); // before the page loads, so its hello finds this job
  const url = jobUrl(j);
  let tab = null;
  if (run.tabId !== null) {
    try { tab = await chrome.tabs.update(run.tabId, { url, active: true }); } catch { tab = null; }
  }
  if (!tab) tab = await chrome.tabs.create({ url, active: true });
  run.tabId = tab.id;
  await putRun(run);
}

async function finishJob(run, result) {
  const j = run.jobs[run.index];
  run.results.push({ date: j.date, id: j.id, company: j.company, title: j.title, outcome: result.outcome,
    reason: result.reason || "", questions: result.questions || [] });
  run.index += 1;
  run.current = null;
  if (run.index >= run.jobs.length) {
    Object.assign(run, { active: false, finished: true, phase: "done", endedAt: Date.now() });
    chrome.alarms.clear(WATCH_ALARM);
    await putRun(run);
    return;
  }
  const [lo, hi] = await pauseRange();
  const ms = Math.round((lo + Math.random() * (hi - lo)) * 1000);
  run.phase = "pause";
  run.nextAt = Date.now() + ms;
  await putRun(run);
  if (!run.paused) scheduleNext(ms);
}

function scheduleNext(ms) {
  chrome.alarms.create(NEXT_ALARM, { when: Date.now() + ms });
  setTimeout(() => locked(advance), ms + 50); // while the worker is awake; the alarm otherwise
}

async function advance() {
  const run = await getRun();
  if (!run || !run.active || run.paused || run.phase !== "pause") return;
  if (Date.now() < run.nextAt - 100) return;
  await openJob(run);
}

// The run's tab reported its current job applied or parked: record it, move on.
async function jobOutcome(sender, ref, result) {
  const tabId = sender && sender.tab ? sender.tab.id : -1;
  await locked(async () => {
    const run = await getRun();
    if (!run || !run.active || tabId !== run.tabId || run.phase !== "job") return;
    if (!run.current || run.current.date !== ref.date || run.current.id !== ref.id) return;
    await finishJob(run, result);
  });
}

async function watch() {
  const run = await getRun();
  if (!run || !run.active) {
    chrome.alarms.clear(WATCH_ALARM);
    return;
  }
  if (run.paused) return;
  if (run.phase === "pause") return advance(); // a timer the sleeping worker missed
  const c = run.current;
  if (run.phase !== "job" || !c) return;
  const age = Date.now() - c.startedAt;
  let reason = "";
  if (!c.hello && age > HELLO_MS) reason = "not-supported: Scout Fill could not run on this page (a site it does not cover yet, or the page did not load)";
  else if (age > JOB_MS) reason = "timeout: the application was not finished within 8 minutes";
  if (!reason) return;
  try { await postPark({ date: c.date, id: c.id, reason, questions: [] }); } catch {}
  await finishJob(run, { outcome: "parked", reason, questions: [] });
}

async function stopRun() {
  const run = await getRun();
  if (run && run.active) {
    Object.assign(run, { active: false, stopped: true, finished: true, phase: "done", endedAt: Date.now() });
    chrome.alarms.clear(WATCH_ALARM);
    chrome.alarms.clear(NEXT_ALARM);
    await putRun(run);
  }
  return run;
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === WATCH_ALARM) locked(watch);
  if (a.name === NEXT_ALARM) locked(advance);
});
chrome.tabs.onRemoved.addListener((tabId) => {
  locked(async () => {
    const run = await getRun();
    if (run && run.active && run.tabId === tabId) await stopRun(); // he closed the run's tab
  });
});

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(out);
}

async function handle(msg, sender) {
  switch (msg && msg.type) {
    case "lookup": {
      const url = String(msg.url || "").slice(0, 2000);
      const title = String(msg.title || "").slice(0, 500);
      return (await api(`/api/scout/ext/lookup?url=${encodeURIComponent(url)}&title=${encodeURIComponent(title)}`)).json();
    }
    case "adopt": {
      // a job he opened himself: the mini finds its JD, lists it, tailors the CV
      const str = (v, n) => String(v || "").slice(0, n);
      const url = str(msg.url, 2000);
      if (!url.startsWith("https://")) throw new Error("not an https page");
      return (await api("/api/scout/ext/adopt", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url, title: str(msg.title, 500), company: str(msg.company, 200), text: str(msg.text, 60000) }),
      })).json();
    }
    case "answer": {
      // date+id = a job on his list; neither = any other (LinkedIn Easy Apply).
      const ref = msg.date === undefined && msg.id === undefined ? {} : job(msg);
      const questions = Array.isArray(msg.questions) ? msg.questions.slice(0, 60) : [];
      return (await api("/api/scout/ext/answer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...ref, questions }),
      })).json();
    }
    case "layout": {
      const html = String(msg.html || "").slice(0, 2 * 1024 * 1024);
      return (await api("/api/scout/ext/layout", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: String(msg.url || "").slice(0, 2000), html }),
      })).json();
    }
    case "cv": {
      const { date, id } = job(msg);
      const kind = msg.kind === "cover" ? "&kind=cover" : ""; // the cover letter as a PDF
      const res = await api(`/api/scout/ext/cv?date=${date}&id=${encodeURIComponent(id)}${kind}`);
      return { base64: toBase64(await res.arrayBuffer()) };
    }
    case "applied": {
      const { date, id } = job(msg);
      const data = await (await api("/api/scout/ext/applied", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ date, id }),
      })).json();
      await jobOutcome(sender, { date, id }, { outcome: "applied" });
      return data;
    }
    case "park": {
      // autopilot could not finish this job: admin lists it with the reason
      // and the questions only he can answer
      const { date, id } = job(msg);
      const reason = String(msg.reason || "").slice(0, 300);
      if (!reason) throw new Error("bad-park: no reason");
      const questions = (Array.isArray(msg.questions) ? msg.questions : []).slice(0, 30)
        .map((q) => String(q || "").slice(0, 500)).filter(Boolean);
      const data = await postPark({ date, id, reason, questions });
      await jobOutcome(sender, { date, id }, { outcome: "parked", reason, questions });
      return data;
    }
    case "run-start":
      return locked(startRun);
    case "run-stop":
      return locked(stopRun);
    case "run-state":
      return getRun();
    case "run-pause": {
      const paused = Boolean(msg.paused);
      await chrome.storage.local.set({ autopilotPaused: paused });
      return locked(async () => {
        const run = await getRun();
        if (run && run.active) {
          run.paused = paused;
          if (!paused && run.current) run.current.startedAt = Date.now(); // the watchdog starts over
          if (!paused && run.phase === "pause") scheduleNext(Math.max(0, run.nextAt - Date.now()));
          await putRun(run);
        }
        return { paused };
      });
    }
    case "run-hello": {
      // a content script asking: is this tab the run's tab, on the run's current job?
      const ref = msg.date === undefined && msg.id === undefined ? null : job(msg);
      const tabId = sender && sender.tab ? sender.tab.id : -1;
      return locked(async () => {
        const run = await getRun();
        if (!run || !run.active || tabId !== run.tabId) return { inRun: false };
        const c = run.current;
        const current = Boolean(c && ref && c.date === ref.date && c.id === ref.id);
        if (current && !c.hello) {
          c.hello = true;
          await putRun(run);
        }
        const j = run.jobs[run.index];
        return { inRun: true, current, host: current && j ? new URL(j.form_url).hostname : "",
          index: run.index, total: run.jobs.length };
      });
    }
    case "save": {
      const answers = (Array.isArray(msg.answers) ? msg.answers : []).slice(0, 30)
        .map((a) => ({ label: String(a.label || "").slice(0, 500), value: String(a.value || "").slice(0, 4000) }));
      return (await api("/api/scout/ext/save-answers", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ answers }),
      })).json();
    }
    case "ping":
      return (await api("/api/scout/ext/lookup?url=")).json();
    default:
      throw new Error("unknown request");
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Only this extension's own content scripts and pages may ask.
  if (sender.id !== chrome.runtime.id) return false;
  handle(msg, sender).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String((err && err.message) || err) }),
  );
  return true;
});

// He clicks the toolbar icon on any page: the panel opens there even on sites
// the content script doesn't run on by itself. activeTab grants access to that
// one tab for that one click -- no standing access to other sites.
async function openOn(tab) {
  if (!tab || tab.id === undefined) return;
  try {
    // content.js ignores a second injection; its first copy handles the message
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "scout-open" }, { frameId: 0 });
  } catch (e) {
    // chrome:// pages, the Web Store and PDFs refuse scripts; nothing to fill there
    console.warn("Scout Fill: cannot open on this page", e);
  }
}
chrome.action.onClicked.addListener(openOn);
