import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { inputModeFor } from "../format.js";
import Icon from "./Icon.jsx";

export const MODES = {
  kb: { label: "Ask anything", short: "Ask", icon: "book",
        placeholder: "Ask anything about lenders, codes, RMs, payouts or policies" },
  ask: { label: "Ask about this result", short: "About the result", icon: "spark",
         placeholder: "Why not the other lenders? What if CIBIL is 720?" },
  answer: { label: "Answer the question", short: "Answer", icon: "check", placeholder: "Type your answer" },
  describe: { label: "Describe a loan case", short: "Describe a case", icon: "edit",
              placeholder: "Describe the case, e.g. Salaried, 32, 85k in hand, wants 40 lakh for a resale flat in Pune" },
};
const ACCEPT = ".pdf,.docx,.doc,.txt,.md,.csv,.xls,.xlsx,.xlsm,.json,.html,.htm,.png,.jpg,.jpeg,.webp,.gif,.bmp";
const IS_IMAGE = /\.(png|jpe?g|webp|gif|bmp)$/i;
const isImage = (f) => (f.type || "").startsWith("image/") || IS_IMAGE.test(f.name || "");
// What to ask when only files are sent, like a chat assistant does with a bare upload.
const defaultAsk = (list) => (list.every((f) => f.kind === "image")
  ? `Summarize ${list.length === 1 ? "this image" : "these images"}.`
  : `Summarize ${list.length === 1 ? "this document" : "these documents"}.`);
const MAX_ATTACHMENTS = 5;
// Unsent text per chat, kept while the app is open, so switching chats does not lose a half-typed question.
const drafts = new Map();
// Focusing the box on a touch screen opens the keyboard over the answer being read: only do it with a mouse.
const finePointer = () => window.matchMedia?.("(pointer: fine)").matches ?? true;
const size = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);

