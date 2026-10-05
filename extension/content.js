// Scout Fill -- content script (Teodor, 2026-09-25).
//
// On a job application form from his Scout list -- or inside LinkedIn's Easy
// Apply window on any job, after he clicks Fill -- find every field, ask Scout
// (via the background worker -> admin -> the mini) for its answers, fill them,
// attach the tailored CV, and highlight whatever only he can answer.
//
// Autopilot (Teodor, 2026-10-05: "I don't want to click anything"; ON by
// default, Pause in the panel): after a fill it presses the form's own Next /
// Continue / Submit -- after a 1-3 s human pause -- ONLY when every check
// passes: each required field has a value, no answer needs him, the CV (and
// cover letter where asked) is attached, no CAPTCHA, password or sign-in wall,
// and it is this job's form. Any failed check stops it on that step: the
// fields are outlined and the job is parked on his list with the reason and
// the questions. With autopilot off, he presses Submit himself.
// Either way, "applied" is ticked only after the site shows its confirmation.
//
// Page text is untrusted: everything shown in the panel goes through
// textContent, never innerHTML.
(() => {
  if (window.__scoutFill) return;
  window.__scoutFill = true;

  const HASH_RE = /#scout-fill=(\d{4}-\d{2}-\d{2})~([A-Za-z0-9_-]{1,64})/;
  const STORE_KEY = "scoutFillJob";
  const CONFIRM_RE =
    /(application (has been |was )?(submitted|received|sent)|thank(s| you) for (applying|your application|submitting)|we(?:'|’)ve received your application|your application is (in|complete)|thank you!?\s+all done)/i; // last one: TalentLyft
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "password", "image", "file"]);
  const LINKEDIN = /(^|\.)linkedin\.com$/.test(location.hostname);

  const send = (msg) =>
    new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => resolve(res || { ok: false, error: "no response" }));
      } catch (e) {
        resolve({ ok: false, error: String(e) });
      }
    });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const norm = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  // ---------------------------------------------------------- autopilot ----
  // on: his setting; paused: the panel's Pause; inRun: this tab is "Run my
  // list"'s tab (a run always uses autopilot); current: on the run's job.
  const auto = { on: true, paused: false, inRun: false, current: false, runHost: "", run: null };
  const autoEnabled = () => auto.on || auto.inRun;
  let renderAutoUi = () => {};
  let pendingAuto = null; // the step it held back while paused
  let autoStopped = false; // parked (or the run was stopped): no more clicks on this page
  const loadAuto = () => new Promise((resolve) => {
    try {
      chrome.storage.local.get(["autopilot", "autopilotPaused", "scoutRun"], (got) => {
        const g = got || {};
        auto.on = g.autopilot !== false;
        auto.paused = Boolean(g.autopilotPaused);
        auto.run = g.scoutRun || null;
        resolve();
      });
    } catch {
      resolve();
    }
  });
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (changes.autopilot) auto.on = changes.autopilot.newValue !== false;
      if (changes.scoutRun) {
        auto.run = changes.scoutRun.newValue || null;
        if (auto.inRun && auto.run && auto.run.stopped && !autoStopped) {
          autoStopped = true;
          pendingAuto = null;
          if (panel) panel.status("Run stopped. Nothing more is pressed on this page.");
        }
      }
      if (changes.autopilotPaused) {
        auto.paused = Boolean(changes.autopilotPaused.newValue);
        if (!auto.paused && pendingAuto) {
          const go = pendingAuto;
          pendingAuto = null;
          go();
        }
      }
      renderAutoUi();
    });
  } catch {}
  async function hello(job) {
    const res = await send({ type: "run-hello", ...(job ? { date: job.date, id: job.id } : {}) });
    if (res.ok && res.data) {
      auto.inRun = Boolean(res.data.inRun);
      auto.current = Boolean(res.data.current);
      auto.runHost = String(res.data.host || "");
    }
  }
  const humanDelay = () => sleep(1000 + Math.random() * 2000);
  function runText(r) {
    if (!r) return "";
    if (r.active) {
      const n = `Run: job ${Math.min(r.index + 1, r.jobs.length)} of ${r.jobs.length}`;
      if (r.paused) return `${n} (paused)`;
      if (r.phase === "pause") return `${n}. Next job in ${Math.max(0, Math.round((r.nextAt - Date.now()) / 1000))} s`;
      return n;
    }
    if (!r.finished || !auto.inRun) return "";
    const applied = r.results.filter((x) => x.outcome === "applied").length;
    const parked = r.results.length - applied;
    const left = r.jobs.length - r.results.length;
    return `Run ${r.stopped ? "stopped" : "finished"}: applied ${applied}, parked ${parked}${left ? `, ${left} not started` : ""}.`;
  }
  function runDetails(r) {
    if (!r || r.active || !r.finished || !auto.inRun) return [];
    return r.results.filter((x) => x.outcome === "parked").map((x) =>
      `Parked ${x.company || x.id}${x.title ? ` - ${x.title}` : ""}: ${x.reason}${x.questions.length ? ` (${x.questions.join("; ")})` : ""}`);
  }

  // ---------------------------------------------------------------- job ----
  async function resolveJob() {
    const m = location.href.match(HASH_RE);
    if (m) {
      const job = { date: m[1], id: m[2] };
      try { sessionStorage.setItem(STORE_KEY, JSON.stringify(job)); } catch {}
      return { ...job, auto: true };
    }
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
      if (saved && saved.date && saved.id) return { ...saved, auto: false };
    } catch {}
    // A LinkedIn job applied to on the company's own site has a URL Scout never
    // saw: the page's title and heading let admin recognise it anyway.
    const h1 = document.querySelector("h1");
    const title = `${document.title} | ${h1 ? clean(h1.innerText) : ""}`.slice(0, 500);
    const res = await send({ type: "lookup", url: location.href, title });
    if (res.ok && res.data && res.data.job) return { ...res.data.job, auto: false };
    return null;
  }

  // -------------------------------------------------------------- panel ----
  let panel = null;
  let markHandler = null; // set once the submission watcher runs
  // The toolbar owl, drawn inline (static markup, no page text in it).
  const OWL_SVG = '<svg viewBox="0 0 128 128" xmlns="http://www.w3.org/2000/svg"><rect width="128" height="128" rx="28" fill="#0f6b5c"/>'
    + '<path d="M30 40 L36 16 L52 34 Z M98 40 L92 16 L76 34 Z" fill="#d9a15c"/>'
    + '<path d="M64 26 C96 26 106 50 106 74 C106 100 88 114 64 114 C40 114 22 100 22 74 C22 50 32 26 64 26 Z" fill="#e9b872"/>'
    + '<ellipse cx="64" cy="94" rx="24" ry="17" fill="#f6e3bf"/><circle cx="45" cy="60" r="17" fill="#fff"/>'
    + '<circle cx="83" cy="60" r="17" fill="#fff"/><circle cx="47" cy="61" r="9" fill="#18211f"/>'
    + '<circle cx="81" cy="61" r="9" fill="#18211f"/><path d="M58 74 L70 74 L64 84 Z" fill="#f28c28"/></svg>';
  function owlIcon() {
    const svg = new DOMParser().parseFromString(OWL_SVG, "image/svg+xml").documentElement;
    svg.setAttribute("class", "owl");
    svg.setAttribute("aria-hidden", "true");
    return document.importNode(svg, true);
  }

  function makePanel(job, { line: lineText, button: buttonText } = {}) {
    const host = document.createElement("div");
    host.id = "scout-fill-panel";
    host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      .box{font:13px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif;background:#fff;color:#18211f;
        border:1px solid #0f6b5c;border-radius:10px;padding:12px 14px;width:300px;box-shadow:0 6px 24px rgba(0,0,0,.18)}
      .t{font-weight:600;margin-bottom:4px}
      button{font:inherit;font-weight:600;margin-top:8px;padding:6px 12px;border-radius:6px;border:1px solid #0f6b5c;
        background:#0f6b5c;color:#fff;cursor:pointer}
      button[disabled]{opacity:.5;cursor:default}
      ul{margin:6px 0 0;padding-left:18px;max-height:140px;overflow:auto}
      .muted{color:#5b6865}
      .auto{margin:2px 0 4px;font-size:12px}
      button.small{margin:0 4px 0 0;padding:1px 8px;font-size:12px;font-weight:600;background:#fff;color:#0f6b5c}
      .head{display:flex;align-items:center;gap:6px;margin-bottom:4px;cursor:move;user-select:none;touch-action:none}
      .head .t{flex:1;margin:0}
      .owl{width:20px;height:20px;flex:none;display:block}
      button.min{margin:0;padding:0 8px;line-height:20px;background:#fff;color:#0f6b5c;font-size:16px;cursor:pointer}
      .pill{font:600 13px -apple-system,"Segoe UI",Roboto,sans-serif;display:flex;align-items:center;gap:6px;margin:0;padding:6px 12px 6px 8px;border-radius:999px;
        box-shadow:0 6px 24px rgba(0,0,0,.18);cursor:move;touch-action:none}
      [hidden]{display:none!important}`;
    const box = document.createElement("div");
    box.className = "box";
    const head = document.createElement("div");
    head.className = "head";
    head.title = "Drag to move";
    const title = document.createElement("div");
    title.className = "t";
    title.textContent = "Scout";
    const minimize = document.createElement("button");
    minimize.type = "button";
    minimize.className = "min";
    minimize.textContent = "\u2013";
    minimize.title = "Minimize";
    minimize.setAttribute("aria-label", "Minimize Scout");
    head.append(owlIcon(), title, minimize);
    // minimized: a small draggable pill; clicking it opens the panel again
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.title = "Open Scout (drag to move)";
    pill.append(owlIcon(), "Scout");
    pill.hidden = true;
    const line = document.createElement("div");
    line.className = "muted";
    line.textContent = lineText || (job && job.company ? `${job.company} - ${job.title || ""}` : "Job from your Scout list");
    const status = document.createElement("div");
    status.setAttribute("role", "status");
    const list = document.createElement("ul");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = buttonText || "Fill this form";
    const save = document.createElement("button");
    save.type = "button";
    save.hidden = true;
    save.style.marginLeft = "6px";
    const report = document.createElement("button");
    report.type = "button";
    report.textContent = "Send this form's layout to Scout";
    report.style.cssText = "display:block;background:#fff;color:#0f6b5c;font-weight:500";
    report.hidden = true;
    const mark = document.createElement("button");
    mark.type = "button";
    mark.textContent = "It went through: mark as applied";
    mark.style.cssText = "display:block";
    mark.hidden = true;
    mark.addEventListener("click", () => { if (markHandler) markHandler(); });
    // autopilot state + Pause, and "Run my list" (static labels only)
    const autoRow = document.createElement("div");
    autoRow.className = "auto";
    const autoText = document.createElement("span");
    const pauseBtn = document.createElement("button");
    pauseBtn.type = "button";
    pauseBtn.className = "small";
    const runBtn = document.createElement("button");
    runBtn.type = "button";
    runBtn.className = "small";
    runBtn.textContent = "Run my list";
    const stopBtn = document.createElement("button");
    stopBtn.type = "button";
    stopBtn.className = "small";
    stopBtn.textContent = "Stop run";
    const runLine = document.createElement("div");
    runLine.className = "muted";
    const runList = document.createElement("ul");
    autoRow.append(autoText, pauseBtn, runBtn, stopBtn);
    pauseBtn.addEventListener("click", () => send({ type: "run-pause", paused: !auto.paused }));
    stopBtn.addEventListener("click", () => send({ type: "run-stop" }));
    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true;
      const res = await send({ type: "run-start" });
      runBtn.disabled = false;
      if (!res.ok) status.textContent = `Could not start the run: ${res.error}`;
      else if (!res.data || !res.data.jobs || !res.data.jobs.length) status.textContent = "Your Scout list has nothing to apply to right now.";
    });
    renderAutoUi = () => {
      const on = autoEnabled();
      const r = auto.run;
      autoText.textContent = !on ? "Autopilot off: you press Submit. " : auto.paused ? "Autopilot paused. " : "Autopilot on. ";
      host.dataset.autopilot = !on ? "off" : auto.paused ? "paused" : "on";
      pauseBtn.hidden = !on;
      pauseBtn.textContent = auto.paused ? "Resume" : "Pause";
      runBtn.hidden = Boolean(r && r.active);
      stopBtn.hidden = !(r && r.active);
      runLine.textContent = runText(r);
      runList.replaceChildren(...runDetails(r).map((t) => {
        const li = document.createElement("li");
        li.textContent = t;
        return li;
      }));
    };
    setInterval(() => { if (auto.run && auto.run.active && host.isConnected) renderAutoUi(); }, 1000);
    box.append(head, line, autoRow, runLine, runList, status, list, btn, save, mark, report);
    root.append(style, box, pill);
    document.documentElement.append(host);

    // ---- minimize and move (he may need what is under the panel) ----
    const place = { left: null, top: null, min: false };
    const apply = () => {
      box.hidden = place.min;
      pill.hidden = !place.min;
      if (place.left === null) return;
      const r = host.getBoundingClientRect();
      const left = Math.min(Math.max(0, place.left), Math.max(0, innerWidth - r.width));
      const top = Math.min(Math.max(0, place.top), Math.max(0, innerHeight - r.height));
      host.style.left = `${left}px`;
      host.style.top = `${top}px`;
      host.style.right = "auto";
      host.style.bottom = "auto";
    };
    const remember = () => {
      try { chrome.storage.local.set({ scoutPanel: { ...place } }); } catch {}
    };
    try {
      chrome.storage.local.get("scoutPanel", (got) => {
        const saved = got && got.scoutPanel;
        if (saved) { Object.assign(place, saved); apply(); }
      });
    } catch {}
    const setMin = (min) => { place.min = min; apply(); remember(); };
    minimize.addEventListener("click", () => setMin(true));
    let moved = false;
    const drag = (handle) => handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || (handle === head && e.target === minimize)) return;
      const r = host.getBoundingClientRect();
      const dx = e.clientX - r.left;
      const dy = e.clientY - r.top;
      const x0 = e.clientX;
      const y0 = e.clientY;
      moved = false;
      handle.setPointerCapture(e.pointerId);
      const move = (ev) => {
        if (!moved && Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) < 4) return;
        moved = true;
        place.left = ev.clientX - dx;
        place.top = ev.clientY - dy;
        apply();
      };
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        // the click that ends a drag must not also open the pill
        if (moved) { remember(); setTimeout(() => { moved = false; }, 0); }
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
    });
    drag(head);
    drag(pill);
    pill.addEventListener("click", () => { if (!moved) setMin(false); });
    addEventListener("resize", apply);
    renderAutoUi();
    return {
      host,
      btn,
      save,
      report,
      mark,
      line: (text) => { line.textContent = text; },
      status: (text) => { status.textContent = text; },
      needs: (labels) => {
        list.replaceChildren(...labels.map((l) => {
          const li = document.createElement("li");
          li.textContent = l;
          return li;
        }));
      },
    };
  }

  // --------------------------------------------------------------- scan ----
  function visible(el) {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }

  function containerOf(el) {
    return el.closest(
      ".ashby-application-form-field-entry, .application-question, .application-field, fieldset, .field, [class*='question'], li, .form-group",
    );
  }

  function labelOf(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((n) => n.innerText).join(" ");
      if (clean(t)) return clean(t);
    }
    const box = containerOf(el);
    if (box) {
      const l = box.querySelector("label, legend, .application-label, [class*='label'], [class*='question-title']");
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    // join.com asks one question per step as the page heading, with an
    // unlabelled box under it: the heading IS the question. A question-shaped
    // heading wins; otherwise a real placeholder ("First name") beats a
    // section heading, and a generic one ("Your answer") does not.
    const heading = headingBefore(el);
    if (heading && /\?\s*$/.test(heading)) return heading;
    const ph = clean(el.getAttribute("placeholder") || "");
    if (ph && !GENERIC_PLACEHOLDER_RE.test(ph)) return ph;
    return heading || ph || clean(el.getAttribute("name") || "");
  }
  const GENERIC_PLACEHOLDER_RE =
    /^(your |type (your |an )?|enter (your |an )?|write (your |an )?)?(answer|response|text|reply|here)\b.{0,20}$/i;
  function headingBefore(el) {
    let best = null;
    for (const h of document.querySelectorAll("h1, h2, h3, h4, legend")) {
      if (!visible(h) || h.closest("#scout-fill-panel")) continue;
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = h;
    }
    return best ? clean(best.innerText).slice(0, 300) : "";
  }

  function isRequired(el, label) {
    if (el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label || "")) return true;
    // TalentLyft marks required fields only through its FormValidation attributes.
    if (el.getAttribute("data-fv-not-empty") === "true" || Number(el.getAttribute("data-fv-choice___min")) >= 1) return true;
    // Ashby draws its "*" with CSS on a _required_ label class, so innerText lacks it.
    const box = containerOf(el);
    return Boolean(box && box.querySelector("label[class*='required'], [class*='question-title'][class*='required']"));
  }

  function optionText(input) {
    if (input.labels && input.labels.length) return clean(input.labels[0].innerText);
    return clean(input.getAttribute("aria-label") || input.value);
  }

  // Each field: {kind, label, required, options, set(value) -> Promise<bool>, el}
  function scan(root = document) {
    const fields = [];
    const seenGroups = new Set();
    const controls = root.querySelectorAll("input, textarea, select");
    for (const el of controls) {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (el.tagName === "INPUT" && SKIP_TYPES.has(type)) continue;
      if (/recaptcha|captcha|honeypot/i.test(el.name || el.id || "")) continue;
      if (type === "radio" || type === "checkbox") {
        const key = `${type}:${el.name || el.id}`;
        if (seenGroups.has(key)) continue;
        seenGroups.add(key);
        const group = el.name
          ? [...root.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)]
          : [el];
        const box = containerOf(el);
        // Ashby's Yes/No control: two buttons over a hidden, unnamed checkbox
        // that tracks "Yes". The button pass below owns it -- read as a lone
        // checkbox it looked like consent and was never answered.
        const yesNoButtons = box && [...box.querySelectorAll("button")].filter((b) => /^(yes|no)$/i.test(clean(b.innerText)));
        if (type === "checkbox" && !visible(el) && yesNoButtons && yesNoButtons.length === 2) continue;
        if (type === "checkbox" && group.length === 1) {
          const lbl = labelOf(el) || clean(box && box.innerText);
          fields.push({ kind: "consent", label: lbl, required: isRequired(el, lbl), options: [lbl], el,
            set: async (v) => setConsent(el, v) });
          continue;
        }
        const qLabel = (box && clean((box.querySelector("legend, label, [class*='label']") || {}).innerText)) || labelOf(el);
        fields.push({ kind: type, label: qLabel, required: group.some((g) => isRequired(g, qLabel)),
          options: group.map(optionText), el,
          set: async (v) => setChoice(group, v, type) });
        continue;
      }
      // A native <select> may be visually replaced by a custom widget, so its
      // surroundings decide -- but never inside a hidden panel/tab.
      if (!visible(el) && !(el.tagName === "SELECT" && visible(el.parentElement))) continue;
      const label = labelOf(el);
      if (!label) continue;
      if (el.tagName === "SELECT") {
        const options = [...el.options].map((o) => clean(o.text)).filter((t) => t && !/^select|^choose|^--/i.test(t));
        fields.push({ kind: "select", label, required: isRequired(el, label), options, el,
          set: async (v) => setSelect(el, v) });
      } else if (el.getAttribute("role") === "combobox") {
        fields.push({ kind: "text", label, required: isRequired(el, label), options: [], el,
          set: async (v) => setCombobox(el, v) });
      } else {
        const kind = el.tagName === "TEXTAREA" ? "textarea" : type === "number" ? "number" : "text";
        fields.push({ kind, label, required: isRequired(el, label), options: [], el,
          set: async (v) => setText(el, v) });
      }
    }
    // Ashby-style Yes/No toggle buttons with no native radio underneath.
    for (const box of root.querySelectorAll(".ashby-application-form-field-entry, fieldset, [class*='question']")) {
      const buttons = [...box.querySelectorAll("button")].filter((b) => /^(yes|no)$/i.test(clean(b.innerText)));
      if (buttons.length !== 2 || box.querySelector("input[type=radio]")) continue;
      const lbl = clean((box.querySelector("label, legend, [class*='label']") || {}).innerText);
      if (!lbl || fields.some((f) => f.label === lbl)) continue;
      fields.push({ kind: "radio", label: lbl, required: isRequired(buttons[0], lbl), options: ["Yes", "No"], el: buttons[0],
        set: async (v) => {
          const b = buttons.find((x) => norm(x.innerText) === norm(v));
          if (!b) return false;
          b.click();
          return true;
        } });
    }
    return fields;
  }

  // What he has put in a field (for saving his own answers).
  function currentValue(f) {
    const el = f.el;
    if (f.kind === "select") return el.selectedIndex > 0 ? clean(el.options[el.selectedIndex].text) : "";
    if (f.kind === "radio" || f.kind === "checkbox") {
      if (el.tagName === "BUTTON") {
        const on = [...(containerOf(el) || document).querySelectorAll("button")]
          .find((b) => b.getAttribute("aria-pressed") === "true");
        return on ? clean(on.innerText) : "";
      }
      const group = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
      return group.filter((g) => g.checked).map(optionText).join(", ");
    }
    if (f.kind === "consent") return "";
    return clean(el.value);
  }

  // ------------------------------------------------------------ setters ----
  function nativeSet(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function setText(el, value) {
    let v = String(value);
    if ((el.getAttribute("type") || "") === "number") {
      const m = v.replace(/[, ]/g, "").match(/-?\d+(\.\d+)?/);
      if (!m) return false;
      v = m[0];
    }
    if ((el.getAttribute("type") || "") === "url" && !/^https?:\/\//i.test(v)) v = `https://${v}`;
    el.focus();
    nativeSet(el, v);
    el.blur();
    return el.value === v;
  }

  function pick(options, value) {
    const want = norm(value);
    return options.find((o) => norm(o) === want)
      || options.find((o) => norm(o).startsWith(want) || want.startsWith(norm(o)))
      || options.find((o) => norm(o).includes(want));
  }

  async function setSelect(el, value) {
    const opts = [...el.options];
    const text = pick(opts.map((o) => clean(o.text)), value);
    const opt = text && opts.find((o) => clean(o.text) === text);
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el.value === opt.value;
  }

  async function setChoice(group, value, type) {
    const wanted = type === "checkbox" ? String(value).split(/\s*[,;]\s*/) : [String(value)];
    let ok = false;
    for (const w of wanted) {
      const text = pick(group.map(optionText), w);
      const input = text && group.find((g) => optionText(g) === text);
      if (!input) continue;
      if (!input.checked) (input.labels && input.labels[0] ? input.labels[0] : input).click();
      if (!input.checked) input.click();
      ok = input.checked || ok;
    }
    return ok;
  }

  async function setConsent(el, value) {
    if (String(value).toLowerCase() !== "yes") return false;
    if (!el.checked) (el.labels && el.labels[0] ? el.labels[0] : el).click();
    if (!el.checked) el.click();
    return el.checked;
  }

  async function setCombobox(el, value) {
    el.focus();
    nativeSet(el, String(value));
    for (let i = 0; i < 12; i++) {
      await sleep(250);
      const options = [...document.querySelectorAll("[role=option]")].filter(visible);
      const text = pick(options.map((o) => clean(o.innerText)), value);
      const hit = text && options.find((o) => clean(o.innerText) === text);
      if (hit) {
        hit.click();
        return true;
      }
    }
    return false; // typed, but nothing to pick: he confirms it
  }

  // The upload box itself, for pages that only render a file input on click:
  // the smallest element that says "drag & drop" / "drop a file".
  function dropZoneOf(root) {
    const hits = [...root.querySelectorAll("div, label, section, button, [role=button]")]
      .filter((n) => visible(n) && /drag\s*(&|and)\s*drop|drop (a |your )?(file|cv|resume)/i.test(n.innerText || ""));
    return hits.find((n) => !hits.some((m) => m !== n && n.contains(m))) || null;
  }

  // Which document an upload wants: its own label/name first ("cover_letter",
  // "Resume"), then the step it sits on (join.com: "Upload your cover letter",
  // .../apply/coverLetter). null = the input doesn't say.
  const COVER_RE = /cover/i;
  const CV_RE = /resume|\bcv\b|curriculum/i;
  function fileKind(input) {
    const own = `${input.id} ${input.name} ${labelOf(input)}`;
    if (COVER_RE.test(own)) return "cover";
    if (CV_RE.test(own)) return "cv";
    return null;
  }
  function stepKind(root) {
    const heading = [...root.querySelectorAll("h1, h2")].filter(visible).map((h) => h.innerText).join(" ");
    return COVER_RE.test(`${heading} ${location.pathname}`) ? "cover" : "cv";
  }
  const FILE_NAMES = { cv: "Teodor-Lutoiu-CV.pdf", cover: "Teodor-Lutoiu-Cover-Letter.pdf" };

  async function attachCv(job, root = document, kind = "cv") {
    if (!job) return null; // a job not on his list has no tailored CV
    const inputs = [...root.querySelectorAll("input[type=file]")];
    // An input that names its document wins. Unnamed inputs are used when the
    // step itself is about this document (join.com's "Upload your cover
    // letter" has two unnamed Chakra uploaders, one hidden), or for the CV on
    // an ordinary form with no input named "resume".
    let candidates = inputs.filter((i) => fileKind(i) === kind);
    if (!candidates.length) {
      const unnamed = inputs.filter((i) => !fileKind(i));
      const cvFallback = kind === "cv" && !inputs.some((i) => fileKind(i) === "cv");
      if (stepKind(root) === kind || cvFallback) candidates = unnamed;
    }
    // prefer the uploader he can see (the other variant sits in a hidden wrapper)
    const shown = (i) => visible(i) || visible(i.parentElement);
    const target = candidates.find(shown) || candidates[0];
    const dropBox = target || inputs.length ? null : dropZoneOf(root);
    if (!target && !dropBox) return null;
    const res = await send({ type: "cv", date: job.date, id: job.id, kind });
    if (!res.ok) return false;
    const bytes = Uint8Array.from(atob(res.data.base64), (c) => c.charCodeAt(0));
    const file = new File([bytes], FILE_NAMES[kind], { type: "application/pdf" });
    if (!target) {
      // join.com-style upload box with no file input in the page: drop onto it
      const before = document.body.innerText;
      for (const type of ["dragenter", "dragover", "drop"]) {
        const dtx = new DataTransfer();
        dtx.items.add(file);
        dropBox.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dtx }));
      }
      await sleep(1500);
      return document.body.innerText !== before;
    }
    const dt = new DataTransfer();
    dt.items.add(file);
    target.files = dt.files;
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    // TalentLyft hides the real input behind a Dropzone that only reads drops.
    const zone = target.offsetParent === null && target.parentNode && target.parentNode.querySelector(".dropzone");
    if (zone) {
      const drop = new DataTransfer();
      drop.items.add(file);
      zone.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: drop }));
      await new Promise((r) => setTimeout(r, 300));
      return zone.querySelector(".dz-preview") !== null || document.querySelector(".dz-preview") !== null;
    }
    return target.files.length === 1;
  }

  function flag(el) {
    const box = containerOf(el) || el;
    box.style.outline = "3px solid #e0891b";
    box.style.outlineOffset = "4px";
  }

  // --------------------------------------------------------------- fill ----
  let filled = false;
  async function fill(job, root = document) {
    panel.btn.disabled = true;
    panel.status("Reading the form...");
    let fields = scan(root);
    if (!fields.length && root === document) {
      // Ashby lands on the job overview; the form is behind an "Application"
      // tab. Opening a tab only switches the view -- it never submits.
      const tab = [...document.querySelectorAll("[role=tab], a, button")]
        .find((n) => /^application$/i.test(clean(n.innerText)) && visible(n));
      if (tab) {
        tab.click();
        await sleep(1200); // Ashby re-renders the resume input after the tab opens
        fields = scan();
      }
    }
    if (!fields.length && root === document && job && autoEnabled() && !autoStopped
        && !root.querySelector("input[type=file]") && !dropZoneOf(root)) {
      await sleep(3000); // a single-page form still rendering
      fields = scan();
    }
    if (!fields.length && job && (root.querySelector("input[type=file]") || dropZoneOf(root))) {
      // an upload-only step (join.com "Upload your CV")
      const input = root.querySelector("input[type=file]");
      const kind = (input && fileKind(input)) || stepKind(root);
      const what = kind === "cover" ? "cover letter" : "CV";
      panel.status(`Uploading your tailored ${what}...`);
      const cv = await attachCv(job, root, kind);
      panel.status(cv ? `Tailored ${what} uploaded. Check it shows, then continue.`
        : `Couldn't upload the ${what} here. Press "Send this form's layout to Scout" so it can be fixed.`);
      panel.report.hidden = Boolean(cv);
      panel.btn.textContent = "Upload again";
      panel.btn.disabled = false;
      filled = true;
      panel.host.dataset.state = "filled";
      await autoStep(root, job, { questions: [], uploads: cv ? [] : [`${what} upload`] });
      return;
    }
    if (!fields.length) {
      panel.status(LINKEDIN ? "Nothing to fill on this step. Press Next." : "No form found on this page yet. Open the application form, then press Fill.");
      panel.btn.disabled = false;
      panel.report.hidden = false;
      if (job && root === document && autoEnabled()) await autoPark(job, { reason: "no-form: Scout found no application form on this page" });
      return;
    }
    panel.status(`Asking Scout about ${fields.length} fields...`);
    const res = await send({
      type: "answer", ...(job ? { date: job.date, id: job.id } : {}),
      questions: fields.map((f) => ({ label: f.label.slice(0, 500), kind: f.kind, required: f.required, options: f.options.slice(0, 100) })),
    });
    if (!res.ok) {
      panel.status(`Scout could not answer: ${res.error}`);
      panel.btn.disabled = false;
      return;
    }
    const { answers = [], cover_letter: letter = "" } = res.data || {};
    const needs = [];
    const doubts = []; // answered, but Scout said only he can be sure
    let done = 0;
    for (let i = 0; i < fields.length; i++) {
      const f = fields[i];
      let value = answers[i] && answers[i].value;
      if (!value && letter && f.kind === "textarea" && /cover letter|why .*(join|us|role|interested)/i.test(f.label)) value = letter;
      let ok = false;
      if (value) {
        try { ok = await f.set(value); } catch { ok = false; }
      }
      if (ok) {
        done += 1;
        if (answers[i] && answers[i].needs_you) {
          doubts.push(fieldLabel(f));
          flag(f.el);
        }
      } else if (f.required || (answers[i] && answers[i].needs_you)) {
        needs.push(fieldLabel(f));
        flag(f.el);
      }
    }
    pending = pending.concat(fields.filter((f) => needs.includes(fieldLabel(f))));
    const questions = [...needs, ...doubts.filter((d) => !needs.includes(d))];
    const uploads = [];
    const cv = await attachCv(job, root, "cv");
    if (cv === false) {
      needs.push("CV upload (download it from your Scout list)");
      uploads.push("CV upload");
    }
    if (job && [...root.querySelectorAll("input[type=file]")].some((i) => fileKind(i) === "cover")) {
      if (await attachCv(job, root, "cover") === false) {
        needs.push("Cover letter upload");
        uploads.push("Cover letter upload");
      }
    }
    if (cv === null && !job && root.querySelector("input[type=file]")) needs.push("CV upload (not on your Scout list, so attach your own)");
    panel.needs(needs);
    const next = LINKEDIN ? "Next (or Submit on the last step)" : "Submit";
    panel.status(needs.length
      ? `Filled ${done} of ${fields.length}${cv ? " + CV" : ""}. ${needs.length} need you (outlined). Then press ${next}.`
      : `Filled ${done} of ${fields.length}${cv ? " + CV" : ""}. Review, then press ${next}.`);
    panel.report.hidden = !(needs.length || done < fields.length);
    panel.btn.textContent = LINKEDIN ? "Fill this step again" : "Fill again";
    panel.btn.disabled = false;
    filled = true;
    panel.host.dataset.state = "filled"; // observable finish line (tests, and future tooling)
    await autoStep(root, job, { questions, uploads });
  }
  const fieldLabel = (f) => f.label.replace(/\s*\*\s*$/, "");

  // ------------------------------------------- autopilot: check, press ----
  // The checks are re-run on the live page right before every click, never
  // trusted from the fill: a site can add a CAPTCHA or an error at any time.
  const SUBMIT_RE = /^(submit|send|apply)\b|\b(submit|send) (my |your |the )?application\b/i;
  const NEXT_RE = /^(next|continue|review|proceed|save (and|&) continue|go to next step)\b/i;
  const LOGIN_RE = /\b(sign ?in|log ?in|sign ?up|create (an |your )?account|register)\b/i;
  const OTHER_RE = /\bwith (linkedin|indeed|google|seek|xing|facebook|github)\b|easy apply|^(yes|no)$/i;
  const CAPTCHA_SEL = ["iframe[src*='bframe']", "iframe[src*='recaptcha']", ".g-recaptcha", "iframe[src*='hcaptcha']",
    ".h-captcha", "iframe[src*='turnstile']", "iframe[src*='challenges.cloudflare.com']", ".cf-turnstile"].join(", ");

  function captchaShown() {
    return [...document.querySelectorAll(CAPTCHA_SEL)].some((el) => {
      // the invisible reCAPTCHA badge resolves on submit by itself
      if (el.closest(".grecaptcha-badge, .g-recaptcha[data-size=invisible]")) return false;
      if (!visible(el)) return false;
      const r = el.getBoundingClientRect();
      return r.top > -3000 && r.left > -3000; // parked off-screen = not shown to anyone
    });
  }
  const passwordShown = () => [...document.querySelectorAll("input[type=password]")].some(visible);
  const controlText = (b) => clean(b.innerText || b.value || b.getAttribute("aria-label") || "");
  function controls(root) {
    return [...root.querySelectorAll("button, input[type=submit], input[type=button], [role=button]")]
      .filter((b) => visible(b) && !b.disabled && b.getAttribute("aria-disabled") !== "true" && !b.closest("#scout-fill-panel"));
  }
  // The form's own Submit (or Next/Continue on a step). Buttons inside the
  // filled form win over page chrome ("Apply for this job" in a header).
  function stepControl(root) {
    const all = controls(root);
    const forms = new Set(scan(root).map((f) => f.el.closest("form")).filter(Boolean));
    const inForm = all.filter((b) => (b.form && forms.has(b.form)) || [...forms].some((f) => f.contains(b)));
    for (const list of [inForm, all]) {
      const usable = list.filter((b) => !OTHER_RE.test(controlText(b)));
      const sub = usable.find((b) => SUBMIT_RE.test(controlText(b)));
      if (sub) return { el: sub, kind: "submit", login: LOGIN_RE.test(controlText(sub)) };
      const next = usable.find((b) => NEXT_RE.test(controlText(b)));
      if (next) return { el: next, kind: "next", login: false };
      const typed = usable.find((b) => b.type === "submit" && b.form && forms.has(b.form));
      if (typed) return { el: typed, kind: "submit", login: LOGIN_RE.test(controlText(typed)) };
    }
    // no way forward, but a sign-in/sign-up button in the form: an account wall
    if (inForm.some((b) => LOGIN_RE.test(controlText(b)))) return { el: null, kind: "none", login: true };
    return null;
  }
  function hasValue(f) {
    if (f.kind === "consent") return f.el.checked;
    if (f.el.tagName === "BUTTON") {
      const box = containerOf(f.el) || document;
      return [...box.querySelectorAll("button")].some((b) => b.getAttribute("aria-pressed") === "true"
        || /\b(selected|active)\b/.test(b.className)) || [...box.querySelectorAll("input[type=checkbox]")].some((c) => c.checked);
    }
    return currentValue(f) !== "";
  }
  const siteOf = (host) => host.split(".").slice(-2).join(".");
  function stepKey(root) {
    return `${location.href}|${scan(root).map((f) => f.label).join("|")}|${controls(root).map(controlText).join("|")}`;
  }

  function checkStep(root, report) {
    if (captchaShown()) return { reason: "captcha: the site shows a CAPTCHA only you can solve" };
    if (passwordShown()) return { reason: "login-required: the site asks for an account password" };
    const ctl = stepControl(root);
    if (ctl && ctl.login) return { reason: "login-required: the form wants you to sign in or create an account" };
    if (auto.inRun && auto.current && auto.runHost && siteOf(location.hostname) !== siteOf(auto.runHost)) {
      return { reason: `wrong-page: the job link led to ${location.hostname}, not its application form` };
    }
    const questions = [...report.questions];
    for (const f of scan(root)) {
      if (!f.required || hasValue(f)) continue;
      const l = fieldLabel(f) || "An unlabelled required field";
      if (!questions.includes(l)) questions.push(l);
      flag(f.el);
    }
    if (questions.length) {
      return { reason: `needs-answers: ${questions.length} question${questions.length === 1 ? "" : "s"} only you can answer`, questions };
    }
    const uploads = [...report.uploads];
    for (const i of root.querySelectorAll("input[type=file][required]")) {
      if (!i.files || !i.files.length) uploads.push(fileKind(i) === "cover" ? "Cover letter upload" : "CV upload");
    }
    if (uploads.length) return { reason: `upload-failed: ${[...new Set(uploads)].join(", ")}` };
    if (!ctl) return { reason: "no-submit: Scout found no Submit, Next or Continue button on the form" };
    return null;
  }

  let autoClicks = 0;
  async function autoStep(root, job, report) {
    if (!job || !autoEnabled() || autoStopped || submitted) return;
    if (auto.paused) {
      pendingAuto = () => autoStep(root, job, report);
      panel.status("Filled. Autopilot is paused: press Resume, or Submit yourself.");
      return;
    }
    pendingAuto = null;
    let problem = checkStep(root, report);
    if (problem) return autoPark(job, problem);
    let ctl = stepControl(root);
    panel.status(ctl.kind === "submit" ? "Every check passed. Submitting in a moment..." : "Every check passed. Going to the next step...");
    await humanDelay();
    if (autoStopped || submitted) return;
    if (auto.paused) {
      pendingAuto = () => autoStep(root, job, report);
      panel.status("Filled. Autopilot is paused: press Resume, or Submit yourself.");
      return;
    }
    problem = checkStep(root, report); // the page may have changed during the pause
    if (problem) return autoPark(job, problem);
    ctl = stepControl(root);
    if ((autoClicks += 1) > 25) return autoPark(job, { reason: "stuck: more than 25 steps on one application" });
    const before = stepKey(root);
    panel.host.dataset.auto = ctl.kind;
    ctl.el.click();
    if (ctl.kind === "submit") {
      panel.status("Submitted by autopilot. Waiting for the site to confirm...");
      setTimeout(() => {
        if (!submitted && !autoStopped) {
          autoPark(job, { reason: "no-confirmation: autopilot pressed Submit but the site showed no confirmation; check the page" });
        }
      }, 30000);
    } else {
      setTimeout(() => {
        if (!submitted && !autoStopped && !pendingAuto && stepKey(root) === before) {
          autoPark(job, { reason: "stuck: the form did not move on after Next (it may show an error)" });
        }
      }, 20000);
    }
  }

  // Stop on this step, outline what needs him, and park the job on his list.
  async function autoPark(job, problem) {
    if (autoStopped || submitted) return;
    autoStopped = true;
    pendingAuto = null;
    if (!panel) panel = makePanel(job);
    const questions = (problem.questions || []).map((q) => String(q).slice(0, 500)).slice(0, 30);
    const why = problem.reason.replace(/^[a-z-]+: /, "");
    if (questions.length) panel.needs(questions);
    panel.status(`Autopilot stopped: ${why}. ${auto.inRun ? "Parked; the run moves on." : "Parked on your Scout list; finish it here if you like."}`);
    panel.host.dataset.state = "parked";
    const res = await send({ type: "park", date: job.date, id: job.id, reason: problem.reason, questions });
    if (!res.ok) panel.status(`Autopilot stopped: ${why}. Could not park it on your list: ${res.error}`);
  }

  // ---------------------------------------- saving what HE typed ----
  // Outlined questions are the ones Scout could not answer. Once he fills
  // them, offer to save his answers so no form ever asks him twice. On a
  // confirmed submission they are saved anyway: that is what he sent.
  let pending = [];
  // Remembered as he types: a multi-step form (LinkedIn) deletes a step's
  // fields when he presses Next, long before the submission it belongs to.
  const lastTyped = new Map();
  function rememberTyped() {
    for (const f of pending) {
      if (f.kind === "consent" || !f.el.isConnected) continue;
      const value = currentValue(f).slice(0, 4000);
      if (value) lastTyped.set(f.label.slice(0, 500), value);
      else lastTyped.delete(f.label.slice(0, 500));
    }
  }
  function typedAnswers() {
    rememberTyped();
    return [...lastTyped].map(([label, value]) => ({ label, value }));
  }
  function refreshSaveButton() {
    if (!panel) return;
    const n = typedAnswers().length;
    panel.save.hidden = n === 0;
    panel.save.textContent = `Save ${n} answer${n === 1 ? "" : "s"} for next time`;
  }
  async function saveTyped(quiet) {
    const answers = typedAnswers();
    if (!answers.length) return;
    const res = await send({ type: "save", answers });
    if (!quiet && panel) panel.status(res.ok ? `Saved ${res.data.saved}. Scout will fill ${res.data.saved === 1 ? "it" : "them"} next time.` : `Could not save: ${res.error}`);
    if (res.ok) {
      pending = pending.filter((f) => f.el.isConnected && !currentValue(f));
      lastTyped.clear();
      refreshSaveButton();
    }
  }
  const onEdit = () => { if (pending.length) { rememberTyped(); refreshSaveButton(); } };
  document.addEventListener("input", onEdit, true);
  document.addEventListener("change", () => setTimeout(onEdit, 50), true);
  // capture phase: runs BEFORE a Next click swaps the step away
  document.addEventListener("click", () => { if (pending.length) rememberTyped(); setTimeout(onEdit, 50); }, true);

  // ------------------------------------------ layout report (his click) ----
  // For a form Scout could not fill -- above all LinkedIn, which Claude cannot
  // log into -- he sends its STRUCTURE so the filler can be fixed. Every value
  // he or the site typed is stripped; scripts, styles and images are dropped.
  function layoutOf(root) {
    const src = root === document ? document.body : root;
    const copy = src.cloneNode(true);
    copy.querySelectorAll("script, style, noscript, svg, img, video, canvas, iframe, #scout-fill-panel").forEach((n) => n.remove());
    copy.querySelectorAll("input, textarea").forEach((n) => { n.removeAttribute("value"); n.textContent = ""; });
    copy.querySelectorAll("option[selected]").forEach((n) => n.removeAttribute("selected"));
    return copy.outerHTML.slice(0, 2 * 1024 * 1024);
  }
  async function sendLayout(root) {
    panel.report.disabled = true;
    const res = await send({ type: "layout", url: location.href, html: layoutOf(root) });
    panel.status(res.ok ? "Layout sent. Tell Claude it's there." : `Could not send: ${res.error}`);
    panel.report.disabled = false;
  }

  // ------------------------------------------------- after HE submits ----
  // He pressed Submit: watch for the site's confirmation, then tick the job on
  // his list. The "pressed Submit on job X" mark is kept in the tab for a few
  // minutes, so a site that loads a separate thank-you page still records it.
  // If no confirmation shows up, the panel offers a button to record it by hand
  // and always says which of the two happened.
  const ARMED_KEY = "scoutFillArmed";
  const ARMED_TTL_MS = 10 * 60 * 1000;
  function armedFromTab() {
    try {
      const a = JSON.parse(sessionStorage.getItem(ARMED_KEY) || "null");
      if (a && a.date && a.id && Date.now() - a.at < ARMED_TTL_MS) return a;
    } catch {}
    return null;
  }
  let watching = false;
  let submitted = false; // the application was sent: no more filling in this tab
  function watchSubmission(getJob) {
    if (watching) return;
    watching = true;
    let armed = Boolean(armedFromTab());
    let recorded = false;
    let markTimer = null;
    const jobNow = () => getJob() || armedFromTab();
    const record = async (how) => {
      if (recorded) return;
      recorded = true;
      submitted = true;
      clearTimeout(markTimer);
      await saveTyped(true);
      const job = jobNow();
      try { sessionStorage.removeItem(ARMED_KEY); } catch {}
      if (!panel) panel = makePanel(job);
      panel.mark.hidden = true;
      if (!job) {
        panel.status("Submitted. This job isn't on your Scout list, so there is nothing to tick. Your typed answers are saved.");
        return;
      }
      const res = await send({ type: "applied", date: job.date, id: job.id });
      panel.status(res.ok
        ? `Recorded as applied on your Scout list${how === "hand" ? " (marked by you)" : ""}.`
        : "NOT recorded: Scout could not reach your list. Tick it on your Scout list.");
      panel.host.dataset.state = res.ok ? "recorded" : "record-failed";
      try { sessionStorage.removeItem(STORE_KEY); } catch {}
    };
    const arm = () => {
      if (!filled || recorded) return;
      armed = true;
      const job = getJob();
      if (job) {
        try { sessionStorage.setItem(ARMED_KEY, JSON.stringify({ date: job.date, id: job.id, at: Date.now() })); } catch {}
      }
      clearTimeout(markTimer);
      markTimer = setTimeout(() => {
        if (recorded || !panel) return;
        if (jobNow()) {
          panel.status("No confirmation from the site yet. If your application went through, mark it here.");
          panel.mark.hidden = false;
        }
      }, 8000);
    };
    document.addEventListener("submit", arm, true);
    document.addEventListener("click", (e) => {
      const b = e.target && e.target.closest && e.target.closest("button, input[type=submit], [role=button]");
      if (b && /submit|apply|send/i.test(clean(b.innerText || b.value || b.getAttribute("aria-label")))) arm();
    }, true);
    const check = () => {
      if (!armed || recorded) return;
      if (CONFIRM_RE.test(document.body ? document.body.innerText : "")) record("site");
    };
    markHandler = () => record("hand");
    new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    setInterval(check, 1500);
    check();
  }

  // ------------------------------------------------------------ LinkedIn ----
  // Easy Apply is a multi-step window over the job page. The panel exists only
  // while that window is open. With autopilot off, nothing runs until he
  // clicks "Fill this step"; after that each new step is filled ONCE as it
  // appears and he presses Next / Review / Submit himself. With autopilot on
  // and a job from his list, it opens Easy Apply (a run's link) and walks the
  // steps: fill, check, press Next / Review / Submit application -- or park.
  function easyApplyWindow() {
    return document.querySelector(".jobs-easy-apply-modal, [data-test-modal-id='easy-apply-modal']")
      || [...document.querySelectorAll("[role=dialog]")].find((d) =>
        d.querySelector("form, input, select, textarea")
        && /apply/i.test(`${d.getAttribute("aria-labelledby") || ""} ${clean((d.querySelector("h1, h2, h3") || {}).innerText)}`));
  }
  function stepSignature(win) {
    return scan(win).map((f) => f.label).join("|");
  }
  const footerText = (w) => controls(w).map(controlText).join("|");
  // A run's LinkedIn link: press Easy Apply for him (he would have).
  async function openEasyApply(job) {
    for (let i = 0; i < 20 && !easyApplyWindow() && !autoStopped; i++) {
      if (passwordShown()) return autoPark(job, { reason: "login-required: LinkedIn wants you to sign in" });
      const b = [...document.querySelectorAll("button, a")].find((n) => visible(n) && !n.closest("#scout-fill-panel")
        && /^easy apply\b/i.test(controlText(n)));
      if (!b) {
        await sleep(1000);
        continue;
      }
      await humanDelay();
      if (auto.paused) {
        pendingAuto = () => openEasyApply(job);
        return;
      }
      if (!easyApplyWindow()) b.click();
      await sleep(3000);
    }
    if (!easyApplyWindow() && !autoStopped) {
      autoPark(job, { reason: "no-easy-apply: this LinkedIn job has no Easy Apply window (it applies on the company site)" });
    }
  }
  async function bootLinkedIn() {
    let win = null;
    let following = false;
    let job = null;
    let filledSteps = new Set();
    let stepping = false;
    // autopilot also walks steps with nothing to fill (Review), so its step
    // key includes the footer buttons
    const liStepKey = (w) => {
      const sig = stepSignature(w);
      const walking = Boolean(job) && autoEnabled() && !autoStopped && !submitted;
      return walking && controls(w).length ? `${sig}#${footerText(w)}` : sig;
    };
    const m = location.href.match(HASH_RE);
    const linkJob = m ? { date: m[1], id: m[2] } : null; // opened from his list / a run
    watchSubmission(() => job);
    await hello(linkJob);
    if (linkJob && autoEnabled()) openEasyApply(linkJob);
    setInterval(async () => {
      const w = easyApplyWindow();
      if (w && w !== win) {
        win = w;
        following = false;
        filledSteps = new Set();
        pending = [];
        lastTyped.clear();
        if (linkJob) job = linkJob;
        else {
          const res = await send({ type: "lookup", url: location.href });
          job = res.ok && res.data && res.data.job ? res.data.job : null;
        }
        if (panel) panel.host.remove();
        panel = makePanel(job, {
          line: job ? `${job.company || "Job"} - ${job.title || ""} (on your Scout list)` : "Easy Apply - filled from your profile and saved answers",
          button: "Fill this step",
        });
        following = Boolean(job) && autoEnabled() && !autoStopped;
        panel.status(following ? "Autopilot: filling each step, then pressing Next."
          : job ? "Tailored CV ready. Click Fill on each step you want filled." : "Click Fill to fill this step.");
        panel.btn.addEventListener("click", async () => {
          following = true;
          if (!job) job = await adoptThisJob();
          if (stepping) return;
          stepping = true; // one fill (and one autopilot press) per step at a time
          try {
            filledSteps.add(liStepKey(win));
            await fill(job, win);
          } finally {
            stepping = false;
          }
        });
        panel.save.addEventListener("click", () => saveTyped(false));
        panel.report.hidden = false;
        panel.report.addEventListener("click", () => sendLayout(win));
      } else if (!w && win) {
        win = null;
        if (panel) { panel.host.remove(); panel = null; }
      } else if (w && following && !stepping && !(panel && panel.btn.disabled)) {
        const sig = stepSignature(w);
        const key = liStepKey(w);
        if (!key || filledSteps.has(key)) return;
        filledSteps.add(key);
        stepping = true;
        try {
          if (sig) await fill(job, w);
          else await autoStep(w, job, { questions: [], uploads: [] });
        } finally {
          stepping = false;
        }
      }
    }, 1000);
  }

  // ----------------------------------------------- opened by HIM ----
  // He clicked the Scout icon: open the panel on this page even if it isn't a
  // job from his list, and fill straight away (the click is his go).
  let panelBooted = false;
  let currentJob = null;
  function bootPanel(job, line) {
    panelBooted = true;
    panel = makePanel(job, line ? { line } : {});
    panel.btn.addEventListener("click", () => fill(currentJob));
    panel.save.addEventListener("click", () => saveTyped(false));
    panel.report.addEventListener("click", () => sendLayout(document));
    watchSubmission(() => currentJob);
    // join.com moves through CV -> cover letter -> questions without loading
    // a new page. Once he has used the panel, each new step is filled once.
    let lastHref = location.href;
    const done = new Set();
    setInterval(async () => {
      if (location.href === lastHref) return;
      lastHref = location.href;
      if (!filled || submitted || done.has(lastHref) || !panel || panel.btn.disabled) return;
      done.add(lastHref);
      await sleep(1200); // let the new step render
      if (!submitted) fill(currentJob);
    }, 700);
  }
  // A job not on his list: Scout reads its description (this page, the job
  // page behind it, or -- on LinkedIn -- only the page he has open), adds it
  // to his list and tailors his CV to it, so every fill is for THIS job.
  function pageHints() {
    if (LINKEDIN) {
      const id = new URL(location.href).searchParams.get("currentJobId")
        || (location.pathname.match(/\/jobs\/view\/(?:[^/]*?-)?(\d{6,})/) || [])[1];
      const parts = document.title.split("|").map(clean);
      const desc = document.querySelector("#job-details, .jobs-description__content, .jobs-description, [class*='jobs-description']");
      return {
        url: id ? `https://www.linkedin.com/jobs/view/${id}/` : location.href,
        title: parts[0] || "", company: parts[1] && parts[1] !== "LinkedIn" ? parts[1] : "",
        text: clean(desc ? desc.innerText : "").slice(0, 60000),
      };
    }
    const h1 = document.querySelector("h1");
    return {
      url: location.href, company: "",
      title: `${document.title} | ${h1 ? clean(h1.innerText) : ""}`.slice(0, 500),
      text: clean(document.body ? document.body.innerText : "").slice(0, 60000),
    };
  }
  async function adoptThisJob() {
    panel.btn.disabled = true;
    panel.status("Reading the job description and tailoring your CV to it (up to a minute)...");
    const res = await send({ type: "adopt", ...pageHints() });
    panel.btn.disabled = false;
    if (!res.ok || !res.data || !res.data.id) {
      panel.status(`Couldn't find a job description here (${res.error || "no JD"}). Filling from your profile only.`);
      return null;
    }
    const job = { date: res.data.date, id: res.data.id, company: res.data.company, title: res.data.title };
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify({ date: job.date, id: job.id })); } catch {}
    panel.line(`${job.company} - ${job.title} (added to your Scout list, CV tailored)`);
    return job;
  }

  async function openByHand() {
    if (LINKEDIN) {
      if (panel) return;
      panel = makePanel(null, { line: "LinkedIn", button: "Close" });
      panel.status("Open the Easy Apply window first. The Fill button appears on it by itself.");
      panel.btn.addEventListener("click", () => { panel.host.remove(); panel = null; });
      return;
    }
    if (panelBooted) {
      if (panel && !panel.btn.disabled) fill(currentJob);
      return;
    }
    currentJob = await resolveJob();
    bootPanel(currentJob, currentJob ? null : "New job - adding it to your Scout list");
    if (!currentJob) currentJob = await adoptThisJob();
    fill(currentJob);
  }
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "scout-open" && window.top === window) openByHand();
  });

  // --------------------------------------------------------------- boot ----
  // A run moving to the next job on the same page (only the #scout-fill link
  // differs) does not reload it: reload, so the new job starts clean.
  const bootLink = (location.href.match(HASH_RE) || [])[0] || "";
  addEventListener("hashchange", () => {
    const m = location.href.match(HASH_RE);
    if (m && m[0] !== bootLink && window.top === window) location.reload();
  });

  (async () => {
    await loadAuto();
    if (LINKEDIN) {
      if (window.top === window) bootLinkedIn();
      return;
    }
    if (window.top !== window && !document.querySelector("form, input, textarea")) return;
    const job = await resolveJob();
    if (job && job.auto) {
      // a fresh link: a "pressed Submit" mark left by ANOTHER job is stale
      const a = armedFromTab();
      if (a && (a.date !== job.date || a.id !== job.id)) {
        try { sessionStorage.removeItem(ARMED_KEY); } catch {}
      }
    }
    if (window.top === window) await hello(job);
    // in a run, the run's job counts as opened from its link even if the
    // site dropped the #scout-fill part -- but never on its thank-you page
    if (job && !job.auto && auto.current && !armedFromTab()) job.auto = true;
    if (!job && armedFromTab() && window.top === window) {
      // the thank-you page after a Submit he pressed on this tab's form
      filled = true;
      watchSubmission(() => null);
      return;
    }
    if (!job || panelBooted) return; // not a job from his list: stay invisible until he clicks the icon
    currentJob = job;
    bootPanel(job);
    panel.status(job.auto ? "Filling in a moment..." : "This job is on your Scout list.");
    if (job.auto) {
      await sleep(1200); // let single-page forms finish rendering
      fill(job);
    }
  })();
})();
