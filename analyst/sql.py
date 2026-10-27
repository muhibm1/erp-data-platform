"""Checking and running the agent's SQL.

Two layers, so a mistake in one is caught by the other:

1. `check` parses the query with sqlglot before anything runs. It must be one
   SELECT (or WITH ... SELECT), every table it names must sit in one of the
   domain's schemas, and it may not contain a write, a DDL statement or a
   server-side function that reads files. The agent gets the reason back and
   can fix its query.
2. The database decides what actually runs. For the finance warehouse, queries
   go through `edp.ask.query` unchanged: a read-only role over the modelled
   schemas, a read-only transaction and a 5 second limit. For other domains the
   same rules run here, with the domain's own read-only role, plus its privacy
   rule.
"""

import datetime as dt
import re
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import sqlglot
from sqlglot import exp

MAX_ROWS = 200
BANNED_FUNCTIONS = re.compile(r"(?i)\b(pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|dblink\w*|"
                              r"pg_sleep|set_config|current_setting|pg_terminate_backend|pg_cancel_backend)\s*\(")
WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge,
          exp.TruncateTable, exp.Grant)


def check(sql: str, schemas: list[str]) -> tuple[bool, str, list[str]]:
    """(ok, reason, tables). Static checks on one query before it runs."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        return False, "empty query", []
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        return False, f"does not parse: {str(exc).splitlines()[0]}", []
    if len(statements) != 1:
        return False, "send exactly one statement", []
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return False, "only a SELECT query may run", []
    if any(tree.find_all(*WRITES)):
        return False, "the query may only read", []
    if BANNED_FUNCTIONS.search(text):
        return False, "that server function is not allowed", []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = []
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        schema = (t.db or "").lower()
        if not schema and name in ctes:
            continue
        if not schema:
            return False, f"name the schema for table {name} (one of {schemas})", []
        if schema not in schemas:
            return False, f"table {schema}.{name} is outside the readable schemas {schemas}", []
        tables.append(f"{schema}.{name}")
    return True, "", sorted(set(tables))


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _edp():
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import edp.ask
    return edp.ask


def prepare(dom, conn) -> None:
    """Make sure the domain's read-only role exists before the first query."""
    if dom.executor == "edp":
        _edp().ensure_reader(conn)


def run(dom, conn, sql: str) -> dict:
    """Runs one checked query. Returns {ok, rows, truncated} or {ok: False, error}."""
    ok, reason, _ = check(sql, dom.schemas)
    if not ok:
        return {"ok": False, "error": reason}
    if dom.executor == "edp":
        return _edp().query(conn, sql)
    text = sql.strip().rstrip(";")
    runs = [text]
    if dom.privacy:
        ok, reason = privacy_check(text, dom.privacy)
        if not ok:
            return {"ok": False, "error": reason}
        for probe in inner_group_probes(text, dom.privacy):
            try:
                smallest = _execute(dom, conn, probe)[0]["m"]
            except psycopg.Error:
                return {"ok": False, "error": "the size of a group inside this query could not be checked; "
                                              "write it without a correlated subquery over people counts"}
            if smallest is not None and smallest < dom.privacy["min_count"]:
                return {"ok": False, "error": f"a group inside this query holds fewer than "
                                              f"{dom.privacy['min_count']} people, so its figures cannot be shown"}
        augmented = with_helpers(text, dom.privacy, dom.columns)
        if augmented != text:
            runs = [augmented, text]
    for attempt in runs:
        try:
            rows = _execute(dom, conn, attempt)
        except psycopg.Error as exc:
            if attempt is not runs[-1]:
                continue  # the helper columns did not fit this query: run it as written
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"}
        text = attempt
        break
    out = {"ok": True, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}
    if dom.privacy:
        out = apply_privacy(out, dom.privacy, text, dom.columns)
        if out.get("needs_count"):
            return {"ok": False, "error": out["needs_count"]}
    return out


def _execute(dom, conn, text: str) -> list[dict]:
    """Runs one query under the domain's read-only role and returns its rows."""
    with conn.transaction():
        conn.execute("set transaction read only")
        conn.execute("set local statement_timeout = '5s'")
        conn.execute(f"set local role {dom.reader_role}")
        with conn.cursor() as cur:
            cur.execute(f"select * from ({text}) q limit {MAX_ROWS + 1}")
            cols = [c.name for c in cur.description]
            return [{k: _plain(v) for k, v in zip(cols, r)} for r in cur.fetchall()]


