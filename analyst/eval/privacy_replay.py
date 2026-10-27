"""Replay every query the agent wrote in the ads runs through a privacy rule, with no model.

Prints each query whose result under the current tool differs from its result under the tool
at another commit (by default the scored one, 4a9224a), so a change to the privacy rule can be
checked against everything the agent actually wrote. Needs the ads database
(`python -m analyst.ads.simulate`).

    python -m analyst.eval.privacy_replay [commit]
"""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from analyst import domain, sql

RUNS = Path(__file__).parent / "runs"


def old_tool(commit: str):
    """The analyst's sql module as it was at `commit`."""
    source = subprocess.run(["git", "show", f"{commit}:analyst/sql.py"], capture_output=True, text=True,
                            check=True, cwd=Path(__file__).parent).stdout
    path = Path(tempfile.mkdtemp()) / "sql_then.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("sql_then", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    commit = sys.argv[1] if len(sys.argv) > 1 else "4a9224a"
    then = old_tool(commit)
    dom = domain.load("ads")
    conn = dom.connect()
    domain.read_columns(dom, conn)
    sql.prepare(dom, conn)
    old_dom = copy.copy(dom)
    old_dom.privacy = {"min_count": 50, "count_columns": ["installs"]}  # the rule at the scored commit
    seen, changed = set(), 0
    for path in sorted(RUNS.glob("ads-*.json")):
        for record in json.loads(path.read_text())["records"]:
            for attempt in record.get("attempts") or []:
                query = attempt.get("sql")
                if not query or query in seen:
                    continue
                seen.add(query)
                before, after = then.run(old_dom, conn, query), sql.run(dom, conn, query)
                same = json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True).replace(
                    "installs/taps", "installs")
                if not same:
                    changed += 1
                    print(f"{path.name} {record['id']}\n  then: {json.dumps(before)[:200]}\n  now:  {json.dumps(after)[:200]}")
    print(f"{len(seen)} distinct queries, {changed} with a different result")


if __name__ == "__main__":
    main()
