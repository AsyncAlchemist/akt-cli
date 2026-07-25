"""Unit tests for the verify audit comparison."""
from __future__ import annotations

import pytest
from akt.coa import parse_coa
from akt.verify import find_miscodings

pytestmark = pytest.mark.unit

_COA = """
[[account]]
code = 628
name = "Other / Uncategorized"
type_id = 12
[[account]]
code = 615
name = "Hosting & Infrastructure"
type_id = 12
"""

# live lookups
_CATS = {52: {"name": "Other / Uncategorized", "type": "expense"},
         38: {"name": "Hosting & Infrastructure", "type": "expense"}}
_ACCTS_BY_ID = {91: {"code": 628, "name": "Other / Uncategorized"},
                90: {"code": 615, "name": "Hosting & Infrastructure"}}
_ACCTS_BY_CODE = {628: 91, 615: 90}


def _txn(tid, cat, amount=10.0):
    return {"id": tid, "paid_at": "2025-06-01", "amount": amount, "category_id": cat}


def test_flags_txn_posted_to_wrong_account():
    coa = parse_coa(_COA)
    txns = [_txn(1, 38)]                     # category Hosting -> expected 615
    item_account_by_txn = {1: 91}            # but actually posted to 628
    out = find_miscodings(txns, _CATS, _ACCTS_BY_ID, _ACCTS_BY_CODE, item_account_by_txn, coa)
    assert len(out) == 1
    assert out[0]["expected_code"] == 615 and out[0]["actual_code"] == 628


def test_passes_correctly_coded_txn():
    coa = parse_coa(_COA)
    txns = [_txn(1, 38)]
    item_account_by_txn = {1: 90}            # posted to 615 as expected
    assert find_miscodings(txns, _CATS, _ACCTS_BY_ID, _ACCTS_BY_CODE, item_account_by_txn, coa) == []


def test_flags_category_with_no_mirror_account():
    coa = parse_coa(_COA)
    txns = [_txn(1, 999)]                     # unknown category id
    out = find_miscodings(txns, {999: {"name": "Mystery", "type": "expense"}},
                          _ACCTS_BY_ID, _ACCTS_BY_CODE, {1: 91}, coa)
    assert len(out) == 1 and out[0]["reason"] == "category has no mirror account in COA"


def test_flags_txn_not_posted_at_all():
    coa = parse_coa(_COA)
    txns = [_txn(1, 38)]
    out = find_miscodings(txns, _CATS, _ACCTS_BY_ID, _ACCTS_BY_CODE, {}, coa)  # no ledger row
    assert len(out) == 1
    assert out[0]["actual_code"] is None
    assert out[0]["reason"] == "not posted to the ledger"
