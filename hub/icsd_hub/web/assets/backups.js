// Backups (phase 3) and domains (phase 4): pages across all servers.
import { html, useEffect, useState } from "./vendor/preact-htm.js";
import { CertPill } from "./manage.js";
import { Dialog, Empty, Icon, Pill, ago, api, bytes, dt, dur, fa, useAction, usePoll } from "./lib.js";

const enc = encodeURIComponent;
const everyLabel = (h) => (h % 24 === 0 ? (h === 24 ? "روزانه" : `هر ${fa(h / 24)} روز`) : `هر ${fa(h)} ساعت`);

export function BackupsPage({ me }) {
  const [d, error, reload] = usePoll("/backups", 10000);
  const [ov] = usePoll("/overview", 0);
  const [form, setForm] = useState(null);
  const [busy, run] = useAction();
  if (error && !d) return html`<div class="error">${error.message}</div>`;
  if (!d) return html`<div class="skeleton">…</div>`;
  const servers = ov ? ov.servers.filter((s) => s.status !== "pending") : [];
  const runNow = (p) => run(() => api(`/backups/plans/${p.id}/run`, { method: "POST" }), "شروع شد").then(reload).catch(() => {});
  return html`<div class="page-head"><h1 class="grow">بک‌آپ‌ها</h1>
      <button class="primary" onClick=${() => setForm({})}><${Icon} name="plus" />برنامهٔ بک‌آپ</button></div>
    <p class="muted small" style="margin-top:-8px">هر برنامه روی سرور خودش بک‌آپ می‌گیرد (با بررسی فضای آزاد)، چند نسخهٔ آخر را نگه می‌دارد
      و در صورت تعیین مقصد، هر نسخه را به سرور دیگری کپی می‌کند. شکست هر بک‌آپ هشدار می‌دهد.</p>
    <div class="card"><div class="hd"><h2>برنامه‌ها</h2></div>
      ${!d.plans.length ? html`<${Empty} title="هنوز برنامه‌ای نیست">مثلاً دیتابیس فرش‌پلاس هر شب ساعت ۳، با کپی روی سرور دیگر.<//>` : html`<div class="tbl-wrap"><table>
        <thead><tr><th>نام</th><th>سرور</th><th>چه چیزی</th><th>زمان‌بندی</th><th>نگه‌داری</th><th>کپی به</th><th>آخرین اجرا</th><th></th></tr></thead>
        <tbody>${d.plans.map((p) => html`<tr class=${p.enabled ? "" : "faint"}>
          <td><b>${p.name}</b></td><td>${p.server_name}</td>
          <td class="ltr small" style="text-align:right">${p.kind === "db" ? `${p.target.db.name} (${p.target.db.engine})` : p.target.path}</td>
          <td class="small">${everyLabel(p.every_hours)} از ساعت ${fa(p.start_hour)}<div class="faint">بعدی: ${p.enabled ? dt(p.next_run) : "خاموش"}</div></td>
          <td class="small">${fa(p.keep)} نسخه</td>
          <td class="small">${p.dest_name ? html`${p.dest_name} (${fa(p.dest_keep)})${p.delete_local ? html`<div class="faint">نسخهٔ محلی حذف می‌شود</div>` : null}` : "—"}</td>
          <td>${p.running ? html`<${Pill} s="pending" label="در حال اجرا" />` : p.last ? html`<${Pill} s=${p.last.state === "done" ? "ok" : "crit"}
            label=${p.last.state === "done" ? "موفق" : "ناموفق"} /><div class="faint small">${ago(p.last.started)}</div>` : html`<span class="faint small">هنوز نه</span>`}</td>
          <td class="nowrap"><button class="sm" disabled=${busy || p.running} onClick=${() => runNow(p)}><${Icon} name="play" />اجرا</button>
            <button class="ghost sm" onClick=${() => setForm(p)}><${Icon} name="edit" /></button></td></tr>`)}</tbody></table></div>`}
    </div>
    <div class="card" style="margin-top:16px"><div class="hd"><h2>اجراهای اخیر</h2></div>
      ${!d.runs.length ? html`<${Empty} title="هنوز اجرایی نیست" />` : html`<div class="tbl-wrap"><table>
        <thead><tr><th>برنامه</th><th>شروع</th><th>مدت</th><th class="num">حجم</th><th>وضعیت</th><th></th></tr></thead>
        <tbody>${d.runs.map((r) => html`<tr>
          <td>${r.plan_name || "—"}<div class="faint small">${r.server_name}${r.trigger === "manual" ? " · دستی" : ""}</div></td>
          <td class="small nowrap">${dt(r.started)}</td><td class="small">${r.ended ? dur(r.ended - r.started) : "…"}</td>
          <td class="num">${r.size ? bytes(r.size) : "—"}</td>
          <td>${r.state === "running" ? html`<${Pill} s="pending" label="در حال اجرا" />` : r.state === "done"
            ? html`<${Pill} s="ok" label="موفق" />${r.dest_name ? html`<div class="faint small">کپی روی ${r.dest_name}</div>` : null}`
            : html`<${Pill} s="crit" label="ناموفق" /><div class="error small" style="padding:2px 6px;margin-top:4px">${r.error}</div>`}</td>
          <td class="nowrap">${r.state === "done" && r.path && !r.dest_path && html`<a class="btn ghost sm" title="دانلود"
              href=${`/api/servers/${r.server_id}/fs/download?path=${enc(r.path)}`}><${Icon} name="down" /></a>`}
            ${r.state === "done" && r.dest_path && html`<a class="btn ghost sm" title="دانلود از سرور مقصد"
              href=${`/api/servers/${r.dest_server}/fs/download?path=${enc(r.dest_path)}`}><${Icon} name="down" /></a>`}</td></tr>`)}</tbody></table></div>`}
    </div>
    ${form && html`<${PlanForm} plan=${form.id ? form : null} servers=${servers} onClose=${() => setForm(null)}
      onSaved=${() => { setForm(null); reload(); }} />`}`;
}