HELPER = "__people_"


def _reads(node, counted: set) -> bool:
    return any(c.name.lower() in counted for c in node.find_all(exp.Column))


def _plain_people(e, counted: set) -> bool:
    """Whether an aggregate's argument is a counted column as it is, or picked by a CASE."""
    while isinstance(e, (exp.Cast, exp.Paren, exp.Distinct)):
        e = e.expressions[0] if isinstance(e, exp.Distinct) else e.this
    if isinstance(e, exp.Column):
        return True
    if isinstance(e, exp.AggFunc):  # sum(sum(installs)) over (): a total of counts is a count
        return _plain_people(e.this, counted) and not e.expressions
    if isinstance(e, exp.Case):
        branches = [i.args.get("true") for i in e.args.get("ifs", [])] + [e.args.get("default")]
        return all(b is None or isinstance(b, exp.Null) or (isinstance(b, exp.Literal) and b.this == "0")
                   or _plain_people(b, counted) for b in branches)
    return not _reads(e, counted)


def privacy_check(sql: str, rule: dict) -> tuple[bool, str]:
    """(ok, reason). Refuses query shapes whose people figures could not be checked by value.

    Every people figure in a result is checked against the threshold after the query runs,
    including the ones behind a rate (see `with_helpers`). That holds only if each one is a
    count of people: so inside an aggregate over a counted column (say `sum(installs)`), the
    column must appear as it is or picked by a CASE, never scaled (`sum(installs * 100)` would
    turn 12 people into 1,200). A subquery over people counts inside the selected columns is
    refused for the same reason."""
    counted = {c.lower() for c in rule["count_columns"]}
    tree = sqlglot.parse_one(sql, read="postgres")
    for agg in tree.find_all(exp.AggFunc):
        if _reads(agg, counted) and (not _plain_people(agg.this, counted) or agg.expressions):
            return False, (f"inside {agg.sql(dialect='postgres')}, aggregate the people count column as "
                           f"it is (a CASE or FILTER may pick rows); do arithmetic outside the aggregate")
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            for sub in projection.find_all(exp.Subquery):
                if _reads(sub, counted):
                    return False, "a subquery over people counts inside the selected columns is not allowed here"
    return True, ""


_WRAPPERS = (exp.Alias, exp.Cast, exp.Round, exp.Paren, exp.Coalesce)
_PLAIN_AGGREGATES = (exp.Sum, exp.Count, exp.Max, exp.Min)


def _plain_step(e) -> bool:
    """Whether one lineage step passes a counted column on unchanged, or summed or counted,
    over all rows or over rows picked by a FILTER or a CASE."""
    while isinstance(e, _WRAPPERS):
        e = e.this
    if isinstance(e, exp.Filter):
        e = e.this
    if isinstance(e, _PLAIN_AGGREGATES):
        e = e.this.expressions[0] if isinstance(e.this, exp.Distinct) else e.this
        while isinstance(e, (exp.Cast, exp.Paren)):
            e = e.this
        if isinstance(e, exp.Case):
            branches = [i.args.get("true") for i in e.args.get("ifs", [])] + [e.args.get("default")]
            return all(b is None or isinstance(b, (exp.Null, exp.Column))
                       or (isinstance(b, exp.Literal) and b.this == "0") for b in branches)
    return isinstance(e, exp.Column)


def _kind(node, counted: set) -> str | None:
    """'count' when every path from this lineage node to a counted column is plain, 'derived'
    when some path does more to it, None when no counted column is reached."""
    if not node.downstream:
        return "count" if node.name.split(".")[-1].lower() in counted else None
    kinds = {_kind(child, counted) for child in node.downstream} - {None}
    if not kinds:
        return None
    if "derived" in kinds or not (isinstance(node.expression, exp.Table) or _plain_step(node.expression)):
        return "derived"
    return "count"


def _aggregated_below(node) -> bool:
    """Whether a lineage path below this node passes through an aggregate (in a CTE or subquery)."""
    return any(child.expression.find(exp.AggFunc) is not None or _aggregated_below(child)
               for child in node.downstream if not isinstance(child.expression, exp.Table))


def _schema(columns: dict | None) -> dict | None:
    schema: dict = {}
    for table, cols in (columns or {}).items():
        db, _, name = table.partition(".")
        schema.setdefault(db, {})[name] = {c: str(k) for c, k in cols}
    return schema or None


