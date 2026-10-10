// Phases 2–4 on one server: files, services, containers, databases, SSL, console.
import { html, useEffect, useRef, useState } from "./vendor/preact-htm.js";
import {
  Dialog, Empty, Icon, JobBox, LogLines, Pill, api, bytes, dt, fa, toast, useAction, usePoll,
} from "./lib.js";

const enc = encodeURIComponent;
const join = (dir, name) => (dir.endsWith("/") ? dir + name : dir + "/" + name);
const parent = (p) => {
  const i = p.replace(/\/+$/, "").lastIndexOf("/");
  return i <= 0 ? "/" : p.slice(0, i);
};

// ── files ────────────────────────────────────────────────────────────
export function FilesTab({ sid, caps }) {
  const [path, setPath] = useState("/");
  const [data, error, reload] = usePoll(`/servers/${sid}/fs/list?path=${enc(path === "/" ? "" : path)}`, 0, [sid, path]);
  const [sel, setSel] = useState([]);
  const [dlg, setDlg] = useState(null); // {kind, entry?}
  const [busy, run] = useAction();
  const [job, setJob] = useState(null);
  const upRef = useRef(null);
  useEffect(() => setSel([]), [path]);

  const open = (e) => {
    const p = join(data.path, e.name);
    if (e.type === "dir" || e.type === "dirlink" || data.virtual) setPath(data.virtual ? e.name : p);
    else setDlg({ kind: "file", entry: e, path: p });
  };
  const ro = data && (data.readonly || data.virtual);
  const crumbs = path === "/" ? [] : path.split("/").filter(Boolean);
  const toggle = (name) => setSel(sel.includes(name) ? sel.filter((n) => n !== name) : [...sel, name]);
  const selPaths = () => sel.map((n) => join(data.path, n));

  const upload = async (files) => {
    if (!files.length) return;
    const form = new FormData();
    for (const f of files) {
      if (f.size > (caps.max_mb || 512) * 1048576) { toast(`${f.name}: بزرگ‌تر از سقف ${caps.max_mb} مگابایت`, true); return; }
      form.append("files", f);
    }
    await run(() => api(`/servers/${sid}/fs/upload?dir=${enc(data.path)}`, { method: "POST", form }), "آپلود شد").catch(() => {});
    reload();
  };
  const del = () => run(() => api(`/servers/${sid}/fs/delete`, { method: "POST", body: { paths: selPaths() } }), "حذف شد")
    .then(() => { setDlg(null); reload(); }).catch(() => {});
  const archive = (name) => run(() => api(`/servers/${sid}/fs/archive`, { method: "POST",
    body: { paths: selPaths(), dest: join(data.path, name) } })).then((r) => { setDlg(null); setJob({ id: r.job, title: name }); }).catch(() => {});

  return html`<div class="card">
    <div class="hd fm-bar">
      <button class="ghost sm" title="بالا" disabled=${path === "/"} onClick=${() => setPath(data && !data.virtual && caps.files.concat(caps.readonly || []).includes(data.path) ? "/" : parent(path))}><${Icon} name="up" /></button>
      <div class="crumbs ltr grow">
        <a onClick=${() => setPath("/")}>⌂</a>${crumbs.map((c, i) => html`<span>/</span><a onClick=${() => setPath("/" + crumbs.slice(0, i + 1).join("/"))}>${c}</a>`)}
      </div>
      <button class="ghost sm" onClick=${reload} title="تازه‌سازی"><${Icon} name="refresh" /></button>
      ${!ro && html`
        <button class="sm" onClick=${() => setDlg({ kind: "mkdir" })}><${Icon} name="folder" />پوشه</button>
        <button class="sm" onClick=${() => setDlg({ kind: "newfile" })}><${Icon} name="file" />فایل</button>
        <button class="sm" disabled=${busy} onClick=${() => upRef.current.click()}><${Icon} name="upload" />آپلود</button>
        <input type="file" multiple ref=${upRef} class="hidden" onChange=${(e) => { upload([...e.target.files]); e.target.value = ""; }} />`}
    </div>
    ${sel.length > 0 && html`<div class="fm-sel">${fa(sel.length)} مورد انتخاب شده
      <button class="sm" onClick=${() => setDlg({ kind: "archive" })}><${Icon} name="archive" />فشرده‌سازی</button>
      ${!ro && html`<button class="sm danger" onClick=${() => setDlg({ kind: "delete" })}><${Icon} name="trash" />حذف</button>`}
      <button class="ghost sm" onClick=${() => setSel([])}>لغو</button></div>`}
    ${job && html`<div class="bd"><${JobBox} sid=${sid} jobId=${job.id} title=${job.title} onDone=${reload} /></div>`}
    <div class="bd flush">
      ${error && html`<div class="error" style="margin:12px">${error.message}</div>`}
      ${!data && !error && html`<div class="skeleton">…</div>`}
      ${data && html`<div class="tbl-wrap"><table class="fm">
        <thead><tr>${!data.virtual && html`<th style="width:28px"></th>`}<th>نام</th><th class="num">اندازه</th><th>تغییر</th><th>دسترسی</th><th>مالک</th></tr></thead>
        <tbody>${(data.entries || []).map((e) => html`<tr class=${sel.includes(e.name) ? "on" : ""}>
          ${!data.virtual && html`<td><input type="checkbox" checked=${sel.includes(e.name)} onChange=${() => toggle(e.name)} /></td>`}
          <td><a class="fm-name" onClick=${() => open(e)}><${Icon} name=${e.type === "dir" || e.type === "dirlink" ? "folder" : "file"} />
            <span class="ltr">${e.name}</span>${e.link ? html` <span class="faint small ltr">→ ${e.link}</span>` : null}
            ${data.virtual && e.readonly ? html` <span class="faint small">(فقط خواندنی)</span>` : null}</a></td>
          <td class="num">${e.type === "file" ? bytes(e.size) : ""}</td>
          <td class="small faint nowrap">${dt(e.mtime)}</td>
          <td class="mono small">${e.mode || ""}</td><td class="small">${e.owner || ""}</td>
        </tr>`)}</tbody></table>
        ${!data.entries || !data.entries.length ? html`<${Empty} title="خالی است" />` : null}
        ${data.truncated && html`<div class="note" style="margin:10px">فقط ۵۰۰۰ مورد اول نشان داده شد.</div>`}</div>`}
    </div>
    ${dlg && dlg.kind === "file" && html`<${FileDialog} sid=${sid} entry=${dlg.entry} path=${dlg.path} readonly=${ro}
        onClose=${() => setDlg(null)} onChanged=${() => { setDlg(null); reload(); }} onJob=${(j) => { setDlg(null); setJob(j); }} />`}
    ${dlg && dlg.kind === "editor" && html`<${Editor} sid=${sid} path=${dlg.path} isNew=${dlg.isNew} onClose=${() => { setDlg(null); reload(); }} />`}
    ${dlg && (dlg.kind === "mkdir" || dlg.kind === "newfile" || dlg.kind === "archive") && html`<${NameDialog}
        title=${dlg.kind === "mkdir" ? "پوشهٔ تازه" : dlg.kind === "newfile" ? "فایل تازه" : "نام فایل فشرده (tar.gz)"}
        initial=${dlg.kind === "archive" ? (sel.length === 1 ? sel[0] : "archive") + ".tar.gz" : ""}
        onClose=${() => setDlg(null)} busy=${busy}
        onOk=${(name) => dlg.kind === "mkdir"
          ? run(() => api(`/servers/${sid}/fs/mkdir`, { method: "POST", body: { path: join(data.path, name) } }), "ساخته شد").then(() => { setDlg(null); reload(); }).catch(() => {})
          : dlg.kind === "newfile" ? setDlg({ kind: "editor", path: join(data.path, name), isNew: true })
          : archive(name)} />`}
    ${dlg && dlg.kind === "delete" && html`<${Dialog} title="حذف" onClose=${() => setDlg(null)} footer=${html`
        <button onClick=${() => setDlg(null)}>انصراف</button><button class="danger" disabled=${busy} onClick=${del}>حذف کن</button>`}>
      <p style="margin:0">این موارد برای همیشه حذف می‌شوند (سطل بازیافت ندارد):</p>
      <ul class="ltr small">${sel.map((n) => html`<li>${join(data.path, n)}</li>`)}</ul><//>`}
  </div>`;
}

function NameDialog({ title, initial, onOk, onClose, busy }) {
  const [v, setV] = useState(initial || "");
  return html`<${Dialog} title=${title} onClose=${onClose}>
    <form class="form" onSubmit=${(e) => { e.preventDefault(); if (v.trim() && !v.includes("/")) onOk(v.trim()); }}>
      <input class="ltr" autofocus value=${v} onInput=${(e) => setV(e.target.value)} />
      <div><button class="primary" disabled=${busy}>تأیید</button></div>
    </form><//>`;
}

