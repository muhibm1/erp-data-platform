"""Questions that name things the way a person would, written before any agent was run on them.

A marketer asks about "the Mint keyword", "the German campaign" or "the subtitle test". The
data says `mint budget`, `Ledgerly - DE - Search` and `Product page: shorter subtitle`, and
the domain notes do not list keywords or experiment names. To answer, the agent has to look
the name up first. Three questions name something that does not exist; the right reply to
those is "not answerable", not 0.

Answers come from the gold SQL below, never written by hand or by a model.

  python -m analyst.eval.explore_questions   # writes data/explore-dev.jsonl and explore-test.jsonl
"""

import json
from decimal import Decimal
from pathlib import Path

import psycopg

from .ads_questions import JOIN, LAST7, USD, _ci

OUT = Path(__file__).parent / "data"
EXP = "reporting.experiment_daily e join reporting.experiments x using (experiment_id)"

DEV = [
    ("xd-komoot-gb", "How many installs did our Komoot keyword bring in for Trailhead in the UK over the whole period?",
     f"select sum(installs) from {JOIN} where k.keyword = 'komoot' and c.campaign_name = 'Trailhead - GB - Search'",
     "count"),
    ("xd-shots-taps", "How many taps did the screenshots experiment get in total?",
     f"select sum(taps) from {EXP} where x.experiment_name = 'Product page: new screenshots'", "count"),
    ("xd-plant-id-spend", "What did the plant identifier keywords cost across all campaigns over the whole period, "
     "in US dollars?",
     f"select round(sum({USD}), 2) from {JOIN} where k.keyword = 'plant identifier'", "money"),
    ("xd-german-last", "On what day did the German campaign last spend money? Give the date as YYYY-MM-DD.",
     f"select max(d.day)::text from {JOIN} where c.campaign_name = 'Ledgerly - DE - Search' and d.spend > 0",
     "text"),
    ("xd-none-habit", "How many installs did the 'habit tracker' keyword get?", None, "none"),
]

TEST = [
    ("xt-ynab-7d", "How many installs did the YNAB keywords get in the last 7 days, across all campaigns?",
     f"select sum(installs) from {JOIN} where k.keyword = 'ynab' and {LAST7}", "count"),
    ("xt-mint-spend", "How much did we spend on the Mint keyword over the whole period, in US dollars?",
     f"select round(sum({USD}), 2) from {JOIN} where k.keyword = 'mint budget'", "money"),
    ("xt-gaia-cpi", "What was the cost per install of the Gaia keyword in the US Trailhead campaign, in US dollars?",
     f"select round(sum({USD}) / sum(installs), 2) from {JOIN} "
     f"where k.keyword = 'gaia gps' and c.campaign_name = 'Trailhead - US - Search'", "money"),
    ("xt-hiking-gb-cpt", "What was the cost per tap of the hiking keywords in the UK Trailhead campaign, "
     "in US dollars?",
     f"select round(sum({USD}) / sum(taps), 2) from {JOIN} "
     f"where k.keyword like '%hiking%' and c.campaign_name = 'Trailhead - GB - Search'", "money"),
    ("xt-copilot-ttr", "What was the tap-through rate of the Copilot keyword in the US Ledgerly campaign, "
     "as a percentage?",
     f"select round(100.0 * sum(taps) / sum(impressions), 2) from {JOIN} "
     f"where k.keyword = 'copilot money' and c.campaign_name = 'Ledgerly - US - Search'", "number"),
    ("xt-jp-broad", "Which keywords in the Japanese Sprout campaign are broad match?",
     "select array_agg(k.keyword) from reporting.keywords k join reporting.ad_groups g using (ad_group_id) "
     "join reporting.campaigns c using (campaign_id) "
     "where c.campaign_name = 'Sprout - JP - Search' and k.match_type = 'broad'", "set"),
    ("xt-alltrails-7d", "How many installs did bidding on AllTrails bring Trailhead in America over the last 7 days?",
     f"select sum(installs) from {JOIN} "
     f"where k.keyword = 'alltrails' and c.campaign_name = 'Trailhead - US - Search' and {LAST7}", "count"),
    ("xt-subtitle-sig", "Was the subtitle test's result statistically significant? Answer yes or no.",
     _ci(2, "sig"), "text"),
    ("xt-shots-b-cr", "What was the conversion rate of variant B in the screenshots test, as a percentage?",
     f"select round(100.0 * sum(installs) / sum(taps), 2) from {EXP} "
     "where x.experiment_name = 'Product page: new screenshots' and e.variant = 'B'", "number"),
    ("xt-de-brand-cpi", "What did the Ledgerly brand keywords cost per install in Germany, in US dollars?",
     f"select round(sum({USD}) / sum(installs), 2) from {JOIN} "
     "where g.ad_group_name = 'Brand' and c.campaign_name = 'Ledgerly - DE - Search'", "money"),
    ("xt-plant-care-jp", "How much did the plant care keyword spend in Japan over the whole period, in yen?",
     f"select round(sum(d.spend), 2) from {JOIN} "
     "where k.keyword = 'plant care' and c.campaign_name = 'Sprout - JP - Search'", "money"),
    ("xt-not-running", "Which of our campaigns is not running right now?",
     "select array_agg(campaign_name) from reporting.campaigns where status <> 'running'", "set"),
    ("xt-offline-match", "Is the offline maps keyword exact or broad match in the UK campaign?",
     "select k.match_type from reporting.keywords k join reporting.ad_groups g using (ad_group_id) "
     "join reporting.campaigns c using (campaign_id) "
     "where k.keyword = 'offline maps' and c.campaign_name = 'Trailhead - GB - Search'", "text"),
    ("xt-none-notion", "How many installs did the 'notion' keyword get last week?", None, "none"),
    ("xt-none-icon", "Did the new app icon experiment beat the old icon?", None, "none"),
    ("xt-none-canada", "How much did the Canadian Trailhead campaign spend, in US dollars?", None, "none"),
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
            with open(OUT / f"explore-{split}.jsonl", "w") as f:
                for r in rows:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
                    print(split, r["id"], r["answer"])


if __name__ == "__main__":
    build()
