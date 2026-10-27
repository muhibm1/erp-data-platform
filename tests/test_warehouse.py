"""Checks against a built warehouse. Skipped when there is none (run edp.demo first)."""

import os

import psycopg
import pytest

from edp.trace import trace

DSN = os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo")


@pytest.fixture(scope="module")
def conn():
    try:
        c = psycopg.connect(DSN, connect_timeout=3)
        c.execute("select 1 from marts.revenue_by_customer limit 1")
    except psycopg.Error:
        pytest.skip("no built warehouse")
    yield c
    c.close()


def _keys(conn, sql):
    return [r[0] for r in conn.execute(sql).fetchall()]


def test_every_customer_number_traces_to_records_that_add_up(conn):
    for name in _keys(conn, "select customer_name from marts.revenue_by_customer"):
        for table in ["revenue_by_customer", "receivables_aging"]:
            if table == "receivables_aging" and not _keys(
                    conn, f"select 1 from marts.receivables_aging where customer_name = '{name}'"):
                continue
            out = trace(conn, table, name)
            assert "DOES NOT ADD UP" not in out, out


def test_every_won_deal_traces(conn):
    for deal in _keys(conn, "select deal_id from marts.deal_to_cash"):
        out = trace(conn, "deal_to_cash", str(deal))
        assert "DOES NOT ADD UP" not in out, out


def test_a_second_load_with_no_changes_adds_nothing(conn):
    last = conn.execute("select detail from raw.load_runs where status = 'ok' "
                        "order by started_at desc limit 1").fetchone()[0]
    assert all(v == 0 for v in last.values()), last
