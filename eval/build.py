"""Build the finance question set from the warehouse.

Each question is about one customer, comes with that customer's records as
the model would get them from retrieval, and has an answer computed by SQL
over the warehouse, never by a model.

The records are given as they sit in Odoo, mess included: an edited invoice
appears in every version the loader saw, a duplicated customer appears under
both names, and euro invoices carry both currencies. A model that adds up the
wrong rows gets the wrong number.

Splits are by customer: train, dev and test never share a customer, so a score
cannot come from having seen the same company's records before. The split is
a hash of the customer key, so it never changes when questions are added.

Every question exists in two prompt variants: "hints" tells the model how the
ERP records are messy, "plain" does not. The gap between the two is what the
rules are worth. Only train is exported as chat examples for fine-tuning;
dev and test never leave this folder. DATASET.md, the dataset card, is written
on every build with the leakage checks.
"""

import argparse
import collections
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


SPLITS = ("train", "dev", "test")


def split_of(customer_key: int) -> str:
    """About 30% of customers in test, 20% in dev, 50% in train; stable forever."""
    bucket = int(hashlib.sha256(f"cust-{customer_key}".encode()).hexdigest(), 16) % 10
    return "test" if bucket < 3 else "dev" if bucket < 5 else "train"


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
    if any(d["stage"] == "Won" and not any(o["deal_id"] == d["deal_id"] for o in ctx["sales_orders"])
           for d in ctx["deals"]):
        tags.append("won_deal_without_order")
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
    deals = _rows(cur, """
        select deal_id, deal_name as deal, stage, closed_on from staging.stg_deals
        where partner_id = any(%s) order by deal_id""", (partners,))
    orders = _rows(cur, """
        select order_ref as sales_order, deal_id, state, amount_total as amount, invoice_ids
        from staging.stg_orders where partner_id = any(%s) order by order_id""", (partners,))
    return {
        "as_of": AS_OF,
        "deals": [{k: _plain(v) for k, v in r.items()} for r in deals],
        "sales_orders": [{k: _plain(v) for k, v in r.items()} for r in orders],
        "invoices": [{k: _plain(v) for k, v in r.items()} for r in invoices],
        "payments": [{k: _plain(v) for k, v in r.items()} for r in payments],
    }


# The deal each customer is asked about: their most recently closed won deal.
DEAL_SQL = ("select deal_id, deal_name from marts.deal_to_cash d join staging.stg_deals s using (deal_id, deal_name) "
            "where d.customer_key = %s order by s.closed_on desc nulls last, deal_id desc limit 1")

QUESTIONS = [
    # (template id, question, answer sql returning one value, answer type)
    ("billed", "How much has {name} been billed in total, in US dollars, net of credit notes?",
     "select coalesce(invoiced, 0) from marts.revenue_by_customer where customer_key = %s", "money"),
    ("owed", "How much does {name} still owe, in US dollars?",
     "select coalesce(open, 0) from marts.revenue_by_customer where customer_key = %s", "money"),
    ("late30", "As of {as_of}, how much of what {name} owes is more than 30 days past its due date, in US dollars?",
     "select coalesce(sum(amount_open), 0) from intermediate.int_invoices "
     "where customer_key = %s and days_past_due > 30", "money"),
    ("unpaid_refs", "Which of {name}'s invoices (not credit notes) are not fully paid? List the invoice numbers.",
     "select coalesce(array_agg(invoice_ref order by invoice_ref), '{}') from intermediate.int_invoices "
     "where customer_key = %s and move_type = 'out_invoice' and amount_open <> 0", "set"),
    ("oldest_late", "As of {as_of}, how many days past its due date is {name}'s most overdue unpaid invoice? "
     "Answer 0 if nothing unpaid is past due.",
     "select coalesce(max(days_past_due), 0) from intermediate.int_invoices "
     "where customer_key = %s and move_type = 'out_invoice' and amount_open > 0", "count"),
    ("deal_collected", "How much has been collected so far on {name}'s deal \"{deal}\", in US dollars?",
     "select coalesce(paid, 0) from marts.deal_to_cash where deal_id = %s", "money"),
]


