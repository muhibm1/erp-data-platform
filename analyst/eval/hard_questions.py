"""The harder question set, written before any agent was run on it.

Each question needs more than one lookup: totals across customers, joins from deals
to invoices, the merged customer, credit notes, or history. Five cannot be answered
from the warehouse at all; the right reply to those is "not answerable".

Answers are computed by the SQL here, never written by hand or by a model.
`python -m analyst.eval.hard_questions` writes `hard-dev.jsonl` and `hard-test.jsonl`
with each answer filled in. Dev questions may be looked at while building the agent;
test questions are scored once, after it is frozen.
"""

import json
from decimal import Decimal
from pathlib import Path

import psycopg

OUT = Path(__file__).parent / "data"

# (id, question, gold sql returning one value, answer type)
# Types: money (USD, within one cent), number (within 0.1), count (exact),
# set (names or references, order ignored), text (one value), none (not answerable).
DEV_QUESTIONS = [
    ("d-not-yet-due", "In total, how much is owed to us that is not yet due, in US dollars?",
     "select sum(coalesce(not_yet_due, 0)) from marts.receivables_aging", "money"),
    ("d-owe-nothing", "How many customers owe us nothing?",
     "select count(*) from marts.revenue_by_customer where coalesce(open, 0) = 0", "count"),
    ("d-euro-customers", "Which customers have been billed in euros?",
     "select array_agg(distinct customer_name) from intermediate.int_invoices where currency = 'EUR'", "set"),
    ("d-lost-value", "What is the total expected revenue of the deals we lost, in US dollars?",
     "select sum(expected_revenue) from staging.stg_deals where is_lost", "money"),
    ("d-paid-deals", "How many won deals are fully paid?",
     "select count(*) from marts.deal_to_cash where status = 'paid'", "count"),
    ("d-avg-invoice", "What is the average invoice amount in US dollars, not counting credit notes?",
     "select round(avg(amount), 2) from intermediate.int_invoices where move_type = 'out_invoice'", "money"),
    ("d-top-billed", "Which customer has been billed the most, net of credit notes?",
     "select array_agg(customer_name) from (select customer_name from marts.revenue_by_customer "
     "order by invoiced desc nulls last limit 1) t", "set"),
    ("d-u-product", "Which product sold the most units?", None, "none"),
]

