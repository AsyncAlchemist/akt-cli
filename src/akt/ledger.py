"""Helpers for the `akt ledger` command."""
from __future__ import annotations


def resolve_account_id(live_accounts: list[dict], ref: str) -> int:
    """Map a chart-of-accounts ref (numeric code or exact name) to its live id."""
    if str(ref).lstrip("-").isdigit():
        hit = next((a for a in live_accounts if int(a.get("code", -1)) == int(ref)), None)
    else:
        hit = next((a for a in live_accounts if a.get("name") == ref), None)
    if hit is None:
        raise ValueError(f"account {ref!r} not found in the chart of accounts")
    return int(hit["id"])
