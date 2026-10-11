// Settings > Usage & billing: current plan, token usage, usage by day and model, plans (upgrade, trial,
// downgrade, cancel), usage history and payments. A paid plan is switched on by the server only after it has
// verified the payment with the provider; this page only starts the checkout.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import Icon from "./Icon.jsx";
import { BarChart, HBars, UsageBar, compact, dateTime, inr, longDate, periodText, shortDate, tokens } from "./Usage.jsx";

const RANGES = [{ id: "7d", label: "7 days" }, { id: "30d", label: "30 days" }, { id: "period", label: "Current billing period" }];
const STATUS = { active: "Active", trialing: "Trial", past_due: "Payment overdue", canceled: "Canceled", expired: "Expired" };
const per = (p) => (p.billing_interval === "yearly" ? "year" : "month");

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("Could not load the payment window."));
    document.head.appendChild(s);
  });
}

function MockCheckout({ checkout, onDone, onClose }) {
  const ref = useRef(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);
  async function finish(outcome) {
    setBusy(true); setErr("");
    try { await api.mockComplete(checkout.checkout_id, outcome); onDone(outcome); }
    catch (e) { setErr(e.message); setBusy(false); }
  }
  return (
    <dialog ref={ref} className="dialog" onClose={onClose} onCancel={onClose} aria-labelledby="mock-title">
      <div className="dialog-body">
        <h2 id="mock-title">Test checkout</h2>
        <p className="hint">Test mode: no real money moves. The server receives signed test webhooks, exactly as it
          would from the payment provider.</p>
        <p><b>{checkout.plan}</b> plan · {inr(checkout.amount)}</p>
        {err && <p className="err" role="alert">{err}</p>}
        <div className="row-gap">
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => finish("success")}>Pay {inr(checkout.amount)}</button>
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => finish("fail")}>Simulate a failed payment</button>
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onClose}>Cancel</button>
        </div>
      </div>
    </dialog>
  );
}

function PlanCard({ plan, sub, trialAvailable, busy, onSubscribe, onTrial, onDowngrade }) {
  const current = sub.plan.id === plan.id;
  const curPrice = sub.is_trial ? 0 : sub.plan.price;
  let action = null;
  if (current && !sub.is_trial) action = <span className="tag ok">Current plan</span>;
  else if (plan.price > curPrice || (current && sub.is_trial)) {
    action = (
      <div className="plan-actions">
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => onSubscribe(plan)}>
          {current ? "Subscribe now" : "Upgrade"}</button>
        {plan.trial_days > 0 && trialAvailable && sub.is_free && (
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onTrial(plan)}>
            Start {plan.trial_days}-day free trial</button>
        )}
      </div>
    );
  } else if (sub.pending_plan?.id === plan.id) action = <span className="tag">Starts at renewal</span>;
  else action = <button className="btn btn-ghost btn-sm" disabled={busy || sub.is_free} onClick={() => onDowngrade(plan)}>Downgrade</button>;
  return (
    <div className={`plan-card${current ? " current" : ""}`}>
      <h3>{plan.name}</h3>
      <p className="plan-price">{plan.price ? inr(plan.price, 0) : "₹0"}<span> / {per(plan)}</span></p>
      <p className="plan-tokens"><b>{tokens(plan.token_limit)}</b> tokens / {per(plan)}</p>
      <ul>{plan.features.map((f) => <li key={f}><Icon name="check" size={14} />{f}</li>)}</ul>
      {action}
    </div>
  );
}

