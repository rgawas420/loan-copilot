"""Embeddings + vector store. Pinecone when PINECONE_API_KEY is set, otherwise in-memory.

The Urban Money folder is the primary knowledge base (see app/services/knowledge_base.py). Pinecone is
synced incrementally; in-memory mode rebuilds from data/sample_kb, the Urban Money folder (using cached text
extractions) and every policy file uploaded earlier (kept in uploads/ and listed in the policy_docs table).

search() is hybrid: Urban Money chunks rank ahead of other sources, and exact-match terms in the query
(pincodes, codes, phone numbers) are looked up in a keyword index as well as by embedding similarity.
"""
import logging
import threading
import time
from functools import lru_cache
from typing import List, Optional

from app.core.config import get_settings
from app.services.ingestion import load_and_chunk, load_directory

log = logging.getLogger(__name__)
PRIMARY_BOOST = 0.1          # similarity bonus for Urban Money chunks over other sources
RRF_K = 60                   # reciprocal-rank fusion constant
_build_lock = threading.Lock()
_vs = None


def query_prompt(model: str) -> str:
    """The instruction an embedding model expects before search queries (passages are embedded as they are)."""
    m = model.lower()
    if "bge-" in m and "-en" in m:              # BAAI/bge-small-en-v1.5, bge-base-en-v1.5, ...
        return "Represent this sentence for searching relevant passages: "
    return ""


@lru_cache
def get_embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings
    model = get_settings().embedding_model
    prompt = query_prompt(model)

    def build(local_only: bool):
        return HuggingFaceEmbeddings(model_name=model, model_kwargs={"local_files_only": local_only},
                                     encode_kwargs={"normalize_embeddings": True},
                                     query_encode_kwargs={"normalize_embeddings": True, **({"prompt": prompt} if prompt else {})})
    try:                       # cached copy first: checking the Hub for every file takes a minute on a slow link
        return build(True)
    except Exception:
        log.info("Embedding model %s not cached; downloading", model)
        return build(False)


def is_pinecone() -> bool:
    return bool(get_settings().pinecone_api_key)


def _reload_uploads(vs):
    from app.services import audit
    s = get_settings()
    for row in audit.list_policy_docs():
        path = s.upload_dir / f"{row['sha']}{row['ext']}"
        if not path.exists():
            continue
        try:
            docs = load_and_chunk(path, source_name=row["filename"], lender=row["lender"], loan_type=row["loan_type"])
            vs.add_documents(docs, ids=chunk_ids(row["sha"], len(docs)))
        except Exception:
            log.exception("Could not reload uploaded policy %s", row["filename"])


def _build():
    s = get_settings()
    if is_pinecone():
        from pinecone import Pinecone, ServerlessSpec
        from langchain_pinecone import PineconeVectorStore
        pc = Pinecone(api_key=s.pinecone_api_key)
        if s.pinecone_index_name not in [i["name"] for i in pc.list_indexes()]:
            pc.create_index(name=s.pinecone_index_name, dimension=s.embedding_dim, metric="cosine",
                            spec=ServerlessSpec(cloud="aws", region="us-east-1"))
            while not pc.describe_index(s.pinecone_index_name).status["ready"]:
                time.sleep(1)
        return PineconeVectorStore(index=pc.Index(s.pinecone_index_name), embedding=get_embeddings(),
                                   namespace=s.pinecone_namespace)
    from langchain_core.vectorstores import InMemoryVectorStore
    from app.services import knowledge_base
    log.warning("PINECONE_API_KEY not set: building in-memory KB from the Urban Money folder, %s and uploads", s.kb_dir)
    vs = InMemoryVectorStore(get_embeddings())
    vs.add_documents(load_directory(s.kb_dir))
    _reload_uploads(vs)
    try:
        knowledge_base.sync(vs)
    except Exception:
        log.exception("Urban Money knowledge base could not be loaded")
    return vs


def get_vectorstore():
    """Built once; concurrent first callers wait for the same build instead of building twice."""
    global _vs
    if _vs is None:
        with _build_lock:
            if _vs is None:
                _vs = _build()
    return _vs


def chunk_ids(sha: str, n: int) -> List[str]:
    return [f"{sha[:16]}-{i}" for i in range(n)]


