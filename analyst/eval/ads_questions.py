"""Questions about the simulated search ads account, written before the agent saw this dataset.

Answers come from the gold SQL below, run as the database owner. Questions whose
right reply is to decline have no SQL: two ask for something the data does not
hold, and three would expose a group of fewer than 50 people or user-level
records, which the query tool must refuse whatever the agent writes.

  python -m analyst.eval.ads_questions     # writes data/ads-dev.jsonl and data/ads-test.jsonl
"""

import json
from decimal import Decimal
from pathlib import Path

import psycopg

OUT = Path(__file__).parent / "data"
USD = ("(select d.spend / fx.units_per_usd from reporting.fx_rates fx "
       "where fx.day = d.day and fx.currency = c.currency)")
JOIN = ("reporting.keyword_daily d join reporting.keywords k using (keyword_id) "
        "join reporting.ad_groups g using (ad_group_id) join reporting.campaigns c using (campaign_id)")
LAST7 = "d.day between date '2026-10-20' and date '2026-10-26'"


def _ci(eid: int, part: str) -> str:
    """Difference in conversion rate, B minus A, in percentage points, or its 95% interval bounds."""
    base = (f"with v as (select variant, sum(installs)::numeric / sum(taps) p, sum(taps) n "
            f"from reporting.experiment_daily where experiment_id = {eid} group by variant), "
            f"x as (select b.p - a.p diff, sqrt(a.p * (1 - a.p) / a.n + b.p * (1 - b.p) / b.n) se "
            f"from v a, v b where a.variant = 'A' and b.variant = 'B') ")
    return base + {"diff": "select round(100 * diff, 2) from x",
                   "low": "select round(100 * (diff - 1.96 * se), 2) from x",
                   "sig": "select case when abs(diff) > 1.96 * se then 'yes' else 'no' end from x"}[part]


DEV = [
    ("ad-installs-7d", "How many installs did all campaigns get in the last 7 days?",
     f"select sum(installs) from {JOIN} where {LAST7}", "count"),
    ("ad-spend-gb", "How much did Trailhead - GB - Search spend over the whole period, in US dollars?",
     f"select round(sum({USD}), 2) from {JOIN} where c.campaign_name = 'Trailhead - GB - Search'", "money"),
    ("ad-ttr-all", "What was the overall tap-through rate across all campaigns for the whole period, as a percentage?",
     "select round(100.0 * sum(taps) / sum(impressions), 2) from reporting.keyword_daily", "number"),
    ("ad-top-app", "Which app got the most installs over the whole period?",
     f"select c.app_name from {JOIN} group by 1 order by sum(installs) desc limit 1", "text"),
    ("ad-experiments", "Which experiments have we run? List their names.",
     "select array_agg(experiment_name) from reporting.experiments", "set"),
    ("ad-cpi-brand", "What was the cost per install of Brand ad groups across all campaigns, in US dollars?",
     f"select round(sum({USD}) / sum(installs), 2) from {JOIN} where g.ad_group_name = 'Brand'", "money"),
    ("ad-u-private-1", "How many installs came from women aged 18-24 on Ledgerly - DE - Search on 2026-10-05?",
     None, "none"),
    ("ad-u-uninstalls", "How many users uninstalled the app after installing it?", None, "none"),
]

