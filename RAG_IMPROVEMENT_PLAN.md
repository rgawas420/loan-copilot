# RAG quality and data architecture: audit and plan

Status as of 2026-10-09. Measurements were taken on the live index (`var/kb_index.db`, 1.6 GB) and the 81 recorded
answer traces in `audit.db`. Section 2 lists the first round of changes and section 2b the second; sections 3–7 are the
original plan, with each item's status.

## 2b. Second round: implementation status

243 tests pass (212 originally). Retrieval on the 34 seed cases that do not need the LLM (live index, Pinecone):
**34/34** with the right document in the top 3 and the expected value in the model's context (it was 32/34 before
the code-question fix below).

| Plan item | Status | Where |
|---|---|---|
| Content-hash de-duplication at sync (4) | ✅ A file identical to one already indexed is recorded as `duplicate` and not indexed; if the original goes, the copy is indexed at the next sync. The admin list shows "Duplicate". | `knowledge_base.sync`, `_mark_duplicate` |
| Superseding monthly lists (4) | ✅ **Changed approach**: older editions are *not* disabled, because the May 2026 RM list has 225,850 rows and Aug 2026 only 20,355 (an update, not a replacement). When two editions of a document both match, the older one is ranked lower and labelled "older edition" in the prompt and on the source card; prompt rule 6 says the newer edition applies. | `kb_versions.family / older_editions`, `retrieval._mark_older_editions`, `lookup` |
| Effective dates in citations (4) | ✅ Source cards show the edition ("August 2026") and "Older edition". | `grounding.ref_view`, `ChatView.jsx` |
| Split `category` into doc type and product (3.4) | ✅ New `kb_docs.doc_type`, inferred from the name, then the text; backfilled from cached text (no re-index). Active documents typed "General": 200 → 116. Source authority and the "payout question → payout sheet" bonus use it. | `entities.infer_doc_type`, `kb_versions.backfill_doc_types` |
| Bank filter inside the search (4) | ✅ A question naming a bank also runs a bank-scoped vector query (Pinecone metadata filter) and a BM25 query that must name the bank. | `retrieval.vector_hits(banks=…)`, `lexical_search(require_any=…)` |
| One search implementation (4) | ✅ The recommendation workflow uses `retrieval.retrieve()` (reranking, threshold, evidence check). | `workflow.search` |
| One eligibility function (5.1) | ✅ `eligibility.calculate()` is used by the questionnaire, chat profiles and the rules engine; it warns on a monthly income over ₹50 lakh and an LTV over 90%. | `eligibility.py`, `profile.compute` |
| Ask, don't guess (5.3) | ✅ A customer profile without a loan type or city gets the app's own reply (profile, eligibility, what is missing), with no search and no LLM call. | `Profile.ask_answer`, `chat.Prepared.direct` |
| State the ranking basis (5.2) | ✅ The banker table carries one line saying it is ranked by RM availability (and not by lender rules when none are loaded). Ranking *by* eligibility still needs lender policies (A7). | `Profile.ranking_basis` |
| Layout-aware contact parsing (4) | ✅ Several people per row, split phone columns, other column names, city in brackets. Bajaj SM (1,275 contacts) and Canara (231) are now in the directory; the third file's rows without a city are reported, not guessed. | `banker_directory.people_in_row` |
| Code / payout / pincode questions read the right file type | ✅ (found by the seed evaluation) Code questions read code books, not RM lists or payout sheets; for codes, rows with a code column first. Fixed "ICICI new car loan code" and "latest Kotak machinery code". | `lookup._wanted_types` |
| Aliases in tables, admin-editable (3.3) | ✅ `kb_aliases` for banks, products and cities, layered on the built-in names; a change rebuilds the banker directory. Admin > Data quality > Name aliases. | `services/aliases.py` |
| Structured policy facts (3.5, 3.6) | ✅ Pipeline built: `policy_facts` table, CSV import, approval, effective dates. Approved values in force answer questions first (STRUCTURED FACTS passage, prompt rule 15). **Empty until the credit team imports values.** Admin > Data quality > Policy facts. | `services/policy_facts.py` |
| Golden set (6) | ✅ 41 cases in 8 categories, `evals/rag_golden_seed.yaml`; load with `python -m evals.seed_rag_golden`. Scoring now uses what the model was given (banker and lookup passages count) and supports "(not found)". **Not loaded into your database**: each run calls the LLM once per case. | `evals/`, `rag_debug.evaluate` |
| Latency (5.5, 7) | ✅ Directory / lookup / fact answers use a light passage search (10 candidates, no second pass): code and payout questions 18–61 s → 2–3 s on the live index. Vector search has a timeout (`VECTOR_TIMEOUT_S`, default 8 s); a stalled Pinecone call no longer holds up the answer. | `retrieval.retrieve(supporting=…)`, `vectorstore._scored` |
| Admin screens | ✅ Admin > Data quality: banker data problems, name aliases, policy facts (frontend rebuilt). | `DataQuality.jsx` |

