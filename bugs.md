# Review findings and status

A code review of the first version found the issues below. Each one lists what was wrong, what changed, and
the tests that cover it. Status as of 2026-10-04: **21 of 22 fixed, 1 partly fixed** (partner login, 5a).

## 1. Bugs that gave wrong answers

| # | Problem | Fix | Tests |
|---|---|---|---|
| 1a | Eligibility came out as ₹0 for New Car, Working Capital, LRD, Commercial Vehicle, Education and others, because only `net_salary` / `itr_income` / `net_profit` were read. The ₹0 pushed the LLM toward "Not Suitable". | `INCOME_SOURCES` picks the income field for each loan type and converts annual figures to monthly. LRD is rent-based, Working Capital is 20% of the latest turnover, and Construction Finance is marked "not computed". Missing income gives `None` ("not computed"), never 0. [eligibility.py](app/rag/eligibility.py) | `test_eligibility_uses_the_right_income_field_per_loan_type`, `test_eligibility_is_none_not_zero_when_income_missing`, `test_annual_income_is_converted_to_monthly` |
| 1b | Owner, co-applicant, tenant, employer, business and builder names were sent to the LLM; only name and mobile were removed. | `llm_safe_case()` is an allowlist: known questionnaire keys minus `PERSONAL_KEYS`. Every LLM prompt uses it. [questionnaire.py](app/rag/questionnaire.py) | `test_llm_safe_case_removes_personal_and_unknown_fields`, `test_structured_path_llm_only_explains_and_never_sees_pii` |
| 1c | After a query rewrite, the same web search ran twice. | The rewrite now happens before the web step, and the web is searched at most once. | `test_weak_kb_rewrites_once_then_web_gives_general_guidance_only` |
| 1d | The LLM's "None" lender showed as a heading, "None: …". | "None", "null" and "n/a" are converted to `null` on the server; the UI only shows a heading for a real lender. | `test_text_path_none_lender_becomes_null` |
| 1e | Untagged PDF/DOCX uploads were dropped whenever a Markdown chunk matched, and long sections could separate the Banker/RM line from its program. | Uploads can be tagged with lender and loan type. Search filters by loan type and keeps untagged documents (LLM-graded). Every chunk of a long section repeats the lender and program header and the contact line. | `test_long_markdown_section_keeps_contact_line_in_every_chunk`, `test_uploader_tags_apply_to_untagged_documents` |

## 2. Design: rules decide, the LLM explains

| Recommendation | Status |
|---|---|
| Store lender programs as structured data | **Done.** `data/policies/*.yaml`, validated by the `Program` schema in [policies.py](app/rag/policies.py), can be uploaded through `/api/ingest`. |
| Score every program in Python and rank them, using the lender's own FOIR and ROI | **Done.** [rules.py](app/rag/rules.py) runs about 20 checks (pass / fail / deviation / unknown / warn) and ranks by status, number of fails, open items, then indicative maximum. |
| LLM only writes "why suitable" and next steps | **Done.** `explain` receives the rules results. Status, lender, contacts and the list of concerns come from the rules engine. Without an LLM, a template explanation is used. |
| Comparison table: each lender a row, each criterion ✓ / ✗ / ! | **Done.** In the result view; each row expands to show its checks. |
| Check loan-type match in code instead of an LLM grader | **Done.** `grade_kb` calls the LLM only for untagged documents. |
| Web fallback gives general information only | **Done.** `web_guidance` lists typical criteria and never names a lender. |

## 3. Chatbot experience

| Recommendation | Status |
|---|---|
| Free-text intake | **Done.** `/api/intake` and the "Describe the case" box. The mobile number is redacted before the LLM, names are discarded, every value goes through the same validation as a typed answer, and typed answers are never overwritten. |
| Critical questions first, Skip for optional ones | **Done.** Critical questions can't be skipped (the server enforces it). `overdue`, `settled`, `possession`, `legal_cat` and `salary_mode` are now critical where the rules need them. |
| Follow-up chat | **Done.** `/api/chat`, grounded on the structured rules for the loan type and the result. Contacts are excluded from the chat context. |
| Stream progress | **Done.** `/api/recommend/stream` (server-sent events); the UI shows each step live. |

## 4. Validation and model

| Recommendation | Status |
|---|---|
| Answer types and validation | **Done.** `kind_of()` / `validate()`: money, money list, PIN, mobile, CIBIL 300–900 or Not Checked, age, year, years, count, percent, options. The money parser handles lakh/lac/L, cr, k and LPA as whole words ("place" is no longer read as lakh). |
| Larger model, retries, timeouts | **Done.** `GROQ_MODEL_SMART` (default `openai/gpt-oss-120b`) for explanations, text-only recommendations and chat; `GROQ_MODEL` for extraction and grading. `LLM_TIMEOUT_S`, client retries, and one retry when the JSON output is invalid. |

## 5. Security and operations

| # | Recommendation | Status |
|---|---|---|
| 5a | Saved cases readable and overwritable by ID | **Partly fixed.** Each case now has a random access token (only its hash is stored); loading or updating needs it. **User accounts are now built**: login, private per-user chats and an admin console (see README). The legacy case-link endpoints stay public for the `/classic` page. |
| 5b | Rate-limit the LLM endpoints | **Done.** Per-IP limit on recommend, stream, intake and chat (`RATE_LIMIT_PER_MIN`). It is in-process; use a shared limiter (for example Redis) if you run several workers. |
| 5c | Ingestion: dedupe, versioning, delete, persistence, stale warning | **Done.** Content-hash dedupe (409), stable chunk IDs, `GET /api/policies`, `DELETE /api/policies/doc/{id}`, uploads reloaded at startup in in-memory mode. Policies older than `POLICY_STALE_DAYS` show a warning. Old versions are not kept: re-uploading a lender's YAML replaces it, so keep `data/policies/` in git. |
| 5d | Docker | **Done.** Model downloaded at build time, KB warmed at startup, non-root user, SQLite and uploads on the `/srv/var` volume. |

## 6. Measuring quality

**Done.** [evals/golden_cases.yaml](evals/golden_cases.yaml) has 30 cases with expected status and lender.
`tests/test_golden_cases.py` runs them through the full graph without an LLM, so they are deterministic and
run in CI. `python -m evals.run_evals` runs them with your real configuration and prints accuracy. The cases
target the sample policies: rewrite them with the credit team once real policies are loaded, and grow the
set toward 50.

## Still open

1. ~~Partner login and case ownership (5a).~~ Done: user accounts, private chats and the admin console.
   Password reset by email is not built (admins reset passwords in the console).
2. Real lender policies, real `ASSUMPTIONS`, and golden cases from the credit team.
3. Tavily web search has been confirmed working with a real key (2026-10-07). Real Groq, Pinecone and
   HuggingFace calls have not been run in this environment; check them with `python -m evals.run_evals`
   after adding keys.
4. The React frontend (`frontend/`) was checked in headless Edge: resuming a case, recommendation, comparison
   table, validation error, phone width (no sideways scroll) and the admin page all work, with no console
   errors. Follow-up chat and free-text intake were only checked in their no-key state.