const TEXT_EXT = /\.(txt|md|html?|css|js|mjs|json|ya?ml|toml|ini|conf|cfg|py|php|sh|env|sql|xml|csv|log|service|vue|ts|jsx|tsx|go|rs|java|kt|dart|gitignore|htaccess)$/i;

function FileDialog({ sid, entry, path, readonly, onClose, onChanged, onJob }) {
  const [mode, setMode] = useState(null);
  const [busy, run] = useAction();
  const [tail, setTail] = useState(null);
  const isArchive = /\.(zip|tar\.gz|tgz)$/i.test(entry.name);
  const showTail = async () => setTail(await run(() => api(`/servers/${sid}/fs/tail?path=${enc(path)}&lines=500`)).catch(() => null));
  if (mode === "edit") return html`<${Editor} sid=${sid} path=${path} readonly=${readonly} onClose=${onClose} />`;
  return html`<${Dialog} title=${entry.name} onClose=${onClose}>
    <div class="stack">
      <div class="faint small ltr">${path} · ${bytes(entry.size)} · ${entry.mode} · ${entry.owner}</div>
      <div class="row">
        ${(TEXT_EXT.test(entry.name) || entry.size < 256 * 1024) && html`<button onClick=${() => setMode("edit")}><${Icon} name="edit" />${readonly ? "نمایش" : "ویرایش"}</button>`}
        <a class="btn" href=${`/api/servers/${sid}/fs/download?path=${enc(path)}`}><${Icon} name="down" />دانلود</a>
        <button onClick=${showTail}><${Icon} name="list" />انتهای فایل</button>
        ${!readonly && isArchive && html`<button disabled=${busy} onClick=${() => run(() => api(`/servers/${sid}/fs/extract`, { method: "POST",
          body: { path, dest: parent(path) } })).then((r) => onJob({ id: r.job, title: "باز کردن " + entry.name })).catch(() => {})}><${Icon} name="archive" />باز کردن همین‌جا</button>`}
      </div>
      ${!readonly && html`<div class="two-col">
        <form class="row" onSubmit=${(e) => { e.preventDefault(); const to = e.target.to.value.trim(); if (to && !to.includes("/"))
          run(() => api(`/servers/${sid}/fs/rename`, { method: "POST", body: { from: path, to: join(parent(path), to) } }), "تغییر نام داده شد").then(onChanged).catch(() => {}); }}>
          <input name="to" class="ltr grow" value=${entry.name} /><button disabled=${busy}>تغییر نام</button></form>
        <form class="row" onSubmit=${(e) => { e.preventDefault();
          run(() => api(`/servers/${sid}/fs/chmod`, { method: "POST", body: { path, mode: e.target.mode.value.trim() } }), "دسترسی عوض شد").then(onChanged).catch(() => {}); }}>
          <input name="mode" class="ltr mono" style="width:80px" value=${(entry.mode || "").replace(/^0/, "")} /><button disabled=${busy}>دسترسی</button></form>
      </div>`}
      ${tail && html`<${LogLines} lines=${tail.lines} />`}
    </div><//>`;
}

