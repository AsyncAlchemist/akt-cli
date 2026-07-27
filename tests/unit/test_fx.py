"""Offline tests for the FX rate resolver (akt.fx).

No network: every provider takes an injectable ``get_json`` callable, so these
exercise URL construction, response parsing, rate direction, routing, and the
historical cache without touching a live feed.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from akt import fx

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Frankfurter provider (ECB majors)
# --------------------------------------------------------------------------

def test_frankfurter_historical_direction_and_url():
    seen = {}

    def get(url):
        seen["url"] = url
        return {"amount": 1, "base": "USD", "date": "2025-05-30", "rates": {"EUR": 0.88}}

    r = fx.FrankfurterProvider(get_json=get).usd_per("EUR", date(2025, 6, 2))
    assert r == Decimal("0.88")
    assert "2025-06-02" in seen["url"]
    assert "base=USD" in seen["url"] and "symbols=EUR" in seen["url"]
    assert r < 1  # EUR-per-USD < 1 for a USD base — guards against an inverted rate


def test_frankfurter_latest_when_no_date():
    seen = {}

    def get(url):
        seen["url"] = url
        return {"rates": {"GBP": 0.79}}

    assert fx.FrankfurterProvider(get_json=get).usd_per("GBP", None) == Decimal("0.79")
    assert "latest" in seen["url"]


def test_frankfurter_unsupported_returns_none():
    # ECB does not publish ARS — the code is simply absent from `rates`.
    assert fx.FrankfurterProvider(get_json=lambda url: {"rates": {}}).usd_per(
        "ARS", date(2025, 6, 2)) is None


# --------------------------------------------------------------------------
# Argentina provider (ARS: ArgentinaDatos historical + dolarapi latest)
# --------------------------------------------------------------------------

def test_argentina_historical_mid_and_url():
    seen = {}

    def get(url):
        seen["url"] = url
        return {"moneda": "USD", "casa": "bolsa", "fecha": "2025-06-02",
                "compra": 1150, "venta": 1200}

    assert fx.ArgentinaProvider(get_json=get).usd_per("ARS", date(2025, 6, 2)) == Decimal("1175")
    assert "/cotizaciones/dolares/bolsa/2025/06/02" in seen["url"]


def test_argentina_side_venta_and_compra():
    resp = {"compra": 1150, "venta": 1200}
    assert fx.ArgentinaProvider(side="venta", get_json=lambda u: resp).usd_per(
        "ARS", date(2025, 6, 2)) == Decimal("1200")
    assert fx.ArgentinaProvider(side="compra", get_json=lambda u: resp).usd_per(
        "ARS", date(2025, 6, 2)) == Decimal("1150")


def test_argentina_list_response_shape():
    resp = [{"casa": "bolsa", "compra": 1150, "venta": 1200}]
    assert fx.ArgentinaProvider(get_json=lambda u: resp).usd_per(
        "ARS", date(2025, 6, 2)) == Decimal("1175")


def test_argentina_latest_uses_dolarapi_casa():
    seen = {}

    def get(url):
        seen["url"] = url
        return {"compra": 1150, "venta": 1200, "casa": "blue"}

    assert fx.ArgentinaProvider(casa="blue", get_json=get).usd_per("ARS", None) == Decimal("1175")
    assert "dolares/blue" in seen["url"]


def test_argentina_non_ars_returns_none_without_call():
    called = []

    def get(url):
        called.append(url)
        return {}

    assert fx.ArgentinaProvider(get_json=get).usd_per("EUR", date(2025, 6, 2)) is None
    assert called == []  # short-circuits before any HTTP


# --------------------------------------------------------------------------
# resolver: routing, hub/cross, cache, hard-fail
# --------------------------------------------------------------------------

def _boom(url):
    raise AssertionError(f"should not have fetched: {url}")


def test_resolve_same_currency_is_one_no_fetch():
    assert fx.resolve_rate("USD", "USD", date(2020, 1, 2), get_json=_boom) == Decimal(1)


def test_resolve_usd_base_eur():
    r = fx.resolve_rate("USD", "EUR", date(2020, 1, 2),
                        get_json=lambda u: {"rates": {"EUR": 0.88}}, cache_dir=None)
    assert r == Decimal("0.88")


def test_resolve_routes_ars_to_argentina(tmp_path):
    def get(url):
        assert "cotizaciones/dolares" in url  # ArgentinaDatos, not Frankfurter
        return {"compra": 1150, "venta": 1200}

    r = fx.resolve_rate("USD", "ARS", date(2020, 1, 2),
                        cache_dir=str(tmp_path), get_json=get)
    assert r == Decimal("1175")


def test_resolve_cross_via_usd_for_nonusd_base(tmp_path):
    # default EUR, foreign GBP: currency_rate = GBP-per-USD / EUR-per-USD
    def get(url):
        if "symbols=GBP" in url:
            return {"rates": {"GBP": 0.80}}
        if "symbols=EUR" in url:
            return {"rates": {"EUR": 0.90}}
        raise AssertionError(url)

    r = fx.resolve_rate("EUR", "GBP", date(2020, 1, 2),
                        cache_dir=str(tmp_path), get_json=get)
    assert r == Decimal("0.80") / Decimal("0.90")


def test_resolve_caches_historical(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return {"rates": {"EUR": 0.88}}

    on = date(2020, 1, 2)
    r1 = fx.resolve_rate("USD", "EUR", on, cache_dir=str(tmp_path), get_json=get)
    r2 = fx.resolve_rate("USD", "EUR", on, cache_dir=str(tmp_path), get_json=get)
    assert r1 == r2 == Decimal("0.88")
    assert len(calls) == 1  # second call served from the on-disk cache


def test_resolve_latest_not_cached(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return {"rates": {"EUR": 0.9}}

    fx.resolve_rate("USD", "EUR", None, cache_dir=str(tmp_path), get_json=get)
    fx.resolve_rate("USD", "EUR", None, cache_dir=str(tmp_path), get_json=get)
    assert len(calls) == 2  # today's/latest rate is volatile — never cached


def test_resolve_hard_fails_on_unsupported(tmp_path):
    with pytest.raises(fx.FxError):
        fx.resolve_rate("USD", "XYZ", date(2020, 1, 2),
                        cache_dir=str(tmp_path), get_json=lambda u: {"rates": {}})


def test_resolve_disabled_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("AKT_FX_DISABLE", "1")
    with pytest.raises(fx.FxError):
        fx.resolve_rate("USD", "EUR", date(2020, 1, 2), cache_dir=str(tmp_path),
                        get_json=lambda u: {"rates": {"EUR": 0.9}})
