"""Unit tests for akt ledger helpers."""
from __future__ import annotations

import pytest
from akt.ledger import resolve_account_id

pytestmark = pytest.mark.unit

_ACCOUNTS = [
    {"id": 91, "code": 628, "name": "Other / Uncategorized"},
    {"id": 82, "code": 105, "name": "Operating Checking"},
]


def test_resolve_by_numeric_code():
    assert resolve_account_id(_ACCOUNTS, "628") == 91


def test_resolve_by_name():
    assert resolve_account_id(_ACCOUNTS, "Operating Checking") == 82


def test_resolve_unknown_raises():
    with pytest.raises(ValueError):
        resolve_account_id(_ACCOUNTS, "nope")