function PlanForm({ plan, servers, onClose, onSaved }) {
  const [f, setF] = useState(plan ? {
    server_id: plan.server_id, name: plan.name, kind: plan.kind, db: plan.target.db || null, path: plan.target.path || "",
    exclude: (plan.target.exclude || []).join("\n"), every_hours: plan.every_hours, start_hour: plan.start_hour, keep: plan.keep,
    dest_server: plan.dest_server || "", dest_keep: plan.dest_keep, delete_local: plan.delete_local, enabled: plan.enabled,
  } : { server_id: servers.length ? servers[0].id : "", name: "", kind: "db", db: null, path: "", exclude: "",
    every_hours: 24, start_hour: 3, keep: 7, dest_server: "", dest_keep: 14, delete_local: false, enabled: true });
  const [dbs, setDbs] = useState(null);
  const [dbErr, setDbErr] = useState("");
  const [busy, run] = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  useEffect(() => {
    if (f.kind !== "db" || !f.server_id) return;
    setDbs(null); setDbErr("");
    api(`/servers/${f.server_id}/databases`).then((r) => setDbs(r.databases || [])).catch((e) => setDbErr(e.message));
  }, [f.kind, f.server_id]);
  const key = (d) => d && `${d.engine}|${d.where}|${d.name}`;
  const body = () => ({ ...f, every_hours: +f.every_hours, start_hour: +f.start_hour, keep: +f.keep, dest_keep: +f.dest_keep,
    dest_server: f.dest_server || null, exclude: f.exclude.split("\n").map((s) => s.trim()).filter(Boolean),
    name: f.name || (f.kind === "db" && f.db ? "دیتابیس " + f.db.name : f.path) });
  const save = (e) => {
    e.preventDefault();
    run(() => api(plan ? `/backups/plans/${plan.id}` : "/backups/plans", { method: plan ? "PUT" : "POST", body: body() }), "ذخیره شد").then(onSaved).catch(() => {});
  };
  const del = () => run(() => api(`/backups/plans/${plan.id}`, { method: "DELETE" }), "حذف شد").then(onSaved).catch(() => {});
  return html`<${Dialog} title=${plan ? "ویرایش برنامهٔ بک‌آپ" : "برنامهٔ بک‌آپ تازه"} onClose=${onClose}>
    <form class="form" onSubmit=${save}>
      <div class="two">
        <label class="field">سرور<select value=${f.server_id} onChange=${set("server_id")}>${servers.map((s) => html`<option value=${s.id}>${s.name}</option>`)}</select></label>
        <label class="field">نوع<select value=${f.kind} onChange=${set("kind")}><option value="db">دیتابیس</option><option value="files">پوشه (فایل‌ها)</option></select></label>
      </div>
      ${f.kind === "db" ? html`<label class="field">دیتابیس
          ${dbErr ? html`<div class="error">${dbErr}</div>` : !dbs ? html`<div class="faint small">در حال خواندن فهرست…</div>`
          : html`<select value=${key(f.db)} onChange=${(e) => setF({ ...f, db: dbs.find((d) => key(d) === e.target.value) || null })}>
              <option value="">— انتخاب کنید —</option>
              ${dbs.map((d) => html`<option value=${key(d)}>${d.name} — ${d.engine}، ${d.where === "native" ? "روی سرور" : d.where} (${bytes(d.size)})</option>`)}</select>`}</label>`
      : html`<label class="field">مسیر پوشه<input class="ltr" placeholder="/opt/farshplus/media" value=${f.path} onInput=${set("path")} /></label>
        <label class="field">مستثناها (هر خط یک مسیر؛ نسبی یا مطلق)<textarea class="ltr" rows="2" placeholder="cache" value=${f.exclude} onInput=${set("exclude")}></textarea></label>`}
      <label class="field">نام (اختیاری)<input value=${f.name} onInput=${set("name")} /></label>
      <div class="two">
        <label class="field">هر چند ساعت (۲۴ = روزانه)<input type="number" min="1" value=${f.every_hours} onInput=${set("every_hours")} /></label>
        <label class="field">از ساعت (به وقت تهران)<input type="number" min="0" max="23" value=${f.start_hour} onInput=${set("start_hour")} /></label>
      </div>
      <label class="field">چند نسخهٔ آخر روی همین سرور بماند<input type="number" min="1" value=${f.keep} onInput=${set("keep")} /></label>
      <div class="two">
        <label class="field">کپی به سرور دیگر<select value=${f.dest_server} onChange=${set("dest_server")}><option value="">— نه —</option>
          ${servers.filter((s) => s.id !== f.server_id).map((s) => html`<option value=${s.id}>${s.name}</option>`)}</select></label>
        ${f.dest_server && html`<label class="field">نسخه‌های نگه‌داشته روی مقصد<input type="number" min="1" value=${f.dest_keep} onInput=${set("dest_keep")} /></label>`}
      </div>
      ${f.dest_server && html`<label class="check"><input type="checkbox" checked=${f.delete_local} onChange=${set("delete_local")} />
        پس از کپی موفق، نسخهٔ روی خود سرور حذف شود (برای دیسک‌های کوچک)</label>`}
      <label class="check"><input type="checkbox" checked=${f.enabled} onChange=${set("enabled")} />فعال</label>
      <div class="row"><button class="primary" disabled=${busy}>ذخیره</button><span class="grow"></span>
        ${plan && html`<button type="button" class="danger" disabled=${busy} onClick=${del}><${Icon} name="trash" />حذف برنامه</button>`}</div>
    </form><//>`;
}

