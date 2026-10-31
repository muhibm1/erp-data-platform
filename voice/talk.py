"""Talk to the finance desk through the Mac's microphone and speakers.

    ELEVENLABS_API_KEY=... python -m voice.talk

ElevenAgents handles listening, turn-taking, interruptions and the voice. This program only
streams audio both ways and answers the agent's tool calls from the local warehouse. Every
turn, tool call and timing goes to `voice/runs/session-<time>.jsonl`.

Live conversations use ElevenLabs credits. Nothing here runs in CI.
"""

import argparse
import json
import os
import signal
import time
from pathlib import Path

from edp import ask
from voice.bridge import Bridge, log

HERE = Path(__file__).resolve().parent
IDS = HERE / "agent-ids.json"
RUNS = HERE / "runs"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--agent-id", default=None, help="defaults to the id deploy.py saved")
    p.add_argument("--dsn", default=os.environ.get("EDP_DSN", "host=/tmp dbname=warehouse user=odoo"))
    args = p.parse_args()

    from elevenlabs.client import ElevenLabs
    from elevenlabs.conversational_ai.conversation import ClientTools, Conversation
    from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface

    agent_id = args.agent_id or json.loads(IDS.read_text())["agent_id"]
    session_log = RUNS / f"session-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"
    bridge = Bridge(args.dsn, session_log)
    tools = ClientTools()
    for name in ask.TOOLS:
        tools.register(name, bridge.handler(name))

    # How long the person waits: from ElevenLabs' final transcript of their turn to the first
    # chunk of the agent's voice arriving here. It leaves out the moment ElevenLabs takes to
    # decide the turn has ended, so the real wait is a little longer.
    turn = {"heard_at": None}

    class TimedAudio(DefaultAudioInterface):
        def output(self, audio):
            if turn["heard_at"] is not None:
                log(session_log, {"event": "reply_wait_ms",
                                  "value": round((time.monotonic() - turn["heard_at"]) * 1000)})
                turn["heard_at"] = None
            super().output(audio)

    def heard(event, text):
        if event == "you":
            turn["heard_at"] = time.monotonic()
        print(f"{event}: {text}")
        log(session_log, {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "text": text})

    conversation = Conversation(
        ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"]),
        agent_id,
        requires_auth=True,
        audio_interface=TimedAudio(),
        client_tools=tools,
        callback_user_transcript=lambda text: heard("you", text),
        callback_agent_response=lambda text: heard("agent", text),
        callback_agent_response_correction=lambda before, after: heard("agent_cut_off", after),
        # The connection's network round trip, as ElevenLabs measures it.
        callback_latency_measurement=lambda ms: log(session_log, {"event": "ping_ms", "value": ms}),
    )
    conversation.start_session()
    signal.signal(signal.SIGINT, lambda *_: conversation.end_session())
    conversation_id = conversation.wait_for_session_end()
    log(session_log, {"event": "ended", "conversation_id": conversation_id})
    print(f"Conversation {conversation_id}; log in {session_log.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
