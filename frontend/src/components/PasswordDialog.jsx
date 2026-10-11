import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import Icon from "./Icon.jsx";

export default function PasswordDialog({ onClose }) {
  const [f, setF] = useState({ current: "", next: "", confirm: "" });
  const [msg, setMsg] = useState({ text: "", ok: false });
  const ref = useRef(null);
  useEffect(() => {
    const d = ref.current;
    d?.showModal();
    return () => d?.close();
  }, []);
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    if (f.next !== f.confirm) { setMsg({ text: "The new passwords do not match." }); return; }
    try {
      await api.changePassword(f.current, f.next);
      setMsg({ text: "Password changed.", ok: true });
      setF({ current: "", next: "", confirm: "" });
    } catch (err) { setMsg({ text: err.message }); }
  }

  return (
    <dialog ref={ref} className="dialog" onClose={onClose} onCancel={onClose} aria-labelledby="pw-title">
      <form onSubmit={submit}>
        <div className="dialog-head">
          <h2 id="pw-title">Change password</h2>
          <button type="button" className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <label className="field"><span>Current password</span>
          <input type="password" autoComplete="current-password" required value={f.current} onChange={set("current")} /></label>
        <label className="field"><span>New password</span>
          <input type="password" autoComplete="new-password" minLength={8} required value={f.next} onChange={set("next")} /></label>
        <label className="field"><span>Confirm new password</span>
          <input type="password" autoComplete="new-password" minLength={8} required value={f.confirm} onChange={set("confirm")} /></label>
        <p className={msg.ok ? "form-ok" : "form-error"} role="status">{msg.text}</p>
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>Close</button>
          <button type="submit" className="btn btn-primary">Change password</button>
        </div>
      </form>
    </dialog>
  );
}
