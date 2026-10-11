"""Release gate: does the current answer configuration (model, prompt, retrieval, ...) do at least as well as the
approved baseline on the golden tests? Used by deploy/upload.sh before shipping.

    python -m evals.gate                     # the current configuration's latest finished run vs the baseline
    python -m evals.gate --run 9             # a given run vs the baseline
    python -m evals.gate --set-baseline 9    # approve run 9 as the baseline

Exit code: 0 passed (or the run is the baseline), 1 failed, 2 not decidable (no baseline, no finished run for the
current configuration, or the run did not finish).
"""
import sys

PASSING = ("PASS", "BASELINE")


def report(g: dict) -> str:
    where = " vs ".join(x for x in (f"run {g['run_id']}" if g.get("run_id") else "",
                                    f"baseline run {g['baseline_id']}" if g.get("baseline_id") else "") if x)
    lines = [f"Release gate: {g['verdict']}" + (f"  ({where})" if where else "")]
    if g.get("compared") is not None:
        lines.append(f"  compared {g['compared']} golden test(s)" + (f"; {g['new_cases']} new" if g.get("new_cases") else ""))
    for k, (b, n) in (g.get("deltas") or {}).items():
        lines.append(f"  {k:<20} {b if b is not None else '-':>6} -> {n if n is not None else '-'}")
    lines += [f"  - {r}" for r in g.get("reasons", [])]
    lines += [f"  regressed: [{c['category'] or '-'}] {c['question'][:90]}" for c in g.get("regressions", [])]
    lines += [f"  fixed:     [{c['category'] or '-'}] {c['question'][:90]}" for c in g.get("fixed", [])]
    return "\n".join(lines)


def main(argv=None) -> int:
    from app.services import rag_debug
    argv = list(sys.argv[1:] if argv is None else argv)
    rag_debug.init_db()
    if "--set-baseline" in argv:
        rid = int(argv[argv.index("--set-baseline") + 1])
        rag_debug.set_baseline(rid)
        print(f"Run {rid} is now the baseline.")
        return 0
    if "--run" in argv:
        run = rag_debug.run_detail(int(argv[argv.index("--run") + 1]))
        if run is None:
            print("Run not found.")
            return 2
        g = {**run["gate"], "run_id": run["id"]}
    else:
        g = rag_debug.gate_current()
    print(report(g))
    return 0 if g["verdict"] in PASSING else 1 if g["verdict"] == "FAIL" else 2


if __name__ == "__main__":
    sys.exit(main())
