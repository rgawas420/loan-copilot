"""Index the Urban Money folder into the knowledge base (the app also does this at startup).

    python sync_urban_money.py              # incremental: new / changed files indexed, deleted files removed
    python sync_urban_money.py --full       # re-index everything (e.g. after clearing the Pinecone namespace)
    python sync_urban_money.py --dry-run    # extract + chunk only (fills the OCR cache), no vector writes
"""
import argparse
import json
from collections import Counter

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services import knowledge_base as kb


def dry_run():
    root = kb.kb_root()
    if root is None:
        print("Urban Money folder not found. Set URBAN_MONEY_DIR.")
        return
    totals, failed, skipped = Counter(), [], []
    for n, path in enumerate(kb.discover(root), 1):
        rel = kb._rel(path, root)
        if path.suffix.lower() not in kb.ALLOWED_EXTENSIONS:
            skipped.append(rel)
            continue
        try:
            docs = kb.chunk_file(path, root, kb._hash_file(path))
            totals[path.suffix.lower()] += len(docs)
            print(f"{n:4d} {len(docs):7d} chunks  {rel}", flush=True)
        except Exception as e:
            failed.append(f"{rel}: {type(e).__name__}: {e}")
            print(f"{n:4d}  FAILED        {rel}: {e}", flush=True)
    print("\nChunks by type:", dict(totals), "\nTotal chunks:", sum(totals.values()))
    print("Skipped (unsupported):", skipped)
    print("Failed:", json.dumps(failed, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="re-index every file")
    ap.add_argument("--dry-run", action="store_true", help="extract and chunk only")
    args = ap.parse_args()
    setup_logging()
    if args.dry_run or not get_settings().pinecone_api_key:
        if not args.dry_run:
            print("PINECONE_API_KEY is empty: the app indexes the folder in memory at startup. "
                  "Doing a dry run to fill the extraction (OCR) cache so startup is faster.\n")
        dry_run()
    else:
        print(json.dumps(kb._summary(kb.sync(full=args.full)), indent=1))
