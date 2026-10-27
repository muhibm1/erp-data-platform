"""Monitors: is the data fresh, complete and shaped the way the models expect?

Run after every load. Each check returns ok, warn or fail with the evidence.
Results are stored in raw.monitor_results so a person can see the history,
appended to an alerts file, and posted to a webhook when one is configured.
A fail makes `edp monitor` exit non-zero, so a scheduler can page on it.

Checks:
- last_run      the newest load finished, and did not fail
- freshness     the newest successful load is younger than the limit
- volume        no model lost more than a set share of its records since the last load
- schema        no field the models read disappeared from Odoo; new fields are noted
"""

import datetime as dt
import json
import os
import urllib.request
from dataclasses import asdict, dataclass

import psycopg

from .sources import SOURCES

DDL = """
create table if not exists raw.monitor_results (
    checked_at timestamptz not null default now(), run_id text, check_name text not null,
    model text, status text not null, detail jsonb);
"""

FRESHNESS_LIMIT = dt.timedelta(hours=float(os.environ.get("EDP_FRESHNESS_HOURS", "2")))
VOLUME_DROP_LIMIT = float(os.environ.get("EDP_VOLUME_DROP", "0.2"))


@dataclass
class Result:
    check_name: str
    status: str  # ok | warn | fail
    message: str
    model: str | None = None
    run_id: str | None = None


def _runs(cur, ok_only=False, limit=2):
    cur.execute("select run_id, started_at, finished_at, status, detail from raw.load_runs "
                + ("where status = 'ok' " if ok_only else "")
                + "order by started_at desc limit %s", (limit,))
    return cur.fetchall()


def check_last_run(cur) -> list[Result]:
    runs = _runs(cur)
    if not runs:
        return [Result("last_run", "fail", "no load has ever run")]
    run_id, started, finished, status, detail = runs[0]
    if status == "failed":
        return [Result("last_run", "fail", f"load {run_id} failed: {(detail or {}).get('error')}", run_id=run_id)]
    if status == "running" and dt.datetime.now(dt.timezone.utc) - started > dt.timedelta(hours=1):
        return [Result("last_run", "fail", f"load {run_id} has been running since {started:%Y-%m-%d %H:%M} UTC",
                       run_id=run_id)]
    return [Result("last_run", "ok", f"load {run_id} {status}", run_id=run_id)]


def check_freshness(cur, now=None) -> list[Result]:
    now = now or dt.datetime.now(dt.timezone.utc)
    ok = _runs(cur, ok_only=True, limit=1)
    if not ok:
        return [Result("freshness", "fail", "no successful load yet")]
    run_id, _, finished, _, _ = ok[0]
    age = now - finished
    status = "ok" if age <= FRESHNESS_LIMIT else "fail"
    return [Result("freshness", status,
                   f"data through {finished:%Y-%m-%d %H:%M} UTC ({age.total_seconds() / 3600:.1f} h old, "
                   f"limit {FRESHNESS_LIMIT.total_seconds() / 3600:g} h)", run_id=run_id)]


def check_volume(cur) -> list[Result]:
    ok = _runs(cur, ok_only=True, limit=2)
    if len(ok) < 2 or "live" not in (ok[1][4] or {}):
        return [Result("volume", "ok", "fewer than two loads with counts; nothing to compare")]
    (new_id, *_, new), (_, *_, old) = ok
    out = []
    for model in SOURCES:
        before, after = old["live"].get(model), new["live"].get(model)
        if before is None or after is None:
            continue
        drop = (before - after) / before if before else 0
        if after == 0 and before > 0:
            out.append(Result("volume", "fail", f"{model}: {before} records before, none now", model, new_id))
        elif drop > VOLUME_DROP_LIMIT:
            out.append(Result("volume", "fail", f"{model}: {before} -> {after} records ({drop:.0%} fewer)",
                              model, new_id))
    return out or [Result("volume", "ok", "no model lost more than "
                          f"{VOLUME_DROP_LIMIT:.0%} of its records", run_id=new_id)]


def check_schema(cur) -> list[Result]:
    cur.execute("""
        select run_id, model, fields from raw.source_schemas
        where run_id in (select run_id from raw.source_schemas group by run_id
                         order by max(captured_at) desc limit 2)
        order by captured_at""")
    rows = cur.fetchall()
    runs = list(dict.fromkeys(r[0] for r in rows))
    if len(runs) < 2:
        return [Result("schema", "ok", "fewer than two schema snapshots; nothing to compare")]
    old = {m: set(f) for r, m, f in rows if r == runs[0]}
    new = {m: set(f) for r, m, f in rows if r == runs[1]}
    out = []
    for model, fields in new.items():
        if model not in old:
            continue
        added, removed = sorted(fields - old[model]), sorted(old[model] - fields)
        used = set(SOURCES.get(model, []))
        if removed:
            hit = [f for f in removed if f in used or f.startswith("x_")]
            out.append(Result("schema", "fail" if hit else "warn",
                              f"{model}: fields removed in Odoo: {removed}"
                              + (f" (read by the load: {hit})" if hit else ""), model, runs[1]))
        if added:
            out.append(Result("schema", "warn", f"{model}: new fields in Odoo: {added}", model, runs[1]))
    return out or [Result("schema", "ok", "no field added or removed since the last load", run_id=runs[1])]


def run_checks(conn: psycopg.Connection, now=None) -> list[Result]:
    with conn.cursor() as cur:
        cur.execute(DDL)
        results = [*check_last_run(cur), *check_freshness(cur, now), *check_volume(cur), *check_schema(cur)]
        for r in results:
            cur.execute("insert into raw.monitor_results (run_id, check_name, model, status, detail) "
                        "values (%s, %s, %s, %s, %s)",
                        (r.run_id, r.check_name, r.model, r.status, json.dumps({"message": r.message})))
    conn.commit()
    alert([r for r in results if r.status != "ok"])
    return results


def alert(results: list[Result]) -> None:
    """Appends warnings and failures to EDP_ALERT_FILE, and posts them to EDP_ALERT_WEBHOOK if set."""
    if not results:
        return
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path = os.environ.get("EDP_ALERT_FILE", "alerts.jsonl")
    with open(path, "a") as f:
        for r in results:
            f.write(json.dumps({"at": stamp, **asdict(r)}) + "\n")
    hook = os.environ.get("EDP_ALERT_WEBHOOK")
    if hook:
        text = "\n".join(f"[{r.status.upper()}] {r.check_name}: {r.message}" for r in results)
        req = urllib.request.Request(hook, json.dumps({"text": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()


def render(results: list[Result]) -> str:
    return "\n".join(f"{r.status.upper():<5} {r.check_name:<10} {r.message}" for r in results)
