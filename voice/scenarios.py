"""Ten test conversations for the finance desk, run by ElevenAgents' own testing.

Two kinds, both built into ElevenAgents:

- **Tool tests** check what the agent does next: that it calls the right tool with the right
  arguments, such as `list_invoices` with `status` set to `overdue`.
- **Reply tests** check what it says, judged against a written pass condition with examples.

When a test needs earlier tool results, they are real outputs of the tools on this repository's
warehouse, saved in `voice/fixtures/` by `python -m voice.scenarios --refresh`.
`tests/test_voice.py` checks that they still match the warehouse, so a test can never be
judged against numbers the warehouse does not hold.
"""

import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Every saved tool result, by fixture name: the tool and the arguments that produced it.
# Customer keys are looked up when fixtures are refreshed, never typed in by hand.
FIXTURE_CALLS = {
    "find_northwind": ("find_customers", {"name": "northwind"}),
    "summary_northwind": ("customer_summary", {"customer_key": "@northwind"}),
    "over_90_days": ("query", {"sql": "select customer_name, late_over_90, total_open "
                                      "from marts.receivables_aging where late_over_90 > 0 "
                                      "order by late_over_90 desc"}),
}


def fixture(name: str) -> dict:
    """A saved tool call: {"tool", "args", "result"}."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def northwind_key() -> int:
    """Northwind's customer_key, from the saved find_customers result."""
    return fixture("find_northwind")["result"]["customers"][0]["customer_key"]


def resolved_args(args: dict, keys: dict[str, int]) -> dict:
    """Arguments with "@customer" placeholders replaced by real customer keys."""
    return {k: keys[v[1:]] if isinstance(v, str) and v.startswith("@") else v for k, v in args.items()}


def refresh(dsn: str) -> None:
    """Re-runs every fixture call on the warehouse and saves the results."""
    from edp import ask

    FIXTURES.mkdir(exist_ok=True)
    keys = {"northwind": ask.call(dsn, "find_customers", {"name": "northwind"})["customers"][0]["customer_key"]}
    for name, (tool, args) in FIXTURE_CALLS.items():
        args = resolved_args(args, keys)
        result = json.loads(json.dumps(ask.call(dsn, tool, args), default=str))
        (FIXTURES / f"{name}.json").write_text(
            json.dumps({"tool": tool, "args": args, "result": result}, indent=2) + "\n")


# A conversation so far, as turns: ("user", text), ("agent", text), or ("tool", fixture name).
LOOKED_UP_NORTHWIND = [
    ("user", "Look up Northwind for me."),
    ("tool", "find_northwind"),
    ("agent", "Found Northwind Logistics. It's entered twice in the ERP, merged into one customer here. "
              "What do you want to know?"),
]
NORTHWIND_SUMMARY = LOOKED_UP_NORTHWIND + [
    ("user", "What's their balance?"),
    ("tool", "summary_northwind"),
]

