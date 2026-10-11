"""Test isolation: no API keys, no network, no model downloads, temp database, uploads and policy folder.
Environment variables override .env, and must be set before app settings are first read."""
import os
import shutil
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="umtest-"))
_fixtures = Path(__file__).resolve().parent / "fixtures"     # fictitious sample lenders, test-only
shutil.copytree(_fixtures / "policies", _tmp / "policies")
(_tmp / "uploads").mkdir()
(_tmp / "urban_money").mkdir()
os.environ.update({
    "URBAN_MONEY_DIR": str(_tmp / "urban_money"), "KB_INDEX_DB": str(_tmp / "kb_index.db"),
    "KB_CACHE_DIR": str(_tmp / "kb_cache"), "OCR_ENABLED": "false",
    "GROQ_API_KEY": "", "TAVILY_API_KEY": "", "PINECONE_API_KEY": "", "OPENAI_API_KEY": "", "EVAL_JUDGE_PROVIDER": "groq",
    "LLM_PROVIDER": "groq", "OPENAI_MODEL_SMART": "gpt-5-mini", "RAG_MODEL": "", "LIVE_CHECK": "false",
    "APP_ENV": "test",
    "ADMIN_API_KEY": "test-admin-key", "WARMUP": "false", "RATE_LIMIT_PER_MIN": "1000", "AUTH_RATE_LIMIT_PER_MIN": "1000",
    "AUDIT_DB": str(_tmp / "audit.db"), "POLICY_DIR": str(_tmp / "policies"), "KB_DIR": str(_fixtures / "sample_kb"),
    "UPLOAD_DIR": str(_tmp / "uploads"), "ATTACHMENT_DIR": str(_tmp / "attachments"),
    "RERANKER": "lexical", "RAG_EVAL_AUTORUN": "false",
})
