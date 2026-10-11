import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { LOAN_TYPES } from "../format.js";
import Markdown from "./Markdown.jsx";

// Admin: RAG quality. Shows exactly how each answer was produced (question understanding, every retrieved
// passage with its scores, filtering, reranking, the evidence check, prompt, model, answer, citations), so a
// wrong answer can be traced to the stage that caused it: extraction, chunking, retrieval, filtering,
// reranking, context, prompt or generation.

const SECTIONS = [
  ["test", "Test a question"], ["log", "Question log"], ["feedback", "Feedback"], ["golden", "Golden tests"], ["config", "Configuration"],
];
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
const num = (x, d = 3) => (x == null ? "–" : Number(x).toFixed(d));
const VERDICT = { GOOD: "ok", CORRECT: "ok", PARTIAL: "warn", MISSED: "off", WRONG: "off", NONE: "warn", SKIPPED: "info", "n/a": "" };

function Verdict({ label, value }) {
  return <span className={`tag ${VERDICT[value] ?? ""}`} title={label}>{label}: {value}</span>;
}

function Collapse({ title, children, open = false }) {
  return (
    <details className="trace-sec" open={open}>
      <summary>{title}</summary>
      <div className="trace-body">{children}</div>
    </details>
  );
}

function Diagnosis({ stages }) {
  if (!stages?.length) return null;
  return (
    <ol className="diagnosis">
      {stages.map((s, i) => (
        <li key={i} className={`stage-${s.status}`}>
          <b>{s.stage}</b> <span className={`tag ${s.status === "ok" ? "ok" : s.status === "problem" ? "off" : ""}`}>{s.status}</span>
          <div className="small">{s.detail}</div>
        </li>
      ))}
    </ol>
  );
}

