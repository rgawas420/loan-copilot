import { useCallback, useRef, useState } from "react";
import { api, streamChat, streamRecommend } from "./api.js";
import { label } from "./format.js";

// One chat holds a loan case and/or free questions. The questionnaire, recommendation and AI answers all run
// on the backend; this hook turns each step into chat messages, streams AI answers token by token, and saves
// the conversation.
//
// Conversations live in a local store keyed by id, so a reply that is still streaming lands in the
// conversation it belongs to even after the user switches chats. A new chat is a "draft" until its first
// message, when it is created on the server.
//
// AI answer message: { id, role: "assistant", kind: "answer", mode, text, streaming, stages: [{text, done}],
//   checking, sources, verification, places, ad, replyTo, stopped, interrupted, error, limit, feedback, feedbackReason, confidence }
// checking: the answer is being checked against the sources (the text of the step); a rewrite replaces text.
// confidence: {level: high / medium / low, reasons} from the claim check, shown under the answer.
// limit: the server refused the request for the plan's limits ({code, message, plan, limit, used, ...}).

const EMPTY_VIEW = { question: null, answered: 0, total: 1, ready: false, message: "", summary: [] };
const SAVE_DELAY_MS = 400;
const FLUSH_MS = 33;                     // batch streamed tokens into ~30 renders a second

const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
const emptyData = () => ({ case: {}, stack: [], messages: [], result: null, resultFor: null, autoTitle: true });
const entryFor = (data) => ({ data, view: EMPTY_VIEW, busy: null, steps: [], status: "ready", error: "", abort: null });
const viewOf = (r) => ({ question: r.question, answered: r.answered, total: r.total, ready: r.ready,
                         message: r.message, summary: r.summary || [] });
const caseKey = (c) => JSON.stringify(c || {});
const answeredCount = (c) => Object.keys(c || {}).filter((k) => !k.startsWith("_")).length;

const withMessages = (data, ...msgs) =>
  ({ ...data, messages: [...data.messages, ...msgs.filter(Boolean).map((m) => ({ id: uid(), ts: Date.now(), ...m }))] });
const questionMsg = (view) =>
  view.question && { role: "assistant", kind: "question", text: view.question.text, key: view.question.key };
const readyMsg = (before, after) =>
  after.ready && !before.ready && {
    role: "assistant", kind: "note",
    text: `${after.message} Select **Find suitable banks** when you are ready` +
      (after.question ? ", or answer a few optional questions to sharpen the match." : "."),
  };
const resultText = (r) =>
  [r.status, r.top_lender && `${r.top_lender}: ${r.program}`, r.summary || r.message].filter(Boolean).join(" · ");

function titleFor(mode, text) {
  const t = text.replace(/\s+/g, " ").trim();
  if (mode === "answer" && t.length < 40) return `${t} case`;
  return t.length > 60 ? `${t.slice(0, 57).trimEnd()}…` : t;
}

/** Earlier turns of the same kind of conversation, for the model's context. */
function chatHistory(messages, mode) {
  const out = [];
  for (const m of messages) {
    if (m.kind === "result") { if (mode === "ask") out.length = 0; continue; }
    if (m.mode !== mode || !m.text || m.error) continue;
    if (m.role === "user" || m.kind === "answer") out.push({ role: m.role, text: m.text });
  }
  return out.slice(-12);
}

/** A saved conversation may have been closed mid-answer: show that answer as interrupted, not streaming. */
function normalise(data) {
  return {
    ...emptyData(), ...data,
    messages: (data.messages || []).map((m) => (m.streaming
      ? { ...m, streaming: false, stages: (m.stages || []).map((s) => ({ ...s, done: true })),
          interrupted: !!m.text, error: m.text ? null : "The answer did not finish." }
      : m)),
  };
}

/** Plan-limit details of a refused request (quota, rate, size), for the limit card. */
const limitOf = (err) => (err?.data?.code ? err.data : null);

