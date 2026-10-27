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


ADS_RULE = {"min_count": 50, "count_columns": ["installs", "taps"]}
ADS_COLUMNS = {
    "reporting.audience_daily": [("day", "date"), ("campaign_id", "integer"), ("age_band", "text"),
                                 ("gender", "text"), ("impressions", "integer"), ("taps", "integer"),
                                 ("installs", "integer")],
}


@pytest.mark.parametrize("query,rows", [
    # Renaming the count used to skip the check: it is traced to installs, whatever its name.
    ("select age_band, sum(installs) as n from reporting.audience_daily group by 1",
     [{"age_band": "65+", "n": 12}]),
    ("select age_band, sum(installs) installs_total from reporting.audience_daily group by 1",
     [{"age_band": "65+", "installs_total": 12}]),
    ("select age_band, taps from reporting.audience_daily", [{"age_band": "65+", "taps": 3}]),
    ("with a as (select age_band, sum(installs) as x from reporting.audience_daily group by 1) "
     "select age_band, x as hidden from a", [{"age_band": "65+", "hidden": 12}]),
    ("select * from reporting.audience_daily",
     [{"day": "2026-10-01", "campaign_id": 1, "age_band": "65+", "gender": "f", "impressions": 900,
       "taps": 30, "installs": 9}]),
    ("select age_band, sum(installs) n from reporting.audience_daily group by 1 "
     "union all select gender, sum(taps) from reporting.audience_daily group by 1",
     [{"age_band": "f", "n": 7}]),
    ("select sum(installs) - sum(installs) filter (where age_band <> '65+') as rest, "
     "sum(installs) as n from reporting.audience_daily", [{"rest": 12, "n": 9000}]),
])
def test_privacy_blanks_a_small_count_whatever_it_is_called(query, rows):
    out = sql.apply_privacy({"ok": True, "rows": rows}, ADS_RULE, query, ADS_COLUMNS)
    assert out["suppressed"] == 1
    assert all(v is None for v in out["rows"][0].values() if isinstance(v, (int, type(None))))


@pytest.mark.parametrize("query", [
    "select sum(installs) total, sum(installs * 100) older from reporting.audience_daily",
    "select sum(installs) total, sum(case when age_band = '65+' then installs * 100 end) older "
    "from reporting.audience_daily",
    "select sum(installs) total, (select sum(installs) from reporting.audience_daily "
    "where age_band = '65+') older from reporting.audience_daily",
])
def test_privacy_refuses_people_figures_it_could_not_check(query):
    ok, _ = sql.privacy_check(query, ADS_RULE)
    assert not ok


def test_privacy_checks_the_counts_behind_a_rate():
    query = ("select age_band, round(sum(installs)::numeric / sum(taps), 2) as cvr, "
             "sum(installs) filter (where gender = 'f') as women from reporting.audience_daily group by 1")
    assert sql.privacy_check(query, ADS_RULE)[0]
    augmented = sql.with_helpers(query, ADS_RULE, ADS_COLUMNS)
    assert "SUM(taps) AS __people_a0" in augmented and "SUM(installs) AS __people_a1" in augmented
    out = sql.apply_privacy({"ok": True, "rows": [
        {"age_band": "18-24", "cvr": 0.4, "women": 400, "__people_a0": 2000, "__people_a1": 900},
        {"age_band": "65+", "cvr": 0.4, "women": 400, "__people_a0": 30, "__people_a1": 12},
    ]}, ADS_RULE, augmented, ADS_COLUMNS)
    assert out["rows"][0] == {"age_band": "18-24", "cvr": 0.4, "women": 400}
    assert out["rows"][1]["cvr"] is None and out["suppressed"] == 1


def test_privacy_asks_for_the_count_beside_a_rate_it_cannot_check():
    query = ("with r as (select installs::numeric / taps as rate from reporting.audience_daily) "
             "select rate from r")
    out = sql.apply_privacy({"ok": True, "rows": [{"rate": 0.4}]}, ADS_RULE, query, ADS_COLUMNS)
    assert out["rows"][0]["rate"] is None and "plain count" in out["needs_count"]


def test_privacy_probes_groups_inside_a_cte():
    query = ("with v as (select age_band, sum(installs)::numeric / sum(taps) p from reporting.audience_daily "
             "group by age_band) select max(p) - min(p) from v")
    probes = sql.inner_group_probes(query, ADS_RULE)
    assert len(probes) == 1 and "LEAST(" in probes[0] and "SUM(taps)" in probes[0] and "GROUP BY age_band" in probes[0]


def test_privacy_leaves_figures_that_count_no_people():
    query = "select campaign_id, sum(impressions) as seen, count(*) as days from reporting.audience_daily group by 1"
    out = sql.apply_privacy({"ok": True, "rows": [{"campaign_id": 1, "seen": 40, "days": 3}]},
                            ADS_RULE, query, ADS_COLUMNS)
    assert out["suppressed"] == 0 and "needs_count" not in out


@pytest.fixture(scope="module")
def ads():
    dom = domains.load("ads")
    try:
        conn = dom.connect()
        domains.read_columns(dom, conn)
        sql.prepare(dom, conn)
    except psycopg.OperationalError:
        pytest.skip("ads demo database not reachable (python -m analyst.ads.simulate)")
    yield dom, conn
    conn.close()


SMALL = ("age_band = '55+' and gender = 'female' and day = date '2026-09-09' "
         "and campaign_id = (select min(campaign_id) from reporting.campaigns)")


