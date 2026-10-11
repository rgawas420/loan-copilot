# ROLE
You are a senior Forward Deployed Engineer. Build a complete, runnable project called
**Urban Money Loan Copilot**: an internal tool for loan channel partners (DSAs). It collects case details
from a free-text note or one question at a time, checks the case against structured lender policies, and
explains the result using an Agentic RAG workflow.

# CORE DESIGN RULE
**The rules engine decides; the LLM explains.** Status, lender, program, Banker/RM, mobile, Urban Money Code
and the list of concerns must come from structured policy data and deterministic checks. The LLM may only:
phrase the explanation, extract fields from free text, grade untagged documents, rewrite a search query,
summarise general web criteria, and answer follow-up questions. The app must work without an LLM key
(rules-only mode with template explanations).

# ATTACHED INPUTS (read them first)
1. Urban_Money_Loan_App_Questionnaire.pdf: the full question-based input specification
   (Step 1 common questions plus the HL, LAP, Working Capital, BL, PL, Professional, Machinery, New Car,
   Used Car, CV, CE, Education, LRD, Commercial/Industrial Purchase, Construction Finance,
   BT + Top-up and Plot + Construction modules, and the recommended app flow).
2. Agentic_RAG.ipynb: the reference LangGraph pattern (route, retrieve, grade, web fallback,
   rewrite/retry, generate, source tracking).
3. steps.md: the build order and file structure to follow.

# BUSINESS GOAL
A channel partner describes the case or answers simple questions. The app returns:
Top Lender -> Program -> Indicative Eligibility -> Why Suitable -> Concerns/Deviations -> Next Steps ->
Banker/RM -> Mobile -> Urban Money Code -> Alternative Lenders -> Policy Last Verified Date, plus a comparison
of every program checked. Recommendation status must be exactly one of:
"Strong Match", "Possible - Credit Check Required", "Not Suitable", "More Information Required".

# TECH STACK
FastAPI, LangGraph, Groq (ChatGroq; two models from env: GROQ_MODEL default openai/gpt-oss-20b for
extraction/grading/rewrite, GROQ_MODEL_SMART default openai/gpt-oss-120b for explanations, text-only
recommendations and chat; timeout and retries on every call), HuggingFace embeddings
(sentence-transformers/all-MiniLM-L6-v2, 384 dims), Pinecone (fall back to an in-memory vector store when
PINECONE_API_KEY is empty), Tavily web search, PyYAML, SQLite, plain HTML/CSS/JavaScript (no frontend
framework), Docker, pytest. Use lower-bound versions in requirements.txt (do not invent exact pins).

# PROJECT STRUCTURE
```
app/api/routes.py  app/core/config.py  app/core/logging.py  app/core/ratelimit.py
app/rag/questionnaire.py  app/rag/eligibility.py  app/rag/policies.py  app/rag/rules.py  app/rag/llm.py
app/rag/state.py  app/rag/vectorstore.py  app/rag/workflow.py  app/rag/intake.py  app/rag/chat.py
app/services/ingestion.py  app/services/audit.py  app/main.py
data/policies/*.yaml  data/sample_kb/*.md  evals/golden_cases.yaml  evals/run_evals.py
static/css/style.css  static/js/app.js  templates/index.html
tests/  uploads/  .env.example  Dockerfile  ingest_sample_kb.py  requirements.txt  run.py  README.md
```

# QUESTIONNAIRE ENGINE (app/rag/questionnaire.py)
- Encode EVERY question from the PDF as `Q(key, text, options, when)` where `when` is a conditional rule.
- Conditional logic examples: Salaried vs Self-Employed income blocks; GST turnover only if GST registered;
  BT questions only for Balance Transfer / BT + Top-up; plot questions only for Self Construction /
  Plot + Construction; Used Car also includes all New Car questions; fleet questions only for Fleet Owners;
  existing-loan details only if "Yes".
- Answer types via `kind_of(q)`: choice, money, money_list (oldest to latest, separated by /), pin, mobile,
  cibil (300-900 or "Not Checked"), age, year, years, count, percent, text. `validate(q, value)` returns a
  normalised value or a clear error. Validate on the server; the client shows the error.
- Minimum-critical-information gate per loan type (`MIN_COMMON` includes overdue and settled; add the
  fields the rules need, e.g. possession and legal_cat for Home Loan, salary_mode for Personal Loan).
- `next_question(case)` asks unanswered CRITICAL questions first, then optional ones. Optional questions can
  be skipped (stored in `case["_skipped"]`); critical ones cannot.
