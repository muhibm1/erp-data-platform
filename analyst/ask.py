"""Ask the analyst a question from the command line.

  python -m analyst.ask "Which three customers owe us the most?"
  python -m analyst.ask --domain ads --thread t1 "Which country had the most installs last week?"
  python -m analyst.ask --thread t1 "And the week before?"     # follow-up in the same conversation
"""

import argparse
import json
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from . import domain as domains
from .graph import ask, build
from .model import CommandLineModel


def main():
    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--domain", default="finance")
    p.add_argument("--thread", default="cli")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--memory", default=str(Path(__file__).parent / ".threads.sqlite"), help="where conversations are saved")
    a = p.parse_args()
    dom = domains.load(a.domain)
    with dom.connect() as conn:
        domains.read_columns(dom, conn)
    with SqliteSaver.from_conn_string(a.memory) as saver:
        app = build(dom, CommandLineModel(a.model), checkpointer=saver)
        out = ask(app, a.question, a.thread)
    print(json.dumps({k: out.get(k) for k in ("status", "answer", "explanation", "sql", "sources")},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
