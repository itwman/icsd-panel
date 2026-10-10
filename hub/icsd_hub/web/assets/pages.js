// Alerts, URL checks and settings.
import { html, useEffect, useState } from "./vendor/preact-htm.js";
import { Setup2FA } from "./auth.js";
import { Chart } from "./chart.js";
import { AlertList, CheckRows } from "./dashboard.js";
import { Dialog, Empty, Icon, Pill, api, dt, fa, toast, useAction, usePoll } from "./lib.js";

// ── alerts ───────────────────────────────────────────────────────────
export function AlertsPage({ me }) {
  const [d, error, reload] = usePoll("/alerts", 10000);
  if (error && !d) return html`<div class="error">${error.message}</div>`;
  if (!d) return html`<div class="skeleton">در حال بارگذاری…</div>`;
  return html`<div class="page-head"><h1 class="grow">هشدارها</h1></div>
    <div class="stack">
      <div class="card"><div class="hd"><h2>باز</h2></div><div class="bd flush"><${AlertList} items=${d.open} /></div></div>
      <div class="card"><div class="hd"><h2 class="grow">قاعده‌ها</h2>
        <span class="faint small">هر هشدار یک بار فرستاده می‌شود و وقتی رفع شد یک «رفع شد».</span></div>
        <div class="bd flush"><${Rules} rules=${d.rules} admin=${me.role === "admin"} onSaved=${reload} /></div></div>
      <div class="card"><div class="hd"><h2>تاریخچه</h2></div><div class="bd flush">
        <${AlertList} items=${d.history.filter((e) => e.ended)} empty="هنوز هشداری رفع نشده است" /></div></div>
    </div>`;
}

function Rules({ rules, admin, onSaved }) {
  const [edit, setEdit] = useState({});
  const [busy, run] = useAction();
  const val = (r, k) => (edit[r.kind] && k in edit[r.kind] ? edit[r.kind][k] : r[k]);
  const set = (r, k, v) => setEdit({ ...edit, [r.kind]: { ...(edit[r.kind] || {}), [k]: v } });
  const saveRule = (r) => run(() => api("/rules/" + r.kind, { method: "PUT", body: {
    threshold: Number(val(r, "threshold")), for_s: Math.round(Number(val(r, "for_s"))),
    severity: val(r, "severity"), enabled: !!val(r, "enabled"),
  } }), "ذخیره شد").then(() => { setEdit({ ...edit, [r.kind]: undefined }); onSaved(); });
  const noThreshold = ["container"];
  return html`<div class="tbl-wrap"><table>
    <thead><tr><th>فعال</th><th>قاعده</th><th>آستانه</th><th>ماندگاری (ثانیه)</th><th>شدت</th><th></th></tr></thead>
    <tbody>${rules.map((r) => html`<tr>
      <td><input type="checkbox" disabled=${!admin} checked=${val(r, "enabled")} onChange=${(e) => set(r, "enabled", e.target.checked)} /></td>
      <td>${r.label}</td>
      <td>${noThreshold.includes(r.kind) ? html`<span class="faint">—</span>` : html`<div class="row" style="flex-wrap:nowrap">
        <input type="number" step="any" style="width:90px" disabled=${!admin} value=${val(r, "threshold")}
          onInput=${(e) => set(r, "threshold", e.target.value)} /><span class="faint small nowrap">${r.unit}</span></div>`}</td>
      <td>${r.kind === "offline" || r.kind === "check" || r.kind === "cert" ? html`<span class="faint">—</span>`
        : html`<input type="number" min="0" style="width:90px" disabled=${!admin} value=${val(r, "for_s")}
          onInput=${(e) => set(r, "for_s", e.target.value)} />`}</td>
      <td><select style="width:auto" disabled=${!admin} value=${val(r, "severity")} onChange=${(e) => set(r, "severity", e.target.value)}>
        <option value="warning">هشدار</option><option value="critical">بحرانی</option></select></td>
      <td>${admin && edit[r.kind] && html`<button class="primary sm" disabled=${busy} onClick=${() => saveRule(r)}>ذخیره</button>`}</td>
    </tr>`)}</tbody></table></div>`;
}