// ── domains ──────────────────────────────────────────────────────────
const DNS_FA = { ok: ["ok", "درست"], elsewhere: ["warn", "جای دیگر / CDN"], none: ["crit", "رکورد ندارد"] };

export function DomainsPage() {
  const [refresh, setRefresh] = useState(0);
  const [d, error] = usePoll(`/domains${refresh ? "?refresh=1" : ""}`, 0, [refresh]);
  if (error && !d) return html`<div class="error">${error.message}</div>`;
  return html`<div class="page-head"><h1 class="grow">دامنه‌ها و SSL</h1>
      <button onClick=${() => setRefresh(refresh + 1)}><${Icon} name="refresh" />بررسی دوباره</button></div>
    <p class="muted small" style="margin-top:-8px">دامنه‌ها از سایت‌های nginx و گواهی‌های هر سرور خوانده می‌شوند. بررسی می‌شود که رکورد DNS هر دامنه
      به همان سروری اشاره کند که آن را سرو می‌کند، و گواهی کی منقضی می‌شود (هشدار ۱۴ روز مانده).</p>
    ${!d ? html`<div class="skeleton">در حال بررسی…</div>` : html`
    <div class="card"><div class="tbl-wrap"><table>
      <thead><tr><th>دامنه</th><th>سرور</th><th>DNS</th><th>گواهی</th><th>صادرکننده</th></tr></thead>
      <tbody>${d.domains.map((r) => html`<tr>
        <td class="ltr" style="text-align:right"><a href=${"https://" + r.domain} target="_blank" rel="noopener">${r.domain}</a></td>
        <td><a href=${"#/server/" + r.server_id}>${r.server}</a></td>
        <td><${Pill} s=${DNS_FA[r.dns_state][0]} label=${DNS_FA[r.dns_state][1]} />
          <div class="faint small ltr">${[...r.dns.a, ...r.dns.aaaa].join(", ")}</div></td>
        <td>${r.cert ? html`<${CertPill} ts=${r.cert.not_after} />` : r.ssl ? html`<${Pill} s="idle" label="نامشخص" />` : html`<${Pill} s="warn" label="HTTPS ندارد" />`}</td>
        <td class="small faint">${r.cert ? r.cert.issuer : ""}</td></tr>`)}</tbody></table>
      ${!d.domains.length && html`<${Empty} title="دامنه‌ای پیدا نشد">مدیریت (agent با --upgrade) باید روی سرورها فعال باشد.<//>`}</div></div>
    <div class="card" style="margin-top:16px"><div class="hd"><h2>رکوردهای ایمیل</h2>
      <span class="faint small">سرور ایمیل روی این سرورها اجرا نمی‌شود؛ فقط درستی رکوردها بررسی می‌شود.</span></div>
      <div class="tbl-wrap"><table><thead><tr><th>دامنه</th><th>MX</th><th>SPF</th><th>DMARC</th></tr></thead>
        <tbody>${Object.entries(d.mail).map(([b, m]) => html`<tr>
          <td class="ltr" style="text-align:right"><b>${b}</b></td>
          <td>${m.mx.length ? html`<div class="ltr small">${m.mx.join(" · ")}</div>` : html`<${Pill} s="idle" label="ندارد" />`}</td>
          <td>${m.spf ? html`<${Pill} s=${m.spf_multiple ? "warn" : "ok"} label=${m.spf_multiple ? "چند رکورد!" : "دارد"} /><div class="ltr small faint">${m.spf}</div>`
            : html`<${Pill} s=${m.mx.length ? "warn" : "idle"} label="ندارد" />`}</td>
          <td>${m.dmarc ? html`<${Pill} s="ok" label="دارد" /><div class="ltr small faint">${m.dmarc}</div>`
            : html`<${Pill} s=${m.mx.length ? "warn" : "idle"} label="ندارد" />`}</td></tr>`)}</tbody></table></div></div>`}`;
}