Still open (needs data or a decision, not code): load structured lender policies (A7) so lenders can be ranked by
eligibility; import and approve policy facts; the data clean-up in 3.2; rotate the keys in `.env.example` (A13);
the August payout sheet is extracted with the code sheet's column names ("CODE IN NAME", "ELIGIBLE CARS / TRUCKS"
hold the payout figures): re-export it with its own header row.

---

## 1. Audit

### What already works (keep it)

The pipeline is further along than most RAG projects. These parts should be kept and built on:

- **Retrieval** ([retrieval.py](app/rag/retrieval.py)): hybrid search (vector + BM25 + exact terms) with reciprocal-rank
  fusion, a cross-encoder reranker, an ACTIVE-version filter, source-authority weighting, an evidence check, and one
  wider second pass. It answers "not found" without calling the LLM when there's no evidence.
- **Grounding** ([grounding.py](app/rag/grounding.py)): numbered `[S#]` passages, citations mapped back to sources,
  a check that every figure exists in the sources, and one automatic rewrite when that check fails.
- **Versioning** ([kb_versions.py](app/services/kb_versions.py)): one ACTIVE version per document (enforced by a unique
  index), an audit log, rollback, staging review for uploads, and authority priorities with penalties for OCR'd and
  expired documents.
- **Rules first, LLM explains** ([workflow.py](app/rag/workflow.py), [rules.py](app/rag/rules.py)): the LLM never decides
  status, lender or contacts on the structured path.
- **Observability**: every answer has a full trace, and the admin console has a golden-test runner.

### Weaknesses found (with evidence)

