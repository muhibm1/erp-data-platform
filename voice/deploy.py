"""Creates or updates the finance desk in ElevenAgents: its six tools, the agent, and its tests.

    python -m voice.deploy --dry-run         # print everything it would send; no account needed
    ELEVENLABS_API_KEY=... python -m voice.deploy
    ELEVENLABS_API_KEY=... python -m voice.deploy --run-tests

Run again after a change and it updates the tools, agent and tests it created before, so
the workspace never fills up with copies. The ids are kept in `voice/agent-ids.json`.
Creating and updating is free; `--run-tests` and live conversations use ElevenLabs credits.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from voice import agent, scenarios

HERE = Path(__file__).resolve().parent
IDS = HERE / "agent-ids.json"


def test_bodies(tool_ids: dict[str, str]) -> list[dict]:
    """Each scenario as an ElevenAgents test request body."""
    return [scenarios.test_body(s, tool_ids) for s in scenarios.SCENARIOS]


def dry_run(llm: str, voice_id: str | None) -> dict:
    """Everything deploy would send, with placeholders where the workspace assigns ids."""
    tool_ids = {t["name"]: f"<id of {t['name']}>" for t in agent.tool_configs()}
    return {
        "tools": agent.tool_configs(),
        "agent": {"name": agent.NAME, "tags": [agent.TAG],
                  "conversation_config": agent.conversation_config(list(tool_ids.values()), llm, voice_id)},
        "tests": test_bodies(tool_ids),
    }


def deploy(client, llm: str, voice_id: str | None) -> dict:
    """Creates or updates every tool, the agent and every test; returns their ids."""
    from elevenlabs import ConversationalConfig, ToolRequestModel

    ids = json.loads(IDS.read_text()) if IDS.exists() else {}
    # Only ids this script recorded are updated, so a same-named tool elsewhere in the
    # workspace is never touched.
    tool_ids = dict(ids.get("tool_ids", {}))
    for config in agent.tool_configs():
        request = ToolRequestModel.model_validate({"tool_config": config})
        if config["name"] in tool_ids:
            client.conversational_ai.tools.update(tool_ids[config["name"]], request=request)
        else:
            tool_ids[config["name"]] = client.conversational_ai.tools.create(request=request).id

    config = ConversationalConfig.model_validate(agent.conversation_config(list(tool_ids.values()), llm, voice_id))
    if ids.get("agent_id"):
        client.conversational_ai.agents.update(ids["agent_id"], conversation_config=config,
                                               name=agent.NAME, tags=[agent.TAG])
        agent_id = ids["agent_id"]
    else:
        agent_id = client.conversational_ai.agents.create(conversation_config=config, name=agent.NAME,
                                                          tags=[agent.TAG]).agent_id

    from elevenlabs.conversational_ai.tests import TestsCreateRequestBody, TestsUpdateRequestBody
    from pydantic import TypeAdapter

    test_ids = dict(ids.get("test_ids", {}))
    for body in test_bodies(tool_ids):
        if body["name"] in test_ids:
            client.conversational_ai.tests.update(
                test_ids[body["name"]], request=TypeAdapter(TestsUpdateRequestBody).validate_python(body))
        else:
            test_ids[body["name"]] = client.conversational_ai.tests.create(
                request=TypeAdapter(TestsCreateRequestBody).validate_python(body)).id
    current = {s["name"] for s in scenarios.SCENARIOS}
    test_ids = {name: tid for name, tid in test_ids.items() if name in current}

    ids = {"agent_id": agent_id, "tool_ids": tool_ids, "test_ids": test_ids}
    IDS.write_text(json.dumps(ids, indent=2) + "\n")
    return ids


def run_tests(client, ids: dict) -> list[dict]:
    """Runs every test once against the agent and waits for the verdicts."""
    invocation = client.conversational_ai.agents.run_tests(
        ids["agent_id"], tests=[{"test_id": tid} for tid in ids["test_ids"].values()], name="voice.deploy")
    while True:
        result = client.conversational_ai.tests.invocations.get(invocation.id)
        if all(run.status not in ("pending", None) for run in result.test_runs):
            break
        time.sleep(5)
    return [{"test": run.test_name, "status": run.status, "credits_used": run.credits_used,
             "rationale": getattr(run.condition_result, "rationale", None)} for run in result.test_runs]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run-tests", action="store_true")
    p.add_argument("--llm", default=agent.DEFAULT_LLM)
    p.add_argument("--voice-id", default=os.environ.get("ERP_VOICE_ID"))
    args = p.parse_args()
    if args.dry_run:
        json.dump(dry_run(args.llm, args.voice_id), sys.stdout, indent=2)
        print()
        return

    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    ids = deploy(client, args.llm, args.voice_id)
    print(f"agent {ids['agent_id']}: {len(ids['tool_ids'])} tools, {len(ids['test_ids'])} tests")
    if args.run_tests:
        results = run_tests(client, ids)
        for r in results:
            print(f"{r['status']:>9}  {r['test']}")
        passed = sum(r["status"] == "passed" for r in results)
        print(f"{passed} of {len(results)} passed; "
              f"{sum(r['credits_used'] or 0 for r in results)} credits used")
        out = HERE / "runs" / f"tests-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