function Editor({ sid, path, isNew, readonly, onClose }) {
  const [doc, setDoc] = useState(isNew ? { text: "", mtime: 0 } : null);
  const [err, setErr] = useState("");
  const [busy, run] = useAction();
  const ta = useRef(null);
  useEffect(() => {
    if (isNew) return;
    api(`/servers/${sid}/fs/read?path=${enc(path)}`).then(setDoc).catch((e) => setErr(e.message));
  }, [path]);
  const save = () => run(() => api(`/servers/${sid}/fs/write`, { method: "POST",
    body: { path, text: ta.current.value, if_mtime: doc.mtime || 0 } }), "ذخیره شد")
    .then((r) => setDoc({ ...doc, text: ta.current.value, mtime: r.mtime })).catch(() => {});
  const key = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (!readonly) save(); } };
  return html`<div class="overlay"><div class="dialog editor" role="dialog" aria-label=${path}>
    <div class="hd"><b class="grow ltr small">${path}</b>
      ${!readonly && doc && html`<button class="primary sm" disabled=${busy} onClick=${save}><${Icon} name="save" />ذخیره (Ctrl+S)</button>`}
      <button class="ghost sm" onClick=${onClose}>✕</button></div>
    ${err && html`<div class="error" style="margin:12px">${err}</div>`}
    ${!doc && !err && html`<div class="skeleton">…</div>`}
    ${doc && html`<textarea ref=${ta} class="code" spellcheck="false" readonly=${readonly} onKeyDown=${key} value=${doc.text}></textarea>`}
  </div></div>`;
}

