"""Run the analyst on a question set under one condition, grade it by code, and save every step.

  python -m analyst.eval.run --set dev --condition full
  python -m analyst.eval.run --set hard-test --condition one-shot
  python -m analyst.eval.run compare analyst/eval/runs/a.json analyst/eval/runs/b.json

Each run file keeps, per question: the answer, its grade, every query tried with
its error, the context retrieved, the citations and the time taken. That file is
the trace: a reviewer can see why any question passed or failed without
rerunning anything. Model tokens per call go to a `.usage.jsonl` beside it.
"""

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import json
import math
import time
from pathlib import Path

from .. import domain as domains
from ..graph import ask, build, one_shot
from ..model import ModelError, SubscriptionModel
from ..retrieve import Retriever

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SETS = {
    "dev": ("finance", ROOT / "eval" / "data" / "dev.jsonl"),
    "test": ("finance", ROOT / "eval" / "data" / "test.jsonl"),
    "hard-dev": ("finance", HERE / "data" / "hard-dev.jsonl"),
    "hard-test": ("finance", HERE / "data" / "hard-test.jsonl"),
    "ads-dev": ("ads", HERE / "data" / "ads-dev.jsonl"),
    "ads-test": ("ads", HERE / "data" / "ads-test.jsonl"),
}
CONDITIONS = ("one-shot", "no-context", "full")


def load(name: str) -> tuple[str, list[dict]]:
    dom, path = SETS[name]
    return dom, [json.loads(line) for line in path.open()]


def _num(v):
    if isinstance(v, list) and len(v) == 1:
        v = v[0]
    if isinstance(v, str):
        v = v.replace("$", "").replace(",", "").replace("%", "").strip()
    return float(v)


def correct(row: dict, rec: dict) -> bool:
    kind, want = row["answer_type"], row["answer"]
    status, got = rec.get("status"), rec.get("answer")
    if kind == "none":
        return status in ("not_answerable", "clarify")
    if status != "answer":
        return False
    try:
        if kind in ("money", "number", "count"):
            tol = {"money": 0.01, "number": 0.1, "count": 0}[kind]
            return abs(_num(got) - float(want)) <= tol + 1e-9
        if kind == "set":
            got = got if isinstance(got, list) else [got]
            return sorted(str(x).strip().lower() for x in got) == sorted(str(x).strip().lower() for x in want)
        if kind == "text":
            got = got[0] if isinstance(got, list) and len(got) == 1 else got
            return str(got).strip().lower() == str(want).strip().lower()
    except (TypeError, ValueError):
        return False
    return False


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(100 * (c - r) / d, 1), round(100 * (c + r) / d, 1))


def kind_of(row: dict) -> str:
    return row["id"].rsplit("-", 1)[0] if row["id"][-1].isdigit() else ("not answerable" if row["answer_type"] == "none"
                                                                        else "answerable")


def run_one(dom, model, condition, retriever, row) -> dict:
    t = time.time()
    try:
        if condition == "one-shot":
            out = one_shot(dom, model, row["question"])
            out["attempts"] = [{"sql": out.get("sql"), "error": out.get("explanation") or None}]
        else:
            app = build(dom, model, context=condition == "full", retriever=retriever)
            out = ask(app, row["question"], thread=row["id"])
    except ModelError as exc:
        out = {"status": "failed", "answer": None, "explanation": str(exc)}
    rec = {"id": row["id"], "status": out.get("status"), "answer": out.get("answer"),
           "explanation": out.get("explanation"), "sql": out.get("sql"),
           "attempts": out.get("attempts", []), "retrieved": out.get("retrieved", []),
           "sources": out.get("sources", []), "citations_ok": (out.get("citations") or {}).get("all_ok"),
           "seconds": round(time.time() - t, 1)}
    rec["correct"] = correct(row, rec)
    rec["expected"] = row["answer"]
    return rec


def summarise(rows: list[dict], records: list[dict]) -> dict:
    by = collections.defaultdict(lambda: [0, 0])
    for row, rec in zip(rows, records):
        for key in ("all", kind_of(row)):
            by[key][0] += rec["correct"]
            by[key][1] += 1
    tries = [len(r["attempts"]) for r in records]
    return {
        "scores": {k: {"correct": f"{a}/{n}", "pct": round(100 * a / n, 1), "ci95": wilson(a, n)}
                   for k, (a, n) in sorted(by.items())},
        "repaired": sum(1 for r in records if r["correct"] and any(a["error"] for a in r["attempts"])),
        "queries_per_question": round(sum(tries) / max(len(tries), 1), 2),
        "median_seconds": sorted(r["seconds"] for r in records)[len(records) // 2] if records else None,
    }


def run(set_name: str, condition: str, model_name: str, out_dir: Path, limit: int | None, parallel: int) -> Path:
    dom_name, rows = load(set_name)
    rows = rows[:limit] if limit else rows
    dom = domains.load(dom_name)
    with dom.connect() as conn:
        domains.read_columns(dom, conn)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{set_name}-{condition}-{model_name}-{stamp}.json"
    model = SubscriptionModel(model_name, usage_log=path.with_suffix(".usage.jsonl"))
    retriever = Retriever(domains.documents(dom)) if condition == "full" else None
    with cf.ThreadPoolExecutor(parallel) as pool:
        records = list(pool.map(lambda r: run_one(dom, model, condition, retriever, r), rows))
    report = {"set": set_name, "condition": condition, "model": model_name, "domain": dom_name,
              "started": stamp, "summary": summarise(rows, records), "records": records}
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=str) + "\n")
    return path


def compare(a_path: str, b_path: str) -> dict:
    """Paired comparison on the same questions: which flipped, and an exact sign test."""
    a = {r["id"]: r for r in json.loads(Path(a_path).read_text())["records"]}
    b = {r["id"]: r for r in json.loads(Path(b_path).read_text())["records"]}
    both = sorted(set(a) & set(b))
    gained = [i for i in both if b[i]["correct"] and not a[i]["correct"]]
    lost = [i for i in both if a[i]["correct"] and not b[i]["correct"]]
    n = len(gained) + len(lost)
    k = min(len(gained), len(lost))
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0
    return {"questions": len(both), "a_correct": sum(a[i]["correct"] for i in both),
            "b_correct": sum(b[i]["correct"] for i in both), "b_gained": gained, "b_lost": lost,
            "sign_test_p": round(p, 4)}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    p.add_argument("--set", choices=sorted(SETS), default="dev")
    p.add_argument("--condition", choices=CONDITIONS, default="full")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--limit", type=int)
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--out", default=str(HERE / "runs"))
    a = p.parse_args()
    if a.cmd == "compare":
        print(json.dumps(compare(a.a, a.b), indent=2))
        return
    path = run(a.set, a.condition, a.model, Path(a.out), a.limit, a.parallel)
    print(path)
    print(json.dumps(json.loads(path.read_text())["summary"], indent=2))


if __name__ == "__main__":
    main()
