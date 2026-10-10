// Login and the mandatory 2FA setup screen.
import { html, useState } from "./vendor/preact-htm.js";
import { api } from "./lib.js";

const Logo = () => html`<div class="logo"><img src="/assets/logo.svg" alt="" />
  <h1>پنل ICSD</h1><span class="muted small">مدیریت یکپارچهٔ سرورها</span></div>`;

export function Login({ onDone }) {
  const [f, setF] = useState({ username: "", password: "", otp: "" });
  const [needOtp, setNeedOtp] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const me = await api("/auth/login", { method: "POST", body: { ...f, otp: needOtp ? f.otp : null } });
      onDone(me);
    } catch (ex) {
      if (ex.data && ex.data.need_otp) {
        if (needOtp) setErr(ex.message);
        setNeedOtp(true);
        setF((v) => ({ ...v, otp: "" }));
      } else {
        setErr(ex.message);
      }
    } finally {
      setBusy(false);
    }
  };

  return html`<div class="auth"><div class="box">
    <${Logo} />
    <form class="card bd form" style="padding:22px" onSubmit=${submit}>
      ${!needOtp && html`
        <label class="field">نام کاربری
          <input class="ltr" autocomplete="username" autofocus required value=${f.username} onInput=${set("username")} /></label>
        <label class="field">رمز عبور
          <input class="ltr" type="password" autocomplete="current-password" required value=${f.password} onInput=${set("password")} /></label>`}
      ${needOtp && html`
        <div class="note">کد ۶ رقمی برنامهٔ Authenticator گوشی را وارد کنید.</div>
        <label class="field">کد رمز دوم
          <input class="ltr mono" inputmode="numeric" autocomplete="one-time-code" maxlength="6" autofocus required
            value=${f.otp} onInput=${set("otp")} style="font-size:20px;letter-spacing:6px;text-align:center" /></label>`}
      ${err && html`<div class="error">${err}</div>`}
      <button class="primary" disabled=${busy}>${busy ? "…" : needOtp ? "تأیید" : "ورود"}</button>
      ${needOtp && html`<button type="button" class="ghost" onClick=${() => { setNeedOtp(false); setErr(""); }}>بازگشت</button>`}
    </form>
  </div></div>`;
}

export function Setup2FA({ me, onDone, onLogout, embedded }) {
  const [data, setData] = useState(null);
  const [code, setCode] = useState("");
  const [current, setCurrent] = useState("");
  const replacing = !!me.totp_on; // changing an active 2FA needs a current code
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const begin = async () => {
    setErr("");
    try {
      setData(await api("/auth/2fa/begin", { method: "POST", body: { code: replacing ? current : null } }));
    } catch (e) { setErr(e.message); }
  };
  const confirm = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      onDone(await api("/auth/2fa/confirm", { method: "POST", body: { code } }));
    } catch (ex) { setErr(ex.message); } finally { setBusy(false); }
  };

  const body = html`<div class="card bd stack" style="padding:22px">
    <div>
      <h2>فعال‌سازی رمز دوم (2FA)</h2>
      <p class="muted small" style="margin:6px 0 0">
        این پنل به سرورها دسترسی دارد؛ پس فقط رمز عبور کافی نیست. با برنامه‌ای مثل
        Google Authenticator، Microsoft Authenticator یا Aegis کد زیر را اسکن کنید.</p>
    </div>
    ${!data && replacing && html`<label class="field">کد فعلی رمز دوم (از برنامهٔ گوشی قبلی)
      <input class="ltr mono" inputmode="numeric" maxlength="6" value=${current} onInput=${(e) => setCurrent(e.target.value)}
        style="font-size:18px;letter-spacing:5px;text-align:center" /></label>`}
    ${!data && html`<button class="primary" onClick=${begin}>شروع</button>`}
    ${data && html`
      <div class="qr" dangerouslySetInnerHTML=${{ __html: data.qr_svg }}></div>
      <details class="small muted"><summary>اسکن نمی‌شود؟ کلید را دستی وارد کنید</summary>
        <code class="ltr" style="display:block;margin-top:6px;word-break:break-all">${data.secret}</code></details>
      <form class="form" onSubmit=${confirm}>
        <label class="field">کدی که برنامه نشان می‌دهد
          <input class="ltr mono" inputmode="numeric" maxlength="6" autofocus required value=${code}
            onInput=${(e) => setCode(e.target.value)} style="font-size:20px;letter-spacing:6px;text-align:center" /></label>
        <button class="primary" disabled=${busy}>تأیید و فعال‌سازی</button>
      </form>`}
    ${err && html`<div class="error">${err}</div>`}
    ${!embedded && html`<button class="ghost" onClick=${onLogout}>خروج (${me.username})</button>`}
  </div>`;
  if (embedded) return body;
  return html`<div class="auth"><div class="box" style="width:min(420px,100%)"><${Logo} />${body}</div></div>`;
}
