import { useEffect, useState } from "react";
import { api } from "../api.js";

export default function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState("login");
  const [allowRegister, setAllowRegister] = useState(true);
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.authConfig().then((c) => setAllowRegister(c.allow_registration)).catch(() => {}); }, []);

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

  return (
    <main className="auth">
      <div className="auth-card">
        <span className="logo lg" aria-hidden="true">₹</span>
        <h1>{register ? "Create your account" : "Welcome back"}</h1>
        <p className="auth-sub">Urban Money Loan Copilot</p>
        <form onSubmit={submit} noValidate>
          {register && (
            <label className="field">
              <span>Name</span>
              <input type="text" autoComplete="name" required maxLength={80} value={form.name} onChange={set("name")} />
            </label>
          )}
          <label className="field">
            <span>Email address</span>
            <input type="email" autoComplete="email" required maxLength={200} value={form.email} onChange={set("email")} autoFocus />
          </label>
          <label className="field">
            <span>Password</span>
            <input type="password" autoComplete={register ? "new-password" : "current-password"} required
                   minLength={register ? 8 : undefined} maxLength={200} value={form.password} onChange={set("password")} />
          </label>
          {register && <p className="field-hint">At least 8 characters.</p>}
          <p className="form-error" role="alert">{error}</p>
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
