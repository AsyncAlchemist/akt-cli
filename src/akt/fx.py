"""Foreign-exchange rate resolution for transaction input.

akt stores every foreign-currency transaction with a ``currency_rate`` snapshot
(foreign units per 1 unit of the company default currency); Akaunting then derives
the base amount as ``amount / currency_rate``. The web UI copies that rate from the
currency's live ``rate`` (kept fresh by the paid Live Currency app); over the API
akt must supply it. This module fetches the rate — live or **historical**, for the
transaction's own date — from keyless public feeds:

  * majors (~30 ECB currencies): Frankfurter (https://frankfurter.dev)
  * ARS (Argentine peso, which ECB does not publish): ArgentinaDatos (historical,
    by date) + dolarapi.com (latest), honouring a selectable ``casa``
    (oficial/blue/bolsa/contadoconliqui/…) and price side (mid/venta/compra).

Every provider takes an injectable ``get_json`` callable so the network layer is
trivially faked in tests. Historical rates are immutable and cached on disk; the
latest/today rate is volatile and never cached.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import requests

GetJson = Callable[[str], Any]

_FRANKFURTER_URL = os.environ.get("AKT_FX_FRANKFURTER_URL", "https://api.frankfurter.dev/v1")
_ARGENTINADATOS_URL = os.environ.get("AKT_FX_ARGENTINADATOS_URL", "https://api.argentinadatos.com/v1")
_DOLARAPI_URL = os.environ.get("AKT_FX_DOLARAPI_URL", "https://dolarapi.com/v1")

_ARS_CASAS = {"oficial", "blue", "bolsa", "contadoconliqui", "cripto",
              "mayorista", "solidario", "turista"}
_ARS_SIDES = {"mid", "venta", "compra"}


class FxError(ValueError):
    """A rate could not be resolved (unsupported currency, feed down, offline).

    Subclasses ValueError so the CLI's top-level handler surfaces it as a normal
    ``error: ...`` (exit 1) rather than an uncaught traceback.
    """


def _http_get_json(url: str) -> Any:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "akt-cli fx"})
    except requests.RequestException as e:  # pragma: no cover - network path
        raise FxError(f"exchange-rate request failed ({url}): {e}")
    if resp.status_code != 200:
        raise FxError(f"exchange-rate feed returned HTTP {resp.status_code} ({url})")
    try:
        return resp.json()
    except ValueError as e:  # pragma: no cover - network path
        raise FxError(f"exchange-rate feed returned non-JSON ({url}): {e}")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


class FrankfurterProvider:
    """ECB reference rates (majors), latest + historical by date. Returns foreign
    units per 1 USD, or None when the currency is not in ECB's set."""

    def __init__(self, base_url: str | None = None, get_json: GetJson | None = None):
        self.base_url = (base_url or _FRANKFURTER_URL).rstrip("/")
        self.get_json = get_json or _http_get_json

    def usd_per(self, code: str, on: date | None) -> Decimal | None:
        code = code.upper()
        segment = "latest" if on is None else on.strftime("%Y-%m-%d")
        url = f"{self.base_url}/{segment}?base=USD&symbols={code}"
        rates = (self.get_json(url) or {}).get("rates") or {}
        if code not in rates:
            return None
        return _dec(rates[code])


