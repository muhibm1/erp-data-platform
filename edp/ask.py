"""Ask the warehouse: tools an AI agent uses to answer finance questions with sources.

An assistant that answers "how much does Northwind owe?" from a warehouse
needs two things a chat box does not give it: a safe way to read the data, and
a way to prove where each number came from. These tools give both.

- Every tool is read-only. `query` runs inside a read-only transaction with a
  time limit, over the modelled schemas only (never raw, never Odoo).
- Every row that comes from an Odoo record carries a source reference, such as
  `account.move/181@2026-10-27T09:21:59`: the model, the record id, and the
  exact saved version the number used.
- `check_citations` takes an answer's references and checks each one against
  the raw tables: that the record exists, that the version exists, and that it
  is the newest version. An agent can cite only what it was shown, and a
  person can check any answer without trusting the agent.

The same functions are served over MCP (`edp mcp`) and on the command line
(`edp tool <name> '<json arguments>'`), so any agent framework can use them.
"""

import datetime as dt
import inspect
import json
import re
from decimal import Decimal
from pathlib import Path

import psycopg

# Receivables are aged as of the dbt project's as_of date; the tools report it.
AS_OF = re.search(r'as_of:\s*"([\d-]+)"', (Path(__file__).resolve().parent.parent / "dbt" / "dbt_project.yml")
                  .read_text()).group(1)
QUERY_SCHEMAS = ("marts", "intermediate", "staging")
MAX_ROWS = 200
REF = re.compile(r"\b(account\.move|account\.payment|sale\.order)/(\d+)(?:@(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}))?")

PAYMENT_REF = ("'account.payment/' || payment_id || '@' || "
               "to_char(source_version, 'YYYY-MM-DD\"T\"HH24:MI:SS')")
ORDER_REF = ("'sale.order/' || order_id || '@' || "
             "to_char(source_version, 'YYYY-MM-DD\"T\"HH24:MI:SS')")


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args or None)  # no params: a literal % in agent SQL stays literal
        cols = [c.name for c in cur.description]
        return [{k: _plain(v) for k, v in zip(cols, r)} for r in cur.fetchall()]


def find_customers(conn, name: str) -> dict:
    """Customers whose name contains the text, with every Odoo partner merged into each."""
    found = _rows(conn, """
        select c.customer_key, max(c.customer_name) as customer_name,
               array_agg(c.partner_id || ': ' || p.partner_name order by c.partner_id) as odoo_partners
        from intermediate.int_customers c
        join staging.stg_partners p using (partner_id)
        where c.customer_key in (select c2.customer_key from intermediate.int_customers c2
                                 join staging.stg_partners p2 using (partner_id)
                                 where c2.customer_name ilike %s or p2.partner_name ilike %s)
        group by c.customer_key order by c.customer_key""", (f"%{name}%", f"%{name}%"))
    return {"customers": found,
            "note": "A customer can be several Odoo partners merged into one; always use customer_key."}


def customer_summary(conn, customer_key: int) -> dict:
    """Billed, paid and owed in USD, aging buckets, and the records behind them."""
    rev = _rows(conn, "select * from marts.revenue_by_customer where customer_key = %s", (customer_key,))
    if not rev:
        return {"found": False, "customer_key": customer_key}
    r = rev[0]
    aging = _rows(conn, """select not_yet_due, late_1_30, late_31_60, late_61_90, late_over_90, total_open
                           from marts.receivables_aging where customer_key = %s""", (customer_key,))
    inv = _rows(conn, "select source_ref from intermediate.int_invoices where customer_key = %s "
                      "order by invoice_id", (customer_key,))
    pay = _rows(conn, f"select {PAYMENT_REF} as source_ref from staging.stg_payments "
                      "where payment_id = any(%s) order by payment_id", (r["source_payment_ids"] or [],))
    return {
        "found": True, "customer_key": customer_key, "customer_name": r["customer_name"],
        "billed_usd": r["invoiced"], "paid_usd": r["paid"], "owed_usd": r["open"],
        "aging_usd": aging[0] if aging else {"total_open": 0},
        "aged_as_of": AS_OF,
        "sources": [x["source_ref"] for x in inv] + [x["source_ref"] for x in pay],
    }


def list_invoices(conn, customer_key: int, status: str = "all") -> dict:
    """The customer's posted invoices and credit notes, newest version of each, in USD."""
    where = {"all": "", "unpaid": "and amount_open <> 0",
             "overdue": "and amount_open <> 0 and days_past_due > 0"}[status]
    rows = _rows(conn, f"""
        select invoice_ref, move_type as kind, invoice_date, due_date, days_past_due, currency,
               amount_in_invoice_currency, amount as amount_usd, amount_paid as paid_usd,
               amount_open as open_usd, order_id, deal_id, source_ref
        from intermediate.int_invoices where customer_key = %s {where}
        order by invoice_date, invoice_id""", (customer_key,))
    return {"customer_key": customer_key, "status": status, "aged_as_of": AS_OF, "invoices": rows,
            "note": "kind out_invoice is an invoice, out_refund a credit note (negative amounts)."}