// ── services ─────────────────────────────────────────────────────────
export function ServicesTab({ sid }) {
  const [list, error, reload] = usePoll(`/servers/${sid}/services`, 15000, [sid]);
  const [q, setQ] = useState("");
  const [logs, setLogs] = useState(null);
  const [busy, run] = useAction();
  if (error && !list) return html`<div class="error">${error.message}</div>`;
  if (!list) return html`<div class="skeleton">…</div>`;
  const act = (name, action) => run(() => api(`/servers/${sid}/services/action`, { method: "POST", body: { name, action } }),
    `${name}: ${action}`).then(reload).catch(() => {});
  const showLogs = (name) => run(() => api(`/servers/${sid}/services/logs?name=${enc(name)}&lines=400`)).then((r) => setLogs({ name, lines: r.lines })).catch(() => {});
  const shown = list.filter((s) => !q || s.name.includes(q) || (s.desc || "").toLowerCase().includes(q.toLowerCase()));
  return html`<div class="card">
    <div class="hd"><input class="ltr" style="max-width:260px" placeholder="جستجو: nginx, gunicorn…" value=${q} onInput=${(e) => setQ(e.target.value)} />
      <span class="grow"></span><span class="faint small">${fa(list.filter((s) => s.active === "active").length)} فعال از ${fa(list.length)}</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>سرویس</th><th>وضعیت</th><th>راه‌اندازی خودکار</th><th>توضیح</th><th></th></tr></thead>
      <tbody>${shown.map((s) => html`<tr>
        <td class="ltr mono" style="text-align:right"><b>${s.name}</b></td>
        <td><${Pill} s=${s.active === "active" ? "running" : s.active === "failed" ? "crit" : "idle"} label=${s.active + " · " + s.sub} /></td>
        <td class="small">${s.enabled === "enabled" ? "بله" : s.enabled || "—"}</td>
        <td class="small faint ltr" style="text-align:right">${s.desc}</td>
        <td class="nowrap">
          <button class="ghost sm" title="لاگ" onClick=${() => showLogs(s.name)}><${Icon} name="list" /></button>
          <button class="ghost sm" title="ری‌استارت" disabled=${busy} onClick=${() => act(s.name, "restart")}><${Icon} name="refresh" /></button>
          ${s.active === "active"
            ? html`<button class="ghost sm danger" title="توقف" disabled=${busy} onClick=${() => act(s.name, "stop")}><${Icon} name="stop" /></button>`
            : html`<button class="ghost sm" title="شروع" disabled=${busy} onClick=${() => act(s.name, "start")}><${Icon} name="play" /></button>`}
        </td></tr>`)}</tbody></table></div>
    ${logs && html`<${Dialog} title=${"لاگ " + logs.name} onClose=${() => setLogs(null)}><${LogLines} lines=${logs.lines} /><//>`}
  </div>`;
}

// ── container actions (used by the containers tab) ───────────────────
export function ContainerActions({ sid, c, manage }) {
  const [busy, run] = useAction();
  const [logs, setLogs] = useState(null);
  if (!manage) return null;
  const act = (action) => run(() => api(`/servers/${sid}/docker/action`, { method: "POST", body: { id: c.name, action } }), `${c.name}: ${action}`).catch(() => {});
  return html`<span class="nowrap">
    <button class="ghost sm" title="لاگ" onClick=${() => run(() => api(`/servers/${sid}/docker/logs?id=${enc(c.name)}&lines=400`)).then((r) => setLogs(r.lines)).catch(() => {})}><${Icon} name="list" /></button>
    <button class="ghost sm" title="ری‌استارت" disabled=${busy} onClick=${() => act("restart")}><${Icon} name="refresh" /></button>
    ${c.state === "running"
      ? html`<button class="ghost sm danger" title="توقف" disabled=${busy} onClick=${() => act("stop")}><${Icon} name="stop" /></button>`
      : html`<button class="ghost sm" title="شروع" disabled=${busy} onClick=${() => act("start")}><${Icon} name="play" /></button>`}
    ${logs && html`<${Dialog} title=${"لاگ " + c.name} onClose=${() => setLogs(null)}><${LogLines} lines=${logs} /><//>`}
  </span>`;
}

// ── databases ────────────────────────────────────────────────────────
const dbKey = (d) => `${d.engine}|${d.where}|${d.name}`;
const whereLabel = (w) => (w === "native" ? "روی سرور" : "کانتینر " + w.replace("docker:", ""));

export function DatabasesTab({ sid }) {
  const [data, error, reload] = usePoll(`/servers/${sid}/databases`, 0, [sid]);
  const [files, , reloadFiles] = usePoll(`/servers/${sid}/backup-files`, 0, [sid]);
  const [job, setJob] = useState(null);
  const [restore, setRestore] = useState(null);
  const [busy, run] = useAction();
  if (error && !data) return html`<div class="error">${error.message}</div>`;
  const dump = (d) => run(() => api(`/servers/${sid}/databases/dump`, { method: "POST", body: d }))
    .then((r) => setJob({ id: r.job, title: "dump " + d.name })).catch(() => {});
  return html`<div class="stack">
    ${job && html`<div class="card bd"><${JobBox} sid=${sid} jobId=${job.id} title=${job.title} onDone=${reloadFiles} /></div>`}
    <div class="card"><div class="hd"><h3 class="grow">دیتابیس‌ها</h3><button class="ghost sm" onClick=${reload}><${Icon} name="refresh" /></button></div>
      ${!data ? html`<div class="skeleton">…</div>` : html`
      ${data.notes && data.notes.length > 0 && html`<div class="note" style="margin:12px">${data.notes.map((n) => html`<div class="ltr small">${n}</div>`)}</div>`}
      ${!data.databases || !data.databases.length ? html`<${Empty} title="دیتابیسی پیدا نشد">MySQL/MariaDB و PostgreSQL، روی سرور یا داخل Docker، خودکار پیدا می‌شوند.<//>`
      : html`<div class="tbl-wrap"><table><thead><tr><th>نام</th><th>نوع</th><th>محل</th><th class="num">حجم</th><th></th></tr></thead>
        <tbody>${data.databases.sort((a, b) => b.size - a.size).map((d) => html`<tr>
          <td class="ltr mono" style="text-align:right"><b>${d.name}</b></td>
          <td>${d.engine === "mysql" ? "MySQL / MariaDB" : "PostgreSQL"}</td><td class="small">${whereLabel(d.where)}</td>
          <td class="num">${bytes(d.size)}</td>
          <td class="nowrap"><button class="sm" disabled=${busy} onClick=${() => dump(d)}><${Icon} name="down" />بک‌آپ الان</button>
            <button class="sm danger" onClick=${() => setRestore(d)}>بازگردانی…</button></td></tr>`)}</tbody></table></div>`}`}
    </div>
    <${BackupFiles} sid=${sid} files=${files} reload=${reloadFiles} />
    ${restore && html`<${RestoreDialog} sid=${sid} db=${restore} files=${files} onClose=${() => setRestore(null)}
      onJob=${(j) => { setRestore(null); setJob(j); }} />`}
  </div>`;
}

