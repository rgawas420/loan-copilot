// Admin > Billing & usage: revenue and provider cost, where tokens go, per-user usage, the usage ledger,
// plans, model prices, payments and webhooks, and billing settings.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { BarChart, HBars, UsageBar, compact, dateTime, inr, periodText, shortDate, tokens } from "./Usage.jsx";

const SECTIONS = [["overview", "Overview"], ["users", "Users"], ["ledger", "Usage ledger"], ["plans", "Plans"],
                  ["prices", "Model prices"], ["payments", "Payments"], ["settings", "Settings"]];
const COMPONENT_LABELS = { system: "System prompt", history: "Conversation history", context: "RAG context",
                           question: "User question", other: "Case, attachments and other input", output: "AI responses" };

function Stat({ label, value, note }) {
  return <div className="stat"><span>{label}</span><b>{value ?? "–"}</b>{note && <small className="muted">{note}</small>}</div>;
}

function useLoad(fn, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(() => fn().then((d) => { setData(d); setError(""); }).catch((e) => setError(e.message)), deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [load]);
  return [data, error, load];
}

function Overview() {
  const [o, error] = useLoad(api.billingOverview);
  const [daily] = useLoad(api.billingDaily);
  if (error) return <p className="err" role="alert">{error}</p>;
  if (!o) return <p className="empty">Loading…</p>;
  const comp = o.components || {};
  return (
    <>
      <div className="stats">
        <Stat label="Total users" value={tokens(o.total_users)} />
        <Stat label="Active subscribers" value={tokens(o.active_subscribers)}
              note={[o.trialing && `${o.trialing} on trial`, o.past_due && `${o.past_due} past due`].filter(Boolean).join(" · ")} />
        <Stat label="Free users" value={tokens(o.free_users)} />
        <Stat label="Revenue this month" value={inr(o.monthly_revenue)} note={`MRR ${inr(o.mrr)}`} />
        <Stat label="Provider AI cost (month)" value={inr(o.provider_cost_inr)} note={`$${o.provider_cost_usd} at ₹${o.usd_to_inr}/$`} />
        <Stat label="Tokens this month" value={compact(o.total_tokens)} note={`${tokens(o.llm_calls)} model calls`} />
        <Stat label="Average tokens / user" value={compact(o.avg_tokens_per_user)} />
        <Stat label="Overage revenue" value={inr(o.overage_revenue)} note={`${inr(o.overage_due)} due · ${inr(o.overage_accruing)} accruing`} />
      </div>
      {o.estimated_calls > 0 && <p className="hint">{o.estimated_calls} call(s) this month have estimated token counts (the
        provider returned no usage, usually because an answer was stopped).</p>}
      <section className="panel">
        <h2>Tokens per day <span className="muted small">all users, last 30 days</span></h2>
        {daily && <BarChart data={daily.days.map((d) => ({ key: d.date, label: shortDate(d.date), value: d.total_tokens,
          detail: `${tokens(d.input_tokens)} in · ${tokens(d.output_tokens)} out · ${inr(d.cost * o.usd_to_inr)}` }))} />}
      </section>
      <section className="panel">
        <h2>Where the tokens go <span className="muted small">grounded answers this month</span></h2>
        <HBars rows={Object.keys(COMPONENT_LABELS).filter((k) => comp[k]).map((k) => ({ key: k, label: COMPONENT_LABELS[k], value: comp[k] }))} />
        <p className="hint">Input tokens are reported by the provider as one number; the split between system prompt,
          history, retrieved context and question is proportional to their length.</p>
      </section>
      <section className="panel">
        <h2>By model</h2>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Model</th><th className="num">Calls</th><th className="num">Input</th><th className="num">Output</th><th className="num">Total</th><th className="num">Cost</th></tr></thead>
            <tbody>{o.models.map((m) => (
              <tr key={m.provider + m.model}><td>{m.model}<small className="block muted">{m.provider}</small></td><td className="num">{tokens(m.calls)}</td>
                <td className="num">{tokens(m.input_tokens)}</td><td className="num">{tokens(m.output_tokens)}</td>
                <td className="num">{tokens(m.total_tokens)}</td><td className="num">{inr(m.cost_inr)}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </section>
      <section className="panel">
        <h2>Top users this month</h2>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>User</th><th className="num">Calls</th><th className="num">Tokens</th><th className="num">Provider cost</th></tr></thead>
            <tbody>{o.top_users.map((u) => (
              <tr key={u.user_id ?? "none"}><td>{u.name}<small className="block muted">{u.email}</small></td>
                <td className="num">{tokens(u.calls)}</td><td className="num">{tokens(u.total_tokens)}</td><td className="num">{inr(u.cost_inr)}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </section>
    </>
  );
}

function UserDetail({ id, plans, mockTools, onClose, onChanged }) {
  const [d, error, load] = useLoad(() => api.billingUser(id), [id]);
  const [planId, setPlanId] = useState("");
  const [msg, setMsg] = useState("");
  async function run(fn, ok) {
    setMsg("");
    try { await fn(); await load(); onChanged(); setMsg(ok); } catch (e) { setMsg(e.message); }
  }
  if (error) return <p className="err">{error}</p>;
  if (!d) return <p className="empty">Loading…</p>;
  const u = d.usage;
  return (
    <section className="panel">
      <div className="panel-head"><h2>{d.user.name} <span className="muted small">{d.user.email}</span></h2>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button></div>
      <div className="stats">
        <Stat label="Plan" value={u.plan} note={u.is_trial ? "trial" : u.status} />
        <Stat label="Billing period" value={periodText(u.period_start, u.period_end)} />
        <Stat label="Token usage" value={`${compact(u.used)} / ${compact(u.limit)}`} note={`${u.percentage}%`} />
        <Stat label="Input" value={tokens(u.input_tokens)} />
        <Stat label="Output" value={tokens(u.output_tokens)} />
        <Stat label="Estimated provider cost" value={inr(d.provider_cost_inr)} note={`$${d.provider_cost_usd}`} />
        <Stat label="Requests" value={tokens(u.requests)} />
      </div>
      <UsageBar usage={u} />
      <h3 className="sub-h">Where this user's tokens go</h3>
      <HBars rows={Object.keys(COMPONENT_LABELS).filter((k) => d.components[k]).map((k) => ({ key: k, label: COMPONENT_LABELS[k], value: d.components[k] }))} />
      {d.models.length > 0 && <HBars rows={d.models.map((m) => ({ key: m.model, label: m.model, value: m.total_tokens, extra: inr(m.cost_inr) }))} />}
      <h3 className="sub-h">Change plan (no payment)</h3>
      <div className="row-gap">
        <select value={planId} onChange={(e) => setPlanId(e.target.value)} aria-label="Plan">
          <option value="">Choose a plan</option>
          {plans.filter((p) => p.is_active).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button className="btn btn-primary btn-sm" disabled={!planId}
                onClick={() => run(() => api.billingAssignPlan(id, Number(planId)), "Plan changed. A new billing period started.")}>Assign</button>
        <span className="muted small">For complimentary plans or deals paid outside the app.</span>
      </div>
      <h3 className="sub-h">Subscriptions</h3>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Plan</th><th>Status</th><th>Period</th><th>Provider</th><th /></tr></thead>
          <tbody>{d.subscriptions.map((s) => (
            <tr key={s.id}><td>{s.plan_name}{s.is_trial ? <span className="tag">trial</span> : null}</td><td>{s.status}{s.cancel_at_period_end ? " (ends at period end)" : ""}</td>
              <td className="nowrap">{periodText(s.current_period_start, s.current_period_end)}</td>
              <td>{s.payment_provider}<small className="block muted">{s.external_subscription_id}</small></td>
              <td className="actions">{mockTools && s.payment_provider === "mock" && ["trialing", "active", "past_due"].includes(s.status) &&
                ["renew", "fail", "cancel", "expire"].map((k) => (
                  <button key={k} className="link small" onClick={() => run(() => api.billingMockEvent(s.id, k), `Test webhook sent: ${k}.`)}>{k}</button>
                ))}</td></tr>
          ))}</tbody>
        </table>
      </div>
      {mockTools && <p className="hint">Test tools send signed webhook events through the normal webhook path (development only).</p>}
      {msg && <p className="hint" role="status">{msg}</p>}
    </section>
  );
}

function Users({ plans, mockTools }) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState(null);
  const [open, setOpen] = useState(null);
  const load = useCallback(() => api.billingUsers(q.trim()).then((r) => setRows(r.users)).catch(() => setRows([])), [q]);
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t); }, [load]);
  return (
    <>
      {open && <UserDetail id={open} plans={plans} mockTools={mockTools} onClose={() => setOpen(null)} onChanged={load} />}
      <section className="panel">
        <div className="panel-head"><h2>User usage</h2>
          <input type="search" className="search" placeholder="Search by name or email" aria-label="Search users" value={q}
                 onChange={(e) => setQ(e.target.value)} /></div>
        {rows && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>User</th><th>Plan</th><th>Billing period</th><th>Token usage</th><th className="num">Requests</th><th className="num">Cost</th></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.user_id} className="clickable" onClick={() => setOpen(r.user_id)}>
                  <td><button className="link" onClick={() => setOpen(r.user_id)}>{r.name}</button><small className="block muted">{r.email}</small></td>
                  <td>{r.plan}{r.is_trial && <span className="tag">trial</span>}{r.status === "past_due" && <span className="tag off">past due</span>}</td>
                  <td className="nowrap">{periodText(r.period_start, r.period_end)}</td>
                  <td className="usage-cell"><span>{compact(r.used)} / {compact(r.limit)}</span><UsageBar usage={{ percentage: r.percentage, blocked: r.percentage >= 100, warning: r.percentage >= 75 }} /></td>
                  <td className="num">{tokens(r.requests)}</td><td className="num">{inr(r.cost_inr)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

const FEATURES = [["", "All types"], ["chat", "Questions"], ["recommend", "Lender match"], ["intake", "Case description"], ["system", "System"]];

function Ledger() {
  const [f, setF] = useState({ user_id: "", date_from: "", date_to: "", model: "", provider: "", conversation_id: "", subscription_id: "", feature: "" });
  const [page, setPage] = useState(0);
  const [data, setData] = useState(null);
  const [fx, setFx] = useState(88);
  const size = 50;
  const set = (k) => (e) => { setPage(0); setF((x) => ({ ...x, [k]: e.target.value })); };
  useEffect(() => { api.billingSettings().then((s) => setFx(s.settings.usd_to_inr)).catch(() => {}); }, []);
  useEffect(() => {
    const t = setTimeout(() => api.billingLedger({ ...f, limit: size, offset: page * size }).then(setData).catch(() => setData(null)), 250);
    return () => clearTimeout(t);
  }, [f, page]);
  return (
    <section className="panel">
      <h2>Usage ledger <span className="muted small">one row per model call, never changed after it is written</span></h2>
      <div className="form-grid">
        <label>User id<input inputMode="numeric" value={f.user_id} onChange={set("user_id")} /></label>
        <label>From<input type="date" value={f.date_from} onChange={set("date_from")} /></label>
        <label>To<input type="date" value={f.date_to} onChange={set("date_to")} /></label>
        <label>Model<input value={f.model} onChange={set("model")} placeholder="openai/gpt-oss-120b" /></label>
        <label>Provider<input value={f.provider} onChange={set("provider")} placeholder="groq" /></label>
        <label>Conversation<input value={f.conversation_id} onChange={set("conversation_id")} /></label>
        <label>Subscription id<input inputMode="numeric" value={f.subscription_id} onChange={set("subscription_id")} /></label>
        <label>Type<select value={f.feature} onChange={set("feature")}>{FEATURES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      </div>
      {data && (
        <>
          <p className="muted small">{tokens(data.totals.count)} calls · {tokens(data.totals.input_tokens)} input · {tokens(data.totals.output_tokens)} output ·{" "}
            <b>{tokens(data.totals.total_tokens)}</b> tokens · {inr(data.totals.cost * fx)}</p>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Date</th><th>User</th><th>Type</th><th>Model</th><th className="num">Input</th><th className="num">Output</th><th className="num">Total</th><th className="num">Cost</th></tr></thead>
              <tbody>{data.items.map((r) => (
                <tr key={r.id} title={r.system_tokens != null ? `System ${r.system_tokens} · history ${r.history_tokens} · RAG context ${r.context_tokens} · question ${r.question_tokens} · other ${r.other_tokens}` : ""}>
                  <td className="nowrap">{dateTime(r.created)}</td>
                  <td>{r.user_name || (r.user_id ? `#${r.user_id}` : <span className="muted">none</span>)}<small className="block muted">{r.plan_name}</small></td>
                  <td>{r.feature_label}<small className="block muted">{r.purpose_label}{r.request_status !== "completed" ? ` · ${r.request_status}` : ""}{r.usage_source === "estimated" ? " · estimated" : ""}{r.overage_tokens ? ` · ${tokens(r.overage_tokens)} over` : ""}</small></td>
                  <td className="muted">{r.model}</td>
                  <td className="num">{tokens(r.input_tokens)}</td><td className="num">{tokens(r.output_tokens)}</td>
                  <td className="num"><b>{tokens(r.total_tokens)}</b></td><td className="num">{inr(r.total_cost * fx, 4)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="row-gap pager">
            <button className="btn btn-ghost btn-sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Newer</button>
            <button className="btn btn-ghost btn-sm" disabled={(page + 1) * size >= data.totals.count} onClick={() => setPage((p) => p + 1)}>Older</button>
          </div>
        </>
      )}
    </section>
  );
}

const EMPTY_PLAN = { name: "", description: "", price: 0, currency: "INR", billing_interval: "monthly", token_limit: 100000,
  allowed_models: [], max_requests: 0, requests_per_minute: 20, max_concurrent: 2, max_context_tokens: 16000,
  max_output_tokens: 1200, rag_enabled: true, max_file_mb: 5, max_files_per_month: 30, allow_overage: false,
  overage_price_per_1k: 0, max_overage_tokens: 0, trial_days: 0, features: [], external_ids: {}, is_default: false,
  is_active: true, sort_order: 10 };
const NUM = ["price", "token_limit", "max_requests", "requests_per_minute", "max_concurrent", "max_context_tokens", "max_output_tokens",
  "max_file_mb", "max_files_per_month", "overage_price_per_1k", "max_overage_tokens", "trial_days", "sort_order"];

function PlanForm({ plan, models, onSaved, onCancel }) {
  const [p, setP] = useState({ ...EMPTY_PLAN, ...plan });
  const [err, setErr] = useState("");
  const set = (k, kind) => (e) => setP((x) => ({ ...x, [k]: kind === "bool" ? e.target.checked : e.target.value }));
  async function save(e) {
    e.preventDefault(); setErr("");
    const body = { ...p };
    for (const k of NUM) body[k] = Number(body[k]);
    body.allowed_models = typeof p.allowed_models === "string" ? p.allowed_models.split(",").map((s) => s.trim()).filter(Boolean) : p.allowed_models;
    body.features = typeof p.features === "string" ? p.features.split("\n").map((s) => s.trim()).filter(Boolean) : p.features;
    body.external_ids = { ...(p.external_ids || {}), razorpay: p.rzp ?? p.external_ids?.razorpay ?? "" };
    for (const k of ["id", "code", "price_minor", "overage_price_per_1k_minor", "created", "updated", "rzp"]) delete body[k];
    try { await api.billingSavePlan(body, plan.id); onSaved(); } catch (x) { setErr(x.message); }
  }
  const field = (k, label, props = {}) => <label>{label}<input value={p[k]} onChange={set(k)} {...props} /></label>;
  return (
    <form className="panel inline-form" onSubmit={save}>
      <h3>{plan.id ? `Edit ${plan.name}` : "New plan"}</h3>
      <div className="form-grid">
        {field("name", "Plan name", { required: true, maxLength: 60 })}
        {field("price", "Price (₹)", { type: "number", min: 0, step: "0.01" })}
        <label>Billing interval<select value={p.billing_interval} onChange={set("billing_interval")}><option value="monthly">Monthly</option><option value="yearly">Yearly</option></select></label>
        {field("token_limit", "Tokens per period", { type: "number", min: 0 })}
        <label>Allowed answer models (comma, basic → advanced; empty = all)
          <input value={Array.isArray(p.allowed_models) ? p.allowed_models.join(", ") : p.allowed_models} onChange={set("allowed_models")}
                 placeholder={models.join(", ")} /></label>
        {field("max_requests", "Max requests per period (0 = no limit)", { type: "number", min: 0 })}
        {field("requests_per_minute", "Requests per minute", { type: "number", min: 1 })}
        {field("max_concurrent", "Concurrent requests", { type: "number", min: 1 })}
        {field("max_context_tokens", "Max request size (tokens)", { type: "number", min: 100 })}
        {field("max_output_tokens", "Max answer length (tokens)", { type: "number", min: 50 })}
        {field("max_file_mb", "Max file size (MB)", { type: "number", min: 0 })}
        {field("max_files_per_month", "Files per period (0 = no limit)", { type: "number", min: 0 })}
        {field("trial_days", "Free trial days", { type: "number", min: 0 })}
        {field("overage_price_per_1k", "Overage price per 1,000 tokens (₹)", { type: "number", min: 0, step: "0.01" })}
        {field("max_overage_tokens", "Overage cap in tokens (0 = none)", { type: "number", min: 0 })}
        <label>Razorpay plan id<input value={p.rzp ?? p.external_ids?.razorpay ?? ""} onChange={set("rzp")} placeholder="plan_..." /></label>
        {field("sort_order", "Display order", { type: "number" })}
        <label>Features (one per line)<textarea rows={4} value={Array.isArray(p.features) ? p.features.join("\n") : p.features} onChange={set("features")} /></label>
      </div>
      <div className="row-gap">
        <label className="check"><input type="checkbox" checked={p.rag_enabled} onChange={set("rag_enabled", "bool")} />Knowledge-base (RAG) answers</label>
        <label className="check"><input type="checkbox" checked={p.allow_overage} onChange={set("allow_overage", "bool")} />Allow overage (billed at period end)</label>
        <label className="check"><input type="checkbox" checked={p.is_active} onChange={set("is_active", "bool")} />Active (offered to users)</label>
        <label className="check"><input type="checkbox" checked={p.is_default} onChange={set("is_default", "bool")} />Default free plan</label>
      </div>
      {err && <p className="err" role="alert">{err}</p>}
      <div className="row-gap">
        <button className="btn btn-primary btn-sm" type="submit">Save plan</button>
        <button className="btn btn-ghost btn-sm" type="button" onClick={onCancel}>Cancel</button>
      </div>
      <p className="hint">Changes apply to everyone on the plan from their next request. Overage is never charged automatically:
        it becomes a due charge at the end of the period.</p>
    </form>
  );
}

function Plans({ plans, models, reload }) {
  const [edit, setEdit] = useState(null);
  return (
    <>
      {edit && <PlanForm plan={edit} models={models} onSaved={() => { setEdit(null); reload(); }} onCancel={() => setEdit(null)} />}
      <section className="panel">
        <div className="panel-head"><h2>Plans</h2><button className="btn btn-primary btn-sm" onClick={() => setEdit({})}>New plan</button></div>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Plan</th><th className="num">Price</th><th className="num">Tokens</th><th>Limits</th><th>Overage</th><th>Status</th><th /></tr></thead>
            <tbody>{plans.map((p) => (
              <tr key={p.id} className={p.is_active ? "" : "inactive"}>
                <td><b>{p.name}</b>{p.is_default && <span className="tag">default</span>}<small className="block muted">{p.allowed_models.join(", ") || "all models"}</small></td>
                <td className="num">{inr(p.price, 0)}<small className="block muted">/{p.billing_interval === "yearly" ? "year" : "month"}</small></td>
                <td className="num">{compact(p.token_limit)}</td>
                <td className="small muted">{p.requests_per_minute}/min · {p.max_concurrent} at once · answers ≤{compact(p.max_output_tokens)}
                  {p.trial_days ? ` · ${p.trial_days}-day trial` : ""}{p.rag_enabled ? "" : " · no RAG"}</td>
                <td className="small">{p.allow_overage ? `${inr(p.overage_price_per_1k)}/1K` : "Blocked at limit"}</td>
                <td><span className={`tag ${p.is_active ? "ok" : "off"}`}>{p.is_active ? "Active" : "Hidden"}</span></td>
                <td><button className="link small" onClick={() => setEdit(p)}>Edit</button></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>
    </>
  );
}

function Prices() {
  const [data, error, load] = useLoad(api.billingPrices);
  const [f, setF] = useState({ provider: "groq", model: "", input_per_1m: "", output_per_1m: "", currency: "USD" });
  const [msg, setMsg] = useState("");
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  async function save(e) {
    e.preventDefault(); setMsg("");
    try {
      await api.billingSetPrice({ ...f, input_per_1m: Number(f.input_per_1m), output_per_1m: Number(f.output_per_1m) });
      setMsg("Saved. The new price applies to calls from now on; earlier calls keep their recorded cost."); load();
    } catch (x) { setMsg(x.message); }
  }
  return (
    <section className="panel">
      <h2>Model prices <span className="muted small">provider cost per 1 million tokens</span></h2>
      {error && <p className="err">{error}</p>}
      <form className="form-grid" onSubmit={save}>
        <label>Provider<input required value={f.provider} onChange={set("provider")} /></label>
        <label>Model<input required value={f.model} onChange={set("model")} placeholder="openai/gpt-oss-120b" /></label>
        <label>Input per 1M<input required type="number" min="0" step="0.0001" value={f.input_per_1m} onChange={set("input_per_1m")} /></label>
        <label>Output per 1M<input required type="number" min="0" step="0.0001" value={f.output_per_1m} onChange={set("output_per_1m")} /></label>
        <label>Currency<input maxLength={3} value={f.currency} onChange={set("currency")} /></label>
        <label>&nbsp;<button className="btn btn-primary btn-sm" type="submit">Set price</button></label>
      </form>
      {msg && <p className="hint" role="status">{msg}</p>}
      {data && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Provider</th><th>Model</th><th className="num">Input / 1M</th><th className="num">Output / 1M</th><th>From</th><th>Until</th></tr></thead>
            <tbody>{data.prices.map((p) => (
              <tr key={p.id} className={p.effective_to ? "inactive" : ""}><td>{p.provider}</td><td>{p.model}</td>
                <td className="num">{p.input_per_1m} {p.currency}</td><td className="num">{p.output_per_1m} {p.currency}</td>
                <td className="nowrap">{dateTime(p.effective_from)}</td><td className="nowrap">{p.effective_to ? dateTime(p.effective_to) : "current"}</td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <p className="hint">Check these against the provider's price list. A model without a price is recorded with zero cost.</p>
    </section>
  );
}

function Payments() {
  const [d, error, load] = useLoad(api.billingPayments);
  if (error) return <p className="err">{error}</p>;
  if (!d) return <p className="empty">Loading…</p>;
  return (
    <>
      <section className="panel">
        <h2>Payments</h2>
        {d.payments.length === 0 ? <p className="empty">No payments yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Date</th><th>User</th><th>Payment</th><th className="num">Amount</th><th>Status</th></tr></thead>
              <tbody>{d.payments.map((p) => (
                <tr key={p.id}><td className="nowrap">{dateTime(p.created)}</td><td>{p.name}<small className="block muted">{p.email}</small></td>
                  <td>{p.provider}<small className="block muted">{p.external_payment_id}</small></td>
                  <td className="num">{inr(p.amount)}{p.refunded > 0 && <small className="block muted">−{inr(p.refunded)} refunded</small>}</td>
                  <td><span className={`tag ${p.status === "captured" ? "ok" : "off"}`}>{p.status}</span></td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel">
        <h2>Overage charges</h2>
        {d.charges.length === 0 ? <p className="empty">No overage charges.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>User</th><th>Period</th><th className="num">Tokens</th><th className="num">Amount</th><th>Status</th></tr></thead>
              <tbody>{d.charges.map((c) => (
                <tr key={c.id}><td>{c.name}<small className="block muted">{c.email}</small></td><td className="nowrap">{periodText(c.period_start, c.period_end)}</td>
                  <td className="num">{tokens(c.tokens)}</td><td className="num">{inr(c.amount)}</td>
                  <td><select value={c.status} aria-label="Charge status" onChange={(e) => api.billingChargeStatus(c.id, e.target.value).then(load)}>
                    <option value="due">Due</option><option value="paid">Paid</option><option value="waived">Waived</option></select></td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel">
        <h2>Webhook events <span className="muted small">latest 50</span></h2>
        {d.webhooks.length === 0 ? <p className="empty">No webhook events yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Received</th><th>Event</th><th>Status</th><th>Result</th></tr></thead>
              <tbody>{d.webhooks.map((w) => (
                <tr key={w.id}><td className="nowrap">{dateTime(w.received)}</td><td>{w.event_type}<small className="block muted">{w.provider} · {w.raw_type} · {w.event_id}</small></td>
                  <td><span className={`tag ${w.status === "processed" ? "ok" : ""}`}>{w.status}</span></td><td className="small muted">{w.detail}</td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <p className="hint">Each event id is processed once; repeated deliveries are ignored. Unsigned or wrongly signed webhooks are rejected.</p>
      </section>
    </>
  );
}

function Settings({ data, reload }) {
  const [f, setF] = useState(null);
  const [msg, setMsg] = useState("");
  useEffect(() => { if (data) setF({ ...data.settings, warn_thresholds: data.settings.warn_thresholds.join(", ") }); }, [data]);
  if (!f) return <p className="empty">Loading…</p>;
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  async function save(e) {
    e.preventDefault(); setMsg("");
    try {
      await api.billingSaveSettings({ warn_thresholds: String(f.warn_thresholds).split(",").map((x) => Number(x.trim())).filter(Boolean),
        usd_to_inr: Number(f.usd_to_inr), grace_days: Number(f.grace_days), near_limit_policy: f.near_limit_policy,
        anonymous_daily_tokens: Number(f.anonymous_daily_tokens), anonymous_max_concurrent: Number(f.anonymous_max_concurrent) });
      setMsg("Saved."); reload();
    } catch (x) { setMsg(x.message); }
  }
  return (
    <section className="panel">
      <h2>Billing settings</h2>
      <form onSubmit={save}>
        <div className="form-grid">
          <label>Warning thresholds (% of allowance)<input value={f.warn_thresholds} onChange={set("warn_thresholds")} placeholder="75, 90" /></label>
          <label>Request that may not fit in what is left
            <select value={f.near_limit_policy} onChange={set("near_limit_policy")}>
              <option value="block">Block it (never go over the limit)</option>
              <option value="allow">Allow it while some allowance is left</option>
            </select></label>
          <label>Grace days after a failed renewal<input type="number" min="0" value={f.grace_days} onChange={set("grace_days")} /></label>
          <label>USD to INR (for cost reports)<input type="number" min="0" step="0.01" value={f.usd_to_inr} onChange={set("usd_to_inr")} /></label>
          <label>Tokens per day without login (0 = log in required)<input type="number" min="0" value={f.anonymous_daily_tokens} onChange={set("anonymous_daily_tokens")} /></label>
          <label>Concurrent requests without login<input type="number" min="0" value={f.anonymous_max_concurrent} onChange={set("anonymous_max_concurrent")} /></label>
        </div>
        <div className="row-gap">
          <button className="btn btn-primary btn-sm" type="submit">Save settings</button>
          <span className="muted small">Payment provider: <b>{data.provider}</b> (set PAYMENT_PROVIDER in .env)</span>
        </div>
      </form>
      {msg && <p className="hint" role="status">{msg}</p>}
      <h3 className="sub-h">Maintenance</h3>
      <div className="row-gap">
        <button className="btn btn-ghost btn-sm" onClick={() => api.billingRebuildTotals()
          .then((r) => setMsg(`Recomputed ${r.periods} usage periods from the ledger.`)).catch((x) => setMsg(x.message))}>
          Recompute usage counters from the ledger</button>
      </div>
    </section>
  );
}

export default function AdminBilling() {
  const [section, setSection] = useState("overview");
  const [plans, , reloadPlans] = useLoad(api.billingPlans);
  const [settings, , reloadSettings] = useLoad(api.billingSettings);
  const mockTools = !!settings?.mock_tools;
  return (
    <>
      <nav className="seg small-seg" aria-label="Billing sections">
        {SECTIONS.map(([id, label]) => <button key={id} className={section === id ? "on" : ""} onClick={() => setSection(id)}>{label}</button>)}
      </nav>
      {section === "overview" && <Overview />}
      {section === "users" && <Users plans={plans?.plans || []} mockTools={mockTools} />}
      {section === "ledger" && <Ledger />}
      {section === "plans" && plans && <Plans plans={plans.plans} models={settings?.models || []} reload={reloadPlans} />}
      {section === "prices" && <Prices />}
      {section === "payments" && <Payments />}
      {section === "settings" && settings && <Settings data={settings} reload={reloadSettings} />}
    </>
  );
}
