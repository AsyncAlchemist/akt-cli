"""Full-surface integration tests against a live Akaunting 3.x instance.

Each test creates real records, exercises them, and registers them with the
`tracker` fixture so they are deleted on teardown (even on assertion failure).
Test data is prefixed `AKT-IT` and tagged with a per-run token to stay
identifiable and avoid collisions with prior runs.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# A minimal but valid PDF so Akaunting's mime validation (pdf/jpg/png) accepts it.
_PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

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

def test_bank_and_transfer(akt, tracker):
    num = f"99{RID}"
    acct = akt("bank", "create", "--name", f"AKT-IT Savings {RID}", "--number", num, "--currency-code", "USD")
    tracker("bank", acct["id"])
    assert acct["number"] == num

    # transfer from the seeded default bank account (id 1) into the new one
    tr = akt("transfer", "create", "--from-bank", "1", "--to-bank", str(acct["id"]), "--amount", "25")
    tracker("transfer", tr["id"])
    assert tr.get("id")


def test_standalone_income_payment(akt, tracker):
    p = akt("payment", "create", "--type", "income", "--amount", "33.33", "--description", f"AKT-IT income {RID}")
    tracker("payment", p["id"])
    assert p["type"] == "income"
    assert _amount(p) == 33.33


def test_bank_opening_balance_date_redates_journal_entry(akt, tracker):
    # Requires the Double-Entry module; skip if the journal-entry API isn't there.
    probe = akt("journal-entry", "list", raw=True)
    if probe.returncode != 0:
        pytest.skip("Double-Entry module not available on this instance")

    name = f"AKT-IT OB Bank {RID}"
    bank = akt("bank", "create", "--name", name, "--number", f"OB{RID}",
               "--currency-code", "USD", "--opening-balance", "500",
               "--opening-balance-date", "2024-12-31")
    tracker("bank", bank["id"])

    jes = akt("journal-entry", "list", "--all")
    je = next(
        (j for j in jes
         if str(j.get("reference") or "").startswith("opening-balance:")
         and str(j.get("description") or "").endswith(";" + name)),
        None,
    )
    if je is None:
        pytest.skip("no opening-balance JE created "
                    "(owners-contribution account not configured)")
    tracker("journal-entry", je["id"])

    assert str(je["paid_at"]).startswith("2024-12-31")   # re-dated to the period boundary
    assert je["journal_number"]                          # backfilled, non-empty


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


# --------------------------------------------------------------------------
# attachments: upload to an existing transaction, list, download, remove
# --------------------------------------------------------------------------

def _attachment_ids(record) -> list:
    att = record.get("attachment")
    return [m["id"] for m in att] if isinstance(att, list) else []


def test_payment_attachment_lifecycle(akt, tracker, tmp_path):
    pdf = tmp_path / "receipt.pdf"
    pdf.write_bytes(_PDF_BYTES)

    # a standalone transaction that starts with no attachment
    pay = akt("payment", "create", "--type", "expense", "--amount", "12.50",
              "--description", f"AKT-IT attach {RID}")
    tracker("payment", pay["id"])
    assert not _attachment_ids(pay)

    # attach to the EXISTING transaction (payment update -> multipart)
    updated = akt("payment", "update", str(pay["id"]), "--attachment", str(pdf))
    ids = _attachment_ids(updated)
    assert len(ids) == 1, "attachment should be present after update"

    # list surfaces the media metadata
    listed = akt("payment", "attachments", str(pay["id"]))
    assert [r["id"] for r in listed] == ids
    assert listed[0]["name"].endswith(".pdf")

    # download it back and verify byte-for-byte
    out = tmp_path / "dl"
    saved = akt("payment", "download-attachment", str(pay["id"]), "--out", str(out))
    assert len(saved) == 1
    got = Path(saved[0]["path"]).read_bytes()
    assert got == _PDF_BYTES, "downloaded bytes must match the uploaded file"

    # remove the attachment — assert via a FRESH fetch, not the update response
    # (Akaunting serializes a cached attachment relation on the PUT response).
    akt("payment", "update", str(pay["id"]), "--remove-attachment")
    assert akt("payment", "attachments", str(pay["id"])) == [], \
        "attachment should be gone after --remove-attachment"


def test_document_linked_payment_attachment(akt, tracker, tmp_path):
    """The tricky case: a bill/invoice payment routes through the nested
    documents/{doc}/transactions/{id} endpoint, whose update job takes a single
    `attachment` (not the `attachment[]` array every other route wants)."""
    pdf = tmp_path / "paid.pdf"
    pdf.write_bytes(_PDF_BYTES)

    vendor = akt("vendor", "create", "--name", f"AKT-IT DocPayVendor {RID}", "--currency-code", "USD")
    tracker("vendor", vendor["id"])
    bill = akt("bill", "create", "--contact", str(vendor["id"]),
               "--item", "name=Widget,price=100,quantity=1", "--status", "received")
    tracker("bill", bill["id"])
    pay = akt("payment", "create", "--bill", str(bill["id"]))
    tracker("payment", pay["id"])

    # attach to the EXISTING document-linked payment (nested-route multipart)
    updated = akt("payment", "update", str(pay["id"]), "--attachment", str(pdf))
    assert updated.get("document_id"), "payment should still be linked to its bill"
    assert len(_attachment_ids(updated)) == 1

    saved = akt("payment", "download-attachment", str(pay["id"]), "--out", str(tmp_path / "dl"))
    assert Path(saved[0]["path"]).read_bytes() == _PDF_BYTES

    akt("payment", "update", str(pay["id"]), "--remove-attachment")
    assert akt("payment", "attachments", str(pay["id"])) == []


def test_bill_create_with_attachment(akt, tracker, tmp_path):
    pdf = tmp_path / "supplier-bill.pdf"
    pdf.write_bytes(_PDF_BYTES)

    vendor = akt("vendor", "create", "--name", f"AKT-IT AttVendor {RID}", "--currency-code", "USD")
    tracker("vendor", vendor["id"])

    # attachment supplied at create time on a document (multipart with nested items)
    bill = akt("bill", "create", "--contact", str(vendor["id"]),
               "--item", "name=Widget,price=100,quantity=2", "--status", "received",
               "--attachment", str(pdf))
    tracker("bill", bill["id"])
    assert _amount(bill) == 200, "line-item total must survive the multipart encoding"
    assert len(_attachment_ids(bill)) == 1

    out = tmp_path / "dl"
    saved = akt("bill", "download-attachment", str(bill["id"]), "--out", str(out))
    assert Path(saved[0]["path"]).read_bytes() == _PDF_BYTES


# --------------------------------------------------------------------------
# double-entry: chart of accounts (read) + journal entries (CRUD).
# These need the DoubleEntry module installed; tests skip gracefully if not.
# --------------------------------------------------------------------------

def _gl_accounts(akt):
    """Return the chart of accounts, or None if the module isn't installed."""
    proc = akt("--json", "account", "list", "--all", raw=True)
    if proc.returncode != 0:
        return None
    import json
    return json.loads(proc.stdout)


