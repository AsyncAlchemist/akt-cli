"""Full-surface integration tests against a live Akaunting 3.x instance.

Each test creates real records, exercises them, and registers them with the
`tracker` fixture so they are deleted on teardown (even on assertion failure).
Test data is prefixed `AKT-IT` and tagged with a per-run token to stay
identifiable and avoid collisions with prior runs.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

# Per-run token (tests run in one process) to keep unique numbers/codes distinct.
RID = str(int(time.time()))[-6:]


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True")


def _amount(obj) -> float:
    return float(obj.get("amount") or 0)


# --------------------------------------------------------------------------
# connectivity
# --------------------------------------------------------------------------

def test_ping_and_company(akt):
    pong = akt("ping")
    assert pong.get("status") == "ok"
    companies = akt("company")
    assert isinstance(companies, list) and companies, "expected at least one company"


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------

def test_customer_lifecycle(akt, tracker):
    c = akt("customer", "create", "--name", f"AKT-IT Customer {RID}", "--currency-code", "USD")
    tracker("customer", c["id"])
    assert c["type"] == "customer"

    u = akt("customer", "update", str(c["id"]), "--phone", "555-0100", "--website", "https://example.com")
    assert u["phone"] == "555-0100"
    assert u["name"] == c["name"], "update must preserve required fields"

    assert not _truthy(akt("customer", "disable", str(c["id"]))["enabled"])
    assert _truthy(akt("customer", "enable", str(c["id"]))["enabled"])

    got = akt("customer", "get", str(c["id"]))
    assert got["id"] == c["id"]

    listed = akt("customer", "list", "--all")
    assert any(r["id"] == c["id"] for r in listed)


def test_vendor_create(akt, tracker):
    v = akt("vendor", "create", "--name", f"AKT-IT Vendor {RID}", "--currency-code", "USD")
    tracker("vendor", v["id"])
    assert v["type"] == "vendor"


# --------------------------------------------------------------------------
# simple resources
# --------------------------------------------------------------------------

def test_item_category_tax(akt, tracker):
    item = akt("item", "create", "--name", f"AKT-IT Item {RID}", "--sale-price", "150", "--purchase-price", "90")
    tracker("item", item["id"])
    assert float(item["sale_price"]) == 150

    cat = akt("category", "create", "--name", f"AKT-IT Cat {RID}", "--type", "income")
    tracker("category", cat["id"])
    assert cat["type"] == "income"

    tax = akt("tax", "create", "--name", f"AKT-IT Tax {RID}", "--rate", "8.25")
    tracker("tax", tax["id"])
    assert float(tax["rate"]) == 8.25


def test_currency_lifecycle(akt, tracker):
    # Akaunting validates the code against ISO 4217; pick a real code that the
    # fresh install doesn't seed (seeds are USD + ARS).
    code = "SGD"
    # purge any leftover from a prior interrupted run
    for cur in akt("currency", "list", "--all"):
        if cur.get("code") == code:
            akt("currency", "delete", str(cur["id"]), parse=False, check=False)
    cur = akt("currency", "create", "--name", "AKT-IT Singapore Dollar", "--code", code, "--rate", "1.35")
    tracker("currency", cur["id"])
    assert cur["code"] == code


# --------------------------------------------------------------------------
# banking
# --------------------------------------------------------------------------

def test_account_and_transfer(akt, tracker):
    num = f"99{RID}"
    acct = akt("account", "create", "--name", f"AKT-IT Savings {RID}", "--number", num, "--currency-code", "USD")
    tracker("account", acct["id"])
    assert acct["number"] == num

    # transfer from the seeded default account (id 1) into the new one
    tr = akt("transfer", "create", "--from-account-id", "1", "--to-account-id", str(acct["id"]), "--amount", "25")
    tracker("transfer", tr["id"])
    assert tr.get("id")


def test_standalone_income_payment(akt, tracker):
    p = akt("payment", "create", "--type", "income", "--amount", "33.33", "--description", f"AKT-IT income {RID}")
    tracker("payment", p["id"])
    assert p["type"] == "income"
    assert _amount(p) == 33.33


# --------------------------------------------------------------------------
# purchase flow: vendor -> bill -> payment -> paid
# --------------------------------------------------------------------------

def test_bill_payment_flow(akt, tracker):
    vendor = akt("vendor", "create", "--name", f"AKT-IT BillVendor {RID}", "--currency-code", "USD")
    tracker("vendor", vendor["id"])
    item = akt("item", "create", "--name", f"AKT-IT BillItem {RID}", "--sale-price", "100", "--purchase-price", "100")
    tracker("item", item["id"])

    bill = akt(
        "bill", "create",
        "--contact", str(vendor["id"]),
        "--item", f"name=Widget,price=100,quantity=3,item_id={item['id']}",
        "--status", "received",
    )
    tracker("bill", bill["id"])
    assert _amount(bill) == 300, "server-computed total should be price*qty, not doubled"

    # partial then full payment to exercise status transitions
    pay = akt("payment", "create", "--bill", str(bill["id"]))
    tracker("payment", pay["id"])
    assert pay["type"] == "expense"
    assert _amount(pay) == 300

    paid = akt("bill", "get", str(bill["id"]))
    assert paid["status"] == "paid"

    # update must not wipe the single line item
    updated = akt("bill", "update", str(bill["id"]), "--due-at", "2030-01-01")
    assert len(updated.get("items", {}).get("data", [])) == 1
    assert _amount(updated) == 300


# --------------------------------------------------------------------------
# sales flow: invoice creation may be blocked by Akaunting's plan-limit gate
# --------------------------------------------------------------------------

def test_invoice_flow(akt, tracker):
    customer = akt("customer", "create", "--name", f"AKT-IT InvCustomer {RID}", "--currency-code", "USD")
    tracker("customer", customer["id"])
    item = akt("item", "create", "--name", f"AKT-IT InvItem {RID}", "--sale-price", "150", "--purchase-price", "0")
    tracker("item", item["id"])

    proc = akt(
        "--json", "invoice", "create",
        "--contact", str(customer["id"]),
        "--item", f"name=Service,price=150,quantity=2,item_id={item['id']}",
        "--status", "sent",
        raw=True,
    )
    if proc.returncode != 0:
        msg = proc.stderr.lower()
        if "not able to create a new user" in msg or "plan" in msg:
            pytest.xfail("Akaunting plan-limit gate blocks invoice creation (apps.api_key unset)")
        raise AssertionError(f"invoice create failed:\n{proc.stderr.strip()}")

    import json
    invoice = json.loads(proc.stdout)
    tracker("invoice", invoice["id"])
    assert _amount(invoice) == 300

    pay = akt("payment", "create", "--invoice", str(invoice["id"]))
    tracker("payment", pay["id"])
    assert pay["type"] == "income"

    paid = akt("invoice", "get", str(invoice["id"]))
    assert paid["status"] == "paid"
