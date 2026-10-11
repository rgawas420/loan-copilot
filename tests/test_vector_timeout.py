"""A slow vector store does not hold up the answer."""
import time

import app.rag.vectorstore as vectorstore
from app.core.config import get_settings


class Slow:
    def similarity_search_with_score(self, query, k, filter=None):
        time.sleep(1.0)
        return [("late", 0.9)]


def test_slow_vector_search_is_skipped(monkeypatch, caplog):
    monkeypatch.setattr(vectorstore, "is_pinecone", lambda: True)
    monkeypatch.setattr(get_settings(), "vector_timeout_s", 0.2)
    t = time.perf_counter()
    assert vectorstore._scored(Slow(), "q", 5, None) == []
    assert time.perf_counter() - t < 0.8 and "longer than 0.2s" in caplog.text
    monkeypatch.setattr(get_settings(), "vector_timeout_s", 0)
    assert vectorstore._scored(Slow(), "q", 5, None) == [("late", 0.9)]
