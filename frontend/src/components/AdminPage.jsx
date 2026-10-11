import { useEffect, useState } from "react";
import { api } from "../api.js";
import { LOAN_TYPES, badgeClass } from "../format.js";

const POLICY_ACCEPT = ".yaml,.yml,.pdf,.docx,.doc,.txt,.md,.csv,.xls,.xlsx,.xlsm,.png,.jpg,.jpeg,.webp,.gif,.bmp";

function Upload({ adminKey, onDone }) {
  const [file, setFile] = useState(null);
  const [lender, setLender] = useState("");
  const [loanType, setLoanType] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  async function upload() {
    if (!file) { setMsg("Choose a file first."); return; }
    setBusy(true); setMsg("Uploading");
    try {
      const d = await api.ingest(adminKey, file, lender, loanType);
      setMsg(d.kind === "structured"
        ? `Loaded ${d.programs} structured programs for ${d.lender}.`
        : `Added ${d.chunks_indexed} policy sections from ${d.filename}.`);
      onDone();
    } catch (e) { setMsg(e.message); }
    finally { setBusy(false); }
  }

  return (
    <section className="panel">
      <h2>Add lender policy</h2>
      <p className="hint">
        Structured rules (.yaml) decide the match. Policy documents (PDF, Word, Excel, TXT, MD, images) are supporting
        text; tag them with a lender and loan type so they are found for the right cases.
      </p>
      <div className="form-grid">
        <label>File<input type="file" accept={POLICY_ACCEPT} onChange={(e) => setFile(e.target.files[0] || null)} /></label>
        <label>Lender (documents only)<input type="text" value={lender} onChange={(e) => setLender(e.target.value)} /></label>
        <label>Loan type (documents only)
          <select value={loanType} onChange={(e) => setLoanType(e.target.value)}>
            <option value="">Not tagged</option>
            {LOAN_TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
      </div>
      <button className="primary" disabled={busy} onClick={upload}>Upload policy</button>
      <p className="hint" role="status">{msg}</p>
    </section>
  );
}

function DeleteButton({ onDelete }) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) return <button className="linkish" onClick={() => setConfirming(true)}>Delete</button>;
  return (
    <span className="confirm">
      <button className="danger" onClick={onDelete}>Confirm delete</button>
      <button className="linkish" onClick={() => setConfirming(false)}>Cancel</button>
    </span>
  );
}

// Policies and audit. Opened from the admin console, so the admin session authorises these calls (no key).
export default function AdminPage() {
  const adminKey = "";
  const [data, setData] = useState(null);
  const [audit, setAudit] = useState([]);
  const [error, setError] = useState("");
  const [openTrace, setOpenTrace] = useState(null);

  async function load(key = adminKey) {
    setError("");
    try {
      const [p, a] = await Promise.all([api.policies(key), api.audit(key)]);
      setData(p); setAudit(a);
    } catch (e) { setError(e.message); setData(null); setAudit([]); }
  }

  useEffect(() => { load(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  async function remove(id) {
    try { await api.deleteDoc(adminKey, id); await load(); }
    catch (e) { setError(e.message); }
  }

  return (
    <div className="admin-section">
      {error && <p className="err" role="alert">{error}</p>}

      <Upload adminKey={adminKey} onDone={() => data && load()} />

      {data && (
        <>
          <section className="panel">
            <h2>Structured programs <span className="count">{data.structured_programs.length}</span></h2>
            <div className="cmp-wrap">
              <table className="cmp">
                <thead><tr><th>Lender</th><th>Program</th><th>Loan type</th><th>Last verified</th><th>Flags</th></tr></thead>
                <tbody>
                  {data.structured_programs.map((p) => (
                    <tr key={p.id}>
                      <td>{p.lender}</td><td>{p.program}</td><td>{p.loan_type}</td>
                      <td>{p.policy_last_verified || "Unknown"}</td>
                      <td>
                        {p.stale && <span className="flag stale">Needs re-verification</span>}
                        {p.sample && <span className="flag sample">Sample data</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel">
            <h2>Uploaded documents <span className="count">{data.documents.length}</span></h2>
            {data.documents.length === 0 ? <p className="empty">No documents uploaded yet.</p> : (
              <div className="cmp-wrap">
                <table className="cmp">
                  <thead><tr><th>File</th><th>Lender</th><th>Loan type</th><th>Sections</th><th>Added</th><th /></tr></thead>
                  <tbody>
                    {data.documents.map((d) => (
                      <tr key={d.sha}>
                        <td>{d.filename}</td><td>{d.lender || "Not tagged"}</td><td>{d.loan_type || "Not tagged"}</td>
                        <td>{d.chunks}</td><td>{d.ts?.replace("T", " ").slice(0, 16)}</td>
                        <td><DeleteButton onDelete={() => remove(d.sha)} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <h2>Recent recommendations <span className="count">{audit.length}</span></h2>
            {audit.length === 0 ? <p className="empty">No runs yet.</p> : (
              <div className="cmp-wrap">
                <table className="cmp">
                  <thead><tr><th>Time (UTC)</th><th>Loan type</th><th>Status</th><th>Source</th><th>Trace</th></tr></thead>
                  <tbody>
                    {audit.map((a) => (
                      <tr key={a.id}>
                        <td>{a.ts?.replace("T", " ").slice(0, 16)}</td><td>{a.loan_type}</td>
                        <td><span className={`badge small ${badgeClass(a.status)}`}>{a.status}</span></td>
                        <td>{a.source}</td>
                        <td>
                          <button className="linkish" aria-expanded={openTrace === a.id}
                                  onClick={() => setOpenTrace(openTrace === a.id ? null : a.id)}>
                            {openTrace === a.id ? "Hide" : `${a.trace.length} steps`}
                          </button>
                          {openTrace === a.id && <ol className="mini-trace">{a.trace.map((t, i) => <li key={i}>{t}</li>)}</ol>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
