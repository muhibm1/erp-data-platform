"""The ask-the-warehouse tools: read-only, cited, and checkable. Needs a built warehouse."""

import os

import psycopg
import pytest

from edp import ask

DSN = os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo")


@pytest.fixture(scope="module")
def conn():
    try:
        c = psycopg.connect(DSN, connect_timeout=3, autocommit=True)
        c.execute("select 1 from marts.revenue_by_customer limit 1")
    except psycopg.Error:
        pytest.skip("no built warehouse")
    ask.ensure_reader(c)
    yield c
    c.close()


def test_find_customers_shows_merged_partners(conn):
    found = ask.find_customers(conn, "northwind")["customers"]
    assert len(found) == 1 and len(found[0]["odoo_partners"]) == 2


def test_every_summary_source_is_a_current_record(conn):
    for (key,) in conn.execute("select customer_key from marts.revenue_by_customer").fetchall():
        s = ask.customer_summary(conn, key)
        assert s["sources"], key
        assert ask.check_citations(conn, s["sources"])["all_ok"], key


def test_query_reads_the_models(conn):
    out = ask.query(conn, "select count(*) as n from marts.revenue_by_customer")
    assert out["ok"] and out["rows"][0]["n"] > 0


@pytest.mark.parametrize("sql", [
    "select * from raw.account_move",
    "delete from marts.revenue_by_customer",
    "select 1; select 2",
    "select pg_read_file('/etc/hostname')",
    "select * from pg_catalog.pg_authid",
])
def test_query_refuses_anything_outside_the_models(conn, sql):
    assert not ask.query(conn, sql)["ok"]


def test_query_cannot_write_even_through_a_function(conn):
    out = ask.query(conn, "select set_config('statement_timeout', '0', false)")
    assert out["ok"]  # a harmless function runs, but only inside the read-only transaction
    assert not ask.query(conn, "with x as (insert into marts.revenue_by_customer default values returning 1) "
                               "select * from x")["ok"]


def test_citations_catch_invented_and_stale_references(conn):
    edited = conn.execute("select odoo_id, min(write_date), max(write_date) from raw.account_move "
                          "group by odoo_id having count(*) > 1 limit 1").fetchone()
    assert edited, "the demo has an edited invoice"
    rid, old, new = edited
    stamp = "%Y-%m-%dT%H:%M:%S"
    out = ask.check_citations(conn, [f"account.move/{rid}@{new:{stamp}}", f"account.move/{rid}@{old:{stamp}}",
                                     "account.move/999999@2026-01-01T00:00:00", f"account.move/{rid}@2020-01-01T00:00:00"])
    assert [r["status"] for r in out["results"]] == ["ok", "superseded by a newer version", "unknown record",
                                                      "unknown version"]
    assert not out["all_ok"]