| # | Problem | Evidence | Impact |
|---|---|---|---|
| A1 | **Banker records were never structured.** RM lists are kept only as pipe-joined text in an FTS table (`kb_rows`). Each question re-reads, parses and de-duplicates the rows in Python. | 371,665 RM-list rows contain only **158,068** distinct lender + banker + phone records (57% duplicates). One "home loan RM in Mumbai" question parsed 23,230 rows. | Slow (0.2–1.6 s per banker query before the LLM); hard to validate; no place to record data problems. |
| A2 | **Identical files are all ACTIVE.** `WhatsApp Chat with Product Team x Sales Helpdesk/` holds byte-identical copies of the RM lists, code books and payout sheets. | 14 duplicate files; about 430k duplicate rows in `kb_rows`. | Counts are doubled, and the same rows fill the model's context twice. |
| A3 | **Table lookups showed the oldest lists.** Files were ordered by number of matching rows, and older lists are bigger. | "SBI home loan RM list in Pune" showed May 2026, its WhatsApp copy, and Jan 2026. **The Aug 2026 list was never shown.** | Outdated contacts reach the answer. |
| A4 | **Popular banker questions answered "not found".** A lookup matching more than 500 rows is refused, and duplicates push common questions over that limit. | "Home loan RM for Bajaj Finserv in Pune" matched 1,136 rows (353 distinct bankers), so it was refused. 4 of the 26 recorded "not found" answers were banker questions like this one. | Failure on the most common use case. |
| A5 | **Three contact files can't be read at all.** | `Bajaj SM contact details.xlsx` (phone split over "First 5 / Last 5" columns), `CANARA BANK Business Contacts.xlsx` (`RAH HEAD NAME/CONTACT` columns), `BANKER CONTACT NUMBER LIST-1.xlsx` (`COMPANY NAME`, city inside the name). | Those bankers are never found. Before this change, nothing reported it. |
| A6 | **Dirty values.** | 9,780 names with a phone number inside them ("KULDEEP SINGH - 7579077329"); 1,667 invalid mobiles; 10,149 bankers listed with different numbers in equally recent lists; 294 lender spellings ("HDFC BANK" / "HDFC LTD", "ICICI BANK" / "ICICI Bank"). | Duplicates the de-duplication can't see; numbers to verify. |
| A7 | **The rules engine has no data.** `data/policies/` is empty and `policy_docs` has 0 rows. | `match_policies` always returns nothing, so every recommendation goes to the text-only path. | No deterministic lender eligibility. Lender ranking in chat uses only list recency and RM counts. |
| A8 | **Eligibility was computed twice, inconsistently.** | [profile.py](app/rag/profile.py) `compute()` vs [eligibility.py](app/rag/eligibility.py) `indicative_eligibility()`. "18 LPA" in the monthly-income field was read as ₹18 lakh **per month**, giving a max loan of about ₹12 crore. The workflow path silently treated a missing existing EMI as ₹0. | Wrong figures in the answer. |
| A9 | **The answer check couldn't catch a swapped phone number.** `verify_figures` checks that a number appears *somewhere* in the context, not next to the right name. | A model that moves a number to the wrong row of a banker table passed the check. | Wrong contact shown to a partner. |
| A10 | **`category` mixes document type and product.** | 310 of 582 documents are "General", and others are "Home Loan" or "LAP" where a type was expected. | The "payout question → payout sheet" bonus misses most documents; metadata filters are weak. |
| A11 | **Retrieval quality is unmeasured.** | `rag_golden` has 0 cases. The YAML golden cases only test the rules engine against fictitious sample lenders. | No regression signal for prompt, model or retrieval changes. |
| A12 | **Latency.** | Recorded traces: p50 22 s, p90 67 s. | Slow; the LLM rewrite, a second retrieval pass and the correction call stack up. |
| A13 | **Secrets in the example config.** | `.env.example` contains what look like live Groq, Tavily and Pinecone keys. | Example files are made to be shared and committed, so anyone who gets the project folder or repo has the keys. (`.dockerignore` keeps them out of the image.) **Rotate them** and blank the example. |

---

## 2. Changed in this round

All 212 existing tests still pass; 8 new tests were added (`tests/test_banker_directory.py`). Total: 220 passing.

| Change | Files | Measured effect |
|---|---|---|
| **Banker directory.** Normalized tables `dir_lenders`, `dir_bankers`, `dir_coverage` and `dir_issues`, with primary keys, foreign keys, unique constraints and a `(city_key, product, last_seen DESC)` index. Built from ACTIVE row files after every sync; identical files are skipped. The rebuild only runs when the set of active RM files changes (a fingerprint), and `bankers.find()` falls back to the old path whenever the directory is out of date. | new [banker_directory.py](app/services/banker_directory.py); [bankers.py](app/rag/bankers.py); [knowledge_base.py](app/services/knowledge_base.py) | 158k bankers, 285 lenders. Banker query **0.01–0.30 s** (was 0.16–1.59 s). Rebuild 25–40 s in the background. |
| **Normalization.** Lender names map to a canonical bank only when nothing else remains in the name ("HDFC LTD" → HDFC Bank; "HDFC Credila…" and "Muthoot Fincorp" stay separate). Raw loan types ("Top Up-HL", "BT-HL", "HLUC", "Auto Loan") map to canonical products. City spellings (Bengaluru → Bangalore). Honorifics and embedded phone numbers are stripped from names. | banker_directory.py | "HDFC LTD" and "HDFC Bank" rows for the same RM now merge. |
| **Data-quality report.** Every duplicate file, unreadable contact file, missing lender/city/phone, invalid number, number-in-name and conflicting number is logged with examples. | `GET /api/admin/kb/banker-quality` ([kb_admin.py](app/api/kb_admin.py)) | Surfaces A2, A5 and A6 for the data team. |
| **Banker questions go to the directory.** A question that asks for people (RM / banker / contact), names a bank or product, and names a known city is answered from the directory: ranked, de-duplicated, newest list first, with the total match count. Prompt rule 14 tells the model to copy that table exactly. | [chat.py](app/rag/chat.py); [grounding.py](app/rag/grounding.py) (`grounded-v5`) | All 4 failing questions from the traces (SBI, HDFC, Bajaj, Kotak) now return 10 bankers. |
| **Table lookup: count identical files once, newest list first.** | [lookup.py](app/rag/lookup.py) | The SBI Pune lookup now leads with Aug 2026 and doesn't show the WhatsApp copies. |
| **Contact check.** Each phone number in the answer must sit next to a name that the sources list with that number. A failure triggers the existing one-shot rewrite, and a remaining failure is shown to the user as "Check before use". | grounding.py `verify_contacts`; chat.py | A swapped-number table is caught. Lines naming several people are judged against the whole line, so this check doesn't raise false alarms. |
| **Eligibility inputs.** Annual figures in the monthly field are converted; the workflow notes when existing EMIs weren't captured. | profile.py; eligibility.py | "18 LPA" is read as ₹1.5 lakh/month. |

