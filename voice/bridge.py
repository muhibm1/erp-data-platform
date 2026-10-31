"""Runs the voice agent's tool calls against the local warehouse and logs each one.

ElevenAgents sends a client tool call as a name and a dict of parameters (plus its own
`tool_call_id`). The bridge checks the parameters against the tool's declared arguments,
runs the tool from `edp/ask.py` on a fresh connection, and returns the result as JSON text.
A bad call comes back as an error message the agent can say, never as a crash that would
end the conversation.
"""

import inspect
import json
import time
from pathlib import Path

from edp import ask


class ToolError(ValueError):
    """A tool call the bridge refused: an unknown tool or arguments the tool does not take."""


class Bridge:
    def __init__(self, dsn: str, log_path: Path | None = None):
        self.dsn = dsn
        self.log_path = log_path

    def call(self, name: str, parameters: dict) -> str:
        """Runs one tool call and returns its result as JSON text. Raises ToolError on a bad call."""
        started = time.monotonic()
        args = {k: v for k, v in parameters.items() if k != "tool_call_id"}
        try:
            fn = ask.TOOLS.get(name)
            if fn is None:
                raise ToolError(f"unknown tool {name}")
            accepted = list(inspect.signature(fn).parameters)[1:]
            unexpected = sorted(set(args) - set(accepted))
            if unexpected:
                raise ToolError(f"{name} does not take {', '.join(unexpected)}")
            result = ask.call(self.dsn, name, args)
        except Exception as exc:
            self._log(name, args, started, error=f"{type(exc).__name__}: {exc}")
            raise
        self._log(name, args, started)
        return json.dumps(result, default=str)

    def handler(self, name: str):
        """A function in the shape the ElevenLabs SDK's ClientTools.register expects."""
        return lambda parameters: self.call(name, parameters)

    def _log(self, name, args, started, error=None):
        if self.log_path is None:
            return
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": "tool", "tool": name,
                 "args": args, "ms": round((time.monotonic() - started) * 1000)}
        if error:
            entry["error"] = error
        log(self.log_path, entry)


def log(path: Path, entry: dict) -> None:
    """Appends one JSON line to a session log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
