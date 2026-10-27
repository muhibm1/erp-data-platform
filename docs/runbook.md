# Runbook

For the team that runs this after hand-off. Each section says what to do, then why.

## Run a load

```bash
.venv/bin/python -m d2c.cli extract
.venv/bin/dbt build --project-dir dbt --profiles-dir dbt
```

A load only fetches records whose `write_date` is at or after the newest one
already stored, so it takes seconds. Running it twice in a row is safe: the
second run stores nothing new (`raw.load_runs` shows zero for every model).

## Check that the data is fresh

```sql
select run_id, started_at, status, detail from raw.load_runs order by started_at desc limit 5;
```

The newest run's `started_at` is the "data through" time. Most "the number is
wrong" reports are a load that failed or did not run. A failed run has
`status = 'failed'` and the error in `detail`, and nothing from it is half-written:
each model's records commit together.

## Reload from scratch

```bash
.venv/bin/python -m d2c.cli extract --full
```

Re-reads every record. Versions already stored are skipped, so this adds only
what is missing. Use it after a load was down for a long time, or to check
nothing was missed.

## Explain a number

```bash
.venv/bin/python -m d2c.cli trace revenue_by_customer "northwind"
.venv/bin/python -m d2c.cli trace receivables_aging "harbor freight"
.venv/bin/python -m d2c.cli trace deal_to_cash 129
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
  source changed`. Update `d2c/sources.py`, then the staging model that reads it.
- **Someone adds a custom field** (`x_...`). It is loaded automatically into
  the record's `payload`. Nothing breaks; add it to a staging model when it is needed.
- **Records are deleted.** The load notices (`raw.deleted`) and the models leave them out.

## Before changing a model

```bash
.venv/bin/fathom sync
.venv/bin/fathom preflight --intent "drop the late_over_90 column from receivables_aging"
```

Fathom names what the change reaches (month-end close, the collections queue),
who owns it, and which rule applies. Changing `revenue_by_customer` or
`receivables_aging` columns, or the customer matching rule, needs finance
sign-off (`fathom/constraints/`).

## Known limits

- Some Odoo fields are computed, not stored (an order's invoice list, a
  payment's matched invoices). Changing them does not always change the
  record's `write_date`, so a link can arrive one load late. A `--full` load
  catches up.
- Aging is as of the `as_of` date in `dbt/dbt_project.yml`. For a live
  system, set it to the run date.
- It has not run on a schedule or against a production Odoo yet.
