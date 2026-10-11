# Urban Money Loan Copilot

An internal tool for loan channel partners (DSAs). The partner describes a case in one line or answers
questions one at a time. The copilot checks the case against every lender program for that loan type and
recommends a lender, program, Banker/RM and Urban Money Code. It shows the reasoning as a lender-by-lender
comparison and answers follow-up questions about the result.

**Design rule: the rules engine decides, the LLM explains.** The status, lender, contacts and codes always
come from structured policy data and deterministic checks, never from the LLM. The LLM only phrases the
explanation, reads free-text notes, and answers follow-up questions.

Stack: FastAPI, LangGraph, Groq, HuggingFace embeddings, Pinecone or in-memory vector store, Tavily, SQLite,
React + Vite frontend (plus the original plain HTML/JS page at `/classic`), Docker, pytest.

## How a case flows

```text
Free-text note  ──(LLM extracts fields, validated)──┐
Question by question (critical questions first) ────┴─> case sheet ─> "enough information" gate
                                                                          │
check_case ──missing──> More Information Required                          ▼
   │ ready
compute_eligibility (income source per loan type)
   │
match_policies (rules engine over data/policies/*.yaml)
   ├─ programs found ─> retrieve_evidence ─> explain (LLM, or template)       source: structured_policy
   └─ none ─> build_query ─> retrieve_kb ─> grade_kb
                 GOOD ─> recommend_from_text (capped at "Possible")           source: private_kb
                 WEAK ─> rewrite_query ─> retrieve_kb (once)
                 WEAK again ─> search_web ─> web_guidance (no lender named)   source: web_general
```

Statuses: **Strong Match**, **Possible - Credit Check Required**, **Not Suitable**, **More Information Required**.

Each check is pass, fail, deviation, unknown or warn. The rules engine sets a program's status like this:

| Check results | Program status |
|---|---|
| any **fail** | Not Suitable |
| any **deviation** or **unknown** | Possible - Credit Check Required |
| all pass | Strong Match |
| **warn** (stale or sample policy) | shown as a concern, status unchanged |

The agent's steps stream to the screen as they happen (server-sent events).

## Setup

```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # add GROQ_API_KEY (and TAVILY_API_KEY if wanted)
python ingest_sample_kb.py                            # only needed with Pinecone
python sync_urban_money.py                            # first index of data/URBAN MONEY (the app also syncs at startup)
python run.py                                         # http://127.0.0.1:8000  (API docs at /docs)
```

### Frontend (React)

The React app lives in `frontend/` and needs Node 20.19+ or 22.12+.

```bash
cd frontend
npm install
npm run build        # writes frontend/dist; FastAPI then serves it at http://127.0.0.1:8000/
npm run dev          # or: live-reload dev server on http://127.0.0.1:5173, proxying /api to :8000
```

Without a build, `/` shows the original HTML/JS page, which is always available at `/classic`.

The React app is a chat workspace in the style of ChatGPT:

- **Accounts.** Users sign up and log in with email and password. Each user sees only their own chats.
- **One chat per loan case.** Describe the case in your own words, or pick the loan type and answer one question
  at a time. Option questions show as chips, with Back and Skip where allowed. The case sheet (right panel) shows
  every answer and has Change links. When there is enough information, **Find suitable banks** runs the agent.
  Its steps stream into the chat, and the result (with the comparison table) appears as a reply. Follow-up
  questions about the result, or questions to the knowledge base, use the same composer (switch with the
  mode button).
- **Streaming answers.** Questions ("Ask anything", or follow-ups about a result) go to `POST /api/chat/stream`
  (server-sent events). The chat shows the real retrieval steps as they happen (searching the knowledge base,
  reading N documents, preparing the answer), then the text arrives token by token from the model with a
  cursor, rendered as Markdown (tables, lists, code blocks with syntax highlighting and Copy). Sources appear as
  cards when the answer is complete; click one to see the passage the answer used (admins can also download
  the file). Stop cancels generation and keeps the partial answer. Answers have Like, Dislike, Regenerate (in
  place, no duplicate question) and Copy. A failed request shows Try again; a stream that breaks off keeps
  the partial text and offers Continue (the model picks up where it stopped) or Retry. Auto-scroll follows the
  answer until you scroll up; a ↓ button jumps back to the latest content.
