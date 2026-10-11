import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Admin: data quality. The banker directory's problems (from the RM lists), the name aliases admins add, and the
// structured policy facts (ROI, payout, fees ...) that answer exact questions once approved.

const SECTIONS = [["bankers", "Banker data"], ["aliases", "Name aliases"], ["facts", "Policy facts"]];
const KIND_LABEL = { bank: "Bank / lender", product: "Product", city: "City" };

function BankerData() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(() => { setError(""); api.bankerQuality(20).then(setData).catch((e) => setError(e.message)); }, []);
  useEffect(load, [load]);
  const built = data?.built;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Banker directory</h2>
        <button className="btn btn-ghost btn-sm" onClick={load}>Refresh</button>
      </div>
      <p className="hint">The RM lists are read into one de-duplicated directory after every sync. Fix the problems below in
        the source spreadsheets, then sync: the directory is rebuilt automatically.</p>
      {error && <p className="err" role="alert">{error}</p>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && !built && <p className="empty">Not built yet. It is built by the next knowledge-base sync.</p>}
      {built && (
        <>
          <p>
            <b>{built.bankers?.toLocaleString()}</b> bankers at <b>{built.lenders}</b> lenders from <b>{built.files}</b> files
            ({built.rows_used?.toLocaleString()} rows used). {data.current
              ? <span className="tag ok">up to date</span>
              : <span className="tag warn">out of date: rebuilding after the next sync</span>}
          </p>
          {Object.entries(data.issues).map(([kind, it]) => (
            <details key={kind} className="trace-sec">
              <summary>
                <span className={`tag ${it.count ? "warn" : "ok"}`}>{it.count.toLocaleString()}</span> {it.label}
              </summary>
              <div className="trace-body">
                {it.examples.length === 0 ? <p className="muted">None.</p> : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead><tr><th>File</th><th>Detail</th></tr></thead>
                      <tbody>{it.examples.map((e, i) => (
                        <tr key={i}><td className="small">{e.source}</td><td className="small">{e.detail}</td></tr>
                      ))}</tbody>
                    </table>
                  </div>
                )}
                {it.count > it.examples.length && <p className="hint">Showing {it.examples.length} of {it.count.toLocaleString()}.</p>}
              </div>
            </details>
          ))}
        </>
      )}
    </section>
  );
}

