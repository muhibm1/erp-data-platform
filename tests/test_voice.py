"""The voice finance desk: its tools, its bridge to the warehouse, and its test conversations.

No ElevenLabs account or SDK is needed. Checks that need the built warehouse skip without it;
checks that need the SDK (only to validate request shapes) skip when it is not installed.
"""

import inspect
import json
import os
import re

import psycopg
import pytest

from edp import ask
from voice import agent, deploy, scenarios
from voice.bridge import Bridge, ToolError

DSN = os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo")


@pytest.fixture(scope="module")
def warehouse():
    try:
        with psycopg.connect(DSN, connect_timeout=3) as c:
            c.execute("select 1 from marts.revenue_by_customer limit 1")
    except psycopg.Error:
        pytest.skip("no built warehouse")
    return DSN


def test_every_warehouse_tool_is_a_voice_tool_with_its_real_arguments():
    configs = {t["name"]: t for t in agent.tool_configs()}
    assert list(configs) == list(ask.TOOLS)
    for name, fn in ask.TOOLS.items():
        params = list(inspect.signature(fn).parameters.values())[1:]
        schema = configs[name]["parameters"]
        assert list(schema["properties"]) == [p.name for p in params], name
        assert schema["required"] == [p.name for p in params if p.default is inspect.Parameter.empty], name
        assert configs[name]["type"] == "client" and configs[name]["expects_response"]


def test_overdue_is_one_of_the_statuses_the_agent_can_pick():
    status = next(t for t in agent.tool_configs() if t["name"] == "list_invoices")["parameters"]["properties"]["status"]
    assert status["enum"] == ["all", "unpaid", "overdue"]


@pytest.mark.parametrize("name, params, message", [
    ("drop_table", {}, "unknown tool"),
    ("find_customers", {"name": "x", "limit": 5}, "does not take limit"),
])
def test_bridge_refuses_calls_the_tools_do_not_accept(name, params, message, tmp_path):
    log = tmp_path / "s.jsonl"
    with pytest.raises(ToolError, match=message):
        Bridge("dbname=unused", log).call(name, params)
    entry = json.loads(log.read_text())
    assert entry["tool"] == name and message in entry["error"]


def test_bridge_answers_from_the_warehouse_and_logs_the_call(warehouse, tmp_path):
    log = tmp_path / "s.jsonl"
    out = json.loads(Bridge(warehouse, log).call("find_customers", {"name": "northwind", "tool_call_id": "t1"}))
    assert out["customers"][0]["customer_name"] == "Northwind Logistics"
    entry = json.loads(log.read_text())
    assert entry["args"] == {"name": "northwind"} and "error" not in entry


def test_bridge_keeps_the_read_only_rules(warehouse):
    out = json.loads(Bridge(warehouse).call("query", {"sql": "delete from marts.revenue_by_customer"}))
    assert out == {"ok": False, "error": "one SELECT statement only"}


def _without_versions(result) -> str:
    """A result as text with each source reference's version time blanked: a rebuilt warehouse
    saves the same records at a new time, so only the record, not the time, must match."""
    return re.sub(r"@\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "@<version>", json.dumps(result, default=str))


def test_saved_tool_results_still_match_the_warehouse(warehouse):
    for name in scenarios.FIXTURE_CALLS:
        saved = scenarios.fixture(name)
        live = ask.call(warehouse, saved["tool"], saved["args"])
        assert _without_versions(live) == _without_versions(saved["result"]), name


def test_ten_scenarios_each_checking_a_real_tool():
    names = [s["name"] for s in scenarios.SCENARIOS]
    assert len(names) == 10 == len(set(names))
    for s in scenarios.SCENARIOS:
        assert s["history"][-1][0] == "user", s["name"]
        if s["kind"] == "tool":
            assert s["tool"] in ask.TOOLS
        else:
            assert s["success_examples"] and s["failure_examples"]


def test_reply_examples_use_the_real_balance():
    owed = scenarios.fixture("summary_northwind")["result"]["owed_usd"]
    assert owed == 26835.0  # the example below says it in words
    balance = next(s for s in scenarios.SCENARIOS if "balance" in s["name"])
    assert "twenty-six thousand, eight hundred thirty-five" in balance["success_examples"][0]


def test_dry_run_builds_every_request_without_an_account():
    plan = deploy.dry_run(agent.DEFAULT_LLM, None)
    assert len(plan["tools"]) == 6 and len(plan["tests"]) == 10
    prompt = plan["agent"]["conversation_config"]["agent"]["prompt"]
    assert prompt["temperature"] == 0 and len(prompt["tool_ids"]) == 6
    key = str(scenarios.northwind_key())
    summary = next(t for t in plan["tests"] if t["name"] == "voice: uses the customer key it was given")
    assert summary["tool_call_parameters"]["parameters"][0]["eval"]["expected_value"] == key


def test_requests_match_the_elevenlabs_sdk():
    elevenlabs = pytest.importorskip("elevenlabs")
    from elevenlabs.conversational_ai.tests import TestsCreateRequestBody
    from pydantic import TypeAdapter

    plan = deploy.dry_run(agent.DEFAULT_LLM, None)
    for tool in plan["tools"]:
        elevenlabs.ToolRequestModel.model_validate({"tool_config": tool})
    elevenlabs.ConversationalConfig.model_validate(plan["agent"]["conversation_config"])
    for body in plan["tests"]:
        TypeAdapter(TestsCreateRequestBody).validate_python(body)
