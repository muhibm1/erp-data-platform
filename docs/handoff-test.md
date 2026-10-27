# Hand-off test, 2026-10-27

A runbook is only as good as what a newcomer can do with it. To test this one,
the pipeline was broken the way a bad morning breaks it, and a fresh agent
with no history of the project was asked to fix it.

## The setup

1. Odoo was stopped and a scheduled `edp run` was started: it failed and
   recorded the failure.
2. Odoo came back.
3. Two posted invoices were deleted from the warehouse's raw tables, and the
   models were rebuilt. Every dbt check still passed: the finance tables
   agreed with the raw tables, and both were short.

## The brief

"Finance says the customer totals look wrong this morning." The agent could
read `README.md`, `docs/runbook.md` and `docs/checks.md` only: no source code,
no file edits, no hand-written deletes or updates.

## What it did

| Step | Command | What it showed |
|---|---|---|
| 1 | `edp monitor` | `last_run` failed (Odoo refused the connection); `source_totals` failed: Odoo billed $1,409,175.41, the warehouse $1,324,650.41 |
| 2 | read-only SQL on `raw.load_runs` | the failed run, after four good ones |
| 3 | checked Odoo answers | it did |
| 4 | `edp extract --full` | stored 2 invoice versions, nothing else |
| 5 | `edp run` | dbt 23 pass, 6 warn; all five monitors OK; warehouse matches Odoo |

Fixed in seven commands, about 40 seconds, without reading code.

## What the docs lacked, and what changed

| The agent said | Change to the runbook |
|---|---|
| No connection line for the SQL; it found the settings in the README | A "Connect to the warehouse" section with the `psql` line |
| `left(detail, 120)` failed because `detail` is JSON | The query casts `detail::text`, and says why |
| Unclear whether a later good run clears `last_run` | Says so |
| Unclear whether `edp run` rebuilds, or a `dbt build` is also needed | Says `edp run` rebuilds and re-checks |
| A $84,525 gap from 2 rows looked suspicious, and nothing showed which rows | SQL listing the records a repair restored (here INV/2026/00001 and 00002, $36,512.50 and $48,012.50), and a note on how a few rows make a large gap |
| Exit codes of `edp monitor` not stated | Stated |
| Nothing on why rows go missing | The loader never deletes; ask who had write access |