def people_columns(sql: str, counted: set, columns: dict | None) -> list[str | None] | None:
    """For each selected column in order: 'count', 'derived' or None (no people count behind
    it), traced through CTEs, subqueries and unions with sqlglot's lineage. A column that
    cannot be traced counts as derived, so a query too complex to read fails closed. None when
    the query cannot be read at all."""
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.qualify import qualify
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
        if not parsed.find(exp.Star) and not _reads(parsed, counted):
            return [None] * len(parsed.selects)
        tree = qualify(parsed, schema=_schema(columns), dialect="postgres")
    except Exception:  # noqa: BLE001 - a query that cannot be qualified fails closed
        return None
    kinds = []
    for select in tree.selects:
        try:
            node = lineage(select.alias_or_name, tree, schema=_schema(columns), dialect="postgres")
            kind = _kind(node, counted)
            if kind == "derived" and _sum_of_counts(select):
                kind = "count"  # total minus a part is a count too: checked by value, so a small difference shows as blank
            elif kind == "derived" and select.find(exp.AggFunc) is None and _aggregated_below(node):
                kind = "inner"  # a rate computed in a CTE or subquery, whose groups the probes check
            kinds.append(kind)
        except Exception:  # noqa: BLE001 - any failure to trace fails closed
            kinds.append("derived")
    return kinds


def _sum_of_counts(projection) -> bool:
    """Whether a selected column adds or subtracts aggregates (and constants), and nothing else,
    as `sum(installs) - sum(installs) filter (where ...)` does. Columns are left out: the
    difference of two rates from a CTE is not a count."""
    e = projection.this if isinstance(projection, exp.Alias) else projection
    while isinstance(e, (exp.Paren, exp.Neg, exp.Cast, exp.Round, exp.Coalesce)):
        e = e.this
    return isinstance(e, (exp.Add, exp.Sub)) and _terms(e)


def _terms(e) -> bool:
    if isinstance(e, (exp.AggFunc, exp.Filter, exp.Literal)):
        return True
    if isinstance(e, (exp.Add, exp.Sub)):
        return _terms(e.this) and _terms(e.expression)
    if isinstance(e, (exp.Paren, exp.Neg, exp.Cast, exp.Round, exp.Coalesce)):
        return _terms(e.this)
    return False


def inner_group_probes(sql: str, rule: dict) -> list[str]:
    """One query per CTE or subquery that aggregates a people count, returning the smallest
    such figure over its groups as `m`. A rate computed inside a CTE (say, a conversion rate per
    test variant) reaches the result with its counts left behind, so the counts are checked
    where they are made: the query is refused if any group there is under the threshold."""
    counted = {c.lower() for c in rule["count_columns"]}
    tree = sqlglot.parse_one(sql, read="postgres")
    probes = []
    for select in tree.find_all(exp.Select):
        if select is tree:
            continue  # the outer SELECT's figures are checked row by row
        atoms = [a for projection in select.expressions for a in projection.find_all(exp.AggFunc, exp.Filter)
                 if _reads(a, counted) and not isinstance(a.parent, exp.Filter) and _scope_of(a) is select]
        if not atoms:
            continue
        probe = select.copy()
        least = exp.func("least", *[a.copy() for a in atoms]) if len(atoms) > 1 else atoms[0].copy()
        probe.set("expressions", [least.as_("m")])
        for key in ("order", "limit", "offset", "distinct"):
            probe.set(key, None)
        if tree.args.get("with") is not None:
            probe.set("with", tree.args["with"].copy())
        probes.append(f"select min(m) as m from ({probe.sql(dialect='postgres')}) p")
    return probes


def _scope_of(node):
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


