"""Unit tests for the financial report builders."""
from __future__ import annotations

import pytest
from akt.reports import (account_class, balances_by_id, build_trial_balance,
                         build_profit_loss, build_balance_sheet, reconcile)

pytestmark = pytest.mark.unit

# A tiny balanced ledger: cash 1000 (asset, dr), sales 1000 (income, cr),
# then an expense 300 paid from cash: expense 300 dr, cash 300 cr.
# Net: cash 700 dr, sales 1000 cr, expense 300 dr  -> debits 1000 == credits 1000.
_ACCTS = {
    10: {"code": 100, "name": "Cash",     "type_id": 6},   # asset (class 1)
    20: {"code": 400, "name": "Sales",    "type_id": 13},  # income (class 4)
    30: {"code": 600, "name": "Supplies", "type_id": 12},  # expense (class 3)
    40: {"code": 300, "name": "Owners Equity", "type_id": 16},  # equity (class 5)
}
_ROWS = [
    {"account_id": 10, "debit": 1000, "credit": 300},   # cash net +700 dr
    {"account_id": 20, "debit": 0,    "credit": 1000},  # sales net -1000 (cr)
    {"account_id": 30, "debit": 300,  "credit": 0},     # supplies +300 dr
]


def test_account_class_and_unknown_warns(capsys):
    assert account_class(6) == 1 and account_class(13) == 4 and account_class(12) == 3
    assert account_class(99) is None
    assert "type_id 99" in capsys.readouterr().err


def test_account_class_prefers_server_map():
    assert account_class(99, {99: 4}) == 4     # custom type resolved via the installation map
    assert account_class(6, {6: 4}) == 4       # server map is authoritative, overrides the seed
    assert account_class(6, {99: 4}) == 1      # map lacks 6 -> falls back to the seed (asset)


def test_profit_loss_uses_server_type_class_map():
    accts = {50: {"code": 900, "name": "Custom Rev", "type_id": 99}}
    balances = {50: -500.0}                     # credit balance
    assert build_profit_loss(balances, accts)["total_income"] == 0.0        # 99 unknown -> excluded
    assert build_profit_loss(balances, accts, {99: 4})["total_income"] == 500.0  # mapped -> income


def test_balances_by_id():
    assert balances_by_id(_ROWS) == {10: 700.0, 20: -1000.0, 30: 300.0}


def test_trial_balance_is_balanced():
    tb = build_trial_balance(balances_by_id(_ROWS), _ACCTS)
    assert tb["total_debit"] == 1000.0 and tb["total_credit"] == 1000.0
    assert tb["balanced"] is True
    cash = next(r for r in tb["rows"] if r["code"] == 100)
    assert cash["debit"] == 700.0 and cash["credit"] == 0.0
    sales = next(r for r in tb["rows"] if r["code"] == 400)
    assert sales["credit"] == 1000.0 and sales["debit"] == 0.0


def test_profit_loss_net():
    pl = build_profit_loss(balances_by_id(_ROWS), _ACCTS)
    assert pl["total_income"] == 1000.0 and pl["total_expense"] == 300.0
    assert pl["net_profit"] == 700.0
    assert pl["income"][0]["amount"] == 1000.0     # shown positive


def test_balance_sheet_balances():
    bs = build_balance_sheet(balances_by_id(_ROWS), _ACCTS)
    assert bs["total_assets"] == 700.0
    assert bs["net_income"] == 700.0               # income 1000 - expense 300
    # Assets == Liabilities + Equity + NetIncome  (0 + 0 + 700)
    assert bs["balanced"] is True


def test_reconcile():
    assert reconcile(700.0, 700.0)["ok"] is True
    r = reconcile(700.0, 650.0)
    assert r["ok"] is False and r["diff"] == 50.0


from akt.cli import _build_parser


def test_report_commands_parse():
    ns = _build_parser().parse_args(["balance", "--account", "105", "--to", "2025-12-31", "--expected", "5170.84"])
    assert ns._special == "balance" and ns.account == "105" and ns.expected == 5170.84
    ns = _build_parser().parse_args(["trial-balance", "--to", "2025-12-31"])
    assert ns._special == "trial_balance" and ns.date_to == "2025-12-31"
    ns = _build_parser().parse_args(["report", "profit-loss", "--from", "2025-01-01", "--to", "2025-12-31"])
    assert ns._special == "report_pnl"
    ns = _build_parser().parse_args(["report", "balance-sheet", "--to", "2025-12-31"])
    assert ns._special == "report_bs"


def test_gc_ledger_parses():
    ns = _build_parser().parse_args(["gc-ledger"])
    assert ns._special == "gc_ledger" and ns.apply is False
    ns = _build_parser().parse_args(["gc-ledger", "--apply"])
    assert ns.apply is True