// ── URL checks ───────────────────────────────────────────────────────
export function ChecksPage({ me, theme }) {
  const [d, error, reload] = usePoll("/checks", 15000);
  const [form, setForm] = useState(null);
  const [open, setOpen] = useState(null);
  const admin = me.role === "admin";
  if (error && !d) return html`<div class="error">${error.message}</div>`;
  if (!d) return html`<div class="skeleton">در حال بارگذاری…</div>`;
  return html`<div class="page-head"><h1 class="grow">پایش آدرس‌ها</h1>
      ${admin && html`<button class="primary" onClick=${() => setForm({})}><${Icon} name="plus" />افزودن آدرس</button>`}</div>
    <p class="muted small" style="margin-top:-8px">پنل هر آدرس را به‌طور منظم باز می‌کند: پاسخ ۵xx یا قطعی یعنی خرابی، و تاریخ انقضای گواهی SSL هم بررسی می‌شود.</p>
    <div class="card">${d.checks.length ? html`<div class="tbl-wrap"><table>
        <thead><tr><th>وضعیت</th><th>آدرس</th><th class="num">پاسخ</th><th class="num">در دسترس ۲۴ساعت</th><th class="num">SSL</th><th></th></tr></thead>
        <tbody>${d.checks.map((c) => html`
          <${CheckRows} checks=${[c]} />
          <tr><td colspan="6" style="padding-top:0">
            <div class="row small">
              ${c.error && html`<span class="error" style="padding:2px 8px">${c.error}</span>`}
              <span class="faint">هر ${fa(c.interval)} ثانیه · انتظار ${c.expect}${c.keyword ? " · عبارت «" + c.keyword + "»" : ""}</span>
              <span class="grow"></span>
              <button class="ghost sm" onClick=${() => setOpen(open === c.id ? null : c.id)}>${open === c.id ? "بستن نمودار" : "نمودار"}</button>
              ${admin && html`<button class="ghost sm" onClick=${() => setForm(c)}><${Icon} name="edit" />ویرایش</button>`}
            </div>
            ${open === c.id && html`<${CheckChart} id=${c.id} theme=${theme} />`}
          </td></tr>`)}</tbody></table></div>`
      : html`<${Empty} title="هیچ آدرسی ثبت نشده">مثلاً صفحهٔ اصلی و API سایت‌هایتان را اضافه کنید.<//>`}</div>
    ${form && html`<${CheckForm} c=${form} onClose=${() => setForm(null)} onSaved=${() => { setForm(null); reload(); }} />`}`;
}

function CheckChart({ id, theme }) {
  const [d] = usePoll(`/checks/${id}/log?hours=24`, 60000, [id]);
  if (!d) return html`<div class="skeleton">…</div>`;
  if (!d.t.length) return html`<${Empty} title="هنوز نتیجه‌ای نیست" />`;
  return html`<div style="margin-top:8px"><${Chart} data=${d} theme=${theme} height=${160} fmt=${(v) => fa(v) + "ms"}
      series=${[{ key: "ms", label: "زمان پاسخ", color: "--c2", fill: true }]} />
    ${d.errors.length > 0 && html`<div class="small" style="margin-top:6px">${d.errors.slice(-5).reverse().map((e) =>
      html`<div class="faint">${dt(e.ts)} — ${e.error}</div>`)}</div>`}</div>`;
}

function CheckForm({ c, onClose, onSaved }) {
  const [f, setF] = useState({ name: c.name || "", url: c.url || "https://", interval: c.interval || 60,
    timeout: c.timeout || 10, expect: c.expect || "2xx,3xx", keyword: c.keyword || "", enabled: c.enabled ?? true });
  const [busy, run] = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const submit = (e) => {
    e.preventDefault();
    run(() => api(c.id ? "/checks/" + c.id : "/checks", { method: c.id ? "PUT" : "POST", body: f }), "ذخیره شد").then(onSaved);
  };
  const del = () => run(() => api("/checks/" + c.id, { method: "DELETE" }), "حذف شد").then(onSaved);
  return html`<${Dialog} title=${c.id ? "ویرایش آدرس" : "افزودن آدرس"} onClose=${onClose}>
    <form class="form" onSubmit=${submit}>
      <label class="field">آدرس<input class="ltr" required value=${f.url} onInput=${set("url")} placeholder="https://farshplus.ir/" /></label>
      <label class="field">نام<input value=${f.name} onInput=${set("name")} placeholder="مثلاً سایت فرش‌پلاس" /></label>
      <div class="two">
        <label class="field">هر چند ثانیه<input type="number" min="30" max="3600" value=${f.interval} onInput=${set("interval")} /></label>
        <label class="field">مهلت پاسخ (ثانیه)<input type="number" min="2" max="60" value=${f.timeout} onInput=${set("timeout")} /></label>
      </div>
      <div class="two">
        <label class="field">کد پاسخ قابل قبول<input class="ltr" value=${f.expect} onInput=${set("expect")} /></label>
        <label class="field">عبارتی که باید در صفحه باشد (اختیاری)<input value=${f.keyword} onInput=${set("keyword")} /></label>
      </div>
      <label class="check"><input type="checkbox" checked=${f.enabled} onChange=${set("enabled")} />فعال</label>
      <div class="row"><button class="primary" disabled=${busy}>ذخیره</button><span class="grow"></span>
        ${c.id && html`<button type="button" class="danger" disabled=${busy} onClick=${del}><${Icon} name="trash" />حذف</button>`}</div>
    </form><//>`;
}