let serverNames = {};
export function BackupFiles({ sid, files, reload }) {
  const [busy, run] = useAction();
  useEffect(() => { api("/overview").then((o) => { serverNames = Object.fromEntries(o.servers.map((s) => [s.id, s.name])); }).catch(() => {}); }, []);
  if (!files) return null;
  return html`<div class="card"><div class="hd"><h3 class="grow">فایل‌های بک‌آپ این سرور</h3>
      <span class="faint small">${files.dir} · فضای آزاد ${bytes(files.free)}</span></div>
    ${!files.files || !files.files.length ? html`<${Empty} title="هنوز بک‌آپی نیست" />` : html`<div class="tbl-wrap"><table>
      <thead><tr><th>فایل</th><th>از سرور</th><th class="num">حجم</th><th>زمان</th><th></th></tr></thead>
      <tbody>${files.files.map((f) => html`<tr><td class="ltr mono small" style="text-align:right">${f.name}</td>
        <td class="small">${f.from === "pre-restore" ? "نسخهٔ احتیاطی پیش از بازگردانی" : f.from ? (serverNames[f.from] || f.from) : "همین سرور"}</td><td class="num">${bytes(f.size)}</td><td class="small nowrap">${dt(f.mtime)}</td>
        <td class="nowrap"><a class="btn ghost sm" href=${`/api/servers/${sid}/fs/download?path=${enc(f.path)}`} title="دانلود"><${Icon} name="down" /></a>
          <button class="ghost sm danger" disabled=${busy} title="حذف" onClick=${() => run(() => api(`/servers/${sid}/fs/delete`,
            { method: "POST", body: { paths: [f.path] } }), "حذف شد").then(reload).catch(() => {})}><${Icon} name="trash" /></button></td></tr>`)}</tbody>
    </table></div>`}</div>`;
}

