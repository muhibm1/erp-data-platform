"""The analyst agent: a LangGraph flow from a plain-English question to a checked answer.

    retrieve ─▶ draft ─▶ execute (check, then run) ─▶ answer ─▶ cite ─▶ remember ─▶ END
                  ▲            │
                  └── repair ──┘   (at most MAX_TRIES queries)
                  │
                  └─▶ remember when the model decides the data cannot answer it

- retrieve: picks the tables, business terms and examples the question needs
  (`retrieve.py`). With `context=False` the model sees only bare table and
  column names, which is the ablation in the evaluation.
- draft: the model writes one query, or says the question cannot be answered
  from this data, or asks a clarifying question. It sees earlier turns of the
  conversation, and any failed attempt with its error.
- execute: code, not the model (`sql.py`). A query that fails either
  goes back to draft with the reason, up to MAX_TRIES times.
- answer: the model reads the rows and gives the answer as a value plus one
  sentence. It may still decline if the rows do not answer the question.
- cite: code collects source references from the rows and checks they are real
  and current (finance domain, through `edp.ask.check_citations`).

The conversation is saved per thread by the checkpointer, so "and what about
last month?" works as a follow-up.
"""

import json
import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from . import sql as sqltool
from .domain import Domain, bare_schema
from .retrieve import Retriever

