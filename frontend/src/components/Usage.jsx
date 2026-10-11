// Shared pieces of the usage and billing UI: number formats, the usage bar, the sidebar meter, the warning
// banner, the limit-reached card and a small bar chart. All numbers come from the server.
import { useState } from "react";
import Icon from "./Icon.jsx";

export const tokens = (n) => Number(n || 0).toLocaleString("en-US");
export const compact = (n) => {
  n = Number(n || 0);
  if (n >= 1e6) return `${(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 || n % 1e3 === 0 ? 0 : 1)}K`;
  return String(n);
};
export const inr = (n, digits = 2) =>
  "₹" + Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: digits });
export const shortDate = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: "2-digit", month: "short" }) : "");
export const longDate = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "long" }) : "");
export const dateTime = (iso) =>
  (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
/** A period is [start, end): show the last day it covers. */
export const periodText = (start, end) =>
  `${shortDate(start)} – ${shortDate(new Date(new Date(end).getTime() - 1000).toISOString())}`;

const toneOf = (u) => (u.blocked ? "bad" : u.warning ? "warn" : "ok");

export function UsageBar({ usage, label }) {
  const pct = Math.min(usage.percentage || 0, 100);
  return (
    <div className={`ubar ${toneOf(usage)}`} role="progressbar" aria-valuemin={0} aria-valuemax={100}
         aria-valuenow={Math.round(pct)} aria-label={label || "Token allowance used"}>
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

/** Subtle indicator in the sidebar: plan, bar, used / limit. */
export function UsageMeter({ usage, onOpen }) {
  if (!usage) return null;
  return (
    <button className="umeter" onClick={onOpen} title="Usage & billing">
      <span className="umeter-head">
        <b>{usage.plan}{usage.is_trial ? " trial" : ""}</b>
        <span className={`umeter-pct ${toneOf(usage)}`}>
          {usage.warning && <Icon name="alert" size={13} />}{Math.round(usage.percentage)}%
        </span>
      </span>
      <UsageBar usage={usage} />
      <span className="umeter-foot">{compact(usage.used)} / {compact(usage.limit)} tokens</span>
    </button>
  );
}

function dismissKey(u) { return `um-usage-warn:${u.period_start}:${u.warning?.threshold}`; }
function isDismissed(u) { try { return localStorage.getItem(dismissKey(u)) === "1"; } catch { return false; } }

/** Warning above the composer at the admin's thresholds (e.g. 75%, 90%); the limit itself shows in the chat. */
export function UsageWarning({ usage, onView }) {
  const [, force] = useState(0);
  if (!usage?.warning || usage.blocked || isDismissed(usage)) return null;
  const dismiss = () => { try { localStorage.setItem(dismissKey(usage), "1"); } catch { /* private mode */ } force((x) => x + 1); };
  return (
    <div className={`usage-warn ${usage.warning.level}`} role="status">
      <Icon name="alert" size={16} />
      <span>{usage.warning.message}</span>
      <button className="link small" onClick={onView}>View usage</button>
      <button className="icon-btn subtle" aria-label="Dismiss" onClick={dismiss}><Icon name="x" size={14} /></button>
    </div>
  );
}

const QUOTA_CODES = new Set(["quota_exceeded", "insufficient_quota", "request_limit"]);
const UPGRADE_CODES = new Set([...QUOTA_CODES, "rag_not_in_plan", "file_limit", "request_too_large"]);

/** Shown in the chat when the server refused a request for the plan's limits. */
export function LimitCard({ limit }) {
  const go = () => { window.location.hash = "#/billing"; };
  const quota = QUOTA_CODES.has(limit.code);
  return (
    <div className="limit-card" role="alert">
      <Icon name="gauge" size={20} />
      <div>
        <b>{limit.code === "quota_exceeded" ? "You've reached your AI usage limit for this billing period."
          : limit.code === "insufficient_quota" ? "Not enough of your AI allowance is left for this request."
          : limit.code === "rate_limited" || limit.code === "too_many_concurrent" ? "Slow down a little."
          : "Your plan does not allow this request."}</b>
        {quota && limit.limit != null ? (
          <>
            <p>Your {limit.plan} plan allows {tokens(limit.limit)} tokens per billing period.
              {limit.code === "insufficient_quota" && ` ${limit.message}`}</p>
            <p className="limit-usage">Your usage: <b>{tokens(limit.used)} / {tokens(limit.limit)}</b>
              {limit.period_end && <span className="muted"> · resets {longDate(limit.period_end)}</span>}</p>
          </>
        ) : <p>{limit.message}</p>}
        {UPGRADE_CODES.has(limit.code) && (
          <div className="row-gap">
            <button className="btn btn-primary btn-sm" onClick={go}>Upgrade plan</button>
            <button className="btn btn-ghost btn-sm" onClick={go}>View usage</button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Single-series bar chart with a hover tooltip. data: [{key, label, value, detail}] */
export function BarChart({ data, height = 150, unit = "tokens", labelEvery }) {
  const [hover, setHover] = useState(null);
  const max = Math.max(...data.map((d) => d.value), 0);
  const every = labelEvery || Math.ceil(data.length / 8);
  if (!data.length) return <p className="empty">No data yet.</p>;
  return (
    <div className="bchart">
      <div className="bchart-max muted small" aria-hidden="true">{max ? compact(max) : ""}</div>
      <div className="bchart-plot" style={{ height }} onMouseLeave={() => setHover(null)}>
        {data.map((d, i) => (
          <div key={d.key} className={`bchart-col${hover === i ? " on" : ""}`} tabIndex={0}
               onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}
               aria-label={`${d.label}: ${tokens(d.value)} ${unit}`}>
            <span className="bchart-bar" style={{ height: max ? `${(d.value / max) * 100}%` : 0 }} />
            {hover === i && (
              <span className={`bchart-tip${i > data.length * 0.6 ? " left" : ""}`} role="tooltip">
                <b>{d.label}</b>{tokens(d.value)} {unit}{d.detail && <small>{d.detail}</small>}
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="bchart-axis" aria-hidden="true">
        {data.map((d, i) => <span key={d.key}>{i % every === 0 || i === data.length - 1 ? d.label : ""}</span>)}
      </div>
    </div>
  );
}

/** Horizontal bars for a few named values (usage by model). */
export function HBars({ rows }) {
  const max = Math.max(...rows.map((r) => r.value), 0);
  if (!rows.length) return <p className="empty">No usage yet.</p>;
  return (
    <ul className="hbars">
      {rows.map((r) => (
        <li key={r.key} title={`${r.label}: ${tokens(r.value)} tokens`}>
          <span className="hbars-label">{r.label}</span>
          <span className="hbars-track"><span style={{ width: max ? `${(r.value / max) * 100}%` : 0 }} /></span>
          <span className="hbars-value">{tokens(r.value)}{r.extra && <small className="muted"> {r.extra}</small>}</span>
        </li>
      ))}
    </ul>
  );
}
