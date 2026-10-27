"""Checking and running the agent's SQL.

Two layers, so a mistake in one is caught by the other:

1. `check` parses the query with sqlglot before anything runs. It must be one
   SELECT (or WITH ... SELECT), every table it names must sit in one of the
   domain's schemas, and it may not contain a write, a DDL statement or a
   server-side function that reads files. The agent gets the reason back and
   can fix its query.
2. The database decides what actually runs. For the finance warehouse, queries
   go through `edp.ask.query` unchanged: a read-only role over the modelled
   schemas, a read-only transaction and a 5 second limit. For other domains the
   same rules run here, with the domain's own read-only role, plus its privacy
   rule.
"""

import datetime as dt
import re
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import sqlglot
from sqlglot import exp

MAX_ROWS = 200
BANNED_FUNCTIONS = re.compile(r"(?i)\b(pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|dblink\w*|"
                              r"pg_sleep|set_config|current_setting|pg_terminate_backend|pg_cancel_backend)\s*\(")
WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge,
          exp.TruncateTable, exp.Grant)


def check(sql: str, schemas: list[str]) -> tuple[bool, str, list[str]]:
    """(ok, reason, tables). Static checks on one query before it runs."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        return False, "empty query", []
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        return False, f"does not parse: {str(exc).splitlines()[0]}", []
    if len(statements) != 1:
        return False, "send exactly one statement", []
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return False, "only a SELECT query may run", []
    if any(tree.find_all(*WRITES)):
        return False, "the query may only read", []
    if BANNED_FUNCTIONS.search(text):
        return False, "that server function is not allowed", []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = []
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        schema = (t.db or "").lower()
        if not schema and name in ctes:
            continue
        if not schema:
            return False, f"name the schema for table {name} (one of {schemas})", []
        if schema not in schemas:
            return False, f"table {schema}.{name} is outside the readable schemas {schemas}", []
        tables.append(f"{schema}.{name}")
    return True, "", sorted(set(tables))


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _edp():
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import edp.ask
    return edp.ask


def prepare(dom, conn) -> None:
    """Make sure the domain's read-only role exists before the first query."""
    if dom.executor == "edp":
        _edp().ensure_reader(conn)


def run(dom, conn, sql: str) -> dict:
    """Runs one checked query. Returns {ok, rows, truncated} or {ok: False, error}."""
    ok, reason, _ = check(sql, dom.schemas)
    if not ok:
        return {"ok": False, "error": reason}
    if dom.executor == "edp":
        return _edp().query(conn, sql)
    text = sql.strip().rstrip(";")
    try:
        with conn.transaction():
            conn.execute("set transaction read only")
            conn.execute("set local statement_timeout = '5s'")
            conn.execute(f"set local role {dom.reader_role}")
            with conn.cursor() as cur:
                cur.execute(f"select * from ({text}) q limit {MAX_ROWS + 1}")
                cols = [c.name for c in cur.description]
                rows = [{k: _plain(v) for k, v in zip(cols, r)} for r in cur.fetchall()]
    except psycopg.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"}
    out = {"ok": True, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}
    if dom.privacy:
        out = apply_privacy(out, dom.privacy)
    return out


def apply_privacy(result: dict, rule: dict) -> dict:
    """Suppress any result row built from too few people.

    Every row that carries one of the rule's count columns (say, installs) with a
    value under the threshold has all its numbers blanked and is marked
    suppressed, so a small group cannot be singled out. A row with no count
    column at all is suppressed too when the rule requires one, because a rate
    with its count hidden can still describe a handful of people."""
    k = rule["min_count"]
    counted = [c.lower() for c in rule["count_columns"]]
    suppressed = 0
    rows = []
    for row in result["rows"]:
        present = [c for c in row if c.lower() in counted]
        small = any(row[c] is not None and row[c] < k for c in present)
        missing = rule.get("require_count", False) and not present and _has_numbers(row)
        if small or missing:
            suppressed += 1
            row = {c: (None if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                   for c, v in row.items()}
            row["_suppressed"] = f"fewer than {k} {'/'.join(rule['count_columns'])}" if small else \
                f"select a count column ({', '.join(rule['count_columns'])}) with any metric"
        rows.append(row)
    return {**result, "rows": rows, "suppressed": suppressed}


def _has_numbers(row: dict) -> bool:
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row.values())
