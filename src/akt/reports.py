"""Financial report builders — pure functions over per-account balances.

A `balances` map is {account_id: debit - credit}. `accounts_by_id` is
{id: {"code":..., "name":..., "type_id":...}} from the chart of accounts.
"""
from __future__ import annotations

import sys

# DoubleEntry standard seeds (Database/Seeds/Types.php): type_id -> class_id.
CLASS_NAMES = {1: "Assets", 2: "Liabilities", 3: "Expenses", 4: "Income", 5: "Equity"}
TYPE_ID_CLASS = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 10: 1,   # assets (incl. depreciation)
                 7: 2, 8: 2, 9: 2, 17: 2,                      # liabilities (incl. tax)
                 11: 3, 12: 3,                                 # expenses (incl. direct costs)
                 13: 4, 14: 4, 15: 4,                          # income
                 16: 5}                                        # equity


def account_class(type_id) -> "int | None":
    c = TYPE_ID_CLASS.get(type_id)
    if c is None:
        print(f"warning: unknown account type_id {type_id}; excluded from "
              f"class-grouped reports", file=sys.stderr)
    return c


def balances_by_id(rows: list[dict]) -> dict[int, float]:
    return {int(r["account_id"]): round(float(r.get("debit") or 0)
                                        - float(r.get("credit") or 0), 2)
            for r in rows}


def _line(a: dict, amount: float) -> dict:
    return {"code": a.get("code"), "name": a.get("name"), "amount": round(amount, 2)}


def build_trial_balance(balances: dict[int, float], accounts_by_id: dict[int, dict]) -> dict:
    rows = []
    for aid, bal in balances.items():
        if abs(bal) < 0.005:
            continue
        a = accounts_by_id.get(aid, {})
        rows.append({"code": a.get("code"), "name": a.get("name"),
                     "debit": round(bal, 2) if bal > 0 else 0.0,
                     "credit": round(-bal, 2) if bal < 0 else 0.0})
    rows.sort(key=lambda r: (r["code"] is None, str(r["code"])))
    total_debit = round(sum(r["debit"] for r in rows), 2)
    total_credit = round(sum(r["credit"] for r in rows), 2)
    return {"rows": rows, "total_debit": total_debit, "total_credit": total_credit,
            "balanced": abs(total_debit - total_credit) < 0.02}


def build_profit_loss(balances: dict[int, float], accounts_by_id: dict[int, dict]) -> dict:
    income, expense = [], []
    for aid, bal in balances.items():
        a = accounts_by_id.get(aid, {})
        cls = account_class(a.get("type_id"))
        if cls == 4 and abs(bal) >= 0.005:
            income.append(_line(a, -bal))          # income is credit-positive
        elif cls == 3 and abs(bal) >= 0.005:
            expense.append(_line(a, bal))          # expense is debit-positive
    income.sort(key=lambda r: str(r["code"]))
    expense.sort(key=lambda r: str(r["code"]))
    total_income = round(sum(i["amount"] for i in income), 2)
    total_expense = round(sum(e["amount"] for e in expense), 2)
    return {"income": income, "expense": expense, "total_income": total_income,
            "total_expense": total_expense, "net_profit": round(total_income - total_expense, 2)}


def build_balance_sheet(balances: dict[int, float], accounts_by_id: dict[int, dict]) -> dict:
    assets, liabilities, equity = [], [], []
    net_income = 0.0
    for aid, bal in balances.items():
        a = accounts_by_id.get(aid, {})
        cls = account_class(a.get("type_id"))
        if cls == 1 and abs(bal) >= 0.005:
            assets.append(_line(a, bal))           # asset debit-positive
        elif cls == 2 and abs(bal) >= 0.005:
            liabilities.append(_line(a, -bal))     # liability credit-positive
        elif cls == 5 and abs(bal) >= 0.005:
            equity.append(_line(a, -bal))          # equity credit-positive
        elif cls in (3, 4):
            net_income += -bal                     # income(+) and expense(-) both -bal
    for s in (assets, liabilities, equity):
        s.sort(key=lambda r: str(r["code"]))
    total_assets = round(sum(x["amount"] for x in assets), 2)
    total_liabilities = round(sum(x["amount"] for x in liabilities), 2)
    total_equity = round(sum(x["amount"] for x in equity), 2)
    net_income = round(net_income, 2)
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "net_income": net_income, "total_assets": total_assets,
            "total_liabilities": total_liabilities, "total_equity": total_equity,
            "balanced": abs(total_assets - (total_liabilities + total_equity + net_income)) < 0.02}


def reconcile(actual: float, expected: float, tol: float = 0.005) -> dict:
    diff = round(actual - expected, 2)
    return {"actual": round(actual, 2), "expected": round(expected, 2),
            "diff": diff, "ok": abs(actual - expected) < tol}
