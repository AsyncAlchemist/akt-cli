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


def build_recode_plan(item_ledgers: list[dict], category_by_txn: dict[int, int],
                      categories_by_id: dict[int, dict], accounts_by_id: dict[int, dict],
                      accounts_by_code: dict[int, int], coa: CoaConfig) -> list[dict]:
    """Per mis-coded item-leg, the ledger row + target account to repoint it to.

    Only rows whose transaction is in ``category_by_txn`` (the standalone
    income/expense set) and whose category maps to a mirror account that differs
    from where it currently sits are included."""
    plan: list[dict] = []
    for row in item_ledgers:
        txn = row["ledgerable_id"]
        cat = categories_by_id.get(category_by_txn.get(txn))
        expected = coa.by_category(cat["name"], cat["type"]) if cat else None
        if expected is None:
            continue
        to_id = accounts_by_code.get(expected.code)
        if to_id is None or row["account_id"] == to_id:
            continue                                   # unmapped, or already correct
        frm = accounts_by_id.get(row["account_id"]) or {}
        plan.append({
            "ledger_id": row["id"],
            "transaction_id": txn,
            "from_account_id": row["account_id"],
            "from_code": frm.get("code"),
            "to_account_id": to_id,
            "to_code": expected.code,
            "category": cat["name"],
        })
    return plan
