"""Build the finance question set from the warehouse.

Each question is about one customer, comes with that customer's records as
the model would get them from retrieval, and has an answer computed by SQL
over the warehouse, never by a model.

The records are given as they sit in Odoo, mess included: an edited invoice
appears in every version the loader saw, a duplicated customer appears under
both names, and euro invoices carry both currencies. A model that adds up the
wrong rows gets the wrong number.

Splits are by customer: no customer appears in both dev and test, so a score
cannot come from having seen the same company's records before. The split is
a hash of the customer key, so it never changes when questions are added.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import psycopg

AS_OF = "2026-10-27"
OUT = Path(__file__).parent / "data"


def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def split_of(customer_key: int) -> str:
    """test for about a third of customers, dev for the rest; stable forever."""
    return "test" if int(hashlib.sha256(f"cust-{customer_key}".encode()).hexdigest(), 16) % 3 == 0 else "dev"


def messy_tags(ctx: dict) -> list[str]:
    """Which source problems this customer's records contain, for per-slice scores."""
    tags = []
    ids = [i["invoice_id"] for i in ctx["invoices"]]
    if len(ids) != len(set(ids)):
        tags.append("edited_invoice")
    if len({i["customer_as_entered"] for i in ctx["invoices"]}) > 1:
        tags.append("duplicate_customer")
    if any(i["currency"] != "USD" for i in ctx["invoices"]):
        tags.append("foreign_currency")
    if any(i["kind"] == "out_refund" for i in ctx["invoices"]):
        tags.append("credit_note")
    return tags


def context_for(cur, customer_key: int) -> dict:
    """The customer's Odoo records, as retrieval would return them."""
    partners = [r["partner_id"] for r in _rows(
        cur, "select partner_id from intermediate.int_customers where customer_key = %s", (customer_key,))]
    invoices = _rows(cur, """
        select odoo_id as invoice_id, write_date as version_saved_at,
               payload->>'name' as invoice, payload->'partner_id'->>1 as customer_as_entered,
               payload->>'move_type' as kind, payload->>'state' as state,
               payload->>'invoice_date' as invoice_date, payload->>'invoice_date_due' as due_date,
               payload->'currency_id'->>1 as currency,
               (payload->>'amount_total')::numeric as amount,
               (payload->>'amount_total_signed')::numeric as amount_usd,
               (payload->>'amount_residual_signed')::numeric as still_owed_usd
        from raw.account_move
        where (payload->'partner_id'->>0)::int = any(%s)
          and payload->>'move_type' in ('out_invoice', 'out_refund')
          and payload->>'state' = 'posted'
        order by odoo_id, write_date""", (partners,))
    payments = _rows(cur, """
        select payment_ref as payment, payment_date, currency, amount as amount_usd, invoice_ids
        from staging.stg_payments where partner_id = any(%s) and state = 'posted'
        order by payment_date""", (partners,))
    return {
        "as_of": AS_OF,
        "invoices": [{k: _plain(v) for k, v in r.items()} for r in invoices],
        "payments": [{k: _plain(v) for k, v in r.items()} for r in payments],
    }


QUESTIONS = [
    # (template id, question, answer sql returning one value, answer type)
    ("billed", "How much has {name} been billed in total, in US dollars, net of credit notes?",
     "select coalesce(invoiced, 0) from marts.revenue_by_customer where customer_key = %s", "money"),
    ("owed", "How much does {name} still owe, in US dollars?",
     "select coalesce(open, 0) from marts.revenue_by_customer where customer_key = %s", "money"),
    ("late30", "As of {as_of}, how much of what {name} owes is more than 30 days past its due date, in US dollars?",
     "select coalesce(sum(amount_open), 0) from intermediate.int_invoices "
     "where customer_key = %s and days_past_due > 30", "money"),
    ("unpaid_refs", "Which of {name}'s invoices are not fully paid? List the invoice numbers.",
     "select coalesce(array_agg(invoice_ref order by invoice_ref), '{}') from intermediate.int_invoices "
     "where customer_key = %s and amount_open <> 0", "set"),
]


def build(dsn: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = {"dev": [], "test": []}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        customers = _rows(cur, "select customer_key, customer_name from marts.revenue_by_customer "
                               "order by customer_key")
        runs = _rows(cur, "select run_id from raw.load_runs where status = 'ok' order by started_at")
        for c in customers:
            ctx = context_for(cur, c["customer_key"])
            split = split_of(c["customer_key"])
            for tid, text, sql, kind in QUESTIONS:
                cur.execute(sql, (c["customer_key"],))
                answer = _plain(cur.fetchone()[0])
                rows[split].append({
                    "id": f"{tid}-{c['customer_key']}",
                    "customer_key": c["customer_key"],
                    "question": text.format(name=c["customer_name"], as_of=AS_OF),
                    "context": ctx,
                    "answer": answer,
                    "answer_type": kind,
                    "answer_sql": sql.replace("%s", str(c["customer_key"])),
                    "messy": messy_tags(ctx),
                })
    for split, items in rows.items():
        with open(OUT / f"{split}.jsonl", "w") as f:
            for r in items:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        # The same dev rows as chat examples, ready for fine-tuning; test is never exported.
        if split == "dev":
            with open(OUT / "dev.chat.jsonl", "w") as f:
                for r in items:
                    f.write(json.dumps({"messages": [
                        {"role": "user", "content": prompt(r)},
                        {"role": "assistant", "content": json.dumps({"answer": r["answer"]})},
                    ]}) + "\n")
    dev_keys = {r["customer_key"] for r in rows["dev"]}
    test_keys = {r["customer_key"] for r in rows["test"]}
    assert not dev_keys & test_keys, "a customer is in both splits"
    card = {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "warehouse_load_runs": [r["run_id"] for r in runs],
        "questions": {k: len(v) for k, v in rows.items()},
        "customers": {"dev": len(dev_keys), "test": len(test_keys), "in_both": 0},
        "templates": [q[0] for q in QUESTIONS],
        "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(OUT.glob("*.jsonl"))},
    }
    (OUT / "card.json").write_text(json.dumps(card, indent=2) + "\n")
    return card


def prompt(row: dict) -> str:
    return (
        "You are a finance assistant. Answer from the records below only.\n"
        "Records come straight from the company's ERP. An invoice can appear more than once if it "
        "was edited: each row is a saved version, and only the newest version counts. The same "
        "customer can appear under more than one spelling. Amounts in amount_usd and "
        "still_owed_usd are already in US dollars.\n\n"
        f"Records (as of {row['context']['as_of']}):\n{json.dumps(row['context'])}\n\n"
        f"Question: {row['question']}\n"
        'Reply with JSON only: {"answer": <number>} for an amount, or {"answer": [<invoice numbers>]} for a list.'
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", default=os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo"))
    print(json.dumps(build(p.parse_args().dsn), indent=2))
