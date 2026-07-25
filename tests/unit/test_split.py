"""Offline tests for `payment create --split` — multi-GL-leg bank transactions.

Covers the split-leg parser, the build_payment_create split resolution + balance
guard, and the split_payment_legs post_write hook. resolve_coding is monkeypatched
(it has its own tests); the live endpoint is covered by tests/integration.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from akt.registry import PAYMENT
from akt.resources import parse_split_leg, build_payment_create, split_payment_legs

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# parse_split_leg
# --------------------------------------------------------------------------

def test_parse_split_leg_credit():
    assert parse_split_leg("account=400,credit=13686") == {"account": "400", "credit": 13686.0}


def test_parse_split_leg_debit():
    assert parse_split_leg("account=545,debit=280") == {"account": "545", "debit": 280.0}


def test_parse_split_leg_name_account():
    assert parse_split_leg("account=Customer Refunds,debit=5") == {
        "account": "Customer Refunds", "debit": 5.0}


def test_parse_split_leg_requires_account():
    with pytest.raises(ValueError):
        parse_split_leg("credit=10")


def test_parse_split_leg_requires_exactly_one_side():
    with pytest.raises(ValueError):
        parse_split_leg("account=400")                       # neither
    with pytest.raises(ValueError):
        parse_split_leg("account=400,debit=1,credit=1")      # both


def test_parse_split_leg_rejects_bare_field():
    with pytest.raises(ValueError):
        parse_split_leg("account=400,debit")


# --------------------------------------------------------------------------
# build_payment_create --split
# --------------------------------------------------------------------------

class _Client:
    """Minimal client: canned settings + empty transaction list for numbering."""
    def __init__(self, settings=None):
        self._settings = settings or {}

    def setting(self, key, default=None):
        return self._settings.get(key, default)

    def list(self, path, **kw):
        return []


_CANNED = {"400": (47, 2), "545": (108, 59), "605": (21, 60)}


@pytest.fixture
def _coded(monkeypatch):
    """Monkeypatch resolve_coding: account code -> (de_account_id, category_id)."""
    def fake(config, client, *, account_ref=None, category_ref=None):
        return _CANNED[account_ref]
    monkeypatch.setattr("akt.resources.resolve_coding", fake)


def _payment_ns(**over):
    base = dict(
        type="income", invoice=None, bill=None, document_id=None, contact_id=None,
        category_id=None, account=None, category=None, split=None, amount=None,
        account_id=20, currency_code=None, currency_rate=None, number="TXN-1",
        reference=None, description="PayPro settlement 2025-01",
        payment_method=None, set_=None, data=None, _coa=object(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_split_resolves_legs_and_placeholder(_coded):
    ns = _payment_ns(amount=13400, split=[
        "account=400,credit=13686", "account=545,debit=280", "account=605,debit=6"])
    body = build_payment_create(PAYMENT, _Client(), ns)
    # placeholder item leg = first leg's account; category = first leg's mirror
    assert body["de_account_id"] == 47
    assert body["category_id"] == 2
    # resolved legs stashed for the post_write hook
    assert ns._split_resolved == [
        {"account_id": 47, "credit": 13686.0},
        {"account_id": 108, "debit": 280.0},
        {"account_id": 21, "debit": 6.0},
    ]


def test_split_income_balance_ok(_coded):
    # income: legs net to -amount (charge credit 13686 - refund/fee debits 286 = -13400)
    ns = _payment_ns(amount=13400, split=[
        "account=400,credit=13686", "account=545,debit=280", "account=605,debit=6"])
    build_payment_create(PAYMENT, _Client(), ns)   # no raise


def test_split_expense_balance_ok(_coded):
    # expense: legs net to +amount
    ns = _payment_ns(type="expense", amount=100, split=[
        "account=545,debit=130", "account=400,credit=30"])
    build_payment_create(PAYMENT, _Client(), ns)   # 130 - 30 = 100 == +amount


def test_split_imbalanced_raises(_coded):
    ns = _payment_ns(amount=13400, split=[
        "account=400,credit=13686", "account=545,debit=280"])   # nets -13406, needs -13400
    with pytest.raises(ValueError, match="net"):
        build_payment_create(PAYMENT, _Client(), ns)


def test_split_and_account_mutually_exclusive(_coded):
    ns = _payment_ns(amount=100, account="400", split=["account=400,credit=100"])
    with pytest.raises(ValueError, match="either --split or --account"):
        build_payment_create(PAYMENT, _Client(), ns)


def test_split_requires_coa(_coded):
    ns = _payment_ns(amount=100, split=["account=400,credit=100"], _coa=None)
    with pytest.raises(ValueError, match="COA config"):
        build_payment_create(PAYMENT, _Client(), ns)


def test_split_explicit_category_id_wins(_coded):
    ns = _payment_ns(amount=13400, category_id=99, split=[
        "account=400,credit=13686", "account=545,debit=280", "account=605,debit=6"])
    body = build_payment_create(PAYMENT, _Client(), ns)
    assert body["category_id"] == 99          # explicit --category-id not overridden by leg


# --------------------------------------------------------------------------
# split_payment_legs post_write hook
# --------------------------------------------------------------------------

class _HookClient:
    def __init__(self, *, has_api=True, item_legs=1):
        self._has_api = has_api
        self._item_legs = item_legs
        self.posts = []

    def has_ledger_api(self):
        return self._has_api

    def get(self, path, params=None, **kw):
        assert path == "akt-api/ledgers"
        assert params["entry_type"] == "item"
        rows = [{"id": 900 + i} for i in range(self._item_legs)]
        return {"data": rows}

    def post(self, path, json_body, **kw):
        self.posts.append((path, json_body))
        return {"data": json_body["legs"]}


def test_hook_noop_without_split():
    c = _HookClient()
    split_payment_legs(PAYMENT, c, {"id": 5}, SimpleNamespace())
    assert c.posts == []


def test_hook_posts_split():
    c = _HookClient(item_legs=1)
    legs = [{"account_id": 47, "credit": 13686.0}, {"account_id": 108, "debit": 280.0}]
    ns = SimpleNamespace(_split_resolved=legs)
    split_payment_legs(PAYMENT, c, {"id": 77}, ns)
    assert c.posts == [("akt-api/ledgers/900/split", {"legs": legs})]


def test_hook_requires_module():
    c = _HookClient(has_api=False)
    ns = SimpleNamespace(_split_resolved=[{"account_id": 47, "credit": 1.0}])
    with pytest.raises(ValueError, match="akt-api companion module"):
        split_payment_legs(PAYMENT, c, {"id": 1}, ns)


def test_hook_requires_single_item_leg():
    c = _HookClient(item_legs=2)                 # already split / ambiguous
    ns = SimpleNamespace(_split_resolved=[{"account_id": 47, "credit": 1.0}])
    with pytest.raises(ValueError, match="exactly one item leg"):
        split_payment_legs(PAYMENT, c, {"id": 1}, ns)