function Candidates({ rows }) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState(null);
  const shown = all ? rows : rows.slice(0, 15);
  return (
    <>
      <div className="table-wrap">
        <table className="table trace-table">
          <thead>
            <tr><th>#</th><th>Passage</th><th className="num" title="Cosine similarity">Vector</th>
              <th className="num" title="BM25 keyword score">Keyword</th><th title="Found by">Found by</th>
              <th className="num" title="Reciprocal-rank fusion">Fused</th><th className="num" title="Reranker relevance 0-1">Rerank</th>
              <th className="num">Final</th><th>Decision</th></tr>
          </thead>
          <tbody>
            {shown.map((c, i) => (
              <tr key={c.id} className={c.selected ? "selected" : c.dropped ? "inactive" : ""}>
                <td className="num">{i + 1}</td>
                <td>
                  <button className="link small doc-link" onClick={() => setOpen(open === c.id ? null : c.id)}>{c.title || c.source}</button>
                  <small className="block muted">{[c.page && `Page ${c.page}`, c.section, c.version && `v${c.version}`, c.bank].filter(Boolean).join(" · ")}</small>
                  {open === c.id && <pre className="trace-pre">{c.text}</pre>}
                </td>
                <td className="num">{num(c.vector_score)}</td>
                <td className="num">{num(c.keyword_score, 2)}</td>
                <td className="small">{c.found_by.join(", ")}{c.exact ? " (exact)" : ""}</td>
                <td className="num">{num(c.fused, 4)}</td>
                <td className="num">{num(c.rerank)}</td>
                <td className="num">{num(c.score)}</td>
                <td className="small">{c.selected ? <span className="tag ok">used</span> : c.dropped ? <span className="tag off">{c.dropped}</span>
                  : <span className="muted">ranked lower</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > 15 && !all && <button className="btn btn-ghost btn-sm more" onClick={() => setAll(true)}>Show all {rows.length}</button>}
    </>
  );
}

export function TraceView({ data }) {
  if (!data) return null;
  const r = data.retrieval || {};
  const plan = r.plan || {};
  const gen = data.generation;
  const passes = r.passes || [];
  return (
    <div className="rtrace">
      <Collapse title="1. Question understanding" open>
        <dl className="kv">
          <dt>Original question</dt><dd>{data.question}</dd>
          <dt>Interpreted as</dt><dd>{plan.standalone}</dd>
          <dt>Rewrite</dt><dd>{plan.rewrite}{plan.followup ? " (follow-up)" : ""}</dd>
          <dt>Search queries</dt><dd>{(plan.queries || []).map((q, i) => <code key={i} className="block">{q}</code>)}</dd>
          <dt>Banks</dt><dd>{plan.banks?.join(", ") || "–"}</dd>
          <dt>Products</dt><dd>{plan.products?.join(", ") || "–"}</dd>
          <dt>Asked for</dt><dd>{plan.metrics?.join(", ") || "–"}</dd>
          {plan.loan_type && <><dt>Loan type</dt><dd>{plan.loan_type}</dd></>}
        </dl>
      </Collapse>
      <Collapse title={`2. Retrieval, filtering and reranking (${passes.length} pass${passes.length === 1 ? "" : "es"})`} open>
        {passes.map((p) => (
          <p key={p.pass} className="small">
            <b>Pass {p.pass}</b>{p.why ? ` (expanded because: ${p.why})` : ""}: {p.k} candidates per search, {p.pool} reranked with{" "}
            <b>{p.reranker}</b>, threshold {p.threshold}, {p.selected.length} used.
            {" "}Filters: {p.filters.status}{p.filters.bank_filter ? `, bank = ${p.filters.banks.join(" / ")}` : ""}
            {Object.keys(p.filters.dropped || {}).length > 0 && ` — dropped: ${Object.entries(p.filters.dropped).map(([k, v]) => `${v} ${k}`).join(", ")}`}.
            {" "}<span className="muted">Search {p.search_s}s, rerank {p.rerank_s}s.</span>
          </p>
        ))}
        <Candidates rows={r.candidates || []} />
      </Collapse>
      <Collapse title={`3. Evidence check: ${r.validation?.sufficient ? "sufficient" : "insufficient"}`} open={!r.validation?.sufficient}>
        <ul className="checks">
          {(r.validation?.checks || []).map((c, i) => (
            <li key={i}><span className={`tag ${c.info ? "" : c.ok ? "ok" : "off"}`}>{c.info ? "info" : c.ok ? "pass" : "fail"}</span> <b>{c.check}</b>: {c.detail}</li>
          ))}
        </ul>
      </Collapse>
      <Collapse title="4. Final context sent to the model">
        <pre className="trace-pre tall">{data.prompt?.context || "(none)"}</pre>
      </Collapse>
      <Collapse title={`5. Prompt (${data.prompt?.version || "?"})`}>
        <pre className="trace-pre tall">{data.prompt?.full || data.prompt?.system}</pre>
      </Collapse>
      <Collapse title="6. Model and parameters">
        {gen ? (
          <dl className="kv">
            <dt>Model</dt><dd>{gen.model}</dd><dt>Temperature</dt><dd>{gen.temperature}</dd>
            <dt>Max tokens</dt><dd>{gen.max_tokens}</dd><dt>Top P</dt><dd>{gen.top_p}</dd>
            <dt>Reasoning effort</dt><dd>{gen.reasoning_effort}</dd>
            {gen.stopped && <><dt>Stopped</dt><dd>The user stopped the answer early</dd></>}
          </dl>
        ) : <p className="hint">The model was not called ({data.not_found ? "no reliable evidence: answered “not found”" : "no LLM configured"}).</p>}
        <p className="small muted">Config {data.config?.hash} · embeddings {data.config?.embedding_model} · reranker {data.config?.reranker_in_use}
          {" "}· timings: {Object.entries(data.timings || {}).map(([k, v]) => `${k.replace("_s", "")} ${v}s`).join(", ")}</p>
      </Collapse>
      <Collapse title="7. Answer and citations" open>
        {data.answer ? <div className="md trace-answer"><Markdown text={data.answer} /></div> : <p className="hint">No answer generated.</p>}
        {data.unsupported_figures?.length > 0 && <p className="err">Figures not found in the sources: {data.unsupported_figures.join(", ")}</p>}
        {data.invalid_citations?.length > 0 && <p className="err">Citations to passages that do not exist: {data.invalid_citations.join(", ")}</p>}
        <ul className="checks">
          {(data.citations || []).map((s) => (
            <li key={s.ref}><span className="cite-num">{s.ref}</span> {s.title}{s.location && ` — ${s.location}`}
              {s.uncited && <span className="tag warn">not cited by the answer</span>}</li>
          ))}
        </ul>
        {data.attachments?.length > 0 && <p className="small muted">Attachments: {data.attachments.join(", ")}</p>}
      </Collapse>
    </div>
  );
}

function Expectations({ value, onChange }) {
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });
  return (
    <div className="form-grid">
      <label>Expected answer (optional)<input type="text" value={value.expected_answer} onChange={set("expected_answer")} placeholder="e.g. ₹50 lakh" /></label>
      <label>Expected document (optional)<input type="text" value={value.expected_document} onChange={set("expected_document")} placeholder="e.g. HDFC Business Loan Policy" /></label>
      <label>Expected page (optional)<input type="number" min="1" value={value.expected_page} onChange={set("expected_page")} /></label>
    </div>
  );
}

