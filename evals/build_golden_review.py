"""Build the review workbook for the credit team: evals/golden_review.xlsx.

    python -m evals.build_golden_review

One row per candidate golden question: the 41 seed cases (evals/rag_golden_seed.yaml), the drafted candidates
(evals/golden_candidates.yaml) and real partner questions from the answer log (rag_traces), each with the app's
latest answer to help the reviewer. The team writes the reference answer and approves each row; then
python -m evals.import_golden_review loads the approved rows into the golden tests.
"""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from app.core.config import get_settings
from evals.import_golden_review import COLUMNS, SHEET

HERE = Path(__file__).parent
OUT = HERE / "golden_review.xlsx"
# Chat-log questions that are not knowledge-base tests: small talk, needs an attachment or a recommendation on screen,
# or follows an answer that is not recorded with it.
SKIP_LOG = re.compile(r"^(hi|hello|thanks?|ok|sachin agarwal.*|more con\w*cts|rm list|give me rm list|give me summery.*|"
                      r"summari[sz]e (this|the) docs?|why not the other lenders\?|please suggest nbfc.*)$", re.I)
ANSWER_CHARS = 1200

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
HEAD = Font(name="Arial", size=10, bold=True, color="FFFFFF")
GUESS = Font(name="Arial", size=10, color="0000FF", italic=True)      # pre-filled guesses: blue italic
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")                   # cells the reviewer fills in
READ_FILL = PatternFill("solid", fgColor="F2F2F2")                    # app output, for reference only
THIN = Border(*(Side(style="thin", color="D9D9D9"),) * 4)
WRAP = Alignment(wrap_text=True, vertical="top")

HEADERS = ["ID", "Category", "Question", "Earlier questions (follow-ups)", "Loan type", "Origin",
           "Current app answer (not checked)", "Should the app answer?", "Reference answer", "Must contain",
           "Expected document", "Expected page", "Reviewed by", "Approved?", "Reviewer notes"]
WIDTHS = [8, 13, 42, 28, 15, 11, 60, 16, 60, 18, 30, 9, 15, 11, 30]
INPUTS = {"Should the app answer?", "Reference answer", "Must contain", "Expected document", "Expected page",
          "Reviewed by", "Approved?", "Reviewer notes"}
assert set(COLUMNS) <= set(HEADERS)


def _key(q: str) -> str:
    return " ".join(re.sub(r"[^\w₹%]+", " ", q.lower().replace("**", "")).split())


def _log(db: Path):
    """Latest answer per question, and the real partner questions (chat, knowledge-base mode), oldest first."""
    answers, asked = {}, {}
    if not db.exists():
        return answers, []
    with closing(sqlite3.connect(db)) as c:
        for q, origin, mode, data in c.execute("SELECT question, origin, mode, data FROM rag_traces ORDER BY ts"):
            k = _key(q)
            ans = (json.loads(data).get("answer") or "").strip()
            if ans:
                answers[k] = ans
            if origin == "chat" and mode == "kb" and not SKIP_LOG.match(q.strip()):
                asked.setdefault(k, q.strip())
    return answers, list(asked.values())


def rows():
    seeds = yaml.safe_load((HERE / "rag_golden_seed.yaml").read_text(encoding="utf-8"))["cases"]
    drafts = yaml.safe_load((HERE / "golden_candidates.yaml").read_text(encoding="utf-8"))["cases"]
    answers, asked = _log(get_settings().audit_db)
    out, seen = [], set()

    def add(origin, c, guess_doc=False):
        k = _key(c["question"])
        # questions differing only in a typo or punctuation count once ("prime miniter" / "prime minister")
        if k in seen or any(len(k) > 15 and _close(k, s) for s in seen):
            return
        seen.add(k)
        nothing = (c.get("expected_answer") or "") == "(not found)" or c.get("should_answer") == "Not found"
        out.append({"Category": c.get("category") or "chat log", "Question": c["question"],
                    "Earlier questions (follow-ups)": c.get("history") or "", "Loan type": c.get("loan_type") or "",
                    "Origin": origin, "Current app answer (not checked)": _clip(answers.get(k, "")),
                    "Should the app answer?": "Not found" if nothing else "",
                    "Must contain": "" if nothing else c.get("expected_answer") or "",
                    "Expected document": c.get("expected_document") or c.get("likely_document") or "",
                    "_guess_doc": guess_doc})

    for c in seeds:
        add("seed", c)
    for c in drafts:
        add("drafted", c, guess_doc=True)
    for q in asked:
        add("chat log", {"question": q})
    for n, r in enumerate(out, 1):
        r["ID"] = f"G{n:03d}"
    return out


