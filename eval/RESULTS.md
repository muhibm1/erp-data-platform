# Results

## Version 2, 2026-10-27 (current)

The question set is described in `DATASET.md`: 184 questions over 31
customers, split by customer into train (89), dev (48) and test (47), six
templates, and two prompt variants. "Hints" tells the model how the ERP
records are messy; "plain" gives the same records and question with no
warning.

The model was Claude Sonnet, run on a Claude subscription (no API credit).
For each variant it got one file holding every test question, grouped by
customer with that customer's records once per group, built from the same
records and wording `build.prompt` uses. It read that one file and used no
other tool (checked in the run's tool log). `run.py grade` scored the answers,
which are in `runs/v2-test-*.json`.

| Test split, 8 customers | Correct |
| --- | --- |
| Sonnet, hints | 47 of 47 |
| Sonnet, plain | 47 of 47 |
| Floor: answer 0 or an empty list to everything | 23 of 47 |

The floor by template: billed 1 of 8, deal collected 2 of 7, invoices not
fully paid 4 of 8, owed 5 of 8, oldest late 5 of 8, late over 30 days 6 of 8.
By data problem, the floor gets 1 of 6 on the edited invoice and 1 of 6 on the
duplicated customer, where Sonnet got 6 of 6 on both.

**What this says.** On records this size, Sonnet reads ERP mess correctly
without being told about it: it used the newest invoice version, joined both
spellings of the customer, netted credit notes and followed a deal through its
orders to its payments. The hints added nothing measurable here. The value of
the set is the method, ready to point at harder records: SQL answers, splits
no customer crosses, a floor, slices by data problem, code grading, and a
variant that measures what the prompt's rules are worth.

**The v1 fault is fixed.** Version 1 counted credit notes as unpaid invoices,
which made the model's two "misses" faults in the question. The question now
says "invoices (not credit notes)" and the SQL agrees. This is a fresh run on
a rebuilt set, not a regrade of the old answers.

## Version 1, 2026-10-27 (superseded)

Two splits (dev 92, test 32), four templates, hinted prompt only. Sonnet got
30 of 32 on test and 92 of 92 on dev; the two misses were the credit-note
fault above.

## Limits

- One model, one run per variant, on simulated records. 47 test questions
  can show a method working; they cannot rank models.
- Answers came from one file per variant rather than one call per question,
  so a model could in principle carry reasoning from one question to the next
  within a customer. `run.py ask --provider claude-code` asks one question per
  call for a stricter run.
- Harder tests are the next step: more customers per question, longer
  histories, partial payments across several invoices, and records from a
  second system (a CRM export next to the ERP).
