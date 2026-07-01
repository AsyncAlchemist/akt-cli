"""Offline tests for request-body builders and arg parsing.

These exercise the parts of akt that don't need network access: document /
payment body construction, item parsing, and the argparse namespace wiring
(the connection-flag vs resource-field collision that once caused a 401).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akt.registry import INVOICE, BILL, PAYMENT, JOURNAL_ENTRY, CHART_OF_ACCOUNT, BY_NOUN
from akt.resources import (
    parse_item,
    parse_journal_item,
    parse_set,
    build_account_create,
    build_account_update,
    build_document_create,
    build_document_update,
    build_journal_create,
    build_journal_update,
    build_payment_create,
    resolve_payment_delete,
    resolve_payment_update,
    flatten_form,
    load_attachments,
    body_from_fields,
)
from akt.cli import _build_parser

pytestmark = pytest.mark.unit


class FakeClient:
    """Stand-in for akt.client.Client with canned lookups."""

    def __init__(self, *, contacts=None, documents=None, settings=None, docs_list=None,
                 txns_list=None, transactions=None, journals_list=None):
        self._contacts = contacts or {}
        self._documents = documents or {}
        self._settings = settings or {}
        self._docs_list = docs_list or []
        self._txns_list = txns_list or []
        self._transactions = transactions or {}
        self._journals_list = journals_list or []

    def show(self, path, ident, *, type_scope=None):
        ident = str(ident)
        if path == "contacts":
            return self._contacts[ident]
        if path == "documents":
            return self._documents[ident]
        if path == "transactions":
            return self._transactions[ident]
        raise KeyError(path)

    def setting(self, key, default=None):
        return self._settings.get(key, default)

    def list(self, path, **kw):
        if path == "documents":
            return self._docs_list
        if path == "transactions":
            return self._txns_list
        if path == "journal-entry":
            return self._journals_list
        return []


# --------------------------------------------------------------------------
# parse helpers
# --------------------------------------------------------------------------

def test_parse_item_defaults_quantity():
    item = parse_item("name=Widget,price=10")
    assert item == {"name": "Widget", "price": 10, "quantity": 1, "description": ""}


def test_parse_item_full():
    item = parse_item("name=Consulting,price=150.5,quantity=3,tax_id=1")
    assert item["price"] == 150.5
    assert item["quantity"] == 3
    assert item["tax_id"] == 1


def test_parse_item_requires_name_and_price():
    with pytest.raises(ValueError):
        parse_item("price=10")
    with pytest.raises(ValueError):
        parse_item("name=NoPrice")


def test_parse_set_json_coercion():
    out = parse_set(["enabled=1", "name=Acme", "rate=8.25", "flag=true"])
    assert out == {"enabled": 1, "name": "Acme", "rate": 8.25, "flag": True}


# --------------------------------------------------------------------------
# document builder
# --------------------------------------------------------------------------

def _invoice_ns(**over):
    base = dict(contact="5", item=["name=Consulting,price=150,quantity=10",
                                   "name=Setup,price=500,quantity=1"],
               number=None, status="sent", issued_at=None, due_at=None,
               currency_code=None, currency_rate=None, category_id=None,
               order_number=None, notes=None, set_=None, data=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_build_invoice_computes_total_and_contact():
    client = FakeClient(
        contacts={"5": {"id": 5, "name": "Northwind", "email": "n@x.com",
                        "currency_code": "USD"}},
        settings={"default.income_category": "2", "invoice.number_prefix": "INV-",
                  "invoice.number_digit": "5"},
        docs_list=[{"document_number": "INV-00007"}],
    )
    body = build_document_create(INVOICE, client, _invoice_ns())
    assert body["type"] == "invoice"
    assert body["contact_id"] == 5
    assert body["contact_name"] == "Northwind"
    assert body["category_id"] == 2
    # amount is sent as 0; Akaunting recomputes the total from items server-side
    assert body["amount"] == 0
    assert body["items"][0]["price"] == 150 and body["items"][0]["quantity"] == 10
    assert all("description" in it for it in body["items"])
    assert body["document_number"] == "INV-00008"  # max(7)+1, zero-padded to 5
    assert body["currency_code"] == "USD"
    assert body["status"] == "sent"
    assert len(body["items"]) == 2


def test_build_invoice_requires_contact():
    client = FakeClient()
    with pytest.raises(ValueError):
        build_document_create(INVOICE, client, _invoice_ns(contact=None))


def test_build_invoice_requires_items():
    client = FakeClient(
        contacts={"5": {"id": 5, "name": "N", "currency_code": "USD"}},
        settings={"default.income_category": "2"},
    )
    with pytest.raises(ValueError):
        build_document_create(INVOICE, client, _invoice_ns(item=[]))


def test_build_bill_uses_expense_category_and_vendor_scope():
    client = FakeClient(
        contacts={"9": {"id": 9, "name": "Supplier", "currency_code": "USD"}},
        settings={"default.expense_category": "4", "bill.number_prefix": "BILL-",
                  "bill.number_digit": "4"},
        docs_list=[],
    )
    ns = _invoice_ns(contact="9")
    body = build_document_create(BILL, client, ns)
    assert body["type"] == "bill"
    assert body["category_id"] == 4
    assert body["document_number"] == "BILL-0001"
    assert BILL.contact_scope() == "vendor"


def test_invoice_explicit_number_and_dates():
    client = FakeClient(
        contacts={"5": {"id": 5, "name": "N", "currency_code": "USD"}},
        settings={"default.income_category": "2"},
    )
    ns = _invoice_ns(number="CUSTOM-1", issued_at="2026-01-15", due_at="2026-02-15")
    body = build_document_create(INVOICE, client, ns)
    assert body["document_number"] == "CUSTOM-1"
    assert body["issued_at"] == "2026-01-15 00:00:00"
    assert body["due_at"] == "2026-02-15 00:00:00"


def test_update_preserves_existing_items_when_none_given():
    """Regression: Akaunting deletes & recreates all items from the request on
    update, so an update that only changes status must resend the line items."""
    current = {
        "type": "invoice", "document_number": "INV-1", "status": "draft",
        "issued_at": "2026-01-01T00:00:00+00:00", "due_at": "2026-02-01T00:00:00+00:00",
        "currency_code": "USD", "currency_rate": 1, "contact_id": 5,
        "contact_name": "N", "category_id": 2, "amount": 2000,
        "items": {"data": [
            {"item_id": "3", "name": "Consulting", "description": "",
             "price": 150, "total": 1500, "taxes": {"data": []}},
            {"item_id": None, "name": "Setup", "description": "", "price": 500,
             "total": 500, "taxes": {"data": []}},
        ]},
    }
    ns = SimpleNamespace(status="sent", issued_at=None, due_at=None, number=None,
                         category_id=None, currency_code=None, currency_rate=None,
                         notes=None, order_number=None, item=None, set_=None, data=None)
    body = build_document_update(INVOICE, FakeClient(), ns, current)
    assert body["status"] == "sent"          # overlay applied
    assert body["amount"] == 0               # server recomputes
    assert len(body["items"]) == 2           # items preserved, not wiped
    assert body["items"][0]["quantity"] == 10   # 1500 / 150
    assert body["items"][0]["item_id"] == 3


# --------------------------------------------------------------------------
# payment builder
# --------------------------------------------------------------------------

def _payment_ns(**over):
    base = dict(invoice=None, bill=None, type=None, document_id=None, contact_id=None,
                category_id=None, amount=None, account_id=None, paid_at=None,
                currency_code=None, currency_rate=None, payment_method=None,
                number=None, reference=None, description=None, set_=None, data=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_payment_against_invoice_infers_everything():
    client = FakeClient(
        documents={"3": {"id": 3, "contact_id": 5, "category_id": 2,
                         "amount": 2000, "amount_due": 2000, "currency_code": "USD"}},
        settings={"default.account": "1"},
        txns_list=[{"number": "PAY-00002"}],
    )
    ns = _payment_ns(invoice="3")
    body = build_payment_create(PAYMENT, client, ns)
    assert body["type"] == "income"
    # document payments route to the nested endpoint, not a flat document_id
    assert body["__endpoint__"] == "documents/3/transactions"
    assert body["__type_scope__"] == "invoice"
    assert "document_id" not in body
    assert body["contact_id"] == 5
    assert body["category_id"] == 2
    assert body["amount"] == 2000
    assert body["account_id"] == 1
    assert body["payment_method"] == "offline-payments.cash.1"
    assert body["number"] == "PAY-00003"


def test_payment_against_bill_is_expense():
    client = FakeClient(
        documents={"7": {"id": 7, "contact_id": 9, "category_id": 4,
                         "amount": 500, "amount_due": 500, "currency_code": "USD"}},
        settings={"default.account": "1"},
        txns_list=[],
    )
    body = build_payment_create(PAYMENT, client, _payment_ns(bill="7"))
    assert body["type"] == "expense"
    assert body["__endpoint__"] == "documents/7/transactions"
    assert body["__type_scope__"] == "bill"
    assert body["amount"] == 500


def test_standalone_income_payment_partial_amount():
    client = FakeClient(settings={"default.income_category": "2", "default.account": "1"})
    ns = _payment_ns(type="income", amount=42.5, category_id=None)
    body = build_payment_create(PAYMENT, client, ns)
    assert body["type"] == "income"
    assert body["amount"] == 42.5
    assert body["category_id"] == 2
    assert "document_id" not in body


def test_payment_requires_amount_when_standalone():
    client = FakeClient(settings={"default.income_category": "2", "default.account": "1"})
    with pytest.raises(ValueError):
        build_payment_create(PAYMENT, client, _payment_ns(type="income"))


def test_delete_document_payment_uses_nested_route():
    """A payment linked to a bill/invoice must delete via the nested route."""
    client = FakeClient(transactions={
        "16": {"id": 16, "type": "expense", "document_id": 7},
        "20": {"id": 20, "type": "income", "document_id": 3},
    })
    assert resolve_payment_delete(PAYMENT, client, "16") == ("documents/7/transactions/16", "bill")
    assert resolve_payment_delete(PAYMENT, client, "20") == ("documents/3/transactions/20", "invoice")


def test_delete_standalone_payment_uses_flat_route():
    client = FakeClient(transactions={
        "2": {"id": 2, "type": "income", "document_id": None},
    })
    assert resolve_payment_delete(PAYMENT, client, "2") == ("transactions/2", None)


# --------------------------------------------------------------------------
# journal entry (double-entry) builder
# --------------------------------------------------------------------------

def _journal_ns(**over):
    base = dict(journal_number=None, paid_at=None, description="Opening balances",
                basis=None, reference=None, currency_code=None, currency_rate=None,
                item=["account_id=10,debit=100", "account_id=20,credit=100"],
                set_=None, data=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_parse_journal_item_debit_and_credit_lines():
    assert parse_journal_item("account_id=10,debit=100") == {"account_id": 10, "debit": 100}
    assert parse_journal_item("account_id=20,credit=50.5") == {"account_id": 20, "credit": 50.5}


def test_parse_journal_item_requires_account_and_side():
    with pytest.raises(ValueError):
        parse_journal_item("debit=100")               # no account_id
    with pytest.raises(ValueError):
        parse_journal_item("account_id=10")           # neither debit nor credit


def test_build_journal_create_balances_and_autonumbers():
    client = FakeClient(
        settings={"double-entry.journal.number_prefix": "MJE-",
                  "double-entry.journal.number_digit": "5"},
        journals_list=[{"journal_number": "MJE-00007"}],
    )
    body = build_journal_create(JOURNAL_ENTRY, client, _journal_ns(basis="cash"))
    assert body["journal_number"] == "MJE-00008"      # max(7)+1, padded to 5
    assert body["basis"] == "cash"
    assert body["currency_code"] == "USD"             # default
    assert body["amount"] == 0                         # server recomputes
    # both debit & credit keys present on every line (Akaunting validates both)
    assert body["items"][0] == {"account_id": 10, "debit": 100, "credit": 0}
    assert body["items"][1] == {"account_id": 20, "credit": 100, "debit": 0}


def test_build_journal_create_defaults_basis_accrual():
    client = FakeClient(journals_list=[])
    body = build_journal_create(JOURNAL_ENTRY, client, _journal_ns(journal_number="JE-1"))
    assert body["basis"] == "accrual"
    assert body["journal_number"] == "JE-1"


def test_build_journal_create_rejects_imbalance():
    client = FakeClient(journals_list=[])
    ns = _journal_ns(item=["account_id=10,debit=100", "account_id=20,credit=90"])
    with pytest.raises(ValueError, match="does not balance"):
        build_journal_create(JOURNAL_ENTRY, client, ns)


def test_build_journal_create_requires_two_lines():
    client = FakeClient(journals_list=[])
    ns = _journal_ns(item=["account_id=10,debit=100"])
    with pytest.raises(ValueError, match="at least 2"):
        build_journal_create(JOURNAL_ENTRY, client, ns)


def test_build_journal_create_requires_description():
    client = FakeClient(journals_list=[])
    with pytest.raises(ValueError, match="description"):
        build_journal_create(JOURNAL_ENTRY, client, _journal_ns(description=None))


def test_build_journal_update_preserves_ledgers_with_ids():
    """Regression: an update that only changes one field must resend the ledger
    lines (with their ids) so Akaunting doesn't delete the untouched rows."""
    current = {
        "paid_at": "2026-01-01T00:00:00+00:00", "journal_number": "MJE-1",
        "description": "orig", "basis": "accrual", "currency_code": "USD",
        "currency_rate": 1,
        "ledgers": {"data": [
            {"id": 5, "account_id": 10, "debit": 100, "credit": None},
            {"id": 6, "account_id": 20, "debit": None, "credit": 100},
        ]},
    }
    ns = _journal_ns(description="revised", item=None)
    body = build_journal_update(JOURNAL_ENTRY, FakeClient(), ns, current)
    assert body["description"] == "revised"           # overlay applied
    assert body["amount"] == 0                          # server recomputes
    assert body["items"] == [
        {"account_id": 10, "debit": 100.0, "credit": 0.0, "id": 5},
        {"account_id": 20, "debit": 0.0, "credit": 100.0, "id": 6},
    ]


