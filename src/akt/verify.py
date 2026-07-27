"""Read-and-compare audit: each standalone income/expense transaction's actual
posted GL account vs the COA mirror of its category."""
from __future__ import annotations

from .coa import CoaConfig
from .reports import account_class

# GL account class (from reports.account_class) -> the transaction type Akaunting's
# DoubleEntry COA P&L report requires for a posting to that account to be counted.
_CLASS_EXPECTS_TYPE = {3: "expense", 4: "income"}


def find_report_dropped(transactions: list[dict], item_account_by_txn: dict[int, int],
                        accounts_by_id: dict[int, dict],
                        type_class: "dict[int, int] | None" = None) -> list[dict]:
    """Flag standalone transactions Akaunting's COA P&L report silently DROPS.

    The DoubleEntry report has a type-guard: a posting is counted only when the
    backing transaction's type matches the GL account's class. So an *income*
    transaction posted to an *expense* account (a refund/reversal booked as income),
    or an *expense* transaction on an *income* account, is ignored — its amount
    never reaches the P&L even though the ledger balances. The correct vehicle for
    an expense refund / income reversal is a **journal entry**, which the report
    nets. Pure — all lookups pre-fetched; accounts_by_id values must carry type_id."""
    findings: list[dict] = []
    for t in transactions:
        ttype = t.get("type")
        if ttype not in ("income", "expense"):
            continue
        acct = accounts_by_id.get(item_account_by_txn.get(t["id"]))
        if not acct:
            continue
        expects = _CLASS_EXPECTS_TYPE.get(account_class(acct.get("type_id"), type_class))
        if expects is None or ttype == expects:
            continue                                   # not a P&L account, or types agree
        findings.append({
            "transaction_id": t["id"],
            "paid_at": str(t.get("paid_at", ""))[:10],
            "amount": t.get("amount"),
            "category": None,
            "expected_code": None,
            "actual_code": acct.get("code"),
            "reason": f"{ttype} txn posts to {expects}-class account {acct.get('code')} — "
                      "COA report silently drops this; book as a journal entry",
        })
    return findings


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


def find_unposted(transactions: list[dict], item_account_by_txn: dict[int, int],
                  banks_by_id: dict[int, dict]) -> list[dict]:
    """Flag standalone income/expense transactions that posted no item leg.

    DoubleEntry's Transaction observer posts a transaction's ledger legs only if
    its bank has a ``double_entry_account_bank`` mapping; a payment on an unmapped
    bank silently posts nothing. A transaction whose id is absent from
    ``item_account_by_txn`` (txn id -> posted item-leg GL account) has no item leg.
    Pure — all lookups pre-fetched."""
    findings: list[dict] = []
    for t in transactions:
        if t["id"] in item_account_by_txn:
            continue                                   # posted an item leg — fine
        bank = banks_by_id.get(t.get("account_id"))
        bank_label = bank["name"] if bank else t.get("account_id")
        findings.append({
            "transaction_id": t["id"],
            "paid_at": str(t.get("paid_at", ""))[:10],
            "amount": t.get("amount"),
            "bank": bank["name"] if bank else None,
            "category": None,
            "expected_code": None,
            "actual_code": None,
            "reason": f"posted no ledger legs — bank '{bank_label}' is likely "
                      "unmapped; run DoubleEntry's CopyData install job",
        })
    return findings