- Provide `applicable()`, `progress()`, `missing_critical()`, `enough_info()`, `prune_case()` (drop stale
  answers and unknown keys), `apply_answer()`, `next_payload()` and `field_catalogue()` for intake.
- `PERSONAL_KEYS` (name, mobile, every owner/co-applicant/tenant/employer/business/builder/developer/
  dealer/supplier name or property description) and `llm_safe_case()`: an ALLOWLIST of known, non-personal
  fields. Use it for every LLM prompt and web query.

# ELIGIBILITY (app/rag/eligibility.py)
- Parse amounts like "12.5 lakh", "1.2 cr", "45k", "12 LPA", "Rs 1,30,000" (units as whole words only).
- `INCOME_SOURCES` per loan type (e.g. income_amt for cars, biz_income for CV, coapp_income for Education,
  monthly_rent for LRD, profit_3y for Working Capital); convert annual to monthly.
- FOIR-based maximum loan; accept the lender's own FOIR/ROI/tenure, falling back to placeholder ASSUMPTIONS.
  Working Capital = share of latest turnover; Construction Finance = not computed.
- Missing income returns `None` ("not computed"), never 0.

# STRUCTURED POLICIES + RULES ENGINE (app/rag/policies.py, app/rag/rules.py)
- One YAML file per lender: `lender`, `sample`, `programs[]`. Program fields (all optional except id, name,
  loan_type): profiles, age_min/max, min_cibil, cibil_deviation_floor, min_monthly_income, min_job_years,
  min_total_exp_years, min_business_years, min_turnover, amount_min/max, max_ltv or ltv_slabs, foir, roi,
  tenure_years, legal_allowed, legal_case_to_case, rera_required_under_construction, salary_modes,
  max_unsecured_loans, max_enquiries_3m, no_bounces, no_overdue, no_settled, banker_rm, mobile,
  urban_money_code, policy_last_verified, source. Validate with Pydantic.
- Each check returns pass / fail / deviation / unknown / warn with a one-line detail. Status: any fail ->
  Not Suitable; any deviation or unknown -> Possible - Credit Check Required; else Strong Match. warn
  (policy older than POLICY_STALE_DAYS, or sample data) never changes status.
- Rank programs by status, fails, open items, then indicative maximum.

# AGENTIC RAG WORKFLOW (LangGraph, app/rag/workflow.py)
```
check_case -> (missing info? -> more_info -> END)
compute_eligibility -> match_policies
  programs found -> retrieve_evidence -> explain -> END                      (source: structured_policy)
  none -> build_query -> retrieve_kb -> grade_kb
            GOOD -> recommend_from_text -> END                               (source: private_kb)
            WEAK -> rewrite_query -> retrieve_kb (max 1 retry)
            WEAK again -> search_web (once) -> web_guidance -> END           (source: web_general, no lender)
```
- grade_kb: GOOD without an LLM call if any chunk is tagged with the case's loan type; LLM-grade only
  untagged chunks.
- explain: the LLM returns summary, why_suitable and next_steps only. Concerns come from the checks. On LLM
  failure or no key, use a template.
- recommend_from_text guardrails: status is capped at "Possible - Credit Check Required"; banker/RM,
  mobile, code and verified date are kept only if they appear verbatim in the evidence; a "None" lender
  becomes null.
- web_guidance: general criteria only; never names or recommends a lender.
- Use structured (JSON) outputs with the schema in the prompt and one retry on invalid JSON.
- Keep a `trace` list in the state (reducer: operator.add). Provide `run_recommendation()` and
  `stream_recommendation()` (yields each trace step, then the result).
- Return `comparison` (every program with its checks) and citations.

# FREE-TEXT INTAKE AND FOLLOW-UP CHAT
- app/rag/intake.py: redact mobile numbers locally before the LLM; extract fields using `field_catalogue()`;
  drop personal keys; never overwrite typed answers; apply fields in dependency order (loan_type first)
  through the same validation; report rejected fields with reasons.
- app/rag/chat.py: answer follow-up and "what if" questions from the structured rules for the loan type
  (contacts excluded), the result and the safe case; keep the last few turns; be brief; never invent.

# KNOWLEDGE BASE + INGESTION
- Sample KB: 3 FICTITIOUS lenders (Sample Lender A/B/C) as both YAML policies and Markdown policy text,
  covering Home Loan (salaried and self-employed), LAP, Business Loan, Personal Loan. Label them SAMPLE
  DATA everywhere.
