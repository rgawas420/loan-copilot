import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, uploadKbFiles } from "../api.js";
import { LOAN_TYPES } from "../format.js";
import Icon from "./Icon.jsx";

// Admin: the documents behind the knowledge base. Files live in the Urban Money folder; every change here is
// processed (text extraction, OCR, chunking, embedding, keyword index) by a background job on the server.
// Uploads are checked first: a file that matches nothing is added as a new document; one that may be a new
// version of an existing document, an exact duplicate or an image waits in "Review uploads" for a decision.

const STATUS = {
  indexed: { label: "Indexed", cls: "ok" },
  keyword: { label: "Keyword index", cls: "info", tip: "Very large list: searchable by exact terms only" },
  pending: { label: "Not processed", cls: "warn", tip: "New or changed since the last sync" },
  queued: { label: "Queued", cls: "info" },
  processing: { label: "Processing", cls: "info" },
  failed: { label: "Failed", cls: "off" },
  unsupported: { label: "Unsupported type", cls: "" },
  too_large: { label: "Too large", cls: "" },
  duplicate: { label: "Duplicate", cls: "", tip: "Same content as another file: searched once, under that file" },
};
const FILTERS = [["", "All statuses"], ["indexed", "Indexed"], ["keyword", "Keyword index"],
  ["attention", "Needs attention"], ["ocr", "Text not verified (OCR)"], ["pending", "Not processed"], ["failed", "Failed"],
  ["active", "Queued or processing"]];
const PAGE = 100;

const size = (n) => (n == null ? "" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB`
  : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`);
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
const matches = (f, status) => !status ||
  (status === "ocr" ? f.extraction === "ocr" :
    status === "attention" ? ["failed", "pending", "unsupported", "too_large"].includes(f.status) || f.extraction === "ocr"
    : status === "active" ? ["queued", "processing"].includes(f.status) : f.status === status);

function Confirm({ text, label = "Confirm", onYes, onNo }) {
  return (
    <div className="confirm-bar" role="alertdialog">
      <span>{text}</span>
      <button className="btn btn-danger btn-sm" onClick={onYes}>{label}</button>
      <button className="btn btn-ghost btn-sm" onClick={onNo}>Cancel</button>
    </div>
  );
}

function Uploader({ folders, documents, maxMb, types, onDone }) {
  const [files, setFiles] = useState([]);
  const [folder, setFolder] = useState("");
  const [target, setTarget] = useState("");
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const input = useRef(null);
  const accept = types.map((t) => `.${t}`).join(",");

  function add(list) {
    const next = [...files];
    for (const f of list) if (!next.some((x) => x.name === f.name && x.size === f.size)) next.push(f);
    setFiles(next.slice(0, 50));
    setResult(null);
  }

  async function upload() {
    if (!files.length) return;
    setProgress(0); setResult(null);
    try {
      const r = await uploadKbFiles(files, folder, target, setProgress);
      setResult(r);
      setTarget("");
      setFiles(r.errors.length ? files.filter((f) => r.errors.some((e) => e.file === f.name)) : []);
      onDone();
    } catch (e) { setResult({ staged: [], errors: [{ file: "", error: e.message }] }); }
    finally { setProgress(null); }
  }

  const tooBig = files.filter((f) => f.size > maxMb * 1048576);
  return (
    <section className="panel">
      <h2>Upload documents</h2>
      <div className={`dropzone${drag ? " drag" : ""}`} role="button" tabIndex={0}
           onClick={() => input.current?.click()}
           onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
           onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
           onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}>
        <Icon name="folder" size={26} />
        <b>Drop files here or click to choose</b>
        <span>PDF, Word, Excel, CSV, text, images (OCR), JSON, HTML, contact cards. Up to {maxMb} MB each, 50 at a time.</span>
        <input ref={input} type="file" multiple hidden accept={accept} onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
      </div>

      {files.length > 0 && (
        <ul className="upload-list">
          {files.map((f) => (
            <li key={f.name + f.size} className={f.size > maxMb * 1048576 ? "bad" : ""}>
              <span className="fname">{f.name}</span><span className="muted">{size(f.size)}</span>
              <button className="icon-btn subtle" aria-label={`Remove ${f.name}`}
                      onClick={() => setFiles(files.filter((x) => x !== f))}><Icon name="x" size={14} /></button>
            </li>
          ))}
        </ul>
      )}

      <div className="upload-opts">
        <label className="grow">Save in folder (optional)
          <input type="text" list="kb-folders" placeholder="Top level, or e.g. Payouts/2026" value={folder}
                 onChange={(e) => setFolder(e.target.value)} />
          <datalist id="kb-folders">{folders.map((f) => <option key={f} value={f} />)}</datalist>
        </label>
        <label className="grow">Updates an existing document (optional)
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Detect automatically</option>
            {documents.map((d) => <option key={d.doc_id} value={d.path}>{d.title}{d.label ? ` (${d.label})` : ""}</option>)}
          </select>
        </label>
        <button className="btn btn-primary" disabled={!files.length || progress !== null || tooBig.length > 0} onClick={upload}>
          {progress !== null ? `Uploading ${Math.round(progress * 100)}%` : `Upload ${files.length || ""} file${files.length === 1 ? "" : "s"}`}
        </button>
      </div>
      {progress !== null && <div className="bar"><i style={{ width: `${Math.round(progress * 100)}%` }} /></div>}
      {tooBig.length > 0 && <p className="err">Remove files over {maxMb} MB before uploading.</p>}
      {result && (
        <div className="upload-result" role="status">
          {result.staged.length > 0 && (
            <p className="ok-text">Uploaded {result.staged.length} file{result.staged.length === 1 ? "" : "s"}. They are being
              checked: new documents are added automatically; possible new versions and duplicates wait under “Review uploads”.</p>
          )}
          {result.errors.map((e, i) => <p key={i} className="err">{e.error}</p>)}
        </div>
      )}
    </section>
  );
}

function PreviewDialog({ path, load, onClose }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const ref = useRef(null);
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);
  useEffect(() => { (load || (() => api.kbPreview(path)))().then(setData).catch((e) => setError(e.message)); }, [path, load]);
  return (
    <dialog ref={ref} className="dialog wide" onClose={onClose} onCancel={onClose} aria-labelledby="pv-title">
      <div className="dialog-body">
        <div className="dialog-head">
          <h2 id="pv-title" title={path}>{path.split("/").pop()}</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        {!data && !error && <p className="muted">Loading…</p>}
        {error && <p className="err">{error}</p>}
        {data?.message && <p className="hint">{data.message}</p>}
        {data?.chunks?.length > 0 && (
          <>
            <p className="hint">
              What the copilot reads from this file: {data.total} section{data.total === 1 ? "" : "s"}
              {data.total > data.chunks.length ? `, first ${data.chunks.length} shown` : ""}.
              {data.status === "keyword" && " This file is searched by exact terms only (too many rows for semantic search)."}
            </p>
            <ol className="chunks">{data.chunks.map((c, i) => <li key={i}><pre>{c}</pre></li>)}</ol>
          </>
        )}
      </div>
    </dialog>
  );
}

const VSTATUS = { ACTIVE: "ok", ARCHIVED: "", DISABLED: "off" };
const day = (iso) => (iso ? new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { dateStyle: "medium" }) : "");
const period = (from, until) => (from || until ? `${day(from) || "…"} → ${day(until) || "current"}` : "");

