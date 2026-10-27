"""Grade an agent that answered the question set through the ask-the-warehouse tools.

The agent gets only the question text, never the records or the answers, and
reaches the data only through `edp tool`. Each answer comes with the source
references it rests on. This scores two things, both by code:

- correct: the answer matches the SQL answer (the same rule as run.py);
- grounded: every cited reference is a real, current Odoo record version, and
  an answer that is not 0 or empty cites at least one.

  python eval/agent.py --split test --answers eval/runs/<file>.json
"""

import argparse
import collections
import json
import os
from pathlib import Path

import psycopg

from run import correct, load

ROOT = Path(__file__).resolve().parent.parent


def grade(split: str, answers: dict, dsn: str) -> dict:
    import sys
    sys.path.insert(0, str(ROOT))
    from edp.ask import check_citations

    by = collections.defaultdict(lambda: [0, 0, 0])
    problems = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        for r in load(split):
            got = answers.get(r["id"]) or {}
            ok = correct(r, got.get("answer"))
            refs = got.get("sources") or []
            checked = check_citations(conn, refs) if refs else {"all_ok": True, "results": []}
            empty = got.get("answer") in (0, 0.0, [], None)
            grounded = checked["all_ok"] and (bool(refs) or empty)
            for key in ["all", f"template:{r['id'].rsplit('-', 1)[0]}", *(f"messy:{t}" for t in r["messy"])]:
                by[key][0] += ok
                by[key][1] += grounded
                by[key][2] += 1
            if not (ok and grounded):
                problems.append({"id": r["id"], "expected": r["answer"], "got": got.get("answer"),
                                 "bad_refs": [c for c in checked["results"] if c["status"] != "ok"],
                                 "cited": len(refs)})
    return {"split": split,
            "scores": {k: {"correct": f"{a}/{n}", "grounded": f"{g}/{n}"} for k, (a, g, n) in sorted(by.items())},
            "citations": sum(len((answers.get(i) or {}).get("sources") or []) for i in answers),
            "problems": problems}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--split", default="test")
    p.add_argument("--answers", required=True)
    p.add_argument("--dsn", default=os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo"))
    a = p.parse_args()
    print(json.dumps(grade(a.split, json.loads(Path(a.answers).read_text()), a.dsn), indent=2, default=str))
