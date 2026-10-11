import { useState } from "react";
import { CHECK_LABEL, CHECK_MARK, badgeClass, label, money } from "../format.js";

// The recommendation, written like a chat answer: the verdict in one line, a short explanation, then short
// sections (why it fits, what to check, who to contact, other options). The full comparison and the agent's
// steps are folded away underneath, one click from view.

const NOT_IN_KB = "Not available in KB";
const TEXT_ONLY = /^Matched from policy text only/i;
const known = (v) => v && v !== NOT_IN_KB && v !== "Unknown";
// A tap-to-call link for a number copied from the knowledge base: only a complete Indian number (10 digits, with an
// optional +91 / 0 prefix); anything else stays plain text rather than dialling a guessed number.
function telHref(v) {
  const d = String(v).replace(/[\s()-]/g, "").replace(/^(\+91|0091|0)/, "");
  return /^[6-9]\d{9}$/.test(d) ? `tel:+91${d}` : null;
}

const STATUS_TEXT = {
  "Strong Match": "Strong match",
  "Possible - Credit Check Required": "Possible match: credit check required",
  "Not Suitable": "Not suitable",
  "More Information Required": "More information required",
};

function Section({ title, items }) {
  if (!items?.length) return null;
  return (
    <>
      <h3 className="md-h">{title}</h3>
      <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </>
  );
}

function Fold({ title, children }) {
  return (
    <details className="res-fold">
      <summary>{title}</summary>
      <div className="res-fold-body">{children}</div>
    </details>
  );
}

