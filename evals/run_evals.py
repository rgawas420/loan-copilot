"""Run the golden cases through the full agent and print accuracy.

    python -m evals.run_evals            # uses your .env (real LLM / KB if configured)

Use it before and after changing prompts, models, policies or retrieval. Exit code 1 if any case fails.
"""
import sys
import time
from pathlib import Path

import yaml

CASES_FILE = Path(__file__).with_name("golden_cases.yaml")


def load_cases():
    data = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    for c in data["cases"]:
        case = {**data["bases"][c["base"]], **(c.get("changes") or {})}
        yield c["id"], {k: v for k, v in case.items() if v not in (None, "")}, c["expect"]


def main():
    from app.rag.workflow import run_recommendation
    rows, ok = [], 0
    for cid, case, exp in load_cases():
        t = time.perf_counter()
        r = run_recommendation(case)
        good = r.get("status") == exp["status"] and r.get("top_lender") == exp["lender"]
        ok += good
        rows.append((cid, "PASS" if good else "FAIL", r.get("status"), r.get("top_lender"),
                     f"{time.perf_counter() - t:.1f}s", "" if good else f"expected {exp['status']} / {exp['lender']}"))
    w = max(len(r[0]) for r in rows)
    for row in rows:
        print(f"{row[0]:<{w}}  {row[1]}  {row[2]} / {row[3]}  ({row[4]})  {row[5]}")
    print(f"\n{ok}/{len(rows)} passed")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
