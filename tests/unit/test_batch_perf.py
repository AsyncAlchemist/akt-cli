"""Unit tests for the bulk-create performance + rate-limit-bypass features:
reference-list caching, one-time transaction-number seeding, and the
X-Akt-Bypass header / config token."""
import pytest

from akt.client import Client
from akt.config import Config, load_config
from akt.resources import _next_transaction_number

pytestmark = pytest.mark.unit


def _client(**kw):
    return Client(Config(base_url="http://x", email="e", password="p", company_id=1, **kw))


def test_list_ref_caches_per_client(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "list", lambda path, **kw: calls.append(path) or [{"id": 1}])
    assert c.list_ref("chart-of-accounts") == [{"id": 1}]
    assert c.list_ref("chart-of-accounts") == [{"id": 1}]
    assert calls == ["chart-of-accounts"]  # fetched exactly once, then cached


def test_next_transaction_number_seeds_ledger_once(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "setting", lambda k, d=None: "PAY-" if "prefix" in k else d)
    monkeypatch.setattr(c, "list",
                        lambda path, **kw: calls.append(path) or [{"number": "PAY-00005"},
                                                                   {"number": "PAY-00009"}])
    assert _next_transaction_number(c) == "PAY-00010"
    assert _next_transaction_number(c) == "PAY-00011"  # increments in-memory
    assert calls == ["transactions"]  # ledger scanned once, not per create


def test_config_loads_bypass_token(monkeypatch):
    for k in ("AKT_BASE_URL", "AKT_EMAIL", "AKT_PASSWORD", "AKT_API_BYPASS_TOKEN"):
        monkeypatch.setenv(k, {"AKT_API_BYPASS_TOKEN": "sekret"}.get(k, "x"))
    assert load_config().bypass_token == "sekret"


def test_client_sends_bypass_header_when_configured():
    assert _client(bypass_token="sekret")._session.headers.get("X-Akt-Bypass") == "sekret"
    assert "X-Akt-Bypass" not in _client()._session.headers  # absent when unset
