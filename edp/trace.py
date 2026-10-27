"""Trace a number in a finance table back to the Odoo records behind it.

Every mart row carries the Odoo ids it was built from. This expands them into
the records themselves, with the exact version each total used, and checks
that the records add up to the number. A trace that does not add up says so.
"""

from decimal import Decimal

TOL = Decimal("0.01")


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _money(x) -> str:
    return "-" if x is None else f"{x:,.2f}"


def _table(rows, cols) -> str:
    if not rows:
        return "  (none)"
    cells = [[str(r[c]) if not isinstance(r[c], Decimal) else _money(r[c]) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    line = lambda vals: "  " + "  ".join(v.ljust(w) for v, w in zip(vals, widths))
    return "\n".join([line(cols), line(["-" * w for w in widths]), *map(line, cells)])


INVOICE_SQL = """
select i.invoice_ref as invoice, i.invoice_date as dated, i.due_date as due,
       i.days_past_due as days_late, i.currency, i.amount_in_invoice_currency as in_currency,
       i.amount as usd, i.amount_paid as paid, i.amount_open as open,
       o.order_ref as "order", d.deal_name as deal, i.source_ref
from intermediate.int_invoices i
left join staging.stg_orders o on o.order_id = i.order_id
left join staging.stg_deals d on d.deal_id = i.deal_id
where i.invoice_id = any(%s) order by i.invoice_date, i.invoice_id
"""

PAYMENT_SQL = """
select payment_ref as payment, payment_date as dated, currency, amount as usd,
       array_to_string(invoice_ids, ',') as invoice_ids,
       'account.payment/' || payment_id || '@' || to_char(source_version, 'YYYY-MM-DD"T"HH24:MI:SS') as source_ref
from staging.stg_payments where payment_id = any(%s) order by payment_date, payment_id
"""

INV_COLS = ["invoice", "dated", "due", "currency", "in_currency", "usd", "paid", "open", "order", "deal", "source_ref"]


def _check(label, claimed, parts) -> str:
    total = sum((p or Decimal(0)) for p in parts)
    ok = abs((claimed or Decimal(0)) - total) <= TOL
    return f"  {label}: {_money(claimed)} = sum of the records above ({_money(total)}) {'OK' if ok else 'DOES NOT ADD UP'}"


def _customer(conn, table, key):
    found = _rows(conn, f"select * from marts.{table} where customer_name ilike %s", (f"%{key}%",))
    if len(found) != 1:
        names = ", ".join(r["customer_name"] for r in found) or "no match"
        raise SystemExit(f"'{key}' matches {len(found)} customers in {table}: {names}")
    return found[0]


def trace(conn, table: str, key: str) -> str:
    out = []
    if table == "revenue_by_customer":
        r = _customer(conn, table, key)
        out.append(f"{r['customer_name']}: billed {_money(r['invoiced'])}, paid {_money(r['paid'])}, "
                   f"open {_money(r['open'])} (USD)")
        merged = _rows(conn, "select partner_id from intermediate.int_customers where customer_key = %s "
                             "order by 1", (r["customer_key"],))
        if len(merged) > 1:
            out.append(f"  merged from Odoo partners {[m['partner_id'] for m in merged]}")
        invs = _rows(conn, INVOICE_SQL, (r["source_invoice_ids"],))
        out += ["", "Invoices", _table(invs, INV_COLS)]
        pays = _rows(conn, PAYMENT_SQL, (r["source_payment_ids"] or [],))
        out += ["", "Payments", _table(pays, ["payment", "dated", "currency", "usd", "invoice_ids", "source_ref"]), ""]
        out.append(_check("billed", r["invoiced"], [i["usd"] for i in invs]))
        out.append(_check("open", r["open"], [i["open"] for i in invs]))
        out.append(_check("paid", r["paid"], [p["usd"] for p in pays]))
    elif table == "receivables_aging":
        r = _customer(conn, table, key)
        out.append(f"{r['customer_name']}: owes {_money(r['total_open'])} (USD) as of the aging date")
        out.append("  not yet due {}, 1-30 late {}, 31-60 {}, 61-90 {}, over 90 {}".format(
            *(_money(r[c]) for c in ["not_yet_due", "late_1_30", "late_31_60", "late_61_90", "late_over_90"])))
        invs = _rows(conn, INVOICE_SQL, (r["source_invoice_ids"],))
        out += ["", "Open invoices", _table(invs, ["invoice", "due", "days_late", "usd", "paid", "open", "order", "source_ref"]), ""]
        out.append(_check("owed", r["total_open"], [i["open"] for i in invs]))
    else:
        found = _rows(conn, "select * from marts.deal_to_cash where deal_id = %s", (int(key),))
        if not found:
            raise SystemExit(f"no won deal with id {key}")
        r = found[0]
        out.append(f"Deal {r['deal_id']} '{r['deal_name']}' for {r['customer_name']}: {r['status']}")
        out.append(f"  expected {_money(r['expected_revenue'])}, ordered {_money(r['ordered'])}, "
                   f"invoiced {_money(r['invoiced'])}, paid {_money(r['paid'])}, open {_money(r['open'])}")
        orders = _rows(conn, "select order_ref as \"order\", order_date as dated, state, amount_total as amount, "
                             "'sale.order/' || order_id as source_ref from staging.stg_orders "
                             "where order_id = any(%s) order by 1", (r["source_order_ids"],))
        out += ["", "Orders", _table(orders, ["order", "dated", "state", "amount", "source_ref"])]
        invs = _rows(conn, INVOICE_SQL, (r["source_invoice_ids"],))
        out += ["", "Invoices", _table(invs, ["invoice", "dated", "due", "usd", "paid", "open", "source_ref"]), ""]
        out.append(_check("ordered", r["ordered"], [o["amount"] for o in orders]))
        out.append(_check("invoiced", r["invoiced"], [i["usd"] for i in invs]))
    return "\n".join(out)
