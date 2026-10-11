import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import CaseSheet from "./CaseSheet.jsx";
import Composer from "./Composer.jsx";
import Icon from "./Icon.jsx";
import Markdown from "./Markdown.jsx";
import PlacesMap from "./PlacesMap.jsx";
import ResultView from "./ResultView.jsx";
import { LimitCard, UsageWarning } from "./Usage.jsx";

const SUGGESTIONS = [
  { mode: "kb", label: "Find an RM", text: "Who is the home loan RM for Bajaj Finserv in Pune?" },
  { mode: "kb", label: "Explain a policy", text: "Explain the payout capping and clawback rules" },
  { mode: "describe", label: "Match lenders", text: "Salaried, 32, 85k in hand, wants 40 lakh for a resale flat in Pune, CIBIL 760, no EMIs" },
  { mode: "attach", label: "Analyze a document", text: "Attach a sanction letter, statement or policy and ask about it" },
];
const FOLLOW_UPS = ["Why is this the best fit?", "Why not the other lenders?", "What would make this a Strong Match?"];
const NEAR_BOTTOM = 80;

function useCopy() {
  const [done, setDone] = useState(false);
  const copy = (text) => navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); });
  return [done, copy];
}

function Thinking({ label, steps }) {
  return (
    <div className="msg assistant enter">
      <span className="logo" aria-hidden="true">₹</span>
      <div className="msg-body">
        <p className="thinking" role="status"><span className="dots"><i /><i /><i /></span>{label}</p>
        {steps?.length > 0 && (
          <ol className="stages">{steps.map((s, i) => (
            <li key={i} className={i === steps.length - 1 ? "now" : "done"}>
              {i === steps.length - 1 ? <span className="spinner" /> : <Icon name="check" size={14} />}{s}
            </li>
          ))}</ol>
        )}
      </div>
    </div>
  );
}

function Stages({ stages }) {
  return (
    <ol className="stages" role="status" aria-live="polite">
      {stages.map((s, i) => (
        <li key={i} className={s.done ? "done" : "now"}>
          {s.done ? <Icon name="check" size={14} /> : <span className="spinner" />}{s.text}{!s.done && "…"}
        </li>
      ))}
    </ol>
  );
}

const IMG_RE = /\.(png|jpe?g|webp|gif|bmp)$/i;
const isImg = (a) => a.kind === "image" || (!a.kind && IMG_RE.test(a.name || a.title || ""));
const fileUrl = (id) => `/api/attachments/${encodeURIComponent(id)}/file`;

// Full-size view of an attached image, like clicking an image in a chat app.
function ImageViewer({ id, name, onClose }) {
  const ref = useRef(null);
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);
  return (
    <dialog ref={ref} className="img-viewer" onClose={onClose} onCancel={onClose} aria-label={name}
            onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <button className="icon-btn img-viewer-close" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
      <img src={fileUrl(id)} alt={name} />
      <p className="img-viewer-name">{name}</p>
    </dialog>
  );
}

function SentFiles({ files }) {
  const [open, setOpen] = useState(null);
  const [broken, setBroken] = useState({});
  const images = files.filter((a) => isImg(a) && a.id && !broken[a.id]);
  const others = files.filter((a) => !images.includes(a));
  return (
    <>
      {images.length > 0 && (
        <div className={`sent-images n${Math.min(images.length, 3)}`}>
          {images.map((a) => (
            <button key={a.id} type="button" className="sent-image" onClick={() => setOpen(a)} aria-label={`View ${a.name}`}>
              <img src={fileUrl(a.id)} alt={a.name} loading="lazy" onError={() => setBroken((b) => ({ ...b, [a.id]: true }))} />
            </button>
          ))}
        </div>
      )}
      {others.length > 0 && (
        <ul className="sent-files">{others.map((a, i) => <li key={a.id || i}><Icon name={isImg(a) ? "image" : "file"} size={14} />{a.name}</li>)}</ul>
      )}
      {open && <ImageViewer id={open.id} name={open.name} onClose={() => setOpen(null)} />}
    </>
  );
}