- Markdown is split per program section. If a section is longer than one chunk, every piece repeats the
  lender/program header and the Banker/RM contact line.
- Ingestion supports YAML (structured, validated, stored as <lender-slug>.yaml) and PDF, TXT, MD, DOCX
  (optional lender and loan-type tags, recursive chunking 1000/150, content-hash dedupe, stable chunk ids,
  stored as <sha256><ext>, listed and deletable, reloaded at startup in in-memory mode).
- Retrieval filters by loan type (plus untagged documents) and, for evidence, by the top lenders.

# API
GET /api/health; POST /api/questionnaire/next; POST /api/questionnaire/answer (422 with message on invalid,
skip support); POST /api/intake; GET /api/pin/{pin} (India Post public API, 6-digit validation, graceful
failure); POST /api/case/save (returns case_id + random token, stores only its hash, update needs the token)
and GET /api/case/{id} (X-Case-Token header); POST /api/recommend and POST /api/recommend/stream
(server-sent events: step, result, error; audit every run); POST /api/chat; POST /api/ingest,
GET /api/policies, DELETE /api/policies/doc/{id}, GET /api/audit (X-Admin-Key, constant-time compare, refuse
the default key when APP_ENV=production, file type and size checks, never trust the uploaded filename for
paths). Per-IP rate limit on recommend, stream, intake and chat. Heavy imports lazy.

# UI (templates/index.html, static/css/style.css, static/js/app.js)
- Case sheet ledger on the left (dotted leaders, Change button per answer, skipped answers shown muted).
- "Describe the case in your own words" box (asks the partner to leave out names).
- One large question in the centre: option chips or one text input with a type-specific placeholder and
  input mode, "Needed to match lenders" tag on critical questions, inline validation errors, progress bar,
  Back / Skip this question (optional only) / Save and continue later / Next question.
- Result: status badge, lender and program, summary, indicative eligibility vs requested amount, banker/RM,
  mobile, Urban Money code, policy date, why suitable, concerns, next steps, alternatives, a comparison table
  (each program a row with status, indicative maximum and expandable ✓ / ✗ / ! / ? checks), a warning for
  text-only or web results, a live "How the agent is deciding" timeline, and an "indicative, not a sanction"
  note.
- Follow-up chat under the result with three suggested questions.
- Collapsible "Add lender policy": admin key, file (.yaml/.pdf/.txt/.md/.docx), optional lender and loan type.
- Design: fintech-ledger feel; cool paper background, deep green primary, brass for "possible", red for
  "not suitable". Bricolage Grotesque for headings, Figtree for body, with system fallbacks. Sentence-case,
  action-specific button text. Responsive down to mobile (tables scroll inside their container), visible
  focus states, respects prefers-reduced-motion, no innerHTML with user data (build DOM safely).
- Resume a saved case via ?case=ID#t=TOKEN (token in the fragment). Health pill shows rules-only mode when
  GROQ_API_KEY is missing.

# QUALITY BAR
- pytest tests that need no network or API keys: question conditionals, critical-first ordering, skip rules,
  answer validation, prune_case, amount parsing, eligibility income per loan type, every rules-engine check,
  PII allowlist, intake redaction and merge order, API endpoints (FastAPI TestClient) including case tokens,
  admin auth, YAML and document ingest with dedupe and delete, rate limit, SSE stream, ingestion metadata
  and long-section contact lines, and graph routing with stubbed LLM / retriever / web search (structured
  path with and without an LLM, not-suitable path, text path guardrails, weak KB -> one rewrite -> one web
  search -> general guidance, no evidence, missing-info early stop, streaming).
- evals/golden_cases.yaml: at least 30 cases (base case + changes -> expected status and lender), run in
  pytest without an LLM and by `python -m evals.run_evals` with the real configuration.
- Syntax-check all Python and JS. README with setup, flow diagram, policy format, endpoints, privacy choices,
  Docker notes, and a prominent note that sample lenders, ASSUMPTIONS and golden cases must be replaced
  with real, verified data.
- Docker: download the embedding model at build time, non-root user, SQLite and uploads on a volume.
- Do not claim anything was tested that you did not run.

# DELIVERABLE
The complete project as files plus a zip. Finish with a short summary of what is verified, what
was not (real Groq/Tavily/Pinecone/HuggingFace calls, browser rendering), and what I must replace
before real use (sample policies, ASSUMPTIONS, golden cases, and partner login before wider exposure).