export default function Composer({ convKey, modes, defaultMode, question, busy, streaming, onSend, onStop, onMode,
                                   placeholder: heroPlaceholder, prefill }) {
  const [text, setText] = useState("");
  const [override, setOverride] = useState(null);
  const [menu, setMenu] = useState(false);
  const [pinHint, setPinHint] = useState("");
  const [files, setFiles] = useState([]);         // {key, name, size, kind, preview?, status: uploading|ready|error, id?, error?}
  const ref = useRef(null);
  const menuRef = useRef(null);
  const fileRef = useRef(null);

  const mode = override && modes.includes(override) ? override : defaultMode;
  useEffect(() => { onMode?.(mode); }, [mode, onMode]);
  const textRef = useRef(text);
  textRef.current = text;
  useEffect(() => {                            // switching chats: keep this chat's draft, restore the other's
    setOverride(null); setText(drafts.get(convKey) || ""); setFiles([]);
    return () => { if (textRef.current.trim()) drafts.set(convKey, textRef.current); else drafts.delete(convKey); };
  }, [convKey]);
  useEffect(() => {                            // a suggestion chip to complete ("Change the city to ")
    if (!prefill) return;
    setText(prefill.text);
    const el = ref.current;
    if (el) { el.focus(); requestAnimationFrame(() => el.setSelectionRange(prefill.text.length, prefill.text.length)); }
  }, [prefill]);
  useEffect(() => () => files.forEach((f) => f.preview && URL.revokeObjectURL(f.preview)), []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setOverride((o) => (o === "kb" || o === "ask" ? o : null)); }, [defaultMode]);
  useEffect(() => { if (!busy && finePointer()) ref.current?.focus({ preventScroll: true }); }, [busy, convKey, question?.key]);

  useLayoutEffect(() => {                      // grow with the text, up to the CSS max-height
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  useEffect(() => {                            // PIN code: show the city it belongs to (existing lookup)
    const v = text.trim();
    if (mode !== "answer" || question?.kind !== "pin" || !/^\d{6}$/.test(v)) { setPinHint(""); return; }
    let live = true;
    api.pin(v).then((p) => live && setPinHint(`${p.city}, ${p.district}, ${p.state}`)).catch(() => live && setPinHint(""));
    return () => { live = false; };
  }, [text, mode, question]);

  useEffect(() => {
    if (!menu) return;
    const h = (e) => { if (!menuRef.current?.contains(e.target)) setMenu(false); };
    const k = (e) => { if (e.key === "Escape") setMenu(false); };
    document.addEventListener("mousedown", h);
    document.addEventListener("keydown", k);
    return () => { document.removeEventListener("mousedown", h); document.removeEventListener("keydown", k); };
  }, [menu]);

  function attach(list) {
    const room = MAX_ATTACHMENTS - files.length;
    const picked = [...list].slice(0, Math.max(room, 0));
    if (!picked.length) return;
    if (mode !== "kb" && mode !== "ask") setOverride("kb");          // attachments go with questions
    const added = picked.map((f) => ({ key: Math.random().toString(36).slice(2), name: f.name || "pasted-image.png", size: f.size,
      kind: isImage(f) ? "image" : "file", preview: isImage(f) ? URL.createObjectURL(f) : null, status: "uploading" }));
    setFiles((cur) => [...cur, ...added]);
    picked.forEach((f, i) => {
      api.uploadAttachment(f)
        .then((r) => setFiles((cur) => cur.map((x) => (x.key === added[i].key ? { ...x, status: "ready", id: r.id, kind: r.kind || x.kind, readWith: r.read_with } : x))))
        .catch((e) => setFiles((cur) => cur.map((x) => (x.key === added[i].key ? { ...x, status: "error", error: e.message } : x))));
    });
  }

  function onPaste(e) {                         // Ctrl+V a screenshot, like in a chat app
    const pasted = [...(e.clipboardData?.files || [])].filter(isImage);
    if (!pasted.length) return;
    e.preventDefault();
    attach(pasted.map((f, i) => (f.name && f.name !== "image.png" ? f
      : new File([f], `pasted-image-${new Date().toISOString().slice(11, 19).replace(/:/g, "")}${i ? `-${i}` : ""}.png`, { type: f.type }))));
  }

  function remove(f) {
    if (f.preview) URL.revokeObjectURL(f.preview);
    setFiles((cur) => cur.filter((x) => x.key !== f.key));
    if (f.id) api.deleteAttachment(f.id).catch(() => {});
  }

  const uploading = files.some((f) => f.status === "uploading");
  const ready = files.filter((f) => f.status === "ready");
  const withFiles = (mode === "kb" || mode === "ask") && ready.length > 0;
  const canSend = !busy && !streaming && !uploading && (text.trim().length > 0 || withFiles);

  function submit(e) {
    e?.preventDefault();
    if (!canSend) return;
    const sent = withFiles ? ready.map(({ id, name, size: s, kind }) => ({ id, name, size: s, kind })) : [];
    onSend(text.trim() || defaultAsk(sent), mode, sent);
    files.forEach((f) => f.preview && URL.revokeObjectURL(f.preview));
    drafts.delete(convKey);
    textRef.current = "";
    setText("");
    setFiles([]);
  }

  const placeholder = mode === "answer" && question
    ? (question.options ? "Pick an option above, or type it" : question.placeholder || "Type your answer")
    : mode === "kb" && heroPlaceholder ? heroPlaceholder : MODES[mode].placeholder;

  return (
    <form className={`composer${streaming ? " is-streaming" : ""}`} onSubmit={submit}
          onClick={(e) => {                     // a tap on the box's padding (not a button) goes to the text
            if (e.target === e.currentTarget || e.target.classList?.contains("composer-bar")) ref.current?.focus();
          }}
          onDragOver={(e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); }}
          onDrop={(e) => { if (e.dataTransfer?.files?.length) { e.preventDefault(); attach(e.dataTransfer.files); } }}>
      {pinHint && <p className="composer-hint">{pinHint}</p>}
      {files.length > 0 && (
        <ul className="attachments" aria-label="Attached files">
          {files.map((f) => (f.preview ? (
            <li key={f.key} className={`att-img ${f.status}`} title={f.error || f.name}>
              <img src={f.preview} alt={f.name} />
              {f.status === "uploading" && <span className="att-img-state"><span className="spinner light" /><small>Reading…</small></span>}
              {f.status === "error" && <span className="att-img-state err"><Icon name="alert" size={16} /><small>{f.error}</small></span>}
              <button type="button" className="att-remove" aria-label={`Remove ${f.name}`} onClick={() => remove(f)}><Icon name="x" size={12} /></button>
            </li>
          ) : (
            <li key={f.key} className={`att ${f.status}`} title={f.error || f.name}>
              <span className="att-icon">{f.status === "uploading" ? <span className="spinner" /> : <Icon name={f.status === "error" ? "x" : "file"} size={16} />}</span>
              <span className="att-text"><b>{f.name}</b><small>{f.status === "error" ? f.error : f.status === "uploading" ? "Reading…" : size(f.size)}</small></span>
              <button type="button" className="att-remove" aria-label={`Remove ${f.name}`} onClick={() => remove(f)}><Icon name="x" size={12} /></button>
            </li>
          )))}
        </ul>
      )}
      <label htmlFor="composer-input" className="sr-only">{MODES[mode].label}</label>
      <textarea id="composer-input" ref={ref} rows={1} value={text} maxLength={2000}
                placeholder={placeholder} inputMode={mode === "answer" ? inputModeFor(question?.kind) : "text"}
                enterKeyHint="send"
                onChange={(e) => setText(e.target.value)} onPaste={onPaste}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e); }} />
      <div className="composer-bar">
        <div className="composer-tools">
          <button type="button" className="tool-btn" aria-label="Attach files" title="Attach files or images (or paste a screenshot)"
                  disabled={files.length >= MAX_ATTACHMENTS || busy} onClick={() => fileRef.current?.click()}>
            <Icon name="plus" size={18} />
          </button>
          <input ref={fileRef} type="file" multiple hidden accept={ACCEPT}
                 onChange={(e) => { attach(e.target.files); e.target.value = ""; }} />
          <div className="mode-pick" ref={menuRef}>
            <button type="button" className="mode-btn" aria-haspopup="menu" aria-expanded={menu}
                    disabled={modes.length < 2} onClick={() => setMenu((m) => !m)}>
              <Icon name={MODES[mode].icon} size={15} />
              {MODES[mode].short}
              {modes.length > 1 && <Icon name="chevron" size={14} />}
            </button>
            {menu && (
              <div className="menu up" role="menu">
                {modes.map((m) => (
                  <button key={m} type="button" role="menuitemradio" aria-checked={m === mode}
                          onClick={() => { setOverride(m); setMenu(false); ref.current?.focus(); }}>
                    {m === mode ? <Icon name="check" /> : <span className="icon-gap" />}{MODES[m].label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        {streaming ? (
          <button type="button" className="send-btn stop" aria-label="Stop generating" title="Stop" onClick={onStop}>
            <span className="stop-square" />
          </button>
        ) : (
          <button type="submit" className="send-btn" aria-label="Send" disabled={!canSend}>
            {busy ? <span className="spinner light" /> : <Icon name="send" size={18} />}
          </button>
        )}
      </div>
    </form>
  );
}
