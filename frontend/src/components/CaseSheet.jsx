import Icon from "./Icon.jsx";

// The answers so far, as a side panel. "Change" asks that question again in the chat.
export default function CaseSheet({ items, answered, total, onChange, onClose, disabled }) {
  const pct = Math.round((answered / Math.max(total, 1)) * 100);
  return (
    <aside className="sheet" aria-label="Case sheet">
      <div className="sheet-head">
        <h2>Case sheet</h2>
        <button className="icon-btn" aria-label="Close case sheet" onClick={onClose}><Icon name="x" /></button>
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
