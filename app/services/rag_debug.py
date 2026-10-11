"""RAG debugging and evaluation (admin only).

Traces: for every knowledge-base answer, the original and rewritten question, every retrieved candidate with
its vector / keyword / reranker scores and why it was kept or dropped, the evidence check, the exact prompt,
model and parameters, the answer, its citations and the figure check. Kept in the app database (last
RAG_TRACE_KEEP); attachments are recorded by name only.

Diagnosis: given what the answer should have been (document, page, value), walks the debugging order —
extraction, chunking, retrieval, metadata filtering, reranking, final context, generation — and reports the
first stage that lost the answer.

Golden tests: admin-written questions with the expected answer / document / page, run on demand and
automatically when the configuration (models, prompt, reranker, chunking...) or the documents change. Each
run stores the configuration it ran with, so results can be compared across changes.
"""
import contextvars
import hashlib
import itertools
import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.core.config import get_settings
from app.services import db

log = logging.getLogger(__name__)
_run_lock = threading.Lock()
_pending = {"trigger": None}
state = {"running": False, "trigger": None, "progress": ""}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn():
    c = sqlite3.connect(get_settings().audit_db, timeout=db.BUSY_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    return db.tune(c)


def init_db():
    get_settings().audit_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(_conn()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rag_traces(id TEXT PRIMARY KEY, ts TEXT, user_id INTEGER, origin TEXT, mode TEXT,
            question TEXT, rewritten TEXT, sufficient INTEGER, not_found INTEGER, flagged INTEGER, model TEXT,
            config_hash TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS rag_traces_ts ON rag_traces(ts);
        CREATE TABLE IF NOT EXISTS rag_golden(id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
            expected_answer TEXT, expected_document TEXT, expected_page INTEGER, loan_type TEXT, notes TEXT,
            active INTEGER NOT NULL DEFAULT 1, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS rag_eval_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT,
            trigger TEXT, status TEXT, config_hash TEXT, config TEXT, total INTEGER, passed INTEGER, results TEXT);
        """)
        have = {r["name"] for r in c.execute("PRAGMA table_info(rag_golden)")}
        for col in GOLDEN_ADDED_COLUMNS:
            if col not in have:
                c.execute(f"ALTER TABLE rag_golden ADD COLUMN {col} TEXT")
        c.execute("UPDATE rag_golden SET category=substr(notes, 7) WHERE category IS NULL AND notes LIKE 'seed: %'")
        run_cols = {r["name"] for r in c.execute("PRAGMA table_info(rag_eval_runs)")}
        if "heartbeat" not in run_cols:
            c.execute("ALTER TABLE rag_eval_runs ADD COLUMN heartbeat TEXT")
        if "baseline" not in run_cols:          # the approved run the release gate compares every run with
            c.execute("ALTER TABLE rag_eval_runs ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0")
        have = {r["name"] for r in c.execute("PRAGMA table_info(rag_traces)")}
        for col in ("conversation_id", "message_id"):         # the chat message an answer belongs to (feedback)
            if col not in have:
                c.execute(f"ALTER TABLE rag_traces ADD COLUMN {col} TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS rag_traces_message ON rag_traces(conversation_id, message_id)")
        # A run whose process died stays 'running'. Only a run without progress for a while is marked interrupted:
        # a run started by a script (python -m ...) may still be going while the app starts.
        stale = (datetime.now(timezone.utc) - STALE_RUN).isoformat(timespec="seconds")
        c.execute("UPDATE rag_eval_runs SET status='interrupted' WHERE status='running' AND COALESCE(heartbeat, started) < ?",
                  (stale,))
        c.commit()


# ------------------------------------------------------------------ configuration
def config() -> dict:
    """Everything that decides an answer. Recorded with every trace and run; a change triggers the golden tests."""
    from app.rag import grounding, rerank, retrieval
    from app.rag.llm import configured, describe_params, model_for, rag_params
    from app.services.ingestion import CHUNKER_VERSION
    s = get_settings()
    return {**describe_params(rag_params()),
            "system_prompt_version": grounding.PROMPT_VERSION, "rewrite_model": model_for("fast"),
            "query_rewrite": s.rag_query_rewrite, "live_check": s.live_check, "embedding_model": s.embedding_model,
            "reranker": s.reranker, "reranker_in_use": rerank.describe(), "candidates": s.rag_candidates,
            "top_k": s.rag_top_k, "min_relevance": s.rag_min_relevance, "chunker_version": CHUNKER_VERSION,
            "retrieval_version": retrieval.RETRIEVAL_VERSION,
            "vector_store": "pinecone" if s.pinecone_api_key else "in-memory",
            "llm_configured": configured()}


def judge_config() -> dict:
    """The golden-test judge. Recorded with each run, but not part of the answer configuration (a judge change
    does not change any answer, so it does not trigger a re-run)."""
    from app.rag import judge
    from app.rag.llm import judge_model
    s = get_settings()
    return {"enabled": s.eval_judge, "provider": s.eval_judge_provider, "model": judge_model(),
            "reasoning_effort": s.eval_judge_reasoning_effort or "model default",
            "prompt_version": judge.JUDGE_PROMPT_VERSION}


def config_hash(cfg: Optional[dict] = None) -> str:
    cfg = dict(cfg or config())
    cfg.pop("reranker_in_use", None)               # "not loaded yet" vs loaded is not a configuration change
    cfg.pop("judge", None)
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ traces
TRIM_EVERY = 50                                       # traces beyond RAG_TRACE_KEEP are deleted every 50 answers
_recorded = itertools.count()                         # first record after a start trims too


# (conversation_id, message_id) of the chat answer being produced: set by the chat endpoint, read when its trace is
# recorded, so a partner's feedback on that message leads to the exact trace.
message_link: contextvars.ContextVar = contextvars.ContextVar("rag_message_link", default=(None, None))


def record(data: dict, user_id: Optional[int] = None, origin: str = "chat") -> Optional[str]:
    if not get_settings().rag_debug_log and origin == "chat":
        return None
    tid = uuid.uuid4().hex[:16]
    cid, mid = message_link.get()
    try:
        with closing(_conn()) as c:
            c.execute("INSERT INTO rag_traces(id, ts, user_id, origin, mode, question, rewritten, sufficient, not_found, "
                      "flagged, model, config_hash, data, conversation_id, message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (tid, _now(), user_id, origin, data.get("mode"), data.get("question", "")[:2000],
                       (data.get("retrieval") or {}).get("plan", {}).get("standalone", "")[:2000],
                       int(bool((data.get("retrieval") or {}).get("validation", {}).get("sufficient"))),
                       int(bool(data.get("not_found"))), int(bool(data.get("unsupported_figures"))),
                       (data.get("generation") or {}).get("model"), (data.get("config") or {}).get("hash"),
                       json.dumps(data, default=str, ensure_ascii=False), cid, mid))
            if next(_recorded) % TRIM_EVERY == 0:      # trimming walks the whole index: not on every answer
                keep = get_settings().rag_trace_keep
                c.execute("DELETE FROM rag_traces WHERE id IN (SELECT id FROM rag_traces ORDER BY ts DESC LIMIT -1 OFFSET ?)", (keep,))
            c.commit()
    except Exception:
        log.exception("Could not record the RAG trace")
        return None
    return tid


def traces(limit: int = 100, q: str = "", only: str = "") -> List[dict]:
    sql = ("SELECT t.id, t.ts, t.origin, t.mode, t.question, t.rewritten, t.sufficient, t.not_found, t.flagged, t.model, "
           "t.config_hash, u.email FROM rag_traces t LEFT JOIN users u ON u.id = t.user_id WHERE 1=1")
    args: list = []
    if q:
        sql += " AND (t.question LIKE ? OR t.rewritten LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if only == "not_found":
        sql += " AND t.not_found=1"
    elif only == "flagged":
        sql += " AND t.flagged=1"
    elif only == "insufficient":
        sql += " AND t.sufficient=0"
    sql += " ORDER BY t.ts DESC LIMIT ?"
    args.append(limit)
    with closing(_conn()) as c:
        try:
            return [dict(r) for r in c.execute(sql, args)]
        except sqlite3.OperationalError:            # users table missing (scripts without accounts)
            return [dict(r) for r in c.execute(sql.replace(", u.email", "").replace(" LEFT JOIN users u ON u.id = t.user_id", ""), args)]


def trace_for_message(user_id: int, conversation_id: str, message_id: str) -> Optional[dict]:
    """The latest trace of a chat message (a regenerated answer has several)."""
    with closing(_conn()) as c:
        r = c.execute("SELECT id FROM rag_traces WHERE user_id=? AND conversation_id=? AND message_id=? "
                      "ORDER BY ts DESC LIMIT 1", (user_id, conversation_id, message_id)).fetchone()
    return trace(r["id"]) if r else None


def trace(tid: str) -> Optional[dict]:
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM rag_traces WHERE id=?", (tid,)).fetchone()
    if r is None:
        return None
    return {**{k: r[k] for k in r.keys() if k != "data"}, "data": json.loads(r["data"])}


# ------------------------------------------------------------------ matching expectations
def _norm(s: str) -> str:
    s = (s or "").lower().replace("₹", " ").replace("rs.", " ").replace("inr", " ").replace("_", " ")
    s = re.sub(r"\*\*|__|`", "", s)
    return re.sub(r"\s+", " ", re.sub(r"[^\w%.,/ -]", " ", s)).strip()


def _doc_matches(expected: str, source: str, title: str = "") -> bool:
    if not expected:
        return False
    e = _norm(expected).replace(".pdf", "").replace(".xlsx", "")
    return e in _norm(source) or e in _norm(title) or (_norm(title) and _norm(title) in e)


def answer_matches(expected: str, answer: str) -> bool:
    """The expected answer appears in the answer: as text, or as the same figures (₹50 lakh == 50,00,000)."""
    if not expected:
        return True
    if _norm(expected) in _norm(answer):
        return True
    from app.rag.grounding import _figures
    e_amt, e_pct, e_num = _figures(expected)
    if not (e_amt or e_pct or e_num):
        words = [w for w in _norm(expected).split() if len(w) > 2]
        return bool(words) and all(w in _norm(answer) for w in words)
    a_amt, a_pct, a_num = _figures(answer)
    amounts = {round(v, 2) for _, v, _, _ in a_amt}
    pcts = {round(v, 4) for _, v in a_pct}
    nums = {d for _, d in a_num}
    return all(round(v, 2) in amounts for _, v, _, _ in e_amt) and all(round(v, 4) in pcts for _, v in e_pct) \
        and all(d in nums or d in re.sub(r"\D", "", answer) for _, d in e_num)


NOT_FOUND_EXPECTED = "(not found)"     # expected answer of a question the knowledge base cannot answer


def evaluate(result: dict, expected_answer: str = "", expected_document: str = "",
             expected_page: Optional[int] = None) -> dict:
    """Compare one pipeline result with the expectation."""
    r = result.get("trace") or {}
    # What the model was given, in order: the passages of the context (banker matches and table lookups come
    # before the retrieved passages), else the retrieval's selected candidates (older traces).
    selected = r.get("context_refs") or [c for c in r.get("retrieval", {}).get("candidates", []) if c.get("selected")]
    sources = result.get("sources") or []
    out = {"retrieval": "n/a", "answer": "n/a", "citations": "n/a", "rank": None}
    if expected_document:
        hits = [i for i, c in enumerate(selected, 1) if _doc_matches(expected_document, c.get("source") or "", c.get("title") or "")]
        page_hits = [i for i in hits if expected_page is None or selected[i - 1].get("page") == expected_page]
        out["rank"] = (page_hits or hits or [None])[0]
        out["retrieval"] = "GOOD" if page_hits and page_hits[0] <= 3 else "PARTIAL" if hits else "MISSED"
        cited = [s for s in sources if not s.get("uncited")]
        ok = any(_doc_matches(expected_document, s.get("source") or "", s.get("title") or "") and
                 (expected_page is None or s.get("page") == expected_page) for s in cited)
        out["citations"] = "CORRECT" if ok else "WRONG" if cited else "NONE"
    if expected_answer.strip().lower() == NOT_FOUND_EXPECTED:
        out["answer"] = "CORRECT" if result.get("not_found") else "WRONG"
    elif expected_answer:
        if result.get("llm_skipped") and not result.get("not_found"):
            out["answer"] = "SKIPPED"
        else:
            out["answer"] = "CORRECT" if answer_matches(expected_answer, result.get("answer", "")) else "WRONG"
    out["passed"] = out["retrieval"] in ("GOOD", "PARTIAL", "n/a") and out["answer"] in ("CORRECT", "n/a", "SKIPPED") \
        and out["citations"] in ("CORRECT", "n/a")
    return out


def diagnose(data: dict, expected_answer: str = "", expected_document: str = "",
             expected_page: Optional[int] = None) -> List[dict]:
    """Walk the debugging order and report each stage: ok / problem / unknown, stopping at the first problem."""
    from app.services import knowledge_base as kb
    stages: List[dict] = []
    ret = data.get("retrieval") or {}
    cands = ret.get("candidates", [])
    selected = [c for c in cands if c.get("selected")]
    exp = _norm(expected_answer)

    def has_answer(text: str) -> bool:
        return bool(exp) and (exp in _norm(text) or answer_matches(expected_answer, text))

    def add(stage, ok, detail):
        stages.append({"stage": stage, "status": "ok" if ok else "problem" if ok is False else "unknown", "detail": detail})
        return ok is not False

    if not expected_document and not expected_answer:
        return [{"stage": "Diagnosis", "status": "unknown",
                 "detail": "Enter the expected document and/or answer to see which stage fails."}]
    files = kb.files(5000)
    doc_files = [f for f in files if _doc_matches(expected_document, f["path"])] if expected_document else []
    if expected_document:
        if not doc_files:
            add("1. Extraction", False, f"No indexed file matches '{expected_document}'. Upload it, or check the name.")
            return stages
        bad = [f for f in doc_files if f["status"] not in ("indexed", "keyword")]
        if not add("1. Extraction", not bad and all(f["chunks"] for f in doc_files),
                   ", ".join(f"{f['path']}: {f['status']}, {f['chunks']} chunks" for f in doc_files[:3])):
            return stages
    chunks = [d for f in doc_files for d in kb.document_chunks(f["path"])]
    if exp and chunks:
        holding = [d for d in chunks if has_answer(d.page_content)]
        if expected_page is not None:
            holding = [d for d in holding if d.metadata.get("page") in (None, expected_page)] or holding
        if not add("2. Chunking", bool(holding),
                   f"{len(holding)} chunk(s) of the document contain '{expected_answer}'" if holding else
                   f"No chunk of the document contains '{expected_answer}': the text was not extracted as written "
                   "(scanned page, table, or a different wording). Preview the file to check."):
            return stages
    elif expected_document:
        add("2. Chunking", None, f"{len(chunks)} chunk(s) indexed for the document (no expected answer to look for).")
    if expected_document:
        in_cands = [c for c in cands if _doc_matches(expected_document, c.get("source") or "", c.get("title") or "")]
        if not add("3. Retrieval", bool(in_cands),
                   f"{len(in_cands)} passage(s) from the document were retrieved" if in_cands else
                   "Vector and keyword search did not return the document. Try different wording, check the "
                   "document's bank / product tags, or add the missing terms to the document."):
            return stages
        kept = [c for c in in_cands if not c.get("dropped")]
        reasons = sorted({c["dropped"] for c in in_cands if c.get("dropped")})
        if not add("4. Metadata filtering", bool(kept),
                   f"{len(kept)} passage(s) kept" + (f"; dropped: {', '.join(reasons)}" if reasons else "") if kept else
                   f"All passages from the document were filtered out: {', '.join(reasons)}. Check its version "
                   "status and bank / product tags."):
            return stages
        chosen = [c for c in kept if c.get("selected")]
        best = max((c.get("rerank") or 0 for c in kept), default=0)
        if not add("5. Reranking", bool(chosen),
                   f"{len(chosen)} passage(s) from the document selected" if chosen else
                   f"The document's passages were ranked too low (best reranker score {best:.2f}, threshold "
                   f"{(ret.get('passes') or [{}])[-1].get('threshold', '?')})."):
            return stages
    if exp:
        ctx_ok = any(has_answer(c.get("text", "")) for c in selected) or has_answer((data.get("prompt") or {}).get("context", ""))
        if not add("6. Final context", ctx_ok,
                   "The final context contains the expected answer." if ctx_ok else
                   "The selected passages do not contain the expected answer (insufficient context)."):
            return stages
        if data.get("not_found") and not data.get("generation"):
            add("7. Prompt / evidence check", False, "The evidence check judged the context insufficient, so the LLM "
                "was not asked: " + (ret.get("validation") or {}).get("reason", ""))
            return stages
        ans_ok = answer_matches(expected_answer, data.get("answer", ""))
        add("7-8. Prompt and LLM generation", ans_ok,
            "The answer contains the expected value." if ans_ok else
            "The context had the answer but the model did not give it" +
            (f"; unsupported figures: {', '.join(data.get('unsupported_figures'))}" if data.get("unsupported_figures") else "") + ".")
    return stages


# ------------------------------------------------------------------ golden tests
# category: banker, code, payout, policy, ...; reference_answer: the full answer signed off by the credit team (the
# judge compares the answer with it; "(not found)" when the knowledge base has none); history: earlier user
# questions of the conversation, one per line (follow-up tests); approved_by: who signed off the reference.
GOLDEN_ADDED_COLUMNS = ("category", "reference_answer", "history", "approved_by")
GOLDEN_FIELDS = ("question", "expected_answer", "expected_document", "expected_page", "loan_type", "notes", "active",
                 *GOLDEN_ADDED_COLUMNS)


def golden_history(g: dict) -> List[dict]:
    return [{"role": "user", "text": line.strip()} for line in (g.get("history") or "").splitlines() if line.strip()]


def golden_reference(g: dict) -> Tuple[str, bool]:
    """What the judge compares the answer with: (the reviewer's answer, False), else (the must-contain value, True)."""
    ref = (g.get("reference_answer") or "").strip()
    return (ref, False) if ref else ((g.get("expected_answer") or "").strip(), True)


def golden_list(active_only: bool = False) -> List[dict]:
    with closing(_conn()) as c:
        rows = c.execute("SELECT * FROM rag_golden" + (" WHERE active=1" if active_only else "") + " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def golden_save(item: dict, gid: Optional[int] = None) -> dict:
    data = {k: item.get(k) for k in GOLDEN_FIELDS if k in item}
    if "question" in data and not (data["question"] or "").strip():
        raise ValueError("Enter a question.")
    if data.get("expected_page") in ("", None):
        data["expected_page"] = None
    with closing(_conn()) as c:
        if gid is None:
            data.setdefault("active", 1)
            cols = ", ".join(data)
            cur = c.execute(f"INSERT INTO rag_golden({cols}, created, updated) VALUES ({', '.join('?' * len(data))}, ?, ?)",
                            (*data.values(), _now(), _now()))
            gid = cur.lastrowid
        else:
            if not c.execute("SELECT 1 FROM rag_golden WHERE id=?", (gid,)).fetchone():
                raise LookupError("Test not found.")
            if data:
                c.execute(f"UPDATE rag_golden SET {', '.join(f'{k}=?' for k in data)}, updated=? WHERE id=?",
                          (*data.values(), _now(), gid))
        c.commit()
        return dict(c.execute("SELECT * FROM rag_golden WHERE id=?", (gid,)).fetchone())


def golden_delete(gid: int) -> bool:
    with closing(_conn()) as c:
        n = c.execute("DELETE FROM rag_golden WHERE id=?", (gid,)).rowcount
        c.commit()
    return bool(n)


def runs(limit: int = 20) -> List[dict]:
    with closing(_conn()) as c:
        rows = c.execute("SELECT id, started, finished, trigger, status, config_hash, total, passed, baseline "
                         "FROM rag_eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def run_detail(rid: int) -> Optional[dict]:
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM rag_eval_runs WHERE id=?", (rid,)).fetchone()
        prev = c.execute("SELECT results FROM rag_eval_runs WHERE id<? AND status='finished' ORDER BY id DESC LIMIT 1",
                         (rid,)).fetchone()
    if r is None:
        return None
    out = {**dict(r), "config": json.loads(r["config"] or "{}"), "results": json.loads(r["results"] or "[]")}
    before = {x["id"]: x for x in json.loads(prev["results"])} if prev else {}
    for x in out["results"]:
        p = before.get(x["id"])
        x["previous"] = None if p is None else p.get("passed")
        x["regression"] = bool(p and p.get("passed") and not x.get("passed"))
    out["scores"] = scores(out["results"])
    out["gate"] = gate(out)
    return out


# ------------------------------------------------------------------ release gate
# A run passes the gate when, on the golden tests it shares with the baseline run, no test that passed now fails
# and no accuracy score got worse (beyond the tolerance below, for the judge's run-to-run noise).
GATE_TOLERANCE = {"faithfulness": 1.0, "correct": 0.0, "correct_refusals": 0.0}    # points a score may drop
GATE_NO_INCREASE = ("unsupported_claims", "false_refusals")                         # counts that must not rise
GATE_LABELS = {"faithfulness": "faithfulness", "correct": "correct vs reference", "correct_refusals": "correct refusals",
               "unsupported_claims": "unsupported claims", "false_refusals": "false refusals"}


def baseline_id() -> Optional[int]:
    with closing(_conn()) as c:
        r = c.execute("SELECT id FROM rag_eval_runs WHERE baseline=1 ORDER BY id DESC LIMIT 1").fetchone()
    return r["id"] if r else None


def set_baseline(rid: int) -> dict:
    """Approve a finished run as the baseline every later run is compared with."""
    with closing(_conn()) as c:
        r = c.execute("SELECT status FROM rag_eval_runs WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise LookupError("Run not found.")
        if r["status"] != "finished":
            raise ValueError("Only a finished run can be the baseline (resume a stopped run first).")
        c.execute("UPDATE rag_eval_runs SET baseline=(id=?)", (rid,))
        c.commit()
    return {"baseline": rid}


def gate(run: dict, base: Optional[dict] = None) -> dict:
    """Compare a run (run_detail without the gate) with the baseline run. verdict: PASS, FAIL, INCOMPLETE (the run
    did not finish), NO_BASELINE, or BASELINE (it is the baseline)."""
    if base is None:
        bid = baseline_id()
        if bid is None:
            return {"verdict": "NO_BASELINE", "reasons": ["No baseline run is set: approve a good run as the baseline."]}
        if bid == run["id"]:
            return {"verdict": "BASELINE", "reasons": [], "baseline_id": bid}
        with closing(_conn()) as c:
            b = c.execute("SELECT id, results FROM rag_eval_runs WHERE id=?", (bid,)).fetchone()
        base = {"id": b["id"], "results": json.loads(b["results"] or "[]")}
    out = {"baseline_id": base["id"], "reasons": [], "regressions": [], "fixed": [], "new_cases": 0, "deltas": {}}
    if run.get("status") != "finished":
        out.update(verdict="INCOMPLETE", reasons=[f"The run did not finish ({run.get('status')}): resume it, then check again."])
        return out
    old = {x["id"]: x for x in base["results"] if not x.get("error")}
    now = {x["id"]: x for x in run["results"] if not x.get("error")}
    shared = [i for i in now if i in old]
    out["compared"], out["new_cases"] = len(shared), len([i for i in now if i not in old])
    for i in shared:
        case = {"id": i, "question": now[i]["question"], "category": now[i].get("category") or ""}
        if old[i].get("passed") and not now[i].get("passed"):
            out["regressions"].append(case)
        elif now[i].get("passed") and not old[i].get("passed"):
            out["fixed"].append(case)
    errors = [x["question"] for x in run["results"] if x.get("error")]
    if errors:
        out["reasons"].append(f"{len(errors)} golden test(s) could not run: {', '.join(q[:60] for q in errors[:3])}")
    if out["regressions"]:
        out["reasons"].append(f"{len(out['regressions'])} golden test(s) that passed in the baseline now fail.")
    b, n = _score([old[i] for i in shared]), _score([now[i] for i in shared])
    for k, tol in GATE_TOLERANCE.items():
        out["deltas"][k] = [b.get(k), n.get(k)]
        if b.get(k) is not None and n.get(k) is not None and n[k] < b[k] - tol:
            out["reasons"].append(f"{GATE_LABELS[k].capitalize()} fell from {b[k]}% to {n[k]}%.")
    for k in GATE_NO_INCREASE:
        out["deltas"][k] = [b.get(k), n.get(k)]
        if (n.get(k) or 0) > (b.get(k) or 0):
            out["reasons"].append(f"{GATE_LABELS[k].capitalize()} rose from {b.get(k) or 0} to {n.get(k)}.")
    out["verdict"] = "FAIL" if out["reasons"] else "PASS"
    return out


def gate_current() -> dict:
    """The gate for the configuration in use now: its latest finished run, compared with the baseline. A
    configuration without a finished run is 'UNTESTED' (for deploy scripts: python -m evals.gate)."""
    h = config_hash()
    with closing(_conn()) as c:
        r = c.execute("SELECT id FROM rag_eval_runs WHERE config_hash=? AND status='finished' ORDER BY id DESC LIMIT 1",
                      (h,)).fetchone()
    if r is None:
        return {"verdict": "UNTESTED", "config_hash": h,
                "reasons": ["The current configuration has no finished golden-test run: run the golden tests first."]}
    run = run_detail(r["id"])
    return {**run["gate"], "run_id": run["id"], "config_hash": h, "scores": run["scores"]["overall"]}


def _score(results: List[dict]) -> dict:
    ran = [r for r in results if not r.get("error")]
    judged = [r["judge"] for r in ran if (r.get("judge") or {}).get("verdict") in ("PASS", "WARN", "FAIL")]
    faith = [j["faithfulness"] for j in judged if j.get("faithfulness") is not None]
    graded = [j for j in judged if j.get("correctness") in ("correct", "partial", "incorrect")]
    expect_nothing = [r for r in ran if (r.get("expected_answer") or "").strip().lower() == NOT_FOUND_EXPECTED
                      or (r.get("reference_answer") or "").strip().lower() == NOT_FOUND_EXPECTED]
    expect_answer = [r for r in ran if r not in expect_nothing and (r.get("expected_answer") or r.get("reference_answer"))]
    pct = lambda n, d: round(100 * n / d, 1) if d else None   # noqa: E731
    return {
        "cases": len(results), "ran": len(ran), "errors": len(results) - len(ran),
        "pass_rate": pct(sum(1 for r in ran if r.get("passed")), len(ran)),
        "judged": len(judged),
        # share of factual claims supported by the sources (partly supported = half)
        "faithfulness": round(100 * sum(faith) / len(faith), 1) if faith else None,
        "unsupported_claims": sum(j.get("unsupported", 0) for j in judged),
        "citation_errors": sum(j.get("citation_errors", 0) for j in judged),
        "correct": pct(sum(1 for j in graded if j["correctness"] == "correct"), len(graded)),
        "incorrect": pct(sum(1 for j in graded if j["correctness"] == "incorrect"), len(graded)),
        # refused although the knowledge base has the answer / answered although it has none
        "false_refusals": sum(1 for r in expect_answer if r.get("not_found")),
        "correct_refusals": pct(sum(1 for r in expect_nothing if r.get("not_found")), len(expect_nothing)),
        "retrieval_good": pct(sum(1 for r in ran if r.get("retrieval") == "GOOD"),
                              sum(1 for r in ran if r.get("retrieval") not in (None, "n/a"))),
    }


def scores(results: List[dict]) -> dict:
    """Run scores, overall and per category (the release gate compares these between runs)."""
    cats = sorted({r.get("category") or "" for r in results})
    return {"overall": _score(results),
            "by_category": {c or "(none)": _score([r for r in results if (r.get("category") or "") == c]) for c in cats}}


MAX_WAIT_S = 120          # a per-minute limit asking for a longer wait (or a daily limit) stops the run
STALE_RUN = timedelta(minutes=30)   # longer than one case can take (answer + judge + rate-limit waits)


def _beat(rid: int):
    with closing(_conn()) as c:
        c.execute("UPDATE rag_eval_runs SET heartbeat=? WHERE id=?", (_now(), rid))
        c.commit()
MAX_WAITS = 5             # waits per provider call before the run stops


class _Stop(Exception):
    """The provider's limit will not reset soon: stop the run (it can be resumed later)."""


def _with_waits(call, progress: str):
    """Run one provider call; on a short rate limit, wait as long as the provider asks and try again."""
    import time
    from app.rag.llm import is_daily_limit, is_out_of_credit, is_rate_limit, rate_limit_wait_s
    for attempt in range(MAX_WAITS + 1):
        try:
            return call()
        except Exception as e:
            if not is_rate_limit(e):
                raise
            if is_out_of_credit(e):
                raise _Stop("AI provider account has no credit left (add credit, then resume)") from e
            wait = rate_limit_wait_s(e)
            if is_daily_limit(e) or wait is None or wait > MAX_WAIT_S or attempt == MAX_WAITS:
                raise _Stop("AI provider " + ("daily" if is_daily_limit(e) else "rate") + " limit") from e
            state["progress"] = f"{progress} (waiting {wait:.0f}s: provider per-minute limit)"
            log.info("Golden tests: rate limited, waiting %.1fs", wait)
            time.sleep(wait + 1)


def _run_case(g: dict, jcfg: dict, progress: str) -> dict:
    from app.rag import chat, judge
    res = _with_waits(lambda: chat.run("kb", g["question"], loan_type=g.get("loan_type") or None,
                                       history=golden_history(g), origin="eval"), progress)
    ev = evaluate(res, g.get("expected_answer") or "", g.get("expected_document") or "", g.get("expected_page"))
    if jcfg["enabled"] and not res.get("llm_skipped"):
        state["progress"] = f"{progress} (judging)"
        ref, key_value = golden_reference(g)
        verdict = _with_waits(lambda: judge.judge(g["question"], res.get("answer", ""), res.get("trace") or {},
                                                  ref, bool(res.get("not_found")), key_value), progress)
        if verdict:
            ev["judge"] = verdict
            ev["passed"] = ev["passed"] and verdict["verdict"] != "FAIL"
    return {"id": g["id"], "question": g["question"], "category": g.get("category") or "",
            **ev, "answer_text": res.get("answer", "")[:1500], "not_found": bool(res.get("not_found")),
            "expected_answer": g.get("expected_answer") or "",
            "reference_answer": (g.get("reference_answer") or "")[:1500],
            "expected_document": g.get("expected_document") or "",
            "trace_id": res.get("trace_id"),
            "sources": [f"{s.get('title')}" + (f" — Page {s['page']}" if s.get("page") else "")
                        for s in res.get("sources", []) if not s.get("uncited")][:6]}


def resumable(rid: int) -> List[dict]:
    """The results of run rid to keep when it is resumed (cases that ran); raises ValueError if it can't be."""
    with closing(_conn()) as c:
        r = c.execute("SELECT status, config_hash, results FROM rag_eval_runs WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise LookupError("Run not found.")
    if not (r["status"] or "").startswith("stopped"):
        raise ValueError("Only a run stopped by the AI provider's limit can be resumed.")
    if r["config_hash"] != config_hash():
        raise ValueError("The configuration changed since this run, so its results can't be combined: start a new run.")
    return [x for x in json.loads(r["results"] or "[]") if not x.get("error")]


def run_golden(trigger: str = "manual", resume: Optional[int] = None) -> Optional[int]:
    """Run every active golden test through the full pipeline. Returns the run id (None if nothing to run or a
    run is already going: then one more run is queued to follow it).

    A short per-minute rate limit is waited out; a daily limit stops the run ("stopped at n/N"). resume=<run id>
    continues a stopped run: its finished cases are kept and only the rest run (into a new run)."""
    cases = golden_list(active_only=True)
    if not cases:
        return None
    kept = resumable(resume) if resume else []
    if not _run_lock.acquire(blocking=False):
        if not resume:
            _pending["trigger"] = trigger
        return None
    cfg = config()
    jcfg = judge_config()
    try:
        done = {x["id"] for x in kept}
        kept = [x for x in kept if x["id"] in {g["id"] for g in cases}]   # tests deleted or switched off meanwhile
        todo = [g for g in cases if g["id"] not in done]
        state.update(running=True, trigger=trigger, progress=f"{len(kept)}/{len(cases)}")
        with closing(_conn()) as c:
            rid = c.execute("INSERT INTO rag_eval_runs(started, trigger, status, config_hash, config, total, passed) "
                            "VALUES (?,?,?,?,?,?,0)", (_now(), trigger, "running", config_hash(cfg),
                                                       json.dumps({**cfg, "judge": jcfg}), len(cases))).lastrowid
            c.commit()
        results, stopped = list(kept), ""
        for g in todo:
            progress = f"{len(results) + 1}/{len(cases)}"
            state["progress"] = progress
            _beat(rid)
            try:
                results.append(_run_case(g, jcfg, progress))
            except _Stop as e:
                # The provider's limit is shared with the partners' chats: stop instead of using up what is left of
                # it (the remaining cases would only fail the same way). The run can be resumed later.
                log.warning("Golden tests stopped at case %s: %s", progress, e)
                stopped = f"stopped at {progress}: {e}"
                break
            except Exception as e:
                results.append({"id": g["id"], "question": g["question"], "category": g.get("category") or "",
                                "passed": False, "error": f"{type(e).__name__}: {e}"[:300]})
                log.exception("Golden test %s failed to run", g["id"])
        order = {g["id"]: i for i, g in enumerate(cases)}
        results.sort(key=lambda x: order.get(x["id"], len(order)))
        passed = sum(1 for r in results if r.get("passed"))
        with closing(_conn()) as c:
            c.execute("UPDATE rag_eval_runs SET finished=?, status=?, passed=?, results=? WHERE id=?",
                      (_now(), stopped or "finished", passed, json.dumps(results, ensure_ascii=False), rid))
            c.commit()
        log.info("Golden tests (%s): %d/%d passed", trigger, passed, len(results))
        return rid
    finally:
        state.update(running=False, trigger=None, progress="")
        _run_lock.release()
        nxt, _pending["trigger"] = _pending["trigger"], None
        if nxt:
            run_golden_async(nxt)


def rejudge(rid: int) -> Optional[int]:
    """Judge a finished run's answers again with the current judge (after a judge change), without generating
    new answers. Writes a new run; answers whose trace was trimmed keep their old verdict."""
    from app.rag import judge
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM rag_eval_runs WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise LookupError("Run not found.")
    if not _run_lock.acquire(blocking=False):
        return None
    jcfg = judge_config()
    try:
        tests = {g["id"]: g for g in golden_list()}
        results = json.loads(r["results"] or "[]")
        state.update(running=True, trigger=f"re-judge run {rid}", progress=f"0/{len(results)}")
        for n, x in enumerate(results, 1):
            state["progress"] = progress = f"{n}/{len(results)}"
            t = trace(x["trace_id"]) if x.get("trace_id") else None
            if x.get("error") or t is None:
                continue
            g = {**{"question": x["question"], "expected_answer": x.get("expected_answer", "")}, **tests.get(x["id"], {})}
            ref, key_value = golden_reference(g)
            verdict = _with_waits(lambda: judge.judge(x["question"], t["data"].get("answer", ""), t["data"], ref,
                                                      bool(x.get("not_found")), key_value), progress)
            x.pop("judge", None)
            x["passed"] = x["retrieval"] in ("GOOD", "PARTIAL", "n/a") and x["answer"] in ("CORRECT", "n/a", "SKIPPED") \
                and x["citations"] in ("CORRECT", "n/a")
            if verdict:
                x["judge"] = verdict
                x["passed"] = x["passed"] and verdict["verdict"] != "FAIL"
        cfg = {**json.loads(r["config"] or "{}"), "judge": jcfg}
        with closing(_conn()) as c:
            new = c.execute("INSERT INTO rag_eval_runs(started, finished, trigger, status, config_hash, config, total, passed, "
                            "results) VALUES (?,?,?,?,?,?,?,?,?)",
                            (r["started"], _now(), f"re-judged run {rid} ({jcfg['prompt_version']})", r["status"],
                             r["config_hash"], json.dumps(cfg), r["total"], sum(1 for x in results if x.get("passed")),
                             json.dumps(results, ensure_ascii=False))).lastrowid
            c.commit()
        return new
    except _Stop as e:
        log.warning("Re-judging run %s stopped: %s", rid, e)
        return None
    finally:
        state.update(running=False, trigger=None, progress="")
        _run_lock.release()


def run_golden_async(trigger: str = "manual", resume: Optional[int] = None) -> bool:
    if not golden_list(active_only=True):
        return False
    if resume:
        resumable(resume)                     # raise now (LookupError / ValueError), not in the thread
    threading.Thread(target=run_golden, args=(trigger, resume), daemon=True, name="rag-golden").start()
    return True


def last_config_hash() -> Optional[str]:
    with closing(_conn()) as c:
        r = c.execute("SELECT config_hash FROM rag_eval_runs WHERE status='finished' ORDER BY id DESC LIMIT 1").fetchone()
    return r["config_hash"] if r else None


def autorun_if_config_changed():
    """At startup: if the configuration differs from the last run's, re-run the golden tests."""
    if not get_settings().rag_eval_autorun:
        return
    last = last_config_hash()
    if last is not None and last != config_hash():
        run_golden_async("configuration changed")


def autorun_after_documents_changed(report: dict):
    """After a knowledge-base job that added, changed or removed documents."""
    if get_settings().rag_eval_autorun and report and (report.get("indexed") or report.get("removed") or report.get("keyword_only")):
        run_golden_async("documents updated")