MAX_TRIES = 3
ROWS_SHOWN = 60

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["query", "not_answerable", "clarify"]},
        "sql": {"type": "string", "description": "One PostgreSQL SELECT when decision is query, else empty."},
        "reason": {"type": "string", "description": "One sentence: why this query, or why not."},
    },
    "required": ["decision", "sql", "reason"],
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["answer", "not_answerable"]},
        "answer": {"description": "A number, a string, a list of strings, or null when not answerable.",
                   "anyOf": [{"type": "number"}, {"type": "string"},
                             {"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "explanation": {"type": "string", "description": "One plain sentence for a business reader."},
    },
    "required": ["status", "answer", "explanation"],
}


class State(TypedDict, total=False):
    question: str
    history: Annotated[list, operator.add]   # earlier turns in this thread, saved by the checkpointer
    context: str
    retrieved: list
    attempts: list                           # every query tried this turn, with its error if any
    sql: str
    rows: list
    status: str                              # answer | not_answerable | clarify | failed
    answer: Any
    explanation: str
    sources: list
    citations: dict


def _system(dom: Domain) -> str:
    return (
        f"You are a data analyst answering questions about {dom.title}. You work only from the "
        f"PostgreSQL warehouse described below, never from memory. Always name tables with their schema "
        f"({', '.join(dom.schemas)}). Write the simplest query that answers exactly what was asked. "
        + (f"Dates are as of {dom.as_of}. " if dom.as_of else "")
        + "If the warehouse has no data that could answer the question, say it is not answerable "
          "rather than guessing. Ask to clarify only when two readings would give different numbers "
          "and nothing in the question or the notes settles it."
    )


def build(dom: Domain, model, *, context: bool = True, retriever: Retriever | None = None, checkpointer=None,
          max_tries: int = MAX_TRIES):
    """Compile the graph for one domain. `model(system, prompt, schema) -> dict`."""
    from .domain import documents

    if context and retriever is None:
        retriever = Retriever(documents(dom))
    system = _system(dom)

    def retrieve(state: State) -> dict:
        if not context:
            return {"context": "TABLES (name and columns only):\n" + bare_schema(dom), "retrieved": [],
                    "attempts": [], "sources": [], "citations": {}}
        picked = retriever.select(state["question"])
        text = "\n\n".join(d["text"] for d in picked)
        others = [t for t in dom.columns if t not in {d["key"] for d in picked if d["kind"] == "table"}]
        if others:
            text += "\n\nOTHER TABLES (ask for nothing else; these exist if needed): " + ", ".join(others)
        return {"context": text, "retrieved": [d["key"] for d in picked], "attempts": [],
                "sources": [], "citations": {}}

    def draft(state: State) -> dict:
        parts = [f"WAREHOUSE NOTES\n{state['context']}"]
        if state.get("history"):
            turns = "\n".join(f"- Q: {h['question']}\n  SQL: {h.get('sql') or '(none)'}\n  A: {h.get('answer')}"
                              for h in state["history"][-4:])
            parts.append(f"EARLIER IN THIS CONVERSATION\n{turns}")
        if state.get("attempts"):
            tried = "\n".join(f"- {a['sql']}\n  FAILED: {a['error']}" for a in state["attempts"])
            parts.append(f"YOUR EARLIER ATTEMPTS AT THIS QUESTION\n{tried}\nFix the problem and try again.")
        parts.append(f"QUESTION\n{state['question']}")
        out = model(system, "\n\n".join(parts), DRAFT_SCHEMA)
        if out.get("decision") != "query":
            status = "clarify" if out.get("decision") == "clarify" else "not_answerable"
            return {"status": status, "sql": "", "answer": None, "explanation": out.get("reason", "")}
        return {"sql": out.get("sql", ""), "status": "running"}

    def execute(state: State) -> dict:
        with dom.connect() as conn:
            sqltool.prepare(dom, conn)
            result = sqltool.run(dom, conn, state["sql"])
        if not result["ok"]:
            attempts = state.get("attempts", []) + [{"sql": state["sql"], "error": result["error"]}]
            failed = len(attempts) >= max_tries
            return {"attempts": attempts, "rows": [], "status": "failed" if failed else "repair",
                    "explanation": result["error"] if failed else ""}
        return {"rows": result["rows"], "status": "ran",
                "attempts": state.get("attempts", []) + [{"sql": state["sql"], "error": None}]}

    def answer(state: State) -> dict:
        rows = state["rows"]
        shown = json.dumps(rows[:ROWS_SHOWN], default=str)
        more = f"\n({len(rows) - ROWS_SHOWN} more rows not shown)" if len(rows) > ROWS_SHOWN else ""
        prompt = (f"QUESTION\n{state['question']}\n\nQUERY YOU RAN\n{state['sql']}\n\n"
                  f"RESULT ({len(rows)} rows)\n{shown}{more}\n\n"
                  "Give the answer. A single amount or count is a number (dollars as a plain number, "
                  "no symbols); a list is a list of strings. If the query is the right one for the question, "
                  "no rows or a NULL total means nothing matched: the answer is 0 for an amount or count and "
                  "[] for a list. Decline only when the question asks for something this data does not "
                  "hold, or the query was the wrong one.")
        out = model(system, prompt, ANSWER_SCHEMA)
        status = out.get("status") if out.get("status") in ("answer", "not_answerable") else "answer"
        return {"status": status, "answer": out.get("answer"), "explanation": out.get("explanation", "")}

    def cite(state: State) -> dict:
        refs = sorted({str(r[c]) for r in state.get("rows", []) for c in r
                       if c == "source_ref" and r[c]})
        result = {}
        if refs and dom.citations == "edp":
            with dom.connect() as conn:
                result = sqltool._edp().check_citations(conn, refs)
        return {"sources": refs, "citations": result}

    def remember(state: State) -> dict:
        return {"history": [{"question": state["question"], "sql": state.get("sql"),
                             "answer": state.get("answer"), "status": state.get("status")}]}

    g = StateGraph(State)
    for name, fn in [("retrieve", retrieve), ("draft", draft), ("execute", execute), ("answer", answer),
                     ("cite", cite), ("remember", remember)]:
        g.add_node(name, fn)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_conditional_edges("draft", lambda s: "execute" if s["status"] == "running" else "remember")
    g.add_conditional_edges("execute", lambda s: {"repair": "draft", "failed": "remember"}.get(s["status"], "answer"))
    g.add_edge("answer", "cite")
    g.add_edge("cite", "remember")
    g.add_edge("remember", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


def ask(app, question: str, thread: str = "default") -> dict:
    """One turn. Returns the final state: status, answer, explanation, sql, attempts, sources."""
    return app.invoke({"question": question}, config={"configurable": {"thread_id": thread}})


def one_shot(dom: Domain, model, question: str) -> dict:
    """The baseline: one call with the bare schema, one query, its result taken as the answer."""
    prompt = f"TABLES (name and columns only):\n{bare_schema(dom)}\n\nQUESTION\n{question}"
    out = model(_system(dom), prompt, DRAFT_SCHEMA)
    if out.get("decision") != "query":
        return {"status": "not_answerable", "answer": None, "sql": "", "explanation": out.get("reason", "")}
    with dom.connect() as conn:
        sqltool.prepare(dom, conn)
        result = sqltool.run(dom, conn, out.get("sql", ""))
    if not result["ok"]:
        return {"status": "failed", "answer": None, "sql": out.get("sql"), "explanation": result["error"]}
    return {"status": "answer", "answer": value_of(result["rows"]), "sql": out.get("sql"), "explanation": ""}


def value_of(rows: list[dict]):
    """A result read the way a dashboard would: one cell is the answer, otherwise the first column."""
    if not rows:
        return []
    if len(rows) == 1 and len(rows[0]) == 1:
        v = next(iter(rows[0].values()))
        return v if not isinstance(v, list) else [str(x) for x in v]
    return [str(next(iter(r.values()))) for r in rows]
