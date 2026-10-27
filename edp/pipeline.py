"""One scheduled run: load, build, check, record. Safe to start twice.

`edp run` takes a Postgres advisory lock first, so a second run started while
one is going (a cron overlap, a person running it by hand) exits at once and
says so, instead of loading the same records twice in parallel. Then:

1. extract from Odoo (the client retries a dropped connection with backoff);
2. dbt build: models and every check;
3. monitors: last run, freshness, volume, schema.

Each run is recorded in raw.pipeline_runs with what each step returned. The
exit code is 0 when everything passed, 1 when a step failed or a monitor
failed, and 3 when another run held the lock.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

from .load import ensure_schema, extract, new_run_id
from .monitor import render, run_checks

LOCK_KEY = 4_202_610  # any constant; one per warehouse
ROOT = Path(__file__).resolve().parent.parent

DDL = """
create table if not exists raw.pipeline_runs (
    run_id text primary key, started_at timestamptz not null, finished_at timestamptz,
    status text not null, steps jsonb not null default '{}'::jsonb);
"""


def dbt_build() -> tuple[bool, str]:
    dbt = os.path.join(os.path.dirname(sys.executable), "dbt")
    out = subprocess.run([dbt, "build", "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(ROOT / "dbt")],
                         capture_output=True, text=True)
    summary = next((line.split("Done. ")[-1] for line in reversed(out.stdout.splitlines()) if "Done." in line),
                   out.stdout[-300:])
    return out.returncode == 0, summary.strip()


def run_once(conn: psycopg.Connection, odoo_factory, since: dt.date | None = None, full: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute("select pg_try_advisory_lock(%s)", (LOCK_KEY,))
        if not cur.fetchone()[0]:
            print("another run holds the lock; nothing to do")
            return 3
    try:
        ensure_schema(conn)
        conn.execute(DDL)
        run_id = new_run_id()
        conn.execute("insert into raw.pipeline_runs (run_id, started_at, status) values (%s, now(), 'running')",
                     (run_id,))
        conn.commit()
        steps: dict = {}
        status = "ok"
        try:
            steps["extract"] = extract(odoo_factory(), conn, full=full, since=since, run_id=run_id)
        except Exception as exc:  # recorded, then the monitors still run and report it
            steps["extract"] = {"error": f"{type(exc).__name__}: {exc}"}
            status = "failed"
        if status == "ok":
            built, summary = dbt_build()
            steps["dbt"] = {"ok": built, "summary": summary}
            status = "ok" if built else "failed"
        results = run_checks(conn)
        steps["monitors"] = [{"check": r.check_name, "status": r.status, "message": r.message} for r in results]
        if any(r.status == "fail" for r in results):
            status = "failed"
        conn.execute("update raw.pipeline_runs set finished_at = now(), status = %s, steps = %s where run_id = %s",
                     (status, json.dumps(steps, default=str), run_id))
        conn.commit()
        print(json.dumps({"run_id": run_id, "status": status, **{k: v for k, v in steps.items() if k != "monitors"}},
                         default=str))
        print(render(results))
        return 0 if status == "ok" else 1
    finally:
        conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.commit()


def run_forever(dsn: str, odoo_factory, every: int) -> None:
    """A simple scheduler for a single machine. Cron or a workflow scheduler does the same job."""
    while True:
        with psycopg.connect(dsn) as conn:
            run_once(conn, odoo_factory)
        time.sleep(every)