export function useConversation({ onCreated, onSaved, onUsage }) {
  const store = useRef(new Map());       // key -> entry
  const alias = useRef(new Map());       // draft key -> server id, once created
  const creating = useRef(new Map());    // draft key -> Promise<id>
  const timers = useRef(new Map());
  const currentKey = useRef(null);
  const [current, setCurrent] = useState(() => ({ key: null, id: null, ...entryFor(emptyData()) }));

  const resolve = (key) => alias.current.get(key) || key;
  const isDraft = (key) => key?.startsWith("draft:");
  const get = (key) => store.current.get(resolve(key));

  const show = useCallback((key) => {
    const k = resolve(key);
    if (k !== resolve(currentKey.current)) return;
    const e = store.current.get(k);
    if (e) setCurrent({ key: k, id: isDraft(k) ? null : k, ...e });
  }, []);

  const save = useCallback((id) => {
    clearTimeout(timers.current.get(id));
    timers.current.set(id, setTimeout(async () => {
      timers.current.delete(id);
      const e = store.current.get(id);
      if (!e) return;
      try { onSaved?.(await api.updateConversation(id, { data: e.data })); }
      catch (err) { if (err.status !== 404) console.warn("Could not save the conversation", err); }
    }, SAVE_DELAY_MS));
  }, [onSaved]);

  // Apply fn to a conversation's entry; persist when its data changed.
  const patch = useCallback((key, fn) => {
    const k = resolve(key);
    const e = store.current.get(k);
    if (!e) return;
    const next = { ...e, ...fn(e) };
    store.current.set(k, next);
    show(k);
    if (!isDraft(k) && next.data !== e.data) save(k);
  }, [show, save]);

  const updateMsg = useCallback((key, msgId, fn) => patch(key, (e) => ({
    data: { ...e.data, messages: e.data.messages.map((m) => (m.id === msgId ? { ...m, ...fn(m) } : m)) },
  })), [patch]);

  const ensureId = useCallback(async (key, title) => {
    const k = resolve(key);
    if (!isDraft(k)) return k;
    if (!creating.current.has(k)) {
      creating.current.set(k, (async () => {
        const conv = await api.createConversation(title, store.current.get(k).data);
        store.current.set(conv.id, store.current.get(k));
        store.current.delete(k);
        alias.current.set(k, conv.id);
        if (currentKey.current === k) currentKey.current = conv.id;
        onCreated?.(conv, currentKey.current === conv.id);
        show(conv.id);
        save(conv.id);                          // anything added while the create call was in flight
        return conv.id;
      })().finally(() => creating.current.delete(k)));
    }
    return creating.current.get(k);
  }, [onCreated, show, save]);

  // ---------------------------------------------------------------- opening chats
  const newChat = useCallback(async (initialCase = {}, note = "") => {
    const key = `draft:${uid()}`;
    let data = { ...emptyData(), case: initialCase };
    if (note) data = withMessages(data, { role: "assistant", kind: "note", text: note });
    store.current.set(key, entryFor(data));
    currentKey.current = key;
    show(key);
    try {
      const r = await api.next(initialCase);
      patch(key, (e) => ({ view: viewOf(r), data: note ? withMessages(e.data, questionMsg(viewOf(r))) : e.data }));
    } catch (err) {
      patch(key, () => ({ error: err.message }));
    }
  }, [show, patch]);

  const open = useCallback(async (id) => {
    currentKey.current = id;
    if (store.current.has(id)) { show(id); return; }
    store.current.set(id, { ...entryFor(emptyData()), status: "loading" });
    show(id);
    try {
      const conv = await api.conversation(id);
      const data = normalise(conv.data);
      store.current.set(id, entryFor(data));
      show(id);
      const r = await api.next(data.case);
      store.current.set(id, { ...store.current.get(id), view: viewOf(r) });
      show(id);
    } catch (err) {
      store.current.set(id, { ...entryFor(emptyData()), status: err.status === 404 ? "missing" : "error", error: err.message });
      show(id);
    }
  }, [show]);

  const forget = useCallback((id) => {
    store.current.get(id)?.abort?.abort();
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    store.current.delete(id);
  }, []);

  // ---------------------------------------------------------------- turns
  const begin = (key, busy) => {
    const e = get(key);
    if (!e || e.busy || e.status !== "ready") return false;
    patch(key, () => ({ busy, error: "" }));
    return true;
  };
  const end = (key) => patch(key, () => ({ busy: null, steps: [], abort: null }));
  const fail = (key, err) => patch(key, (e) => ({ data: withMessages(e.data,
    { role: "assistant", kind: "error", text: err.message, limit: limitOf(err) }) }));

  /** Stream an AI answer into message msgId. continueFrom: text already there, to be continued. */
  const streamInto = useCallback(async (key, msgId, continueFrom = "") => {
    const ctrl = new AbortController();
    patch(key, () => ({ abort: ctrl }));
    updateMsg(key, msgId, () => ({ streaming: true, checking: false, stages: [], error: null, limit: null, stopped: false, interrupted: false,
                                    ...(continueFrom ? {} : { text: "", sources: null, verification: null, places: null, ad: null, confidence: null, feedback: null, feedbackReason: null }) }));
    const e = get(key);
    const msgs = e.data.messages;
    const reply = msgs.find((m) => m.id === msgId);
    const askIdx = msgs.findIndex((m) => m.id === reply.replyTo);
    const ask = msgs[askIdx];
    const body = {
      mode: reply.mode, question: ask.text, history: chatHistory(msgs.slice(0, askIdx), reply.mode),
      case: e.data.case, result: reply.mode === "ask" ? e.data.result || {} : {},
      loan_type: e.data.case.loan_type || null,
      attachment_ids: (ask.attachments || []).map((a) => a.id), partial: continueFrom,
      conversation_id: isDraft(resolve(key)) ? null : resolve(key), message_id: msgId,
    };
    let buf = "", timer = 0, wrote = !!continueFrom;
    const flush = () => {
      timer = 0;
      if (!buf) return;
      const add = buf;
      buf = "";
      updateMsg(key, msgId, (m) => ({ text: (m.text || "") + add }));
    };
    const finishStages = (m) => (m.stages || []).map((s) => ({ ...s, done: true }));
    try {
      await streamChat(body, {
        signal: ctrl.signal,
        onStatus: (st) => {
          if (st.stage === "check") { clearTimeout(timer); flush(); updateMsg(key, msgId, () => ({ checking: st.text || true })); return; }
          updateMsg(key, msgId, (m) => ({ stages: [...finishStages(m), { stage: st.stage, text: st.text, done: false }] }));
        },
        onReplace: (text) => { clearTimeout(timer); buf = ""; updateMsg(key, msgId, () => ({ text, checking: false })); },
        onToken: (t) => {
          if (!wrote) { wrote = true; updateMsg(key, msgId, (m) => ({ stages: finishStages(m) })); }
          buf += t;
          if (!timer) timer = setTimeout(flush, FLUSH_MS);
        },
        onSources: (src) => { clearTimeout(timer); flush(); updateMsg(key, msgId, () => ({ sources: src })); },
        onVerification: (v) => updateMsg(key, msgId, () => ({ verification: v })),
        onConfidence: (c) => updateMsg(key, msgId, () => ({ confidence: c })),
        onPlaces: (p) => updateMsg(key, msgId, () => ({ places: p })),
        onAd: (ad) => updateMsg(key, msgId, () => ({ ad })),
        onUsage: (u) => onUsage?.(u.usage),
      });
      clearTimeout(timer); flush();
      updateMsg(key, msgId, (m) => ({ streaming: false, checking: false, stages: finishStages(m) }));
    } catch (err) {
      clearTimeout(timer); flush();
      updateMsg(key, msgId, (m) => {
        const base = { streaming: false, checking: false, stages: finishStages(m) };
        if (err.name === "AbortError") return { ...base, stopped: true };
        if (wrote && (m.text || "").trim()) return { ...base, interrupted: true, interruptReason: err.message };
        return { ...base, error: err.network ? "I couldn't reach the server, so no answer was generated." : err.message,
                 limit: limitOf(err) };
      });
      if (limitOf(err)) onUsage?.(null);            // refresh the usage indicator
    } finally {
      end(key);
    }
  }, [patch, updateMsg, onUsage]);

  /** A typed message. mode: describe (free-text intake), answer (current question), ask (follow-up), kb. */
  const send = useCallback(async (text, mode, attachments = []) => {
    const key = currentKey.current;
    text = text.trim();
    if (!text || !begin(key, mode === "ask" || mode === "kb" ? "stream" : mode)) return;
    const userId = uid();
    patch(key, (e) => ({ data: withMessages(e.data, { id: userId, role: "user", text, mode,
      ...(attachments.length ? { attachments } : {}) }) }));
    if (mode === "ask" || mode === "kb") {
      const replyId = uid();
      patch(key, (e) => ({ data: withMessages(e.data, { id: replyId, role: "assistant", kind: "answer", mode,
        text: "", streaming: true, stages: [], replyTo: userId }) }));
      try { await ensureId(key, titleFor(mode, text)); }
      catch (err) { updateMsg(key, replyId, () => ({ streaming: false, error: err.message })); end(key); return; }
      await streamInto(key, replyId);
      return;
    }
    try {
      await ensureId(key, titleFor(mode, text));
      const e = get(key);
      if (mode === "describe") {
        const r = await api.intake(e.data.case, text, resolve(key));
        onUsage?.(null);
        const v = viewOf(r);
        const rejected = r.rejected.map((x) => `${label(x.key)} (${x.reason})`);
        const note = (r.filled.length
          ? `I filled ${r.filled.length} answer${r.filled.length === 1 ? "" : "s"} from your description: ` +
            `${r.filled.map(label).join(", ")}. You can check or change them in the case sheet.`
          : "I couldn't pick out any answers from that, so let's go question by question.") +
          (rejected.length ? `\n\nNot used: ${rejected.join("; ")}.` : "");
        patch(key, (x) => ({ view: v, data: withMessages({ ...x.data, case: r.case },
          { role: "assistant", kind: "note", text: note }, readyMsg(x.view, v), questionMsg(v)) }));
      } else {
        const q = e.view.question;
        if (!q) throw new Error("There is no open question right now.");
        const r = await api.answer(e.data.case, q.key, text);
        const v = viewOf(r);
        patch(key, (x) => ({ view: v, data: withMessages({ ...x.data, case: r.case, stack: [...x.data.stack, q.key] },
          readyMsg(x.view, v), questionMsg(v)) }));
      }
    } catch (err) { fail(key, err); }
    finally { end(key); }
  }, [patch, ensureId, streamInto, updateMsg, onUsage]);

  /** Regenerate (or retry) an AI answer in place: same question, no duplicate user message. */
  const regenerate = useCallback(async (msgId) => {
    const key = currentKey.current;
    if (!begin(key, "stream")) return;
    await streamInto(key, msgId);
  }, [streamInto]);

  /** Continue an answer that was cut off, appending to it. */
  const resume = useCallback(async (msgId) => {
    const key = currentKey.current;
    const m = get(key)?.data.messages.find((x) => x.id === msgId);
    if (!m || !begin(key, "stream")) return;
    await streamInto(key, msgId, m.text || "");
  }, [streamInto]);

  const stop = useCallback(() => { get(currentKey.current)?.abort?.abort(); }, []);

  const rate = useCallback((msgId, rating) => {
    const key = currentKey.current;
    const m = get(key)?.data.messages.find((x) => x.id === msgId);
    if (!m) return;
    const next = m.feedback === rating ? null : rating;
    updateMsg(key, msgId, () => ({ feedback: next, feedbackReason: null }));
    const id = resolve(key);
    if (!isDraft(id)) api.feedback(id, msgId, next).catch(() => {});
  }, [updateMsg]);

  /** What was wrong with an answer the user disliked (for the admins' review queue). */
  const explain = useCallback((msgId, reason, comment) => {
    const key = currentKey.current;
    updateMsg(key, msgId, () => ({ feedbackReason: reason }));
    const id = resolve(key);
    if (!isDraft(id)) api.feedback(id, msgId, "down", reason, comment).catch(() => {});
  }, [updateMsg]);

  /** Click on an option chip, or Skip, for the open question. */
  const choose = useCallback(async (value, skip = false) => {
    const key = currentKey.current;
    const q = get(key)?.view.question;
    if (!q || !begin(key, "answer")) return;
    patch(key, (e) => ({ data: withMessages(e.data, { role: "user", text: skip ? "Skip this question" : value, mode: "answer", skipped: skip }) }));
    try {
      await ensureId(key, skip ? "New case" : titleFor("answer", value));
      const r = await api.answer(get(key).data.case, q.key, value, skip);
      const v = viewOf(r);
      patch(key, (x) => ({ view: v, data: withMessages({ ...x.data, case: r.case, stack: [...x.data.stack, q.key] },
        readyMsg(x.view, v), questionMsg(v)) }));
    } catch (err) { fail(key, err); }
    finally { end(key); }
  }, [patch, ensureId]);

  // Remove one answer and ask that question again (Back, or Change in the case sheet).
  const reopen = useCallback(async (key, field, why) => {
    if (!begin(key, "answer")) return;
    try {
      const e = get(key);
      const c = { ...e.data.case };
      delete c[field];
      if (c._skipped) c._skipped = c._skipped.filter((k) => k !== field);
      const r = await api.next(c);
      const v = viewOf(r);
      patch(key, (x) => ({ view: v, data: withMessages(
        { ...x.data, case: r.case, stack: x.data.stack.filter((k) => k !== field) },
        { role: "assistant", kind: "note", text: why }, questionMsg(v)) }));
    } catch (err) { fail(key, err); }
    finally { end(key); }
  }, [patch]);

  const back = useCallback(() => {
    const key = currentKey.current;
    const field = get(key)?.data.stack.at(-1);
    if (field) reopen(key, field, `Going back to ${label(field).toLowerCase()}.`);
  }, [reopen]);

  const change = useCallback((field) => {
    reopen(currentKey.current, field, `Let's change ${label(field).toLowerCase()}.`);
  }, [reopen]);

  /** Run the recommendation agent; its steps stream into the chat as they happen. */
  const find = useCallback(async () => {
    const key = currentKey.current;
    if (!begin(key, "recommend")) return;
    const ctrl = new AbortController();
    patch(key, (e) => ({ abort: ctrl, data: withMessages(e.data, { role: "user", text: "Find suitable banks" }) }));
    try {
      const id = await ensureId(key, "New case");
      const c = get(key).data.case;
      const result = await streamRecommend(c, id, (line) => patch(key, (e) => ({ steps: [...e.steps, line] })), ctrl.signal,
                                           (u) => onUsage?.(u.usage));
      patch(key, (e) => ({ data: withMessages({ ...e.data, result, resultFor: caseKey(c) },
        { role: "assistant", kind: "result", result, text: resultText(result) }) }));
      const e = get(key);
      if (e?.data.autoTitle && c.loan_type) {
        const title = `${c.loan_type} · ${result.top_lender || result.status}`;
        api.updateConversation(resolve(key), { title }).then(onSaved).catch(() => {});
      }
    } catch (err) {
      if (err.name === "AbortError") patch(key, (e) => ({ data: withMessages(e.data,
        { role: "assistant", kind: "note", text: "Stopped. Select **Find suitable banks** to run the search again." }) }));
      else fail(key, err);
    }
    finally { end(key); }
  }, [patch, ensureId, onSaved, onUsage]);

  const rename = useCallback(async (id, title) => {
    const conv = await api.updateConversation(id, { title });
    if (store.current.has(id)) {
      store.current.set(id, { ...store.current.get(id), data: { ...store.current.get(id).data, autoTitle: false } });
      save(id);
    }
    return conv;
  }, [save]);

  // Derived for the screen.
  const { data, view } = current;
  const hasResult = !!data.result;
  const resultCurrent = hasResult && data.resultFor === caseKey(data.case);
  const answered = answeredCount(data.case);
  const modes = ["kb", hasResult ? "ask" : null, view.question && answered > 0 ? "answer" : null,
                 !hasResult ? "describe" : null].filter(Boolean);
  const defaultMode = resultCurrent ? "ask" : answered > 0 && view.question ? "answer" : hasResult ? "ask" : "kb";

  return {
    ...current, hasResult, resultCurrent, modes, defaultMode,
    streaming: current.busy === "stream" || current.busy === "recommend",
    canFind: view.ready && !resultCurrent,
    canGoBack: data.stack.length > 0,
    newChat, open, forget, send, choose, skip: () => choose(null, true), back, change, find, rename,
    regenerate, retry: regenerate, resume, stop, rate, explain,
  };
}
