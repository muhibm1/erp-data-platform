# Voice: ask the finance warehouse out loud

**A voice front end for the warehouse's agent tools, built on ElevenAgents. Ask "who's more than ninety days late?" and it answers in a sentence or two, from the same read-only, cited tools an AI agent uses over MCP.**

ElevenAgents is ElevenLabs' platform for voice agents. It handles the parts of a conversation that are hard to get right by hand: hearing the person, knowing when they have finished talking, letting them interrupt, and speaking the reply. This folder adds the finance desk on top: a prompt written for the ear, the six warehouse tools, and ten test conversations that ElevenAgents runs itself.

The tools run on the machine that holds the warehouse, not on a public server. ElevenAgents decides when to call one, the call comes back to `talk.py` over the conversation's own connection, and `talk.py` runs it with `edp/ask.py` unchanged. The read-only role, the five-second limit and the source references all still apply. Nothing about the data has to be reachable from the internet.

## How a question flows

```
you speak ──▶ ElevenAgents: speech to text, end of turn, the model, the voice
                  │  client tool call: find_customers {"name": "northwind"}
                  ▼
              talk.py on your machine ──▶ edp/ask.py ──▶ warehouse (read-only role)
                  │  result as JSON, with source references
                  ▼
              ElevenAgents speaks: "Northwind still owes twenty-six thousand,
                                    eight hundred thirty-five dollars. Want the invoices?"
```

## What is in it

| File | What it does |
| --- | --- |
| `agent.py` | The agent as plain data: the prompt, the first line it says, the voice model, and one client tool per warehouse tool, with each tool's arguments read from its Python signature |
| `bridge.py` | Runs a tool call on the warehouse. Refuses an unknown tool or an argument the tool does not take, and logs every call with its time |
| `talk.py` | A live conversation through the microphone and speakers, with a log of every turn, tool call, and how long each reply took to start |
| `deploy.py` | Creates or updates the tools, the agent and the tests in an ElevenLabs workspace. `--dry-run` prints all of it with no account. `--run-tests` runs the tests |
| `scenarios.py` | The ten test conversations, and the saved tool results they use |
| `fixtures/` | Real tool results from this repository's warehouse. A test in `tests/test_voice.py` checks they still match it |

### Writing for the ear

A voice answer is heard once, so the prompt asks for different habits than a chat agent's:

- Lead with the answer in one or two short sentences, then offer one next step ("Want the invoices?").
- Never read more than three items; give the top three and say how many more there are.
- Say money rounded to the dollar, and say lateness "as of" the warehouse's aging date.
- Never read source references aloud. Offer to check them instead.
- Every number comes from a tool. What the warehouse does not hold, like a credit limit, gets "That isn't recorded in the warehouse."
- It can only read. Asked to mark an invoice paid, it says so and points to accounting.

### The ten tests

ElevenAgents has its own test runner. These are written for it, in two kinds.

**Tool tests** check what the agent does next:

1. Asked what Northwind owes, it looks the customer up first.
2. Then it uses the `customer_key` it was given, not a guess.
3. Asked for overdue invoices, it sets `status` to `overdue`.
4. Asked whether won deals turned into cash, it calls `list_deals`.
5. Asked who is more than 90 days late, it writes one query on `marts.receivables_aging`.

**Reply tests** check what it says, against a pass condition with good and bad examples:

6. It says Northwind's balance from the tool, rounded, and no other number.
7. It says lateness as of the aging date.
8. Asked to read a long list, it gives three and says how many more.
9. Asked to mark an invoice paid, it refuses and never claims it did.
10. Asked for a credit limit, it says that isn't recorded.

When a test needs earlier tool results, they are real outputs of the tools on this warehouse, saved by `python -m voice.scenarios --refresh`. A test can never be judged against numbers the warehouse does not hold.

## Status

Built and checked offline: the tools, the bridge, the test definitions, and every request `deploy.py` would send (validated against the ElevenLabs SDK's own models). Not yet run live: the next step is to deploy the agent, run the ten tests in ElevenAgents, and record a conversation with its timings.

## Run it

From the repository root, after the main README's setup and a built warehouse:

```bash
.venv/bin/pip install -r voice/requirements.txt          # on a Mac: brew install portaudio first
.venv/bin/python -m pytest -q tests/test_voice.py         # no account needed
.venv/bin/python -m voice.deploy --dry-run                # everything it would send

export ELEVENLABS_API_KEY=...                             # an ElevenLabs key with ElevenAgents access
.venv/bin/python -m voice.deploy                          # creates the tools, agent and tests
.venv/bin/python -m voice.deploy --run-tests              # runs the ten tests
.venv/bin/python -m voice.talk                            # talk to it; Ctrl-C ends the call
```

`--voice-id` (or `ERP_VOICE_ID`) picks the voice; without it the agent keeps ElevenLabs' default. `--llm` picks the model that runs the agent; the default is Claude Haiku 4.5, chosen for speed in conversation. Logs go to `voice/runs/`.
