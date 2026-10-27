# ERP data platform

[![ci](https://github.com/muhibm1/erp-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/muhibm1/erp-data-platform/actions/workflows/ci.yml)

**Gets a company's sales and accounting data out of its ERP, makes it trustworthy, and traces every finance number back to the records behind it.**

A company runs on Odoo, an open-source ERP (enterprise resource planning: the system that holds orders, invoices and the books) with a CRM built in (the system that tracks customers and deals). Finance wants three answers: what each customer was billed and has paid, who is late and by how much, and whether every deal sales won actually turned into cash. This project answers them, and can show its work for any number. Ask why Northwind Logistics owes $26,835.00, and you get the three invoices and two payments behind it, with their Odoo ids and the exact version of each, plus the note that Northwind exists twice in Odoo and was merged ([example](docs/examples/trace-northwind.txt)).

It runs on a schedule, watches itself, and has been broken on purpose five ways to prove it fails loudly and recovers. It also builds a question set an AI finance assistant can be judged on and trained with, with every answer computed from the data rather than written by hand or by a model.

## What it does

```
Odoo (CRM, Sales, Invoicing)
   │  edp run: lock, extract (JSON-RPC, incremental, retries), dbt build, monitors
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
   ├─ edp mcp     read-only, cited tools for AI agents: ask the warehouse
   ├─ monitors    fresh? complete? same totals as Odoo? → alerts file and webhook
   ├─ drills      five failures on purpose, each caught and recovered
   ├─ Fathom      lineage, owners, rules, incidents: what a change would break
   └─ eval/       finance questions with SQL answers: train, dev and test sets
```

1. **Extract.** `edp extract` reads deals, customers, orders, invoices and payments through Odoo's API, the way you would at a client: no database access to the ERP needed. It fetches only what changed since the last load, so a rerun stores nothing new. A field the load needs going missing stops the load and names the field; a custom field a client adds is picked up automatically.
2. **Keep history.** Raw tables are append-only, one row per saved version of a record. When accounting reopens a posted invoice and changes it, both versions stay. When a record is deleted in Odoo, the load notices and records it.
3. **Model.** dbt turns raw records into three finance tables in US dollars. Every row carries the Odoo ids it was built from.
4. **Check.** `dbt build` runs 18 checks. Twelve hard checks fail the build if the finance tables stop matching Odoo. Six source-quality checks list the real-world mess: a customer entered twice, an invoice changed after posting, euro invoices, a deal marked won with no order, a deleted record. Each is handled in the models and listed for a person to fix at the source. See [docs/checks.md](docs/checks.md).
5. **Trace.** `edp trace` expands a number into its records and checks they add up. A test runs it on every customer and every won deal.
6. **Guard changes.** [Fathom](https://github.com/muhibm1/fathom) reads the dbt project for column-level lineage, owners and rules. Proposing to drop a column from the aging table returns BLOCK: it names month-end close and the collections queue as the reports that would break, and finance as the team that must sign off ([example](docs/examples/preflight-drop-column.txt)).
7. **Run and watch.** `edp run` does one load, build and check under a database lock, so a second run started at the same time skips instead of loading twice. Run it from cron or with `--every`. Five monitors follow each run: did the load succeed, is the data fresh, did any table shrink, did Odoo's fields change, and does the warehouse bill the same total as Odoo itself. Failures go to an alerts file and, if set, a Slack or Teams webhook.
8. **Break it on purpose.** `edp.drills` takes Odoo down mid-schedule, starts two runs at once, adds then deletes a custom field in Odoo, deletes invoices from the warehouse, and lets the data go stale. Each is caught and recovered from ([report](drills/report.json)). Two drills found real problems, written up as postmortems in `fathom/incidents/`: a schema change that waited forever on a reader's lock (INC-002), and lost rows that every dbt check missed because the checks compare the warehouse with itself (INC-004). The fix for the second is the monitor that asks Odoo.
9. **Let an agent ask.** `edp mcp` serves six tools to any AI agent over MCP (the same tools run as `edp tool <name>` on the command line): find a customer, summarise what they owe, list invoices, follow won deals to cash, run one read-only query, and check citations. Every row carries the exact Odoo record version it came from, `query` runs as a database role that can only read the modelled tables, and `check_citations` confirms a cited record is real and current. An agent cannot cite what it was not shown, and a person can check any answer without trusting the agent.
10. **Evaluate and train.** `eval/` builds 184 finance questions over 31 customers, six kinds, from the same records retrieval would hand a model, mess included. Answers come from SQL. Train, dev and test never share a customer, and the build fails if any customer, question, invoice or record set crosses a split. Train is exported as chat examples for fine-tuning. See the [dataset card](eval/DATASET.md) and [results](eval/RESULTS.md).

## The data

The company is simulated, but the system is real. `edp.simulate` runs ten months of business through an actual Odoo 17 using Odoo's own logic: 145 deals (74 won, 41 lost, 30 open), orders confirmed from the won deals, 72 invoices raised through Odoo's invoicing wizard, and 59 payments, some late and some partial. Then it does what operations do to data after it has been loaded once: enters a customer twice, reopens and changes a posted invoice, bills three customers in euros, marks a deal won with no order, and deletes a draft invoice. The warehouse is loaded between those steps, so the mess arrives the way it would in production. The simulation is seeded, so it builds the same company every time.

Billed in total: $1,379,155.41. Still owed: $362,891.89, of which $118,375.00 is more than 90 days late.

## Results

| What | Result |
| --- | --- |
| Loads | 3 runs; the third, with nothing changed in Odoo, stored 0 records |
| Failure drills | 5 of 5 caught and recovered: Odoo down (fails in 6 s, catches up after), two runs at once (one skips), a custom field added and removed (warned, value loaded), 3 invoices lost from the warehouse (caught against Odoo, repaired in under a second), 3 hours stale (fails) |
| Checks | 18 in `dbt build`: 12 hard checks pass, 6 source-quality checks warn, one for each kind of planted problem, each naming the records |
| Traceability | Every customer and every won deal traces to records that add up (`tests/test_warehouse.py`) |
| A real bug the checks caught | Partners with no email were being merged into one customer; recorded as `fathom/incidents/INC-001.md` |
| Model on the question set | Claude Sonnet: 47 of 47 on test with or without being told how the records are messy; answering 0 to everything gets 23 ([details](eval/RESULTS.md)) |
| Agent with tools | Sonnet given only the questions and the tools: 46 of 47 correct, and all 73 of its citations real and current. The miss cited a real record and still had the wrong number, which is why answers are graded and cited separately ([details](eval/RESULTS.md)) |
| Analyst agent | A question-to-SQL agent that works on this warehouse and a second, ads dataset: 102 of 103 held-out questions right with Sonnet, and all 18 requests that would expose small groups refused by the tool ([analyst/](analyst/README.md)) |
| Rebuilt from nothing | A fresh Odoo, a fresh warehouse, the same seed: all 184 answers came out identical |
| Hand-off test | A fresh agent given only the docs fixed a broken morning (a failed run plus two invoices lost from the warehouse) in seven commands; its six complaints about the runbook were fixed ([write-up](docs/handoff-test.md)) |
| CI | Every push builds Odoo from source, simulates the year, loads it, runs every check, test and drill, and rebuilds the dataset; green on the first run, about 8 minutes |

## Run it

Needs Python 3.11 and Postgres 16 with a role named `odoo` that can create databases (by default on a socket in `/tmp`; set `PGHOST` and friends otherwise, as [CI](.github/workflows/ci.yml) does).

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
scripts/odoo-local.sh install        # Odoo 17 Community from its official source
scripts/odoo-local.sh reset          # demo database "acme" with CRM, Sales, Invoicing
createdb -h /tmp -U odoo warehouse
.venv/bin/python -m edp.demo         # simulate, load three times, dbt build (about 3 minutes)
.venv/bin/python -m edp.cli trace revenue_by_customer northwind
.venv/bin/python -m pytest -q
.venv/bin/python -m edp.cli run      # one scheduled run: load, build, monitors
EDP_DRILL_ODOO_STOP="scripts/odoo-local.sh stop" EDP_DRILL_ODOO_START="scripts/odoo-local.sh start" \
  .venv/bin/python -m edp.drills     # the five failure drills (demo stack only)
.venv/bin/python eval/build.py       # the question set and its card
```

Day-to-day operation, failures and hand-off notes are in the [runbook](docs/runbook.md).

## Scope

Built in two days to show the data side of an ERP engagement end to end: extraction, trust, traceability, reliability, and data a model can be judged and trained on, with a runbook for the team that takes it over. It has run only against a simulated company in a real Odoo. Next would be a second source system (a CRM or accounting export next to Odoo), a harder question set with longer histories, and a fine-tuning run on the train split.

## Licence

MIT. Odoo Community is LGPL v3 and is downloaded at setup, not included.
