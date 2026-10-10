// One server: charts, disks, containers, processes, events, settings.
import { html, useState } from "./vendor/preact-htm.js";
import { Chart } from "./chart.js";
import { AlertList, InstallSteps } from "./dashboard.js";
import { ConsoleTab, ContainerActions, DatabasesTab, FilesTab, MonitorOnlyNote, SSLTab, ServicesTab } from "./manage.js";
import {
  Bar, Dialog, Empty, Icon, Pill, ago, api, bytes, dt, dur, fa, load, pct, rate, save, useAction, usePoll,
} from "./lib.js";

const RANGES = [["1h", "۱ ساعت"], ["6h", "۶ ساعت"], ["24h", "۲۴ ساعت"], ["7d", "۷ روز"], ["30d", "۳۰ روز"], ["1y", "۱ سال"]];
const P = (v) => fa(v) + "٪";
const R = (v) => (v >= 1048576 ? fa(v / 1048576, 1) + "M" : v >= 1024 ? fa(v / 1024, 0) + "K" : fa(v, 0)) + "B/s";

export function ServerPage({ id, me, theme }) {
  const [range, setRange] = useState(load("icsd.range") || "1h");
  const [d, error, reload] = usePoll("/servers/" + id, 5000, [id]);
  const [series] = usePoll(`/servers/${id}/series?range=${range}`, range === "1h" ? 10000 : 60000, [id, range]);
  const [tab, setTab] = useState("charts");
  if (error && !d) return html`<div class="error">${error.message}</div>`;
  if (!d) return html`<div class="skeleton">در حال بارگذاری…</div>`;
  const s = d.server, h = s.host || {}, last = d.last || {};
  const pick = (r) => { setRange(r); save("icsd.range", r); };
  const manage = me.role === "admin" && d.caps && d.caps.mode === "manage";
  const outdated = d.agent_latest && s.agent_version && s.agent_version !== d.agent_latest;

  return html`
    <div class="page-head">
      <a href="#/" class="btn ghost sm" title="بازگشت"><${Icon} name="back" /></a>
      <div class="grow"><h1>${s.name} <${Pill} s=${s.status} /></h1>
        <div class="sub ltr" style="text-align:right">${[h.hostname, h.os, s.ip].filter(Boolean).join(" · ")}</div></div>
      <div class="seg">${RANGES.map(([k, l]) => html`<button class=${range === k ? "on" : ""} onClick=${() => pick(k)}>${l}</button>`)}</div>
    </div>

    ${s.status === "pending" && html`<div class="card bd" style="margin-bottom:16px"><${Pending} id=${id} me=${me} /></div>`}
    ${outdated && html`<div class="note" style="margin-bottom:16px">نسخهٔ agent این سرور ${s.agent_version} است و نسخهٔ
      ${d.agent_latest} آماده است. روی سرور اجرا کنید:
      <code class="ltr" style="display:block;margin-top:6px">curl -fsSL ${location.origin}/install.sh | sudo bash -s -- --upgrade</code></div>`}

    <div class="stats">
      <div class="stat"><div class="k">پردازنده</div><div class="v">${pct(last.cpu && last.cpu.pct)}
        <small>${fa(h.cpus)} هسته</small></div></div>
      <div class="stat"><div class="k">حافظه</div><div class="v">${pct(last.mem && last.mem.pct)}
        <small class="ltr">${last.mem ? bytes(last.mem.used) + " / " + bytes(last.mem.total) : ""}</small></div></div>
      <div class="stat"><div class="k">بار سیستم (۱ / ۵ / ۱۵ دقیقه)</div><div class="v" style="font-size:18px">
        ${last.load ? last.load.map((v) => fa(v, 2)).join(" · ") : "—"}</div></div>
      <div class="stat"><div class="k">شبکه</div><div class="v ltr" style="font-size:16px;text-align:right">
        ↓ ${rate(last.net && last.net.rx_bps)}<br />↑ ${rate(last.net && last.net.tx_bps)}</div></div>
      <div class="stat"><div class="k">روشن از</div><div class="v" style="font-size:16px">${dur(last.uptime)}</div>
        <div class="faint small">آخرین گزارش ${ago(s.last_seen)}</div></div>
    </div>

    <div class="tabs">
      ${tabsFor(d.caps, me).map(([k, l]) =>
        html`<button class=${tab === k ? "on" : ""} onClick=${() => setTab(k)}>${l}${k === "containers" && last.containers
          ? " (" + fa(last.containers.length) + ")" : ""}</button>`)}
    </div>

    ${tab === "charts" && (series ? html`<div class="grid charts">
      <${ChartCard} title="پردازنده (٪)" data=${series} theme=${theme} yMax=${100} fmt=${P} series=${[
        { key: "cpu", label: "مصرف", color: "--c1", fill: true },
        { key: "iowait", label: "انتظار دیسک", color: "--c3" },
        { key: "steal", label: "steal", color: "--c5" }]} />
      <${ChartCard} title="حافظه (٪)" data=${series} theme=${theme} yMax=${100} fmt=${P} series=${[
        { key: "mem", label: "RAM", color: "--c2", fill: true }, { key: "swap", label: "swap", color: "--c4" }]} />
      <${ChartCard} title="شبکه" data=${series} theme=${theme} fmt=${R} series=${[
        { key: "rx", label: "دریافت", color: "--c1", fill: true }, { key: "tx", label: "ارسال", color: "--c2" }]} />
      <${ChartCard} title="دیسک (خواندن/نوشتن)" data=${series} theme=${theme} fmt=${R} series=${[
        { key: "rd", label: "خواندن", color: "--c2" }, { key: "wr", label: "نوشتن", color: "--c3", fill: true }]} />
      <${ChartCard} title="بار سیستم" data=${series} theme=${theme} fmt=${(v) => fa(v, 1)} series=${[
        { key: "load1", label: "بار ۱ دقیقه", color: "--c4", fill: true }]} />
      <${ChartCard} title="پرترین دیسک (٪)" data=${series} theme=${theme} yMax=${100} fmt=${P} series=${[
        { key: "disk", label: "پر", color: "--c3", fill: true }]} />
    </div>` : html`<div class="skeleton">در حال بارگذاری نمودارها…</div>`)}

    ${tab === "disks" && html`<${Disks} disks=${last.disks} />`}
    ${tab === "containers" && html`<${Containers} list=${last.containers} err=${last.docker_err} sid=${id} manage=${manage && d.caps.docker} />`}
    ${tab === "files" && html`<${FilesTab} sid=${id} caps=${d.caps} />`}
    ${tab === "services" && html`<${ServicesTab} sid=${id} />`}
    ${tab === "databases" && html`<${DatabasesTab} sid=${id} />`}
    ${tab === "ssl" && html`<${SSLTab} sid=${id} />`}
    ${tab === "console" && html`<${ConsoleTab} sid=${id} />`}
    ${tab === "manage" && html`<${MonitorOnlyNote} origin=${location.origin} />`}
    ${tab === "procs" && html`<${Procs} list=${last.top} />`}
    ${tab === "events" && html`<div class="card"><div class="bd flush"><${AlertList} items=${d.events} empty="برای این سرور رویدادی ثبت نشده است" /></div></div>`}
    ${tab === "info" && html`<${Info} d=${d} me=${me} onChange=${reload} />`}`;
}

