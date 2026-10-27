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


def test_loading_twice_in_a_row_stores_nothing_the_second_time(conn):
    from edp.cli import odoo
    from edp.load import extract
    try:
        client = odoo()
    except Exception:
        pytest.skip("Odoo is not running")
    extract(client, conn)
    again = extract(client, conn)
    assert all(v == 0 for k, v in again.items() if k not in ("run_id", "mode")), again