- **Attachments.** The + button attaches up to 5 files (PDF, Word, Excel, CSV, text, images with OCR) to a
  question. Their text is extracted on the server, kept per user (`ATTACHMENT_DIR`) and added to that question's
  prompt only; attachments never enter the shared knowledge base.
- **History.** Chats save automatically after every turn, including answers, sources and feedback. The sidebar
  groups them by date and lets you search (titles and message text), pin, rename, archive, delete and reopen them.
- **Admin console** (admins only): usage overview, users (change role, disable or enable, reset password,
  delete, add), the knowledge-base documents (below), an activity log, and the earlier policies and audit tools. The activity log records actions
  such as logins and recommendation outcomes. It never stores case details.

Old saved-case links (`/?case=ID#t=TOKEN`) open that case in a new chat. The original page at `/classic` is
unchanged and still uses `ADMIN_API_KEY` for its admin tools.

### Managing knowledge-base documents (Admin console, Knowledge base tab)

The documents behind the copilot's answers are the files in the Urban Money folder (see below). The Knowledge
base tab manages that folder from the browser:

- **Upload** files by drag and drop or a file picker (up to 50 at a time, `KB_UPLOAD_MAX_MB` each, default 50),
  optionally into a subfolder. Every upload is **checked first** (`app/services/kb_staging.py`): text extracted,
  sections counted, exact duplicate (SHA-256), and the existing documents it may be a new version of (file name,
  document type, bank / product, content overlap; the month in the name, e.g. "AUG 26", becomes the version label
  and effective date). A file that matches nothing is added at once as a new document. A possible new version, an
  exact duplicate or an image waits under **Review uploads**: *Activate* (the current version is archived and
  kept for rollback, the new one becomes ACTIVE in one step; the database allows only one ACTIVE version per
  document), *Add as new document*, or *Reject*. Nothing waiting for review is searched.
- **Compare versions** before activating (`app/services/kb_review.py`): table rows are matched on their key
  columns (e.g. BANK NAME) and each changed cell is shown old → new, with rows added and removed; text is diffed
  line by line, and every figure found in only one version is listed. *Details* compares any earlier version with
  the active one.
- **Knowledge tests**: golden tests (RAG quality tab) whose expected document is this document must find their
  expected answer in the new version. If any fail, activation is refused until the admin chooses *Activate
  anyway* (recorded in the version note). The full golden run follows automatically after activation.
