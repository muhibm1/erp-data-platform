"""Summarise the scored runs into one table: python -m analyst.eval.report

Takes the newest run file for each set, condition and model, so the numbers in
RESULTS.md can be rebuilt from the files in runs/ without calling a model.
"""

import glob
import json
from pathlib import Path

from .run import compare

RUNS = Path(__file__).parent / "runs"
SCORED = ["test", "hard-test", "ads-test", "explore-test"]
CONDITIONS = ["full-v2", "full", "no-context", "one-shot-read", "one-shot"]


def latest() -> dict:
    found = {}
    for f in sorted(glob.glob(str(RUNS / "*.json"))):
        d = json.loads(Path(f).read_text())
        found[(d["set"], d["condition"], d["model"])] = f
    return found


def tokens(path: str) -> tuple[int, int, int]:
    usage = Path(path).with_suffix(".usage.jsonl")
    rows = [json.loads(line) for line in usage.open()] if usage.exists() else []
    return len(rows), sum(r["input"] for r in rows), sum(r["output"] for r in rows)


def main():
    runs = latest()
    for model in ("sonnet", "haiku"):
        print(f"\n## {model}\n")
        print("| Set | " + " | ".join(CONDITIONS) + " |")
        print("| --- |" + " --- |" * len(CONDITIONS))
        for s in SCORED:
            cells = []
            for c in CONDITIONS:
                f = runs.get((s, c, model))
                if not f:
                    cells.append("")
                    continue
                sc = json.loads(Path(f).read_text())["summary"]["scores"]["all"]
                cells.append(f"{sc['correct']} ({sc['ci95'][0]:.0f}-{sc['ci95'][1]:.0f}%)")
            print(f"| {s} | " + " | ".join(cells) + " |")
        for s in SCORED:
            for best, other in (("full", "no-context"), ("full", "one-shot-read"),
                                ("full-v2", "full"), ("full-v2", "one-shot-read")):
                a, b = runs.get((s, other, model)), runs.get((s, best, model))
                if a and b:
                    r = compare(a, b)
                    print(f"- {s}: {best} vs {other}: {best} gained {r['b_gained']}, lost {r['b_lost']}, "
                          f"sign test p={r['sign_test_p']}")
        for s, c in [(s, c) for s in SCORED for c in ("full", "full-v2")]:
            f = runs.get((s, c, model))
            if f:
                d = json.loads(Path(f).read_text())
                calls, tin, tout = tokens(f)
                n = len(d["records"])
                print(f"- {s} {c}: {calls / n:.1f} model calls, {tin // n} input and {tout // n} output tokens "
                      f"per question; median {d['summary']['median_seconds']} s")


if __name__ == "__main__":
    main()
