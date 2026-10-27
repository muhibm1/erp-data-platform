"""The language model behind the agent. The graph needs only `model(system, prompt, schema) -> dict`.

Two routes satisfy that:

- `ChatModel` wraps any LangChain chat model (`ChatAnthropic` by default) and asks for the
  reply through `with_structured_output`, so each step is an ordinary LangChain call that
  LangSmith traces under the graph's run.
- `CommandLineModel` makes one `claude -p` call per step, with tools off and a JSON schema
  for the reply. API key variables are removed from the child process, so every call uses
  the command line's own sign-in. The scored runs in `eval/RESULTS.md` used this route.

`FakeModel` replays scripted replies, so the graph is tested without a model.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AGENTOPS_TEST_ANTHROPIC_API_KEY")
TIMEOUT = 300
ATTEMPTS = 3
LIMIT_WAIT = 600
_slots = threading.BoundedSemaphore(int(os.environ.get("ANALYST_PARALLEL", "4")))
_log_lock = threading.Lock()


class ModelError(RuntimeError):
    """A model call failed after every attempt."""


# The command line's short names, as model ids for the API.
MODEL_IDS = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "opus": "claude-opus-5-5"}


class ChatModel:
    """One LangChain chat-model call per step, its reply shaped by the step's JSON schema."""

    def __init__(self, llm=None, model: str = "sonnet", usage_log: str | Path | None = None):
        model = MODEL_IDS.get(model, model)
        if llm is None:
            from langchain_anthropic import ChatAnthropic  # imported here: the CLI route needs no SDK
            llm = ChatAnthropic(model=model, temperature=0, max_tokens=2048, max_retries=ATTEMPTS)
        self.llm = llm
        self.model = model
        self.usage_log = Path(usage_log) if usage_log else None

    def __call__(self, system: str, prompt: str, schema: dict) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage

        titled = {"title": "reply", "description": "The reply for this step.", **schema}
        chain = self.llm.with_structured_output(titled, include_raw=True)
        try:
            out = chain.invoke([SystemMessage(system), HumanMessage(prompt)])
        except Exception as exc:  # any provider error ends this step as a failed call, never a crash
            raise ModelError(f"model call failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
        parsed = out.get("parsed")
        if not isinstance(parsed, dict):
            raise ModelError(f"model reply did not fit the schema: {out.get('parsing_error')}")
        self._log(out.get("raw"))
        return parsed

    def _log(self, raw) -> None:
        if not self.usage_log or raw is None:
            return
        u = getattr(raw, "usage_metadata", None) or {}
        row = {"t": round(time.time(), 1), "model": self.model, "input": u.get("input_tokens", 0),
               "output": u.get("output_tokens", 0), "route": "langchain"}
        with _log_lock, self.usage_log.open("a") as f:
            f.write(json.dumps(row) + "\n")


class CommandLineModel:
    """One `claude -p` call per step. Tokens of every call go to `usage_log`."""

    def __init__(self, model: str = "sonnet", usage_log: str | Path | None = None):
        self.model = model
        self.usage_log = Path(usage_log) if usage_log else None

    def __call__(self, system: str, prompt: str, schema: dict) -> dict:
        exe = shutil.which("claude")
        if not exe:
            raise ModelError("the claude command is not installed")
        env = {k: v for k, v in os.environ.items() if k not in KEY_VARIABLES}
        args = [exe, "-p", "--output-format", "json", "--model", self.model, "--tools", "",
                "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                "--system-prompt", system, "--json-schema", json.dumps(schema)]
        last = ""
        attempt = 0
        while attempt < ATTEMPTS:
            try:
                with _slots, tempfile.TemporaryDirectory(prefix="analyst-") as cwd:
                    done = subprocess.run(args, input=prompt, capture_output=True, text=True,
                                          timeout=TIMEOUT, cwd=cwd, env=env, check=False)
                result = json.loads(done.stdout)
            except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                last = str(exc)[:200]
                attempt += 1
                continue
            if "hit your session limit" in str(result.get("result") or "").lower():
                time.sleep(LIMIT_WAIT)  # the usage window is spent: wait, don't fail
                continue
            attempt += 1
            out = result.get("structured_output")
            if not result.get("is_error") and isinstance(out, dict):
                self._log(result)
                return out
            last = str(result.get("result") or result.get("subtype"))[:200]
        raise ModelError(f"model call failed {ATTEMPTS} times: {last}")

    def _log(self, result: dict) -> None:
        if not self.usage_log:
            return
        u = result.get("usage") or {}
        row = {"t": round(time.time(), 1), "model": self.model,
               "input": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
               + (u.get("cache_creation_input_tokens") or 0),
               "output": u.get("output_tokens") or 0, "ms": result.get("duration_ms")}
        with _log_lock, self.usage_log.open("a") as f:
            f.write(json.dumps(row) + "\n")


class FakeModel:
    """Returns scripted replies in order and records every prompt it was sent."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, system: str, prompt: str, schema: dict) -> dict:
        self.prompts.append(prompt)
        if not self.replies:
            raise ModelError("FakeModel ran out of replies")
        return self.replies.pop(0)
