from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    groq_api_key: str = ""
    tavily_api_key: str = ""
    pinecone_api_key: str = ""
    pinecone_index_name: str = "urban-money-lender-policies"
    pinecone_namespace: str = "lender-policies-v1"
    # Which provider writes answers, rewrites questions, reads cases ("groq" or "openai"). The golden tests and the
    # release gate measure whichever is set, so change it, run them, then set a new baseline.
    llm_provider: str = "groq"
    groq_model: str = "openai/gpt-oss-20b"             # fast model: grading, intake extraction, query rewrite
    groq_model_smart: str = "openai/gpt-oss-120b"      # explanations, text-only recommendations, follow-up chat
    openai_model: str = "gpt-5-mini"                   # LLM_PROVIDER=openai: fast tasks
    openai_model_smart: str = "gpt-5-mini"             # LLM_PROVIDER=openai: answers (RAG_MODEL overrides)
    openai_reasoning_effort: str = "low"               # minimal / low / medium / high; RAG_REASONING_EFFORT overrides answers
    openai_max_output_tokens: int = 8000               # answer plus the model's hidden reasoning (both count as output)
    llm_timeout_s: int = 30
    embedding_model: str = "BAAI/bge-small-en-v1.5"     # 384 dims; a change re-indexes every file once
    embedding_dim: int = 384
    admin_api_key: str = "change-me"
    app_env: str = "development"

    # Primary, authoritative knowledge base: every supported file in this folder (and its subfolders) is
    # indexed automatically. Empty = the folder named "Urban Money" (any case) inside data/.
    urban_money_dir: Optional[Path] = None
    kb_index_db: Path = BASE_DIR / "var" / "kb_index.db"  # sync manifest + keyword (full-text) index
    kb_cache_dir: Path = BASE_DIR / "var" / "kb_cache"    # cached text extractions (avoids re-running OCR)
    kb_sync_on_startup: bool = True                      # index new / changed / deleted files at startup
    kb_max_file_mb: int = 200                            # larger files are skipped and reported
    kb_upload_max_mb: int = 50                           # largest file an admin can upload in the browser
    kb_vector_max_chunks: int = 5000                     # bigger files (huge lists) go to the keyword index only
    ocr_enabled: bool = True                             # OCR for images and scanned PDF pages
    # Verifying OCR text (Knowledge base > Verify text): an independent second reading of each image / scanned page
    # by a vision model, compared figure by figure with the OCR. "auto" = OpenAI when OPENAI_API_KEY is set, else
    # Groq when VISION_MODEL is set; "openai" / "groq" force one; "off" = OCR confidence and checks only.
    ocr_review: bool = True                              # check OCR'd pages before they can be verified
    ocr_second_reader: str = "auto"
    ocr_second_reader_model: str = ""                    # empty = gpt-5 (OpenAI) / VISION_MODEL (Groq)
    ocr_second_reader_max_pages: int = 40                # pages per document read a second time (cost cap)
    ocr_low_confidence: float = 0.85                     # OCR boxes below this are listed for checking
    kb_dir: Path = BASE_DIR / "data" / "sample_kb"       # free-text policy sheets (retrieval evidence)
    policy_dir: Path = BASE_DIR / "data" / "policies"    # structured policy rules (YAML, used by the rules engine)
    upload_dir: Path = BASE_DIR / "uploads"
    audit_db: Path = BASE_DIR / "audit.db"
    max_upload_mb: int = 10
    policy_stale_days: int = 90
    rate_limit_per_min: int = 12                         # per logged-in user (per client IP when not logged in)
    warmup: bool = True                                  # load embeddings + KB at startup, not on the first request
    # Concurrency. Every request handler, and every step of a streamed answer, runs on a worker thread; most of
    # their time is spent waiting on Groq / Pinecone, so threads are cheap and the default 40 is far too few.
    threadpool_size: int = 200                           # requests (and answer streams) handled at the same time
    vector_search_threads: int = 32                      # vector searches in flight, shared by all users

    # Grounded answers from the knowledge base (RAG). Every value here is recorded with each answer and each
    # golden-test run, and a change triggers the golden tests; nothing is changed per request.
    rag_model: str = ""                                  # empty = groq_model_smart
    rag_temperature: float = 0.0                         # factual answers: deterministic
    rag_max_tokens: int = 1200
    rag_top_p: float = 1.0
    rag_reasoning_effort: str = ""                       # gpt-oss on Groq: low / medium / high; empty = model default
    reranker: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # a cross-encoder model, "lexical", or "none"
    rag_candidates: int = 20                             # candidates kept after hybrid search, before reranking
    rag_top_k: int = 6                                   # passages sent to the LLM (5-8)
    rag_min_relevance: float = 0.2                       # 0..1 reranker score a passage needs to count as evidence
    vector_timeout_s: float = 8.0                        # a vector search slower than this is skipped (0 = no limit)
    rag_query_rewrite: bool = True                       # LLM rewrite of conversational / follow-up questions
    rag_debug_log: bool = True                           # record every question's retrieval trace for admins
    rag_trace_keep: int = 3000                           # traces kept (oldest are deleted)
    rag_eval_autorun: bool = True                        # run golden tests when the config or documents change
    # Golden-test judge: a second model checks every claim of each answer against the passage it cites, and the
    # answer against the reviewer's reference answer. Best as a different (stronger) model than RAG_MODEL.
    # Live claim check: after each knowledge-base answer, the fast model checks every claim against the passages; an
    # unsupported claim gets one rewrite, and partners see a confidence level (high / medium / low).
    live_check: bool = True
    # Before a knowledge-base answer in a conversation, the fast model decides whether the message is a follow-up
    # (a change to the customer discussed earlier, a lender for that customer, ...) or a new question.
    chat_followup_router: bool = True
    eval_judge: bool = True
    eval_judge_provider: str = "groq"                    # "groq" or "openai" (needs OPENAI_API_KEY; independent of
                                                         # the answer model, and outside Groq's rate limits)
    eval_judge_model: str = ""                           # empty = groq_model_smart (groq) / gpt-5 (openai)
    eval_judge_reasoning_effort: str = "high"            # low / medium / high; empty = model default
    openai_api_key: str = ""

    # User accounts (the chat app). The first account ever registered, and any email listed here, becomes admin.
    admin_emails: str = ""                               # comma-separated
    allow_registration: bool = True                      # false = only admins can create accounts
    # Google Sign-In: the OAuth client id (Web application) from Google Cloud Console; empty = no Google button.
    # Not a secret: the browser needs it too. Its "Authorized JavaScript origins" must list the app's URL.
    google_client_id: str = ""
    session_days: int = 7                                # login cookie lifetime
    auth_rate_limit_per_min: int = 10                    # per client IP, on login and register
    max_conversation_kb: int = 2048                      # largest saved conversation
    attachment_dir: Path = BASE_DIR / "var" / "attachments"  # text extracted from files attached to chats
    attachment_max_mb: int = 15
    ads_enabled: bool = True                             # sponsored cards under knowledge-base answers
    ads_every_n_answers: int = 3                         # at most one ad per this many answers, per user
    # Branch map under customer-profile answers: OpenStreetMap (Nominatim) search, no key; cached, 1 request/s.
    places_enabled: bool = True
    places_cache_db: Path = BASE_DIR / "var" / "places_cache.db"
    places_user_agent: str = "UrbanMoneyLoanCopilot/1.0 (lender branch map)"   # OSM policy: identify the app
    vision_model: str ="qwen/qwen3.8-27b"              # reads attached images (text, tables, layout); empty = OCR only

    # Usage and billing. Plans, model prices, warning thresholds and the other business settings are edited by
    # admins (Admin > Billing & usage) and stored in the database; these are deployment settings only.
    usage_reservation_ttl_min: int = 15                  # a reservation left open longer (crash) is released
    usage_estimate_chat: int = 4000                      # tokens reserved for a chat answer, besides the user's text
    usage_estimate_recommend: int = 6000                 # ... for a recommendation (explanation, grading, rewrites)
    usage_estimate_intake: int = 2500                    # ... for reading a free-text case description
    payment_provider: str = "mock"                       # "razorpay", or "mock" (development only: no real money)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    mock_payment_secret: str = ""                        # empty = a random secret per process


@lru_cache
def get_settings() -> Settings:
    return Settings()
