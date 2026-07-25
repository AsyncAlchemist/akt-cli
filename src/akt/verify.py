"""Read-and-compare audit: each standalone income/expense transaction's actual
posted GL account vs the COA mirror of its category."""
from __future__ import annotations

from .coa import CoaConfig


def find_miscodings(transactions: list[dict], categories_by_id: dict[int, dict],
                    accounts_by_id: dict[int, dict], accounts_by_code: dict[int, int],
                    item_account_by_txn: dict[int, int], coa: CoaConfig) -> list[dict]:
    """Return one finding per transaction whose actual posted item-leg GL account
    differs from the account that mirrors its category. Pure — all lookups are
    passed in pre-fetched."""
    findings: list[dict] = []
    for t in transactions:
        cat = categories_by_id.get(t.get("category_id"))
        cat_name = cat["name"] if cat else None
        expected = coa.by_category(cat_name, cat["type"]) if cat else None
        actual_id = item_account_by_txn.get(t["id"])
        actual = accounts_by_id.get(actual_id) if actual_id is not None else None

        if expected is None:
            reason = "category has no mirror account in COA"
        else:
            expected_id = accounts_by_code.get(expected.code)
            if actual_id == expected_id:
                continue                                   # correctly coded
            reason = "posted to the wrong GL account" if actual_id is not None \
                else "not posted to the ledger"

        findings.append({
            "transaction_id": t["id"],
            "paid_at": str(t.get("paid_at", ""))[:10],
            "amount": t.get("amount"),
            "category": cat_name,
            "expected_code": expected.code if expected else None,
            "expected_name": expected.name if expected else None,
            "actual_code": actual["code"] if actual else None,
            "actual_name": actual["name"] if actual else None,
            "reason": reason,
        })
    return findings