function RestoreDialog({ sid, db, files, onClose, onJob }) {
  const ext = db.engine === "postgres" ? ".dump" : ".sql.gz";
  const options = ((files && files.files) || []).filter((f) => f.name.endsWith(ext));
  const [file, setFile] = useState(options.length ? options[0].path : "");
  const [confirm, setConfirm] = useState("");
  const [busy, run] = useAction();
  return html`<${Dialog} title=${"بازگردانی " + db.name} onClose=${onClose} footer=${html`
    <button onClick=${onClose}>انصراف</button>
    <button class="danger" disabled=${busy || confirm !== db.name || !file} onClick=${() => run(() => api(`/servers/${sid}/databases/restore`,
      { method: "POST", body: { db, file, confirm } })).then((r) => onJob({ id: r.job, title: "بازگردانی " + db.name })).catch(() => {})}>بازگردانی</button>`}>
    <div class="form">
      <div class="note">محتوای فعلی دیتابیس با فایل انتخابی جایگزین می‌شود. پیش از آن، خودکار یک نسخهٔ احتیاطی در
        <code class="ltr">pre-restore/</code> گرفته می‌شود.</div>
      <label class="field">فایل (${ext})
        <select value=${file} onChange=${(e) => setFile(e.target.value)}>${options.map((f) => html`<option value=${f.path}>${f.name} — ${bytes(f.size)}</option>`)}</select></label>
      ${!options.length && html`<div class="error">فایل ${ext} مناسبی در پوشهٔ بک‌آپ این سرور نیست.</div>`}
      <label class="field">برای تأیید، نام دیتابیس را بنویسید: <b class="ltr">${db.name}</b>
        <input class="ltr" value=${confirm} onInput=${(e) => setConfirm(e.target.value)} /></label>
    </div><//>`;
}

