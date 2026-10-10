// Shared helpers: API calls, Persian formatting, icons, small components.
import { html, useEffect, useRef, useState } from "./vendor/preact-htm.js";

// ── API ──────────────────────────────────────────────────────────────
export const events = new EventTarget(); // "unauthorized", "need2fa"

// Dangerous actions need a fresh 2FA code (valid 15 minutes). When the hub
// answers "elevation_required", the app shows a code dialog and the request
// is retried once.
let elevating = null;
export function requestElevation() {
  if (!elevating) {
    elevating = new Promise((resolve) => {
      events.dispatchEvent(new CustomEvent("elevate", { detail: { resolve } }));
    }).finally(() => { elevating = null; });
  }
  return elevating;
}

export async function api(path, { method = "GET", body, form, retried = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: { "X-ICSD": "1" } };
  if (form !== undefined) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch("/api" + path, opts);
  } catch {
    throw new Error("ارتباط با پنل برقرار نشد");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.ok) return data;
  const detail = data && data.detail;
  if (res.status === 401 && !path.startsWith("/auth/login")) events.dispatchEvent(new Event("unauthorized"));
  if (res.status === 403 && detail === "2fa_setup_required") events.dispatchEvent(new Event("need2fa"));
  if (res.status === 403 && detail === "elevation_required" && !retried) {
    if (await requestElevation()) return api(path, { method, body, form, retried: true });
    const err = new Error("تأیید با رمز دوم لغو شد");
    err.status = 403;
    throw err;
  }
  const err = new Error(typeof detail === "string" ? detail : (detail && detail.message) || `خطای ${res.status}`);
  err.status = res.status;
  err.data = detail;
  throw err;
}

// Poll an endpoint while the component is mounted (and the tab is visible).
export function usePoll(path, ms, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const tick = useRef(0);
  const reload = async () => {
    const my = ++tick.current;
    try {
      const d = await api(path);
      if (my === tick.current) { setData(d); setError(null); }
    } catch (e) {
      if (my === tick.current) setError(e);
    }
  };
  useEffect(() => {
    setData(null);
    reload();
    if (!ms) return;
    const id = setInterval(() => { if (!document.hidden) reload(); }, ms);
    return () => { clearInterval(id); tick.current++; };
  }, [path, ...deps]);
  return [data, error, reload, setData];
}

// ── formatting (Persian digits, Jalali dates) ───────────────────────
const nf = {};
export function fa(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const key = digits;
  nf[key] ||= new Intl.NumberFormat("fa-IR", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  return nf[key].format(n);
}
export const pct = (v, d = 0) => (v === null || v === undefined ? "—" : fa(v, d) + "٪");

export function bytes(b, digits = 1) {
  if (b === null || b === undefined) return "—";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (Math.abs(b) >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${fa(b, i === 0 ? 0 : b < 10 ? digits : 0)} ${u[i]}`;
}
export const rate = (b) => (b === null || b === undefined ? "—" : bytes(b) + "/s");

export function dur(sec) {
  if (!sec && sec !== 0) return "—";
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${fa(d)} روز${h ? " و " + fa(h) + " ساعت" : ""}`;
  if (h) return `${fa(h)} ساعت${m ? " و " + fa(m) + " دقیقه" : ""}`;
  if (m) return `${fa(m)} دقیقه`;
  return `${fa(sec)} ثانیه`;
}

export function ago(ts) {
  if (!ts) return "هرگز";
  const s = Date.now() / 1000 - ts;
  if (s < 10) return "همین حالا";
  if (s < 60) return `${fa(s)} ثانیه پیش`;
  return `${dur(s)} پیش`;
}

const dtf = new Intl.DateTimeFormat("fa-IR", { dateStyle: "short", timeStyle: "short" });
const tf = new Intl.DateTimeFormat("fa-IR", { hour: "2-digit", minute: "2-digit" });
const df = new Intl.DateTimeFormat("fa-IR", { month: "short", day: "numeric" });
export const dt = (ts) => (ts ? dtf.format(new Date(ts * 1000)) : "—");
export const timeOnly = (ts) => tf.format(new Date(ts * 1000));
export const dayOnly = (ts) => df.format(new Date(ts * 1000));

export const level = (v, warn = 75, crit = 90) => (v >= crit ? "crit" : v >= warn ? "warn" : "");

// ── per-browser preferences (may be unavailable: private mode, blocked storage)
export function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
export function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* not important */ }
}

// ── toasts ───────────────────────────────────────────────────────────
export function toast(msg, err = false) {
  let box = document.querySelector(".toasts");
  if (!box) {
    box = document.createElement("div");
    box.className = "toasts";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), err ? 6000 : 3000);
}

export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("کپی شد");
  } catch {
    toast("کپی نشد؛ متن را دستی انتخاب کنید", true);
  }
}

