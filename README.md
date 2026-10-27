# ERP data platform

**Gets a company's sales and accounting data out of its ERP, makes it trustworthy, and traces every finance number back to the records behind it.**

A company runs on Odoo, an open-source ERP (enterprise resource planning: the system that holds orders, invoices and the books) with a CRM built in (the system that tracks customers and deals). Finance wants three answers: what each customer was billed and has paid, who is late and by how much, and whether every deal sales won actually turned into cash. This project answers them, and can show its work for any number. Ask why Northwind Logistics owes $26,835.00, and you get the three invoices and two payments behind it, with their Odoo ids and the exact version of each, plus the note that Northwind exists twice in Odoo and was merged ([example](docs/examples/trace-northwind.txt)).

It also builds a question set an AI finance assistant can be judged on, with every answer computed from the data rather than written by hand or by a model.

## What it does

```
Odoo (CRM, Sales, Invoicing)
   │  edp extract: JSON-RPC, incremental, safe to rerun
   ▼
raw.*        every version of every record, deletes noted
   │  dbt
   ▼
staging → intermediate → marts
                          revenue_by_customer   billed, paid, open per customer
                          receivables_aging     what is owed, by days late
                          deal_to_cash          won deal → order → invoice → payment
   │
   ├─ edp trace   any number → the records behind it, checked to add up
   ├─ Fathom      lineage, owners, rules: what a change would break
   └─ eval/       finance questions with SQL answers, for judging a model
```

1. **Extract.** `edp extract` reads deals, customers, orders, invoices and payments through Odoo's API, the way you would at a client: no database access to the ERP needed. It fetches only what changed since the last load, so a rerun stores nothing new. A field the load needs going missing stops the load and names the field; a custom field a client adds is picked up automatically.
2. **Keep history.** Raw tables are append-only, one row per saved version of a record. When accounting reopens a posted invoice and changes it, both versions stay. When a record is deleted in Odoo, the load notices and records it.
3. **Model.** dbt turns raw records into three finance tables in US dollars. Every row carries the Odoo ids it was built from.
4. **Check.** `dbt build` runs 29 checks. Hard checks fail the build if the finance tables stop matching Odoo. Source-quality checks list the real-world mess: a customer entered twice, an invoice changed after posting, euro invoices, a deal marked won with no order, a deleted record. Each is handled in the models and listed for a person to fix at the source. See [docs/checks.md](docs/checks.md).
5. **Trace.** `edp trace` expands a number into its records and checks they add up. A test runs it on every customer and every won deal.
6. **Guard changes.** [Fathom](https://github.com/muhibm1/fathom) reads the dbt project for column-level lineage, owners and rules. Proposing to drop a column from the aging table returns BLOCK: it names month-end close and the collections queue as the reports that would break, and finance as the team that must sign off ([example](docs/examples/preflight-drop-column.txt)).
7. **Evaluate.** `eval/` builds 124 finance questions over 31 customers, from the same records retrieval would hand a model, mess included. Answers come from SQL. Customers never appear in both the dev and test splits. See [eval/RESULTS.md](eval/RESULTS.md).

## The data

The company is simulated, but the system is real. `edp.simulate` runs ten months of business through an actual Odoo 17 using Odoo's own logic: 145 deals (74 won, 41 lost, 30 open), orders confirmed from the won deals, 72 invoices raised through Odoo's invoicing wizard, and 59 payments, some late and some partial. Then it does what operations do to data after it has been loaded once: enters a customer twice, reopens and changes a posted invoice, bills three customers in euros, marks a deal won with no order, and deletes a draft invoice. The warehouse is loaded between those steps, so the mess arrives the way it would in production. The simulation is seeded, so it builds the same company every time.

Billed in total: $1,379,155.41. Still owed: $362,891.89, of which $118,375.00 is more than 90 days late.

## Results

| What | Result |
| --- | --- |
| Loads | 3 runs; the third, with nothing changed in Odoo, stored 0 records |
| Checks | 29 in `dbt build`: 23 pass, 6 warn, one for each kind of planted problem, each naming the records |
| Traceability | Every customer and every won deal traces to records that add up (`tests/test_warehouse.py`) |
| A real bug the checks caught | Partners with no email were being merged into one customer; recorded as `fathom/incidents/INC-001.md` |
| Model on the question set | Claude Sonnet: 30 of 32 on test, 92 of 92 on dev; both misses are a fault in the question ([details](eval/RESULTS.md)) |

## Run it

Needs Python 3.11, [uv](https://github.com/astral-sh/uv), and a local Postgres 16 with a role named `odoo` reachable on `/tmp`.

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python -r requirements.txt
scripts/odoo-local.sh install        # Odoo 17 Community from its official source
scripts/odoo-local.sh reset          # demo database "acme" with CRM, Sales, Invoicing
createdb -h /tmp -U odoo warehouse
.venv/bin/python -m edp.demo         # simulate, load three times, dbt build (about 3 minutes)
.venv/bin/python -m edp.cli trace revenue_by_customer northwind
.venv/bin/pytest -q
```

Day-to-day operation, failures and hand-off notes are in the [runbook](docs/runbook.md).

## Scope

Built in two days to show the first week of an engagement: extraction, trust, traceability, and data a model can be judged on. Not built yet: a schedule, alerting, failure drills, and a training run. The question set is small and comes from a simulation; it proves the method, not a model's quality.

## Licence

MIT. Odoo Community is LGPL v3 and is downloaded at setup, not included.
