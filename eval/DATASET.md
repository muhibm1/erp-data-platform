# Dataset card: ERP finance questions

Questions a finance team asks about one customer, with that customer's records exactly as they
sit in the ERP (edited invoices in every saved version, a customer under two spellings, euro
invoices, credit notes, a won deal with no order). Every answer is computed by SQL over the
warehouse, never by a model, and the SQL is stored next to it.

Built 2026-10-27T09:22:18+00:00 from 3 warehouse loads (last `20261027T092205-ce5f05`). Figures are as of 2026-10-27.

| Split | Customers | Questions | Use |
|---|---|---|---|
| train | 15 | 89 | exported to `data/train.chat.jsonl` for fine-tuning |
| dev | 8 | 48 | for changing prompts and code; score freely |
| test | 8 | 47 | held out; score once per frozen version |

## Question templates

| Template | Asks | Answer | Zero or empty answers |
|---|---|---|---|
| `billed` | How much has <customer> been billed in total, in US dollars, net of credit notes? | money | 2/31 |
| `owed` | How much does <customer> still owe, in US dollars? | money | 17/31 |
| `late30` | As of 2026-10-27, how much of what <customer> owes is more than 30 days past its due date, in US dollars? | money | 22/31 |
| `unpaid_refs` | Which of <customer>'s invoices (not credit notes) are not fully paid? | set | 15/31 |
| `oldest_late` | As of 2026-10-27, how many days past its due date is <customer>'s most overdue unpaid invoice? | count | 19/31 |
| `deal_collected` | How much has been collected so far on <customer>'s deal "<deal>", in US dollars? | money | 13/29 |

Money is right within one cent, a count must be exact, and a list must be exactly the right set.
Grading is code (`eval/run.py grade`); no model grades another model.

## Messy records per split

Each question is tagged with the problems its customer's records contain, so scores can be
read per problem.

| Problem | train | dev | test |
|---|---|---|---|
| credit_note | 0 | 6 | 5 |
| duplicate_customer | 0 | 0 | 6 |
| edited_invoice | 6 | 0 | 6 |
| foreign_currency | 17 | 0 | 0 |
| won_deal_without_order | 0 | 12 | 0 |

## Leakage checks

The build fails if any of these finds an overlap.

| Check | train/dev | train/test | dev/test | Result |
|---|---|---|---|---|
| customers in more than one split | 0 | 0 | 0 | pass |
| question text in more than one split | 0 | 0 | 0 | pass |
| identical record sets in more than one split | 0 | 0 | 0 | pass |
| invoices in more than one split | 0 | 0 | 0 | pass |

## Known limits

- The business is simulated (a seeded year of Odoo activity), so the customers are few and the
  mess is planted, not found. The same build runs unchanged on a real Odoo warehouse.
- The splits are by customer only. Train, dev and test share the same company, chart of
  accounts and product catalogue.

## Files

| File | sha256 |
|---|---|
| `data/dev.jsonl` | `12bb4d2f44513eb3…` |
| `data/test.jsonl` | `3f200812ee37e8f7…` |
| `data/train.chat.jsonl` | `82e0e2ff65ba7e5d…` |
| `data/train.jsonl` | `cf659d1386cdc7f9…` |