function History() {
  const [page, setPage] = useState(0);
  const [data, setData] = useState(null);
  const size = 20;
  useEffect(() => { api.usageHistory(page * size, size).then(setData).catch(() => setData({ items: [], totals: {} })); }, [page]);
  if (!data) return <p className="empty">Loading…</p>;
  if (!data.items.length && page === 0) return <p className="empty">No AI usage yet.</p>;
  return (
    <>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Date</th><th>Type</th><th>Model</th><th className="num">Input</th><th className="num">Output</th><th className="num">Total</th></tr></thead>
          <tbody>
            {data.items.map((r) => (
              <tr key={r.id}>
                <td className="nowrap">{dateTime(r.created)}</td>
                <td>{r.feature_label}<small className="block muted">{r.purpose_label}{r.request_status !== "completed" ? ` · ${r.request_status}` : ""}
                  {r.usage_source === "estimated" ? " · estimated" : ""}</small></td>
                <td className="muted">{r.model}</td>
                <td className="num">{tokens(r.input_tokens)}</td>
                <td className="num">{tokens(r.output_tokens)}</td>
                <td className="num"><b>{tokens(r.total_tokens)}</b></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row-gap pager">
        <button className="btn btn-ghost btn-sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Newer</button>
        <span className="muted small">{page * size + 1}–{page * size + data.items.length} of {tokens(data.totals.count)}</span>
        <button className="btn btn-ghost btn-sm" disabled={(page + 1) * size >= data.totals.count} onClick={() => setPage((p) => p + 1)}>Older</button>
      </div>
    </>
  );
}

export default function BillingPage({ sidebarOpen, onOpenSidebar, onUsageChanged }) {
  const [usage, setUsage] = useState(null);
  const [subData, setSubData] = useState(null);
  const [plans, setPlans] = useState([]);
  const [range, setRange] = useState("7d");
  const [daily, setDaily] = useState(null);
  const [models, setModels] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState({ text: "", error: false });
  const [mock, setMock] = useState(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const plansRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const [u, s, p, m] = await Promise.all([api.usage(), api.subscription(), api.plans(), api.usageByModel("period")]);
      setUsage(u); setSubData(s); setPlans(p.plans); setModels(m.models);
      onUsageChanged?.(u);
    } catch (e) { setMsg({ text: e.message, error: true }); }
  }, [onUsageChanged]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { api.usageDaily(range).then((d) => setDaily(d.days)).catch(() => setDaily([])); }, [range]);

  async function run(fn, ok) {
    setBusy(true); setMsg({ text: "", error: false });
    try { await fn(); await load(); if (ok) setMsg({ text: ok, error: false }); }
    catch (e) { setMsg({ text: e.message, error: true }); }
    finally { setBusy(false); }
  }

  async function subscribe(plan) {
    setBusy(true); setMsg({ text: "", error: false });
    try {
      const { checkout } = await api.checkout(plan.id);
      if (checkout.provider === "mock") { setMock(checkout); return; }
      await loadScript("https://checkout.razorpay.com/v1/checkout.js");
      new window.Razorpay({
        key: checkout.key, subscription_id: checkout.subscription_id, name: checkout.name,
        description: checkout.description, prefill: checkout.prefill, theme: { color: "#0e6b52" },
        handler: (resp) => run(() => api.verifyCheckout(checkout.checkout_id, resp),
          "Payment confirmed. Your new plan is active."),
        modal: { ondismiss: () => setBusy(false) },
      }).open();
    } catch (e) { setMsg({ text: e.message, error: true }); }
    finally { setBusy(false); }
  }

  const sub = subData?.subscription;
  return (
    <div className="chat-main admin-main">
      <header className="topbar">
        {!sidebarOpen && <button className="icon-btn" aria-label="Open sidebar" onClick={onOpenSidebar}><Icon name="menu" size={20} /></button>}
        <h1 className="top-title">Usage &amp; billing</h1>
      </header>
      <div className="admin-scroll">
        <div className="admin-wrap">
          {msg.text && <p className={msg.error ? "err" : "ok-note"} role={msg.error ? "alert" : "status"}>{msg.text}</p>}
          {!sub || !usage ? <p className="empty">Loading…</p> : (
            <>
              <section className="panel plan-now">
                <div className="plan-now-head">
                  <div>
                    <span className="muted small">Current plan</span>
                    <h2 className="plan-name">{sub.plan.name}
                      <span className={`tag ${sub.status === "past_due" ? "off" : "ok"}`}>{STATUS[sub.status] || sub.status}</span></h2>
                    <p className="muted">{sub.plan.price ? `${inr(sub.plan.price, 0)} / ${per(sub.plan)}` : "Free"} ·{" "}
                      {tokens(sub.plan.token_limit)} tokens / {per(sub.plan)}</p>
                  </div>
                  <div className="plan-now-dates">
                    {sub.is_trial ? <>Trial ends<b>{longDate(sub.current_period_end)}</b></>
                      : sub.cancel_at_period_end ? <>Ends<b>{longDate(sub.current_period_end)}</b></>
                      : sub.status === "past_due" ? <>Payment due since<b>{longDate(sub.current_period_end)}</b></>
                      : sub.renews ? <>Renews<b>{longDate(sub.renewal_date)}</b></>
                      : <>Usage resets<b>{longDate(sub.current_period_end)}</b></>}
                  </div>
                </div>
                <div className="plan-meter">
                  <div className="row-between"><span><b>{tokens(usage.used)}</b> used</span>
                    <span className="muted">{Math.round(usage.percentage)}% of {tokens(usage.limit)}</span></div>
                  <UsageBar usage={usage} />
                  <p className="muted small">Billing period {periodText(usage.period_start, usage.period_end)}
                    {usage.in_progress > 0 && ` · ${tokens(usage.in_progress)} reserved for answers in progress`}</p>
                </div>
                {usage.warning && <p className={`usage-warn inline ${usage.warning.level}`}><Icon name="alert" size={16} />{usage.warning.message}</p>}
                {sub.status === "past_due" && <p className="usage-warn inline strong"><Icon name="alert" size={16} />
                  Your last payment did not go through. Update your payment with the provider to keep your plan.</p>}
                {sub.cancel_at_period_end && <p className="hint">Your subscription is canceled. You keep {sub.plan.name} until
                  {" "}{longDate(sub.current_period_end)}, then move to the free plan. Your chats and history are kept.</p>}
                {sub.pending_plan && <p className="hint">You move to {sub.pending_plan.name} on {longDate(sub.current_period_end)}.</p>}
                <div className="row-gap">
                  <button className="btn btn-primary btn-sm" onClick={() => plansRef.current?.scrollIntoView({ behavior: "smooth" })}>
                    {sub.is_free ? "Upgrade plan" : "Manage subscription"}</button>
                  {!sub.is_free && !sub.cancel_at_period_end && !sub.is_trial && (confirmCancel ? (
                    <>
                      <span className="small">Cancel at the end of this period?</span>
                      <button className="btn btn-danger btn-sm" disabled={busy}
                              onClick={() => run(api.cancelSubscription, "Subscription canceled at the end of the period.").then(() => setConfirmCancel(false))}>
                        Yes, cancel</button>
                      <button className="btn btn-ghost btn-sm" onClick={() => setConfirmCancel(false)}>Keep it</button>
                    </>
                  ) : <button className="btn btn-ghost btn-sm" onClick={() => setConfirmCancel(true)}>Cancel subscription</button>)}
                </div>
              </section>

              <section className="panel">
                <h2>Usage this billing period</h2>
                <div className="stats">
                  <div className="stat"><span>Used</span><b>{tokens(usage.used)}</b></div>
                  <div className="stat"><span>Limit</span><b>{tokens(usage.limit)}</b></div>
                  <div className="stat"><span>Remaining</span><b>{tokens(usage.remaining)}</b></div>
                  <div className="stat"><span>Input tokens</span><b>{tokens(usage.input_tokens)}</b></div>
                  <div className="stat"><span>Output tokens</span><b>{tokens(usage.output_tokens)}</b></div>
                  <div className="stat"><span>AI requests</span><b>{tokens(usage.requests)}{usage.max_requests ? <small className="muted"> / {tokens(usage.max_requests)}</small> : null}</b></div>
                </div>
                {usage.allow_overage && (
                  <p className="hint">Usage over the allowance is billed at {inr(usage.overage_price_per_1k)} per 1,000 tokens at the
                    end of the period. Overage so far: {tokens(usage.overage_tokens)} tokens ({inr(usage.overage_amount)}).</p>
                )}
              </section>

              <section className="panel">
                <div className="panel-head">
                  <h2>Token usage</h2>
                  <nav className="seg small-seg" aria-label="Range">
                    {RANGES.map((r) => <button key={r.id} className={range === r.id ? "on" : ""} onClick={() => setRange(r.id)}>{r.label}</button>)}
                  </nav>
                </div>
                {daily && <BarChart data={daily.map((d) => ({ key: d.date, label: shortDate(d.date), value: d.total_tokens,
                  detail: `${tokens(d.input_tokens)} input · ${tokens(d.output_tokens)} output` }))} />}
              </section>

              <section className="panel">
                <h2>Usage by model <span className="muted small">this billing period</span></h2>
                {models && <HBars rows={models.map((m) => ({ key: m.model, label: m.model, value: m.total_tokens,
                  extra: `· ${m.calls} call${m.calls === 1 ? "" : "s"}` }))} />}
              </section>

              <section className="panel" ref={plansRef}>
                <h2>Plans</h2>
                <div className="plan-grid">
                  {plans.map((p) => (
                    <PlanCard key={p.id} plan={p} sub={sub} trialAvailable={subData.trial_available} busy={busy}
                              onSubscribe={subscribe}
                              onTrial={(pl) => run(() => api.startTrial(pl.id), `Your ${pl.name} trial has started.`)}
                              onDowngrade={(pl) => run(() => api.downgrade(pl.id),
                                pl.price ? `You move to ${pl.name} at the end of this period.` : "You move to the free plan at the end of this period.")} />
                  ))}
                </div>
                <p className="hint">Upgrades start a new billing period with the new allowance as soon as the payment is confirmed.
                  Downgrades and cancellations apply at the end of the current period.</p>
              </section>

              <section className="panel">
                <h2>Usage history</h2>
                <History />
              </section>

              <section className="panel">
                <h2>Payments and invoices</h2>
                {subData.payments.length === 0 && subData.charges.length === 0 ? <p className="empty">No payments yet.</p> : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead><tr><th>Date</th><th>Description</th><th className="num">Amount</th><th>Status</th></tr></thead>
                      <tbody>
                        {subData.payments.map((p) => (
                          <tr key={`p${p.id}`}>
                            <td className="nowrap">{dateTime(p.created)}</td>
                            <td>Subscription payment<small className="block muted">{p.external_payment_id}</small></td>
                            <td className="num">{inr(p.amount)}{p.refunded > 0 && <small className="block muted">refunded {inr(p.refunded)}</small>}</td>
                            <td><span className={`tag ${p.status === "captured" ? "ok" : "off"}`}>{p.status.replace("_", " ")}</span></td>
                          </tr>
                        ))}
                        {subData.charges.map((ch) => (
                          <tr key={`c${ch.id}`}>
                            <td className="nowrap">{dateTime(ch.created)}</td>
                            <td>Overage, {compact(ch.tokens)} tokens<small className="block muted">{periodText(ch.period_start, ch.period_end)}</small></td>
                            <td className="num">{inr(ch.amount)}</td>
                            <td><span className={`tag ${ch.status === "paid" ? "ok" : ""}`}>{ch.status}</span></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <h3 className="sub-h">Payment method</h3>
                <p className="hint">{subData.provider === "razorpay"
                  ? "Cards, UPI and net banking are entered in Razorpay's secure checkout. This app never sees or stores your card details."
                  : "Test mode: payments are simulated and no real money moves."}</p>
              </section>
            </>
          )}
        </div>
      </div>
      {mock && <MockCheckout checkout={mock} onClose={() => setMock(null)}
                             onDone={(outcome) => { setMock(null); load(); setMsg(outcome === "success"
                               ? { text: "Payment confirmed. Your new plan is active.", error: false }
                               : { text: "The payment failed, so your plan has not changed.", error: true }); }} />}
    </div>
  );
}