def build(dsn: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = {s: [] for s in SPLITS}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        customers = _rows(cur, "select customer_key, customer_name from marts.revenue_by_customer "
                               "order by customer_key")
        runs = _rows(cur, "select run_id from raw.load_runs where status = 'ok' order by started_at")
        for c in customers:
            ctx = context_for(cur, c["customer_key"])
            split = split_of(c["customer_key"])
            cur.execute(DEAL_SQL, (c["customer_key"],))
            deal = cur.fetchone()
            for tid, text, sql, kind in QUESTIONS:
                if tid == "deal_collected" and deal is None:
                    continue
                key = deal[0] if tid == "deal_collected" else c["customer_key"]
                cur.execute(sql, (key,))
                answer = _plain(cur.fetchone()[0])
                rows[split].append({
                    "id": f"{tid}-{c['customer_key']}",
                    "customer_key": c["customer_key"],
                    "question": text.format(name=c["customer_name"], as_of=AS_OF,
                                            deal=deal[1] if deal else ""),
                    "context": ctx,
                    "answer": answer,
                    "answer_type": kind,
                    "answer_sql": sql.replace("%s", str(key)),
                    "messy": messy_tags(ctx),
                })
    for old in OUT.glob("*.jsonl"):
        old.unlink()
    for split, items in rows.items():
        with open(OUT / f"{split}.jsonl", "w") as f:
            for r in items:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    # Train only, as chat examples for fine-tuning, with the plain prompt: the
    # rules are what the examples teach. Dev and test are never exported.
    with open(OUT / "train.chat.jsonl", "w") as f:
        for r in rows["train"]:
            f.write(json.dumps({"messages": [
                {"role": "user", "content": prompt(r, hints=False)},
                {"role": "assistant", "content": json.dumps({"answer": r["answer"]})},
            ]}) + "\n")
    checks = leakage_checks(rows)
    assert all(c["passed"] for c in checks), checks
    card = {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "warehouse_load_runs": len(runs),
        "last_load_run": runs[-1]["run_id"] if runs else None,
        "questions": {k: len(v) for k, v in rows.items()},
        "customers": {k: len({r["customer_key"] for r in v}) for k, v in rows.items()},
        "templates": [q[0] for q in QUESTIONS],
        "zero_answers": zero_share(rows),
        "messy": {k: dict(sorted(collections.Counter(t for r in v for t in r["messy"]).items()))
                  for k, v in rows.items()},
        "leakage_checks": checks,
        "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(OUT.glob("*.jsonl"))},
    }
    (OUT / "card.json").write_text(json.dumps(card, indent=2) + "\n")
    (OUT.parent / "DATASET.md").write_text(card_markdown(card))
    return card


def _empty(answer) -> bool:
    return answer in (0, 0.0, [], None)


def zero_share(rows: dict) -> dict:
    """Share of questions whose answer is 0 or an empty list, per template: a model answering
    0 to everything scores this much, so the share must stay well under the real scores."""
    out = {}
    for tid, *_ in QUESTIONS:
        items = [r for v in rows.values() for r in v if r["id"].startswith(tid + "-")]
        out[tid] = f"{sum(_empty(r['answer']) for r in items)}/{len(items)}"
    return out


def leakage_checks(rows: dict) -> list[dict]:
    """What would let a score come from memory instead of reading the records."""
    keys = {s: {r["customer_key"] for r in v} for s, v in rows.items()}
    qs = {s: {r["question"] for r in v} for s, v in rows.items()}
    ctx = {s: {hashlib.sha256(json.dumps(r["context"], sort_keys=True).encode()).hexdigest() for r in v}
           for s, v in rows.items()}
    invoices = {s: {i["invoice_id"] for r in v for i in r["context"]["invoices"]} for s, v in rows.items()}
    pairs = [("train", "dev"), ("train", "test"), ("dev", "test")]

    def check(name, sets):
        shared = {f"{a}/{b}": len(sets[a] & sets[b]) for a, b in pairs}
        return {"check": name, "shared": shared, "passed": not any(shared.values())}

    return [check("customers in more than one split", keys),
            check("question text in more than one split", qs),
            check("identical record sets in more than one split", ctx),
            check("invoices in more than one split", invoices)]


def card_markdown(card: dict) -> str:
    q, c = card["questions"], card["customers"]
    lines = [
        "# Dataset card: ERP finance questions",
        "",
        "Questions a finance team asks about one customer, with that customer's records exactly as they",
        "sit in the ERP (edited invoices in every saved version, a customer under two spellings, euro",
        "invoices, credit notes, a won deal with no order). Every answer is computed by SQL over the",
        "warehouse, never by a model, and the SQL is stored next to it.",
        "",
        f"Built {card['built_at']} from {card['warehouse_load_runs']} warehouse loads "
        f"(last `{card['last_load_run']}`). Figures are as of {card['as_of']}.",
        "",
        "| Split | Customers | Questions | Use |",
        "|---|---|---|---|",
        f"| train | {c['train']} | {q['train']} | exported to `data/train.chat.jsonl` for fine-tuning |",
        f"| dev | {c['dev']} | {q['dev']} | for changing prompts and code; score freely |",
        f"| test | {c['test']} | {q['test']} | held out; score once per frozen version |",
        "",
        "## Question templates",
        "",
        "| Template | Asks | Answer | Zero or empty answers |",
        "|---|---|---|---|",
    ]
    for tid, text, _, kind in QUESTIONS:
        lines.append(f"| `{tid}` | {text.split('?')[0].format(name='<customer>', as_of=card['as_of'], deal='<deal>')}? "
                     f"| {kind} | {card['zero_answers'][tid]} |")
    lines += [
        "",
        "Money is right within one cent, a count must be exact, and a list must be exactly the right set.",
        "Grading is code (`eval/run.py grade`); no model grades another model.",
        "",
        "## Messy records per split",
        "",
        "Each question is tagged with the problems its customer's records contain, so scores can be",
        "read per problem.",
        "",
        "| Problem | " + " | ".join(SPLITS) + " |",
        "|---|" + "---|" * len(SPLITS),
    ]
    tags = sorted({t for s in SPLITS for t in card["messy"][s]})
    for t in tags:
        lines.append(f"| {t} | " + " | ".join(str(card["messy"][s].get(t, 0)) for s in SPLITS) + " |")
    lines += [
        "",
        "## Leakage checks",
        "",
        "The build fails if any of these finds an overlap.",
        "",
        "| Check | train/dev | train/test | dev/test | Result |",
        "|---|---|---|---|---|",
    ]
    for ch in card["leakage_checks"]:
        sh = ch["shared"]
        lines.append(f"| {ch['check']} | {sh['train/dev']} | {sh['train/test']} | {sh['dev/test']} | "
                     f"{'pass' if ch['passed'] else 'FAIL'} |")
    lines += [
        "",
        "## Known limits",
        "",
        "- The business is simulated (a seeded year of Odoo activity), so the customers are few and the",
        "  mess is planted, not found. The same build runs unchanged on a real Odoo warehouse.",
        "- The splits are by customer only. Train, dev and test share the same company, chart of",
        "  accounts and product catalogue.",
        "",
        "## Files",
        "",
        "| File | sha256 |",
        "|---|---|",
    ]
    lines += [f"| `data/{n}` | `{h[:16]}…` |" for n, h in card["sha256"].items()]
    return "\n".join(lines) + "\n"


HINTS = (
    "Records come straight from the company's ERP. An invoice can appear more than once if it "
    "was edited: each row is a saved version, and only the newest version counts. The same "
    "customer can appear under more than one spelling. Amounts in amount_usd and "
    "still_owed_usd are already in US dollars, and are negative on credit notes (kind out_refund). "
    "A deal's money is on the invoices of its sales orders.\n"
)


def prompt(row: dict, hints: bool = True) -> str:
    return (
        "You are a finance assistant. Answer from the records below only.\n"
        + (HINTS if hints else "") + "\n"
        f"Records (as of {row['context']['as_of']}):\n{json.dumps(row['context'])}\n\n"
        f"Question: {row['question']}\n"
        'Reply with JSON only: {"answer": <number>} for an amount or a count, '
        'or {"answer": [<invoice numbers>]} for a list.'
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", default=os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo"))
    print(json.dumps(build(p.parse_args().dsn), indent=2))