# --------------------------------------------------------------------------
# chart-of-accounts (web-surface CRUD) builder
# --------------------------------------------------------------------------

def _account_ns(**over):
    base = dict(name=None, code=None, type_id=None, account_id=None,
                description=None, enabled=None, set_=None, data=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_chart_of_account_routes_crud_through_web():
    assert CHART_OF_ACCOUNT.read_only is False
    assert CHART_OF_ACCOUNT.web_endpoint == "double-entry/chart-of-accounts"
    # list/get stay on the /api surface
    assert CHART_OF_ACCOUNT.endpoint == "chart-of-accounts"


def test_build_account_create_top_level():
    body = build_account_create(
        CHART_OF_ACCOUNT, FakeClient(),
        _account_ns(name="Cash", code="1010", type_id="6"),
    )
    assert body["name"] == "Cash"
    assert body["code"] == "1010"
    assert body["type_id"] == "6"
    assert body["enabled"] == 1                 # flag default
    assert body["is_sub_account"] == "false"    # no parent


def test_build_account_create_sub_account():
    body = build_account_create(
        CHART_OF_ACCOUNT, FakeClient(),
        _account_ns(name="Petty Cash", code="1011", type_id="6", account_id="42"),
    )
    assert body["account_id"] == "42"
    assert body["is_sub_account"] == "true"     # parent supplied


def test_build_account_create_requires_name():
    with pytest.raises(ValueError, match="name"):
        build_account_create(CHART_OF_ACCOUNT, FakeClient(), _account_ns(code="1010"))


def test_build_account_update_backfills_required_name():
    current = {"id": 5, "name": "Cash", "code": 1010, "type_id": 6,
               "account_id": None, "enabled": 1, "description": None}
    # change only the code; name (required by Akaunting on update) is resent
    body = build_account_update(CHART_OF_ACCOUNT, FakeClient(),
                                _account_ns(code="1099"), current)
    assert body["name"] == "Cash"
    assert body["code"] == "1099"
    assert body["is_sub_account"] == "false"


# --------------------------------------------------------------------------
# generic field body + arg-parsing collision regression
# --------------------------------------------------------------------------

def test_body_from_fields_applies_defaults_and_type():
    res = BY_NOUN["customer"]
    ns = SimpleNamespace(name="Acme", email=None, phone=None, tax_number=None,
                         website=None, currency_code=None, reference=None, address=None,
                         city=None, zip_code=None, state=None, country=None,
                         enabled=None, set_=None, data=None)
    body = body_from_fields(res, ns, for_update=False)
    assert body["name"] == "Acme"
    assert body["type"] == "customer"
    assert body["currency_code"] == "USD"   # default
    assert body["enabled"] == 1             # flag default


def test_connection_flags_do_not_collide_with_resource_fields():
    """Regression: a customer's --email must not overwrite the auth --email."""
    parser = _build_parser()
    ns = parser.parse_args(
        ["--email", "admin@host.com", "--password", "secret",
         "customer", "create", "--name", "Acme", "--email", "cust@acme.com"]
    )
    assert ns.conn_email == "admin@host.com"   # auth identity
    assert ns.email == "cust@acme.com"         # the customer's own email
    assert ns.conn_password == "secret"


def test_json_flag_works_after_verb():
    parser = _build_parser()
    ns = parser.parse_args(["customer", "list", "--json"])
    assert getattr(ns, "json", False) is True


def _verbs_for(parser, noun):
    import argparse
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    rp = sub.choices[noun]
    vsub = next(a for a in rp._actions if isinstance(a, argparse._SubParsersAction))
    return set(vsub.choices)


def test_chart_of_account_exposes_full_crud_no_toggle():
    verbs = _verbs_for(_build_parser(), "chart-of-account")
    assert {"list", "get", "create", "update", "delete"} <= verbs
    # no enable/disable (supports_toggle=False) and no attachment verbs
    assert "enable" not in verbs and "attachments" not in verbs


def test_journal_entry_exposes_full_crud_and_attachments():
    verbs = _verbs_for(_build_parser(), "journal-entry")
    assert {"list", "get", "create", "update", "delete",
            "attachments", "download-attachment"} <= verbs
    assert "enable" not in verbs        # no enable/disable API route


# --------------------------------------------------------------------------
# attachments: multipart form flattening, file loading, update routing
# --------------------------------------------------------------------------

def test_flatten_form_nested_items_and_taxes():
    body = {
        "type": "invoice",
        "amount": 0,
        "items": [
            {"name": "Widget", "price": 10, "tax_id": [1, 2]},
            {"name": "Setup", "price": 500},
        ],
    }
    pairs = dict(flatten_form(body))
    assert pairs["type"] == "invoice"
    assert pairs["amount"] == "0"
    assert pairs["items[0][name]"] == "Widget"
    assert pairs["items[0][price]"] == "10"
    assert pairs["items[0][tax_id][0]"] == "1"
    assert pairs["items[0][tax_id][1]"] == "2"
    assert pairs["items[1][name]"] == "Setup"


def test_flatten_form_skips_none_and_reserved_and_coerces_bool():
    body = {"name": "Acme", "email": None, "enabled": True, "disabled": False,
            "__endpoint__": "documents/3/transactions", "__type_scope__": "invoice"}
    pairs = dict(flatten_form(body))
    assert pairs == {"name": "Acme", "enabled": "1", "disabled": "0"}
    assert "email" not in pairs          # None dropped
    assert not any(k.startswith("__") for k in pairs)  # routing keys dropped


def test_load_attachments_reads_validates_and_rejects(tmp_path):
    pdf = tmp_path / "receipt.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    files = load_attachments([str(pdf)])
    assert len(files) == 1
    field, (name, content, mime) = files[0]
    assert field == "attachment[]"       # the array field both surfaces expect
    assert name == "receipt.pdf"
    assert content == b"%PDF-1.4 test"
    assert mime == "application/pdf"

    bad = tmp_path / "note.txt"
    bad.write_text("nope")
    with pytest.raises(ValueError):      # disallowed extension
        load_attachments([str(bad)])
    with pytest.raises(ValueError):      # missing file
        load_attachments([str(tmp_path / "ghost.pdf")])


def test_load_attachments_empty_is_noop():
    assert load_attachments(None) == []
    assert load_attachments([]) == []


def test_resolve_payment_update_uses_nested_route_for_document_payment():
    """A document-linked payment must update via documents/{doc}/transactions/{id};
    the flat /transactions route 400s on any request carrying document_id."""
    linked = {"id": 16, "type": "expense", "document_id": 7}
    assert resolve_payment_update(PAYMENT, FakeClient(), "16", linked) == (
        "documents/7/transactions/16", "bill")
    income = {"id": 20, "type": "income", "document_id": 3}
    assert resolve_payment_update(PAYMENT, FakeClient(), "20", income) == (
        "documents/3/transactions/20", "invoice")


def test_resolve_payment_update_uses_flat_route_for_standalone():
    standalone = {"id": 2, "type": "income", "document_id": None}
    assert resolve_payment_update(PAYMENT, FakeClient(), "2", standalone) == (
        "transactions/2", None)


def test_load_attachments_single_field_for_nested_route(tmp_path):
    """The nested transaction-update route reads a single `attachment` file;
    everything else uses the `attachment[]` array."""
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    arr = load_attachments([str(pdf)])
    assert arr[0][0] == "attachment[]"
    single = load_attachments([str(pdf)], field="attachment")
    assert single[0][0] == "attachment"
