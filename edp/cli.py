"""edp: extract from Odoo, and trace a number back to its records."""

import argparse
import json
import os

import psycopg


def _conn():
    return psycopg.connect(os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo"))


def _odoo():
    from .odoo import Odoo
    # The login secret is read from the environment, never from a file in the repo.
    return Odoo(os.environ.get("ODOO_URL", "http://localhost:8069"),
                os.environ.get("ODOO_DB", "acme"),
                os.environ.get("ODOO_USER", "admin"),
                os.environ.get("ODOO_PASSWORD", "admin"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="edp")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="pull new and changed Odoo records into raw")
    e.add_argument("--full", action="store_true", help="ignore the watermark")
    t = sub.add_parser("trace", help="show the source records behind a number")
    t.add_argument("table", choices=["revenue_by_customer", "receivables_aging", "deal_to_cash"])
    t.add_argument("key", help="customer name, or deal id for deal_to_cash")
    args = p.parse_args(argv)
    if args.cmd == "extract":
        from .load import extract
        with _conn() as conn:
            print(json.dumps(extract(_odoo(), conn, args.full), indent=2))
    elif args.cmd == "trace":
        from .trace import trace
        with _conn() as conn:
            print(trace(conn, args.table, args.key))


if __name__ == "__main__":
    main()