def with_helpers(sql: str, rule: dict, columns: dict | None) -> str:
    """The query with every people figure behind a derived column added as a column of its
    own, so it can be checked by value: for `sum(installs) / sum(taps) as cvr`, the two sums
    become `__people_a0` and `__people_a1`. An aggregate over a people count is always checked
    (`privacy_check` makes sure it counts people); a bare column only when it traces to one. The query is returned unchanged when nothing needs
    adding or it is not one plain SELECT (a UNION, or SELECT DISTINCT, whose rows extra
    columns would change)."""
    counted = {c.lower() for c in rule["count_columns"]}
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return sql
    kinds = people_columns(sql, counted, columns)
    if not isinstance(tree, exp.Select) or tree.args.get("distinct") or not kinds \
            or len(kinds) != len(tree.expressions):
        return sql
    atoms = []
    for projection, kind in zip(tree.expressions, kinds):
        if kind != "derived":
            continue
        for node in projection.walk():
            if isinstance(node, (exp.AggFunc, exp.Filter)) and _reads(node, counted) and not any(
                    isinstance(a, (exp.AggFunc, exp.Filter)) for a in _ancestors(node, projection)):
                atoms.append(("a", node))
            elif isinstance(node, exp.Column) and not any(
                    isinstance(a, (exp.AggFunc, exp.Filter, exp.Window)) for a in _ancestors(node, projection)):
                atoms.append(("c", node))
    if not atoms:
        return sql
    for i, (kind, atom) in enumerate(atoms):
        tree = tree.select(atom.copy().as_(f"{HELPER}{kind}{i}"), copy=False)
    return tree.sql(dialect="postgres")


def _ancestors(node, stop):
    parent = node.parent
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.parent


def reads_people(sql: str, counted: set, columns: dict | None) -> bool:
    """Whether a query reads any table with a people count column. True when unsure: a table
    the schema does not list, or a query that does not parse."""
    if not columns:
        return True
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:  # noqa: BLE001 - fails closed
        return True
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    known = {t.lower(): {c.lower() for c, _ in cols} for t, cols in columns.items()}
    for table in tree.find_all(exp.Table):
        if not table.db and table.name.lower() in ctes:
            continue  # a CTE: its own tables are checked where it is defined
        cols = known.get(f"{table.db}.{table.name}".lower())
        if cols is None or cols & counted:
            return True
    return False


def apply_privacy(result: dict, rule: dict, sql: str | None = None, columns: dict | None = None) -> dict:
    """Suppress any result row built from too few people.

    A count column is any result column that passes a counted column (say, installs) through
    unchanged, summed or counted, whatever it is named: with `sql`, each column is traced back
    to its source with sqlglot's lineage; without it, only columns with the counted names are
    known. Helper columns added by `with_helpers` (the people figures behind a rate or share)
    are checked the same way and then removed. A row with any people figure under the
    threshold has all its numbers blanked and is marked suppressed. A row that carries a
    figure derived from a people count but no checkable count is refused as a whole, with
    `needs_count` naming the fix, because a rate with its count hidden can still describe a
    handful of people. With `require_count`, any row with numbers needs a count column."""
    k = rule["min_count"]
    counted = {c.lower() for c in rule["count_columns"]}
    if sql and not reads_people(sql, counted, columns):
        return {**result, "suppressed": 0}  # no table it reads holds a people count
    names = list(result["rows"][0]) if result["rows"] else []
    kinds = dict.fromkeys(names)
    traced = people_columns(sql, counted, columns) if sql and names else None
    if traced is not None and len(traced) == len(names):
        kinds = dict(zip(names, traced))  # matched by position: Postgres and sqlglot name unnamed columns differently
    elif sql and names:
        kinds = {n: "derived" for n in names}
    for name in names:
        if name.lower() in counted and kinds.get(name) is None:
            kinds[name] = "count"
    helpers = [n for n in names if n.startswith(HELPER)]
    checked = [c for c, kind in kinds.items()
               if kind == "count" or (c.startswith(HELPER + "a") and kind)]
    derived = [c for c, kind in kinds.items() if kind == "derived" and c not in helpers]
    suppressed = 0
    needs_count = ""
    rows = []
    for row in result["rows"]:
        small = any(isinstance(row.get(c), (int, float)) and row[c] < k for c in checked)
        missing = not checked and (
            any(isinstance(row.get(c), (int, float)) for c in derived)
            or (rule.get("require_count", False) and _has_numbers(row)))
        if missing:
            needs_count = (f"every row with a figure derived from {'/'.join(rule['count_columns'])} must "
                           f"also select the plain count for the same group, such as sum(installs); "
                           f"rows under {k} are then blanked")
        if small or missing:
            suppressed += 1
            row = {c: (None if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                   for c, v in row.items()}
            row["_suppressed"] = f"fewer than {k} {'/'.join(rule['count_columns'])}" if small else \
                f"select a count column ({', '.join(rule['count_columns'])}) with any metric"
        rows.append({c: v for c, v in row.items() if not c.startswith(HELPER)})
    out = {**result, "rows": rows, "suppressed": suppressed}
    if needs_count:
        out["needs_count"] = needs_count
    return out


def _has_numbers(row: dict) -> bool:
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row.values())