function SourceDialog({ src, isAdmin, onClose }) {
  const ref = useRef(null);
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);
  return (
    <dialog ref={ref} className="dialog wide" onClose={onClose} onCancel={onClose} aria-labelledby="src-title">
      <div className="dialog-body">
        <div className="dialog-head">
          <h2 id="src-title" title={src.source}><Icon name="file" size={18} /> {srcTitle(src)}</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <p className="hint">
          {[src.ref, srcWhere(src), src.version ? `Version ${src.version}` : "", src.edition ? `Edition: ${src.edition}` : "",
            src.older_edition ? "Older edition" : "", ...(src.warnings || []), src.title ? src.source : ""].filter(Boolean).join(" · ")}
        </p>
        {src.attachment && src.kind === "image" && src.attachment_id && (
          <img className="src-image" src={fileUrl(src.attachment_id)} alt={src.title} />
        )}
        <p className="hint">{src.attachment ? (src.kind === "image" ? "What was read from your image:" : "Text read from your file:")
          : "The passage the answer was based on:"}</p>
        <blockquote className="excerpt">{src.excerpt || "No excerpt available."}</blockquote>
        {isAdmin && src.primary && !src.attachment && (
          <a className="btn btn-ghost btn-sm" href={api.kbDownloadUrl(src.source)} download>Download the document</a>
        )}
      </div>
    </dialog>
  );
}

const srcTitle = (s) => s.title || s.source.split("/").pop();
const srcWhere = (s) => s.location || s.section || "";

function Sources({ sources, isAdmin }) {
  const [open, setOpen] = useState(null);
  const [all, setAll] = useState(false);
  if (!sources?.length) return null;
  const shown = all ? sources : sources.slice(0, 4);
  return (
    <div className="sources-block">
      <h4>Sources</h4>
      <div className="source-cards">
        {shown.map((s, i) => s.web && /^https?:\/\//.test(s.url || "") ? (
          <a key={i} className="source-card" href={s.url} target="_blank" rel="noopener noreferrer nofollow" title={s.url}>
            <span className="cite-num" aria-label={`Source ${s.ref}`}>{s.ref}</span>
            <span><b>{s.title}</b><small>{s.location}</small></span>
          </a>
        ) : (
          <button key={i} className="source-card" onClick={() => setOpen(s)} title={`${s.source}${srcWhere(s) ? ` · ${srcWhere(s)}` : ""}`}>
            {s.attachment && s.kind === "image" && s.attachment_id
              ? <img className="source-thumb" src={fileUrl(s.attachment_id)} alt="" />
              : s.ref ? <span className="cite-num" aria-label={`Source ${s.ref}`}>{s.ref}</span> : <Icon name="file" size={16} />}
            <span><b>{srcTitle(s)}</b>{srcWhere(s) && <small>{srcWhere(s)}</small>}
              {(s.edition || s.older_edition || s.warnings?.length > 0) && (
                <small className="src-flags">
                  {s.edition && <span>{s.edition}</span>}
                  {s.older_edition && <span className="warn">older edition</span>}
                  {(s.warnings || []).map((w) => <span key={w} className="warn">{w}</span>)}
                </small>
              )}</span>
          </button>
        ))}
        {sources.length > 4 && !all && <button className="source-more" onClick={() => setAll(true)}>+{sources.length - 4} more</button>}
      </div>
      {open && <SourceDialog src={open} isAdmin={isAdmin} onClose={() => setOpen(null)} />}
    </div>
  );
}

const REPORT_REASONS = [["misleading", "Misleading or wrong"], ["irrelevant", "Not relevant"], ["offensive", "Offensive"], ["other", "Something else"]];

