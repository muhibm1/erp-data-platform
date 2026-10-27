"""A domain: one warehouse the agent can answer questions about, described by a YAML file.

Adding a dataset means writing a domain file, not changing the agent. The file
names the database, the schemas the agent may read, how queries run, and the
knowledge the schema cannot carry: what tables mean, business terms, examples,
and any privacy rule. Column names and types are always read live from the
database, so the file cannot drift from the real schema without it showing.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

DOMAINS = Path(__file__).parent / "domains"


@dataclass
class Domain:
    name: str
    title: str
    dsn: str
    executor: str
    schemas: list[str]
    as_of: str | None
    tables: dict = field(default_factory=dict)
    terms: dict = field(default_factory=dict)
    examples: list = field(default_factory=list)
    privacy: dict | None = None
    reader_role: str | None = None
    citations: str | None = None
    columns: dict = field(default_factory=dict)  # "schema.table" -> [(column, type)], read live

    def connect(self):
        return psycopg.connect(self.dsn, autocommit=True)


def load(name_or_path: str) -> Domain:
    path = Path(name_or_path)
    if not path.suffix:
        path = DOMAINS / f"{name_or_path}.yaml"
    d = yaml.safe_load(path.read_text())
    dom = Domain(
        name=d["name"], title=d["title"],
        dsn=os.environ.get(d["dsn_env"], d["dsn_default"]),
        executor=d["executor"], schemas=list(d["schemas"]), as_of=d.get("as_of"),
        tables=d.get("tables") or {}, terms=d.get("terms") or {}, examples=d.get("examples") or [],
        privacy=d.get("privacy"), reader_role=d.get("reader_role"), citations=d.get("citations"),
    )
    return dom


def read_columns(dom: Domain, conn) -> dict:
    """Every table and column the agent may read, with types, from the live catalog."""
    with conn.cursor() as cur:
        cur.execute("""
            select table_schema || '.' || table_name, column_name, data_type
            from information_schema.columns
            where table_schema = any(%s)
            order by table_schema, table_name, ordinal_position""", (dom.schemas,))
        cols: dict = {}
        for table, column, kind in cur.fetchall():
            cols.setdefault(table, []).append((column, kind))
    dom.columns = cols
    return cols


def bare_schema(dom: Domain) -> str:
    """Table and column names with types only: what a plain text-to-SQL prompt gets."""
    return "\n".join(f"{t}({', '.join(f'{c} {k}' for c, k in cols)})" for t, cols in dom.columns.items())


def documents(dom: Domain) -> list[dict]:
    """The retrievable pieces of domain knowledge: one per table, term and example."""
    docs = []
    for table, cols in dom.columns.items():
        info = dom.tables.get(table) or {}
        notes = info.get("columns") or {}
        lines = [f"TABLE {table}: {info.get('about', '(no description)')}"]
        if info.get("notes"):
            lines.append(f"  Note: {info['notes']}")
        for c, k in cols:
            lines.append(f"  - {c} {k}" + (f": {notes[c]}" if c in notes else ""))
        docs.append({"kind": "table", "key": table, "text": "\n".join(lines)})
    for term, meaning in dom.terms.items():
        docs.append({"kind": "term", "key": term, "text": f"TERM {term}: {meaning}"})
    for i, ex in enumerate(dom.examples):
        docs.append({"kind": "example", "key": f"example-{i}",
                     "text": f"EXAMPLE\n  Question: {ex['question']}\n  SQL: {ex['sql']}"})
    return docs