@pytest.mark.parametrize("query", [
    f"select sum(installs) as n from reporting.audience_daily where {SMALL}",
    f"select sum(installs) total, sum(case when {SMALL} then installs end) older from reporting.audience_daily",
    f"select sum(installs) total, sum(installs) filter (where {SMALL}) older from reporting.audience_daily",
    f"select sum(installs) - sum(installs) filter (where not ({SMALL})) as rest, sum(installs) n "
    f"from reporting.audience_daily",
    f"select round(sum(installs)::numeric / sum(taps), 3) as cvr from reporting.audience_daily where {SMALL}",
    f"with s as (select sum(installs)::numeric / sum(taps) r from reporting.audience_daily where {SMALL}) "
    f"select round(r, 3) from s",
])
def test_ads_tool_never_returns_a_small_group(ads, query):
    dom, conn = ads
    out = sql.run(dom, conn, query)
    assert not out["ok"] or (out["suppressed"] == len(out["rows"]) and all(
        v is None for row in out["rows"] for v in row.values() if not isinstance(v, str)))


def test_ads_tool_still_answers_the_eval_gold_queries(ads):
    import json
    from pathlib import Path
    dom, conn = ads
    data = Path(__file__).resolve().parent.parent / "eval" / "data"
    for name in ("ads-dev.jsonl", "ads-test.jsonl"):
        for line in (data / name).read_text().splitlines():
            q = json.loads(line).get("answer_sql")
            if q:
                out = sql.run(dom, conn, q)
                assert out["ok"] and not out["suppressed"], (q, out)


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


def test_looks_a_name_up_before_the_final_query(ads):
    dom, _ = ads
    model = FakeModel([
        {"decision": "lookup", "reason": "how is Mint spelled",
         "sql": "select distinct keyword from reporting.keywords where lower(keyword) like '%mint%'"},
        {"decision": "query", "reason": "the exact keyword",
         "sql": "select sum(d.installs) as installs from reporting.keyword_daily d "
                "join reporting.keywords k using (keyword_id) where k.keyword = 'mint budget'"},
        {"status": "answer", "answer": 1, "explanation": "x"},
    ])
    out = ask(build(dom, model, context=False), "How many installs did the Mint keyword get?", "t-lookup")
    assert out["status"] == "answer"
    assert out["lookups"][0]["found"] == '[{"keyword": "mint budget"}]'
    assert "mint budget" in model.prompts[1] and "1 more lookup" in model.prompts[1]


def test_lookups_are_capped(ads):
    dom, _ = ads
    look = {"decision": "lookup", "reason": "x", "sql": "select distinct keyword from reporting.keywords limit 1"}
    model = FakeModel([look, look, {"decision": "not_answerable", "sql": "", "reason": "no such keyword"}])
    out = ask(build(dom, model, context=False, max_lookups=2), "Installs for 'notion'?", "t-cap")
    assert out["status"] == "not_answerable" and len(out["lookups"]) == 2
    assert "No lookups are left" in model.prompts[2]


def test_a_declined_reading_goes_back_to_draft_once(ads):
    dom, _ = ads
    q = {"decision": "query", "reason": "x", "sql": "select count(*) as n from reporting.campaigns"}
    decline = {"status": "not_answerable", "answer": None, "explanation": "That counts campaigns, not keywords."}
    model = FakeModel([q, decline, q, decline])
    out = ask(build(dom, model, context=False), "How many keywords?", "t-redraft")
    assert out["status"] == "not_answerable" and out["redrafts"] == 1
    assert "counts campaigns, not keywords" in model.prompts[2]


def test_the_first_scored_agent_has_no_lookups_or_redraft(ads):
    dom, _ = ads
    q = {"decision": "query", "reason": "x", "sql": "select count(*) as n from reporting.campaigns"}
    model = FakeModel([q, {"status": "not_answerable", "answer": None, "explanation": "wrong query"}])
    out = ask(build(dom, model, context=False, max_lookups=0, max_redrafts=0), "How many keywords?", "t-v1")
    assert out["status"] == "not_answerable" and len(model.prompts) == 2
    assert "lookup" not in model.prompts[0].lower()


class _StructuredStub:
    """A chat model's structured-output surface, enough for ChatModel: records what it was asked."""

    def __init__(self, reply):
        self.reply, self.schemas, self.messages = reply, [], []

    def with_structured_output(self, schema, include_raw=False):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda

        self.schemas.append(schema)

        def answer(messages):
            self.messages.append(messages)
            raw = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13})
            return {"parsed": self.reply, "raw": raw, "parsing_error": None}

        return RunnableLambda(answer)


def test_chat_model_route_asks_for_the_step_schema(tmp_path):
    from analyst.graph import DRAFT_SCHEMA
    from analyst.model import ChatModel

    stub = _StructuredStub({"decision": "query", "sql": "select 1", "reason": "x"})
    model = ChatModel(llm=stub, model="sonnet", usage_log=tmp_path / "u.jsonl")
    assert model("SYSTEM", "PROMPT", DRAFT_SCHEMA)["decision"] == "query"
    assert stub.schemas[0]["properties"] == DRAFT_SCHEMA["properties"] and stub.schemas[0]["title"]
    assert [m.content for m in stub.messages[0]] == ["SYSTEM", "PROMPT"]
    assert model.model == "claude-sonnet-5-5"
    assert '"input": 10' in (tmp_path / "u.jsonl").read_text()


def test_chat_model_route_turns_a_bad_reply_into_a_model_error():
    from analyst.model import ChatModel, ModelError

    with pytest.raises(ModelError):
        ChatModel(llm=_StructuredStub(None))("s", "p", {"type": "object", "properties": {}})