Two behavior changes to be aware of:
- Banker answers now show canonical lender names ("ICICI Bank", not "ICICI BANK").
- Top-up and balance-transfer RMs ("Top Up-HL", "BT-HL") now count as home loan RMs. The old matcher dropped them.

---

## 3. Target database schema and migration

**Stay on SQLite for now.** One file already holds the KB index, the data volume is small (about 160k bankers, a few
thousand policy facts), and the Docker volume and backups already cover it. Move to Postgres (plus pgvector if you
want to drop Pinecone) only when you run more than one app worker. The rate limiter and the background jobs are
in-process today, so that move is needed at the same point anyway.

### Tables (✅ = built in this round)

```
reference data
  ✅ dir_lenders(lender_id PK, name, name_key UNIQUE, canonical)
     lender_aliases(alias_key PK, lender_id FK)            -- replaces the hard-coded entities.BANKS; editable by admins
     cities(city_id PK, name UNIQUE, state)  city_aliases(alias_key PK, city_id FK)
     products(product_code PK, name)  product_aliases(alias_key PK, product_code FK)
     employment_types / customer_categories (code PK, name)

contacts
  ✅ dir_bankers(banker_id PK, lender_id FK, name, name_key, phone, phone_valid, email, designation,
                UNIQUE(lender_id, name_key, phone))
  ✅ dir_coverage(banker_id FK, city_key, product, last_seen, first_seen, periods, occurrences, priority, source_path,
                 PK(banker_id, city_key, product))  INDEX(city_key, product, last_seen DESC)
     urban_money_codes(code PK, lender_id FK, product_code FK, code_name, valid_from, valid_to, source_version_id FK)

policy facts (each row carries its source: document version + page/sheet/row)
     loan_products(product_id PK, lender_id FK, product_code FK, name, customer_category, status)
     eligibility_rules(rule_id PK, product_id FK, field, operator, value, unit, hard|deviation,
                       effective_from, effective_to, source_version_id FK)   -- feeds rules.Program
     roi_rates(product_id FK, cibil_min, cibil_max, amount_min, amount_max, roi_min, roi_max, effective_from, ...)
     payout_slabs(lender_id FK, product_code FK, volume_min, volume_max, payout_pct, month, source_version_id FK)
     processing_fees(product_id FK, fee_pct, fee_min, fee_max, effective_from, ...)
     document_requirements(product_id FK, customer_category, document, mandatory)
     property_rules(product_id FK, legal_category, allowed|case_to_case|not_allowed, ltv_cap, ...)

documents (existing, keep)
  kb_docs / kb_versions / kb_audit / kb_rows / kb_fts
  + kb_docs.doc_type and kb_docs.product as separate columns (fixes A10)
  ✅ dir_issues(issue, source_path, detail)  -> generalize to data_issues(entity, entity_id, issue, detail, status)
```

### Migration plan