function Aliases() {
  const [rows, setRows] = useState(null);
  const [f, setF] = useState({ kind: "bank", alias: "", canonical: "" });
  const [error, setError] = useState("");
  const load = useCallback(() => api.aliases().then((r) => setRows(r.aliases)).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  async function add(e) {
    e.preventDefault(); setError("");
    try { await api.aliasAdd(f.kind, f.alias, f.canonical); setF({ ...f, alias: "", canonical: "" }); load(); }
    catch (x) { setError(x.message); }
  }
  async function remove(r) {
    setError("");
    try { await api.aliasRemove(r.kind, r.alias); load(); } catch (x) { setError(x.message); }
  }
  return (
    <section className="panel">
      <h2>Name aliases</h2>
      <p className="hint">Other spellings of a bank, product or city, e.g. "BHFL" → Bajaj Finserv, "HLUC" → Home Loan,
        "Pimpri" → Pune. Used in questions, document tags and the banker directory (rebuilt in the background).</p>
      <form className="inline-form" onSubmit={add}>
        <div className="form-grid">
          <label>Type<select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
            {Object.entries(KIND_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label>Alias<input type="text" required minLength={2} maxLength={80} value={f.alias}
                             onChange={(e) => setF({ ...f, alias: e.target.value })} placeholder="BHFL" /></label>
          <label>Stands for<input type="text" required maxLength={80} value={f.canonical}
                                  onChange={(e) => setF({ ...f, canonical: e.target.value })} placeholder="Bajaj Finserv" /></label>
        </div>
        <button className="btn btn-primary btn-sm">Add alias</button>
      </form>
      {error && <p className="err" role="alert">{error}</p>}
      {rows && (rows.length === 0 ? <p className="empty">No aliases added yet (the built-in names still apply).</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Type</th><th>Alias</th><th>Stands for</th><th>Added by</th><th /></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={`${r.kind}:${r.alias}`}>
                <td className="small">{KIND_LABEL[r.kind]}</td><td>{r.alias}</td><td>{r.canonical}</td>
                <td className="small">{r.by}</td>
                <td><button className="btn btn-ghost btn-sm" onClick={() => remove(r)}>Remove</button></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

function Facts() {
  const [status, setStatus] = useState("pending");
  const [data, setData] = useState(null);
  const [picked, setPicked] = useState(new Set());
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const fileRef = useRef(null);
  const load = useCallback(() => {
    setPicked(new Set());
    api.facts(status).then(setData).catch((e) => setError(e.message));
  }, [status]);
  useEffect(() => { load(); }, [load]);
  const toggle = (id) => setPicked((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  async function upload(file) {
    if (!file) return;
    setError(""); setMsg("");
    try { const r = await api.factsImport(file); setMsg(`${r.added} added, ${r.skipped} already there. Review and approve them below.`); setStatus("pending"); load(); }
    catch (x) { setError(x.message); }
    if (fileRef.current) fileRef.current.value = "";
  }
  async function act(fn, label) {
    setError(""); setMsg("");
    try { const r = await fn([...picked]); setMsg(`${Object.values(r)[0]} ${label}.`); load(); } catch (x) { setError(x.message); }
  }
  const rows = data?.facts || [];
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Policy facts</h2>
        <select value={status} aria-label="Status" onChange={(e) => setStatus(e.target.value)}>
          <option value="pending">Waiting for approval</option><option value="approved">Approved</option><option value="">All</option>
        </select>
        <button className="btn btn-ghost btn-sm" onClick={() => fileRef.current?.click()}>Import CSV</button>
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={(e) => upload(e.target.files[0])} />
      </div>
      <p className="hint">Exact values per lender and product, with the document and dates they come from. Approved values in
        force today answer questions like "HDFC home loan ROI" before any document text. CSV columns: lender, product, fact,
        value, unit, customer_category, effective_from, effective_until, source, page.
        Facts: {data ? Object.keys(data.kinds).join(", ") : "…"}.</p>
      {msg && <p className="hint">{msg}</p>}
      {error && <p className="err" role="alert">{error}</p>}
      {picked.size > 0 && (
        <div className="confirm-bar">
          <span>{picked.size} selected</span>
          {status !== "approved" && <button className="btn btn-primary btn-sm" onClick={() => act(api.factsApprove, "approved")}>Approve</button>}
          <button className="btn btn-danger btn-sm" onClick={() => act(api.factsDelete, "deleted")}>Delete</button>
        </div>
      )}
      {data && (rows.length === 0 ? <p className="empty">No facts here.</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th><input type="checkbox" aria-label="Select all" checked={picked.size === rows.length}
                                  onChange={(e) => setPicked(e.target.checked ? new Set(rows.map((r) => r.fact_id)) : new Set())} /></th>
              <th>Lender</th><th>Product</th><th>Fact</th><th>Value</th><th>Effective</th><th>Source</th><th>Status</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.fact_id}>
                <td><input type="checkbox" aria-label="Select" checked={picked.has(r.fact_id)} onChange={() => toggle(r.fact_id)} /></td>
                <td>{r.lender}</td><td className="small">{r.product || "All"}{r.customer_category ? ` · ${r.customer_category}` : ""}</td>
                <td className="small">{data.kinds[r.fact] || r.fact}</td><td><b>{r.value}</b>{r.unit ? ` ${r.unit}` : ""}</td>
                <td className="small nowrap">{r.effective_from || "…"} → {r.effective_until || "now"}</td>
                <td className="small">{r.source}{r.page ? `, p. ${r.page}` : ""}</td>
                <td><span className={`tag ${r.status === "approved" ? "ok" : "warn"}`}>{r.status}</span></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

export default function DataQuality() {
  const [section, setSection] = useState("bankers");
  return (
    <>
      <nav className="seg" aria-label="Data quality sections">
        {SECTIONS.map(([id, label]) => (
          <button key={id} className={section === id ? "on" : ""} onClick={() => setSection(id)}>{label}</button>
        ))}
      </nav>
      {section === "bankers" && <BankerData />}
      {section === "aliases" && <Aliases />}
      {section === "facts" && <Facts />}
    </>
  );
}