function ComparisonRow({ row, top }) {
  const [open, setOpen] = useState(false);
  const n = (r) => row.checks.filter((c) => c.result === r).length;
  return (
    <>
      <tr className={top ? "best" : ""}>
        <td><b>{row.lender}</b><br /><span className="muted">{row.program}</span>{row.sample && <span className="sample">sample</span>}</td>
        <td data-label="Status"><span className={`badge small ${badgeClass(row.status)}`}>{row.status}</span></td>
        <td data-label="Indicative max" className="nowrap">{money(row.max_loan)}</td>
        <td data-label="Checks">
          {n("pass")} pass, {n("fail")} fail, {n("deviation") + n("unknown")} open{" "}
          <button type="button" className="linkish" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Details"}
          </button>
        </td>
      </tr>
      {open && (
        <tr className="detail">
          <td colSpan={4}>
            <ul className="checks">
              {row.checks.map((c, i) => (
                <li key={i}>
                  <span className={`chk chk-${c.result}`} title={CHECK_LABEL[c.result]} aria-label={CHECK_LABEL[c.result]}>
                    {CHECK_MARK[c.result]}
                  </span>
                  <b>{c.criterion}</b> {c.detail}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

function ComparisonTable({ rows }) {
  return (
    <div className="md-table">
      <table className="cmp">
        <thead>
          <tr><th scope="col">Lender and program</th><th scope="col">Status</th><th scope="col">Indicative max</th><th scope="col">Checks</th></tr>
        </thead>
        <tbody>{rows.map((r, i) => <ComparisonRow key={`${r.lender}-${r.program}`} row={r} top={i === 0} />)}</tbody>
      </table>
    </div>
  );
}

export function Trace({ steps }) {
  return <ol className="res-steps">{steps.map((t, i) => <li key={i}>{t}</li>)}</ol>;
}

function Eligibility({ r }) {
  const e = r.eligibility_calc || {};
  const max = e.indicative_max_loan, asked = e.requested;
  if (typeof max === "number" && typeof asked === "number") {
    const covered = max >= asked;
    return (
      <p>
        The indicative maximum is <strong>{money(max)}</strong>, which {covered ? "covers" : "falls short of"} the{" "}
        <strong>{money(asked)}</strong> requested{covered ? "." : ` by ${money(asked - max)}.`}
        {e.method && <span className="muted"> Calculated with the {e.method} method, using the lender's assumptions.</span>}
      </p>
    );
  }
  return r.indicative_eligibility ? <p>{r.indicative_eligibility}</p> : null;
}

function Contact({ r }) {
  const bits = [
    known(r.banker_rm) && <>RM <strong>{r.banker_rm}</strong></>,
    known(r.mobile) && <>mobile <strong>{telHref(r.mobile) ? <a href={telHref(r.mobile)}>{r.mobile}</a> : r.mobile}</strong></>,
    known(r.urban_money_code) && <>Urban Money code <strong>{r.urban_money_code}</strong></>,
  ].filter(Boolean);
  return (
    <>
      <h3 className="md-h">Who to contact</h3>
      {bits.length ? (
        <p>{bits.map((b, i) => <span key={i}>{i > 0 && (i === bits.length - 1 ? " and " : ", ")}{b}</span>)}.
          {known(r.policy_last_verified) && <span className="muted"> Policy last verified {r.policy_last_verified}.</span>}</p>
      ) : (
        <p>The knowledge base has no RM, mobile number or Urban Money code for this lender yet. Ask your manager or the
          product team before logging the file.</p>
      )}
    </>
  );
}

export default function ResultView({ result: r }) {
  const textOnly = r.evidence_source === "private_kb";
  const concerns = (r.concerns_or_deviations || []).filter((c) => !TEXT_ONLY.test(c));
  const others = r.alternative_lenders || [];
  const viable = others.filter((a) => a.status !== "Not Suitable");
  const ruledOut = others.filter((a) => a.status === "Not Suitable");
  const statusText = STATUS_TEXT[r.status] || r.status;

  return (
    <section className="result md" aria-label="Recommendation">
      <p className="res-status"><span className={`res-dot ${badgeClass(r.status)}`} aria-hidden="true" />{statusText}</p>

      {r.top_lender ? (
        <p className="res-lead">
          <strong>{r.top_lender}</strong>{r.program && <> — {r.program}</>} is the best fit for this case
          {r.status === "Strong Match" ? "." : ", subject to the checks below."}
        </p>
      ) : r.message && <p className="res-lead">{r.message}</p>}
      {r.summary && <p>{r.summary}</p>}
      {r.missing_fields?.length > 0 && <p>Still needed: <strong>{r.missing_fields.map(label).join(", ")}</strong>.</p>}

      {textOnly && (
        <blockquote>
          This lender was matched from its policy text only (it has no structured rules yet), so treat the criteria as
          indicative and confirm them with the RM.
        </blockquote>
      )}

      {r.evidence_source === "web_general" && (
        <>
          <blockquote>
            General guidance from public web results. No lender is recommended because none in the knowledge base covers
            this loan type.
          </blockquote>
          {r.web_summary && <p>{r.web_summary}</p>}
          <Section title="Typical criteria" items={r.general_criteria} />
        </>
      )}

      {r.top_lender && (
        <>
          <h3 className="md-h">Eligibility</h3>
          <Eligibility r={r} />
        </>
      )}
      {r.top_lender && <Section title="Why it fits" items={r.why_suitable} />}
      <Section title={r.top_lender ? "Check before you log the file" : "Concerns"} items={concerns} />
      <Section title="Next steps" items={r.next_steps} />
      {r.top_lender && <Contact r={r} />}
      {r.top_lender && (viable.length > 0 || ruledOut.length > 0) && (
        <>
          <h3 className="md-h">Other options</h3>
          <ul>
            {viable.map((a, i) => <li key={`v${i}`}><strong>{a.lender}</strong> — {a.program}{a.status && <span className="muted"> ({a.status})</span>}</li>)}
            {ruledOut.map((a, i) => <li key={`n${i}`}><span className="muted">{a.lender} — {a.program}: not suitable for this case</span></li>)}
          </ul>
        </>
      )}

      {(r.comparison?.length > 0 || r.trace?.length > 0) && (
        <div className="res-folds">
          {r.comparison?.length > 0 && (
            <Fold title={`Compare all ${r.comparison.length} program${r.comparison.length === 1 ? "" : "s"} checked`}>
              <ComparisonTable rows={r.comparison} />
            </Fold>
          )}
          {r.trace?.length > 0 && <Fold title="How I decided"><Trace steps={r.trace} /></Fold>}
        </div>
      )}
      <p className="disclaimer">Indicative only, not a sanction. The lender makes the final decision.</p>
    </section>
  );
}
