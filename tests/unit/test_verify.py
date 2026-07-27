"""Unit tests for the verify audit comparison."""
from __future__ import annotations

import pytest
from akt.coa import parse_coa
from akt.verify import find_miscodings, build_recode_plan

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


def test_build_recode_plan_targets_only_wrong_item_legs():
    coa = parse_coa(_COA)
    item_ledgers = [
        {"id": 500, "ledgerable_id": 1, "account_id": 91},   # txn1 posted to 628 (wrong)
        {"id": 501, "ledgerable_id": 2, "account_id": 90},   # txn2 posted to 615 (correct)
        {"id": 502, "ledgerable_id": 9, "account_id": 91},   # txn9 not in our set -> skip
    ]
    category_by_txn = {1: 38, 2: 38}                          # both Hosting -> expected 615
    plan = build_recode_plan(item_ledgers, category_by_txn, _CATS,
                             _ACCTS_BY_ID, _ACCTS_BY_CODE, coa)
    assert len(plan) == 1
    p = plan[0]
    assert p["ledger_id"] == 500 and p["transaction_id"] == 1
    assert p["from_code"] == 628 and p["to_code"] == 615 and p["to_account_id"] == 90


# --- find_report_dropped: postings the COA report silently ignores ---
from akt.verify import find_report_dropped  # noqa: E402


def test_find_report_dropped_flags_type_class_mismatch():
    accounts = {91: {"code": 530, "name": "Hosting", "type_id": 11},   # expense class
                40: {"code": 400, "name": "Revenue", "type_id": 13}}   # income class
    txns = [{"id": 1, "type": "income", "paid_at": "2025-11-23", "amount": 247.5},   # refund-as-income on expense acct
            {"id": 2, "type": "expense", "paid_at": "2025-06-01", "amount": 10.0},   # expense on expense acct -> ok
            {"id": 3, "type": "expense", "paid_at": "2025-06-01", "amount": 5.0}]    # expense on income acct -> flag
    item_account_by_txn = {1: 91, 2: 91, 3: 40}
    findings = find_report_dropped(txns, item_account_by_txn, accounts)
    assert {f["transaction_id"] for f in findings} == {1, 3}
    assert all("journal entry" in f["reason"] for f in findings)
    assert findings[0]["actual_code"] == 530


def test_find_report_dropped_ignores_non_pnl_and_matches():
    accounts = {91: {"code": 530, "name": "Hosting", "type_id": 12},   # expense
                10: {"code": 105, "name": "Checking", "type_id": 6}}   # asset -> not P&L
    txns = [{"id": 1, "type": "expense", "paid_at": "2025-06-01", "amount": 10.0},   # ok
            {"id": 2, "type": "income", "paid_at": "2025-06-01", "amount": 10.0}]    # asset acct -> ignore
    assert find_report_dropped(txns, {1: 91, 2: 10}, accounts) == []


# --- find_unposted: transactions that posted no ledger legs (silent banks) ---
from akt.verify import find_unposted  # noqa: E402

_BANKS = {1: {"name": "Cash"}, 2: {"name": "Checking"}}


def _paytxn(tid, account_id, amount=10.0):
    return {"id": tid, "paid_at": "2025-06-01", "amount": amount, "account_id": account_id}


def test_unposted_flags_txn_with_no_item_leg():
    txns = [_paytxn(1, account_id=1)]
    out = find_unposted(txns, item_account_by_txn={}, banks_by_id=_BANKS)
    assert len(out) == 1
    assert out[0]["transaction_id"] == 1
    assert out[0]["bank"] == "Cash"
    assert "unmapped" in out[0]["reason"]


def test_unposted_skips_txn_that_posted():
    txns = [_paytxn(1, account_id=1)]
    assert find_unposted(txns, item_account_by_txn={1: 90}, banks_by_id=_BANKS) == []


def test_unposted_falls_back_to_account_id_when_bank_unknown():
    txns = [_paytxn(1, account_id=99)]
    out = find_unposted(txns, item_account_by_txn={}, banks_by_id=_BANKS)
    assert len(out) == 1
    assert out[0]["bank"] is None
    assert "99" in out[0]["reason"]