def _close(a: str, b: str) -> bool:
    """Same question with a small typo: equal length within 2 and at most 2 differing words."""
    wa, wb = a.split(), b.split()
    return len(wa) == len(wb) and sum(x != y for x, y in zip(wa, wb)) <= 1 and abs(len(a) - len(b)) <= 2


def _clip(s: str) -> str:
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s if len(s) <= ANSWER_CHARS else s[:ANSWER_CHARS].rsplit(" ", 1)[0] + " …(cut; full answer in Admin > RAG quality)"


def _instructions(ws, n_rows: int):
    ws.title = "How to review"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 100
    lines = [
        ("Golden questions: review sheet", None),
        ("", None),
        ("What this is", "These questions test the Loan Copilot's answers. Each approved row becomes an automatic test "
         "that runs whenever the documents, the model or the prompt change. Your reference answer is what the app "
         "is checked against, so it must be correct and current."),
        ("What to do", f"Go through the {n_rows} rows on the '{SHEET}' sheet. Fill in the yellow columns only. "
         "Grey columns are information. Blue italic text is a guess from Claude: confirm it or correct it."),
        ("", None),
        ("Column", "How to fill it"),
        ("Should the app answer?", "Answer: the documents contain the answer. Not found: they don't, and the app must say "
         "so rather than guess. Ask for details: the question is too vague, and the app should ask what it needs."),
        ("Reference answer", "The correct answer as you would give it to a partner, from the current documents. Include "
         "every key figure, condition and contact. Leave empty for 'Not found'. For 'Ask for details', write what "
         "the app should ask."),
        ("Must contain", "Optional. One exact value the answer must include (a phone number, code, % or amount), copied "
         "exactly as the document writes it."),
        ("Expected document", "The document the answer should come from (title or file name). Pre-filled guesses are blue."),
        ("Expected page", "Optional. Page number, for PDFs."),
        ("Reviewed by", "Your name. Required for the row to be imported."),
        ("Approved?", "Yes: import this row as a test. No: drop the question. Needs change: you edited the question "
         "itself, so check it again."),
        ("Reviewer notes", "Anything useful: where the answer differs between documents, when it expires, etc."),
        ("Current app answer", "What the app last answered (not checked). If it is right, you can copy it into the "
         "Reference answer. Do not trust it without checking the document."),
        ("", None),
        ("Example row", None),
        ("Question", "Who is the home loan RM for Bajaj Finserv in Pune?"),
        ("Should the app answer?", "Answer"),
        ("Reference answer", "Chetan Rathod, 9604454809 (Pan India RM list, August 2026)."),
        ("Must contain", "9604454809"),
        ("Expected document", "Pan India RM list Aug"),
        ("Reviewed by / Approved?", "(your name) / Yes"),
        ("", None),
        ("When done", "Send the file back. It is loaded with:  python -m evals.import_golden_review"),
    ]
    for i, (a, b) in enumerate(lines, 1):
        ws.cell(i, 1, a).font = BOLD if b is not None or i == 1 else FONT
        if b is not None:
            ws.cell(i, 2, b).font = BOLD if a == "Column" else FONT
        ws.cell(i, 1).alignment = ws.cell(i, 2).alignment = WRAP
    ws["A1"].font = Font(name="Arial", size=14, bold=True)
    for r in (6, 17):
        ws.cell(r, 1).font = ws.cell(r, 2).font = BOLD