const NO_EXPECT = { expected_answer: "", expected_document: "", expected_page: "" };
const expectBody = (e) => ({ ...e, expected_page: e.expected_page ? Number(e.expected_page) : null });

function TestBench() {
  const [q, setQ] = useState("");
  const [lt, setLt] = useState("");
  const [exp, setExp] = useState(NO_EXPECT);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  async function run(e) {
    e.preventDefault();
    setBusy(true); setError(""); setSaved("");
    try { setRes(await api.ragTest({ question: q.trim(), loan_type: lt || null, ...expectBody(exp) })); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  async function saveGolden() {
    try {
      await api.ragGoldenSave({ question: q.trim(), loan_type: lt || null, notes: "", active: true, ...expectBody(exp) });
      setSaved("Saved as a golden test.");
    } catch (err) { setSaved(err.message); }
  }
  const ev = res?.evaluation;
  return (
    <>
      <section className="panel">
        <h2>Test a question</h2>
        <p className="hint">Runs the full pipeline (not saved to any chat). Add what the answer should be to see which stage fails.</p>
        <form onSubmit={run}>
          <div className="row-gap">
            <input className="grow-input" type="search" required minLength={2} placeholder="e.g. What is the maximum HDFC business loan amount?"
                   aria-label="Test question" value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={lt} aria-label="Loan type" onChange={(e) => setLt(e.target.value)}>
              <option value="">Any loan type</option>{LOAN_TYPES.map((t) => <option key={t}>{t}</option>)}
            </select>
          </div>
          <Expectations value={exp} onChange={setExp} />
          <div className="row-gap">
            <button className="btn btn-primary btn-sm" disabled={busy || q.trim().length < 2}>{busy ? "Running…" : "Run"}</button>
            {res && <button type="button" className="btn btn-ghost btn-sm" onClick={saveGolden}>Save as golden test</button>}
            {saved && <span className="small muted" role="status">{saved}</span>}
          </div>
        </form>
        {error && <p className="err" role="alert">{error}</p>}
      </section>
      {res && (
        <section className="panel">
          <div className="panel-head">
            <h2>Result</h2>
            {ev && <><Verdict label="Retrieval" value={ev.retrieval} /><Verdict label="Answer" value={ev.answer} />
              <Verdict label="Citations" value={ev.citations} /></>}
          </div>
          {res.llm_skipped && <p className="hint">No LLM is configured, so only retrieval was run.</p>}
          {res.not_found && <p className="hint">The evidence check found no reliable evidence, so the user would be told the information was not found.</p>}
          <h3 className="sub-h">Diagnosis</h3>
          <Diagnosis stages={res.diagnosis} />
          <TraceView data={res.trace} />
        </section>
      )}
    </>
  );
}

function QuestionLog() {
  const [rows, setRows] = useState(null);
  const [q, setQ] = useState("");
  const [only, setOnly] = useState("");
  const [sel, setSel] = useState(null);
  const [detail, setDetail] = useState(null);
  const [exp, setExp] = useState(NO_EXPECT);
  const [error, setError] = useState("");
  const load = useCallback(() => api.ragTraces(q.trim(), only).then((r) => setRows(r.traces)).catch((e) => setError(e.message)), [q, only]);
  useEffect(() => { load(); }, [load]);
  const open = useCallback((id, e = NO_EXPECT) => {
    setSel(id); setDetail(null);
    api.ragTrace(id, expectBody(e)).then(setDetail).catch((err) => setError(err.message));
  }, []);
  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2>Question log</h2>
          <input type="search" className="search" placeholder="Search questions" aria-label="Search questions" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={only} aria-label="Filter" onChange={(e) => setOnly(e.target.value)}>
            <option value="">All answers</option><option value="not_found">Answered “not found”</option>
            <option value="insufficient">Insufficient evidence</option><option value="flagged">Unmatched figures</option>
          </select>
          <button className="btn btn-ghost btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint">Every knowledge-base answer, with how it was produced. Click one to see its full trace.</p>
        {error && <p className="err" role="alert">{error}</p>}
        {rows && (rows.length === 0 ? <p className="empty">No questions yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Time</th><th>Question</th><th>Who</th><th>Outcome</th></tr></thead>
              <tbody>
                {rows.map((t) => (
                  <tr key={t.id} className={sel === t.id ? "selected" : ""}>
                    <td className="nowrap small">{when(t.ts)}</td>
                    <td><button className="link doc-link" onClick={() => open(t.id)}>{t.question}</button>
                      {t.rewritten && t.rewritten !== t.question && <small className="block muted">→ {t.rewritten}</small>}</td>
                    <td className="small">{t.origin === "chat" ? (t.email || "user") : t.origin}{t.mode === "ask" ? " · follow-up" : ""}</td>
                    <td className="small">
                      {t.not_found ? <span className="tag warn">not found</span> : <span className="tag ok">answered</span>}
                      {!t.sufficient && !t.not_found && <span className="tag warn">weak evidence</span>}
                      {t.flagged ? <span className="tag off">unmatched figures</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </section>
      {sel && (
        <section className="panel">
          <h2>Trace</h2>
          {!detail ? <p className="muted">Loading…</p> : (
            <>
              <form className="inline-form" onSubmit={(e) => { e.preventDefault(); open(sel, exp); }}>
                <p className="hint">Was this answer wrong? Enter what it should have been to find the stage that failed.</p>
                <Expectations value={exp} onChange={setExp} />
                <button className="btn btn-ghost btn-sm">Diagnose</button>
              </form>
              <Diagnosis stages={detail.diagnosis} />
              <TraceView data={detail.data} />
            </>
          )}
        </section>
      )}
    </>
  );
}

const FEEDBACK_STATUS = { new: ["To review", "warn"], confirmed: ["Mistake confirmed", "off"], dismissed: ["Answer was right", ""],
                          golden: ["Golden test added", "ok"] };
// a disliked answer's reason -> the golden-test category it most likely belongs to
const REASON_CATEGORY = { wrong_contact: "banker", outdated: "outdated", not_found_but_exists: "missing", wrong_lender: "policy" };

function FeedbackItem({ f, onChanged }) {
  const [note, setNote] = useState(f.review_note || "");
  const [trace, setTrace] = useState(null);
  const [golden, setGolden] = useState(false);
  const [err, setErr] = useState("");
  async function setStatus(status) {
    setErr("");
    try { await api.ragFeedbackReview(f.id, status, note); onChanged(); } catch (e) { setErr(e.message); }
  }
  function showTrace() {
    if (trace) { setTrace(null); return; }
    api.ragTrace(f.trace_id).then((t) => setTrace(t.data)).catch((e) => setErr(e.message));
  }
  return (
    <div className="feedback-item">
      {f.history && <p className="small muted">Earlier in the chat: {f.history.split("\n").join(" · ")}</p>}
      <div className="md trace-answer"><Markdown text={f.answer || "(answer not saved)"} /></div>
      {f.comment && <p className="small"><b>Partner's note:</b> {f.comment}</p>}
      <div className="form-grid">
        <label className="span-2">Review note<input type="text" value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="What was wrong, and the cause (document, retrieval, model…)" /></label>
      </div>
      <div className="row-gap">
        {f.status !== "confirmed" && f.status !== "golden" && <button className="btn btn-ghost btn-sm" onClick={() => setStatus("confirmed")}>Confirm mistake</button>}
        {f.status !== "dismissed" && f.status !== "golden" && <button className="btn btn-ghost btn-sm" onClick={() => setStatus("dismissed")}>Answer was right</button>}
        {f.status !== "new" && f.status !== "golden" && <button className="btn btn-ghost btn-sm" onClick={() => setStatus("new")}>Reopen</button>}
        {f.status !== "golden" && <button className="btn btn-primary btn-sm" onClick={() => setGolden(true)}>Add as golden test</button>}
        {f.trace_id && <button className="link small" onClick={showTrace}>{trace ? "Hide how it was answered" : "Show how it was answered"}</button>}
      </div>
      {err && <p className="err" role="alert">{err}</p>}
      {golden && (
        <GoldenForm title="Add as golden test" initial={{ question: f.question, history: f.history, category: REASON_CATEGORY[f.reason] || "" }}
                    save={(item) => api.ragFeedbackGolden(f.id, item)} onSaved={() => { setGolden(false); onChanged(); }}
                    onCancel={() => setGolden(false)} />
      )}
      {trace && <TraceView data={trace} />}
    </div>
  );
}

function Feedback() {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("new");
  const [rating, setRating] = useState("down");
  const [open, setOpen] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api.ragFeedback(status, rating).then(setData).catch((e) => setError(e.message)), [status, rating]);
  useEffect(() => { load(); }, [load]);
  const toReview = data?.counts?.new?.down ?? 0;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Partner feedback {toReview > 0 && <span className="count">{toReview} to review</span>}</h2>
        <select value={status} aria-label="Status" onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>
          {Object.entries(FEEDBACK_STATUS).map(([k, [l]]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <select value={rating} aria-label="Rating" onChange={(e) => setRating(e.target.value)}>
          <option value="down">Disliked</option><option value="up">Liked</option><option value="">Both</option>
        </select>
        <button className="btn btn-ghost btn-sm" onClick={load}>Refresh</button>
      </div>
      <p className="hint">Answers partners rated, with what they saw and how it was produced. Check each disliked answer against its
        sources; turn a real mistake into a golden test with the correct answer, so it is caught before it ships again.</p>
      {error && <p className="err" role="alert">{error}</p>}
      {data && (data.items.length === 0 ? <p className="empty">Nothing here.</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Time</th><th>Question</th><th>Who</th><th>Feedback</th><th>Status</th></tr></thead>
            <tbody>
              {data.items.map((f) => (
                <Fragment key={f.id}>
                  <tr className={open === f.id ? "selected" : ""}>
                    <td className="nowrap small">{when(f.updated)}</td>
                    <td><button className="link doc-link" onClick={() => setOpen(open === f.id ? null : f.id)}>{f.question || "(question not saved)"}</button></td>
                    <td className="small">{f.email || "user"}</td>
                    <td className="small">
                      <span className={`tag ${f.rating === "up" ? "ok" : "off"}`}>{f.rating === "up" ? "liked" : "disliked"}</span>
                      {f.reason_label && <div>{f.reason_label}</div>}
                    </td>
                    <td className="small"><span className={`tag ${FEEDBACK_STATUS[f.status]?.[1] ?? ""}`}>{FEEDBACK_STATUS[f.status]?.[0] ?? f.status}</span>
                      {f.reviewer && <div className="muted">{f.reviewer}</div>}</td>
                  </tr>
                  {open === f.id && <tr><td colSpan={5}><FeedbackItem f={f} onChanged={load} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

const EMPTY_GOLDEN = { question: "", expected_answer: "", expected_document: "", expected_page: "", loan_type: "", notes: "", active: true,
                       category: "", reference_answer: "", history: "", approved_by: "" };

// save: how the test is stored (default: create / update a golden test; feedback review uses its own endpoint).
function GoldenForm({ initial, onSaved, onCancel, save = api.ragGoldenSave, title }) {
  const filled = Object.fromEntries(Object.entries(initial || {}).map(([k, v]) => [k, v ?? ""]));   // database nulls -> ""
  const [f, setF] = useState({ ...EMPTY_GOLDEN, ...filled, active: initial?.active ?? true });
  const [err, setErr] = useState("");
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  async function submit(e) {
    e.preventDefault();
    try {
      await save({ ...f, expected_page: f.expected_page ? Number(f.expected_page) : null, loan_type: f.loan_type || null,
                   active: !!f.active }, initial?.id);
      onSaved();
    } catch (x) { setErr(x.message); }
  }
  return (
    <form className="panel inline-form" onSubmit={submit}>
      <h3>{title || (initial?.id ? "Edit golden test" : "New golden test")}</h3>
      <div className="form-grid">
        <label className="span-2">Question<input type="text" required minLength={2} value={f.question} onChange={set("question")} /></label>
        <label className="span-2">Earlier questions in the chat (follow-up tests, one per line)
          <textarea rows={2} value={f.history} onChange={set("history")} /></label>
        <label className="span-2">Reference answer (signed off; the judge checks the answer against it, "(not found)" if the knowledge base has none)
          <textarea rows={3} value={f.reference_answer} onChange={set("reference_answer")} /></label>
        <label>Must contain<input type="text" value={f.expected_answer} onChange={set("expected_answer")} placeholder="₹50 lakh" /></label>
        <label>Category<input type="text" value={f.category} onChange={set("category")} placeholder="banker, payout, policy…" /></label>
        <label>Approved by<input type="text" value={f.approved_by} onChange={set("approved_by")} /></label>
        <label>Expected document<input type="text" value={f.expected_document} onChange={set("expected_document")} /></label>
        <label>Expected page<input type="number" min="1" value={f.expected_page} onChange={set("expected_page")} /></label>
        <label>Loan type<select value={f.loan_type} onChange={set("loan_type")}><option value="">Any</option>{LOAN_TYPES.map((t) => <option key={t}>{t}</option>)}</select></label>
        <label className="span-2">Notes<input type="text" value={f.notes} onChange={set("notes")} /></label>
      </div>
      <label className="check"><input type="checkbox" checked={!!f.active} onChange={set("active")} /> Include in runs</label>
      <div className="row-gap">
        <button className="btn btn-primary btn-sm">Save</button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onCancel}>Cancel</button>
      </div>
      {err && <p className="err" role="alert">{err}</p>}
    </form>
  );
}

const JUDGE_TAG = { PASS: "ok", WARN: "warn", FAIL: "off", ERROR: "warn" };
const pct = (v) => (v == null ? "–" : `${v}%`);

function Scores({ scores }) {
  if (!scores) return null;
  const o = scores.overall;
  const cats = Object.entries(scores.by_category || {});
  return (
    <>
      <div className="stats">
        <div className="stat"><span>Passed</span><b>{pct(o.pass_rate)}</b></div>
        <div className="stat"><span>Faithfulness (claims supported)</span><b>{pct(o.faithfulness)}</b></div>
        <div className="stat"><span>Correct vs reference</span><b>{pct(o.correct)}</b></div>
        <div className="stat"><span>Unsupported claims</span><b>{o.unsupported_claims}</b></div>
        <div className="stat"><span>Wrong citations</span><b>{o.citation_errors}</b></div>
        <div className="stat"><span>False refusals</span><b>{o.false_refusals}</b></div>
        <div className="stat"><span>Correct refusals</span><b>{pct(o.correct_refusals)}</b></div>
      </div>
      {o.errors > 0 && <p className="err">{o.errors} case(s) did not run (see the errors below).</p>}
      {cats.length > 1 && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Category</th><th>Cases</th><th>Passed</th><th>Faithfulness</th><th>Correct</th><th>Unsupported claims</th><th>False refusals</th></tr></thead>
            <tbody>
              {cats.map(([c, s]) => (
                <tr key={c}><td>{c}</td><td>{s.ran}/{s.cases}</td><td>{pct(s.pass_rate)}</td><td>{pct(s.faithfulness)}</td>
                  <td>{pct(s.correct)}</td><td>{s.unsupported_claims}</td><td>{s.false_refusals}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

const GATE = { PASS: ["Passes the release gate", "ok"], FAIL: ["Fails the release gate", "off"],
               INCOMPLETE: ["Not finished", "warn"], NO_BASELINE: ["No baseline set", "warn"], BASELINE: ["Baseline", "info"],
               UNTESTED: ["Not tested", "warn"] };
const GATE_METRICS = { faithfulness: ["Faithfulness", "%"], correct: ["Correct vs reference", "%"], correct_refusals: ["Correct refusals", "%"],
                       unsupported_claims: ["Unsupported claims", ""], false_refusals: ["False refusals", ""] };

function GatePanel({ g, canBaseline, onBaseline }) {
  if (!g) return null;
  const [label, tone] = GATE[g.verdict] ?? [g.verdict, ""];
  return (
    <div className="gate">
      <div className="row-gap">
        <span className={`tag ${tone}`}>{label}</span>
        {g.baseline_id && g.verdict !== "BASELINE" && <span className="small muted">compared with baseline run {g.baseline_id}
          {g.compared != null && ` on ${g.compared} shared test(s)`}{g.new_cases ? ` · ${g.new_cases} new` : ""}</span>}
        {canBaseline && <button className="btn btn-ghost btn-sm" onClick={onBaseline}
          title="Later runs must do at least as well as this one">Set as baseline</button>}
      </div>
      {g.reasons?.length > 0 && <ul className="small">{g.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
      {g.deltas && Object.keys(g.deltas).length > 0 && (
        <p className="small muted">{Object.entries(g.deltas).map(([k, [b, n]]) => {
          const [name, unit] = GATE_METRICS[k] ?? [k, ""];
          return `${name}: ${b ?? "–"}${b != null ? unit : ""} → ${n ?? "–"}${n != null ? unit : ""}`;
        }).join(" · ")}</p>
      )}
      {g.regressions?.length > 0 && <p className="small err-text">Now failing: {g.regressions.map((c) => c.question).join(" · ")}</p>}
      {g.fixed?.length > 0 && <p className="small">Now passing: {g.fixed.map((c) => c.question).join(" · ")}</p>}
    </div>
  );
}

function JudgeCell({ j }) {
  if (!j) return <span className="muted">not judged</span>;
  if (j.verdict === "ERROR") return <span className="tag warn" title={j.error}>judge error</span>;
  return (
    <>
      <span className={`tag ${JUDGE_TAG[j.verdict] ?? ""}`}>{j.verdict}</span>
      <div>{j.faithfulness != null && `faithfulness ${Math.round(j.faithfulness * 100)}% (${j.supported}/${j.claims} claims) · `}
        correctness {j.correctness}</div>
      {j.correctness_reason && j.correctness !== "correct" && <div className="muted">{j.correctness_reason}</div>}
      {(j.problems || []).map((p, i) => (
        <div key={i} className={p.verdict === "unsupported" ? "err-text" : "muted"}>
          {p.verdict === "supported" ? "wrong citation" : p.verdict}: {p.claim}{p.cited?.length ? ` [${p.cited.join("][")}]` : ""}
          {p.reason && ` — ${p.reason}`}</div>
      ))}
      {j.missing?.length > 0 && <div className="muted">missing: {j.missing.join("; ")}</div>}
    </>
  );
}

function RunDetail({ id, onChanged }) {
  const [run, setRun] = useState(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => api.ragRun(id).then(setRun).catch(() => setRun(null)), [id]);
  useEffect(() => { load(); }, [load]);
  async function makeBaseline() {
    setErr("");
    try { await api.ragRunBaseline(id); load(); onChanged?.(); } catch (e) { setErr(e.message); }
  }
  if (!run) return <p className="muted">Loading…</p>;
  const judge = run.config.judge;
  return (
    <>
      <p className="small muted">Run {run.id} · {run.trigger} · {when(run.started)} · config {run.config_hash} · model {run.config.model}
        {" "}· reranker {run.config.reranker_in_use} · prompt {run.config.system_prompt_version}
        {judge && (judge.enabled ? ` · judge ${judge.model} (${judge.reasoning_effort}, ${judge.prompt_version})` : " · judge off")}</p>
      <GatePanel g={run.gate} canBaseline={run.status === "finished" && !run.baseline} onBaseline={makeBaseline} />
      {err && <p className="err" role="alert">{err}</p>}
      <Scores scores={run.scores} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Question</th><th>Result</th><th>Judge</th><th>Answer</th><th>Sources cited</th></tr></thead>
          <tbody>
            {run.results.map((x) => (
              <tr key={x.id} className={x.passed ? "" : "inactive"}>
                <td>{x.question}{x.category && <small className="block muted">{x.category}</small>}
                  {x.expected_answer && <small className="block muted">expected: {x.expected_answer}{x.expected_document && ` · ${x.expected_document}`}</small>}
                  {x.reference_answer && <small className="block muted">reference: {x.reference_answer}</small>}</td>
                <td className="small">
                  <span className={`tag ${x.passed ? "ok" : "off"}`}>{x.passed ? "pass" : "fail"}</span>
                  {x.regression && <span className="tag off">regression</span>}
                  {x.error ? <div className="err-text">{x.error}</div> : (
                    <div>retrieval {x.retrieval}{x.rank ? ` (#${x.rank})` : ""} · answer {x.answer} · citations {x.citations}</div>)}
                </td>
                <td className="small">{!x.error && <JudgeCell j={x.judge} />}</td>
                <td className="small">{x.answer_text}</td>
                <td className="small">{(x.sources || []).join("; ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Golden() {
  const [data, setData] = useState(null);
  const [edit, setEdit] = useState(null);       // null | {} (new) | test
  const [runId, setRunId] = useState(null);
  const [msg, setMsg] = useState("");
  const load = useCallback(() => api.ragGolden().then((d) => { setData(d); setRunId((r) => r ?? d.runs[0]?.id ?? null); })
    .catch((e) => setMsg(e.message)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!data?.state?.running) return;
    const t = setInterval(load, 2500);
    return () => clearInterval(t);
  }, [data?.state?.running, load]);
  async function runAll() {
    setMsg("");
    try { await api.ragGoldenRun(); setRunId(null); load(); }
    catch (e) { setMsg(e.message); }
  }
  async function resume(id) {
    setMsg("");
    try { await api.ragRunResume(id); setRunId(null); load(); }
    catch (e) { setMsg(e.message); }
  }
  async function remove(t) {
    try { await api.ragGoldenDelete(t.id); load(); } catch (e) { setMsg(e.message); }
  }
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2>Golden tests <span className="count">{data.tests.length}</span></h2>
          <button className="btn btn-primary btn-sm" onClick={() => setEdit({})}>Add test</button>
          <button className="btn btn-ghost btn-sm" disabled={data.state.running || !data.tests.length} onClick={runAll}>
            {data.state.running ? `Running ${data.state.progress}…` : "Run all"}</button>
        </div>
        <p className="hint">Questions with known answers. Run them after changing the model, prompt, embeddings, reranker, chunking
          or documents (automatically when RAG_EVAL_AUTORUN is on). Each run is compared with the approved baseline run: the
          release gate fails if a test that passed now fails or a score gets worse, and deploy/upload.sh refuses to ship it.</p>
        {msg && <p className="err" role="alert">{msg}</p>}
        {edit && <GoldenForm initial={edit} onSaved={() => { setEdit(null); load(); }} onCancel={() => setEdit(null)} />}
        {data.tests.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Question</th><th>Category</th><th>Expected answer</th><th>Expected source</th><th /></tr></thead>
              <tbody>
                {data.tests.map((t) => (
                  <tr key={t.id} className={t.active ? "" : "inactive"}>
                    <td>{t.question}{t.loan_type && <small className="block muted">{t.loan_type}</small>}
                      {t.history && <small className="block muted">follows: {t.history.split("\n")[0]}</small>}</td>
                    <td className="small">{t.category || "–"}</td>
                    <td>{t.expected_answer || "–"}
                      <small className="block muted">{t.reference_answer ? `reference${t.approved_by ? ` · approved by ${t.approved_by}` : ""}` : "no reference answer"}</small></td>
                    <td className="small">{t.expected_document || "–"}{t.expected_page ? ` — Page ${t.expected_page}` : ""}</td>
                    <td className="actions">
                      <button className="link small" onClick={() => setEdit(t)}>Edit</button>
                      <button className="link small danger-text" onClick={() => remove(t)}>Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {data.runs.length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <h2>Runs</h2>
            <select aria-label="Run" value={runId ?? ""} onChange={(e) => setRunId(Number(e.target.value))}>
              {data.runs.map((r) => (
                <option key={r.id} value={r.id}>{r.baseline ? "★ baseline · " : ""}{when(r.started)} · {r.trigger} · {r.status === "finished" ? `${r.passed}/${r.total} passed` : r.status}</option>
              ))}
            </select>
            {data.runs.find((r) => r.id === runId)?.status?.startsWith("stopped") && (
              <button className="btn btn-ghost btn-sm" disabled={data.state.running} onClick={() => resume(runId)}
                title="Keep the cases that ran and run only the rest">Resume</button>)}
          </div>
          {runId && <RunDetail key={runId} id={runId} onChanged={load} />}
        </section>
      )}
    </>
  );
}

function Config() {
  const [c, setC] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => { api.ragConfig().then(setC).catch((e) => setError(e.message)); }, []);
  if (error) return <p className="err">{error}</p>;
  if (!c) return <p className="muted">Loading…</p>;
  return (
    <section className="panel">
      <h2>Configuration</h2>
      <p className="hint">Every answer is produced with these settings (set in the server's .env; nothing changes per request).
        Each answer and golden-test run records this configuration ({c.hash}); a change re-runs the golden tests.
        {c.last_run_hash && c.last_run_hash !== c.hash && " The configuration changed since the last golden-test run."}</p>
      {c.gate && (
        <div className="gate">
          <span className={`tag ${(GATE[c.gate.verdict] ?? [])[1] ?? ""}`}>
            Release gate: {(GATE[c.gate.verdict] ?? [c.gate.verdict])[0]}</span>
          {c.gate.run_id && <span className="small muted"> (run {c.gate.run_id}{c.gate.baseline_id ? ` vs baseline run ${c.gate.baseline_id}` : ""})</span>}
          {c.gate.reasons?.length > 0 && <ul className="small">{c.gate.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
        </div>
      )}
      <dl className="kv">
        {Object.entries(c.config).map(([k, v]) => <Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{String(v)}</dd></Fragment>)}
      </dl>
    </section>
  );
}

export default function RagQuality() {
  const [sec, setSec] = useState("test");
  return (
    <div className="admin-section">
      <nav className="seg seg-sub" aria-label="RAG quality sections">
        {SECTIONS.map(([id, label]) => (
          <button key={id} className={sec === id ? "on" : ""} aria-current={sec === id ? "page" : undefined} onClick={() => setSec(id)}>{label}</button>
        ))}
      </nav>
      {sec === "test" && <TestBench />}
      {sec === "log" && <QuestionLog />}
      {sec === "golden" && <Golden />}
      {sec === "feedback" && <Feedback />}
      {sec === "config" && <Config />}
    </div>
  );
}