// A sponsored card under an answer, like a chat app's ads: clearly labelled, separate from the answer, with
// Hide / Why this ad / Report. Ads are chosen by the server after the answer and never affect it.
function SponsoredCard({ ad }) {
  const [menu, setMenu] = useState(false);
  const [view, setView] = useState("card");          // card | why | report | hidden | reported
  const menuRef = useRef(null);
  useEffect(() => {
    if (!menu) return;
    const h = (e) => { if (!menuRef.current?.contains(e.target)) setMenu(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [menu]);
  const hide = () => { setMenu(false); setView("hidden"); api.adHide(ad.id).catch(() => {}); };
  const report = (reason) => { setView("reported"); api.adReport(ad.id, reason).catch(() => {}); };

  if (view === "hidden" || view === "reported") {
    return <p className="ad-note">{view === "hidden" ? "Ad hidden. You won't see it again." : "Thanks. The ad was reported and hidden."}</p>;
  }
  return (
    <aside className="ad-block" aria-label="Sponsored">
      <p className="ad-lead">This might also interest you.</p>
      <div className="ad-card">
        <a className="ad-main" href={ad.click_url} target="_blank" rel="noopener noreferrer sponsored">
          {ad.image_url && <img className="ad-img" src={ad.image_url} alt="" loading="lazy" />}
          <span className="ad-text">
            <span className="ad-who"><span className="ad-logo" aria-hidden="true">{ad.advertiser.slice(0, 1).toUpperCase()}</span>{ad.advertiser}</span>
            <b className="ad-title">{ad.title}</b>
            {ad.body && <span className="ad-body">{ad.body}</span>}
            {(ad.cta || ad.domain) && <span className="ad-cta">{ad.cta || "Learn more"}{ad.domain && <span className="muted"> · {ad.domain}</span>}</span>}
          </span>
        </a>
        <div className="ad-side" ref={menuRef}>
          <span className="ad-badge">Ad</span>
          <button type="button" className="icon-btn subtle" aria-label="Ad options" aria-haspopup="menu" aria-expanded={menu}
                  onClick={() => setMenu((x) => !x)}><Icon name="more" size={16} /></button>
          {menu && (
            <div className="menu ad-menu" role="menu">
              <button role="menuitem" onClick={hide}><Icon name="x" />Hide this ad</button>
              <button role="menuitem" onClick={() => { setMenu(false); setView("why"); }}><Icon name="spark" />Why am I seeing this?</button>
              <button role="menuitem" onClick={() => { setMenu(false); setView("report"); }}><Icon name="alert" />Report ad</button>
            </div>
          )}
        </div>
      </div>
      {view === "why" && (
        <p className="ad-why">{ad.why} <button className="link small" onClick={() => setView("card")}>Close</button></p>
      )}
      {view === "report" && (
        <div className="ad-why" role="group" aria-label="Report this ad">
          <span>What's wrong with this ad?</span>
          <div className="chips">
            {REPORT_REASONS.map(([k, l]) => <button key={k} className="chip" onClick={() => report(k)}>{l}</button>)}
            <button className="link small" onClick={() => setView("card")}>Cancel</button>
          </div>
        </div>
      )}
    </aside>
  );
}

// How far the answer can be relied on, from the claim check (app/rag/live_check.py). No source names: partners
// never see knowledge-base documents.
const CONFIDENCE = {
  high: ["High confidence", "Every fact was checked against the documents."],
  medium: ["Medium confidence", "Check these points before using the answer:"],
  low: ["Low confidence", "Confirm with the lender or RM before using this answer:"],
};

function Confidence({ c }) {
  const [open, setOpen] = useState(c.level === "low");
  const [label, lead] = CONFIDENCE[c.level] ?? CONFIDENCE.medium;
  const reasons = c.level === "high" ? [] : c.reasons || [];
  return (
    <div className={`confidence ${c.level}`} role="note">
      <button type="button" className="confidence-head" aria-expanded={open} onClick={() => setOpen((x) => !x)}>
        <Icon name={c.level === "high" ? "check" : "alert"} size={15} />
        <b>{label}</b>
        <span className="muted">{c.level === "high" ? lead : c.corrected ? "An unsupported statement was removed." : ""}</span>
      </button>
      {open && reasons.length > 0 && (
        <div className="confidence-body">
          <span>{lead}</span>
          <ul>{reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

// Same codes as app/services/feedback.py (the server checks them).
const FEEDBACK_REASONS = [
  ["wrong_figure", "Wrong figure"], ["wrong_contact", "Wrong contact"], ["outdated", "Outdated"],
  ["wrong_lender", "Wrong lender or product"], ["not_found_but_exists", "Said not found, but it exists"],
  ["incomplete", "Incomplete"], ["other", "Something else"],
];

// After a thumbs-down: what was wrong, so an admin can check the answer against its sources and fix the cause.
function FeedbackReasons({ onSend, onSkip }) {
  const [reason, setReason] = useState(null);
  const [comment, setComment] = useState("");
  return (
    <div className="feedback-why" role="group" aria-label="What was wrong with this answer?">
      <span>What was wrong?</span>
      <div className="chips">
        {FEEDBACK_REASONS.map(([k, l]) => (
          <button key={k} className={`chip${reason === k ? " on" : ""}`} aria-pressed={reason === k} onClick={() => setReason(k)}>{l}</button>
        ))}
      </div>
      {reason && (
        <form className="feedback-comment" onSubmit={(e) => { e.preventDefault(); onSend(reason, comment.trim()); }}>
          <input type="text" maxLength={1000} value={comment} onChange={(e) => setComment(e.target.value)} autoFocus
                 placeholder="Optional: the correct value, or where you saw it" aria-label="Details" />
          <button className="btn btn-primary btn-sm">Send</button>
        </form>
      )}
      <button className="link small" onClick={onSkip}>Skip</button>
    </div>
  );
}

// Knowledge-base sources are internal: only admins see them. Users keep the answer (and their own uploads).
const KB_CITE = /\s?(?:\[\s*(?:sources?:\s*)?S\d+(?:\s*(?:[,;&]|and)?\s*S?\d+)*\s*\]|【\s*S\d+[^】]{0,20}】)/gi;
const userText = (t) => (t || "").replace(KB_CITE, "");

const AnswerMessage = memo(function AnswerMessage({ m, isLast, busy, isAdmin, actions }) {
  const [copied, copy] = useCopy();
  const [askWhy, setAskWhy] = useState(false);
  const rateDown = () => { setAskWhy(m.feedback !== "down"); actions.rate(m.id, "down"); };
  // Admins: each [S#] in the answer opens the passage it cites (users never see knowledge-base sources).
  const [citeSrc, setCiteSrc] = useState(null);
  const citable = isAdmin && !m.streaming && (m.sources || []).some((s) => s.ref && !s.web && !s.attachment);
  const openCite = useCallback((ref) => setCiteSrc((m.sources || []).find((s) => s.ref === ref) || null), [m.sources]);
  const thinking = m.streaming && !m.text;
  const text = isAdmin ? m.text : userText(m.text);
  const sources = isAdmin ? m.sources : (m.sources || []).filter((s) => s.attachment || s.web);
  return (
    <div className={`msg assistant kind-answer${Date.now() - (m.ts || 0) < 4000 ? " enter" : ""}`}>
      <span className={`logo${m.streaming ? " live" : ""}`} aria-hidden="true">₹</span>
      <div className="msg-body">
        {thinking && !m.stages?.length && <p className="thinking" role="status"><span className="dots"><i /><i /><i /></span>Thinking</p>}
        {thinking && m.stages?.length > 0 && <Stages stages={m.stages} />}
        {(m.places?.places?.length > 0 || m.places?.missing?.length > 0) && <PlacesMap places={m.places} />}
        {text && <Markdown text={text} streaming={m.streaming} onCite={citable ? openCite : null} />}
        {citeSrc && <SourceDialog src={citeSrc} isAdmin={isAdmin} onClose={() => setCiteSrc(null)} />}
        {m.streaming && m.checking && (
          <p className="thinking" role="status"><span className="dots"><i /><i /><i /></span>{typeof m.checking === "string" ? m.checking : "Double-checking figures against the sources"}</p>
        )}
        {m.stopped && <p className="msg-flag">Stopped</p>}

        {m.limit && <LimitCard limit={m.limit} />}
        {m.error && !m.limit && (
          <div className="msg-alert" role="alert">
            <Icon name="alert" size={18} />
            <div><b>Something went wrong.</b><p>{m.error}</p>
              <button className="btn btn-ghost btn-sm" disabled={!!busy} onClick={() => actions.retry(m.id)}>
                <Icon name="refresh" size={14} />Try again</button></div>
          </div>
        )}
        {m.interrupted && (
          <div className="msg-alert" role="alert">
            <Icon name="alert" size={18} />
            <div><b>Connection interrupted.</b><p>The response may be incomplete.</p>
              <div className="row-gap">
                <button className="btn btn-ghost btn-sm" disabled={!!busy} onClick={() => actions.resume(m.id)}><Icon name="play" size={14} />Continue</button>
                <button className="btn btn-ghost btn-sm" disabled={!!busy} onClick={() => actions.retry(m.id)}><Icon name="refresh" size={14} />Retry</button>
              </div></div>
          </div>
        )}

        {!m.streaming && m.confidence && <Confidence c={m.confidence} />}
        {!m.streaming && !m.confidence && m.verification?.unsupported?.length > 0 && (
          <p className="verify-note" role="note">
            <Icon name="alert" size={15} />
            <span>Check before use: {m.verification.unsupported.join(", ")} could not be matched to the source
              documents. {isAdmin ? "Open the sources to confirm the exact figures." : "Confirm the exact figures with the lender or RM."}</span>
          </p>
        )}
        {!m.streaming && <Sources sources={sources} isAdmin={isAdmin} />}

        {!m.streaming && !m.error && m.text && (
          <div className={`msg-actions${isLast ? " show" : ""}`}>
            <button className={`icon-btn subtle${m.feedback === "up" ? " on" : ""}`} aria-pressed={m.feedback === "up"}
                    aria-label="Good response" title="Good response" onClick={() => { setAskWhy(false); actions.rate(m.id, "up"); }}><Icon name="up" size={16} /></button>
            <button className={`icon-btn subtle${m.feedback === "down" ? " on" : ""}`} aria-pressed={m.feedback === "down"}
                    aria-label="Bad response" title="Bad response" onClick={rateDown}><Icon name="down" size={16} /></button>
            {m.replyTo && (
              <button className="icon-btn subtle" aria-label="Regenerate" title="Regenerate" disabled={!!busy}
                      onClick={() => actions.regenerate(m.id)}><Icon name="refresh" size={16} /></button>
            )}
            <button className="icon-btn subtle" aria-label={copied ? "Copied" : "Copy"} title={copied ? "Copied" : "Copy"}
                    onClick={() => copy(text)}><Icon name={copied ? "check" : "copy"} size={16} /></button>
          </div>
        )}
        {askWhy && m.feedback === "down" && (
          <FeedbackReasons onSkip={() => setAskWhy(false)}
                           onSend={(reason, comment) => { setAskWhy(false); actions.explain(m.id, reason, comment); }} />
        )}
        {m.feedbackReason && m.feedback === "down" && !askWhy && (
          <p className="ad-note">Thanks. An admin will check this answer against its sources.</p>
        )}
        {!m.streaming && !m.error && m.ad && <SponsoredCard ad={m.ad} />}
      </div>
    </div>
  );
});

function QuestionActions({ question, canGoBack, onChoose, onSkip, onBack }) {
  return (
    <div className="q-actions">
      {question.required && <span className="req">Needed to match lenders</span>}
      {question.options && (
        <div className="chips" role="group" aria-label="Options">
          {question.options.map((o) => <button key={o} className="chip" onClick={() => onChoose(o)}>{o}</button>)}
        </div>
      )}
      {(canGoBack || !question.required) && (
        <div className="chips subtle">
          {canGoBack && <button className="chip ghost" onClick={onBack}><Icon name="back" size={14} />Back</button>}
          {!question.required && <button className="chip ghost" onClick={onSkip}>Skip this question</button>}
        </div>
      )}
    </div>
  );
}

function Message({ m, active, c, last, onSuggest, isAdmin, actions }) {
  if (m.role === "user") {
    return (
      <div className={`msg user${Date.now() - (m.ts || 0) < 4000 ? " enter" : ""}`}>
        <div className="user-col">
          {m.attachments?.length > 0 && <SentFiles files={m.attachments} />}
          <div className={`bubble${m.skipped ? " skipped" : ""}`}>{m.text}</div>
        </div>
      </div>
    );
  }
  if (m.kind === "answer") return <AnswerMessage m={m} isLast={last} busy={c.busy} isAdmin={isAdmin} actions={actions} />;
  return (
    <div className={`msg assistant kind-${m.kind}${Date.now() - (m.ts || 0) < 4000 ? " enter" : ""}`}>
      <span className="logo" aria-hidden="true">₹</span>
      <div className="msg-body">
        {m.kind === "result" ? <ResultView result={m.result} /> :
         m.kind === "question" ? <p className="q-text">{m.text}</p> :
         m.kind === "error" && m.limit ? <LimitCard limit={m.limit} /> :
         m.kind === "error" ? (
           <div className="msg-alert" role="alert"><Icon name="alert" size={18} /><div><b>Something went wrong.</b><p>{m.text}</p></div></div>
         ) : <Markdown text={m.text} />}
        {active && <QuestionActions question={c.view.question} canGoBack={c.canGoBack}
                                    onChoose={(o) => c.choose(o)} onSkip={c.skip} onBack={c.back} />}
        {m.kind === "result" && last && !c.busy && c.resultCurrent && m.result.top_lender && (
          <div className="chips subtle">
            {FOLLOW_UPS.map((s) => <button key={s} className="chip" onClick={() => onSuggest(s)}>{s}</button>)}
          </div>
        )}
      </div>
    </div>
  );
}

function TopMenu({ conv, onRename, onPin, onArchive, onDelete, onSheet }) {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (!ref.current?.contains(e.target)) { setOpen(false); setConfirm(false); } };
    const k = (e) => { if (e.key === "Escape") { setOpen(false); setConfirm(false); } };
    document.addEventListener("mousedown", h); document.addEventListener("keydown", k);
    return () => { document.removeEventListener("mousedown", h); document.removeEventListener("keydown", k); };
  }, [open]);
  const run = (fn) => () => { setOpen(false); fn(); };
  return (
    <div className="top-menu" ref={ref}>
      <button className="icon-btn" aria-label="Chat options" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="more" size={20} />
      </button>
      {open && (
        <div className="menu down" role="menu">
          <button role="menuitem" onClick={run(onSheet)}><Icon name="sheet" />Case sheet</button>
          {conv && <>
            <button role="menuitem" onClick={run(onRename)}><Icon name="edit" />Rename</button>
            <button role="menuitem" onClick={run(() => onPin(!conv.pinned))}><Icon name="pin" />{conv.pinned ? "Unpin" : "Pin"}</button>
            <button role="menuitem" onClick={run(() => onArchive(true))}><Icon name="archive" />Archive</button>
            {confirm
              ? <button role="menuitem" className="danger-text" onClick={run(onDelete)}><Icon name="trash" />Confirm delete</button>
              : <button role="menuitem" className="danger-text" onClick={() => setConfirm(true)}><Icon name="trash" />Delete</button>}
          </>}
        </div>
      )}
    </div>
  );
}

export default function ChatView({ c, conv, title, health, isAdmin, sidebarOpen, onOpenSidebar, onNewChat,
                                   onRename, onPin, onArchive, onDelete, usage, onViewUsage }) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const [mode, setMode] = useState(c.defaultMode);
  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [atBottom, setAtBottom] = useState(true);
  const stick = useRef(true);
  const scroller = useRef(null);
  const { data, view, busy } = c;
  const messages = data.messages;
  const empty = messages.length === 0 && c.status === "ready";

  useEffect(() => {                            // below 1280px the sheet is an overlay: close it when shrinking
    const mq = window.matchMedia("(min-width: 1280px)");
    const onChange = (e) => { if (!e.matches) setSheetOpen(false); };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  useEffect(() => { setRenaming(false); }, [c.key]);

  // Auto-scroll: follow new content while the reader is at the bottom; stop when they scroll up.
  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM;
    stick.current = near;
    setAtBottom(near);
  }, []);
  const toBottom = useCallback((smooth) => {
    const el = scroller.current;
    if (!el) return;
    stick.current = true;
    setAtBottom(true);
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);
  const last = messages.at(-1);
  useLayoutEffect(() => { stick.current = true; toBottom(false); }, [c.key, toBottom]);
  useLayoutEffect(() => { if (last?.role === "user") toBottom(false); }, [messages.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const sig = `${messages.length}:${last?.text?.length || 0}:${last?.stages?.length || 0}:${last?.sources ? 1 : 0}:${busy}:${c.steps.length}`;
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [sig]);

  let lastQ = -1, lastResult = -1;
  messages.forEach((m, i) => { if (m.kind === "question") lastQ = i; if (m.kind === "result") lastResult = i; });
  const answering = !busy && view.question && (lastResult < lastQ || mode === "answer");
  const activeQ = answering && lastQ > lastResult && messages[lastQ].key === view.question.key ? lastQ : -1;
  const repeatQ = answering && activeQ === -1 && c.status === "ready" && mode === "answer";
  const answeredCount = view.summary.filter((s) => !s.skipped).length;
  const actions = useMemo(() => ({ retry: c.retry, resume: c.resume, regenerate: c.regenerate, rate: c.rate, explain: c.explain }),
    [c.retry, c.resume, c.regenerate, c.rate, c.explain]);

  const composer = c.status === "ready" && (
    <Composer convKey={c.key} modes={c.modes} defaultMode={c.defaultMode} question={view.question}
              busy={!!busy && !c.streaming} streaming={c.streaming} onSend={c.send} onStop={c.stop} onMode={setMode}
              placeholder={empty ? "Ask anything" : undefined} />
  );

  function startRename() { setDraftTitle(title); setRenaming(true); }
  async function saveRename(e) {
    e?.preventDefault();
    const t = draftTitle.trim();
    setRenaming(false);
    if (t && t !== title) await onRename(t).catch(() => {});
  }

  return (
    <div className={`chat-main${empty ? " is-empty" : ""}${sheetOpen ? " sheet-open" : ""}`}>
      <header className="topbar">
        {!sidebarOpen && (
          <button className="icon-btn" aria-label="Open sidebar" onClick={onOpenSidebar}><Icon name="menu" size={20} /></button>
        )}
        {renaming ? (
          <form className="title-edit" onSubmit={saveRename}>
            <label htmlFor="title-input" className="sr-only">Chat name</label>
            <input id="title-input" autoFocus maxLength={120} value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)}
                   onBlur={saveRename} onKeyDown={(e) => { if (e.key === "Escape") setRenaming(false); }} />
          </form>
        ) : <h1 className="top-title" title={title}>{conv?.pinned && <Icon name="pin" size={14} className="pin-mark" />}{title}</h1>}
        <span className={`status-dot ${health.ok === true ? "ok" : health.ok === false ? "bad" : ""}`} title={health.text}
              role="status" aria-label={health.text} />
        {answeredCount > 0 && (
          <button className={`btn btn-ghost btn-sm sheet-toggle${sheetOpen ? " on" : ""}`} aria-expanded={sheetOpen}
                  onClick={() => setSheetOpen((o) => !o)}>
            <Icon name="sheet" size={16} /><span>Case sheet</span><span className="count">{answeredCount}</span>
          </button>
        )}
        <button className="icon-btn" aria-label="New chat" title="New chat" onClick={onNewChat}><Icon name="compose" size={20} /></button>
        <TopMenu conv={conv} onRename={startRename} onPin={onPin} onArchive={onArchive} onDelete={onDelete}
                 onSheet={() => setSheetOpen((o) => !o)} />
      </header>

      <div className="thread-scroll" ref={scroller} onScroll={onScroll}>
        {c.status === "loading" && <div className="thread"><Thinking label="Loading chat" /></div>}
        {(c.status === "missing" || c.status === "error") && (
          <div className="thread"><p className="msg-error center">{c.status === "missing" ? "This chat was not found. It may have been deleted." : c.error}</p></div>
        )}
        {empty && (
          <div className="hero">
            <span className="logo xl" aria-hidden="true">₹</span>
            <h2>How can I help you?</h2>
            <UsageWarning usage={usage} onView={onViewUsage} />
            <div className="hero-composer">{composer}</div>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s.label} className="suggestion" disabled={!!busy}
                        onClick={() => (s.mode === "attach" ? document.querySelector(".composer input[type=file]")?.click() : c.send(s.text, s.mode))}>
                  <b>{s.label}</b><span>{s.text}</span>
                </button>
              ))}
            </div>
            {view.question?.options && (
              <div className="case-start">
                <p>Or match lenders for a case. Pick the loan type:</p>
                <div className="chips center" role="group" aria-label={view.question.text}>
                  {view.question.options.map((o) => (
                    <button key={o} className="chip" disabled={!!busy} onClick={() => c.choose(o)}>{o}</button>
                  ))}
                </div>
              </div>
            )}
            {c.error && <p className="msg-error">{c.error}</p>}
          </div>
        )}
        {!empty && c.status === "ready" && (
          <div className="thread">
            {messages.map((m, i) => (
              <Message key={m.id} m={m} c={c} active={i === activeQ} last={i === messages.length - 1}
                       onSuggest={(s) => c.send(s, "ask")} isAdmin={isAdmin} actions={actions} />
            ))}
            {repeatQ && (
              <Message m={{ id: "open-q", role: "assistant", kind: "question", text: view.question.text }} c={c} active
                       last={false} onSuggest={() => {}} isAdmin={isAdmin} actions={actions} />
            )}
            {busy === "recommend" && <Thinking label="Checking lender policies" steps={c.steps} />}
            {(busy === "describe" || busy === "answer") && <Thinking label={busy === "describe" ? "Reading the description" : "Thinking"} />}
          </div>
        )}
      </div>

      {!empty && (
        <div className="composer-wrap">
          {!atBottom && (
            <button className="jump" aria-label="Scroll to the latest message" onClick={() => toBottom(true)}>
              <Icon name="arrowDown" size={18} />
            </button>
          )}
          {c.canFind && !busy && c.status === "ready" && (
            <div className="ready-bar">
              <span>{c.hasResult ? "The case changed since the last search." : view.message}</span>
              <button className="btn btn-primary btn-sm" onClick={c.find}>Find suitable banks</button>
            </div>
          )}
          <UsageWarning usage={usage} onView={onViewUsage} />
          {composer}
          <p className="fineprint">AI answers can be wrong. Lender matches are indicative, not a sanction.</p>
        </div>
      )}

      {sheetOpen && (
        <>
          <div className="scrim sheet-scrim" onClick={() => setSheetOpen(false)} />
          <CaseSheet items={view.summary} answered={view.answered} total={view.total} disabled={!!busy}
                     onChange={(k) => { c.change(k); if (window.innerWidth < 1280) setSheetOpen(false); }}
                     onClose={() => setSheetOpen(false)} />
        </>
      )}
    </div>
  );
}
