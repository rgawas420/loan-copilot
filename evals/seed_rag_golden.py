"""Load evals/rag_golden_seed.yaml into the golden tests (Admin > RAG quality).

    python -m evals.seed_rag_golden            # add the cases not there yet (same question = already there)
    python -m evals.seed_rag_golden --dry      # only list what would be added

The golden tests then run from the admin console, or automatically when the configuration or documents change
(RAG_EVAL_AUTORUN). Each run calls the real LLM once per case (more when an answer is corrected).
"""
import sys
from pathlib import Path

import yaml

SEED = Path(__file__).with_name("rag_golden_seed.yaml")


def load():
    return yaml.safe_load(SEED.read_text(encoding="utf-8"))["cases"]


def seed(dry: bool = False) -> list:
    from app.services import rag_debug
    rag_debug.init_db()
    have = {g["question"].strip().lower() for g in rag_debug.golden_list()}
    added = []
    for c in load():
        if c["question"].strip().lower() in have:
            continue
        item = {"question": c["question"], "expected_answer": c.get("expected_answer") or "",
                "expected_document": c.get("expected_document") or "", "loan_type": c.get("loan_type") or "",
                "notes": f"seed: {c.get('category', '')}", "category": c.get("category") or ""}
        if not dry:
            rag_debug.golden_save(item)
        added.append(item)
    return added


def main():
    dry = "--dry" in sys.argv
    added = seed(dry)
    for a in added:
        print(f"{'would add' if dry else 'added'}  [{a['notes'][6:]}] {a['question'][:90]}")
    print(f"\n{len(added)} case(s) {'to add' if dry else 'added'}; {len(load()) - len(added)} already present.")


if __name__ == "__main__":
    main()