const ChartCard = ({ title, ...p }) => html`<div class="card chart-card">
  <div class="hd"><h3>${title}</h3></div><div class="bd">
  ${p.data.t.length ? html`<${Chart} ...${p} />` : html`<${Empty} title="هنوز داده‌ای نیست">چند دقیقه صبر کنید.<//>`}</div></div>`;

function Disks({ disks }) {
  if (!disks || !disks.length) return html`<div class="card"><${Empty} title="دیسکی گزارش نشده" /></div>`;
  return html`<div class="card"><div class="tbl-wrap"><table>
    <thead><tr><th>مسیر</th><th>دستگاه</th><th>پر شده</th><th class="num">آزاد</th><th class="num">کل</th><th class="num">inode</th></tr></thead>
    <tbody>${disks.map((x) => html`<tr>
      <td class="ltr mono" style="text-align:right">${x.mount}</td>
      <td class="ltr mono faint" style="text-align:right">${x.device} <span class="small">${x.fs}</span></td>
      <td><div class="inline-bar"><${Bar} v=${x.pct} warn=${80} crit=${90} /><b class="nowrap">${pct(x.pct, 1)}</b></div></td>
      <td class="num">${bytes(x.free)}</td><td class="num">${bytes(x.total)}</td><td class="num">${pct(x.inodes_pct)}</td>
    </tr>`)}</tbody></table></div></div>`;
}

