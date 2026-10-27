# Results

Scored on 2026-10-27, once per condition, after the agent was frozen (commit
`4a9224a`). The plan, including what counts as correct, was written first
([PLAN.md](PLAN.md)). Every number below can be rebuilt from the run files in
`runs/` with `python -m analyst.eval.report`, without calling a model.

## In short

The full agent answered 102 of 103 held-out questions correctly with Claude
Sonnet, and 96 of 103 with the cheaper Claude Haiku. It never ran a query that
writes, never read outside its schemas, and refused all three questions that
would have exposed small groups of people or single users, in every condition, because those
refusals are enforced by the query tool, not the prompt.

The biggest gain is reading the result well. A one-shot query whose first column
is taken as the answer scored 47 of 77 on finance. The same one-shot query read
by the model scored 73 of 77. The domain notes were worth a few more questions
for Sonnet. For Haiku they were worth 7 on finance, but they cost 3 on ads.

## Scores

Questions answered correctly, with the 95% Wilson interval.

| Set | Model | Full agent | Agent, no context | One-shot, read by model | One-shot, first column |
| --- | --- | --- | --- | --- | --- |
| Finance test (47) | Sonnet | **47** (92-100%) | 45 | 45 | 19 |
| Finance hard test (30) | Sonnet | **30** (89-100%) | 28 | 28 | 28 |
| Ads test (26) | Sonnet | **25** (81-99%) | 25 | 25 | |
| Finance test (47) | Haiku | **46** (89-100%) | 43 | 45 | |
| Finance hard test (30) | Haiku | **28** (79-98%) | 24 | 23 | |
| Ads test (26) | Haiku | 22 (66-94%) | **25** | 24 | |

- *Full agent*: domain notes retrieved per question, then a query that is
  checked, run, repaired on error (up to three tries), and read by the model,
  which may decline.
- *No context*: the same loop with only table and column names.
- *One-shot, read by model*: one query from the bare schema, no retry, then the
  same reading step. Added after the first test run as a fairer baseline (see
  the plan); it can only flatter the baseline.
- *One-shot, first column*: the plan's original baseline. Its misses are mostly
  correct queries that also selected the customer id, which was then taken as the
  answer.

## What the differences were

The sets are small and the models are strong, so no single comparison is
statistically significant (exact sign tests, p from 0.125 to 1). What matters
is which questions moved, and why.

**Finance, Sonnet: the notes fixed two traps.** Without the notes, the agent
added the aging buckets as `late_31_60 + late_61_90 + late_over_90`. Empty
buckets are NULL, so the sum was NULL and Northwind's $7,425 came out as 0
(`late30-41`, `late30-70`, `worst-90`). It also read "entered more than once"
as "has several partner records" and listed nine customers instead of the one
that was actually merged (`merged-customers`). The notes say both things in one
line each. Full against no context: 4 gained, 0 lost.

**Finance, Haiku: the notes mattered more.** Without them, Haiku doubted that
amounts were already in dollars. It left euro invoices out of totals, or
declined, on 5 hard questions (`total-owed`, `total-paid`, `total-billed`,
`credit-note-total`, `won-no-order-value`). The note "every amount column is
already in USD" fixed all five. Hard test: 28 with notes, 24 without.

**Ads, Haiku: the notes cost three.** Retrieval put the audience table into
the context for "how many installs did Trailhead get". Haiku summed it instead
of the keyword table, and its rounded splits came to 34,115 against 34,706. It
also read its own bids on a competitor's keyword as the competitor's bids. The
fix belongs in the domain file: say the audience table is only for age and
gender questions, and that no competitor data exists. It was not made, because
the test set is scored once.

**Declining instead of guessing.** Nine times across 697 scored answers, the
reading step saw that its own query was wrong and declined. One case was a
count of campaign-days where the question asked for campaigns. Another was a
total over all deals where the question asked about won deals. None of these
were scored as correct. The next version sends the reason back to the drafting
step for another try, instead of stopping.

**Answer format.** Three ads misses named the right campaign or keyword in the
explanation but returned the dollar figure as the answer. They are scored wrong,
as the plan says.

## Safety, across all 697 scored answers

| Check | Result |
| --- | --- |
| Queries that tried to write, or read outside the allowed schemas | None was attempted. Refusal of each kind is tested in `tests/test_analyst.py` |
| Questions that would expose fewer than 50 people, or user-level records | 18 of 18 refused (3 questions, 6 runs) |
| Questions the data cannot answer (credit score, salesperson, margin, tickets, forecast, in-app revenue, competitor bids) | 46 of 47 declined; the miss is the competitor-bids case above |
| Runs that crashed or never answered | 0 |

## Cost and speed

$0 of API credit. Every model call went through a Claude subscription, one
headless call per step, with API keys removed from the call's environment.

Per question, the full agent made about 2 model calls of about 14,000 input
tokens, most of it the retrieved notes. The median answer took 8 seconds with
Sonnet and 9 with Haiku, with up to four questions in flight. Retrieval itself
takes milliseconds once the local embedding model is loaded.

## Development, for the record

On the first dev run, about a third of first attempts failed. The cause was a
bug in the platform's query tool: a `%` in `ILIKE '%name%'` was read as a
parameter placeholder. The repair loop recovered 33 of those questions by
rewriting the query with `strpos`. The bug was reported and fixed upstream (`b1c276b`). After that fix, and
one change to the reading step (no rows means 0, not "cannot answer"), dev
scored 48 of 48. No query needed a repair on the test sets.

## Limits

- Both datasets are simulated, and the question sets are small (103 scored).
  Sonnet is near the ceiling on them, so they show the method more than they rank models.
- The domain notes were written by the same person who wrote the questions.
  The test questions were frozen first, but the notes still reflect what that
  author knew to be tricky.
- LangSmith tracing was off for these runs, because the account had used its
  monthly trace allowance. Each run file records every step instead: the
  context retrieved, every query tried and its error, the answer, the
  citations and the time taken.
