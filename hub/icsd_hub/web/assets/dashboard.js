// Dashboard: every server at a glance, open alerts, URL checks.
import { html, useState } from "./vendor/preact-htm.js";
import {
  Command, Dialog, Empty, Icon, Meter, Pill, Spark, ago, api, bytes, dt, dur, fa, pct, rate, usePoll,
} from "./lib.js";

export function ServerCard({ s }) {
  const host = s.host || {};
  const d = s.disk;
  return html`<a class="card srv" href=${"#/server/" + s.id}>
    <div class="top">
      <div class="grow">
        <div class="name">${s.name}</div>
        <div class="meta ltr">${host.hostname || "—"}${s.ip ? " · " + s.ip : ""}</div>
      </div>
      ${s.alerts > 0 && html`<${Pill} s="crit" label=${fa(s.alerts) + " هشدار"} />`}
      <${Pill} s=${s.status} />
    </div>
    ${s.status === "pending" ? html`<div class="note">agent هنوز نصب نشده است. دستور نصب را در صفحهٔ سرور ببینید.</div>`
      : html`
      <div class="meters">
        <${Meter} label="پردازنده" v=${s.cpu} />
        <${Meter} label="حافظه" v=${s.mem} sub=${s.mem_used ? bytes(s.mem_used) + " از " + bytes(s.mem_total) : ""} />
        <${Meter} label="دیسک" v=${d && d.pct} warn=${80} crit=${90}
          sub=${d ? `${d.mount}: ${bytes(d.free)} آزاد` : ""} />
      </div>
      <${Spark} data=${s.spark} />
      <div class="foot">
        ${s.status === "online"
          ? html`<span title="بار ۱ دقیقه">بار ${fa(s.load && s.load[0], 2)}</span>
              <span class="ltr" title="شبکه">↓ ${rate(s.rx)} ↑ ${rate(s.tx)}</span>
              ${s.containers && html`<span>کانتینر ${fa(s.containers.running)}/${fa(s.containers.total)}</span>`}
              <span>روشن ${dur(s.uptime)}</span>`
          : html`<span>آخرین گزارش: ${ago(s.last_seen)}</span>`}
      </div>`}
  </a>`;
}

export function AddServer({ onClose, onAdded }) {
  const [name, setName] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const r = await api("/servers", { method: "POST", body: { name } });
      setRes(r);
      onAdded && onAdded();
    } catch (ex) { setErr(ex.message); } finally { setBusy(false); }
  };
  return html`<${Dialog} title="افزودن سرور" onClose=${onClose}
    footer=${res ? html`<button class="primary" onClick=${onClose}>تمام</button>` : null}>
    ${!res ? html`<form class="form" onSubmit=${submit}>
        <label class="field">نام سرور (برای خودتان)
          <input autofocus required maxlength="60" placeholder="مثلاً فرش‌پلاس یا VPS ایران" value=${name}
            onInput=${(e) => setName(e.target.value)} /></label>
        ${err && html`<div class="error">${err}</div>`}
        <button class="primary" disabled=${busy}>ساخت دستور نصب</button>
      </form>`
    : html`<${InstallSteps} command=${res.command} expires=${res.expires} />`}
  <//>`;
}

export const InstallSteps = ({ command, expires }) => html`<div class="stack">
  <ol class="steps">
    <li>با SSH و کاربر root (یا با sudo) وارد سرور شوید.</li>
    <li>این دستور را اجرا کنید:</li>
  </ol>
  <${Command} text=${command} />
  <div class="note">
    این توکن یک‌بارمصرف است و تا ${dt(expires)} اعتبار دارد. agent هیچ پورتی باز نمی‌کند، چیزی نصب نمی‌کند
    و فقط می‌خواند؛ چند ثانیه بعد از اجرا، سرور در داشبورد «آنلاین» می‌شود.
  </div>
</div>`;

export function AlertList({ items, compact, empty }) {
  if (!items || !items.length) return html`<${Empty} title=${empty || "همه‌چیز آرام است"}>${empty ? "" : "هشدار بازی وجود ندارد."}<//>`;
  return items.map((e) => html`<div class="alert-item">
    <${Pill} s=${e.ended ? "ok" : e.severity} label=${e.ended ? "رفع شد" : e.severity === "critical" ? "بحرانی" : "هشدار"} />
    <div class="grow">
      <div><b>${e.server_name || (e.server_id ? e.server_id : "پایش آدرس")}</b>
        <span class="faint small"> · ${dt(e.started)}${e.ended ? " تا " + dt(e.ended) : " · " + ago(e.started)}</span></div>
      <div class="msg">${compact ? e.message.split("\n")[0] : e.message}</div>
    </div></div>`);
}

