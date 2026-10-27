"""Tests for the analyst. No model is called: FakeModel replays scripted replies.

The graph tests need the finance warehouse (see the main README); they skip without it.
"""

import psycopg
import pytest

from analyst import domain as domains
from analyst import sql
from analyst.eval.run import correct, wilson
from analyst.graph import ask, build, one_shot, value_of
from analyst.model import FakeModel
from analyst.retrieve import Retriever

SCHEMAS = ["marts", "intermediate", "staging"]


@pytest.mark.parametrize("query,reason", [
    ("delete from marts.deal_to_cash", "only a SELECT"),
    ("select * from raw.account_move", "outside the readable schemas"),
    ("select 1; select 2", "exactly one statement"),
    ("select * from revenue_by_customer", "name the schema"),
    ("select pg_read_file('/etc/passwd')", "not allowed"),
    ("with x as (delete from marts.deal_to_cash returning *) select * from x", "may only read"),
    ("not sql at all (", "does not parse"),
])
def test_check_refuses(query, reason):
    ok, why, _ = sql.check(query, SCHEMAS)
    assert not ok and reason in why


def test_check_allows_ctes_and_joins():
    ok, _, tables = sql.check(
        "with late as (select customer_key from marts.receivables_aging) "
        "select c.customer_name from late join marts.revenue_by_customer c using (customer_key)", SCHEMAS)
    assert ok and tables == ["marts.receivables_aging", "marts.revenue_by_customer"]


def test_privacy_blanks_small_groups_only():
    rule = {"min_count": 50, "count_columns": ["installs"]}
    out = sql.apply_privacy({"ok": True, "rows": [
        {"country": "US", "installs": 900, "spend": 1200.0},
        {"country": "IS", "installs": 12, "spend": 40.0},
    ]}, rule)
    assert out["rows"][0]["spend"] == 1200.0
    assert out["rows"][1]["installs"] is None and out["rows"][1]["spend"] is None
    assert out["rows"][1]["country"] == "IS" and out["suppressed"] == 1


def test_keyword_retrieval_finds_the_aging_table():
    docs = [
        {"kind": "table", "key": "marts.receivables_aging", "text": "TABLE aging: owed by days past due late_over_90"},
        {"kind": "table", "key": "staging.stg_deals", "text": "TABLE deals: won lost pipeline stage"},
        {"kind": "term", "key": "won deal", "text": "TERM won deal: a CRM deal marked won"},
    ]
    picked = Retriever(docs, embed=None).select("How much is more than 90 days past due?",
                                                {"table": 1, "term": 0})
    assert [d["key"] for d in picked] == ["marts.receivables_aging"]


def test_grading_rules():
    money = {"answer_type": "money", "answer": 100.0}
    assert correct(money, {"status": "answer", "answer": "$100.00"})
    assert not correct(money, {"status": "answer", "answer": 100.02})
    assert not correct(money, {"status": "not_answerable", "answer": None})
    names = {"answer_type": "set", "answer": ["B", "a"]}
    assert correct(names, {"status": "answer", "answer": ["A", "b"]})
    none = {"answer_type": "none", "answer": None}
    assert correct(none, {"status": "not_answerable", "answer": None})
    assert not correct(none, {"status": "answer", "answer": 0})
    assert wilson(10, 10)[0] > 65


def test_value_of_reads_a_result_like_a_dashboard():
    assert value_of([{"n": 4}]) == 4
    assert value_of([{"name": "a", "x": 1}, {"name": "b", "x": 2}]) == ["a", "b"]
    assert value_of([]) == []


@pytest.fixture(scope="module")
def finance():
    dom = domains.load("finance")
    try:
        with dom.connect() as conn:
            domains.read_columns(dom, conn)
    except psycopg.OperationalError:
        pytest.skip("finance warehouse not reachable")
    return dom


def test_repairs_a_failed_query(finance):
    model = FakeModel([
        {"decision": "query", "sql": "select nope from marts.revenue_by_customer", "reason": "first try"},
        {"decision": "query", "sql": "select count(*) as n from marts.revenue_by_customer", "reason": "fixed"},
        {"status": "answer", "answer": 31, "explanation": "31 customers."},
    ])
    out = ask(build(finance, model, context=False), "How many customers are there?", "t-repair")
    assert out["status"] == "answer" and out["answer"] == 31
    assert out["attempts"][0]["error"] and out["attempts"][1]["error"] is None
    assert "FAILED" in model.prompts[1]


def test_gives_up_after_three_bad_queries(finance):
    bad = {"decision": "query", "sql": "select * from raw.account_move", "reason": "x"}
    out = ask(build(finance, FakeModel([bad, bad, bad]), context=False), "Show raw invoices", "t-fail")
    assert out["status"] == "failed" and len(out["attempts"]) == 3


def test_declines_without_running_anything(finance):
    model = FakeModel([{"decision": "not_answerable", "sql": "", "reason": "No credit scores are recorded."}])
    out = ask(build(finance, model, context=False), "What is Northwind's credit score?", "t-decline")
    assert out["status"] == "not_answerable" and out["answer"] is None and len(model.prompts) == 1


def test_follow_up_sees_the_earlier_turn(finance):
    model = FakeModel([
        {"decision": "query", "sql": "select sum(open) as owed from marts.revenue_by_customer", "reason": "total"},
        {"status": "answer", "answer": 362891.89, "explanation": "Total owed."},
        {"decision": "query", "sql": "select sum(paid) as paid from marts.revenue_by_customer", "reason": "paid"},
        {"status": "answer", "answer": 1016263.52, "explanation": "Total paid."},
    ])
    app = build(finance, model, context=False)
    ask(app, "How much do customers owe us?", "t-memory")
    out = ask(app, "And how much have they paid?", "t-memory")
    assert out["answer"] == 1016263.52
    assert "How much do customers owe us?" in model.prompts[2]
    assert len(out["history"]) == 2


def test_cites_rows_that_carry_a_source(finance):
    model = FakeModel([
        {"decision": "query", "reason": "invoices",
         "sql": "select invoice_ref, source_ref from intermediate.int_invoices order by invoice_id limit 2"},
        {"status": "answer", "answer": ["x"], "explanation": "Two invoices."},
    ])
    out = ask(build(finance, model, context=False), "Show two invoices", "t-cite")
    assert len(out["sources"]) == 2 and out["citations"]["all_ok"]


def test_one_shot_takes_the_query_result_as_the_answer(finance):
    model = FakeModel([{"decision": "query", "sql": "select count(*) from marts.deal_to_cash", "reason": "x"}])
    assert one_shot(finance, model, "How many won deals?")["answer"] == 79
