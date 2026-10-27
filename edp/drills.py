"""Failure drills: break the pipeline on purpose, then check it fails loudly and recovers.

Each drill does something that happens in production, against the real Odoo
and warehouse, and records what the pipeline did. Run them on a demo stack,
never on a client's systems:

  EDP_DRILL_ODOO_STOP="..." EDP_DRILL_ODOO_START="..." python -m edp.drills

1. odoo_outage      Odoo goes down mid-schedule, then comes back with new work waiting.
2. overlapping_runs Two runs start at once, as when cron fires while a slow run is still going.
3. schema_change    An admin adds a custom field in Odoo, fills it, then deletes it.
4. lost_records     Rows go missing from the warehouse (a bad manual fix, a restore gone wrong).
5. stale_data       Nothing has loaded for three hours.

The report is written to drills/report.json. A drill
passes when the pipeline caught the problem and recovered without data loss.
"""

import datetime as dt
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import psycopg

from . import simulate
from .cli import dsn, odoo
from .monitor import check_freshness, run_checks
from .pipeline import ROOT, dbt_build

PY = sys.executable


def _run(*args, env=None) -> tuple[int, str, float]:
    t0 = time.time()
    p = subprocess.run([PY, "-m", "edp.cli", *args], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, **(env or {})})
    return p.returncode, p.stdout + p.stderr, round(time.time() - t0, 1)


def _sh(cmd: str) -> None:
    subprocess.run(cmd, shell=True, check=True)


def _last_run(conn):
    return conn.execute("select run_id, status, detail from raw.load_runs order by started_at desc limit 1").fetchone()


def odoo_outage(conn) -> dict:
    _sh(os.environ["EDP_DRILL_ODOO_STOP"])
    code_down, out_down, secs_down = _run("run", env={"EDP_ODOO_RETRIES": "2"})
    failed = _last_run(conn)
    _sh(os.environ["EDP_DRILL_ODOO_START"])
    work = simulate.tick(simulate.Company(odoo(), random.Random(7)))
    code_up, out_up, secs_up = _run("run")
    recovered = _last_run(conn)
    caught_up = sum(v for k, v in (recovered[2] or {}).items() if k != "live" and isinstance(v, int))
    return {
        "passed": code_down == 1 and failed[1] == "failed" and code_up == 0 and caught_up > 0,
        "while_down": {"exit": code_down, "seconds": secs_down, "load_status": failed[1],
                       "error": (failed[2] or {}).get("error")},
        "new_work_in_odoo": work,
        "after_restart": {"exit": code_up, "seconds": secs_up, "new_versions_loaded": caught_up},
    }


def overlapping_runs(conn) -> dict:
    procs = [subprocess.Popen([PY, "-m", "edp.cli", "run"], cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) for _ in range(2)]
    codes = sorted(p.wait() for p in procs)
    outs = [p.stdout.read() for p in procs]
    return {"passed": codes == [0, 3], "exit_codes": codes,
            "skipped_run_said": next((o.strip().splitlines()[0] for o in outs if "lock" in o), None)}


def schema_change(conn) -> dict:
    o = odoo()
    model_id = o.execute("ir.model", "search", [("model", "=", "account.move")])[0]
    field = o.execute("ir.model.fields", "create", {
        "name": "x_po_number", "field_description": "Customer PO number", "model_id": model_id,
        "ttype": "char", "state": "manual"})
    inv = o.execute("account.move", "search", [("move_type", "=", "out_invoice"), ("state", "=", "posted")],
                    limit=1, order="id desc")[0]
    o.execute("account.move", "write", [inv], {"x_po_number": "PO-48213"})
    code_add, out_add, _ = _run("run")
    loaded = conn.execute("select payload->>'x_po_number' from raw.account_move where odoo_id = %s "
                          "order by write_date desc limit 1", (inv,)).fetchone()[0]
    o.execute("ir.model.fields", "unlink", [field])
    code_rm, out_rm, _ = _run("run")
    said_add = [line for line in out_add.splitlines() if line.startswith(("WARN", "FAIL")) and "schema" in line]
    said_rm = [line for line in out_rm.splitlines() if line.startswith(("WARN", "FAIL")) and "schema" in line]
    return {"passed": code_add == 0 and loaded == "PO-48213" and code_rm == 0 and bool(said_add) and bool(said_rm),
            "field_added": {"exit": code_add, "value_loaded": loaded, "monitor": said_add},
            "field_removed": {"exit": code_rm, "monitor": said_rm}}


def lost_records(conn) -> dict:
    gone = conn.execute("""
        delete from raw.account_move where (odoo_id, write_date) in (
            select odoo_id, max(write_date) from raw.account_move
            where payload->>'move_type' = 'out_invoice' and payload->>'state' = 'posted'
            group by odoo_id order by odoo_id desc limit 3)
        returning odoo_id""").fetchall()
    built, _ = dbt_build()
    caught = [r for r in run_checks(conn, odoo=odoo()) if r.check_name == "source_totals"][0]
    code_fix, out_fix, secs = _run("extract", "--full")
    dbt_build()
    after = [r for r in run_checks(conn, odoo=odoo()) if r.check_name == "source_totals"][0]
    return {"passed": caught.status == "fail" and after.status == "ok",
            "rows_deleted_for_invoices": sorted(r[0] for r in gone),
            "detected": {"status": caught.status, "message": caught.message},
            "repair": {"command": "edp extract --full", "exit": code_fix, "seconds": secs},
            "after_repair": {"status": after.status, "message": after.message}}


def stale_data(conn) -> dict:
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
    with conn.cursor() as cur:
        r = check_freshness(cur, now=later)[0]
    return {"passed": r.status == "fail", "checked_as_if": later.isoformat(timespec="minutes"),
            "monitor": f"{r.status.upper()} {r.message}"}


DRILLS = [odoo_outage, overlapping_runs, schema_change, lost_records, stale_data]


def main():
    report = {"ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "drills": {}}
    # Autocommit, so the drill's own reads never hold a lock the pipeline waits on.
    with psycopg.connect(dsn(), autocommit=True) as conn:
        for drill in DRILLS:
            print(f"drill: {drill.__name__} ...", flush=True)
            try:
                report["drills"][drill.__name__] = drill(conn)
            except Exception as exc:
                report["drills"][drill.__name__] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"  passed: {report['drills'][drill.__name__]['passed']}", flush=True)
    out = ROOT / "drills"
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    sys.exit(0 if all(d["passed"] for d in report["drills"].values()) else 1)


if __name__ == "__main__":
    main()