class ArgentinaProvider:
    """USD/ARS from the Argentine market: historical by date (ArgentinaDatos) and
    latest (dolarapi.com), for a chosen casa and price side. Returns ARS per 1 USD,
    or None for any non-ARS code (this feed is ARS-only)."""

    def __init__(self, casa: str = "bolsa", side: str = "mid",
                 historical_url: str | None = None, latest_url: str | None = None,
                 get_json: GetJson | None = None):
        self.casa = casa
        self.side = side
        self.historical_url = (historical_url or _ARGENTINADATOS_URL).rstrip("/")
        self.latest_url = (latest_url or _DOLARAPI_URL).rstrip("/")
        self.get_json = get_json or _http_get_json

    def usd_per(self, code: str, on: date | None) -> Decimal | None:
        if code.upper() != "ARS":
            return None
        if on is None:
            url = f"{self.latest_url}/dolares/{self.casa}"
        else:
            url = f"{self.historical_url}/cotizaciones/dolares/{self.casa}/{on.strftime('%Y/%m/%d')}"
        payload = self.get_json(url)
        if isinstance(payload, list):  # a couple of these endpoints wrap the row in a list
            payload = next((r for r in payload
                            if str(r.get("casa", self.casa)) == self.casa), payload[0] if payload else {})
        payload = payload or {}
        compra, venta = payload.get("compra"), payload.get("venta")
        if compra is None and venta is None:
            return None
        if self.side == "venta":
            return _dec(venta if venta is not None else compra)
        if self.side == "compra":
            return _dec(compra if compra is not None else venta)
        return (_dec(compra) + _dec(venta)) / 2  # mid


def _provider_for(code: str, ars_casa: str, ars_side: str,
                  get_json: GetJson | None) -> FrankfurterProvider | ArgentinaProvider:
    if code.upper() == "ARS":
        return ArgentinaProvider(casa=ars_casa, side=ars_side, get_json=get_json)
    return FrankfurterProvider(get_json=get_json)


def default_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "akt" / "fx"


def _cache_path(cache_dir: str, default_code: str, foreign_code: str, on: date,
                ars_casa: str, ars_side: str) -> Path:
    name = f"{default_code}_{foreign_code}_{on.isoformat()}_{ars_casa}_{ars_side}.json"
    return Path(cache_dir) / name


def resolve_rate(default_code: str, foreign_code: str, on: date | None, *,
                 ars_casa: str = "bolsa", ars_side: str = "mid",
                 cache_dir: str | None = None, get_json: GetJson | None = None,
                 today: date | None = None) -> Decimal:
    """Resolve ``currency_rate`` = foreign units per 1 unit of ``default_code`` for
    ``on`` (the transaction date; None = latest). Raises FxError if unresolvable."""
    default_code = (default_code or "USD").upper()
    foreign_code = (foreign_code or "").upper()
    if not foreign_code:
        raise FxError("no currency code given")
    if foreign_code == default_code:
        return Decimal(1)
    if ars_side not in _ARS_SIDES:
        raise FxError(f"invalid --ars-side {ars_side!r}; choose one of {sorted(_ARS_SIDES)}")
    if os.environ.get("AKT_FX_DISABLE"):
        raise FxError(f"AKT_FX_DISABLE is set — cannot resolve {foreign_code}->{default_code}; "
                      "pass --currency-rate to set it manually")

    today = today or date.today()
    is_historical = on is not None and on < today

    if is_historical and cache_dir:
        cp = _cache_path(cache_dir, default_code, foreign_code, on, ars_casa, ars_side)
        if cp.exists():
            try:
                return _dec(json.loads(cp.read_text())["rate"])
            except (ValueError, KeyError, OSError):
                pass  # corrupt cache entry — refetch

    def usd_per(code: str) -> Decimal:
        if code == "USD":
            return Decimal(1)
        prov = _provider_for(code, ars_casa, ars_side, get_json)
        rate = prov.usd_per(code, on)
        if rate is None or rate == 0:
            feed = "ArgentinaDatos/dolarapi" if code == "ARS" else "Frankfurter (ECB)"
            raise FxError(
                f"no exchange rate for {code} on {on or 'latest'} from {feed}; "
                "pass --currency-rate to set it manually")
        return rate

    rate = usd_per(foreign_code) / usd_per(default_code)

    if is_historical and cache_dir:
        try:
            cp = _cache_path(cache_dir, default_code, foreign_code, on, ars_casa, ars_side)
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps({"rate": str(rate)}))
        except OSError:  # pragma: no cover - best-effort cache
            pass
    return rate