// Source type and priority: when documents disagree, the higher priority applies (decided here, not by the AI).
function AuthorityForm({ path, doc, sourceTypes, sourceDefaults, onSaved }) {
  const [st, setSt] = useState(doc.source_type || "");
  const [prio, setPrio] = useState(doc.priority ?? "");
  const [msg, setMsg] = useState("");
  const fallback = sourceTypes.find((s) => s.key === (st || sourceDefaults[doc.category] || "REFERENCE"));
  async function save(e) {
    e.preventDefault();
    setMsg("");
    try {
      await api.kbUpdateDocument(path, { source_type: st || null, priority: prio === "" ? null : Number(prio) });
      setMsg("Saved. Answers use the new priority straight away."); onSaved();
    } catch (err) { setMsg(err.message); }
  }
  return (
    <form onSubmit={save}>
      <div className="form-grid">
        <label>Source type
          <select value={st} onChange={(e) => setSt(e.target.value)}>
            <option value="">Default for “{doc.category || "General"}”</option>
            {sourceTypes.map((s) => <option key={s.key} value={s.key}>{s.label} ({s.priority})</option>)}
          </select></label>
        <label>Priority (0–100)
          <input type="number" min={0} max={100} value={prio} placeholder={`${fallback?.priority ?? 50} (default)`}
                 onChange={(e) => setPrio(e.target.value)} /></label>
      </div>
      <div className="row-gap"><button className="btn btn-primary btn-sm" type="submit">Save priority</button>
        {msg && <span className="hint" role="status">{msg}</span>}</div>
    </form>
  );
}

