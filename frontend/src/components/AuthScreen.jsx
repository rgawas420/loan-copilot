import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

const GSI_SRC = "https://accounts.google.com/gsi/client";
let gsiLoading = null;

/** Google Identity Services, loaded once and only when Google sign-in is set up. */
function loadGsi() {
  if (window.google?.accounts?.id) return Promise.resolve(window.google);
  gsiLoading = gsiLoading || new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = GSI_SRC; s.async = true; s.defer = true;
    s.onload = () => resolve(window.google);
    s.onerror = () => { gsiLoading = null; reject(new Error("Google sign-in could not be loaded.")); };
    document.head.appendChild(s);
  });
  return gsiLoading;
}

/** Google's own "Continue with Google" button (its branding rules require Google's rendering). The ID token it
 *  returns goes to /api/auth/google, which signs in to the same account system as the form below. */
function GoogleButton({ clientId, register, disabled, onCredential, onError }) {
  const box = useRef(null);
  const latest = useRef(onCredential);
  latest.current = onCredential;
  useEffect(() => {
    let live = true;
    loadGsi().then((google) => {
      if (!live || !box.current) return;
      google.accounts.id.initialize({ client_id: clientId, callback: (r) => latest.current(r.credential),
                                      ux_mode: "popup", cancel_on_tap_outside: true });
      const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches
        && document.documentElement.dataset.theme !== "light";
      box.current.innerHTML = "";
      google.accounts.id.renderButton(box.current, {
        type: "standard", size: "large", shape: "pill", logo_alignment: "center",
        theme: dark ? "filled_black" : "outline", text: register ? "signup_with" : "continue_with",
        width: Math.min(380, box.current.offsetWidth || 380),
      });
    }).catch((e) => live && onError(e.message));
    return () => { live = false; };
  }, [clientId, register, onError]);
  return <div ref={box} className={`google-btn${disabled ? " busy" : ""}`} aria-busy={disabled || undefined} />;
}

export default function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState("login");
  const [allowRegister, setAllowRegister] = useState(true);
  const [googleId, setGoogleId] = useState(null);
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.authConfig().then((c) => { setAllowRegister(c.allow_registration); setGoogleId(c.google_client_id || null); })
      .catch(() => {});
  }, []);

  async function google(credential) {
    setError("");
    setBusy(true);
    try { onAuthed((await api.googleLogin(credential)).user); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const register = mode === "register";

  async function submit(e) {
    e.preventDefault();
    setError("");
    if (register && form.password.length < 8) { setError("Use at least 8 characters for the password."); return; }
    setBusy(true);
    try {
      const r = register ? await api.register(form.email, form.name, form.password)
                         : await api.login(form.email, form.password);
      onAuthed(r.user);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  function switchMode(m) { setMode(m); setError(""); }
  // The error below the form describes the fields; focusing on load would open a phone's keyboard over the page.
  const invalid = error ? { "aria-invalid": true, "aria-describedby": "auth-error" } : {};
  const finePointer = window.matchMedia?.("(pointer: fine)").matches ?? true;

  return (
    <main className="auth">
      <div className="auth-card">
        <span className="logo lg" aria-hidden="true">₹</span>
        <h1>{register ? "Create your account" : "Welcome back"}</h1>
        <p className="auth-sub">Urban Money Loan Copilot</p>
        {googleId && (
          <>
            <GoogleButton clientId={googleId} register={register} disabled={busy} onCredential={google} onError={setError} />
            <p className="auth-or" aria-hidden="true"><span>or</span></p>
          </>
        )}
        <form onSubmit={submit} noValidate>
          {register && (
            <label className="field">
              <span>Name</span>
              <input type="text" autoComplete="name" required maxLength={80} value={form.name} onChange={set("name")} {...invalid} />
            </label>
          )}
          <label className="field">
            <span>Email address</span>
            <input type="email" autoComplete="email" autoCapitalize="none" spellCheck={false} required maxLength={200}
                   value={form.email} onChange={set("email")} autoFocus={finePointer} {...invalid} />
          </label>
          <label className="field">
            <span>Password</span>
            <input type="password" autoComplete={register ? "new-password" : "current-password"} required
                   minLength={register ? 8 : undefined} maxLength={200} value={form.password} onChange={set("password")} {...invalid} />
          </label>
          {register && <p className="field-hint">At least 8 characters.</p>}
          <p className="form-error" id="auth-error" role="alert">{error}</p>
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? "Please wait…" : register ? "Create account" : "Continue"}
          </button>
        </form>
        {allowRegister ? (
          <p className="auth-switch">
            {register ? "Already have an account? " : "Don't have an account? "}
            <button type="button" className="link" onClick={() => switchMode(register ? "login" : "register")}>
              {register ? "Log in" : "Sign up"}
            </button>
          </p>
        ) : !register && <p className="auth-switch muted">Sign-up is closed. Ask an administrator for an account.</p>}
      </div>
      <p className="auth-foot">Indicative lender matching for Urban Money channel partners. Each account's chats are private.</p>
    </main>
  );
}
