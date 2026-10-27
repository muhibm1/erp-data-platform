"""Ask a model the question set, then grade the answers against SQL.

  python eval/run.py ask --split test --provider claude-code   # claude -p, on a Claude subscription
  python eval/run.py ask --split test --provider stub          # answers 0 / [] (the floor)
  python eval/run.py grade --split test --answers eval/runs/<file>.json

Grading is code: an amount is right within one cent; a list of invoices is
right only when it is exactly the right set. No model grades another model.
"""

import argparse
import collections
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from build import prompt

HERE = Path(__file__).parent


def load(split):
    return [json.loads(line) for line in open(HERE / "data" / f"{split}.jsonl")]


def ask_claude_code(text: str, model: str) -> str:
    """One question through `claude -p` with every tool turned off."""
    out = subprocess.run(["claude", "-p", "--model", model, "--tools", "", text],
                         capture_output=True, text=True, timeout=300)
    return out.stdout


def parse(raw: str):
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        return json.loads(m.group(0))["answer"] if m else None
    except (json.JSONDecodeError, KeyError):
        return None


def correct(row, got) -> bool:
    if row["answer_type"] == "money":
        try:
            return abs(float(got) - float(row["answer"])) <= 0.01
        except (TypeError, ValueError):
            return False
    return isinstance(got, list) and sorted(map(str, got)) == sorted(row["answer"])


def grade(split, answers: dict) -> dict:
    rows = load(split)
    by = collections.defaultdict(lambda: [0, 0])
    misses = []
    for r in rows:
        ok = correct(r, answers.get(r["id"]))
        for key in ["all", f"template:{r['id'].rsplit('-', 1)[0]}", *(f"messy:{t}" for t in r["messy"])]:
            by[key][0] += ok
            by[key][1] += 1
        if not ok:
            misses.append({"id": r["id"], "expected": r["answer"], "got": answers.get(r["id"])})
    return {"split": split, "scores": {k: f"{a}/{n}" for k, (a, n) in sorted(by.items())}, "misses": misses}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("--split", default="test")
    a.add_argument("--provider", choices=["claude-code", "stub"], default="stub")
    a.add_argument("--model", default="sonnet")
    g = sub.add_parser("grade")
    g.add_argument("--split", default="test")
    g.add_argument("--answers", required=True)
    args = p.parse_args()
    if args.cmd == "ask":
        answers = {}
        for r in load(args.split):
            if args.provider == "stub":
                answers[r["id"]] = [] if r["answer_type"] == "set" else 0
            else:
                answers[r["id"]] = parse(ask_claude_code(prompt(r), args.model))
        (HERE / "runs").mkdir(exist_ok=True)
        out = HERE / "runs" / f"{args.split}-{args.provider}-{dt.datetime.now():%Y%m%dT%H%M%S}.json"
        out.write_text(json.dumps(answers, indent=1))
        print(out)
    else:
        print(json.dumps(grade(args.split, json.loads(Path(args.answers).read_text())), indent=2))


if __name__ == "__main__":
    main()
