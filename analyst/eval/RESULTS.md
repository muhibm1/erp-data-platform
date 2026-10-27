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
| Questions that would expose fewer than 50 people, or user-level records | 18 of 18 refused (3 questions, 6 runs)* |
| Questions the data cannot answer (credit score, salesperson, margin, tickets, forecast, in-app revenue, competitor bids) | 46 of 47 declined; the miss is the competitor-bids case above |
| Runs that crashed or never answered | 0 |

\* Added 2026-10-27, after scoring: a review found that the privacy rule at the scored commit
checked only a result column literally named `installs`. A query that renamed the sum, or
selected taps, would have passed a small group through. The rule now traces every result
column to its source, checks the counts behind rates and inside CTEs, and is tested against
those attacks on the ads database (`tests/test_analyst.py`). No scored run exploited the gap:
replaying all 147 distinct queries the agent wrote across every ads run through the new rule
(`python -m analyst.eval.privacy_replay`, no model calls) gives the same result for 146. The
one change is a Haiku query in the no-context condition that computed a rate inside an
aggregate; it is now refused with a message the repair loop would act on. All 27 ads
answer-key queries run under the new rule with nothing suppressed. The scored numbers were
not rerun.

## Version 2: questions that name things loosely

**In short.** Real users don't know how the data spells things. They ask about "the Mint
keyword" when the data says `mint budget`, or "the German campaign" when it says
`Ledgerly - DE - Search`. Version 1 wrote one query from the question as asked. When the name
didn't match, the query returned no rows and the agent reported 0. That is a confident wrong
answer. Version 2 can look a name up first and send a self-declined answer back for another
try. On a new set of 16 questions written for this, it answered 15. Version 1 answered 9,
and a single query from the bare schema answered 10.

**The set.** `explore_questions.py` holds 21 ads questions phrased the way a marketer would
ask them: 5 for development and 16 for test. Three of them name something that does not
exist, such as a Canadian campaign, an app-icon test or a "notion" keyword. The right reply
to those is "not answerable", not 0. The questions and their gold SQL were committed
(`69d0058`) before any agent ran on them. The agent was frozen at `745b65f` before the test
set was scored once, on Claude Sonnet.

| Explore test (16) | Correct | 95% interval |
| --- | --- | --- |
| Version 2: lookups, and another try after a declined reading | **15** | 72-99% |
| Version 1: the agent scored above | 9 | 33-77% |
| One query from the bare schema, read by the model | 10 | 39-82% |

- **What changed.** Before the final query, draft may run up to two lookups. A lookup is a
  small `SELECT DISTINCT` that finds how the data spells what the question names. If a lookup
  finds nothing, the question is not answerable. Separately, when the reading step judges that
  its own query was the wrong one, the reason goes back to draft once instead of ending the turn.
- **Where it gained.** Version 2 gained six questions on version 1 and lost none (exact sign
  test p = 0.03). It got four names right that version 1 matched exactly and missed: `mint
  budget`, `gaia gps`, `copilot money` and the two hiking keywords. It also declined two
  questions where version 1 answered 0 for something that does not exist.
- **The miss was in the privacy tool, not the model.** On `xt-alltrails-7d`, both lookups
  were refused. A query over the keywords table with a whole-row reference (`k::text`) could
  not be traced, so the privacy rule failed closed, although that table holds no people
  counts. The rule now skips any query whose tables hold no count of people
  (`sql.reads_people`), with a test. The scored run was not repeated. Replaying the 147
  recorded ads queries gives the same result as before.
- **What it costs.** About one extra model call per question: 3.0 calls and 24,000 input
  tokens per question, against 2.3 and 17,500. The median answer takes 16 seconds instead of 9.
- **Caveats.** It is one run of 16 questions, with one author writing both the questions and
  the agent. The sign test says version 2 is better on this kind of question. It does not say
  by how much.

## A LangChain chat-model route

`model.ChatModel` wraps a LangChain chat model (`ChatAnthropic` by default). It asks for each
step's reply through `with_structured_output`. Under the graph's run, each step is then an
ordinary LangChain call that LangSmith can trace. Use `--route langchain` on the eval and on
`analyst.ask`. Tests cover it with a stand-in model. The scored runs above used the `claude -p`
route, so version 2's numbers do not depend on the new route.

One live check ran on this route after the scoring: version 2 on the 5 loose-name dev questions,
Claude Sonnet through `ChatAnthropic`. It answered all 5, including the declined question about a
keyword that does not exist, in 15 model calls (28,500 input and 4,800 output tokens), with a
median of 10 seconds. The check found one real bug first: the client refuses any `temperature`
setting on the 5.5 models, so `ChatModel` now leaves it at the default. The run file is
`runs/explore-dev-full-v2-sonnet-*` with `"route": "langchain"`.

## Speed and size

Every model call is one headless `claude -p` call per step. Per question, the full agent made about 2 model calls of about 14,000 input
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