// ── SSL ──────────────────────────────────────────────────────────────
export const daysLeft = (ts) => Math.floor((ts - Date.now() / 1000) / 86400);
export const CertPill = ({ ts }) => {
  if (!ts) return html`<${Pill} s="idle" label="بدون گواهی" />`;
  const d = daysLeft(ts);
  return html`<${Pill} s=${d < 7 ? "crit" : d < 21 ? "warn" : "ok"} label=${d < 0 ? "منقضی شده" : fa(d) + " روز"} />`;
};

export function SSLTab({ sid }) {
  const [data, error, reload] = usePoll(`/servers/${sid}/ssl`, 0, [sid]);
  const [job, setJob] = useState(null);
  const [issue, setIssue] = useState(null);
  const [busy, run] = useAction();
  if (error && !data) return html`<div class="error">${error.message}</div>`;
  if (!data) return html`<div class="skeleton">…</div>`;
  const renew = (c, force) => run(() => api(`/servers/${sid}/ssl/renew`, { method: "POST", body: { name: c.name, force } }))
    .then((r) => setJob({ id: r.job, title: "تمدید " + c.name })).catch(() => {});
  return html`<div class="stack">
    ${job && html`<div class="card bd"><${JobBox} sid=${sid} jobId=${job.id} title=${job.title} onDone=${reload} /></div>`}
    <div class="card"><div class="hd"><h3 class="grow">گواهی‌ها</h3>
      ${!data.certbot && html`<span class="faint small">certbot روی این سرور نصب نیست</span>`}</div>
      ${!data.certs.length ? html`<${Empty} title="گواهی‌ای پیدا نشد" />` : html`<div class="tbl-wrap"><table>
        <thead><tr><th>دامنه‌ها</th><th>منبع</th><th>صادرکننده</th><th>انقضا</th><th></th></tr></thead>
        <tbody>${data.certs.map((c) => html`<tr>
          <td class="ltr small" style="text-align:right">${c.domains.join(", ")}</td><td class="small">${c.source}</td>
          <td class="small faint">${c.issuer}</td><td class="nowrap"><${CertPill} ts=${c.not_after} /> <span class="faint small">${dt(c.not_after)}</span></td>
          <td>${c.source === "certbot" && data.certbot && html`<button class="sm" disabled=${busy} onClick=${() => renew(c, daysLeft(c.not_after) > 30)}>تمدید</button>`}
            ${c.source === "caddy" && html`<span class="faint small">Caddy خودکار تمدید می‌کند</span>`}</td></tr>`)}</tbody></table></div>`}
    </div>
    <div class="card"><div class="hd"><h3>سایت‌های nginx</h3></div>
      ${!data.sites.length ? html`<${Empty} title="سایتی در nginx پیدا نشد" />` : html`<div class="tbl-wrap"><table>
        <thead><tr><th>دامنه‌ها</th><th>HTTPS</th><th>مقصد</th><th>فایل</th><th></th></tr></thead>
        <tbody>${data.sites.map((s) => html`<tr>
          <td class="ltr small" style="text-align:right">${s.names.join(" ")}</td>
          <td>${s.ssl ? html`<${Pill} s="ok" label="دارد" />` : html`<${Pill} s="warn" label="ندارد" />`}</td>
          <td class="ltr small faint" style="text-align:right">${s.proxy_pass || s.root || ""}</td>
          <td class="ltr small faint" style="text-align:right">${s.file.replace("/etc/nginx/", "")}</td>
          <td>${!s.ssl && data.certbot && html`<button class="sm primary" onClick=${() => setIssue(s)}>گرفتن گواهی</button>`}</td></tr>`)}</tbody></table></div>`}
    </div>
    ${issue && html`<${IssueDialog} sid=${sid} site=${issue} onClose=${() => setIssue(null)} onJob=${(j) => { setIssue(null); setJob(j); }} />`}
  </div>`;
}