// ── icons (stroke icons, 24px grid) ─────────────────────────────────
const P = {
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0",
  globe: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  server: "M2 3h20v7H2zM2 14h20v7H2zM6 6.5h.01M6 17.5h.01",
  plus: "M12 5v14M5 12h14",
  out: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  copy: "M9 9h11v11H9zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
  trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
  back: "M9 18l6-6-6-6",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  key: "M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8zM15.5 7.5l3 3L22 7l-3-3",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  file: "M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6",
  up: "M12 19V5M5 12l7-7 7 7",
  down: "M12 5v14M5 12l7 7 7-7",
  upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12",
  refresh: "M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15",
  db: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
  lock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
  term: "M4 17l6-6-6-6M12 19h8",
  play: "M6 4l14 8-14 8z",
  stop: "M6 6h12v12H6z",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  archive: "M21 8v13H3V8M1 3h22v5H1zM10 12h4",
  save: "M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8M7 3v5h8",
  cog: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
};
export const Icon = ({ name, cls = "" }) => html`<svg class=${"icon " + cls} viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d=${P[name]} /></svg>`;

// ── small components ────────────────────────────────────────────────
const STATUS_FA = {
  online: "آنلاین", offline: "قطع", pending: "در انتظار نصب", running: "در حال اجرا",
  exited: "متوقف", restarting: "در حال ری‌استارت", dead: "از کار افتاده", created: "ساخته‌شده",
  paused: "مکث", healthy: "سالم", unhealthy: "ناسالم", starting: "در حال شروع",
  critical: "بحرانی", warning: "هشدار", ok: "سالم", crit: "خطا",
};
export const Pill = ({ s, label }) => html`<span class=${"pill " + s}>${label || STATUS_FA[s] || s}</span>`;

export const Bar = ({ v, warn = 75, crit = 90 }) =>
  html`<div class="bar"><i class=${level(v || 0, warn, crit)} style=${`width:${Math.min(100, Math.max(0, v || 0))}%`}></i></div>`;

export const Meter = ({ label, v, sub, warn, crit }) => html`
  <div class="meter" title=${sub || ""}>
    <span class="lbl">${label}</span><${Bar} v=${v} warn=${warn} crit=${crit} /><span class="val">${pct(v)}</span>
  </div>`;

export function Spark({ data }) {
  if (!data || data.length < 2) return html`<svg class="spark"></svg>`;
  const w = 300, h = 34, n = data.length;
  const pts = data.map((v, i) => [(i / (n - 1)) * w, h - 2 - (Math.min(100, v) / 100) * (h - 4)]);
  const line = "M" + pts.map((p) => p.map((x) => x.toFixed(1)).join(",")).join("L");
  return html`<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="CPU، نیم ساعت اخیر">
    <path class="a" d=${`${line}L${w},${h}L0,${h}Z`} /><path class="l" d=${line} /></svg>`;
}

export const Empty = ({ title, children }) => html`<div class="empty"><b>${title}</b>${children}</div>`;

export function Dialog({ title, onClose, children, footer }) {
  useEffect(() => {
    const esc = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);
  return html`<div class="overlay" onClick=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="dialog" role="dialog" aria-modal="true" aria-label=${title}>
      <div class="hd"><h2 class="grow">${title}</h2><button class="ghost sm" onClick=${onClose} aria-label="بستن">✕</button></div>
      <div class="bd">${children}</div>
      ${footer && html`<div class="ft">${footer}</div>`}
    </div></div>`;
}

export function Command({ text }) {
  return html`<div class="cmd"><code>${text}</code>
    <button onClick=${() => copy(text)} title="کپی"><${Icon} name="copy" /></button></div>`;
}

// Run an async action with a busy flag and an error toast.
export function useAction() {
  const [busy, setBusy] = useState(false);
  const run = async (fn, ok) => {
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast(ok);
      return r;
    } catch (e) {
      toast(e.message, true);
      throw e;
    } finally {
      setBusy(false);
    }
  };
  return [busy, run];
}

// Follow a background job on a server until it ends.
export function useJob(sid, jobId, onDone) {
  const [job, setJob] = useState(null);
  useEffect(() => {
    if (!jobId) return;
    let stop = false;
    const poll = async () => {
      try {
        const j = await api(`/servers/${sid}/jobs/${jobId}`);
        if (stop) return;
        setJob(j);
        if (j.state !== "running") { onDone && onDone(j); return; }
      } catch (e) {
        if (stop) return;
        setJob({ state: "error", error: e.message });
        return;
      }
      setTimeout(poll, 1500);
    };
    poll();
    return () => { stop = true; };
  }, [sid, jobId]);
  return job;
}

export function JobBox({ sid, jobId, title, onDone }) {
  const job = useJob(sid, jobId, onDone);
  if (!jobId) return null;
  const st = !job ? "running" : job.state;
  return html`<div class="job">
    <div class="row"><${Pill} s=${st === "done" ? "ok" : st === "error" ? "crit" : "pending"}
      label=${st === "done" ? "انجام شد" : st === "error" ? "ناموفق" : "در حال انجام…"} />
      <b class="grow">${title || (job && job.title) || ""}</b>
      ${job && job.ended ? html`<span class="faint small">${dur(job.ended - job.started)}</span>` : null}</div>
    ${job && job.error && html`<div class="error" style="margin-top:8px">${job.error}</div>`}
    ${job && job.log && html`<pre class="logbox">${job.log}</pre>`}
  </div>`;
}

export const LogLines = ({ lines }) => html`<pre class="logbox tall">${(lines || []).join("\n") || "—"}</pre>`;
