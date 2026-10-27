# Results

## First scored run, 2026-10-27

The model was Claude Sonnet, run on a Claude subscription (no API credit). It
got one batch with every question, grouped by customer, built from the same
prompt `run.py` sends one question at a time (`build.prompt`). It read the
batch file and used no other tool. Its answers are in
`runs/all-sonnet-subagent.json`; `run.py grade` scored them.

| Split | Correct | Floor (answer 0 or an empty list) |
| --- | --- | --- |
| test (8 customers) | 30 of 32 | 15 of 32 |
| dev (23 customers) | 92 of 92 | not run |

By slice, across both splits: edited invoices 8 of 8, the duplicated customer
4 of 4, euro invoices 12 of 12, customers with credit notes 6 of 8.

**The two misses are a fault in the question, not the model.** Both ask
"which invoices are not fully paid?" for a customer that also has open credit
notes. The SQL answer counts credit notes as invoices, the model did not, and
in everyday speech the model is right. The question set needs either the
wording "invoices or credit notes" or SQL that excludes credit notes. That is
the next change, and it gets a fresh run rather than a regrade of these answers.

## How to read this

- **The set is small and made from a simulation.** 124 questions over 31
  customers, from ten simulated months in a real Odoo. It shows the method
  works end to end: SQL answers, customer-disjoint splits, slices by data
  problem, code grading. It is not a benchmark of the model.
- **The floor is high.** Half the test answers are 0 or empty because many
  customers owe nothing. Questions about customers who owe money are the
  ones that separate a good model from a guess.
- **The prompt names the mess.** It tells the model that an invoice can have
  several versions and a customer several spellings. A harder variant would
  leave that out and measure the difference.
