"""Extract from Odoo into Postgres raw tables.

Every raw table is append-only and keyed by (odoo_id, write_date): a record
edited in Odoo lands as a new version, and the old version stays. That is
what lets a later check say "this invoice changed after it was posted", and
what lets any number be traced to the exact version it was computed from.

Loads are incremental on write_date and safe to repeat: the watermark is
inclusive and the key ignores versions already stored. Each model's records
commit together, so a run that fails part-way leaves every model either fully
loaded for that run or untouched.

Each run also saves the source's field list per model (raw.source_schemas),
so the schema monitor can say exactly what changed in Odoo between two runs.
"""

import datetime as dt
import json
import uuid

import psycopg

from .odoo import Odoo
from .sources import DOMAINS, SOURCES, table_name

DDL = """
create schema if not exists raw;
create table if not exists raw.load_runs (
    run_id text primary key, started_at timestamptz not null,
    finished_at timestamptz, status text not null, detail jsonb,
    mode text not null default 'incremental');
create table if not exists raw.deleted (
    model text not null, odoo_id integer not null, seen_missing_at timestamptz not null,
    run_id text not null, primary key (model, odoo_id));
create table if not exists raw.source_schemas (
    run_id text not null, model text not null, fields jsonb not null,
    captured_at timestamptz not null default now(), primary key (run_id, model));
"""

TABLE_DDL = """
create table if not exists raw.{t} (
    odoo_id integer not null, write_date timestamp not null, payload jsonb not null,
    run_id text not null, loaded_at timestamptz not null default now(),
    primary key (odoo_id, write_date));
"""


class SourceChanged(RuntimeError):
    """A field the load depends on is gone from Odoo. Nothing was written for that model."""


def new_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6]


def _fields(present: dict[str, bool], model: str) -> list[str]:
    """Wanted fields that exist here, plus any custom x_ field the client added."""
    missing = [f for f in SOURCES[model] if f not in present]
    if missing:
        raise SourceChanged(f"{model} is missing fields {missing}; the source changed")
    custom = sorted(f for f, stored in present.items() if stored and f.startswith("x_"))
    return ["id", "write_date", *SOURCES[model], *custom]


def _watermark(cur, table: str):
    cur.execute(f"select max(write_date) from raw.{table}")
    return cur.fetchone()[0]


def ensure_schema(conn: psycopg.Connection) -> None:
    """Creates what is missing. Never alters a table that exists: that would wait on every reader."""
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("select 1 from information_schema.columns "
                    "where table_schema = 'raw' and table_name = 'load_runs' and column_name = 'mode'")
        if not cur.fetchone():
            cur.execute("alter table raw.load_runs add column mode text not null default 'incremental'")
        for model in SOURCES:
            cur.execute(TABLE_DDL.format(t=table_name(model)))
    conn.commit()


def extract(odoo: Odoo, conn: psycopg.Connection, full: bool = False,
            since: dt.date | None = None, run_id: str | None = None) -> dict:
    """One run over every source; returns new-version counts per model.

    full re-reads everything; since re-reads records changed on or after a date
    (a backfill after an outage). Either way, versions already stored are skipped.
    """
    run_id = run_id or new_run_id()
    mode = "full" if full else f"backfill:{since}" if since else "incremental"
    counts: dict[str, int] = {}
    live_counts: dict[str, int] = {}
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("insert into raw.load_runs (run_id, started_at, status, mode) "
                    "values (%s, now(), 'running', %s)", (run_id, mode))
        conn.commit()
        try:
            for model in SOURCES:
                t = table_name(model)
                present = odoo.fields(model)
                fields = _fields(present, model)
                cur.execute("insert into raw.source_schemas (run_id, model, fields) values (%s, %s, %s)",
                            (run_id, model, json.dumps(sorted(f for f, stored in present.items() if stored))))
                domain = list(DOMAINS.get(model, []))
                if since:
                    domain.append(("write_date", ">=", f"{since} 00:00:00"))
                elif not full:
                    mark = _watermark(cur, t)
                    if mark:
                        domain.append(("write_date", ">=", mark.strftime("%Y-%m-%d %H:%M:%S")))
                new = 0
                for rec in odoo.search_read(model, domain, fields):
                    cur.execute(
                        f"insert into raw.{t} (odoo_id, write_date, payload, run_id) "
                        "values (%s, %s, %s, %s) on conflict do nothing",
                        (rec["id"], rec["write_date"], json.dumps(rec), run_id))
                    new += cur.rowcount
                counts[model] = new
                # Hard deletes leave no trace in a write_date scan, so compare ids.
                live = set(odoo.execute(model, "search", DOMAINS.get(model, [])))
                live_counts[model] = len(live)
                cur.execute(f"select distinct odoo_id from raw.{t}")
                for (gone,) in cur.fetchall():
                    if gone not in live:
                        cur.execute(
                            "insert into raw.deleted values (%s, %s, now(), %s) on conflict do nothing",
                            (model, gone, run_id))
                conn.commit()
            cur.execute("update raw.load_runs set finished_at = now(), status = 'ok', detail = %s "
                        "where run_id = %s", (json.dumps({**counts, "live": live_counts}), run_id))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            cur.execute("update raw.load_runs set finished_at = now(), status = 'failed', detail = %s "
                        "where run_id = %s",
                        (json.dumps({"error": f"{type(exc).__name__}: {exc}", **counts}), run_id))
            conn.commit()
            raise
    return {"run_id": run_id, "mode": mode, **counts}