def add_documents(docs, ids: Optional[List[str]] = None) -> int:
    get_vectorstore().add_documents(docs, ids=ids)
    return len(docs)


def delete_documents(ids: List[str]):
    for i in range(0, len(ids), 1000):                 # Pinecone deletes at most 1000 ids per call
        get_vectorstore().delete(ids=ids[i:i + 1000])


_search_pool = None
_pool_lock = threading.Lock()


def _scored(vs, query: str, k: int, flt):
    """Similarity search bounded by VECTOR_TIMEOUT_S: a slow vector store (a Pinecone round-trip can stall for
    10-20 s) must not hold up the answer; the keyword search still runs, and the timeout is logged."""
    global _search_pool
    timeout = get_settings().vector_timeout_s
    try:
        if not timeout or not is_pinecone():
            return vs.similarity_search_with_score(query, k=k, filter=flt)
        if _search_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            with _pool_lock:
                # Shared by every user, and the timeout counts time spent queued: too few threads under load
                # means searches silently time out and answers come from keyword search alone.
                _search_pool = _search_pool or ThreadPoolExecutor(get_settings().vector_search_threads,
                                                                  thread_name_prefix="vector")
        return _search_pool.submit(vs.similarity_search_with_score, query, k=k, filter=flt).result(timeout=timeout)
    except TimeoutError:
        log.warning("Vector search took longer than %ss; answering from keyword search", timeout)
        return []
    except Exception:
        log.exception("Vector search failed")
        return []


def _active_only(docs):
    """Drop Urban Money chunks that are not from a document's ACTIVE version (archived / disabled / stale)."""
    try:
        from app.services import kb_versions
        active = kb_versions.active_versions()
    except Exception:
        log.exception("Could not read active document versions")
        return docs
    out = []
    for d in docs:
        m = d.metadata
        if m.get("status") and m["status"] != "ACTIVE":
            continue
        if m.get("sha") and active.get(m.get("source")) != m["sha"]:
            continue
        out.append(d)
    return out


def search(query: str, loan_type: Optional[str] = None, lenders: Optional[List[str]] = None, k: int = 6):
    """Hybrid search. Urban Money chunks (primary KB) first, then policy text restricted to the case's loan
    type (plus untagged uploads) and, optionally, lenders."""
    from app.services.knowledge_base import CORPUS, keyword_search, lexical_search
    vs = get_vectorstore()
    types = [loan_type, "Unknown"] if loan_type else None
    # Urban Money documents are not tagged by lender / loan type, so those filters do not apply; lender
    # names steer the query instead.
    um_query = f"{query} {' '.join(lenders)}" if lenders else query
    if is_pinecone():
        flt = {}
        if types: flt["loan_type"] = {"$in": types}
        if lenders: flt["lender"] = {"$in": lenders}
        primary = _scored(vs, um_query, k, {"corpus": CORPUS})
        others = _scored(vs, query, k, flt or None)
    else:
        def keep(d):
            m = d.metadata
            return (m.get("corpus") != CORPUS and (not types or m.get("loan_type") in types)
                    and (not lenders or m.get("lender") in lenders))
        primary = _scored(vs, um_query, k, lambda d: d.metadata.get("corpus") == CORPUS)
        others = _scored(vs, query, k, keep)

    vector = [d for _, d in sorted([(s + PRIMARY_BOOST, d) for d, s in primary] +
                                   [(s + (PRIMARY_BOOST if d.metadata.get("corpus") == CORPUS else 0), d)
                                    for d, s in others], key=lambda x: -x[0])]
    # Reciprocal-rank fusion of exact-term hits, BM25 word hits (Urban Money only) and embedding hits.
    fused, docs = {}, {}
    for weight, hits in ((2.0, keyword_search(um_query, k)), (1.0, lexical_search(um_query, k)), (1.0, vector)):
        for rank, d in enumerate(hits):
            key = d.page_content
            fused[key] = fused.get(key, 0.0) + weight / (RRF_K + rank)
            docs.setdefault(key, d)
    ranked = _active_only([docs[key] for key in sorted(fused, key=lambda x: -fused[x])])
    if lenders:      # the matched lenders' own policy text must not be crowded out
        own = [d for d, _ in others if d.metadata.get("lender") in lenders][:(k + 1) // 2]
        ranked = own + [d for d in ranked if d not in own]
    return ranked[:k]
