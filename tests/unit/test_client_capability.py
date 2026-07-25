"""Unit tests for the akt-api capability probe."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from akt.client import Client, ApiError

pytestmark = pytest.mark.unit


def _client() -> Client:
    c = Client.__new__(Client)          # bypass __init__/network
    c._capabilities = {}
    return c


def test_has_ledger_api_true_on_success():
    c = _client()
    c.get = MagicMock(return_value={"data": []})
    assert c.has_ledger_api() is True
    c.get.assert_called_once()


def test_has_ledger_api_false_on_404():
    c = _client()
    c.get = MagicMock(side_effect=ApiError(404, "not found"))
    assert c.has_ledger_api() is False


def test_has_ledger_api_reraises_non_404():
    c = _client()
    c.get = MagicMock(side_effect=ApiError(500, "server error"))
    with pytest.raises(ApiError):
        c.has_ledger_api()


def test_has_ledger_api_is_cached():
    c = _client()
    c.get = MagicMock(return_value={"data": []})
    c.has_ledger_api()
    c.has_ledger_api()
    c.get.assert_called_once()          # second call served from cache