// ── settings ─────────────────────────────────────────────────────────
export function SettingsPage({ me, onMe }) {
  const admin = me.role === "admin";
  const tabs = [["account", "حساب من"], ...(admin ? [["notify", "اعلان‌ها"], ["users", "کاربران"], ["audit", "گزارش فعالیت"]] : [])];
  const [tab, setTab] = useState("account");
  return html`<div class="page-head"><h1 class="grow">تنظیمات</h1></div>
    <div class="tabs">${tabs.map(([k, l]) => html`<button class=${tab === k ? "on" : ""} onClick=${() => setTab(k)}>${l}</button>`)}</div>
    ${tab === "account" && html`<${Account} me=${me} onMe=${onMe} />`}
    ${tab === "notify" && html`<${Notify} />`}
    ${tab === "users" && html`<${Users} me=${me} />`}
    ${tab === "audit" && html`<${Audit} />`}`;
}

function Account({ me, onMe }) {
  const [f, setF] = useState({ old: "", new: "", again: "" });
  const [busy, run] = useAction();
  const [redo, setRedo] = useState(false);
  const submit = (e) => {
    e.preventDefault();
    if (f.new !== f.again) { toast("رمز جدید و تکرارش یکسان نیستند", true); return; }
    run(() => api("/me/password", { method: "POST", body: { old: f.old, new: f.new } }), "رمز عوض شد؛ دوباره وارد شوید")
      .then(() => setTimeout(() => location.reload(), 1200)).catch(() => {});
  };
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return html`<div class="grid two">
    <div class="card"><div class="hd"><h2>تغییر رمز</h2></div><form class="bd form" onSubmit=${submit}>
      <label class="field">رمز فعلی<input type="password" class="ltr" autocomplete="current-password" required value=${f.old} onInput=${set("old")} /></label>
      <label class="field">رمز جدید (دست‌کم ۱۰ نویسه)<input type="password" class="ltr" autocomplete="new-password" required minlength="10" value=${f.new} onInput=${set("new")} /></label>
      <label class="field">تکرار رمز جدید<input type="password" class="ltr" autocomplete="new-password" required value=${f.again} onInput=${set("again")} /></label>
      <div><button class="primary" disabled=${busy}>تغییر رمز</button></div>
    </form></div>
    <div class="card"><div class="hd"><h2 class="grow">رمز دوم (2FA)</h2><${Pill} s=${me.totp_on ? "ok" : "warn"} label=${me.totp_on ? "فعال" : "غیرفعال"} /></div>
      <div class="bd">${redo ? html`<${Setup2FA} me=${me} embedded onDone=${(m) => { setRedo(false); onMe(m); }} />`
        : html`<p class="muted small" style="margin-top:0">گوشی را عوض کرده‌اید؟ رمز دوم را دوباره تنظیم کنید. اگر گوشی را گم کرده‌اید
          مدیر دیگری از «کاربران» یا روی سرور با <code>icsd-hub reset-2fa</code> آن را بازنشانی می‌کند.</p>
          <button onClick=${() => setRedo(true)}>تنظیم دوباره</button>`}</div></div>
  </div>`;
}