function tabsFor(caps, me) {
  const t = [["charts", "نمودارها"], ["disks", "دیسک‌ها"], ["containers", "کانتینرها"], ["procs", "پردازه‌ها"]];
  if (me.role === "admin") {
    if (caps && caps.mode === "manage") {
      t.push(["files", "فایل‌ها"], ["services", "سرویس‌ها"]);
      if (caps.databases) t.push(["databases", "دیتابیس"]);
      if (caps.ssl) t.push(["ssl", "SSL"]);
      if (caps.console) t.push(["console", "کنسول"]);
    } else if (caps && caps.mode === "monitor") {
      t.push(["manage", "مدیریت"]);
    }
  }
  t.push(["events", "رویدادها"], ["info", "مشخصات"]);
  return t;
}

function Containers({ list, err, sid, manage }) {
  if (err) return html`<div class="card bd"><div class="error">${err}</div>
    <p class="muted small">agent به Docker دسترسی ندارد. نصب‌کننده کاربر icsd-agent را به گروه docker اضافه می‌کند؛
    اگر Docker بعداً نصب شده، دستور نصب را با <code>--upgrade</code> دوباره اجرا کنید.</p></div>`;
  if (!list || !list.length) return html`<div class="card"><${Empty} title="کانتینری نیست">Docker روی این سرور نیست یا کانتینری ندارد.<//></div>`;
  const order = { restarting: 0, dead: 0, exited: 2, running: 1 };
  const sorted = [...list].sort((a, b) => (order[a.state] ?? 3) - (order[b.state] ?? 3) || a.name.localeCompare(b.name));
  return html`<div class="card"><div class="tbl-wrap"><table>
    <thead><tr><th>نام</th><th>وضعیت</th><th>image</th><th class="num">CPU</th><th class="num">حافظه</th><th>توضیح</th>${manage && html`<th></th>`}</tr></thead>
    <tbody>${sorted.map((c) => html`<tr>
      <td class="ltr mono" style="text-align:right"><b>${c.name}</b></td>
      <td><${Pill} s=${c.health || c.state} /></td>
      <td class="ltr faint small" style="text-align:right">${c.image}</td>
      <td class="num">${c.state === "running" ? pct(c.cpu, 1) : "—"}</td>
      <td class="num">${c.state === "running" ? bytes(c.mem) : "—"}</td>
      <td class="faint small ltr" style="text-align:right">${c.status}</td>
      ${manage && html`<td><${ContainerActions} sid=${sid} c=${c} manage=${manage} /></td>`}
    </tr>`)}</tbody></table></div></div>`;
}

function Procs({ list }) {
  if (!list || !list.length) return html`<div class="card"><${Empty} title="داده‌ای نیست" /></div>`;
  return html`<div class="card"><div class="tbl-wrap"><table>
    <thead><tr><th>پردازه</th><th>کاربر</th><th class="num">PID</th><th class="num">CPU (یک هسته)</th><th class="num">حافظه</th></tr></thead>
    <tbody>${list.map((p) => html`<tr>
      <td class="ltr mono" style="text-align:right"><b>${p.name}</b></td><td class="ltr faint">${p.user}</td>
      <td class="num">${p.pid}</td><td class="num">${pct(p.cpu, 1)}</td><td class="num">${bytes(p.rss)}</td>
    </tr>`)}</tbody></table></div>
    <div class="faint small" style="padding:8px 14px">پرمصرف‌ترین‌ها از نظر CPU و حافظه. فقط نام کوتاه پردازه فرستاده می‌شود، نه خط فرمان (که ممکن است رمز داشته باشد).</div></div>`;
}

function Pending({ id, me }) {
  const [cmd, setCmd] = useState(null);
  const [busy, run] = useAction();
  if (me.role !== "admin") return html`<div class="muted">agent هنوز روی این سرور نصب نشده است.</div>`;
  return html`<div class="stack"><h2>نصب agent</h2>
    ${cmd ? html`<${InstallSteps} command=${cmd.command} expires=${cmd.expires} />`
      : html`<p class="muted" style="margin:0">برای امنیت، توکن نصب فقط یک بار نشان داده می‌شود. یک دستور تازه بسازید:</p>
        <div><button class="primary" disabled=${busy}
          onClick=${() => run(() => api(`/servers/${id}/enroll`, { method: "POST" })).then(setCmd)}>ساخت دستور نصب</button></div>`}
  </div>`;
}

