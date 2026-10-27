# Evaluation plan for the analyst agent

Written on 2026-10-27, before the agent was run on any test question. The hard
test set's answers were frozen first (sha256 `95288c0f…` of `data/hard-test.jsonl`).

## What is measured

Can an agent answer a finance question in plain English by writing SQL against
the warehouse, with no records handed to it? An answer counts only if the value
it returns matches the value a hand-written gold query returns.

## Question sets

| Set | Questions | Use |
| --- | --- | --- |
| `../eval/data/dev.jsonl` | 48, one customer each | Development. Prompts and the domain file may change after looking at these. |
| `data/hard-dev.jsonl` | 8 (7 answerable, 1 not) | Development. |
| `../eval/data/test.jsonl` | 47, one customer each, customers never seen in dev | Scored once per condition, after the agent is frozen. |
| `data/hard-test.jsonl` | 30 (25 answerable, 5 not) | Scored once per condition, after the agent is frozen. |

The per-customer sets are the platform's own question set (six kinds: billed,
owed, more than 30 days late, unpaid invoices, most days overdue, collected on
a deal). The hard sets need more than a lookup: totals across customers, nulls
in the aging table, credit notes, the merged customer, deal statuses, win
rates, invoice history. Five hard test questions cannot be answered from this
warehouse at all (a credit score, a salesperson, margin, support tickets, a
forecast); the right reply is to decline.

## Conditions

1. **One-shot baseline.** The model sees every table and column name with its
   type, writes one query, and the query's result is its answer (one cell, or
   the first column). No retries, no notes. This is the text-to-SQL most teams
   build first.
2. **Agent without context.** The full agent loop (check, run, repair, read
   the result, decline) but only the bare schema: no table notes, terms or
   examples.
3. **Full agent.** The loop plus retrieved context from the domain file.

2 against 3 shows what context retrieval adds. 1 against 2 shows what the
loop adds.

## Model

Claude Sonnet, one headless call per model step.

## Grading (code, no judge)

- money: within $0.01. number: within 0.1. count: exact.
- set: same items, order and case ignored. text: equal after trimming, case ignored.
- not answerable: right only if the agent declines. Declining an answerable question is wrong.
- A query that never runs, or an answer step that fails, is wrong.

## Added after the first test run

**One-shot with a reader.** Condition 1 takes the first column of the result as
the answer, so a correct query that also selects the customer id is scored
wrong. That understates a real one-shot system. This condition writes one query
with the bare schema, gets no retry, and then the same answer step as the agent
reads the rows. It was added after seeing the test results, and it can only make
the baseline look better, never the agent.

Results are reported per set with a 95% Wilson interval, and as paired
differences between conditions on the same questions.
