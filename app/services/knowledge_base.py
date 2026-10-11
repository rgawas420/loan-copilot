"""Urban Money knowledge base: keeps the vector store and a keyword index in sync with the document folder.

Every supported file under the Urban Money folder (any depth, any name) is part of the primary KB. A sync:
  1. discovers files and hashes them (size + mtime unchanged -> previous hash is reused),
  2. extracts text once per content hash (OCR included) and caches it in kb_cache_dir,
  3. indexes new files, re-indexes changed ones and removes deleted ones, in the vector store
     (Pinecone: incremental, tracked in a manifest) and in a SQLite FTS5 keyword index used for exact
     lookups (pincodes, DSA codes, phone numbers, company names) that embeddings miss.
     Files with more than kb_vector_max_chunks chunks (e.g. a 180k-row employer master) go to the keyword
     index only (status 'keyword'): they are exact lookups, and as vectors they would crowd out everything else.
Chunk ids are deterministic (path + content hash + position), so an interrupted sync is safe to re-run.
"""
import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from langchain_core.documents import Document

from app.core.config import BASE_DIR, get_settings
from app.rag import lookup
from app.services import kb_versions
from app.services import db as dbtune
from app.services.ingestion import (ALLOWED_EXTENSIONS, CHUNKER_VERSION, Part, extract, extractor_version,
                                       load_and_chunk)

log = logging.getLogger(__name__)
CORPUS = "urban_money"
BATCH = 256
_sync_lock = threading.Lock()
status: Dict = {"running": False, "started": None, "finished": None, "progress": "", "last_report": None}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ folder discovery
def kb_root() -> Optional[Path]:
    """The configured folder, or the folder named "Urban Money" (any case / separator) inside data/."""
    configured = get_settings().urban_money_dir
    if configured and str(configured) not in ("", "."):
        return configured if configured.is_dir() else None
    data = BASE_DIR / "data"
    if data.is_dir():
        for d in sorted(data.iterdir()):
            if d.is_dir() and re.sub(r"[\s_\-]+", "", d.name.lower()) == "urbanmoney":
                return d
    return None