def list_deals(conn, customer_key: int) -> dict:
    """The customer's won deals, followed through orders and invoices to cash collected."""
    deals = _rows(conn, """
        select deal_id, deal_name, status, expected_revenue, ordered, invoiced, paid, open,
               source_order_ids, source_invoice_ids
        from marts.deal_to_cash where customer_key = %s order by deal_id""", (customer_key,))
    for d in deals:
        orders = _rows(conn, f"select {ORDER_REF} as source_ref from staging.stg_orders "
                             "where order_id = any(%s)", (d.pop("source_order_ids"),))
        invs = _rows(conn, "select source_ref from intermediate.int_invoices where invoice_id = any(%s)",
                     (d.pop("source_invoice_ids"),))
        d["sources"] = [o["source_ref"] for o in orders] + [i["source_ref"] for i in invs]
    return {"customer_key": customer_key, "won_deals": deals}


READER = "edp_reader"


def ensure_reader(conn) -> None:
    """A role that can only SELECT from the modelled schemas. `query` runs as this role, so a
    query cannot read raw tables, files on the server, or anything outside those schemas,
    whatever the agent writes. Default privileges keep it working after dbt rebuilds a table."""
    with conn.cursor() as cur:
        cur.execute("select 1 from pg_roles where rolname = %s", (READER,))
        if not cur.fetchone():
            cur.execute(f"create role {READER} nologin")
        for schema in QUERY_SCHEMAS:
            cur.execute(f"grant usage on schema {schema} to {READER}")
            cur.execute(f"grant select on all tables in schema {schema} to {READER}")
            cur.execute(f"alter default privileges in schema {schema} grant select on tables to {READER}")


def query(conn, sql: str) -> dict:
    """One read-only SELECT over the modelled schemas (marts, intermediate, staging), at most 200 rows,
    5 second limit. Rows that come from an Odoo record include its source_ref when selected."""
    text = sql.strip().rstrip(";")
    if ";" in text or not re.match(r"(?is)^\s*(select|with)\b", text):
        return {"ok": False, "error": "one SELECT statement only"}
    named = set(re.findall(r"(?i)\b(?:from|join)\s+([a-z_]+)\.", text))
    outside = sorted(s for s in named if s.lower() not in QUERY_SCHEMAS)
    if outside or re.search(r"(?i)\b(raw|pg_catalog|information_schema)\.", text):
        return {"ok": False, "error": f"only the schemas {list(QUERY_SCHEMAS)} can be queried"}
    try:
        with conn.transaction():
            conn.execute("set transaction read only")
            conn.execute("set local statement_timeout = '5s'")
            conn.execute(f"set local role {READER}")
            rows = _rows(conn, f"select * from ({text}) q limit {MAX_ROWS + 1}")
    except psycopg.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"}
    return {"ok": True, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}


def check_citations(conn, refs: list[str] | str) -> dict:
    """Checks each source reference against the raw tables: the record, the version, and that it is current."""
    found = REF.findall(refs if isinstance(refs, str) else " ".join(refs))
    results = []
    for model, rid, version in found:
        table = "raw." + model.replace(".", "_")
        ref = f"{model}/{rid}" + (f"@{version}" if version else "")
        latest = _rows(conn, f"select max(write_date) as v from {table} where odoo_id = %s", (int(rid),))[0]["v"]
        if latest is None:
            results.append({"ref": ref, "status": "unknown record"})
        elif not version:
            results.append({"ref": ref, "status": "no version given"})
        elif not _rows(conn, f"select 1 from {table} where odoo_id = %s and write_date = %s",
                       (int(rid), dt.datetime.fromisoformat(version))):
            results.append({"ref": ref, "status": "unknown version"})
        elif dt.datetime.fromisoformat(latest) != dt.datetime.fromisoformat(version):
            results.append({"ref": ref, "status": "superseded by a newer version", "newest": latest})
        else:
            results.append({"ref": ref, "status": "ok"})
    return {"checked": len(results), "all_ok": bool(results) and all(r["status"] == "ok" for r in results),
            "results": results}


TOOLS = {
    "find_customers": find_customers,
    "customer_summary": customer_summary,
    "list_invoices": list_invoices,
    "list_deals": list_deals,
    "query": query,
    "check_citations": check_citations,
}


def call(dsn: str, name: str, args: dict) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        ensure_reader(conn)
        return TOOLS[name](conn, **args)


def serve(dsn: str) -> None:
    """Serves the tools over MCP on stdio."""
    from mcp.server.mcpserver import MCPServer

    with psycopg.connect(dsn, autocommit=True) as conn:
        ensure_reader(conn)
    server = MCPServer("erp-warehouse")
    for name, fn in TOOLS.items():
        def make(fn=fn):
            def tool(**kwargs):
                with psycopg.connect(dsn, autocommit=True) as conn:
                    return json.loads(json.dumps(fn(conn, **kwargs), default=str))
            tool.__name__, tool.__doc__ = fn.__name__, fn.__doc__
            sig = inspect.signature(fn)
            tool.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])
            tool.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "conn"}
            return tool
        server.tool(name=name, description=fn.__doc__)(make())
    server.run(transport="stdio")
