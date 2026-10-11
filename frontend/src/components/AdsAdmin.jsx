import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { LOAN_TYPES } from "../format.js";

// Admin: sponsored cards shown under knowledge-base answers. Create, schedule, target, pause and measure them.

const EMPTY = { advertiser: "", title: "", body: "", link: "https://", cta: "Learn more", banks: "", products: "",
  keywords: "", starts: "", ends: "", priority: 0, active: true };
const list = (v) => (Array.isArray(v) ? v : String(v || "").split(",")).map((x) => x.trim()).filter(Boolean);
const when = (d) => (d ? new Date(d).toLocaleDateString(undefined, { dateStyle: "medium" }) : "");

function Preview({ f, image }) {
  return (
    <div className="ad-block preview">
      <p className="ad-lead">Preview</p>
      <div className="ad-card">
        <span className="ad-main">
          {image && <img className="ad-img" src={image} alt="" />}
          <span className="ad-text">
            <span className="ad-who"><span className="ad-logo">{(f.advertiser || "?").slice(0, 1).toUpperCase()}</span>{f.advertiser || "Advertiser"}</span>
            <b className="ad-title">{f.title || "Ad title"}</b>
            {f.body && <span className="ad-body">{f.body}</span>}
            <span className="ad-cta">{f.cta || "Learn more"}</span>
          </span>
        </span>
        <div className="ad-side"><span className="ad-badge">Ad</span></div>
      </div>
    </div>
  );
}

function AdForm({ initial, onSaved, onCancel }) {
  const [f, setF] = useState(() => initial?.id ? { ...EMPTY, ...initial, banks: initial.banks.join(", "),
    products: initial.products.join(", "), keywords: initial.keywords.join(", "), starts: initial.starts || "", ends: initial.ends || "" } : EMPTY);
  const [file, setFile] = useState(null);
  const [localImg, setLocalImg] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);
  useEffect(() => () => localImg && URL.revokeObjectURL(localImg), [localImg]);
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const body = { ...f, banks: list(f.banks), products: list(f.products), keywords: list(f.keywords),
                     starts: f.starts || null, ends: f.ends || null, priority: Number(f.priority) || 0 };
      delete body.id; delete body.stats; delete body.image_url; delete body.created; delete body.updated;
      let ad = await api.adminAdSave(body, initial?.id);
      if (file) ad = await api.adminAdImage(ad.id, file);
      onSaved(ad);
    } catch (x) { setErr(x.message); }
    finally { setBusy(false); }
  }
  return (
    <form className="panel inline-form" onSubmit={submit}>
      <h3>{initial?.id ? "Edit ad" : "New ad"}</h3>
      <div className="ad-form">
        <div className="form-grid">
          <label>Advertiser<input required maxLength={80} value={f.advertiser} onChange={set("advertiser")} placeholder="e.g. Axis Bank" /></label>
          <label>Button text<input maxLength={30} value={f.cta} onChange={set("cta")} /></label>
          <label className="span-2">Title<input required maxLength={90} value={f.title} onChange={set("title")} placeholder="Home loans from 8.5% for salaried buyers" /></label>
          <label className="span-2">Text<input maxLength={220} value={f.body} onChange={set("body")} placeholder="One line about the offer" /></label>
          <label className="span-2">Link<input required type="url" maxLength={500} value={f.link} onChange={set("link")} /></label>
          <label>Image (optional)
            <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp"
                   onChange={(e) => { const x = e.target.files[0]; setFile(x || null); setLocalImg(x ? URL.createObjectURL(x) : null); }} />
          </label>
          <label>Priority (0–100)<input type="number" min="0" max="100" value={f.priority} onChange={set("priority")} /></label>
          <label>Banks (comma separated)<input value={f.banks} onChange={set("banks")} placeholder="Axis Bank, HDFC Bank" /></label>
          <label>Products<input list="ad-products" value={f.products} onChange={set("products")} placeholder="Home Loan, LAP" />
            <datalist id="ad-products">{LOAN_TYPES.map((t) => <option key={t} value={t} />)}</datalist></label>
          <label className="span-2">Keywords (comma separated)<input value={f.keywords} onChange={set("keywords")} placeholder="balance transfer, top-up" /></label>
          <label>Starts<input type="date" value={f.starts} onChange={set("starts")} /></label>
          <label>Ends<input type="date" value={f.ends} onChange={set("ends")} /></label>
        </div>
        <Preview f={f} image={localImg || initial?.image_url} />
      </div>
      <p className="hint">Leave banks, products and keywords empty to show the ad to everyone. With targeting, it is shown only
        when a question is about those banks / products (both must match when you set both), or uses a keyword.</p>
      <label className="check"><input type="checkbox" checked={!!f.active} onChange={set("active")} /> Active</label>
      <div className="row-gap">
        <button className="btn btn-primary btn-sm" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onCancel}>Cancel</button>
      </div>
      {err && <p className="err" role="alert">{err}</p>}
    </form>
  );
}

