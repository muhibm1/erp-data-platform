"""Unit tests for the parts that need no Odoo and no database."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))

from build import split_of  # noqa: E402
from run import correct, parse  # noqa: E402

from edp.sources import DOMAINS, SOURCES, table_name


def test_table_names():
    assert table_name("crm.lead") == "crm_lead"
    assert table_name("account.move") == "account_move"


def test_every_domain_is_for_a_known_source():
    assert set(DOMAINS) <= set(SOURCES)


def test_split_is_stable_and_two_way():
    assert split_of(41) == split_of(41)
    assert {split_of(k) for k in range(1, 60)} == {"dev", "test"}


def test_parse_reads_json_answers():
    assert parse('{"answer": 12.5}') == 12.5
    assert parse('Sure: {"answer": ["INV/1"]} done') == ["INV/1"]
    assert parse("no json here") is None


def test_money_is_right_within_a_cent():
    row = {"answer_type": "money", "answer": 100.0}
    assert correct(row, 100.004)
    assert not correct(row, 100.02)
    assert not correct(row, None)


def test_lists_must_match_exactly():
    row = {"answer_type": "set", "answer": ["INV/1", "INV/2"]}
    assert correct(row, ["INV/2", "INV/1"])
    assert not correct(row, ["INV/1"])
    assert not correct(row, "INV/1, INV/2")


def test_a_missing_source_field_stops_the_load_and_names_it():
    import pytest

    from edp.load import SourceChanged, _fields
    present = {f: True for f in SOURCES["crm.lead"] if f != "expected_revenue"}
    with pytest.raises(SourceChanged, match="expected_revenue"):
        _fields(present, "crm.lead")


def test_custom_fields_are_loaded_too():
    from edp.load import _fields
    present = {**{f: True for f in SOURCES["account.move"]}, "x_po_number": True, "x_computed": False}
    got = _fields(present, "account.move")
    assert "x_po_number" in got and "x_computed" not in got