export function CheckRows({ checks }) {
  return checks.map((c) => html`<tr>
    <td><${Pill} s=${!c.enabled ? "idle" : c.ok === undefined || c.ok === null ? "pending" : c.ok ? "ok" : "crit"}
      label=${!c.enabled ? "خاموش" : c.ok == null ? "در انتظار" : c.ok ? "برقرار" : "قطع"} /></td>
    <td><b>${c.name}</b><div class="faint small ltr">${c.url}</div></td>
    <td class="num">${c.ms != null ? fa(c.ms) + " ms" : "—"}</td>
    <td class="num">${c.uptime_24h != null ? pct(c.uptime_24h, 2) : "—"}</td>
    <td class="num">${c.cert_days != null ? fa(Math.floor(c.cert_days)) + " روز" : "—"}</td>
  </tr>`);
}

export function Dashboard({ me }) {
  const [data, error, reload] = usePoll("/overview", 5000);
  const [adding, setAdding] = useState(false);
  if (error && !data) return html`<div class="error">${error.message}</div>`;
  if (!data) return html`<div class="skeleton">در حال بارگذاری…</div>`;
  const sv = data.servers;
  const online = sv.filter((s) => s.status === "online").length;
  const checksUp = data.checks.filter((c) => c.enabled && c.ok).length;
  const checksOn = data.checks.filter((c) => c.enabled).length;
  return html`
    <div class="page-head"><h1 class="grow">داشبورد</h1>
      ${me.role === "admin" && html`<button class="primary" onClick=${() => setAdding(true)}><${Icon} name="plus" />افزودن سرور</button>`}
    </div>
    <div class="stats">
      <div class="stat"><div class="k">سرورهای آنلاین</div><div class="v">${fa(online)} <small>از ${fa(sv.length)}</small></div></div>
      <div class="stat"><div class="k">هشدارهای باز</div><div class="v" style=${data.alerts.length ? "color:var(--crit)" : ""}>${fa(data.alerts.length)}</div></div>
      <div class="stat"><div class="k">آدرس‌های برقرار</div><div class="v">${fa(checksUp)} <small>از ${fa(checksOn)}</small></div></div>
      <div class="stat"><div class="k">کانتینرها</div><div class="v">${fa(sv.reduce((a, s) => a + (s.containers ? s.containers.running : 0), 0))}
        <small>در حال اجرا</small></div></div>
    </div>
    ${sv.length === 0 ? html`<div class="card"><${Empty} title="هنوز سروری اضافه نشده">
        ${me.role === "admin" ? html`با «افزودن سرور» یک دستور نصب بگیرید و روی سرور اجرا کنید.` : "مدیر باید سرورها را اضافه کند."}<//></div>`
      : html`<div class="grid servers">${sv.map((s) => html`<${ServerCard} key=${s.id} s=${s} />`)}</div>`}
    <div class="grid two" style="margin-top:16px">
      <div class="card"><div class="hd"><h2 class="grow">هشدارهای باز</h2><a href="#/alerts" class="small">همه</a></div>
        <div class="bd flush"><${AlertList} items=${data.alerts.slice(0, 6)} compact /></div></div>
      <div class="card"><div class="hd"><h2 class="grow">پایش آدرس‌ها</h2><a href="#/checks" class="small">مدیریت</a></div>
        <div class="bd flush">${data.checks.length
          ? html`<div class="tbl-wrap"><table><thead><tr><th>وضعیت</th><th>آدرس</th><th class="num">پاسخ</th>
              <th class="num">در دسترس ۲۴ساعت</th><th class="num">SSL</th></tr></thead>
              <tbody><${CheckRows} checks=${data.checks} /></tbody></table></div>`
          : html`<${Empty} title="هیچ آدرسی پایش نمی‌شود">مثلاً https://farshplus.ir را اضافه کنید تا خطای ۵۰۰ را زودتر از کاربران ببینید.<//>`}
        </div></div>
    </div>
    ${adding && html`<${AddServer} onClose=${() => setAdding(false)} onAdded=${reload} />`}`;
}