function Report({ id }) {
  const [r, setR] = useState(null);
  useEffect(() => { api.adminAdReport(id).then(setR).catch(() => setR({ daily: [], reports: [] })); }, [id]);
  if (!r) return <p className="muted small">Loading…</p>;
  return (
    <div className="ad-report">
      {r.daily.length === 0 ? <p className="muted small">No activity in the last 30 days.</p> : (
        <table className="table">
          <thead><tr><th>Day</th><th className="num">Impressions</th><th className="num">Clicks</th><th className="num">Hidden</th><th className="num">Reports</th></tr></thead>
          <tbody>{r.daily.map((d) => (
            <tr key={d.day}><td>{d.day}</td><td className="num">{d.impression}</td><td className="num">{d.click}</td><td className="num">{d.hide}</td><td className="num">{d.report}</td></tr>
          ))}</tbody>
        </table>
      )}
      {r.reports.length > 0 && (
        <>
          <h4 className="sub-h">User reports</h4>
          <ul className="small">{r.reports.map((x, i) => <li key={i}>{new Date(x.ts).toLocaleString()} — {x.detail}</li>)}</ul>
        </>
      )}
    </div>
  );
}

export default function AdsAdmin() {
  const [data, setData] = useState(null);
  const [edit, setEdit] = useState(null);
  const [open, setOpen] = useState(null);
  const [msg, setMsg] = useState("");
  const load = useCallback(() => api.adminAds().then(setData).catch((e) => setMsg(e.message)), []);
  useEffect(() => { load(); }, [load]);
  async function act(fn) {
    setMsg("");
    try { await fn(); load(); } catch (e) { setMsg(e.message); }
  }
  if (!data) return msg ? <p className="err">{msg}</p> : <p className="muted">Loading…</p>;
  return (
    <div className="admin-section">
      <section className="panel">
        <div className="panel-head">
          <h2>Ads <span className="count">{data.ads.length}</span></h2>
          <button className="btn btn-primary btn-sm" onClick={() => setEdit({})}>New ad</button>
        </div>
        <p className="hint">Sponsored cards appear under “Ask anything” answers, at most one every {data.every_n_answers} answers per
          user{data.enabled ? "" : " (ads are switched off: ADS_ENABLED=false)"}. They never appear with lender recommendations or
          “not found” replies, and never change an answer. Have lender ad copy checked before it runs: rates and offers must be exact.</p>
        {msg && <p className="err" role="alert">{msg}</p>}
        {edit && <AdForm initial={edit} onSaved={() => { setEdit(null); load(); }} onCancel={() => setEdit(null)} />}
        {data.ads.length === 0 ? <p className="empty">No ads yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Ad</th><th>Targeting</th><th>Schedule</th><th className="num">Impr.</th><th className="num">Clicks</th>
                <th className="num">CTR</th><th className="num">Reports</th><th /></tr></thead>
              <tbody>
                {data.ads.map((a) => (
                  <Fragment key={a.id}>
                    <tr className={a.active ? "" : "inactive"}>
                      <td>
                        <b>{a.title}</b><small className="block muted">{a.advertiser}{!a.active && " · paused"}</small>
                      </td>
                      <td className="small">{[...a.banks, ...a.products, ...a.keywords.map((k) => `“${k}”`)].join(", ") || <span className="muted">Everyone</span>}</td>
                      <td className="small nowrap">{a.starts || a.ends ? `${when(a.starts) || "now"} – ${when(a.ends) || "open"}` : <span className="muted">Always</span>}</td>
                      <td className="num">{a.stats.impressions}</td>
                      <td className="num">{a.stats.clicks}</td>
                      <td className="num">{a.stats.ctr}%</td>
                      <td className="num">{a.stats.reports ? <span className="tag off">{a.stats.reports}</span> : 0}</td>
                      <td className="actions">
                        <button className="link small" onClick={() => setOpen(open === a.id ? null : a.id)}>{open === a.id ? "Hide report" : "Report"}</button>
                        <button className="link small" onClick={() => setEdit(a)}>Edit</button>
                        <button className="link small" onClick={() => act(() => api.adminAdActive(a.id, !a.active))}>{a.active ? "Pause" : "Resume"}</button>
                        <button className="link small danger-text" onClick={() => act(() => api.adminAdDelete(a.id))}>Delete</button>
                      </td>
                    </tr>
                    {open === a.id && <tr className="row-detail"><td colSpan={8}><Report id={a.id} /></td></tr>}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