- **Verified text for images and scanned PDFs** (`app/services/kb_verify.py`, checks in `app/services/ocr_review.py`):
  documents read by OCR are marked *OCR · not verified*. *Verify text* shows each scanned page as the OCR saw it,
  with the flagged text boxed on the image, and a checklist the admin must tick before saving: every page
  ("I compared this page with the original"), OCR boxes below `OCR_LOW_CONFIDENCE` (0.85), boxes too uncertain to be
  indexed at all, figures that look misread (letters run into a figure, as in "RO18.50%" for "ROI 8.50%"; O / I / l
  inside numbers; percentages above 100; two decimal points), pages where OCR found no text, and every figure on
  which OCR and an independent **second reading** by a vision model disagree. The second reader is
  `OCR_SECOND_READER` = auto (OpenAI `gpt-5` when `OPENAI_API_KEY` is set, else Groq `VISION_MODEL`), openai, groq or
  off; it reads at most `OCR_SECOND_READER_MAX_PAGES` pages per document (paid: one vision call per page, run once per
  file content when an image upload is checked or the screen is first opened; *Run checks again* repeats it). Its
  transcription can be used as a page's text (its Markdown tables become table rows, like a spreadsheet's).
  Saving is refused while the checks run, while an item is unticked, while `[unreadable]` markers remain, if
  suspicious figures were not accepted, and if someone else saved after the screen was opened. For a PDF with real
  text pages, only its scanned pages are edited and replaced; for an image or a fully scanned PDF the verified text
  replaces the whole extraction. The verified text is used for search, answers, previews and comparisons (the
  original stays the document's file); every earlier verified text is kept in its history. An uploaded image waits
  for review until its text is verified; an image OCR cannot read can be rescued by typing its text. Removing the
  verified text goes back to OCR. Chunks carry `extraction` = text / ocr / verified. `OCR_REVIEW=false` turns the
  checks off.
- **Source priority** (*Details* > Source priority): each document has a source type (Official policy 100,
  Payout sheet 90, Rate sheet 90, Code book 85, Internal reference 70, Reference 50, Old document 30, FAQ 20),
  by default from its document type; admins can set the type or a custom priority (applies at once, no
  re-indexing). Unverified OCR text counts 20 lower, a version past its effective-until date 30 lower. Retrieval
  orders evidence by it (and favours the document type the question asks for: payout → payout sheets), and every
  passage in the prompt shows its authority, dates and text quality; when passages disagree the higher priority
  applies, equal priorities are reported as a conflict. The model never decides authority itself.
- **Change history**: an audit log of every knowledge-base change (upload, add, activate, reject, process,
  remove, archive, restore, delete, rollback, merge, verify, metadata and priority changes) with who, when,
  document and version; searchable in the Knowledge base tab and per document under *Details*.
- **Historical search** (*Test retrieval* > Historical): keyword search over archived versions, for audits.
  Users' answers only ever use ACTIVE versions.
- **Possible duplicates** scans the documents already in the folder (e.g. Code Book-3, -4, -5); keep the current
  one and the others become its archived versions. Files copied into the folder directly are added as they are.
- **Processing** (text extraction, OCR, chunking, embeddings, keyword index) runs in a background job queue, one
  job at a time, so uploads, reprocessing and syncs never overlap. The page shows live progress.
- **Status per file**: indexed, keyword index (very large lists), not processed (new or changed on disk),
  queued, processing, failed (with the error), unsupported type, too large. Filter by status or folder, and
  search by name.
- **Actions**: preview the text the copilot reads from a file, download the original, reprocess, archive,
  restore, or delete. Reprocess, archive and delete also work on a multi-row selection.
  **Process new and changed** picks up files copied into the folder by other means.
  **Rebuild everything** re-reads and re-indexes every file.
- **Archive** moves a file to the folder's hidden `.archive` subfolder, which sync ignores, and removes it from
  the index. Restore puts it back.
- **Test retrieval** runs a question through the same hybrid search the copilot uses, to confirm that a
  document is found.

Every upload, reprocess, sync, archive, restore and delete is recorded in the activity log.

### Accounts and security

- The **first account registered becomes admin**. Any email listed in `ADMIN_EMAILS` also becomes admin when it
  signs up. Admins can promote other users. The app always keeps at least one active admin, and an admin
  cannot demote, disable or delete their own account.
- Passwords are hashed with scrypt (standard library). Sessions are random tokens in an `HttpOnly`,
  `SameSite=Lax` cookie (also `Secure` when `APP_ENV=production`). Only a hash of each session token is stored.
  Sessions last `SESSION_DAYS`. Disabling a user or resetting their password signs them out everywhere.
- Every conversation query is filtered by the owner's user id. Requesting another user's chat returns 404.
- Login and sign-up have a separate rate limit (`AUTH_RATE_LIMIT_PER_MIN` per IP).
- Set `ALLOW_REGISTRATION=false` to close public sign-up. Admins can still add users in the console.
- Staff endpoints (`/api/ingest`, `/api/policies`, `/api/audit`, `/api/kb/*`) accept a logged-in admin or
  the `X-Admin-Key` header.
- Accounts, sessions and chats live in the same SQLite file as the audit log (`AUDIT_DB`).

### Keys and fallbacks

- **Without `GROQ_API_KEY`** the app still works in rules-only mode. Lender matching, comparison and
  template explanations work. Free-text intake, follow-up chat and text-only recommendations are disabled.
- **Without `PINECONE_API_KEY`** the app builds an in-memory KB from the Urban Money folder, `data/sample_kb/`
  and earlier uploads at startup (re-embedding everything on each start; OCR results are cached). Set the key to use Pinecone; the index dimension 384 matches `BAAI/bge-small-en-v1.5` (changing `EMBEDDING_MODEL` re-indexes every file once at the next sync).
- **Without `TAVILY_API_KEY`** the web step is skipped.

Run the tests (no API keys or network needed): `pytest`.
Run the golden cases through the full agent with your real configuration: `python -m evals.run_evals`.

## Urban Money knowledge base (primary source)

Every document in `data/URBAN MONEY/` (found case-insensitively, or set `URBAN_MONEY_DIR`) is part of the
knowledge base, including subfolders. No file names are hard-coded: drop a file in, remove it, or replace it,
and the next sync picks it up.

| Format | How it is read |
|---|---|
| PDF | Text layer per page; pages with no text (scans) are OCR'd |
| DOCX (DOC via LibreOffice if installed) | Paragraphs and tables |
| TXT, MD | As text (WhatsApp exports included) |
| CSV, XLS, XLSX, XLSM, XLSB | Every sheet; each row becomes `Header: value \| ...`, title rows are kept as context |
| JSON | Flattened to `key.path: value` (a list of objects becomes one row per object) |
| HTML | Visible text (scripts and styles dropped) |
| VCF | Contact name, phone, organisation, email |
| PNG, JPG, WEBP, BMP, TIFF, GIF | OCR (RapidOCR, runs locally, no system install needed) |

Audio, video and other formats are listed as skipped. Every chunk starts with `Source: <file> | <sheet/page>`,
because file names often carry the lender, product or RM.

**Sync** runs at startup (`KB_SYNC_ON_STARTUP`), from `python sync_urban_money.py [--full | --dry-run]`, or
through `POST /api/kb/sync`. It hashes each file and, in Pinecone, indexes new files, re-indexes changed ones
and deletes the vectors of removed ones; chunk ids are deterministic, so an interrupted sync is safe to rerun.
Extracted text is cached by content hash in `KB_CACHE_DIR`, so OCR runs once per file. `GET /api/kb/status`
lists every file with its status, chunk count and any error.

**Retrieval** is hybrid. Urban Money chunks rank ahead of other sources, and exact terms in a question
(pincodes, DSA codes, phone numbers, quoted names) are also matched in a local SQLite full-text index
(`KB_INDEX_DB`), which finds rows in large sheets that embeddings miss. The recommendation agent, the
follow-up chat and `POST /api/kb/ask` all treat these documents as authoritative and cite file names.

## Grounded answers: the RAG pipeline

Knowledge-base answers must say what the documents say, not what the model thinks. Every question in the
chat (and `POST /api/kb/ask`, `POST /api/chat`) goes through the same pipeline:

```text
question ─> understanding: banks, products, the fact asked for (max amount, ROI, FOIR, LTV, CIBIL, code, ...)
         ─> rewriting: follow-ups are resolved from the conversation ("What about HDFC?"); conversational questions
            are rewritten by the fast model, which may not add banks, numbers or codes (checked; else rejected);
            precise questions are searched as written
         ─> hybrid search per query: vectors + BM25 keywords + exact terms (codes, pincodes, phones)
         ─> fusion (reciprocal rank) ─> metadata filtering: ACTIVE versions only, other banks' documents dropped,
            other products demoted
         ─> reranking of the top 20 (local cross-encoder; lexical fallback) ─> top 6 above the relevance threshold
         ─> evidence check: relevant passages, the bank covered, the asked-for fact present with a value
              insufficient ─> search again, wider (once) ─> still insufficient ─> "I couldn't find this information
              in the available knowledge base." (the LLM is not asked)
         ─> table lookup (app/rag/lookup.py), for list / contact / code / payout / "how many" questions: every
            spreadsheet row matching the bank, product and other words of the question (e.g. the city), counted
            in code, goes first as [S1].. (passage search alone sees only 6 chunks and cannot count rows)
         ─> strict grounding prompt (app/rag/grounding.py, versioned) with numbered passages [S1]..[S6]
         ─> answer ─> citations mapped back to the exact passages (document, page, section, version)
         ─> figure check: every amount, percentage, code and number in the answer must appear in the sources
            (₹50 lakh = 50,00,000); a failing answer is rewritten once with the problems pointed out (kept only
            if it has fewer); anything still unmatched is shown to the user as "check before use"
```

**Extraction keeps structure.** PDFs keep page numbers, headings (font size / bold), tables (as rows) and drop
running headers and footers; Word keeps its heading hierarchy, lists and tables in order; Markdown and HTML are
split by heading. Every chunk starts with a context header (file, page, document title, bank, product, version,
section) and carries the same fields as metadata (`document_id`, `version_id`, `bank`, `product`, `category`,
`section`, `page`, `chunk_index`, `status`). Changing the chunker (`CHUNKER_VERSION`) re-indexes every file once.

**Versions.** A document is one logical source ("Payout Sheet") with versions that may have different file names
("215 PLUS APRIL 2026 PAYOUT SHEET.xls" → "AUG 26 PAYOUT SHEET.xls"), each with a label and effective dates; only
the ACTIVE one is in the folder, indexed and searched. Activating a new version archives the old one (its bytes
are kept in `.versions`, so it can be rolled back, under its own file name); archiving or deleting a document
disables it. Retrieval checks each chunk's version against the registry, so a stale vector
can never be used. Admins see versions, and edit a document's bank / product / category / title, under
*Knowledge base > Details*. Bank and product are inferred from the file name when a document is first seen.

**RAG quality (Admin console).**
- *Test a question*: runs the full pipeline and shows every step: rewritten question, every candidate passage
  with its vector, keyword, fusion and reranker scores and why it was used or dropped, the evidence check, the
  final context, the prompt, the model and parameters, the answer, citations and the figure check. Enter the
  expected answer / document / page and it diagnoses the first failing stage: extraction, chunking, retrieval,
  metadata filtering, reranking, final context, prompt / generation.
- *Question log*: the same trace for every question users asked (last `RAG_TRACE_KEEP`; attachment names only).
  Filter by "not found", weak evidence or unmatched figures. Turn off with `RAG_DEBUG_LOG=false`.
- *Golden tests*: questions with the expected answer, document and page. They run on demand and automatically
  when the configuration changes (model, temperature, prompt version, embeddings, reranker, chunking, retrieval)
  or documents are added, changed or removed. Each run stores its configuration; regressions are highlighted.
- *Configuration*: the exact settings every answer uses (`RAG_*`, `RERANKER` in `.env`; nothing changes per request).

When an answer differs from what you expect (or from another chatbot), check in this order: is the document
extracted correctly (Preview) -> are the chunks right -> did retrieval find them -> did filtering keep the right
document / version -> did reranking select them -> did the final context contain the answer -> did the model
use it. The diagnosis does exactly this. Do not switch LLM providers before the earlier stages are right.

## Lender policies: two kinds of data

| | Structured policy (`data/policies/*.yaml`) | Policy text (`data/sample_kb/*.md`, uploads) |
|---|---|---|
| Used for | The decision: the rules engine checks every rule | Supporting evidence shown to the LLM and cited |
| Contains | CIBIL minimum and deviation band, age, income, job and business vintage, turnover, amount range, LTV slabs, FOIR, ROI, tenure, accepted and case-to-case legal categories, RERA, salary mode, unsecured loans, enquiries, bounces, overdue and settlement rules, RM, mobile, code, verification date | Free text, one `## Program:` section per program with a `Loan type:` line |
| If missing | The loan type falls back to the text-only path, where status is capped at "Possible" and contacts must appear word for word in the text | No supporting text; the decision is unaffected |

See [data/policies/sample_lender_a.yaml](data/policies/sample_lender_a.yaml) for the format; every rule is
optional. Upload a `.yaml` through *Add lender policy*, or commit it to `data/policies/`. Uploaded YAML is
validated before it is saved.

Uploaded documents are deduplicated by content hash and can be tagged with a lender and loan type. Without a
loan-type tag, a PDF or DOCX can only be judged by the LLM grader. Uploads can be listed with
`GET /api/policies` and deleted with `DELETE /api/policies/doc/{id}`. In in-memory mode they are reloaded
from `uploads/` at startup.

## IMPORTANT: replace the sample data before real use

- `data/policies/` and `data/sample_kb/` hold **fictitious** Sample Lender A, B and C, with made-up RMs,
  mobiles and codes. Every result from them carries a "Sample data" warning.
- `app/rag/eligibility.py`: `ASSUMPTIONS` (FOIR, ROI, tenure per loan type) and `WC_TURNOVER_SHARE` are
  placeholders. They apply only when a program has no FOIR, ROI or tenure of its own.
- `evals/golden_cases.yaml` is written against the sample policies. Rewrite it with your credit team for
  your real policies; it is your regression suite.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status, vector store, key configuration, number of structured programs |
| POST | `/api/questionnaire/next` | Body `{case}`: next question (with type and placeholder), progress, readiness, case sheet |
| POST | `/api/questionnaire/answer` | Body `{case, key, value}` or `{case, key, skip: true}`: validates the answer; 422 with a message if invalid |
| POST | `/api/intake` | Body `{case, text}`: fills fields from a free-text note; returns filled and rejected fields |
| GET | `/api/pin/{pin}` | City / district / state auto-fill (India Post public API; fails gracefully) |
| POST | `/api/case/save` | Save and continue later; returns `case_id` and a private `token` (the token is needed to update) |
| GET | `/api/case/{id}` | Load a saved case; needs the `X-Case-Token` header |
| POST | `/api/recommend` | Run the agent; returns recommendation, comparison, decision trace and citations |
| POST | `/api/recommend/stream` | Same, as server-sent events: `step` for each agent step, then `result` |
| POST | `/api/chat` | Body `{case, result, question, history}`: follow-up question about a result |
| POST | `/api/ingest` | Upload a `.yaml` structured policy or a document in any KB format (`X-Admin-Key`) |
| POST | `/api/kb/ask` | Body `{question, loan_type?, history?}`: answer from the Urban Money KB with source files |
| POST | `/api/kb/sync` | Re-scan the Urban Money folder in the background; `?full=true` re-indexes everything (`X-Admin-Key`) |
| GET | `/api/kb/status` | Folder, sync progress and per-file index status (`X-Admin-Key`) |
| GET | `/api/policies` | Structured programs with stale flags, and uploaded documents (`X-Admin-Key`) |
| DELETE | `/api/policies/doc/{id}` | Remove an uploaded document from the KB (`X-Admin-Key`) |
| GET | `/api/audit` | Recent runs (`X-Admin-Key`) |

`/api/recommend`, `/api/recommend/stream`, `/api/intake`, `/api/chat` and `/api/kb/ask` are rate-limited per client IP
(`RATE_LIMIT_PER_MIN`, default 12).

## Token usage, quotas and billing

Modules: `app/services/usage.py` (ledger, quota, cost, reports), `app/services/billing.py` (plans, subscriptions,
lifecycle, payments, webhooks), `app/services/payments.py` (provider abstraction: Razorpay, and a mock for
development), `app/rag/metering.py` (records every LLM call), `app/api/billing.py` (endpoints).

- **Source of truth.** Every LLM call is recorded in `usage_ledger` with the token counts the provider reported
  (Groq returns them with the response, and in the last chunk of a stream). Database triggers make the ledger
  append-only. A call is marked `estimated` only when the provider returned nothing, e.g. an answer the user stopped.
  For grounded answers the input is also split into system prompt, history, RAG context, question and other
  (proportional to their length; admins only).
- **Quota without races.** Before a request, `usage.reserve()` takes the database write lock (`BEGIN IMMEDIATE`),
  checks rate per minute, concurrent requests, requests per period and tokens (used + still reserved + this
  request's estimate against the limit), and records a reservation. The model calls record actual usage; when
  the request ends the reservation is released. Concurrent requests cannot both spend the same allowance.
- **Billing periods.** Usage belongs to the subscription's period; a new period starts at zero and old rows stay.
- **Plans, prices, thresholds** are data, edited in Admin > Billing & usage (seeded with Free, Starter, Pro and
  Business, and Groq prices for gpt-oss: check them against Groq's current price list). A new model price applies
  from now on; recorded costs never change. Provider cost (USD) is kept apart from what customers pay (INR).
- **Limits per plan:** tokens per period, allowed answer models, requests per minute, concurrent requests, request
  size, answer length, knowledge-base access, attachment size and count, optional overage (accrued as a due charge
  at period end, never taken automatically), trial days.
- **Payments.** A paid plan is activated only by a verified event: a signed webhook (`POST /api/webhooks/payment`),
  or a checkout the server confirmed with the provider's API. Each webhook event id is applied once. With
  `PAYMENT_PROVIDER=razorpay`, create a Razorpay plan for each paid plan, enter its id on the plan, and point the
  Razorpay webhook (subscription.* , payment.failed, refund.*) at `/api/webhooks/payment` with
  `RAZORPAY_WEBHOOK_SECRET`. `mock` (development only) simulates payments with signed test webhooks.
- **Lifecycle:** free → trial → active → past_due (grace days) → expired or canceled → back to free. Applied when a
  subscription is read; chats and usage history are never deleted.
- **Classic page without login:** a shared daily token budget (Admin > Billing & usage > Settings; 0 = log in).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/usage/current` · `/history` · `/daily?range=7d\|30d\|period` · `/by-model` | The user's own usage |
| GET | `/api/plans` · `/api/subscription` | Plans on offer; the user's subscription, payments and charges |
| POST | `/api/subscription/checkout` · `/upgrade` · `/trial` · `/verify` · `/downgrade` · `/cancel` | Subscription changes |
| POST | `/api/webhooks/payment` | Signed payment-provider events |
| GET/POST/PUT | `/api/admin/billing/...` | Overview, users, ledger, plans, prices, payments, settings (admin) |

AI endpoints answer `402` (limit reached, with plan, limit and usage), `429` (rate or concurrency) or `413`
(request too large for the plan) with a JSON `detail.code`; streamed answers end with a `usage` event.

## Privacy and safety

- **Personal data never reaches the LLM, web search or audit log.** Only known, non-personal questionnaire
  fields are sent; see `PERSONAL_KEYS` and `llm_safe_case()` in `app/rag/questionnaire.py`. Name, mobile,
  employer, property, plot and RC owners, co-applicant, tenant, builder, developer, dealer and supplier stay
  in the saved case only.
- Free-text notes: mobile numbers are extracted locally and redacted before the text goes to the LLM. The
  partner is asked to leave names out. Names the LLM returns are discarded.
- Every answer is validated by type: money, PIN, mobile, CIBIL 300–900 or "Not Checked", age, years, counts,
  percentages and options.
- On the text-only path, Banker/RM, mobile, code and verification date are kept only if they appear word for
  word in the retrieved policy text; otherwise they show "Not available in KB".
- Saved cases need an access token. Only its SHA-256 hash is stored, and the resume link carries the token
  in the URL fragment, so it does not reach server logs.
- Admin endpoints use a constant-time key check and refuse the default key when `APP_ENV=production`.
- Uploads are size- and type-checked, stored under their content hash (never the uploaded name), and YAML is
  schema-validated.
- LLM calls have a timeout and a retry. If the explanation call fails, a template explanation is used and
  the decision is unchanged.
- The recommendation is indicative, not a sanction; the UI says so.

**Not yet built:** partner login. The case token stops people from guessing case IDs, but there are no user
accounts. Add SSO or partner login before exposing the app beyond a trusted network.

## Docker

```bash
docker build -t loan-copilot .
docker run -p 8000:8000 --env-file .env -v copilot-data:/srv/var loan-copilot
```

The build has two stages: Node builds the React app, then the Python image serves it. The image downloads the
embedding model at build time and runs as a non-root user. SQLite and uploaded
documents live on the `/srv/var` volume. Structured policies are read from `data/policies/` inside the image:
commit them to the repository, or mount the folder (`-v ./data/policies:/srv/data/policies`) so YAML
uploaded at runtime survives a restart.

## Layout

```text
app/api/routes.py           endpoints                     app/rag/questionnaire.py  questions, conditions, types, validation, gate
app/core/config.py          settings                      app/rag/eligibility.py    income per loan type + FOIR estimate
app/core/ratelimit.py       per-IP rate limit             app/rag/policies.py       structured policy schema + loader
app/rag/llm.py              Groq models, retry            app/rag/rules.py          rules engine (checks, status, ranking)
app/rag/state.py            graph state + LLM schemas     app/rag/workflow.py       LangGraph agent + streaming
app/rag/vectorstore.py      embeddings, filtered search   app/rag/intake.py         free-text intake
app/rag/chat.py             follow-up questions           app/services/ingestion.py loaders + chunking
app/services/audit.py       audit, saved cases, uploads   data/policies/            structured rules (YAML)
data/sample_kb/             policy text (Markdown)        evals/                    golden cases + runner
templates/ static/          classic UI (/classic)         tests/                    unit, API, routing, golden cases
frontend/src/App.jsx        React shell, partner page     frontend/src/useCase.js   case state + question flow
frontend/src/api.js         API client + SSE stream       frontend/src/components/  Header, CaseSheet, IntakeBox,
                                                                                    QuestionCard, ResultView,
                                                                                    ChatPanel, AdminPage
```

The review findings that led to this version, and their status, are in [bugs.md](bugs.md).
