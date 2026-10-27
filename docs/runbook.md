# Runbook

For the team that runs this after hand-off. Each section says what to do, then why.

## Run the pipeline

```bash
.venv/bin/python -m edp.cli run                 # one run: load, dbt build, monitors
.venv/bin/python -m edp.cli run --every 900     # keep running, every 15 minutes
```

Or from cron, which is what a single server needs:

```cron
*/15 * * * * cd /srv/erp-data-platform && .venv/bin/python -m edp.cli run >> run.log 2>&1
```

A load only fetches records whose `write_date` is at or after the newest one
already stored, so it takes seconds. Running it twice in a row is safe: the
second run stores nothing new (`raw.load_runs` shows zero for every model).
Two runs at once are safe too: the second finds the lock taken, says
"another run holds the lock; nothing to do" and exits with code 3.

Exit codes: 0 everything passed, 1 a step or a monitor failed, 3 skipped
because another run was going. Each run is in `raw.pipeline_runs` with what
every step returned.

## Alerts

Every warning and failure is appended to `alerts.jsonl` (set `EDP_ALERT_FILE`
to move it) and, when `EDP_ALERT_WEBHOOK` is set, posted to that URL as
`{"text": ...}`, which Slack and Teams incoming webhooks accept. Thresholds:
`EDP_FRESHNESS_HOURS` (default 2) and `EDP_VOLUME_DROP` (default 0.2).

```bash
.venv/bin/python -m edp.cli monitor     # the monitors on their own; exit 1 on a failure
```

## Connect to the warehouse

```bash
psql -h /tmp -U odoo warehouse        # the demo defaults; production uses its own host and role
```

The pipeline reads the same settings from `EDP_DSN` (a libpq connection
string). Every query below runs in this session.

## Check that the data is fresh

```sql
select run_id, started_at, status, detail::text from raw.load_runs order by started_at desc limit 5;
```

`detail` is JSON (counts per model, or the error), so cast it to text before
cutting it short.

The newest run's `started_at` is the "data through" time. Most "the number is
wrong" reports are a load that failed or did not run. A failed run has
`status = 'failed'` and the error in `detail`, and nothing from it is half-written:
each model's records commit together.

## Reload from scratch, or backfill

```bash
.venv/bin/python -m edp.cli extract --full            # every record
.venv/bin/python -m edp.cli run --since 2026-09-01    # records changed since a date, then build and check
```

Both re-read from Odoo. Versions already stored are skipped, so they add only
what is missing and are safe to repeat. Use `--full` when the `source_totals`
monitor fails (rows are missing from the warehouse), and `--since` after a
known gap, such as a weekend outage.

## When a monitor fails

| Monitor | What it means | Do this |
|---|---|---|
| `last_run` | The newest load failed or is stuck. The error is in the message. | If Odoo was down, wait for it and `edp run` again; the client already retried with backoff. The next successful run clears this monitor and catches up on everything changed meanwhile. |
| `freshness` | Nothing has loaded successfully for longer than the limit. | Check the scheduler is running, then `edp run`. |
| `volume` | A model lost more than 20% of its records between loads. | Ask the Odoo admin whether records were archived or deleted on purpose before anyone uses the tables. |
| `schema` | Fields were added to or removed from Odoo. A fail means a field the load reads is gone. | Warn: nothing to do. Fail: update `edp/sources.py` and the staging model. |
| `source_totals` | The warehouse bills a different total than Odoo itself. | `edp extract --full`, then `edp run` (it rebuilds every model and re-runs the monitors; no separate `dbt build` is needed). If it still fails, `trace` the biggest customers. |

`edp monitor` exits 0 when nothing failed (warnings included) and 1 when
anything failed.

**Which records were missing.** A repair stores only what was missing, tagged
with its run, so this lists them once `extract --full` has run:

```sql
select odoo_id, payload->>'name' as record, payload->'partner_id'->>1 as customer,
       payload->>'amount_total_signed' as amount_usd
from raw.account_move
where run_id = (select run_id from raw.load_runs where mode = 'full' order by started_at desc limit 1);
```

A gap in dollars can be large from few rows: two missing invoices of $40,000
each make an $80,000 gap. Rows go missing from the warehouse only when someone
deletes them there (a manual fix, a restore from an old backup); the loader
never deletes. Ask who had write access before closing the incident.

Every failure the drills produced, and what was done, is written up in
`fathom/incidents/`.

## Explain a number

```bash
.venv/bin/python -m edp.cli trace revenue_by_customer "northwind"
.venv/bin/python -m edp.cli trace receivables_aging "harbor freight"
.venv/bin/python -m edp.cli trace deal_to_cash 129
```

Lists every invoice, payment and order behind the number, with the Odoo id
and the exact version saved, and checks that they add up. If a trace ends in
"DOES NOT ADD UP", stop and investigate before anyone uses that table.

## When a check fails or warns

See `docs/checks.md` for every check. In short:

- **A hard check fails** (`marts_match_odoo_totals`, `aging_adds_up`): the
  finance tables are wrong. Do not publish them. Run `trace` on the customer
  the check names.
- **A source-quality check warns**: the models already handled it. Send the
  listed records to whoever owns them in Odoo (sales for customers and deals,
  accounting for invoices).

## When Odoo changes

- **A field the load needs is gone or renamed.** The load stops before it
  writes anything and names the field: `crm.lead is missing fields [...]; the
  source changed`. Update `edp/sources.py`, then the staging model that reads it.
- **Someone adds a custom field** (`x_...`). It is loaded automatically into
  the record's `payload`. Nothing breaks; add it to a staging model when it is needed.
- **Records are deleted.** The load notices (`raw.deleted`) and the models leave them out.

## Connect an AI assistant

```bash
claude mcp add erp-warehouse -- /srv/erp-data-platform/.venv/bin/python -m edp.cli mcp
.venv/bin/python -m edp.cli tool find_customers '{"name": "northwind"}'   # the same tools by hand
```

Any MCP client works the same way. The tools only read. `query` runs as the
`edp_reader` role, which can select from `marts`, `intermediate` and
`staging` and nothing else, inside a read-only transaction with a 5-second
limit. If an assistant's answer is questioned, run `check_citations` on the
references it gave: each must say `ok`.

## Before changing a model

```bash
.venv/bin/fathom sync
.venv/bin/fathom preflight --intent "drop the late_over_90 column from receivables_aging"
```

Fathom names what the change reaches (month-end close, the collections queue),
who owns it, and which rule applies. Changing `revenue_by_customer` or
`receivables_aging` columns, or the customer matching rule, needs finance
sign-off (`fathom/constraints/`).

## Failure drills

```bash
EDP_DRILL_ODOO_STOP="scripts/odoo-local.sh stop" EDP_DRILL_ODOO_START="scripts/odoo-local.sh start" \
  .venv/bin/python -m edp.drills
```

Breaks the pipeline five ways on purpose (Odoo down, overlapping runs, a
custom field added then deleted, rows lost from the warehouse, three hours
with no load) and checks each one is caught and recovered from. The result is
in `drills/report.json`. Run them on the demo stack only, never against a
client's Odoo: the schema drill creates and deletes a field there. CI runs
them on every push.

## Known limits

- Some Odoo fields are computed, not stored (an order's invoice list, a
  payment's matched invoices). Changing them does not always change the
  record's `write_date`, so a link can arrive one load late. A `--full` load
  catches up.
- Aging is as of the `as_of` date in `dbt/dbt_project.yml`. For a live
  system, set it to the run date.
- It has run on a schedule and through the drills only against the demo
  Odoo, never a production one.