def test_account_read(akt):
    accounts = _gl_accounts(akt)
    if accounts is None:
        pytest.skip("DoubleEntry module not installed (chart-of-accounts unavailable)")
    if not accounts:
        pytest.skip("chart of accounts is empty")
    one = akt("account", "get", str(accounts[0]["id"]))
    assert one["id"] == accounts[0]["id"]
    assert "code" in one and "name" in one


def test_account_crud(akt, tracker):
    """create/update/delete run through the session/CSRF web route, not /api."""
    accounts = _gl_accounts(akt)
    if accounts is None:
        pytest.skip("DoubleEntry module not installed (chart-of-accounts unavailable)")
    # Reuse an existing account's type, but avoid bank-type accounts (module
    # default type id 6): those spawn a linked banking account, which makes them
    # undeletable ("has account related") and would break teardown.
    reusable = [a for a in accounts if str(a.get("type_id")) != "6"]
    if not reusable:
        pytest.skip("no non-bank GL account type available to reuse")
    type_id = reusable[0]["type_id"]
    code = int(f"9{RID}"[:8])   # unlikely-to-collide numeric code

    created = akt("account", "create",
                  "--name", f"AKT-IT Account {RID}", "--code", str(code),
                  "--type-id", str(type_id))
    tracker("account", created["id"])
    assert created["name"].endswith(RID)
    assert int(created["code"]) == code

    updated = akt("account", "update", str(created["id"]),
                  "--description", "AKT-IT updated")
    assert updated["description"] == "AKT-IT updated"
    assert updated["name"] == created["name"], "web update must preserve the name"

    got = akt("account", "get", str(created["id"]))
    assert got["id"] == created["id"]