def discover(root: Path) -> List[Path]:
    """All files under root, skipping hidden files, Office lock files (~$x.xlsx) and folders starting with '.'."""
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
                  and not p.name.startswith("~$"))


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# ------------------------------------------------------------------ local index (manifest + keyword search)
def _db():
    s = get_settings()
    s.kb_index_db.parent.mkdir(parents=True, exist_ok=True)
    c = dbtune.tune(sqlite3.connect(s.kb_index_db, timeout=dbtune.BUSY_TIMEOUT_S))
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS kb_files(store TEXT, path TEXT, sha TEXT, size INTEGER, mtime REAL, chunks INTEGER,
        status TEXT, error TEXT, ts TEXT, PRIMARY KEY(store, path));
    CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(id UNINDEXED, path UNINDEXED, meta UNINDEXED, text,
        tokenize='porter unicode61');
    CREATE TABLE IF NOT EXISTS kb_meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS kb_docs(path TEXT PRIMARY KEY, doc_id TEXT UNIQUE, title TEXT, bank TEXT, product TEXT,
        category TEXT, meta_source TEXT, created TEXT, updated TEXT);
    CREATE TABLE IF NOT EXISTS kb_versions(doc_id TEXT, version INTEGER, sha TEXT, status TEXT, size INTEGER,
        stored TEXT, note TEXT, created TEXT, by TEXT, PRIMARY KEY(doc_id, version));
    """ + lookup.SCHEMA + banker_directory_schema() + aliases_schema() + policy_facts_schema() + kb_staging_schema() + kb_verify_schema() + ocr_review_schema() + kb_audit_schema())
    _migrate(c, s.kb_index_db)
    return c


_migrated: set = set()
VERSION_COLUMNS = {"file": "TEXT", "label": "TEXT", "effective_from": "TEXT", "effective_until": "TEXT",
                   "extraction": "TEXT"}
DOC_COLUMNS = {"source_type": "TEXT", "priority": "INTEGER",           # NULL: the default for the document type
               "doc_type": "TEXT"}                                     # NULL until inferred (see kb_versions)


def _migrate(c, path):
    """Columns added after the first release, and the one-ACTIVE-version-per-document rule (once per database)."""
    if str(path) in _migrated:
        return
    have = {r["name"] for r in c.execute("PRAGMA table_info(kb_versions)")}
    for col, typ in VERSION_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE kb_versions ADD COLUMN {col} {typ}")
    have = {r["name"] for r in c.execute("PRAGMA table_info(kb_docs)")}
    for col, typ in DOC_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE kb_docs ADD COLUMN {col} {typ}")
    from app.services.kb_verify import COLUMNS as VERIFIED_COLUMNS
    have = {r["name"] for r in c.execute("PRAGMA table_info(kb_verified)")}
    for col, typ in VERIFIED_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE kb_verified ADD COLUMN {col} {typ}")
    try:            # the database itself refuses a second ACTIVE version of a document
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS kb_versions_one_active ON kb_versions(doc_id) WHERE status='ACTIVE'")
    except sqlite3.IntegrityError:
        log.error("KB: a document has more than one ACTIVE version; the one-active rule could not be enforced")
    c.commit()
    _migrated.add(str(path))
    kb_versions.backfill_doc_types(c)
    kb_versions.backfill_periods(c)


def policy_facts_schema() -> str:
    from app.services.policy_facts import SCHEMA
    return SCHEMA


def aliases_schema() -> str:
    from app.services.aliases import SCHEMA
    return SCHEMA


def banker_directory_schema() -> str:
    from app.services.banker_directory import SCHEMA
    return SCHEMA


def kb_staging_schema() -> str:
    from app.services.kb_staging import SCHEMA
    return SCHEMA


def kb_audit_schema() -> str:
    from app.services.kb_audit import SCHEMA
    return SCHEMA


def kb_verify_schema() -> str:
    from app.services.kb_verify import SCHEMA
    return SCHEMA


def ocr_review_schema() -> str:
    from app.services.ocr_review import SCHEMA
    return SCHEMA


def _store_key() -> str:
    from app.rag.vectorstore import is_pinecone
    s = get_settings()
    return f"pinecone:{s.pinecone_index_name}/{s.pinecone_namespace}" if is_pinecone() else "memory"


def chunk_ids(path: str, sha: str, n: int) -> List[str]:
    key = hashlib.sha1(path.encode()).hexdigest()[:10]
    return [f"um-{key}-{sha[:12]}-{i}" for i in range(n)]


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------ extraction cache
def machine_extract(path: Path, sha: str) -> List[Part]:
    """The machine extraction of a file (text layer, tables, OCR), from the cache or extracted now and cached."""
    cache = get_settings().kb_cache_dir / f"{sha}.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("v") == extractor_version(path.suffix):
                return [Part.from_dict(p) for p in data["parts"]]
        except (ValueError, TypeError, KeyError):
            pass
    parts = extract(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"v": extractor_version(path.suffix), "file": path.name,
                                 "parts": [p.to_dict() for p in parts]}, ensure_ascii=False), encoding="utf-8")
    return parts


def _extract_cached(path: Path, sha: str) -> List[Part]:
    """The text of a file: its machine extraction with an admin's verified text for this content applied
    (kb_verify: all of it, or only its OCR'd pages)."""
    from app.services import kb_verify
    v = kb_verify.get(sha)
    if not kb_verify.needs_machine(v):
        return kb_verify.apply(sha, None, v)
    return kb_verify.apply(sha, machine_extract(path, sha), v)


def _kind(parts: List[Part], suffix: str) -> str:
    from app.services.kb_verify import kind
    return kind(parts, suffix)


def _sample(parts: List[Part]) -> str:
    return "\n".join(p.text or "\n".join([p.context, *p.rows[:20]]) for p in parts[:3])[:3000]


def chunk_file(path: Path, root: Path, sha: str, meta: Optional[dict] = None,
               parts: Optional[List[Part]] = None) -> List[Document]:
    """Chunks of one file, carrying its document / version metadata (bank, product, version, status...)."""
    rel = _rel(path, root)
    parts = _extract_cached(path, sha) if parts is None else parts
    if meta is None:
        meta = kb_versions.chunk_meta(rel, sha) or {**kb_versions.infer(rel, _sample(parts)), "status": kb_versions.ACTIVE}
    return load_and_chunk(path, source_name=rel, parts=parts,
                          extra_meta={"corpus": CORPUS, "file_type": path.suffix.lower().lstrip("."), **meta})


def _pipeline_key(store: str) -> str:
    return f"pipeline:{store}"


def _pipeline_version() -> str:
    """Chunk text and the embedding model: vectors made under another value must be re-made."""
    return f"{CHUNKER_VERSION}|{get_settings().embedding_model}"


def _pipeline_current(db, store: str) -> bool:
    r = db.execute("SELECT value FROM kb_meta WHERE key=?", (_pipeline_key(store),)).fetchone()
    return bool(r) and r["value"] == _pipeline_version()


def _rows_current(db) -> bool:
    r = db.execute("SELECT value FROM kb_meta WHERE key='rows'").fetchone()
    return bool(r) and r["value"] == lookup.ROW_INDEX_VERSION


# ------------------------------------------------------------------ sync
def sync(vs=None, full: bool = False, on_progress: Optional[Callable[[str], None]] = None,
         only: Optional[set] = None, by: str = "") -> dict:
    """Bring the vector store and keyword index in line with the folder.

    vs: the vector store to write to (default: the app's store). An in-memory store is empty after a restart,
        so it always gets every file (from the extraction cache); Pinecone gets only what changed.
    full: re-index every file even if unchanged (e.g. after the Pinecone namespace was cleared).
    only: limit the sync to these folder-relative paths (admin upload, reprocess, archive or delete of single
        files). A listed path that is no longer in the folder is removed from the index; others are untouched.
    by: who asked (recorded in the audit log; "system" for startup and folder syncs). An in-memory store that is
        rebuilt at every start records nothing then: that is not a change to the knowledge base.
    """
    from app.services import kb_audit
    from app.rag import vectorstore
    report = {"root": None, "indexed": [], "keyword_only": [], "unchanged": 0, "removed": [], "skipped": [],
              "failed": [], "duplicates": [], "chunks": 0}
    root = kb_root()
    if root is None:
        log.warning("Urban Money folder not found (set URBAN_MONEY_DIR); primary KB is empty")
        return report
    report["root"] = str(root)
    if not _sync_lock.acquire(blocking=False):
        raise RuntimeError("A knowledge-base sync is already running.")
    status.update(running=True, started=_now(), finished=None, progress="discovering files")
    try:
        vs = vectorstore.get_vectorstore() if vs is None else vs
        persistent = vectorstore.is_pinecone()
        audit = persistent or only is not None
        store = _store_key()
        max_bytes = get_settings().kb_max_file_mb * 1024 * 1024
        max_vector_chunks = get_settings().kb_vector_max_chunks
        with closing(_db()) as db:
            if not persistent and only is None:
                db.execute("DELETE FROM kb_files WHERE store=?", (store,))     # in-memory store starts empty
            if persistent and not full and only is None and not _pipeline_current(db, store):
                full = True                     # chunk text / embedding model changed: re-index every file once
                report["pipeline_upgrade"] = _pipeline_version()
                log.info("KB: pipeline is now %s; re-indexing every file", _pipeline_version())
            rebuild_rows = not _rows_current(db)            # row index added or changed: fill it from the cache
            known = {r["path"]: dict(r) for r in db.execute("SELECT * FROM kb_files WHERE store=?", (store,))}
            files = discover(root)
            if only is not None:
                files = [p for p in files if _rel(p, root) in only]
            # content hash -> the one path searched for it. A file identical to one already indexed (the same list
            # saved in two folders) is not indexed again: its copies would be counted and quoted twice.
            sha_owner: Dict[str, str] = {}
            if only is not None:
                present = {_rel(p, root) for p in discover(root)}
                sha_owner = {r["sha"]: rel for rel, r in sorted(known.items())
                             if rel not in only and rel in present and r["status"] in ("indexed", "keyword")}
            seen = set()
            for n, path in enumerate(files, 1):
                rel = _rel(path, root)
                msg = f"{n}/{len(files)} {rel}"
                status["progress"] = msg
                if on_progress:
                    on_progress(msg)
                ext, st = path.suffix.lower(), path.stat()
                if ext not in ALLOWED_EXTENSIONS:
                    report["skipped"].append(f"{rel} (unsupported type)")
                    continue
                if st.st_size > max_bytes:
                    report["skipped"].append(f"{rel} (larger than {get_settings().kb_max_file_mb} MB)")
                    continue
                seen.add(rel)                                   # skipped files count as removed below
                old = known.get(rel)
                same_stat = old and old["size"] == st.st_size and old["mtime"] == st.st_mtime
                sha = old["sha"] if same_stat else _hash_file(path)
                owner = sha_owner.get(sha)
                if owner and owner != rel:
                    _mark_duplicate(db, store, rel, sha, st, old, owner, persistent or only is not None, report,
                                    audit, by)
                    continue
                if old and old["sha"] == sha and old["status"] in ("indexed", "keyword", "failed") and not full:
                    if old["status"] == "failed":
                        report["failed"].append(f"{rel}: {old['error']}")
                    else:
                        report["unchanged"] += 1
                        sha_owner.setdefault(sha, rel)
                        if rebuild_rows:
                            _backfill_rows(db, path, rel, sha)
                    if not same_stat:
                        db.execute("UPDATE kb_files SET size=?, mtime=? WHERE store=? AND path=?",
                                   (st.st_size, st.st_mtime, store, rel))
                    continue
                if old and (persistent or only is not None) and old["status"] != "keyword":   # changed: drop old vectors
                    vectorstore.delete_documents(chunk_ids(rel, old["sha"], old["chunks"]))
                try:
                    parts = _extract_cached(path, sha)
                    if not parts:
                        raise ValueError("no readable text")
                    meta = kb_versions.register(rel, sha, st.st_size, _sample(parts), db=db)
                    meta["extraction"] = _kind(parts, path.suffix)          # text / ocr / verified
                    kb_versions.set_extraction(meta["document_id"], sha, meta["extraction"], db)
                    docs = chunk_file(path, root, sha, meta, parts)
                    if not docs:
                        raise ValueError("no readable text")
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"[:300]
                    log.warning("KB: could not read %s: %s", rel, err)
                    report["failed"].append(f"{rel}: {err}")
                    db.execute("INSERT OR REPLACE INTO kb_files VALUES (?,?,?,?,?,?,?,?,?)",
                               (store, rel, sha, st.st_size, st.st_mtime, 0, "failed", err, _now()))
                    db.execute("DELETE FROM kb_fts WHERE path=?", (rel,))
                    lookup.remove_rows(db, rel)
                    db.commit()
                    continue
                ids = chunk_ids(rel, sha, len(docs))
                if len(docs) > max_vector_chunks:                             # huge list: keyword index only
                    _index_keywords(db, rel, ids, docs)
                    lookup.index_rows(db, rel, parts, meta)
                    db.execute("INSERT OR REPLACE INTO kb_files VALUES (?,?,?,?,?,?,?,?,?)",
                               (store, rel, sha, st.st_size, st.st_mtime, len(docs), "keyword", None, _now()))
                    db.commit()
                    report["keyword_only"].append(rel)
                    sha_owner.setdefault(sha, rel)
                    if audit:
                        kb_audit.record("process", by, rel, meta["document_id"], meta["version"],
                                        f"{len(docs)} sections (keyword index only)", db=db)
                        db.commit()
                    log.info("KB: %s has %d chunks (> %d): keyword index only", rel, len(docs), max_vector_chunks)
                    continue
                # record first, so an interrupted upload can still be cleaned up by the next sync
                db.execute("INSERT OR REPLACE INTO kb_files VALUES (?,?,?,?,?,?,?,?,?)",
                           (store, rel, sha, st.st_size, st.st_mtime, len(docs), "indexing", None, _now()))
                db.commit()
                try:
                    for i in range(0, len(docs), BATCH):
                        status["progress"] = f"{msg} (chunks {i}/{len(docs)})"
                        vs.add_documents(docs[i:i + BATCH], ids=ids[i:i + BATCH])
                except Exception as e:                          # left as 'indexing': retried next sync
                    log.exception("KB: vector upload failed for %s", rel)
                    report["failed"].append(f"{rel}: upload failed ({type(e).__name__}); will retry")
                    continue
                _index_keywords(db, rel, ids, docs)
                lookup.index_rows(db, rel, parts, meta)
                db.execute("UPDATE kb_files SET status='indexed' WHERE store=? AND path=?", (store, rel))
                db.commit()
                report["indexed"].append(rel)
                sha_owner.setdefault(sha, rel)
                report["chunks"] += len(docs)
                if audit:
                    kb_audit.record("process", by, rel, meta["document_id"], meta["version"],
                                    f"{len(docs)} sections" + (f", text: {meta['extraction']}" if meta.get("extraction") != "text" else ""),
                                    db=db)
                    db.commit()
                log.info("KB: indexed %s (%d chunks)", rel, len(docs))
            for rel, old in known.items():                                    # deleted from the folder
                if rel in seen or (only is not None and rel not in only):
                    continue
                if (persistent or only is not None) and old["chunks"] and old["status"] != "keyword":
                    vectorstore.delete_documents(chunk_ids(rel, old["sha"], old["chunks"]))
                db.execute("DELETE FROM kb_files WHERE store=? AND path=?", (store, rel))
                db.execute("DELETE FROM kb_fts WHERE path=?", (rel,))
                lookup.remove_rows(db, rel)
                db.commit()
                why = "archived" if (root / ".archive" / rel).exists() else "removed from the folder"
                kb_versions.disable(rel, why)
                report["removed"].append(rel)
                if audit:
                    kb_audit.record("remove", by, rel, detail=why)
                log.info("KB: removed %s", rel)
            stale = [r[0] for r in db.execute("SELECT DISTINCT path FROM kb_fts")
                     if r[0] not in seen and (only is None or r[0] in only)]
            db.executemany("DELETE FROM kb_fts WHERE path=?", [(p,) for p in stale])
            for p in lookup.indexed_paths(db):
                if p not in seen and (only is None or p in only):
                    lookup.remove_rows(db, p)
            if only is None and not any("upload failed" in f for f in report["failed"]):
                db.execute("INSERT OR REPLACE INTO kb_meta(key, value) VALUES (?,?)", (_pipeline_key(store), _pipeline_version()))
            if only is None:
                db.execute("INSERT OR REPLACE INTO kb_meta(key, value) VALUES ('rows', ?)", (lookup.ROW_INDEX_VERSION,))
            db.commit()
        log.info("KB sync: %d indexed (%d chunks), %d keyword-only, %d unchanged, %d removed, %d skipped, %d failed, "
                 "%d duplicates", len(report["indexed"]), report["chunks"], len(report["keyword_only"]),
                 report["unchanged"], len(report["removed"]), len(report["skipped"]), len(report["failed"]),
                 len(report["duplicates"]))
        from app.services import banker_directory
        status["progress"] = "banker directory"
        report["banker_directory"] = banker_directory.rebuild_quietly()   # no-op unless the RM lists changed
        return report
    finally:
        status.update(running=False, finished=_now(), progress="", last_report=_summary(report))
        _sync_lock.release()


def _mark_duplicate(db, store: str, rel: str, sha: str, st, old: Optional[dict], owner: str, drop_vectors: bool,
                    report: dict, audit: bool, by: str):
    """rel has the same content as owner: remove whatever was indexed for it and record it as a duplicate. Its
    document version is disabled; if owner later leaves the folder, the next sync indexes rel again (and register()
    re-activates it)."""
    from app.services import kb_audit
    from app.rag import vectorstore
    if old and drop_vectors and old["chunks"] and old["status"] in ("indexed", "indexing"):
        vectorstore.delete_documents(chunk_ids(rel, old["sha"], old["chunks"]))
    db.execute("DELETE FROM kb_fts WHERE path=?", (rel,))
    lookup.remove_rows(db, rel)
    db.execute("INSERT OR REPLACE INTO kb_files VALUES (?,?,?,?,?,?,?,?,?)",
               (store, rel, sha, st.st_size, st.st_mtime, 0, "duplicate", f"Same content as {owner}", _now()))
    db.commit()
    report["duplicates"].append(f"{rel} = {owner}")
    if old and old["status"] == "duplicate" and old["sha"] == sha:
        return                                              # already recorded
    kb_versions.disable(rel, f"duplicate of {owner}")
    if audit:
        kb_audit.record("duplicate", by, rel, detail=f"same content as {owner}; not indexed")
    log.info("KB: %s is identical to %s; not indexed", rel, owner)


def _summary(report: dict) -> dict:
    return {**{k: len(v) if isinstance(v, list) else v for k, v in report.items()},
            "skipped_files": report["skipped"][:50], "failed_files": report["failed"][:50]}


def _backfill_rows(db, path: Path, rel: str, sha: str):
    """Row index for a file that is already indexed (from the extraction cache: nothing is re-read or re-embedded)."""
    try:
        meta = kb_versions.chunk_meta(rel, sha) or {}
        lookup.index_rows(db, rel, _extract_cached(path, sha), meta)
    except Exception:
        log.exception("KB: could not build the row index for %s", rel)


def _index_keywords(db, rel: str, ids: List[str], docs: List[Document]):
    db.execute("DELETE FROM kb_fts WHERE path=?", (rel,))
    db.executemany("INSERT INTO kb_fts(id, path, meta, text) VALUES (?,?,?,?)",
                   [(i, rel, json.dumps(d.metadata), d.page_content) for i, d in zip(ids, docs)])


def files(limit: int = 1000) -> List[dict]:
    with closing(_db()) as db:
        return [dict(r) for r in db.execute("SELECT path, sha, size, chunks, status, error, ts FROM kb_files "
                                            "WHERE store=? ORDER BY path LIMIT ?", (_store_key(), limit))]


# ------------------------------------------------------------------ keyword search
_STOP = {"the", "and", "for", "with", "what", "which", "who", "whom", "this", "that", "from", "are", "was", "how",
         "can", "does", "did", "has", "have", "need", "want", "our", "you", "your", "his", "her", "their", "there",
         "loan", "loans", "please", "give", "tell", "about", "details", "list", "show", "any", "all", "get", "find",
         "know", "should", "would", "could", "will", "into", "under", "over", "than", "then", "also", "only"}


def _salient_terms(query: str) -> List[str]:
    """Exact-match terms: numbers / codes (pincodes, DSA codes, phones) and quoted phrases."""
    terms = [t for t in re.findall(r'"([^"]{3,60})"', query)]
    for t in re.findall(r"[A-Za-z0-9]+", query):
        if (len(t) >= 4 and any(ch.isdigit() for ch in t)) and t not in terms:
            terms.append(t)
    return terms[:8]


def _fts(match: str, k: int, how: str) -> List[Document]:
    try:
        with closing(_db()) as db:
            rows = db.execute("SELECT id, meta, text, bm25(kb_fts) AS score FROM kb_fts WHERE kb_fts MATCH ? "
                              "ORDER BY score LIMIT ?", (match, k)).fetchall()
    except sqlite3.Error:
        log.exception("Keyword search failed")
        return []
    # bm25() is lower-is-better (negative); stored positive, higher-is-better, for the debug view.
    return [Document(id=r["id"], page_content=r["text"],
                     metadata={**json.loads(r["meta"]), "match": how, "keyword_score": round(-r["score"], 4)})
            for r in rows]


def keyword_search(query: str, k: int = 6) -> List[Document]:
    """Chunks containing an exact number / code / quoted phrase from the query."""
    terms = _salient_terms(query)
    return _fts(" OR ".join('"' + t.replace('"', "") + '"' for t in terms), k, "exact") if terms else []


# Short acronyms that carry meaning in this domain (kept although shorter than 3 letters).
_SHORT = {"hl", "bl", "pl", "cv", "pf", "rm", "sm", "od", "cc"}


def lexical_search(query: str, k: int = 6, require_any: Optional[List[str]] = None) -> List[Document]:
    """BM25 over the meaningful words of the query: rare words (lender / person names) weigh most.
    require_any: phrases of which a chunk must contain at least one (e.g. a bank's aliases)."""
    words = list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]{2,}", query.lower())
                               if (len(w) >= 3 or w in _SHORT) and w not in _STOP))[:16]
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)
    if require_any:
        need = " OR ".join('"' + a.replace('"', "") + '"' for a in dict.fromkeys(require_any))
        match = f"({need}) AND ({match})"
    return _fts(match, k, "lexical")


def document_chunks(path: str, limit: int = 5000) -> List[Document]:
    """Every indexed chunk of one file (the keyword index holds all of them, including keyword-only files)."""
    with closing(_db()) as db:
        rows = db.execute("SELECT id, meta, text FROM kb_fts WHERE path=? LIMIT ?", (path, limit)).fetchall()
    return [Document(id=r["id"], page_content=r["text"], metadata=json.loads(r["meta"])) for r in rows]
