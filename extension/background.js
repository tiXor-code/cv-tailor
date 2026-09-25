// Scout Fill -- background service worker.
// The only component that talks to admin.teodorlutoiu.com, with the personal
// extension key from chrome.storage.local (never synced, never in the page).
// Content scripts ask it for four things; anything else is refused.

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

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(out);
}

async function handle(msg) {
  switch (msg && msg.type) {
    case "lookup": {
      const url = String(msg.url || "").slice(0, 2000);
      return (await api(`/api/scout/ext/lookup?url=${encodeURIComponent(url)}`)).json();
    }
    case "answer": {
      const { date, id } = job(msg);
      const questions = Array.isArray(msg.questions) ? msg.questions.slice(0, 60) : [];
      return (await api("/api/scout/ext/answer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ date, id, questions }),
      })).json();
    }
    case "cv": {
      const { date, id } = job(msg);
      const res = await api(`/api/scout/ext/cv?date=${date}&id=${encodeURIComponent(id)}`);
      return { base64: toBase64(await res.arrayBuffer()) };
    }
    case "applied": {
      const { date, id } = job(msg);
      return (await api("/api/scout/ext/applied", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ date, id }),
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
  handle(msg).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String((err && err.message) || err) }),
  );
  return true;
});
