import { useEffect, useRef } from "react";
import Icon from "./Icon.jsx";

// The answers so far, as a side panel. "Change" asks that question again in the chat.
export default function CaseSheet({ items, answered, total, onChange, onClose, disabled }) {
  const pct = Math.round((answered / Math.max(total, 1)) * 100);
  const closeRef = useRef(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {                       // below 1280px the sheet covers the chat: focus it, Escape closes it
    if (!window.matchMedia("(max-width: 1279px)").matches) return undefined;
    const opener = document.activeElement;
    closeRef.current?.focus({ preventScroll: true });
    const onKey = (e) => { if (e.key === "Escape" && !document.querySelector("dialog[open], .menu")) onCloseRef.current(); };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); if (opener?.isConnected) opener.focus({ preventScroll: true }); };
  }, []);
  return (
    <aside className="sheet" aria-label="Case sheet">
      <div className="sheet-head">
        <h2>Case sheet</h2>
        <button ref={closeRef} className="icon-btn" aria-label="Close case sheet" onClick={onClose}><Icon name="x" /></button>
      </div>
      <div className="sheet-progress">
        <div className="bar" role="progressbar" aria-label="Questions answered" aria-valuemin={0} aria-valuemax={100}
             aria-valuenow={pct}><i style={{ width: `${pct}%` }} /></div>
        <span>{answered} of {total} answered</span>
      </div>
      {items.length === 0 ? <p className="sheet-empty">Answers appear here as you go.</p> : (
        <dl className="ledger">
          {items.map((it) => (
            <div key={it.key} className={it.skipped ? "row skipped" : "row"}>
              <dt>{it.label}</dt>
              <dd>
                <span>{it.value}</span>
                <button type="button" className="link small" disabled={disabled} aria-label={`Change ${it.label}`}
                        onClick={() => onChange(it.key)}>Change</button>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </aside>
  );
}
