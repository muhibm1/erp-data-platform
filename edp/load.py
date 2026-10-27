"""Extract from Odoo into Postgres raw tables.

Every raw table is append-only and keyed by (odoo_id, write_date): a record
edited in Odoo lands as a new version, and the old version stays. That is
what lets a later check say "this invoice changed after it was posted", and
what lets any number be traced to the exact version it was computed from.

Loads are incremental on write_date and safe to repeat: the watermark is
inclusive and the key ignores versions already stored.
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
    finished_at timestamptz, status text not null, detail jsonb);
create table if not exists raw.deleted (
    model text not null, odoo_id integer not null, seen_missing_at timestamptz not null,
    run_id text not null, primary key (model, odoo_id));
"""

TABLE_DDL = """
create table if not exists raw.{t} (
    odoo_id integer not null, write_date timestamp not null, payload jsonb not null,
    run_id text not null, loaded_at timestamptz not null default now(),
    primary key (odoo_id, write_date));
"""


def _fields(odoo: Odoo, model: str) -> list[str]:
    """Wanted fields that exist here, plus any custom x_ field the client added."""
    present = odoo.fields(model)
    missing = [f for f in SOURCES[model] if f not in present]
    if missing:
        raise RuntimeError(f"{model} is missing fields {missing}; the source changed")
    custom = sorted(f for f, stored in present.items() if stored and f.startswith("x_"))
    return ["id", "write_date", *SOURCES[model], *custom]


def _watermark(cur, table: str):
    cur.execute(f"select max(write_date) from raw.{table}")
    return cur.fetchone()[0]


def extract(odoo: Odoo, conn: psycopg.Connection, full: bool = False) -> dict:
    """One run over every source; returns new-version counts per model."""
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6]
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("insert into raw.load_runs values (%s, now(), null, 'running', null)", (run_id,))
        conn.commit()
        try:
            for model in SOURCES:
                t = table_name(model)
                cur.execute(TABLE_DDL.format(t=t))
                domain = list(DOMAINS.get(model, []))
                mark = None if full else _watermark(cur, t)
                if mark:
                    domain.append(("write_date", ">=", mark.strftime("%Y-%m-%d %H:%M:%S")))
                new = 0
                for rec in odoo.search_read(model, domain, _fields(odoo, model)):
                    cur.execute(
                        f"insert into raw.{t} (odoo_id, write_date, payload, run_id) "
                        "values (%s, %s, %s, %s) on conflict do nothing",
                        (rec["id"], rec["write_date"], json.dumps(rec), run_id))
                    new += cur.rowcount
                counts[model] = new
                # Hard deletes leave no trace in a write_date scan, so compare ids.
                live = set(odoo.execute(model, "search", DOMAINS.get(model, [])))
                cur.execute(f"select distinct odoo_id from raw.{t}")
                for (gone,) in cur.fetchall():
                    if gone not in live:
                        cur.execute(
                            "insert into raw.deleted values (%s, %s, now(), %s) on conflict do nothing",
                            (model, gone, run_id))
                conn.commit()
            cur.execute("update raw.load_runs set finished_at = now(), status = 'ok', detail = %s "
                        "where run_id = %s", (json.dumps(counts), run_id))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            cur.execute("update raw.load_runs set finished_at = now(), status = 'failed', detail = %s "
                        "where run_id = %s", (json.dumps({"error": str(exc), **counts}), run_id))
            conn.commit()
            raise
    return {"run_id": run_id, **counts}