1. ✅ **Directory alongside the old path.** Done. The old row-index path remains as the automatic fallback.
2. **Data clean-up (data team, no code).** Delete the `WhatsApp Chat…` copies and `Rm List.xlsx` / `Rm List-1.xlsx`
   (copies of each other). Mark Jan and May 2026 as superseded by Aug 2026, or set `effective_until`. Re-export the
   3 unreadable contact files with standard columns (`Lender Name | Type of Loan | City | RM Name | RM Contact No`).
   Work through `/api/admin/kb/banker-quality` until conflicting numbers are resolved.
3. **Move aliases into tables.** Seed `lender_aliases` / `city_aliases` / `product_aliases` from `entities.py` and
   give admins an edit screen. Read the canonical names from the tables instead of code.
4. **Split `category`** into `doc_type` and `product`. Backfill with `infer()`, then let admins correct it.
5. **Policy facts.** Load structured policies (the YAML format already validated by `Program`) for the top lenders
   by volume first. Then extract ROI, payout and fee tables from the rate sheets and payout sheets into the tables
   above. Each extracted row needs admin approval (reuse the `kb_staging` review flow) and keeps its
   `source_version_id`, so answers can cite document, version and effective date.
6. **Answer exact questions from tables.** ROI, payout, fee, code and document-checklist questions are answered by
   SQL first and sent to the model as a STRUCTURED FACTS passage (like BANKER MATCH). Vector search is the fallback
   for wording that the tables don't hold.

---

## 4. Ingestion and retrieval (remaining)

| Priority | Change | Where |
|---|---|---|
| High | **Content-hash de-duplication at sync.** When a new path's sha equals an ACTIVE document's sha, register it as an alias of that document instead of a new ACTIVE document. This fixes A2 for retrieval and passages too, not only for bankers. | `knowledge_base.sync`, `kb_versions.register` |
| High | **Supersede monthly lists automatically.** Files whose names differ only by period ("Pan India RM list May-2026" / "Jun-26" / "Aug 2026") should be versions of one document, so only the newest is ACTIVE. `kb_staging` already detects this for uploads; apply the same rule to folder syncs. | `kb_versions.infer` / `kb_staging` |
| High | **Push bank / product / doc-type filters into search** instead of dropping candidates afterwards. Use a Pinecone metadata filter on `bank` and `doc_type`, and give `kb_fts` UNINDEXED `bank` and `doc_type` columns used in the WHERE clause. Today a bank's chunks can be crowded out of the top 20 and then filtered away. | `retrieval.vector_hits`, `knowledge_base._fts` |
| Medium | **Layout-aware spreadsheet parsing.** Merge split-phone columns ("First 5 / Last 5"); handle several people per row (Canara: RAH head, AGM, DM each with a contact). Emit one record per person. | `ingestion.table_part`, `bankers._KEYS` |
| Medium | **Expose effective dates in citations.** Put `effective_from` / `version` / `label` into the source cards (the passage header already carries them) so the UI shows "Aug 2026 list, v3". | `grounding.ref_view` |
| Medium | **Single search implementation.** `vectorstore.search()`, used by the recommendation workflow, is a second, simpler hybrid search without reranking or evidence checks. Switch `workflow.retrieve_evidence` / `retrieve_kb` to `retrieval.retrieve()`. | workflow.py |
| Low | Semantic chunking for policy PDFs is already heading-aware; add a table-preserving splitter for policy tables (keep a whole ROI grid in one chunk). | ingestion.py |

---

## 5. Agent workflow and prompts (remaining)

Target flow, using the parts that exist today:

```
extract profile (profile.extract, fast LLM, extraction only)
  -> classify: loan type, city, customer category (code; ask when a critical field is missing)
  -> bankers.find(loan_type, city)              (directory, SQL)               ✅
  -> lender policies: rules.match(case)         (structured; needs A7 data)
       + retrieval.retrieve(policy query)       (hybrid + rerank, bank-filtered)
  -> eligibility.compute (ONE implementation)   (EMI, FOIR, LTV, max loan; every assumption labelled)
  -> rank lenders: rules status > eligibility headroom > RM availability in the city > list recency
  -> validate: every figure / contact / citation (verify_figures, verify_contacts ✅, invalid_citations)
  -> generate: fixed template (PROFILE_RULES); the model writes the summary and the key point only
```

Changes:

1. **One eligibility function.** Make `profile.compute()` a thin adapter over `eligibility.indicative_eligibility()`
   (or the reverse) so the chat and the recommendation paths can never disagree. Add LTV-cap and FOIR-cap checks
   against the lender program when one exists, and a plausibility check (for example, monthly income over
   ₹50 lakh → ask to confirm).
2. **Rank chat lenders by eligibility, not only RM availability.** Once A7 has data, run `rules.match()` inside the
   profile path and order the banker table by program status. Until then, say plainly in the answer that the
   ranking is by RM availability only (the module docstring already admits it; the user isn't told).
3. **Ask, don't guess.** When the profile lacks the city or loan type, return the existing "More Information
   Required" questions instead of a generic search.
4. **Conflict wording.** Rule 6 of the system prompt handles conflicting values by priority. Also add the
   effective date to the passage header comparison, so "Aug 2026 says X; May 2026 said Y" is the stated reason.
5. **Latency (A12).** Skip the LLM query rewrite when entities and metrics are already found (partly done); run the
   second retrieval pass only when the first had zero passages above the threshold, not on any failed check; cap
   `RAG_MAX_TOKENS` per route (banker tables need fewer tokens than comparisons).

---

## 6. Evaluation plan

### Golden set (seed `rag_golden` through Admin > RAG quality, about 60 cases)

| Category | Count | Example | Pass criterion |
|---|---|---|---|
| Banker search | 12 | "Bajaj home loan RM in Pune" | route = directory; every listed phone in the newest list; no duplicates |
| Banker search, alias spellings | 4 | "RM list Bengaluru LAP Kotak" | same result as the canonical spelling |
| Eligibility / lender match | 8 | full salaried HL profile | calculation equals a hand calculation; lenders from the rules engine |
| Policy questions | 8 | "Max LTV for Aavas HL" | expected document cited; value equal |
| ROI / payout | 8 | "Axis HL payout Aug 2026" | value from the Aug 2026 sheet, not April |
| Document checklist | 4 | "Documents for self-employed LAP" | expected document cited |
| Multi-bank comparison | 4 | "Compare HDFC and ICICI PL ROI" | both banks covered; table |
| Missing data | 4 | "Canara RM in Nashik" (none exists) | "not found", no invented contact |
| Conflicting / outdated | 4 | a code changed between April and Aug | Aug value given; difference mentioned |
| Complex profiles | 4 | co-applicant, existing EMIs, LPA income | income converted; EMIs deducted |

### Metrics (from the traces already recorded)

| Metric | How | Baseline (81 traces) | Target |
|---|---|---|---|
| Retrieval precision@k | expected document among the selected passages | not measured | ≥ 0.85 |
| Answer correctness | `rag_debug.answer_matches` on expected answer | not measured | ≥ 0.9 |
| Source accuracy | cited `[S#]` include the expected document | not measured | ≥ 0.9 |
| Hallucination rate | answers with `unsupported_figures` or `contact_mismatches` after the rewrite | 1 / 81 flagged (figures only) | < 2% |
| False "not found" | "not found" on in-scope questions | 4 of 26 "not found" were in-scope banker questions | 0 on the golden set |
| Latency | `timings.total_s + generate_s` | p50 22 s, p90 67 s | p50 < 8 s, p90 < 20 s |
| Cost | `ai_requests` tokens per answer, by route | in Admin > Billing & usage | track per route |

Run it the existing way: the golden runner starts automatically when the configuration or documents change
(`RAG_EVAL_AUTORUN`). The prompt version moved to `grounded-v5`, so the next start triggers a run once cases exist.
Add a CI job that runs `pytest` (deterministic, no keys) on every change.

---

## 7. Performance, cost and reliability

- ✅ Banker queries are an indexed SQL read; the expensive parse happens once per sync.
- Cache `kb_versions.authority_map()` / `active_versions()` already exists; add a small LRU over
  `retrieval.understand()` for repeated identical questions (non-personal text only).
- Per-route token limits (see 5.5), and skip the correction call when the only problem is an uncited answer.
- Timeouts and retries exist for the LLM (`LLM_TIMEOUT_S`); add the same for the reranker load and Pinecone calls,
  which currently block on the first request after a cold start.
- Rotate the keys in `.env.example` (A13) and keep only placeholders in it.