function Notify() {
  const [f, setF] = useState(null);
  const [busy, run] = useAction();
  useEffect(() => { api("/settings/notify").then(setF).catch(() => {}); }, []);
  if (!f) return html`<div class="skeleton">…</div>`;
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const saveAll = () => run(() => api("/settings/notify", { method: "PUT", body: f }), "ذخیره شد").then(setF);
  const test = () => run(() => api("/settings/notify", { method: "PUT", body: f })
    .then(setF).then(() => api("/settings/notify/test", { method: "POST" })))
    .then((r) => r.results.forEach((x) => toast(x.ok ? `${x.channel}: ارسال شد` : `${x.channel}: ${x.error}`, !x.ok)))
    .catch(() => {});
  // A plain function, not a component: an inline component would remount
  // on every keystroke and the input would lose focus.
  const channel = (k, title, help) => html`<div class="card"><div class="hd"><h2 class="grow">${title}</h2>
      <label class="check"><input type="checkbox" checked=${f[k + "_on"]} onChange=${set(k + "_on")} />فعال</label></div>
    <div class="bd form">
      <div class="note">${help}</div>
      <label class="field">توکن ربات
        <input class="ltr" type="password" autocomplete="off" value=${f[k + "_token"]} onInput=${set(k + "_token")}
          placeholder=${f[k + "_token_set"] ? "ذخیره شده — برای تغییر، توکن تازه وارد کنید" : "123456:ABC…"} /></label>
      <label class="field">شناسهٔ چت (chat id)<input class="ltr" value=${f[k + "_chat"]} onInput=${set(k + "_chat")} /></label>
      <label class="field">آدرس API (فقط اگر از پراکسی استفاده می‌کنید)<input class="ltr" value=${f[k + "_api"]} onInput=${set(k + "_api")} /></label>
    </div></div>`;
  return html`<div class="stack">
    <div class="grid two">
      ${channel("bale", "بله", html`در بله با <b>@botfather</b> ربات بسازید، توکن را این‌جا بگذارید، به ربات پیام بدهید
        و شناسهٔ چت را از <code class="ltr">tapi.bale.ai/bot‹توکن›/getUpdates</code> بردارید. از داخل ایران در دسترس است.`)}
      ${channel("telegram", "تلگرام", html`با <b>@BotFather</b> ربات بسازید. اگر پنل روی سرور داخل ایران است، تلگرام
        در دسترس نیست؛ یا بله را فعال کنید یا آدرس یک پراکسی Bot API بدهید.`)}
    </div>
    <div class="row"><button class="primary" disabled=${busy} onClick=${saveAll}>ذخیره</button>
      <button disabled=${busy} onClick=${test}>ذخیره و ارسال پیام آزمایشی</button></div>
  </div>`;
}