SCENARIOS = [
    {
        "name": "voice: finds the customer before anything else",
        "kind": "tool",
        "history": [("user", "How much does Northwind owe us?")],
        "tool": "find_customers",
        "params": [{"path": "name", "eval": {"type": "regex", "pattern": "(?i)northwind"}}],
    },
    {
        "name": "voice: uses the customer key it was given",
        "kind": "tool",
        "history": LOOKED_UP_NORTHWIND + [("user", "What do they owe?")],
        "tool": "customer_summary",
        "params": [{"path": "customer_key", "eval": {"type": "exact", "expected_value": "@northwind"}}],
    },
    {
        "name": "voice: asks for overdue invoices only",
        "kind": "tool",
        "history": LOOKED_UP_NORTHWIND + [("user", "Which of their invoices are overdue?")],
        "tool": "list_invoices",
        "params": [{"path": "customer_key", "eval": {"type": "exact", "expected_value": "@northwind"}},
                   {"path": "status", "eval": {"type": "exact", "expected_value": "overdue"}}],
    },
    {
        "name": "voice: follows deals to cash",
        "kind": "tool",
        "history": LOOKED_UP_NORTHWIND + [("user", "Did every deal we won with them turn into cash?")],
        "tool": "list_deals",
        "params": [{"path": "customer_key", "eval": {"type": "exact", "expected_value": "@northwind"}}],
    },
    {
        "name": "voice: answers a whole-company question with one query",
        "kind": "tool",
        "history": [("user", "Who's more than ninety days late?")],
        "tool": "query",
        "params": [{"path": "sql", "eval": {
            "type": "llm",
            "description": "A single SELECT on marts.receivables_aging that keeps only customers whose "
                           "late_over_90 amount is above zero."}}],
    },
    {
        "name": "voice: says the balance from the tool, rounded",
        "kind": "reply",
        "history": NORTHWIND_SUMMARY + [
            ("agent", "One moment."),
            ("user", "So what's the total they still owe?")],
        "success": "The agent says Northwind still owes the owed_usd amount from the customer_summary result, "
                   "rounded to the dollar, in one or two short sentences, and states no other amount that "
                   "is not in a tool result.",
        "success_examples": ["They still owe twenty-six thousand, eight hundred thirty-five dollars. "
                             "Want the invoices behind it?"],
        "failure_examples": ["They owe about thirty thousand dollars.",
                             "Here is the full breakdown: account.move/181@2026-10-27T09:21:59, ..."],
    },
    {
        "name": "voice: says lateness as of the aging date",
        "kind": "reply",
        "history": NORTHWIND_SUMMARY + [
            ("agent", "One moment."),
            ("user", "Are they late on any of it?")],
        "success": "The agent answers from the aging amounts in the customer_summary result and says the "
                   "figures are as of the aged_as_of date from that result.",
        "success_examples": ["Yes. As of October twenty-seventh, all of it is past due: about nineteen "
                             "thousand dollars up to thirty days late, and seven thousand four hundred "
                             "over ninety. Want the invoices?"],
        "failure_examples": ["No, they're all paid up.", "They're late by about two weeks, I think."],
    },
    {
        "name": "voice: keeps a long list short",
        "kind": "reply",
        "history": [("user", "Who's more than ninety days late?"), ("tool", "over_90_days"),
                    ("agent", "Got it."), ("user", "Read me the list.")],
        "success": "The agent names at most three customers from the query result, says how many more there "
                   "are if the result has more than three, uses no more than three short sentences, and "
                   "reads no source references aloud.",
        "success_examples": ["Maple Street Bakery, Harbor Freight Partners and Quarry Lane Fitness owe "
                             "the most over ninety days. Three more are on the list; want them?"],
        "failure_examples": ["Maple Street Bakery, Harbor Freight Partners, Quarry Lane Fitness, Nimbus Print, "
                             "and the rest, with account.move/83@2026-10-31T10:22:36 behind the first."],
    },
    {
        "name": "voice: refuses to change anything",
        "kind": "reply",
        "history": LOOKED_UP_NORTHWIND + [("user", "Mark their oldest invoice as paid.")],
        "success": "The agent says it can only read the warehouse and cannot mark invoices paid, suggests "
                   "accounting do it in the ERP, and does not claim the invoice was changed.",
        "success_examples": ["I can only read the warehouse, so I can't mark it paid. "
                             "Accounting can do that in the ERP."],
        "failure_examples": ["Done, I've marked it as paid.", "Sure, updating that now."],
    },
    {
        "name": "voice: says what the warehouse doesn't hold",
        "kind": "reply",
        "history": LOOKED_UP_NORTHWIND + [("user", "What's their credit limit?")],
        "success": "The agent says the credit limit isn't recorded in the warehouse and does not give a "
                   "number or guess one.",
        "success_examples": ["That isn't recorded in the warehouse."],
        "failure_examples": ["Their credit limit is fifty thousand dollars.",
                             "Probably around fifty thousand."],
    },
]


def chat_history(turns: list) -> list[dict]:
    """Turns as ElevenAgents chat history; a tool turn becomes an agent turn with its call and result."""
    history = []
    for i, turn in enumerate(turns):
        if turn[0] == "tool":
            saved = fixture(turn[1])
            rid = f"call_{i}"
            history.append({
                "role": "agent", "message": "", "time_in_call_secs": i,
                "tool_calls": [{"type": "client", "request_id": rid, "tool_name": saved["tool"],
                                "params_as_json": json.dumps(saved["args"]), "tool_has_been_called": True}],
                "tool_results": [{"type": "client", "request_id": rid, "tool_name": saved["tool"],
                                  "result_value": json.dumps(saved["result"]), "is_error": False,
                                  "tool_has_been_called": True}],
            })
        else:
            history.append({"role": turn[0], "message": turn[1], "time_in_call_secs": i})
    return history


def test_body(scenario: dict, tool_ids: dict[str, str]) -> dict:
    """One scenario as an ElevenAgents test request body."""
    keys = {"northwind": northwind_key()}
    body = {"name": scenario["name"], "chat_history": chat_history(scenario["history"])}
    if scenario["kind"] == "tool":
        params = []
        for p in scenario["params"]:
            check = dict(p["eval"])
            if check.get("expected_value", "").startswith("@"):
                check["expected_value"] = str(keys[check["expected_value"][1:]])
            params.append({"path": p["path"], "eval": check})
        body.update(type="tool", tool_call_parameters={
            "referenced_tool": {"id": tool_ids[scenario["tool"]], "type": "client"}, "parameters": params})
    else:
        body.update(type="llm", success_condition=scenario["success"],
                    success_examples=[{"type": "success", "response": r} for r in scenario["success_examples"]],
                    failure_examples=[{"type": "failure", "response": r} for r in scenario["failure_examples"]])
    return body


if __name__ == "__main__":
    if sys.argv[1:2] == ["--refresh"]:
        import os

        refresh(os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo"))
    else:
        sys.exit("usage: python -m voice.scenarios --refresh")
