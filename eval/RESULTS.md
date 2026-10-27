# Results

## An agent with tools, 2026-10-27

The runs below hand the model the records. A real assistant has to find them.
Here Claude Sonnet got only the 47 test questions, with neutral ids (q01 to
q47) so nothing in the id gave away a customer, and reached the warehouse only
through the ask-the-warehouse tools (`edp tool ...`, the same functions `edp
mcp` serves). Every answer had to cite the source records it rests on, and
`eval/agent.py` graded it in code.

| Test split, 47 questions | Result |
| --- | --- |
| Correct | 46 of 47 |
| Grounded: every citation a real, current Odoo record version, and every non-zero answer cites at least one | 47 of 47 |
| Citations given | 73, none invented, none pointing at an old version of an edited invoice |

It ran 5 commands in all, batching customers, and checked its own
citations before answering. Its answers are in
`runs/v2-test-sonnet-agent-tools.json`, and its tool log was checked: it read
only the question file and called only `edp tool`.

**The miss is the useful part.** Asked how many days past due a customer's most
overdue unpaid invoice is, it answered 189 and cited INV/2026/00015. The right
answer is 235, INV/2026/00008, which is also unpaid. It never called
`list_invoices`, the tool that shows days late for every invoice. Its citation
was real and current, so the citation check passed: a citation proves where an
answer came from, not that the answer is right. That is why grading checks
both, and why a finance team should get both the number and the records behind
it.

## Version 2, 2026-10-27 (records in the prompt)

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
