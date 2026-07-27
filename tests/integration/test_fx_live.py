"""Live integration tests for foreign-currency support.

The `fx` smoke tests hit the public rate feeds (Frankfurter / ArgentinaDatos) and
need no Akaunting module. The conversion test needs the UPDATED akt-api module
deployed; it self-skips when the instance still runs an older akt-api (probed via
the new /account-types endpoint), so it never fails a release against a
not-yet-redeployed instance.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _has_updated_akt_api(akt) -> bool:
    return akt("raw", "GET", "akt-api/account-types", raw=True).returncode == 0


def _unwrap(payload):
    return payload["data"] if isinstance(payload, dict) and "data" in payload else payload


def _ensure_currency(akt, code, name, rate):
    """Make sure `code` exists on the instance (Akaunting 422s a transaction in an
    unconfigured currency). Idempotent; left in place (harmless on the disposable
    instance) so teardown never has to delete a currency a transaction referenced."""
    rows = _unwrap(akt("currency", "list", "--all"))
    if not any(str(c.get("code")) == code for c in rows):
        akt("currency", "create", "--name", name, "--code", code, "--rate", str(rate))


def test_fx_eur_live(akt):
    out = akt("fx", "EUR", "--to", "USD")
    assert out["code"] == "EUR" and out["base"] == "USD"
    assert 0 < out["rate"] < 1        # EUR per 1 USD is < 1 — direction guard
    assert out["inverse"] > 1


def test_fx_ars_historical_live(akt):
    out = akt("fx", "ARS", "--to", "USD", "--on", "2025-06-02")
    assert out["code"] == "ARS"
    assert out["rate"] > 100          # pesos per USD are in the hundreds/thousands


def test_balances_convert_foreign_leg(akt, tracker):
    if not _has_updated_akt_api(akt):
        pytest.skip("instance runs an older akt-api without base-currency conversion")

    _ensure_currency(akt, "EUR", "Euro", 0.9)
    # A EUR expense at an explicit rate of 0.5 => base = 100 / 0.5 = 200 (default
    # currency). Explicit --currency-rate keeps this deterministic (no feed call).
    created = _unwrap(akt("payment", "create", "--type", "expense", "--amount", "100",
                          "--currency-code", "EUR", "--currency-rate", "0.5"))
    txn_id = created["id"]
    tracker("payment", txn_id)

    legs = _unwrap(akt("raw", "GET", "akt-api/ledgers",
                       "--query", f"ledgerable_id={txn_id}",
                       "--query", "entry_type=item", "--query", "convert=1"))
    assert legs, "no item leg posted (is the bank mapped to a DoubleEntry account?)"
    leg = legs[0]
    assert leg["currency_code"] == "EUR"
    assert float(leg["debit"]) == 100.0            # as posted, in EUR
    assert float(leg["debit_converted"]) == 200.0  # base = 100 / 0.5