function IssueDialog({ sid, site, onClose, onJob }) {
  const [doms, setDoms] = useState(site.names.join(" "));
  const [email, setEmail] = useState("");
  const [busy, run] = useAction();
  return html`<${Dialog} title="گرفتن گواهی SSL" onClose=${onClose} footer=${html`<button onClick=${onClose}>انصراف</button>
    <button class="primary" disabled=${busy} onClick=${() => run(() => api(`/servers/${sid}/ssl/issue`, { method: "POST",
      body: { domains: doms.split(/[\s,]+/).filter(Boolean), email, redirect: true } }))
      .then((r) => onJob({ id: r.job, title: "گواهی " + doms })).catch(() => {})}>درخواست</button>`}>
    <div class="form"><div class="note">رکورد A همهٔ این دامنه‌ها باید به همین سرور اشاره کند. certbot پیکربندی همین سایت را برای HTTPS ویرایش می‌کند.</div>
      <label class="field">دامنه‌ها<input class="ltr" value=${doms} onInput=${(e) => setDoms(e.target.value)} /></label>
      <label class="field">ایمیل (برای هشدار انقضا، اختیاری)<input class="ltr" type="email" value=${email} onInput=${(e) => setEmail(e.target.value)} /></label>
    </div><//>`;
}

// ── console ──────────────────────────────────────────────────────────
export function ConsoleTab({ sid }) {
  const [cmd, setCmd] = useState("");
  const [cwd, setCwd] = useState("/root");
  const [hist, setHist] = useState([]);
  const [busy, run] = useAction();
  const go = (e) => {
    e.preventDefault();
    if (!cmd.trim()) return;
    const c = cmd;
    run(() => api(`/servers/${sid}/console`, { method: "POST", body: { cmd: c, cwd, timeout: 120 } }))
      .then((r) => { setHist([{ cmd: c, ...r }, ...hist].slice(0, 30)); setCmd(""); }).catch(() => {});
  };
  return html`<div class="stack">
    <div class="note">هر دستور با root و جداگانه اجرا می‌شود (بدون حالت تعاملی، حداکثر ۲ دقیقه) و در گزارش فعالیت ثبت می‌شود.</div>
    <form class="card bd row" onSubmit=${go}>
      <input class="ltr mono" style="width:160px" value=${cwd} onInput=${(e) => setCwd(e.target.value)} title="پوشهٔ اجرا" />
      <input class="ltr mono grow" autofocus placeholder="df -h" value=${cmd} onInput=${(e) => setCmd(e.target.value)} />
      <button class="primary" disabled=${busy}><${Icon} name="term" />${busy ? "…" : "اجرا"}</button></form>
    ${hist.map((h) => html`<div class="card"><div class="hd"><code class="ltr grow">$ ${h.cmd}</code>
      <${Pill} s=${h.code === 0 ? "ok" : "crit"} label=${"exit " + h.code} /><span class="faint small">${fa(h.ms)} ms</span></div>
      <pre class="logbox">${h.output || "(بدون خروجی)"}</pre></div>`)}
  </div>`;
}

export function MonitorOnlyNote({ origin }) {
  return html`<div class="card bd stack"><h3>مدیریت این سرور فعال نیست</h3>
    <p class="muted" style="margin:0">agent این سرور فقط مانیتور می‌کند. برای فایل‌ها، سرویس‌ها، دیتابیس، SSL و بک‌آپ، روی همین سرور با root اجرا کنید:</p>
    <code class="ltr cmdline">curl -fsSL ${origin}/install.sh | sudo bash -s -- --upgrade</code>
    <p class="faint small" style="margin:0">پس از آن، محدودهٔ مجاز در <code class="ltr">/etc/icsd-agent/policy.json</code> روی خود سرور تعیین می‌شود (فقط root آن سرور می‌تواند عوضش کند).</p></div>`;
}