function Info({ d, me, onChange }) {
  const s = d.server, h = s.host || {};
  const [name, setName] = useState(s.name);
  const [notes, setNotes] = useState(d.notes || "");
  const [confirm, setConfirm] = useState(false);
  const [cmd, setCmd] = useState(null);
  const [busy, run] = useAction();
  const admin = me.role === "admin";
  // [label, value, latin?] — Latin values are isolated so RTL does not reorder them.
  const rows = [
    ["شناسه", s.id, 1], ["hostname", h.hostname, 1], ["سیستم‌عامل", h.os, 1], ["کرنل", h.kernel, 1],
    ["معماری", h.arch, 1], ["پردازنده", h.cpu_model, 1], ["تعداد هسته", fa(h.cpus)], ["حافظه", bytes(h.mem_total)],
    ["مجازی‌ساز", h.virt, 1], ["روشن شده", h.boot_time ? dt(h.boot_time) : "—"], ["نسخهٔ agent", s.agent_version, 1],
    ["IP اتصال", s.ip, 1], ["افزوده شده", dt(d.created)],
  ];
  return html`<div class="grid two">
    <div class="card"><div class="hd"><h2>مشخصات</h2></div><div class="bd flush"><table><tbody>
      ${rows.map(([k, v, latin]) => html`<tr><td class="muted" style="width:130px">${k}</td><td>${latin ? html`<bdi>${v || "—"}</bdi>` : v || "—"}</td></tr>`)}
    </tbody></table></div></div>
    <div class="stack">
      <div class="card"><div class="hd"><h2>تنظیمات</h2></div><div class="bd form">
        <label class="field">نام<input value=${name} disabled=${!admin} maxlength="60" onInput=${(e) => setName(e.target.value)} /></label>
        <label class="field">یادداشت (ارائه‌دهنده، تاریخ تمدید، …)
          <textarea rows="4" disabled=${!admin} value=${notes} onInput=${(e) => setNotes(e.target.value)}></textarea></label>
        ${admin && html`<div><button class="primary" disabled=${busy} onClick=${() =>
          run(() => api("/servers/" + s.id, { method: "PATCH", body: { name, notes } }), "ذخیره شد").then(onChange)}>ذخیره</button></div>`}
      </div></div>
      ${admin && html`<div class="card"><div class="hd"><h2>نصب دوباره / حذف</h2></div><div class="bd stack">
        ${cmd ? html`<${InstallSteps} command=${cmd.command} expires=${cmd.expires} />` : html`
          <p class="muted small" style="margin:0">برای نصب دوبارهٔ agent (مثلاً پس از نصب مجدد سیستم‌عامل) یک دستور تازه بسازید.
          با اجرای آن، هویت قبلی این سرور باطل می‌شود.</p>
          <div class="row">
            <button disabled=${busy} onClick=${() => run(() => api(`/servers/${s.id}/enroll`, { method: "POST" })).then(setCmd)}>
              <${Icon} name="key" />دستور نصب تازه</button>
            <button class="danger" onClick=${() => setConfirm(true)}><${Icon} name="trash" />حذف سرور</button>
          </div>`}
      </div></div>`}
    </div>
    ${confirm && html`<${Dialog} title="حذف سرور" onClose=${() => setConfirm(false)} footer=${html`
      <button onClick=${() => setConfirm(false)}>انصراف</button>
      <button class="danger" disabled=${busy} onClick=${() => run(() => api("/servers/" + s.id, { method: "DELETE" }), "حذف شد")
        .then(() => { location.hash = "#/"; })}>حذف کن</button>`}>
      <p style="margin:0">«${s.name}» و همهٔ تاریخچه‌اش از پنل حذف می‌شود و agent دیگر پذیرفته نمی‌شود.
      روی خود سرور چیزی پاک نمی‌شود؛ برای برداشتن agent روی سرور اجرا کنید:</p>
      <code class="ltr" style="display:block;margin-top:10px">curl -fsSL ${location.origin}/install.sh | sudo bash -s -- --uninstall</code>
    <//>`}
  </div>`;
}