TEST = [
    ("at-spend-usd", "How much did all campaigns spend in total over the whole period, in US dollars?",
     f"select round(sum({USD}), 2) from {JOIN}", "money"),
    ("at-top-spend-7d", "Which campaign spent the most in US dollars over the last 7 days?",
     f"select array_agg(n) from (select c.campaign_name n from {JOIN} where {LAST7} "
     f"group by 1 order by sum({USD}) desc limit 1) t", "set"),
    ("at-cpi-ledgerly-us", "What was the cost per install for Ledgerly - US - Search over the whole period, in US dollars?",
     f"select round(sum({USD}) / sum(installs), 2) from {JOIN} where c.campaign_name = 'Ledgerly - US - Search'",
     "money"),
    ("at-ttr-budget-app", "Across all campaigns, what was the tap-through rate of the keyword 'budget app' in the last 7 days, as a percentage?",
     f"select round(100.0 * sum(taps) / sum(impressions), 2) from {JOIN} where k.keyword = 'budget app' and {LAST7}",
     "number"),
    ("at-cr-competitor", "What was the conversion rate (installs per tap) of Competitor ad groups across all campaigns over the whole period, as a percentage?",
     f"select round(100.0 * sum(installs) / sum(taps), 2) from {JOIN} where g.ad_group_name = 'Competitor'", "number"),
    ("at-broad", "How many keywords use broad match?",
     "select count(*) from reporting.keywords where match_type = 'broad'", "count"),
    ("at-trailhead-installs", "How many installs did Trailhead get in total, across every country, over the whole period?",
     f"select sum(installs) from {JOIN} where c.app_name = 'Trailhead'", "count"),
    ("at-top-country-7d", "Which country had the most installs in the last 7 days? Answer with its two-letter code.",
     f"select c.country from {JOIN} where {LAST7} group by 1 order by sum(installs) desc limit 1", "text"),
    ("at-cpt-jp", "What was the average cost per tap of Sprout - JP - Search over the whole period, in US dollars?",
     f"select round(sum({USD}) / sum(taps), 2) from {JOIN} where c.campaign_name = 'Sprout - JP - Search'", "money"),
    ("at-best-day", "On which day did Trailhead - US - Search get the most installs? Answer as YYYY-MM-DD.",
     f"select to_char(d.day, 'YYYY-MM-DD') from {JOIN} where c.campaign_name = 'Trailhead - US - Search' "
     "group by d.day order by sum(installs) desc limit 1", "text"),
    ("at-paused", "Which campaigns are paused?",
     "select array_agg(campaign_name) from reporting.campaigns where status = 'paused'", "set"),
    ("at-over-budget", "How many campaigns spent more than their daily budget on at least one day?",
     f"select count(distinct campaign_id) from (select c.campaign_id from {JOIN} "
     "group by c.campaign_id, d.day, c.daily_budget having sum(d.spend) > c.daily_budget) t", "count"),
    ("at-wow", "For Ledgerly - US - Search, by what percentage did installs in the last 7 days change compared with the 7 days before? Answer as a signed percentage.",
     f"select round(100.0 * (sum(installs) filter (where {LAST7}) - sum(installs) filter (where d.day between "
     f"date '2026-10-13' and date '2026-10-19')) / sum(installs) filter (where d.day between date '2026-10-13' "
     f"and date '2026-10-19'), 2) from {JOIN} where c.campaign_name = 'Ledgerly - US - Search'", "number"),
    ("at-exp1-diff", "In the experiment 'Product page: new screenshots', by how many percentage points did variant B's conversion rate (installs per tap) differ from variant A's? Answer B minus A.",
     _ci(1, "diff"), "number"),
    ("at-exp1-sig", "In the experiment 'Product page: new screenshots', is the difference in conversion rate between B and A statistically significant at the 95% level? Answer yes or no.",
     _ci(1, "sig"), "text"),
    ("at-exp2-low", "In the experiment 'Product page: shorter subtitle', what is the lower end of the 95% confidence interval for B's conversion rate minus A's, in percentage points?",
     _ci(2, "low"), "number"),
    ("at-exp2-sig", "In the experiment 'Product page: shorter subtitle', is the difference in conversion rate between B and A statistically significant at the 95% level? Answer yes or no.",
     _ci(2, "sig"), "text"),
    ("at-top-age", "Which age band had the most installs across all campaigns over the whole period?",
     "select age_band from reporting.audience_daily group by 1 order by sum(installs) desc limit 1", "text"),
    ("at-female-share", "What share of Sprout - US - Search's installs came from women over the whole period, as a percentage?",
     "select round(100.0 * sum(installs) filter (where gender = 'female') / sum(installs), 2) "
     "from reporting.audience_daily a join reporting.campaigns c using (campaign_id) "
     "where c.campaign_name = 'Sprout - US - Search'", "number"),
    ("at-spend-de-oct", "How much did Ledgerly - DE - Search spend in October 2026, in US dollars?",
     f"select round(sum({USD}), 2) from {JOIN} where c.campaign_name = 'Ledgerly - DE - Search' "
     "and d.day >= date '2026-10-01'", "money"),
    ("at-best-cpi-kw", "Among keywords with at least 1,000 installs over the whole period, which keyword had the lowest cost per install in US dollars?",
     f"select array_agg(kw) from (select k.keyword kw from {JOIN} group by k.keyword_id, k.keyword "
     f"having sum(installs) >= 1000 order by sum({USD}) / sum(installs) limit 1) t", "set"),
    ("at-u-private-men", "How many installs came from men aged 55+ on Sprout - JP - Search on 2026-10-01?", None, "none"),
    ("at-u-private-users", "List the users who tapped the keyword 'ynab' on 2026-10-25.", None, "none"),
    ("at-u-private-day", "Break down Trailhead - GB - Search's installs on 2026-10-26 by age band and gender.", None, "none"),
    ("at-u-iap", "How much in-app purchase revenue did these campaigns drive?", None, "none"),
    ("at-u-competitor-bids", "How much are competitors bidding on the keyword 'alltrails'?", None, "none"),
]


def _plain(v):
    return float(v) if isinstance(v, Decimal) else v


def build(dsn: str = "host=/tmp dbname=adsdemo user=odoo") -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for split, questions in (("dev", DEV), ("test", TEST)):
            rows = []
            for qid, text, sql, kind in questions:
                answer = None
                if sql:
                    cur.execute(sql)
                    answer = _plain(cur.fetchone()[0])
                rows.append({"id": qid, "question": text, "answer": answer, "answer_type": kind, "answer_sql": sql})
            with open(OUT / f"ads-{split}.jsonl", "w") as f:
                for r in rows:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
                    print(split, r["id"], r["answer"])


if __name__ == "__main__":
    build()
