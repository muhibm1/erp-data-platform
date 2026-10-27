# Analyst: ask a warehouse a question in plain English

**An agent that answers business questions by writing SQL, checking it, running it read-only, and explaining the result. It can show you the query behind every answer.**

Ask "which three customers owe us the most?" and it finds the right tables, writes the query, runs it under a read-only database role, reads the rows and answers: Maple Street Bakery, Larkspur Foods and Glacier Water Co, with the query it used. When the data cannot answer a question, such as a customer's credit score, it says so instead of guessing.

The same agent works on two datasets: the finance warehouse in this repository, and a simulated search ads account. The second one was added by writing one configuration file, with no change to the agent.

## Results

Held-out questions, scored once after the agent was frozen ([plan](eval/PLAN.md), [full results](eval/RESULTS.md)).

| | Claude Sonnet | Claude Haiku |
| --- | --- | --- |
| Finance, one customer per question (47) | 47 | 46 |
| Finance, harder questions across the whole company, 5 not answerable (30) | 30 | 28 |
| Search ads, including A/B tests and privacy (26) | 25 | 22 |
| **All held-out questions (103)** | **102** | **96** |
| Requests that would expose fewer than 50 people or single users | 18 of 18 refused | |
| Queries that wrote, or read outside the allowed tables | none | |

For comparison, the text-to-SQL most teams build first takes one query's first column as the answer. It scored 47 of 77 on finance. The rest of the gap closes in three steps. Letting the model read the result takes it to 73. The repair loop and the domain notes take it to 77.

### Questions that name things loosely

The sets above name keywords and campaigns the way the data spells them. People don't. They ask about "the Mint keyword" when the data says `mint budget`, or "the German campaign" when it says `Ledgerly - DE - Search`. On those questions the first version wrote a query that matched nothing and confidently reported 0.

Version 2 can look a name up before it writes the final query, and gets one more try when it judges its own reading was wrong. A new set of 16 questions was written for this and frozen before any agent ran on it. Three of them ask about things that do not exist.

| Loose-name test set, Claude Sonnet (16) | Correct |
| --- | --- |
| **Version 2: look names up, try again after a declined reading** | **15** |
| Version 1 | 9 |
| One query from the bare schema, read by the model | 10 |

Version 2 gained six questions and lost none (exact sign test p = 0.03), at the price of about one extra model call per question. Its one miss was a privacy-tool false refusal, since fixed. [RESULTS.md](eval/RESULTS.md#version-2-questions-that-name-things-loosely) has the detail.

## How it works

```
question ─▶ retrieve ─▶ draft SQL ─▶ check + run ─▶ read result ─▶ cite ─▶ answer
              │            ▲              │              │
   domain file:            └── repair ────┘              └─▶ "not answerable"
   tables, terms,              (error goes back,
   examples                     up to 3 tries)
```

1. **Context retrieval** (`retrieve.py`). The domain file describes each table, the business terms ("owed", "cost per install", "statistically significant") and a few worked examples. For each question, a keyword ranker (BM25) and a local embedding model (bge-small, on CPU) each rank those pieces. The two rankings are fused with reciprocal rank fusion, and the top tables, terms and examples go into the prompt.
2. **Orchestration** (`graph.py`). A LangGraph state graph: retrieve, draft, execute, answer, cite, remember. The model drafts one query, or declines, or asks to clarify, or (version 2) asks for a lookup: a small `SELECT DISTINCT` that shows how the data spells a name, at most two per question. A lookup that finds nothing means the question is not answerable. Code checks and runs every query. A failure goes back to the model with its error, up to three tries. The model then reads the rows and gives the answer as a value plus a plain sentence. If it sees the query was the wrong one, its reason goes back to draft once for another try. Every limit (lookups, repairs, redrafts) is a counter in the graph state, enforced by code.
3. **Tool execution** (`sql.py`). Before anything runs, sqlglot parses the query. It must be a single SELECT that names only allowed schemas and contains no writes and no file-reading functions. Then the database enforces the same rules again: a read-only role, a read-only transaction, 5 seconds and 200 rows. Finance queries go through the platform's own `edp.ask.query` unchanged, so citations to the exact Odoo record versions are checked by `edp.ask.check_citations`.
4. **Privacy in the tool, not the prompt.** On the ads dataset, user-level taps live in a schema the agent's role cannot read. Every people figure in a result (installs or taps) must come from at least 50 people, or the row comes back blanked and marked suppressed. The tool finds those figures by tracing each result column back to its source with sqlglot, so renaming `sum(installs)` does not hide it. It also checks the counts behind a rate (it adds them to the query as hidden columns), the counts picked out by a `CASE` or `FILTER`, a total minus a part, and the groups inside a CTE (a separate query finds the smallest). A people figure it cannot check, such as `sum(installs * 100)`, is refused before it runs.

   What it does not stop: differencing across two separate questions (ask for a total, then for the total without one group) and joins built to set a small group's rate beside a large group's count. Closing those needs a budget across questions, such as differential privacy. An earlier version checked only a column literally named `installs`; the [results](eval/RESULTS.md) were scored on that version.
5. **Memory** (`ask.py`). Conversations are saved per thread with a SQLite checkpointer, so "and how much have they paid?" works as a follow-up.
6. **Evaluation** (`eval/`). The question sets have answers computed by gold SQL, never written by hand. The dev and test splits are kept apart, and the test sets were frozen before any run. Four conditions isolate what each part adds, and code grades every answer. Every run file records the context retrieved, every query tried with its error, the answer, the citations and the time taken.

## Adding a dataset

Write `domains/<name>.yaml`: the connection, the schemas the agent may read, the read-only role, an optional privacy rule, and notes on tables, terms and examples. Columns and types are read live from the database, so the file never lists them. [`domains/ads.yaml`](domains/ads.yaml) is the whole onboarding for the ads account.

## Run it

From the repository root, after the main README's setup:

```bash
.venv/bin/pip install -r analyst/requirements.txt
.venv/bin/python -m analyst.ask "Which three customers owe us the most?"
.venv/bin/python -m analyst.ask --thread t1 "How much do customers owe us in total?"
.venv/bin/python -m analyst.ask --thread t1 "And how much have they paid?"

.venv/bin/python -m analyst.ads.simulate                 # the ads account, database adsdemo
.venv/bin/python -m analyst.ask --domain ads "What was our cost per install by country last week?"

ANALYST_TESTS=1 .venv/bin/python -m pytest -q analyst     # no model needed
.venv/bin/python -m analyst.eval.run --set dev --condition full
.venv/bin/python -m analyst.eval.report                  # rebuild the results tables from runs/
```

The model sits behind one interface, `model(system, prompt, schema) -> dict`, with two routes. The default makes one headless `claude -p` call per step; the scored runs used it. `--route langchain` (on `analyst.ask` and the eval) uses LangChain's `ChatAnthropic` with `with_structured_output`, so every step is an ordinary LangChain call that LangSmith traces under the graph's run; it reads an Anthropic API key from the environment. The platform's CI skips these tests unless `ANALYST_TESTS=1` is set, so they add no time to it.

## Limits

Both datasets are simulated, and 103 held-out questions is a small set. Sonnet is near the ceiling on them, so the results show the method more than they rank models. The domain notes help the cheaper model a lot on finance. On ads, one note pulled the wrong table into view and cost it three questions; [RESULTS.md](eval/RESULTS.md) explains each miss. The loose-name set is one run of 16 questions. Next steps: tighten the ads notes, and add questions written by someone who did not write the notes or the agent.
