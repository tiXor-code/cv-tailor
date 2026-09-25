// Scout Fill -- content script (Teodor, 2026-09-25).
//
// On a job application form from his Scout list: find every field, ask Scout
// (via the background worker -> admin -> the mini) for its answers, fill them,
// attach the tailored CV, and highlight whatever only he can answer.
//
// It NEVER presses Submit. He reviews and submits from his own browser, which
// is what the sites' spam filters want to see. Only after HE submits and the
// site shows its confirmation does it tick "applied" on his list.
//
// Page text is untrusted: everything shown in the panel goes through
// textContent, never innerHTML.
(() => {
  if (window.__scoutFill) return;
  window.__scoutFill = true;

  const HASH_RE = /#scout-fill=(\d{4}-\d{2}-\d{2})~([A-Za-z0-9_-]{1,64})/;
  const STORE_KEY = "scoutFillJob";
  const CONFIRM_RE =
    /(application (has been |was )?(submitted|received|sent)|thank(s| you) for (applying|your application|submitting)|we(?:'|’)ve received your application|your application is (in|complete))/i;
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "password", "image", "file"]);

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
    const res = await send({ type: "lookup", url: location.href });
    if (res.ok && res.data && res.data.job) return { ...res.data.job, auto: false };
    return null;
  }

  // -------------------------------------------------------------- panel ----
  let panel = null;
  function makePanel(job) {
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
      .muted{color:#5b6865}`;
    const box = document.createElement("div");
    box.className = "box";
    const title = document.createElement("div");
    title.className = "t";
    title.textContent = "Scout";
    const line = document.createElement("div");
    line.className = "muted";
    line.textContent = job.company ? `${job.company} - ${job.title || ""}` : "Job from your Scout list";
    const status = document.createElement("div");
    status.setAttribute("role", "status");
    const list = document.createElement("ul");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Fill this form";
    const save = document.createElement("button");
    save.type = "button";
    save.hidden = true;
    save.style.marginLeft = "6px";
    box.append(title, line, status, list, btn, save);
    root.append(style, box);
    document.documentElement.append(host);
    return {
      host,
      btn,
      save,
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
    return clean(el.getAttribute("placeholder") || el.getAttribute("name") || "");
  }

  function isRequired(el, label) {
    if (el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label || "")) return true;
    // Ashby draws its "*" with CSS on a _required_ label class, so innerText lacks it.
    const box = containerOf(el);
    return Boolean(box && box.querySelector("label[class*='required'], [class*='question-title'][class*='required']"));
  }

  function optionText(input) {
    if (input.labels && input.labels.length) return clean(input.labels[0].innerText);
    return clean(input.getAttribute("aria-label") || input.value);
  }

  // Each field: {kind, label, required, options, set(value) -> Promise<bool>, el}
  function scan() {
    const fields = [];
    const seenGroups = new Set();
    const controls = document.querySelectorAll("input, textarea, select");
    for (const el of controls) {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (el.tagName === "INPUT" && SKIP_TYPES.has(type)) continue;
      if (/recaptcha|captcha|honeypot/i.test(el.name || el.id || "")) continue;
      if (type === "radio" || type === "checkbox") {
        const key = `${type}:${el.name || el.id}`;
        if (seenGroups.has(key)) continue;
        seenGroups.add(key);
        const group = el.name
          ? [...document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)]
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
    for (const box of document.querySelectorAll(".ashby-application-form-field-entry, fieldset, [class*='question']")) {
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

  async function attachCv(job) {
    const inputs = [...document.querySelectorAll("input[type=file]")];
    const target = inputs.find((i) => /resume|cv|curriculum/i.test(`${i.id} ${i.name} ${labelOf(i)}`)) || inputs[0];
    if (!target) return null;
    const res = await send({ type: "cv", date: job.date, id: job.id });
    if (!res.ok) return false;
    const bytes = Uint8Array.from(atob(res.data.base64), (c) => c.charCodeAt(0));
    const file = new File([bytes], "Teodor-Lutoiu-CV.pdf", { type: "application/pdf" });
    const dt = new DataTransfer();
    dt.items.add(file);
    target.files = dt.files;
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    return target.files.length === 1;
  }

  function flag(el) {
    const box = containerOf(el) || el;
    box.style.outline = "3px solid #e0891b";
    box.style.outlineOffset = "4px";
  }

  // --------------------------------------------------------------- fill ----
  let filled = false;
  async function fill(job) {
    panel.btn.disabled = true;
    panel.status("Reading the form...");
    let fields = scan();
    if (!fields.length) {
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
    if (!fields.length) {
      panel.status("No form found on this page yet. Open the application form, then press Fill.");
      panel.btn.disabled = false;
      return;
    }
    panel.status(`Asking Scout about ${fields.length} fields...`);
    const res = await send({
      type: "answer", date: job.date, id: job.id,
      questions: fields.map((f) => ({ label: f.label.slice(0, 500), kind: f.kind, required: f.required, options: f.options.slice(0, 100) })),
    });
    if (!res.ok) {
      panel.status(`Scout could not answer: ${res.error}`);
      panel.btn.disabled = false;
      return;
    }
    const { answers = [], cover_letter: letter = "" } = res.data || {};
    const needs = [];
    let done = 0;
    for (let i = 0; i < fields.length; i++) {
      const f = fields[i];
      let value = answers[i] && answers[i].value;
      if (!value && letter && f.kind === "textarea" && /cover letter|why .*(join|us|role|interested)/i.test(f.label)) value = letter;
      let ok = false;
      if (value) {
        try { ok = await f.set(value); } catch { ok = false; }
      }
      if (ok) done += 1;
      else if (f.required || (answers[i] && answers[i].needs_you)) {
        needs.push(f.label.replace(/\s*\*\s*$/, ""));
        flag(f.el);
      }
    }
    pending = fields.filter((f) => needs.includes(f.label.replace(/\s*\*\s*$/, "")));
    const cv = await attachCv(job);
    if (cv === false) needs.push("CV upload (download it from your Scout list)");
    panel.needs(needs);
    panel.status(needs.length
      ? `Filled ${done} of ${fields.length}${cv ? " + CV" : ""}. ${needs.length} need you (outlined). Then press Submit.`
      : `Filled ${done} of ${fields.length}${cv ? " + CV" : ""}. Review, then press Submit.`);
    panel.btn.textContent = "Fill again";
    panel.btn.disabled = false;
    filled = true;
    panel.host.dataset.state = "filled"; // observable finish line (tests, and future tooling)
  }

  // ---------------------------------------- saving what HE typed ----
  // Outlined questions are the ones Scout could not answer. Once he fills
  // them, offer to save his answers so no form ever asks him twice. On a
  // confirmed submission they are saved anyway: that is what he sent.
  let pending = [];
  function typedAnswers() {
    return pending
      .filter((f) => f.kind !== "consent")
      .map((f) => ({ label: f.label.slice(0, 500), value: currentValue(f).slice(0, 4000) }))
      .filter((a) => a.value);
  }
  function refreshSaveButton() {
    const n = typedAnswers().length;
    panel.save.hidden = n === 0;
    panel.save.textContent = `Save ${n} answer${n === 1 ? "" : "s"} for next time`;
  }
  async function saveTyped(quiet) {
    const answers = typedAnswers();
    if (!answers.length) return;
    const res = await send({ type: "save", answers });
    if (!quiet) panel.status(res.ok ? `Saved ${res.data.saved}. Scout will fill ${res.data.saved === 1 ? "it" : "them"} next time.` : `Could not save: ${res.error}`);
    if (res.ok) {
      pending = pending.filter((f) => !currentValue(f));
      refreshSaveButton();
    }
  }
  document.addEventListener("input", () => pending.length && refreshSaveButton(), true);
  document.addEventListener("change", () => pending.length && setTimeout(refreshSaveButton, 50), true);
  document.addEventListener("click", () => pending.length && setTimeout(refreshSaveButton, 50), true);

  // ------------------------------------------------- after HE submits ----
  function watchSubmission(job) {
    let armed = false;
    const arm = () => { if (filled) armed = true; };
    document.addEventListener("submit", arm, true);
    document.addEventListener("click", (e) => {
      const b = e.target && e.target.closest && e.target.closest("button, input[type=submit]");
      if (b && /submit|apply|send application/i.test(clean(b.innerText || b.value))) arm();
    }, true);
    let recorded = false;
    const check = async () => {
      if (!armed || recorded) return;
      if (CONFIRM_RE.test(document.body ? document.body.innerText : "")) {
        recorded = true;
        await saveTyped(true);
        const res = await send({ type: "applied", date: job.date, id: job.id });
        panel.status(res.ok ? "Submitted. Recorded as applied on your Scout list." : "Submitted. Tick it on your Scout list.");
        try { sessionStorage.removeItem(STORE_KEY); } catch {}
      }
    };
    new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    setInterval(check, 1500);
  }

  // --------------------------------------------------------------- boot ----
  (async () => {
    if (window.top !== window && !document.querySelector("form, input, textarea")) return;
    const job = await resolveJob();
    if (!job) return; // not a job from his list: stay invisible
    panel = makePanel(job);
    panel.status(job.auto ? "Filling in a moment..." : "This job is on your Scout list.");
    panel.btn.addEventListener("click", () => fill(job));
    panel.save.addEventListener("click", () => saveTyped(false));
    watchSubmission(job);
    if (job.auto) {
      await sleep(1200); // let single-page forms finish rendering
      fill(job);
    }
  })();
})();
