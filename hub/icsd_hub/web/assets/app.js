// ICSD Panel — app shell: session, routing (#/…), layout, theme.
import { html, render, useEffect, useState } from "./vendor/preact-htm.js";
import { Login, Setup2FA } from "./auth.js";
import { Dashboard } from "./dashboard.js";
import { AlertsPage, ChecksPage, SettingsPage } from "./pages.js";
import { ServerPage } from "./server.js";
import { Icon, api, events, fa, load, save, usePoll } from "./lib.js";

function useHash() {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const on = () => { setHash(location.hash || "#/"); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function useTheme() {
  const [theme, setTheme] = useState(load("icsd.theme") || "auto");
  useEffect(() => {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    save("icsd.theme", theme);
  }, [theme]);
  const dark = theme === "dark" || (theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  return [dark ? "dark" : "light", () => setTheme(dark ? "light" : "dark")];
}

function Shell({ me, onLogout, onMe }) {
  const hash = useHash();
  const [theme, toggle] = useTheme();
  const [ov] = usePoll("/alerts", 30000);
  const open = ov ? ov.open.length : 0;
  const parts = hash.replace(/^#\/?/, "").split("/");
  const page = parts[0] || "";
  useEffect(() => {
    document.title = (open ? `(${fa(open)}) ` : "") + "پنل ICSD";
  }, [open]);

  const nav = [
    ["", "grid", "داشبورد"],
    ["alerts", "bell", "هشدارها"],
    ["checks", "globe", "پایش آدرس‌ها"],
    ["settings", "gear", "تنظیمات"],
  ];
  let body;
  if (page === "server" && parts[1]) body = html`<${ServerPage} id=${parts[1]} me=${me} theme=${theme} key=${parts[1]} />`;
  else if (page === "alerts") body = html`<${AlertsPage} me=${me} />`;
  else if (page === "checks") body = html`<${ChecksPage} me=${me} theme=${theme} />`;
  else if (page === "settings") body = html`<${SettingsPage} me=${me} onMe=${onMe} />`;
  else body = html`<${Dashboard} me=${me} />`;

  return html`<div class="app">
    <aside class="side">
      <div class="brand"><img src="/assets/logo.svg" alt="" /><div><b>پنل ICSD</b><span>مدیریت یکپارچهٔ سرورها</span></div></div>
      <nav class="nav">${nav.map(([k, icon, label]) => html`<a href=${"#/" + k}
          class=${page === k || (k === "" && page === "server") ? "on" : ""} title=${label}>
          <${Icon} name=${icon} /><span class="lbl">${label}</span>
          ${k === "alerts" && open > 0 ? html`<span class="count">${fa(open)}</span>` : null}</a>`)}</nav>
      <div class="foot">
        <div class="who">${me.username} · ${me.role === "admin" ? "مدیر" : "مشاهده‌گر"}</div>
        <div class="row">
          <button class="ghost sm" onClick=${toggle} title="روشن/تیره"><${Icon} name=${theme === "dark" ? "sun" : "moon"} /></button>
          <button class="ghost sm" onClick=${onLogout} title="خروج"><${Icon} name="out" /><span class="lbl">خروج</span></button>
        </div>
        <div class="faint small who">نسخهٔ ${me.version}</div>
      </div>
    </aside>
    <main class="main">${body}</main>
  </div>`;
}

function App() {
  const [me, setMe] = useState(undefined); // undefined: loading, null: logged out

  useEffect(() => {
    api("/me").then(setMe).catch(() => setMe(null));
    const out = () => setMe(null);
    const need = () => api("/me").then(setMe).catch(() => setMe(null));
    events.addEventListener("unauthorized", out);
    events.addEventListener("need2fa", need);
    return () => { events.removeEventListener("unauthorized", out); events.removeEventListener("need2fa", need); };
  }, []);

  const logout = async () => {
    try { await api("/auth/logout", { method: "POST" }); } catch { /* already out */ }
    setMe(null);
  };

  if (me === undefined) return html`<div class="skeleton">…</div>`;
  if (me === null) return html`<${Login} onDone=${setMe} />`;
  if (me.must_setup_2fa) return html`<${Setup2FA} me=${me} onDone=${setMe} onLogout=${logout} />`;
  return html`<${Shell} me=${me} onLogout=${logout} onMe=${setMe} />`;
}

render(html`<${App} />`, document.getElementById("root"));