function Users({ me }) {
  const [d, , reload, setD] = usePoll("/users", 0);
  const [f, setF] = useState({ username: "", password: "", role: "viewer" });
  const [busy, run] = useAction();
  const [confirm, setConfirm] = useState(null);
  if (!d) return html`<div class="skeleton">…</div>`;
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const add = (e) => {
    e.preventDefault();
    run(() => api("/users", { method: "POST", body: f }), "کاربر ساخته شد")
      .then((r) => { setD(r); setF({ username: "", password: "", role: "viewer" }); }).catch(() => {});
  };
  return html`<div class="grid two">
    <div class="card"><div class="hd"><h2>کاربران</h2></div><div class="bd flush"><table>
      <thead><tr><th>نام</th><th>نقش</th><th>2FA</th><th>آخرین ورود</th><th></th></tr></thead>
      <tbody>${d.users.map((u) => html`<tr>
        <td class="ltr" style="text-align:right"><b>${u.username}</b></td>
        <td>${u.role === "admin" ? "مدیر" : "فقط مشاهده"}</td>
        <td><${Pill} s=${u.totp_on ? "ok" : "warn"} label=${u.totp_on ? "فعال" : "خاموش"} /></td>
        <td class="small faint">${dt(u.last_login)}</td>
        <td class="nowrap">${u.username !== me.username && html`
          ${u.totp_on ? html`<button class="ghost sm" onClick=${() => setConfirm({ u, act: "2fa" })}>بازنشانی 2FA</button>` : null}
          <button class="ghost sm danger" onClick=${() => setConfirm({ u, act: "del" })}><${Icon} name="trash" /></button>`}</td>
      </tr>`)}</tbody></table></div></div>
    <div class="card"><div class="hd"><h2>کاربر تازه</h2></div><form class="bd form" onSubmit=${add}>
      <label class="field">نام کاربری<input class="ltr" required value=${f.username} onInput=${set("username")} /></label>
      <label class="field">رمز اولیه (دست‌کم ۱۰ نویسه)<input class="ltr" type="password" autocomplete="new-password" required minlength="10" value=${f.password} onInput=${set("password")} /></label>
      <label class="field">نقش<select value=${f.role} onChange=${set("role")}>
        <option value="viewer">فقط مشاهده</option><option value="admin">مدیر</option></select></label>
      <div><button class="primary" disabled=${busy}>ساخت</button></div>
    </form></div>
    ${confirm && html`<${Dialog} title=${confirm.act === "del" ? "حذف کاربر" : "بازنشانی رمز دوم"} onClose=${() => setConfirm(null)}
      footer=${html`<button onClick=${() => setConfirm(null)}>انصراف</button>
        <button class="danger" disabled=${busy} onClick=${() => run(() => api(confirm.act === "del" ? "/users/" + confirm.u.id
          : `/users/${confirm.u.id}/reset-2fa`, { method: confirm.act === "del" ? "DELETE" : "POST" }), "انجام شد")
          .then((r) => { setD(r); setConfirm(null); }).catch(() => {})}>تأیید</button>`}>
      <p style="margin:0">${confirm.act === "del" ? `کاربر «${confirm.u.username}» حذف شود؟`
        : `رمز دوم «${confirm.u.username}» خاموش و همهٔ نشست‌هایش بسته می‌شود؛ در ورود بعدی باید دوباره تنظیمش کند.`}</p><//>`}
  </div>`;
}

function Audit() {
  const [d] = usePoll("/audit", 0);
  if (!d) return html`<div class="skeleton">…</div>`;
  const label = { login: "ورود", "login.fail": "ورود ناموفق", "server.add": "افزودن سرور", "server.delete": "حذف سرور",
    "server.token": "دستور نصب تازه", "agent.enroll": "نصب agent", "2fa.enable": "فعال‌سازی 2FA", "password.change": "تغییر رمز",
    "user.add": "افزودن کاربر", "user.delete": "حذف کاربر", "user.reset_2fa": "بازنشانی 2FA", "rule.edit": "ویرایش قاعده",
    "check.add": "افزودن آدرس", "check.delete": "حذف آدرس", "notify.edit": "ویرایش اعلان‌ها" };
  return html`<div class="card"><div class="tbl-wrap"><table>
    <thead><tr><th>زمان</th><th>کاربر</th><th>کار</th><th>جزئیات</th><th>IP</th></tr></thead>
    <tbody>${d.audit.map((a) => html`<tr><td class="small nowrap">${dt(a.ts)}</td><td class="ltr">${a.username || "—"}</td>
      <td>${label[a.action] || a.action}</td><td class="small faint">${a.detail}</td><td class="ltr small faint">${a.ip}</td></tr>`)}</tbody>
  </table></div></div>`;
}