QUESTIONS = [
    ("total-owed", "How much do all customers owe us in total right now, in US dollars?",
     "select sum(coalesce(open, 0)) from marts.revenue_by_customer", "money"),
    ("total-paid", "How much have customers paid us in total, in US dollars?",
     "select sum(coalesce(paid, 0)) from marts.revenue_by_customer", "money"),
    ("total-billed", "What is the total amount billed to all customers, in US dollars, net of credit notes?",
     "select sum(coalesce(invoiced, 0)) from marts.revenue_by_customer", "money"),
    ("over-90", "How much money owed to us is more than 90 days past due, in US dollars?",
     "select sum(coalesce(late_over_90, 0)) from marts.receivables_aging", "money"),
    ("over-60-customers", "How many customers have money more than 60 days past due?",
     "select count(*) from marts.receivables_aging where coalesce(late_61_90, 0) + coalesce(late_over_90, 0) > 0", "count"),
    ("top3-owed", "Which three customers owe us the most? List their names.",
     "select array_agg(customer_name) from (select customer_name from marts.revenue_by_customer "
     "order by coalesce(open, 0) desc limit 3) t", "set"),
    ("worst-90", "Which customer has the most money more than 90 days past due?",
     "select array_agg(customer_name) from (select customer_name from marts.receivables_aging "
     "order by late_over_90 desc nulls last limit 1) t", "set"),
    ("collected-pct", "What percentage of the net amount we have billed has been collected? Answer as a percentage from 0 to 100.",
     "select round(100.0 * sum(coalesce(paid, 0)) / sum(coalesce(invoiced, 0)), 1) from marts.revenue_by_customer", "number"),
    ("billed-customers", "How many customers have been billed at least once?",
     "select count(*) from marts.revenue_by_customer where invoice_count > 0", "count"),
    ("won-no-order", "How many won deals never got a sales order?",
     "select count(*) from marts.deal_to_cash where status = 'won, no order'", "count"),
    ("won-no-order-value", "What is the total expected revenue of won deals that never got a sales order, in US dollars?",
     "select sum(expected_revenue) from marts.deal_to_cash where status = 'won, no order'", "money"),
    ("ordered-not-invoiced", "How much has been ordered on won deals that have not been invoiced yet, in US dollars?",
     "select sum(ordered) from marts.deal_to_cash where status = 'ordered, not invoiced'", "money"),
    ("partly-paid-deals", "How many won deals are only partly paid?",
     "select count(*) from marts.deal_to_cash where status = 'partly paid'", "count"),
    ("paid-share", "What percentage of won deals are fully paid? Answer as a percentage from 0 to 100.",
     "select round(100.0 * count(*) filter (where status = 'paid') / count(*), 1) from marts.deal_to_cash", "number"),
    ("won-open-total", "Across all won deals, how much is invoiced but still unpaid, in US dollars?",
     "select sum(open) from marts.deal_to_cash", "money"),
    ("lost-deals", "How many deals were lost?",
     "select count(*) from staging.stg_deals where is_lost", "count"),
    ("win-rate", "Of the deals that were either won or lost, what percentage were won? Answer as a percentage from 0 to 100.",
     "select round(100.0 * count(*) filter (where is_won) / count(*) filter (where is_won or is_lost), 1) "
     "from staging.stg_deals", "number"),
    ("best-month", "Which month had the highest net billing in US dollars? Answer as YYYY-MM.",
     "select to_char(invoice_date, 'YYYY-MM') from intermediate.int_invoices "
     "group by 1 order by sum(amount) desc nulls last limit 1", "text"),
    ("euro-invoices", "How many invoices were billed in euros?",
     "select count(*) from intermediate.int_invoices where currency = 'EUR' and move_type = 'out_invoice'", "count"),
    ("credit-notes", "How many credit notes have been issued?",
     "select count(*) from intermediate.int_invoices where move_type = 'out_refund'", "count"),
    ("credit-note-total", "What is the total value of all credit notes issued, in US dollars, as a positive number?",
     "select -sum(amount) from intermediate.int_invoices where move_type = 'out_refund'", "money"),
    ("paid-invoices", "How many invoices (not counting credit notes) are fully paid?",
     "select count(*) from intermediate.int_invoices where move_type = 'out_invoice' and amount_open = 0", "count"),
    ("edited-invoices", "How many invoices were changed after they were first loaded, so that the warehouse holds more than one version of them?",
     "select count(*) from (select invoice_id from staging.stg_invoice_versions group by 1 having count(*) > 1) t", "count"),
    ("merged-customers", "Which customers were entered more than once in the ERP and had to be merged? Give the customer names.",
     "select array_agg(distinct customer_name) from intermediate.int_customers where merged_into_other", "set"),
    ("avg-open", "Among customers who still owe money, what is the average amount owed, in US dollars?",
     "select round(avg(open), 2) from marts.revenue_by_customer where open > 0", "money"),
    ("u-credit-score", "What is Northwind Logistics' credit score?", None, "none"),
    ("u-salesperson", "Which salesperson closed the most deals this year?", None, "none"),
    ("u-margin", "What was our gross margin last quarter?", None, "none"),
    ("u-tickets", "How many support tickets has Acme Corporation opened?", None, "none"),
    ("u-forecast", "How much will we bill next month?", None, "none"),
]


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def build(dsn: str = "host=/tmp dbname=warehouse user=odoo") -> list[dict]:
    everything = []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for split, questions in (("dev", DEV_QUESTIONS), ("test", QUESTIONS)):
            rows = []
            for qid, text, sql, kind in questions:
                answer = None
                if sql:
                    cur.execute(sql)
                    answer = _plain(cur.fetchone()[0])
                rows.append({"id": qid, "question": text, "answer": answer, "answer_type": kind,
                             "answer_sql": sql})
            OUT.mkdir(parents=True, exist_ok=True)
            with open(OUT / f"hard-{split}.jsonl", "w") as f:
                for r in rows:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
            everything += rows
    return everything


if __name__ == "__main__":
    for r in build():
        print(r["id"], r["answer"])
