"""edp: load Odoo into the warehouse, run it on a schedule, watch it, and trace any number."""

import argparse
import datetime as dt
import json
import os
import sys

import psycopg


def dsn() -> str:
    return os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo")


def odoo():
    from .odoo import Odoo
    # The login secret is read from the environment, never from a file in the repo.
    return Odoo(os.environ.get("ODOO_URL", "http://localhost:8069"),
                os.environ.get("ODOO_DB", "acme"),
                os.environ.get("ODOO_USER", "admin"),
                os.environ.get("ODOO_PASSWORD", "admin"),
                retries=int(os.environ.get("EDP_ODOO_RETRIES", "4")))


def main(argv=None):
    p = argparse.ArgumentParser(prog="edp")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="pull new and changed Odoo records into raw")
    e.add_argument("--full", action="store_true", help="re-read everything (stored versions are skipped)")
    e.add_argument("--since", type=dt.date.fromisoformat, help="re-read records changed on or after a date")
    r = sub.add_parser("run", help="one scheduled run: extract, dbt build, monitors")
    r.add_argument("--since", type=dt.date.fromisoformat, help="backfill from a date in this run")
    r.add_argument("--every", type=int, help="keep running, this many seconds apart")
    sub.add_parser("monitor", help="run the monitors on their own")
    t = sub.add_parser("trace", help="show the source records behind a number")
    t.add_argument("table", choices=["revenue_by_customer", "receivables_aging", "deal_to_cash"])
    t.add_argument("key", help="customer name, or deal id for deal_to_cash")
    args = p.parse_args(argv)

    if args.cmd == "extract":
        from .load import extract
        with psycopg.connect(dsn()) as conn:
            print(json.dumps(extract(odoo(), conn, args.full, args.since), indent=2))
    elif args.cmd == "run":
        from .pipeline import run_forever, run_once
        if args.every:
            run_forever(dsn(), odoo, args.every)
        with psycopg.connect(dsn()) as conn:
            sys.exit(run_once(conn, odoo, since=args.since))
    elif args.cmd == "monitor":
        from .monitor import render, run_checks
        try:
            client = odoo()
        except Exception as exc:
            client, down = None, f"{type(exc).__name__}: {exc}"
        with psycopg.connect(dsn()) as conn:
            results = run_checks(conn, odoo=client)
        if client is None:
            from .monitor import Result
            results.append(Result("source_totals", "fail", f"Odoo unreachable, totals not compared ({down})"))
        print(render(results))
        sys.exit(1 if any(r.status == "fail" for r in results) else 0)
    elif args.cmd == "trace":
        from .trace import trace
        with psycopg.connect(dsn()) as conn:
            print(trace(conn, args.table, args.key))


if __name__ == "__main__":
    main()