def test_journal_entry_lifecycle(akt, tracker):
    accounts = _gl_accounts(akt)
    if accounts is None:
        pytest.skip("DoubleEntry module not installed (journal-entry unavailable)")
    if len(accounts) < 2:
        pytest.skip("need at least two GL accounts to post a balanced entry")
    debit_acct, credit_acct = accounts[0]["code"], accounts[1]["code"]

    entry = akt(
        "journal-entry", "create",
        "--description", f"AKT-IT journal {RID}",
        "--item", f"account={debit_acct},debit=100",
        "--item", f"account={credit_acct},credit=100",
    )
    tracker("journal-entry", entry["id"])
    assert _amount(entry) == 100, "entry amount is the summed debits"
    assert entry["journal_number"], "a journal number should be auto-assigned"
    ledgers = entry.get("ledgers", {}).get("data", [])
    assert len(ledgers) == 2

    got = akt("journal-entry", "get", str(entry["id"]))
    assert got["id"] == entry["id"]

    listed = akt("journal-entry", "list", "--all")
    assert any(r["id"] == entry["id"] for r in listed)

    # update a scalar field: the two ledger lines must survive (not be wiped)
    updated = akt("journal-entry", "update", str(entry["id"]),
                  "--description", f"AKT-IT journal {RID} revised")
    assert updated["description"].endswith("revised")
    assert len(updated.get("ledgers", {}).get("data", [])) == 2
    assert _amount(updated) == 100


def test_journal_entry_rejects_imbalance(akt):
    """Client-side balance guard: an unbalanced entry never reaches the API."""
    proc = akt("--json", "journal-entry", "create",
               "--description", "AKT-IT bad", "--item", "account=1,debit=100",
               "--item", "account=2,credit=90", raw=True)
    assert proc.returncode != 0
    assert "balance" in proc.stderr.lower()


# --------------------------------------------------------------------------
# COA config: `coa sync` creates an account + its mirror category; coding a
# payment by --account resolves through the config to that category.
# --------------------------------------------------------------------------

def test_coa_sync_and_coded_payment(akt, tracker, akt_env, tmp_path):
    """coa sync creates an account + mirror category; --account codes a
    payment to that mirror category (CLI-observable contract)."""
    import json
    import subprocess

    from conftest import _AKT_CMD

    # A unique code/name so the test is idempotent across runs.
    coa_file = tmp_path / "coa.toml"
    coa_file.write_text(
        '[[account]]\n'
        'code = 991\n'
        'name = "AKT Test Revenue 991"\n'
        'type_id = 13\n'
    )
    env = {**akt_env, "AKT_COA_FILE": str(coa_file)}

    def run_json(*args):
        proc = subprocess.run([*_AKT_CMD, "--json", *args],
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def run_text(*args):
        proc = subprocess.run([*_AKT_CMD, *args], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    # sync (idempotent) then locate the created account + category. --all so the
    # created records are found even past the first page of a populated instance.
    run_text("coa", "sync")
    accounts = run_json("account", "list", "--all")
    acct = next(a for a in accounts if int(a["code"]) == 991)
    cats = run_json("category", "list", "--all")
    cat = next(c for c in cats if c["name"] == "AKT Test Revenue 991" and c["type"] == "income")
    tracker("account", acct["id"])
    tracker("category", cat["id"])

    # Coding by --account resolves through the config and sets the mirror
    # category. (The de_account_id -> general-ledger posting the module performs
    # is NOT exposed on the transaction API surface — the resource carries no
    # `ledgers`, and there is no ledger endpoint — so it cannot be asserted via
    # the CLI; that resolution is covered by the resolve_coding unit tests.)
    by_account = run_json("payment", "create", "--type", "income", "--bank", "1",
                          "--amount", "0.01", "--paid-at", "2026-07-12",
                          "--description", "coa coded (by account)", "--account", "991")
    tracker("payment", by_account["id"])
    assert str(by_account["category_id"]) == str(cat["id"])


# --------------------------------------------------------------------------
# akt-api banks/unmapped: banks with no Double-Entry ledger mapping (the
# root cause behind transactions that silently post nothing). akt verify
# surfaces these. Shape check only — forcing a live unmapped bank isn't
# practical (creating a bank auto-maps it via DoubleEntry's observer).
# --------------------------------------------------------------------------

def test_akt_api_banks_unmapped_shape(akt):
    """The banks/unmapped diagnostic returns a JSON list of {id,name}."""
    import json
    probe = akt("raw", "GET", "akt-api/banks/unmapped", raw=True)
    if probe.returncode != 0 or "404" in probe.stderr:
        pytest.skip("akt-api banks/unmapped endpoint not deployed")
    resp = json.loads(probe.stdout)
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    assert isinstance(data, list)
    for b in data:
        assert "id" in b and "name" in b


def test_verify_runs_without_coa(akt_env):
    """`akt verify` runs the COA-independent health checks with no COA config."""
    import json
    import subprocess

    from conftest import _AKT_CMD

    env = {k: v for k, v in akt_env.items() if k != "AKT_COA_FILE"}
    proc = subprocess.run([*_AKT_CMD, "--json", "verify"],
                          capture_output=True, text=True, env=env)
    if proc.returncode not in (0, 1):
        raise AssertionError(f"verify errored: {proc.stderr.strip()}")
    findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    assert isinstance(findings, list)