function AuditList({ events }) {
  if (!events.length) return <p className="empty">No changes recorded yet.</p>;
  return (
    <div className="table-wrap">
      <table className="table audit-table">
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Document</th><th>Details</th></tr></thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td className="nowrap small">{when(e.ts)}</td>
              <td className="small">{e.actor}</td>
              <td className="nowrap">{e.label}{e.version ? <span className="muted small"> · v{e.version}</span> : null}</td>
              <td className="small doc-cell" title={e.path}>{e.path.split("/").pop()}</td>
              <td className="small muted">{e.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Every change to the knowledge base: uploads, activations, rollbacks, archives, verifications, priorities.
function AuditPanel() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(async (offset = 0) => {
    try {
      const r = await api.kbAudit({ q, action, limit: 50, offset });
      setData((d) => (offset && d ? { ...r, events: [...d.events, ...r.events] } : r));
      setError("");
    } catch (e) { setError(e.message); }
  }, [q, action]);
  useEffect(() => { if (open) load(0); }, [open, load]);
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Change history</h2>
        <button className="btn btn-ghost btn-sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "Hide" : "Show"}</button>
      </div>
      <p className="hint">Every change to the knowledge base: who uploaded, activated, archived, rolled back, verified or re-prioritised what, and when.</p>
      {open && (
        <>
          <div className="filters">
            <input type="search" className="search" placeholder="Search documents, people or details" aria-label="Search history"
                   value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={action} aria-label="Filter by action" onChange={(e) => setAction(e.target.value)}>
              <option value="">All changes</option>
              {data && Object.entries(data.actions).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            {data && <span className="muted small">{data.total} change{data.total === 1 ? "" : "s"}</span>}
          </div>
          {error && <p className="err">{error}</p>}
          {data && <AuditList events={data.events} />}
          {data && data.events.length < data.total && (
            <button className="btn btn-ghost btn-sm more" onClick={() => load(data.events.length)}>Show more</button>
          )}
        </>
      )}
    </section>
  );
}

// Document details: the metadata used for filtering (bank, product, category, title), its source priority, the
// version history and the change history.
function DocumentDialog({ path, sourceTypes, sourceDefaults, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [preview, setPreview] = useState(null);
  const [history, setHistory] = useState(null);
  const ref = useRef(null);
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);
  const load = useCallback(() => api.kbDocument(path).then((r) => {
    setData(r);
    if (r.document) api.kbAudit({ doc_id: r.document.doc_id, limit: 30 }).then((a) => setHistory(a.events)).catch(() => setHistory([]));
    const d = r.document || r.suggested || {};
    setForm({ title: d.title || "", bank: d.bank || "", product: d.product || "", category: d.category || "" });
  }).catch((e) => setError(e.message)), [path]);
  useEffect(() => { load(); }, [load]);

  async function save(e) {
    e.preventDefault();
    setMsg(""); setError("");
    try { await api.kbUpdateDocument(path, form); setMsg("Saved. The document is being re-indexed with the new tags."); onChanged(); load(); }
    catch (err) { setError(err.message); }
  }
  async function activate(v) {
    setMsg(""); setError("");
    try {
      const r = await api.kbActivateVersion(path, v);
      setMsg(`Version ${v} is active again; the document is being re-indexed.`); onChanged();
      if (r.path !== path) onClose(); else load();
    }
    catch (err) { setError(err.message); }
  }
  async function compareWith(v) {
    setPreview({ version: v, comparing: true });
    try { setPreview({ version: v, comparing: true, ...(await api.kbVersionCompare(path, v)) }); }
    catch (err) { setPreview({ version: v, comparing: true, message: err.message }); }
  }
  async function show(v) {
    setPreview({ version: v, chunks: null });
    try { setPreview({ version: v, ...(await api.kbVersionPreview(path, v)) }); }
    catch (err) { setPreview({ version: v, message: err.message, chunks: [] }); }
  }
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <dialog ref={ref} className="dialog wide" onClose={onClose} onCancel={onClose} aria-labelledby="doc-title">
      <div className="dialog-body">
        <div className="dialog-head">
          <h2 id="doc-title" title={path}>{path.split("/").pop()}</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        {error && <p className="err" role="alert">{error}</p>}
        {msg && <p className="ok-text" role="status">{msg}</p>}
        {!data && !error && <p className="muted">Loading…</p>}
        {data && !data.processed && <p className="hint">{data.message}</p>}
        {data?.processed && form && (
          <>
            <h3 className="sub-h">Document metadata</h3>
            <p className="hint">Used to filter search results and shown with every passage. Leave bank or product empty for
              files that cover several (code books, RM lists).{data.document.meta_source === "auto" && " These values were inferred from the file name and text."}</p>
            <form onSubmit={save}>
              <div className="form-grid">
                <label>Title<input type="text" maxLength={120} value={form.title} onChange={set("title")} /></label>
                <label>Bank / lender<input type="text" maxLength={120} value={form.bank} onChange={set("bank")} placeholder="e.g. HDFC Bank" /></label>
                <label>Product<input type="text" list="kb-products" maxLength={120} value={form.product} onChange={set("product")} placeholder="e.g. Business Loan" />
                  <datalist id="kb-products">{LOAN_TYPES.map((t) => <option key={t} value={t} />)}</datalist></label>
                <label>Category<input type="text" maxLength={120} value={form.category} onChange={set("category")} placeholder="e.g. Policy, Payout, Codes" /></label>
              </div>
              <button className="btn btn-primary btn-sm" type="submit">Save and re-index</button>
            </form>

            <h3 className="sub-h">Source priority</h3>
            <p className="hint">When documents disagree, the one with the higher priority is used for the answer. Text read by OCR and not
              verified counts 20 lower, and a version past its effective-until date 30 lower.</p>
            <AuthorityForm key={`${data.document.source_type}-${data.document.priority}`} path={path} doc={data.document}
                           sourceTypes={sourceTypes} sourceDefaults={sourceDefaults} onSaved={() => { onChanged(); load(); }} />

            <h3 className="sub-h">Versions</h3>
            <p className="hint">Only the ACTIVE version is searched. Earlier versions are kept for audit and rollback.</p>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Version</th><th>Status</th><th>Effective</th><th>Added</th><th className="num">Size</th><th>Note</th><th /></tr></thead>
                <tbody>
                  {data.versions.map((v) => (
                    <tr key={v.version}>
                      <td><b>v{v.version}</b>{v.label && <span className="block small">{v.label}</span>}
                        {v.file && v.file !== path && <span className="block small muted" title={v.file}>{v.file.split("/").pop()}</span>}</td>
                      <td><span className={`tag ${VSTATUS[v.status] ?? ""}`}>{v.status}</span></td>
                      <td className="nowrap small">{period(v.effective_from, v.effective_until)}</td>
                      <td className="nowrap small">{when(v.created)}</td>
                      <td className="num nowrap">{size(v.size)}</td>
                      <td className="small muted">{v.note}</td>
                      <td className="actions">
                        <button className="link small" onClick={() => show(v.version)}>Text</button>
                        {v.status !== "ACTIVE" && <button className="link small" onClick={() => compareWith(v.version)}>Compare with active</button>}
                        {v.status !== "ACTIVE" && (v.restorable
                          ? <button className="link small" onClick={() => activate(v.version)}>Roll back to this</button>
                          : <span className="muted small" title="The original file was replaced outside the console, so only its text was kept.">Text only</span>)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h3 className="sub-h">Change history</h3>
            {!history ? <p className="muted">Loading…</p> : <AuditList events={history} />}
            {preview?.comparing && (
              <div className="version-preview">
                <h3 className="sub-h">Changes from v{preview.version} to the active version</h3>
                {!preview.comparison && !preview.message && <p className="muted">Comparing…</p>}
                {preview.message && <p className="hint">{preview.message}</p>}
                {preview.comparison && <Comparison c={preview.comparison} />}
              </div>
            )}
            {preview && !preview.comparing && (
              <div className="version-preview">
                <h3 className="sub-h">Text of v{preview.version}{preview.status ? ` (${preview.status})` : ""}</h3>
                {!preview.chunks && <p className="muted">Loading…</p>}
                {preview.message && <p className="hint">{preview.message}</p>}
                {preview.chunks?.length > 0 && <ol className="chunks">{preview.chunks.map((c, i) => <li key={i}><pre>{c}</pre></li>)}</ol>}
              </div>
            )}
          </>
        )}
      </div>
    </dialog>
  );
}

const PSTATUS = {
  CHECKING: { label: "Checking", cls: "info" }, REVIEW: { label: "Needs review", cls: "warn" },
  DUPLICATE: { label: "Duplicate", cls: "warn" }, FAILED: { label: "Cannot read", cls: "off" },
};
const DONE = {
  PUBLISHED: (p) => `Added as a new document (${p.result?.path || p.name})`,
  SEPARATE: (p) => `Added as a separate document (${p.result?.path || p.name})`,
  ACTIVATED: (p) => `Activated as v${p.result?.version} of ${p.result?.path || p.name}`,
  REJECTED: () => "Rejected",
};

function CheckList({ checks }) {
  return (
    <ul className="checks">
      {checks.map((ch, i) => {
        const cls = ch.ok ? (ch.info ? "info" : "ok") : ch.warn ? "warn" : "bad";
        return <li key={i} className={cls}><span aria-hidden="true">{cls === "bad" ? "✕" : cls === "warn" ? "!" : "✓"}</span>
          <b>{ch.check}</b><span>{ch.detail}</span></li>;
      })}
    </ul>
  );
}

const IMAGE_TYPES = ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif"];
const isImage = (name) => IMAGE_TYPES.includes((name.split(".").pop() || "").toLowerCase());
const EXTRACTION = {
  ocr: { label: "OCR · not verified", cls: "warn", tip: "Read by OCR, which can misread figures. Verify its text." },
  verified: { label: "Verified text", cls: "ok", tip: "An admin checked the text; it is used instead of the OCR output." },
};

// Check the OCR text of an image-based document against its page images, tick every flagged item, correct the text
// and save it as verified text. The server flags low-confidence and dropped OCR boxes, suspicious figures, empty
// pages and figures an independent second reading disagrees on; saving needs every item ticked.
// target: { path } for a document in the knowledge base, or { pendingId, name } for an upload under review.
const ITEM_KINDS = {
  only_ocr: { label: "Readings differ", cls: "bad", tip: "In the OCR text, but the second reading does not have it" },
  only_second: { label: "Readings differ", cls: "bad", tip: "In the second reading, but not in the OCR text" },
  suspicious: { label: "Looks misread", cls: "bad", tip: "A figure shape OCR often gets wrong" },
  dropped: { label: "Left out", cls: "bad", tip: "OCR was too unsure to put this in the text" },
  low_confidence: { label: "Low confidence", cls: "warn", tip: "OCR was not sure of this text" },
  empty_page: { label: "No text", cls: "warn", tip: "OCR found no text on this page" },
  page_error: { label: "Not checked", cls: "bad", tip: "This page could not be checked automatically" },
  page: { label: "Whole page", cls: "info", tip: "Confirm you compared the whole page with the original" },
};
const headsOf = (text) => [...text.matchAll(/^=== (.{1,80}?) ===[ \t]*$/gm)];

// The edited text split into sections: [{ label, body }] (label null for text before the first header).
function sections(text) {
  const heads = headsOf(text);
  if (!heads.length) return [{ label: null, body: text.trim() }];
  const out = [];
  const lead = text.slice(0, heads[0].index).trim();
  if (lead) out.push({ label: null, body: lead });
  heads.forEach((m, i) => out.push({ label: m[1].trim(),
    body: text.slice(m.index + m[0].length, i + 1 < heads.length ? heads[i + 1].index : text.length).trim() }));
  return out;
}
const pageNo = (label) => Number((/^Page (\d+)$/.exec(label || "") || [])[1] || 0);

// Replace (or insert, in page order) one page's section of the edited text.
function withPage(text, label, body, single) {
  if (single) return body;
  const secs = sections(text).filter((s) => s.label !== null || s.body);
  const at = secs.findIndex((s) => s.label === label);
  if (at >= 0) secs[at] = { label, body };
  else {
    const after = secs.findIndex((s) => pageNo(s.label) > pageNo(label));
    secs.splice(after < 0 ? secs.length : after, 0, { label, body });
  }
  return secs.map((s) => (s.label === null ? s.body : `=== ${s.label} ===\n${s.body}`)).join("\n\n");
}

function PageView({ url, page, items, selected, onSelect, showAll }) {
  const flagged = new Map(items.filter((it) => it.box != null).map((it) => [it.box, it]));
  return (
    <div className="page-view">
      <img src={url} alt={`${page.label} of the original`} />
      {page.width > 0 && (
        <svg viewBox={`0 0 ${page.width} ${page.height}`} preserveAspectRatio="none" aria-hidden="true">
          {page.boxes.map((b, i) => {
            const it = flagged.get(i);
            if (!it && !showAll) return null;
            const [x0, y0, x1, y1] = b.box;
            const cls = it ? `box ${ITEM_KINDS[it.kind]?.cls || "warn"}${selected === it.id ? " sel" : ""}` : "box plain";
            return <rect key={i} className={cls} x={x0 - 3} y={y0 - 3} width={x1 - x0 + 6} height={y1 - y0 + 6}
                         onClick={it ? () => onSelect(it.id) : undefined}><title>{`${b.text} · ${Math.round(b.conf * 100)}%`}</title></rect>;
          })}
        </svg>
      )}
    </div>
  );
}

function VerifyDialog({ target, onClose, onSaved }) {
  const [data, setData] = useState(null);
  const [text, setText] = useState("");
  const [checked, setChecked] = useState(() => new Set());
  const [pageIdx, setPageIdx] = useState(0);
  const [selected, setSelected] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState("");
  const [problem, setProblem] = useState(null);      // { code, message, warnings? } from a refused save
  const [saved, setSaved] = useState(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);
  const baseAt = useRef(null);                       // verified-at when the text was loaded (stale-save guard)
  const name = target.name || target.path;
  const fileUrl = target.pendingId ? api.kbPendingFileUrl(target.pendingId) : api.kbDownloadUrl(target.path);
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);

  const served = useRef("");                         // the text the server last sent (to tell untouched edits)
  const load = useCallback((first) => api.kbVerifyGet(target).then((r) => {
    setData(r);
    if (first) baseAt.current = r.verified?.at || null;
    // First load, or the file was extracted while its checks ran: show the new text unless the admin edited it.
    setText((t) => (first || t === served.current ? r.text : t));
    served.current = r.text;
  }).catch((e) => setError(e.message)), [target]);
  useEffect(() => { load(true); }, [load]);
  const running = data?.review.status === "RUNNING";
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => load(false), 2000);
    return () => clearInterval(t);
  }, [running, load]);

  const review = data?.review;
  const pages = review?.pages || [];
  const page = pages[Math.min(pageIdx, pages.length - 1)];
  const items = review?.items || [];
  const pageItems = page ? items.filter((it) => it.page === page.page) : [];
  const open = items.filter((it) => !checked.has(it.id));
  const single = !!data && data.scope !== "pages" && pages.length <= 1 && headsOf(text).length === 0;

  const toggle = (id) => setChecked((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const tickPage = () => setChecked((s) => new Set([...s, ...pageItems.map((it) => it.id)]));
  function select(id) {
    setSelected(id);
    document.getElementById(`vf-item-${id}`)?.scrollIntoView({ block: "nearest" });
  }

  async function save(acceptWarnings = false) {
    setBusy(true); setError(""); setProblem(null);
    try {
      const r = await api.kbVerifySave(target, text, { checked: [...checked], baseAt: baseAt.current, acceptWarnings });
      setSaved(r); onSaved();
      const fresh = await api.kbVerifyGet(target);
      setData(fresh); baseAt.current = fresh.verified?.at || null; served.current = fresh.text;
    } catch (e) {
      if (e.data?.code) setProblem({ ...e.data, message: e.message });
      else setError(e.message);
      if (e.data?.code === "unchecked") {
        const first = items.find((it) => e.data.items.includes(it.id));
        if (first) { setPageIdx(Math.max(0, pages.findIndex((p) => p.page === first.page))); select(first.id); }
      }
    } finally { setBusy(false); }
  }
  async function unverify() {
    setBusy(true); setError("");
    try { await api.kbVerifyRemove(target); onSaved(); onClose(); }
    catch (e) { setError(e.message); setBusy(false); }
  }
  async function recheck() {
    setError("");
    try { await api.kbVerifyRecheck(target); setChecked(new Set()); load(false); }
    catch (e) { setError(e.message); }
  }
  async function reload() { setProblem(null); setSaved(null); setChecked(new Set()); await load(true); }
  async function loadFile(e) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) setText(await f.text());
  }

  return (
    <dialog ref={ref} className="dialog wide verify" onClose={onClose} onCancel={onClose} aria-labelledby="vf-title">
      <div className="dialog-body">
        <div className="dialog-head">
          <h2 id="vf-title" title={name}>Verify text · {name.split("/").pop()}</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <p className="hint">Check each flagged item against the page image and tick it, correct the text, then save. The verified
          text is then used for answers instead of the OCR output.{data?.scope === "pages"
            ? " Only the scanned pages of this PDF are edited here; its other pages keep their own text."
            : ""} Keep the “=== Page N ===” lines so page numbers are kept. Markdown tables become table rows.</p>
        {!data && !error && <p className="muted">Loading…</p>}
        {error && <p className="err" role="alert">{error}</p>}
        {data && (
          <div className="verify-grid">
            <div className="verify-original">
              {pages.length > 1 && (
                <div className="page-tabs" role="tablist" aria-label="Pages">
                  {pages.map((p, i) => {
                    const left = items.filter((it) => it.page === p.page && !checked.has(it.id)).length;
                    return <button key={p.page} role="tab" aria-selected={i === pageIdx} className={`chip${i === pageIdx ? " on" : ""}`}
                                   onClick={() => setPageIdx(i)}>{p.label}{left > 0 && <span className="count">{left}</span>}</button>;
                  })}
                </div>
              )}
              {page && !page.error
                ? <PageView url={api.kbVerifyPageUrl(target, page.page)} page={page} items={pageItems} selected={selected}
                            onSelect={select} showAll={showAll} />
                : isImage(name) ? <div className="page-view"><img src={fileUrl} alt={`Original: ${name}`} /></div>
                : <p><a className="link" href={fileUrl} target="_blank" rel="noreferrer">Open the original file</a></p>}
              <div className="row-gap small">
                {page?.boxes.length > 0 && <label className="check-inline"><input type="checkbox" checked={showAll}
                  onChange={(e) => setShowAll(e.target.checked)} /> Show every OCR box</label>}
                <a className="link" href={fileUrl} target="_blank" rel="noreferrer">Original file</a>
              </div>
            </div>
            <div className="verify-edit">
              {data.verified
                ? <p className="ok-text small">Verified by {data.verified.by} · {when(data.verified.at)}
                    {data.verified.checked?.items ? ` · ${data.verified.checked.items} flagged item(s) checked` : ""}</p>
                : !data.ocr_available && <p className="hint">OCR could not read this file. Type or paste its text, or load it from a .txt file.</p>}

              <div className={`review-status ${running ? "info" : review.status === "FAILED" ? "bad" : open.length ? "warn" : "ok"}`} role="status">
                <span>
                  {running ? <><span className="spinner" aria-hidden="true" /> Checking the pages{review.reader ? ` (second reading by ${review.reader})` : ""}…</>
                    : review.status === "FAILED" ? <>The checks failed: {review.error}. Check the whole text against the original.</>
                    : review.status === "OFF" ? <>Automatic checks are off. Check the whole text against the original.</>
                    : items.length === 0 ? <>No flagged items.{review.reader ? ` OCR and the second reading (${review.reader}) agree on every figure.` : ""}</>
                    : <>{items.length - open.length} of {items.length} flagged items checked</>}
                </span>
                {!running && review.status !== "OFF" && <button className="link small" onClick={recheck}>Run checks again</button>}
              </div>
              {review.status === "READY" && !review.reader && (
                <p className="hint small">No second reader is configured, so figures are not cross-checked. Set OPENAI_API_KEY
                  (or VISION_MODEL on Groq) and run the checks again.</p>
              )}
              {page?.second_error && <p className="hint small">Second reading of {page.label}: {page.second_error}</p>}

              {pageItems.length > 0 && (
                <div className="review-items">
                  <div className="row-gap small">
                    <b>{page.label}: {pageItems.filter((it) => !checked.has(it.id)).length} to check</b>
                    <button className="link small" onClick={tickPage}>I checked all of these</button>
                  </div>
                  <ul>
                    {pageItems.map((it) => {
                      const k = ITEM_KINDS[it.kind] || { label: it.kind, cls: "warn" };
                      return (
                        <li key={it.id} id={`vf-item-${it.id}`} className={`${selected === it.id ? "sel" : ""}${checked.has(it.id) ? " done" : ""}`}>
                          <input type="checkbox" aria-label={`Checked: ${it.text}`} checked={checked.has(it.id)} onChange={() => toggle(it.id)} />
                          <button className="review-item" onClick={() => setSelected(it.id)}>
                            <span className={`tag ${k.cls}`} title={k.tip}>{k.label}</span> <code>{it.text}</code>
                            <span className="muted small"> {it.detail}</span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              {page?.second != null && (
                <details className="second-reading">
                  <summary>Second reading of {page.label}{review.reader ? ` (${review.reader})` : ""}</summary>
                  <pre>{page.second}</pre>
                  <button className="btn btn-ghost btn-sm" onClick={() => setText((t) => withPage(t, page.label, page.second, single))}>
                    Use it as the text of {single ? "this file" : page.label}</button>
                </details>
              )}

              <label className="sr-only" htmlFor="vf-text">Verified text</label>
              <textarea id="vf-text" className="verify-text" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} />

              {problem && (
                <div className={`review-problem ${problem.code === "warnings" ? "warn" : "bad"}`} role="alert">
                  <p>{problem.message}</p>
                  {problem.warnings?.length > 0 && <ul>{problem.warnings.map((w, i) => <li key={i}><code>{w.text}</code> {w.detail}</li>)}</ul>}
                  {problem.code === "warnings" && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => save(true)}>These are correct: save anyway</button>}
                  {problem.code === "conflict" && <button className="btn btn-ghost btn-sm" onClick={reload}>Reload (your edits are lost)</button>}
                </div>
              )}
              <div className="row-gap">
                <button className="btn btn-primary btn-sm" disabled={busy || running || !text.trim() || open.length > 0}
                        title={open.length ? `${open.length} flagged item(s) still to check` : undefined} onClick={() => save(false)}>
                  {busy ? "Saving…" : open.length ? `Save verified text (${open.length} to check)` : "Save verified text"}</button>
                <label className="btn btn-ghost btn-sm">Load from .txt<input type="file" accept=".txt,.md,text/plain" hidden onChange={loadFile} /></label>
                {data.ocr_available && text !== data.ocr_text && <button className="btn btn-ghost btn-sm" onClick={() => setText(data.ocr_text)}>Reset to OCR text</button>}
                {data.verified && <button className="btn btn-ghost btn-sm danger-text" disabled={busy} onClick={unverify}>Remove verified text</button>}
              </div>
              {saved && (
                <div className="comparison" role="status">
                  <p className="ok-text">Saved. {target.pendingId ? "The upload is being checked again." : "The document is being re-indexed from the verified text."}</p>
                  {(saved.changes.removed.length > 0 || saved.changes.added.length > 0) ? (
                    <div className="fig-diff">
                      {saved.changes.removed.length > 0 && <p><span className="muted small">OCR values corrected: </span>{saved.changes.removed.map((f, i) => <del key={i}>{f}</del>)}</p>}
                      {saved.changes.added.length > 0 && <p><span className="muted small">Verified values: </span>{saved.changes.added.map((f, i) => <ins key={i}>{f}</ins>)}</p>}
                    </div>
                  ) : <p className="hint">No figures changed from the OCR text.</p>}
                </div>
              )}
              {data.history.length > 0 && (
                <details>
                  <summary className="small">Earlier verified texts ({data.history.length})</summary>
                  <ul className="row-list small">
                    {data.history.map((h, i) => (
                      <li key={i}>{h.action === "removed" ? "Removed" : "Replaced"} by {h.replaced_by || "—"} · {when(h.replaced_at)}
                        <span className="muted"> (verified by {h.by} · {when(h.at)}{h.changes?.added?.length ? `; set ${h.changes.added.join(", ")}` : ""})</span></li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          </div>
        )}
      </div>
    </dialog>
  );
}

// What changed between two versions: changed table cells, added / removed rows, changed text, changed figures.
function Comparison({ c }) {
  const s = c.summary;
  if (s.identical) return <p className="hint">No differences found: the text of both versions is the same.</p>;
  const parts = [[s.rows_changed, "row(s) changed"], [s.rows_added, "added"], [s.rows_removed, "removed"],
                 [s.text_changes, "text change(s)"]].filter(([n]) => n).map(([n, t]) => `${n} ${t}`);
  return (
    <div className="comparison">
      <p className="hint">{parts.join(" · ") || "Only figures changed"}</p>
      {(c.figures.added.length > 0 || c.figures.removed.length > 0) && (
        <div className="fig-diff">
          {c.figures.removed.length > 0 && <p><span className="muted small">Only in the old version: </span>
            {c.figures.removed.map((f, i) => <del key={i}>{f}</del>)}</p>}
          {c.figures.added.length > 0 && <p><span className="muted small">Only in the new version: </span>
            {c.figures.added.map((f, i) => <ins key={i}>{f}</ins>)}</p>}
        </div>
      )}
      {c.tables.map((t, ti) => (
        <div key={ti} className="table-diff">
          <h4>{t.sheet || "Table"}{t.key.length > 0 && <span className="muted small"> · rows matched on {t.key.join(" + ")}</span>}</h4>
          {t.changed.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Row</th><th>Field</th><th>Old</th><th>New</th></tr></thead>
                <tbody>
                  {t.changed.flatMap((r, ri) => r.cells.map((cell, ci) => (
                    <tr key={`${ri}-${ci}`}>
                      <td>{ci === 0 ? <b>{r.key}</b> : ""}</td><td>{cell.header}</td>
                      <td><del>{cell.old || "—"}</del></td><td><ins>{cell.new || "—"}</ins></td>
                    </tr>
                  )))}
                </tbody>
              </table>
            </div>
          )}
          {t.changed_total > t.changed.length && <p className="hint">…and {t.changed_total - t.changed.length} more changed rows.</p>}
          {t.added.length > 0 && <details><summary>{t.added_total} row(s) added</summary>
            <ul className="row-list">{t.added.map((r, i) => <li key={i}><ins>{r.row}</ins></li>)}</ul></details>}
          {t.removed.length > 0 && <details><summary>{t.removed_total} row(s) removed</summary>
            <ul className="row-list">{t.removed.map((r, i) => <li key={i}><del>{r.row}</del></li>)}</ul></details>}
        </div>
      ))}
      {c.text.length > 0 && (
        <details open={c.tables.length === 0}>
          <summary>{s.text_changes} text change(s)</summary>
          <ol className="text-diff">
            {c.text.map((h, i) => (
              <li key={i}>
                {h.old.map((l, j) => <del key={`o${j}`} className="block">{l}</del>)}
                {h.new.map((l, j) => <ins key={`n${j}`} className="block">{l}</ins>)}
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}

function TestResults({ tests }) {
  if (!tests.total) return <p className="hint">No knowledge tests are linked to this document. Add some in the RAG quality tab
    (set its expected document) to check new versions automatically.</p>;
  return (
    <ul className="checks">
      {tests.tests.map((t) => {
        const cls = t.status === "PASS" ? "ok" : t.status === "FAIL" ? "bad" : "info";
        return <li key={t.id} className={cls}><span aria-hidden="true">{cls === "bad" ? "✕" : "✓"}</span>
          <b>{t.question}</b><span>{t.expected ? `Expected “${t.expected}”: ` : ""}{t.detail}</span></li>;
      })}
    </ul>
  );
}

// One staged upload: what the checks found, which document it may update, and the admin's decision.
function ReviewCard({ item, documents, docTypes, sourceTypes, onDone, onPreview, onVerify }) {
  const cands = item.candidates || [];
  const [choice, setChoice] = useState(cands.length ? cands[0].doc_id : item.status === "DUPLICATE" ? "" : "new");
  const [other, setOther] = useState("");
  const [form, setForm] = useState({ title: item.title || "", category: item.category || "", label: item.label || "", source_type: "",
                                     effective_from: item.effective_from || "", effective_until: item.effective_until || "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [review, setReview] = useState(null);       // {docId, data | error}
  const [blocked, setBlocked] = useState(null);     // failing knowledge tests returned by an activation attempt
  const st = PSTATUS[item.status] || { label: item.status, cls: "" };
  const docId = choice === "other" ? other : choice === "new" ? "" : choice;
  const target = documents.find((d) => d.doc_id === docId);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  async function decide(action, force = false) {
    setBusy(true); setError(""); setBlocked(null);
    try {
      const body = action === "reject" ? { action } : { action, doc_id: action === "version" ? docId : null, force,
        ...form, effective_from: form.effective_from || null, effective_until: form.effective_until || null };
      await api.kbDecide(item.id, body);
      onDone();
    } catch (e) {
      if (e.code === "tests_failed") setBlocked(e.data.tests); else setError(e.message);
      setBusy(false);
    }
  }
  async function compare() {
    if (review?.docId === docId) { setReview(null); return; }
    setReview({ docId, data: null });
    try { setReview({ docId, data: await api.kbPendingReview(item.id, docId) }); }
    catch (e) { setReview({ docId, error: e.message }); }
  }

  return (
    <article className="review-card" aria-label={item.name}>
      <div className="review-head">
        <b className="fname" title={item.name}>{item.name}</b>
        <span className={`tag ${st.cls}`}>{st.label}</span>
        <span className="muted small">{size(item.size)} · {when(item.created)}{item.by ? ` · ${item.by}` : ""}</span>
      </div>
      {item.status === "CHECKING" && <p className="hint">Reading the file and looking for existing versions…</p>}
      {item.checks?.length > 0 && <CheckList checks={item.checks} />}
      {item.status === "FAILED" && <p className="err">{item.error}</p>}

      {(item.status === "REVIEW" || item.status === "DUPLICATE") && (
        <>
          {item.status === "DUPLICATE" && (
            <p className="warn-box">This exact file is already in the knowledge base. Usually this upload should be cancelled.</p>
          )}
          <fieldset className="choices">
            <legend>What is this file?</legend>
            {cands.map((c) => (
              <label key={c.doc_id} className="choice">
                <input type="radio" name={`c-${item.id}`} checked={choice === c.doc_id} onChange={() => setChoice(c.doc_id)} />
                <span>New version of <b>{c.title}</b> <span className="muted">(now {c.label || `v${c.version}`})</span>
                  <small className="block muted">{c.reasons.join(" · ")}</small></span>
              </label>
            ))}
            <label className="choice">
              <input type="radio" name={`c-${item.id}`} checked={choice === "other"} onChange={() => setChoice("other")} />
              <span>New version of another document
                {choice === "other" && (
                  <select className="block" value={other} onChange={(e) => setOther(e.target.value)} aria-label="Document">
                    <option value="">Choose…</option>
                    {documents.map((d) => <option key={d.doc_id} value={d.doc_id}>{d.title}{d.label ? ` (${d.label})` : ""}</option>)}
                  </select>
                )}</span>
            </label>
            <label className="choice">
              <input type="radio" name={`c-${item.id}`} checked={choice === "new"} onChange={() => setChoice("new")} />
              <span>A separate new document</span>
            </label>
          </fieldset>

          {target && (
            <p className="compare-line">
              <span className="muted">Current</span> <b>{target.label || `v${target.version}`}</b>
              <span aria-hidden="true"> → </span>
              <span className="muted">New</span> <b>{form.label || "this upload"}</b>
              <span className="muted small"> · the current version will be archived and kept for rollback</span>
            </p>
          )}
          <div className="form-grid">
            <label>Document name<input type="text" maxLength={120} value={form.title} onChange={set("title")} /></label>
            <label>Document type
              <select value={form.category} onChange={set("category")}>
                {!docTypes.includes(form.category) && form.category && <option value={form.category}>{form.category}</option>}
                {docTypes.map((t) => <option key={t} value={t}>{t}</option>)}
              </select></label>
            <label>Source type
              <select value={form.source_type} onChange={set("source_type")}>
                <option value="">{target ? "Keep the document's" : `Default for “${form.category || "General"}”`}</option>
                {sourceTypes.map((s) => <option key={s.key} value={s.key}>{s.label} ({s.priority})</option>)}
              </select></label>
            <label>Version label<input type="text" maxLength={60} value={form.label} onChange={set("label")} placeholder="e.g. August 2026" /></label>
            <label>Effective from<input type="date" value={form.effective_from} onChange={set("effective_from")} /></label>
            <label>Effective until<input type="date" value={form.effective_until} onChange={set("effective_until")} /></label>
          </div>
          {review?.docId === docId && (
            <div className="review-detail">
              {!review.data && !review.error && <p className="muted">Comparing…</p>}
              {review.error && <p className="err">{review.error}</p>}
              {review.data && (
                <>
                  <h3 className="sub-h">Changes from {review.data.current?.label || `v${review.data.current?.version}`} to {form.label || "this upload"}</h3>
                  <Comparison c={review.data.comparison} />
                  <h3 className="sub-h">Knowledge tests</h3>
                  <TestResults tests={review.data.tests} />
                </>
              )}
            </div>
          )}
          {error && <p className="err" role="alert">{error}</p>}
          {blocked && (
            <div className="warn-box" role="alert">
              <b>Do not activate yet: {blocked.length} knowledge test{blocked.length === 1 ? "" : "s"} failed.</b>
              <ul>{blocked.map((t) => <li key={t.id}>{t.question} (expected “{t.expected}”): {t.detail}</li>)}</ul>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => decide("version", true)}>Activate anyway</button>
            </div>
          )}
          <div className="row-gap">
            {item.status === "DUPLICATE" && (
              <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => decide("reject")}>Cancel upload</button>
            )}
            {choice === "new"
              ? <button className={`btn btn-sm ${item.status === "DUPLICATE" ? "btn-ghost" : "btn-primary"}`} disabled={busy}
                        onClick={() => decide("separate")}>Add as new document</button>
              : <button className={`btn btn-sm ${item.status === "DUPLICATE" ? "btn-ghost" : "btn-primary"}`} disabled={busy || !docId}
                        onClick={() => decide("version")}>
                  {item.status === "DUPLICATE" ? "Use as new version anyway" : `Activate ${form.label || "new version"}`}</button>}
            {docId && <button className="btn btn-ghost btn-sm" aria-expanded={review?.docId === docId} onClick={compare}>
              {review?.docId === docId ? "Hide comparison" : "Compare versions"}</button>}
            {item.image ? <button className="btn btn-primary btn-sm" onClick={() => onVerify(item)}>Verify text</button> : null}
            <button className="btn btn-ghost btn-sm" onClick={() => onPreview(item)}>Preview text</button>
            {item.status !== "DUPLICATE" && <button className="btn btn-ghost btn-sm danger-text" disabled={busy} onClick={() => decide("reject")}>Reject</button>}
          </div>
        </>
      )}
      {item.status === "FAILED" && (
        <div className="row-gap">
          {isImage(item.name) && <button className="btn btn-primary btn-sm" onClick={() => onVerify(item)}>Type its text</button>}
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => decide("reject")}>Remove upload</button>
        </div>
      )}
    </article>
  );
}

function ReviewPanel({ items, documents, docTypes, sourceTypes, onDone, onPreview, onVerify }) {
  const open = items.filter((p) => PSTATUS[p.status]);
  const done = items.filter((p) => !PSTATUS[p.status]);
  const [showDone, setShowDone] = useState(false);
  if (!items.length) return null;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Review uploads <span className="count">{open.length}</span></h2>
        {done.length > 0 && <button className="btn btn-ghost btn-sm" aria-expanded={showDone} onClick={() => setShowDone((s) => !s)}>
          {showDone ? "Hide recent decisions" : "Recent decisions"}</button>}
      </div>
      <p className="hint">A new version is never searched until you activate it. Activating archives the current version
        (kept for rollback), so only one version of a document is ever active.</p>
      {open.length === 0 ? <p className="empty">Nothing waiting for review.</p>
        : open.map((p) => <ReviewCard key={p.id} item={p} documents={documents} docTypes={docTypes} sourceTypes={sourceTypes} onDone={onDone}
                                      onPreview={onPreview} onVerify={onVerify} />)}
      {showDone && (
        <ul className="decided">
          {done.map((p) => <li key={p.id}><b>{p.name}</b> <span className="muted">· {DONE[p.status]?.(p) || p.status}
            · {when(p.decided || p.updated)}{p.decided_by && p.decided_by !== "auto" ? ` · ${p.decided_by}` : ""}</span></li>)}
        </ul>
      )}
    </section>
  );
}

// Documents already in the knowledge base that look like versions of each other (Code Book-3, -4, -5).
function DuplicatesPanel({ onDone, ask }) {
  const [groups, setGroups] = useState(null);
  const [keep, setKeep] = useState({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  async function scan() {
    setBusy(true);
    try {
      const r = await api.kbDuplicates();
      setGroups(r.groups);
      setKeep(Object.fromEntries(r.groups.map((g, i) => [i, g.latest])));
    } catch (e) { setMsg(e.message); }
    finally { setBusy(false); }
  }
  async function merge(g, i) {
    const others = g.members.map((m) => m.path).filter((p) => p !== keep[i]);
    try {
      await api.kbMerge(keep[i], others);
      setMsg(`Kept ${keep[i].split("/").pop()}; ${others.length} file(s) became its older versions.`);
      onDone(); scan();
    } catch (e) { setMsg(e.message); }
  }
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Possible duplicates</h2>
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => { setMsg(""); scan(); }}>
          {busy ? "Scanning…" : groups ? "Scan again" : "Scan the knowledge base"}</button>
      </div>
      <p className="hint">Finds documents that look like versions of the same thing, compared by name, type, bank and content.
        Keep the current one; the others leave the folder and become its archived versions (they can be rolled back).</p>
      {msg && <p className="hint" role="status">{msg}</p>}
      {groups && (groups.length === 0 ? <p className="empty">No likely duplicates found.</p> : groups.map((g, i) => (
        <article key={g.members.map((m) => m.doc_id).join()} className="review-card">
          <p className="small muted">{g.reasons.join(" · ")}</p>
          <fieldset className="choices">
            <legend>Which one is current?</legend>
            {g.members.map((m) => (
              <label key={m.doc_id} className="choice">
                <input type="radio" name={`g-${i}`} checked={keep[i] === m.path} onChange={() => setKeep((k) => ({ ...k, [i]: m.path }))} />
                <span><b>{m.path.split("/").pop()}</b> <span className="muted small">{[m.label, m.category, size(m.size)].filter(Boolean).join(" · ")}</span></span>
              </label>
            ))}
          </fieldset>
          <button className="btn btn-primary btn-sm"
                  onClick={() => ask({ text: `Keep “${keep[i].split("/").pop()}” and make the other ${g.members.length - 1} its archived versions?`,
                                       label: "Merge", run: () => { ask(null); merge(g, i); } })}>
            Keep selected, archive the others as older versions</button>
        </article>
      )))}
    </section>
  );
}

function TestSearch() {
  const [q, setQ] = useState("");
  const [lt, setLt] = useState("");
  const [res, setRes] = useState(null);
  const [val, setVal] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [scope, setScope] = useState("active");
  async function run(e) {
    e.preventDefault();
    if (q.trim().length < 2) return;
    setBusy(true); setError("");
    try {
      if (scope === "history") {
        const r = await api.kbHistorySearch(q.trim());
        setRes(r.results.map((h) => ({ ...h, history: true })));
        setVal(null);
      } else {
        const r = await api.kbSearch(q.trim(), lt);
        setRes(r.results);
        setVal(r.trace?.validation || null);
      }
    }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return (
    <section className="panel">
      <h2>Test retrieval</h2>
      <p className="hint">Runs a question through the same retrieval the copilot uses (no answer is generated), so you can check
        that a document is found. For the full pipeline with the answer, scores and a diagnosis, use the RAG quality tab.</p>
      <form className="row-gap" onSubmit={run}>
        <input className="grow-input" type="search" placeholder="e.g. DSA code for Axis Bank home loan, or a PIN code" value={q}
               aria-label="Search question" onChange={(e) => setQ(e.target.value)} />
        <select value={scope} aria-label="Knowledge to search" onChange={(e) => { setScope(e.target.value); setRes(null); setVal(null); }}>
          <option value="active">Active knowledge</option>
          <option value="history">Historical (archived versions)</option>
        </select>
        {scope === "active" && (
          <select value={lt} aria-label="Loan type" onChange={(e) => setLt(e.target.value)}>
            <option value="">Any loan type</option>
            {LOAN_TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        )}
        <button className="btn btn-primary btn-sm" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
      </form>
      {error && <p className="err">{error}</p>}
      {val && <p className={val.sufficient ? "ok-text" : "err"}>{val.sufficient ? "Enough evidence to answer." : `Not enough evidence: ${val.reason}`}</p>}
      {scope === "history" && <p className="hint">Searches the text of archived and disabled versions by keywords. Users'
        answers never use these versions.</p>}
      {res && (res.length === 0 ? <p className="empty">Nothing found.</p> : (
        <ol className="hits">
          {res.map((h, i) => h.history ? (
            <li key={i}>
              <div className="hit-head">
                <b>{h.title}</b><span className="muted"> · v{h.version}{h.label ? ` · ${h.label}` : ""}{h.section ? ` · ${h.section}` : ""}</span>
                <span className="tag">{h.status}</span>
                {(h.effective_from || h.effective_until) && <span className="tag">{period(h.effective_from, h.effective_until)}</span>}
              </div>
              <small className="block muted">{h.file}</small>
              <pre>{h.text}</pre>
            </li>
          ) : (
            <li key={i}>
              <div className="hit-head">
                <b>{h.source}</b>{h.page && <span className="muted"> · Page {h.page}</span>}{h.section && <span className="muted"> · {h.section}</span>}
                <span className={`tag ${h.primary ? "ok" : ""}`}>{h.primary ? "Urban Money" : "Policy upload"}</span>
                <span className="tag">{h.match === "exact" ? "Exact match" : h.match === "keyword" ? "Keyword" : "Semantic"}</span>
                {h.rerank != null && <span className="tag" title="Reranker relevance (0-1)">relevance {h.rerank.toFixed(2)}</span>}
              </div>
              <pre>{h.text}</pre>
            </li>
          ))}
        </ol>
      ))}
    </section>
  );
}

export default function KnowledgeBase() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [folder, setFolder] = useState("*");
  const [sel, setSel] = useState(new Set());
  const [limit, setLimit] = useState(PAGE);
  const [confirm, setConfirm] = useState(null);       // {text, label, run}
  const [preview, setPreview] = useState(null);
  const [details, setDetails] = useState(null);
  const [verify, setVerify] = useState(null);         // {path} or {pendingId, name}
  const [showUpload, setShowUpload] = useState(false);
  const [showArchive, setShowArchive] = useState(false);

  const load = useCallback(async () => {
    try { setData(await api.kb()); setError(""); }
    catch (e) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const checking = data?.pending.some((p) => p.status === "CHECKING");
  const busy = data?.jobs.busy || checking;
  useEffect(() => {                                  // follow background processing until it finishes
    if (!busy) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [busy, load]);

  const files = data?.files || [];
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return files.filter((f) => matches(f, status) && (folder === "*" || f.folder === folder) &&
      (!needle || f.path.toLowerCase().includes(needle)));
  }, [files, q, status, folder]);
  useEffect(() => { setLimit(PAGE); }, [q, status, folder]);
  useEffect(() => {                                  // drop selections that disappeared
    setSel((s) => new Set([...s].filter((p) => files.some((f) => f.path === p))));
  }, [files]);

  async function act(fn, done) {
    setMsg(""); setConfirm(null);
    try {
      const r = await fn();
      const errs = r?.errors?.map((e) => e.error) || [];
      setMsg([done, ...errs].filter(Boolean).join(" "));
      setSel(new Set());
    } catch (e) { setMsg(e.message); }
    load();
  }

  const selected = [...sel];
  const allShownSelected = shown.length > 0 && shown.slice(0, limit).every((f) => sel.has(f.path));
  const toggleAll = () => setSel(allShownSelected ? new Set() : new Set(shown.slice(0, limit).map((f) => f.path)));
  const toggle = (p) => setSel((s) => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n; });

  const c = data?.counts || {};
  const attention = (c.failed || 0) + (c.pending || 0) + (c.unsupported || 0) + (c.too_large || 0);
  const last = data?.jobs.last;
  const lastText = last && (last.error ? `Last job failed: ${last.error}` :
    `Last run ${when(last.finished)}: ${last.report.indexed} processed, ${last.report.removed} removed, ` +
    `${last.report.failed} failed${last.report.keyword_only ? `, ${last.report.keyword_only} keyword-only` : ""}.`);

  if (error && !data) return <section className="panel"><p className="err" role="alert">{error}</p></section>;
  if (!data) return <p className="muted">Loading documents…</p>;

  return (
    <div className="admin-section">
      <div className="stats">
        <div className="stat"><span>Documents</span><b>{files.length}</b></div>
        <div className="stat"><span>Searchable</span><b>{(c.indexed || 0) + (c.keyword || 0)}</b></div>
        <div className="stat"><span>Needs attention</span><b className={attention ? "warn-num" : ""}>{attention}</b></div>
        <div className="stat"><span>Sections indexed</span><b>{data.chunks.toLocaleString()}</b></div>
        <div className="stat"><span>Total size</span><b>{size(data.total_size)}</b></div>
        <div className="stat"><span>Vector store</span><b className="small-b">{data.store}</b></div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>Documents</h2>
          <button className="btn btn-primary btn-sm" onClick={() => setShowUpload((s) => !s)}>
            <Icon name="send" size={14} />{showUpload ? "Hide upload" : "Upload documents"}
          </button>
          <button className="btn btn-ghost btn-sm" disabled={busy}
                  onClick={() => act(() => api.kbSyncFolder(false), "Processing new and changed files.")}>
            Process new and changed
          </button>
          <button className="btn btn-ghost btn-sm" disabled={busy}
                  onClick={() => setConfirm({ text: "Re-read and re-index every document? With many scanned files this can take a long time.",
                                              label: "Rebuild everything", run: () => act(() => api.kbSyncFolder(true), "Full rebuild started.") })}>
            Rebuild everything
          </button>
        </div>
        <p className="hint folder-path" title={data.folder}>Folder: {data.folder}</p>

        <div className={`job-bar${busy ? " busy" : ""}`} role="status">
          {busy ? (
            <><span className="dots"><i /><i /><i /></span>
              <span>Processing{data.jobs.progress ? `: ${data.jobs.progress}` : "…"}
                {data.jobs.queued > 0 && ` (${data.jobs.queued} more job${data.jobs.queued === 1 ? "" : "s"} waiting)`}</span></>
          ) : <span>{lastText || "Up to date. Upload documents or use “Process new and changed” after adding files to the folder."}</span>}
        </div>
        {msg && <p className="hint" role="status">{msg}</p>}
      </section>

      {showUpload && <Uploader folders={data.folders} documents={data.documents} maxMb={data.max_upload_mb}
                               types={data.allowed_types} onDone={() => { load(); setMsg(""); }} />}

      <ReviewPanel items={data.pending} documents={data.documents} docTypes={data.doc_types} sourceTypes={data.source_types} onDone={load}
                   onPreview={(item) => setPreview({ path: item.name, load: () => api.kbPendingPreview(item.id) })}
                   onVerify={(item) => setVerify({ pendingId: item.id, name: item.name })} />

      <section className="panel">
        <div className="filters">
          <input type="search" className="search" placeholder="Search file names" aria-label="Search file names"
                 value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={status} aria-label="Filter by status" onChange={(e) => setStatus(e.target.value)}>
            {FILTERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <select value={folder} aria-label="Filter by folder" onChange={(e) => setFolder(e.target.value)}>
            <option value="*">All folders</option>
            <option value="">Top level</option>
            {data.folders.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
          <span className="muted small">{shown.length} of {files.length}</span>
        </div>

        {selected.length > 0 && (
          <div className="bulk-bar">
            <b>{selected.length} selected</b>
            <button className="btn btn-ghost btn-sm" onClick={() => act(() => api.kbProcess(selected), `Reprocessing ${selected.length} file(s).`)}>Reprocess</button>
            <button className="btn btn-ghost btn-sm" onClick={() => act(() => api.kbArchive(selected), `Archived ${selected.length} file(s).`)}>Archive</button>
            <button className="btn btn-ghost btn-sm danger-text"
                    onClick={() => setConfirm({ text: `Permanently delete ${selected.length} file(s) from the folder and the index?`,
                                                label: "Delete", run: () => act(() => api.kbDelete(selected), "Deleted.") })}>Delete</button>
            <button className="link small" onClick={() => setSel(new Set())}>Clear selection</button>
          </div>
        )}

        {shown.length === 0 ? <p className="empty">{files.length ? "No documents match these filters." : "No documents yet. Upload some to build the knowledge base."}</p> : (
          <div className="table-wrap">
            <table className="table kb-table">
              <thead>
                <tr>
                  <th className="sel"><input type="checkbox" aria-label="Select all shown" checked={allShownSelected} onChange={toggleAll} /></th>
                  <th>Document</th><th>Status</th><th className="num">Sections</th><th className="num">Size</th><th>Processed</th><th />
                </tr>
              </thead>
              <tbody>
                {shown.slice(0, limit).map((f) => {
                  const st = STATUS[f.status] || { label: f.status, cls: "" };
                  return (
                    <tr key={f.path} className={sel.has(f.path) ? "selected" : ""}>
                      <td className="sel"><input type="checkbox" aria-label={`Select ${f.name}`} checked={sel.has(f.path)} onChange={() => toggle(f.path)} /></td>
                      <td className="doc">
                        <button className="doc-name" title={f.path} onClick={() => setPreview({ path: f.path })}>{f.name}</button>
                        <small className="block muted">{f.type.toUpperCase()}{f.folder && ` · ${f.folder}`}
                          {f.version ? ` · v${f.version}${f.versions > 1 ? ` of ${f.versions}` : ""}` : ""}{f.label && ` · ${f.label}`}</small>
                        {(f.bank || f.product) && <small className="block doc-tags">{[f.bank, f.product].filter(Boolean).join(" · ")}</small>}
                        {f.source_label && <small className="block muted" title="Source priority: used when documents disagree">
                          {f.source_label} · priority {f.priority}{f.notes?.length ? ` (${f.notes.join("; ")})` : ""}</small>}
                        {f.error && <small className="block err-text" title={f.error}>{f.error}</small>}
                      </td>
                      <td><span className={`tag ${st.cls}`} title={st.tip}>{st.label}</span>
                        {EXTRACTION[f.extraction] && <span className={`tag block-tag ${EXTRACTION[f.extraction].cls}`}
                                                          title={EXTRACTION[f.extraction].tip}>{EXTRACTION[f.extraction].label}</span>}</td>
                      <td className="num">{f.chunks ?? ""}</td>
                      <td className="num nowrap">{size(f.size)}</td>
                      <td className="nowrap small">{when(f.processed)}</td>
                      <td className="actions">
                        <button className="link small" onClick={() => setPreview({ path: f.path })}>Preview</button>
                        <button className="link small" onClick={() => setDetails(f.path)}>Details</button>
                        {(EXTRACTION[f.extraction] || (f.status === "failed" && isImage(f.name))) &&
                          <button className="link small" onClick={() => setVerify({ path: f.path })}>Verify text</button>}
                        <a className="link small" href={api.kbDownloadUrl(f.path)} download>Download</a>
                        <button className="link small" onClick={() => act(() => api.kbProcess([f.path]), `Reprocessing ${f.name}.`)}>Reprocess</button>
                        <button className="link small" onClick={() => act(() => api.kbArchive([f.path]), `Archived ${f.name}.`)}>Archive</button>
                        <button className="link small danger-text"
                                onClick={() => setConfirm({ text: `Permanently delete “${f.name}”?`, label: "Delete",
                                                            run: () => act(() => api.kbDelete([f.path]), `Deleted ${f.name}.`) })}>Delete</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {shown.length > limit && (
          <button className="btn btn-ghost btn-sm more" onClick={() => setLimit((l) => l + PAGE)}>Show {Math.min(PAGE, shown.length - limit)} more</button>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Archive <span className="count">{data.archived.length}</span></h2>
          <button className="btn btn-ghost btn-sm" aria-expanded={showArchive} onClick={() => setShowArchive((s) => !s)}>
            {showArchive ? "Hide" : "Show"}
          </button>
        </div>
        <p className="hint">Archived documents are kept on disk but left out of the knowledge base. Restore one to use it again.</p>
        {showArchive && (data.archived.length === 0 ? <p className="empty">Nothing archived.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Document</th><th className="num">Size</th><th>Archived</th><th /></tr></thead>
              <tbody>
                {data.archived.map((f) => (
                  <tr key={f.path}>
                    <td><b>{f.name}</b>{f.folder && <small className="block muted">{f.folder}</small>}</td>
                    <td className="num nowrap">{size(f.size)}</td>
                    <td className="nowrap small">{when(f.modified)}</td>
                    <td className="actions">
                      <button className="link small" onClick={() => act(() => api.kbRestore([f.path]), `Restored ${f.name}.`)}>Restore</button>
                      <a className="link small" href={api.kbDownloadUrl(f.path, true)} download>Download</a>
                      <button className="link small danger-text"
                              onClick={() => setConfirm({ text: `Permanently delete archived “${f.name}”?`, label: "Delete",
                                                          run: () => act(() => api.kbDeleteArchived([f.path]), `Deleted ${f.name}.`) })}>Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </section>

      <DuplicatesPanel onDone={load} ask={setConfirm} />
      <AuditPanel />
      <TestSearch />
      {preview && <PreviewDialog path={preview.path} load={preview.load} onClose={() => setPreview(null)} />}
      {details && <DocumentDialog path={details} sourceTypes={data.source_types} sourceDefaults={data.source_defaults} onClose={() => setDetails(null)} onChanged={load} />}
      {verify && <VerifyDialog target={verify} onClose={() => setVerify(null)} onSaved={load} />}
      {confirm && <Confirm text={confirm.text} label={confirm.label} onYes={confirm.run} onNo={() => setConfirm(null)} />}
    </div>
  );
}