def _summary(wb, n: int, cats):
    ws = wb.create_sheet("Progress")
    ws.sheet_view.showGridLines = False
    q = f"'{SHEET}'!"
    last = n + 1
    hdr = ["Category", "Questions", "Reviewed", "Approved", "Not found expected", "Approved %"]
    for j, h in enumerate(hdr, 1):
        c = ws.cell(1, j, h)
        c.font, c.fill, c.alignment = HEAD, HEAD_FILL, WRAP
    col = {h: get_column_letter(HEADERS.index(h) + 1) for h in ("Category", "Reviewed by", "Approved?", "Should the app answer?")}
    rng = lambda h: f"{q}${col[h]}$2:${col[h]}${last}"   # noqa: E731
    for i, cat in enumerate(cats, 2):
        ws.cell(i, 1, cat)
        ws.cell(i, 2, f'=COUNTIF({rng("Category")},A{i})')
        ws.cell(i, 3, f'=COUNTIFS({rng("Category")},A{i},{rng("Reviewed by")},"<>")')
        ws.cell(i, 4, f'=COUNTIFS({rng("Category")},A{i},{rng("Approved?")},"Yes")')
        ws.cell(i, 5, f'=COUNTIFS({rng("Category")},A{i},{rng("Should the app answer?")},"Not found")')
        ws.cell(i, 6, f"=IF(B{i}=0,0,D{i}/B{i})")
    t = len(cats) + 2
    ws.cell(t, 1, "Total").font = BOLD
    for j in range(2, 6):
        L = get_column_letter(j)
        ws.cell(t, j, f"=SUM({L}2:{L}{t - 1})").font = BOLD
    ws.cell(t, 6, f"=IF(B{t}=0,0,D{t}/B{t})").font = BOLD
    for r in ws.iter_rows(min_row=2, max_row=t):
        for c in r:
            c.font = BOLD if c.row == t else FONT
            c.border = THIN
    for r in range(2, t + 1):
        ws.cell(r, 6).number_format = "0%"
    for j, w in enumerate([18, 11, 11, 11, 13, 11], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.cell(t + 2, 1, "Counts come from the Questions sheet and update as rows are filled in.").font = Font(name="Arial", size=9, italic=True)


def build(path: Path = OUT) -> int:
    data = rows()
    wb = Workbook()
    _instructions(wb.active, len(data))
    ws = wb.create_sheet(SHEET)
    for j, h in enumerate(HEADERS, 1):
        c = ws.cell(1, j, h)
        c.font, c.fill, c.alignment, c.border = HEAD, HEAD_FILL, WRAP, THIN
        ws.column_dimensions[get_column_letter(j)].width = WIDTHS[j - 1]
    for i, r in enumerate(data, 2):
        for j, h in enumerate(HEADERS, 1):
            c = ws.cell(i, j, r.get(h) or None)
            c.font, c.alignment, c.border = FONT, WRAP, THIN
            if h in INPUTS:
                c.fill = INPUT_FILL
            elif h == "Current app answer (not checked)":
                c.fill = READ_FILL
        if r["_guess_doc"] and r.get("Expected document"):
            ws.cell(i, HEADERS.index("Expected document") + 1).font = GUESS
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{len(data) + 1}"
    ws.row_dimensions[1].height = 30
    last = len(data) + 1
    for header, options in (("Should the app answer?", "Answer,Not found,Ask for details"),
                            ("Approved?", "Yes,No,Needs change")):
        L = get_column_letter(HEADERS.index(header) + 1)
        dv = DataValidation(type="list", formula1=f'"{options}"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{L}2:{L}{last}")
    page = get_column_letter(HEADERS.index("Expected page") + 1)
    dv = DataValidation(type="whole", operator="greaterThan", formula1="0", allow_blank=True,
                        error="Enter a page number.", showErrorMessage=True)
    ws.add_data_validation(dv)
    dv.add(f"{page}2:{page}{last}")
    ws.cell(1, HEADERS.index("Current app answer (not checked)") + 1).comment = Comment(
        "The app's latest answer from the answer log, not checked. Use it as a starting point only.", "Claude")
    ws.cell(1, HEADERS.index("Expected document") + 1).comment = Comment(
        "Blue italic = guessed from the document titles in the knowledge base. Confirm or correct.", "Claude")
    _summary(wb, len(data), list(dict.fromkeys(r["Category"] for r in data)))
    wb.active = 1
    wb.calculation.fullCalcOnLoad = True          # the Progress counts are formulas: compute them when opened
    wb.save(path)
    return len(data)


if __name__ == "__main__":
    n = build()
    print(f"Wrote {OUT} with {n} questions.")
