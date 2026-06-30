"""Offline tests for request-body builders and arg parsing.

These exercise the parts of akt that don't need network access: document /
payment body construction, item parsing, and the argparse namespace wiring
(the connection-flag vs resource-field collision that once caused a 401).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akt.registry import INVOICE, BILL, PAYMENT, BY_NOUN
from akt.resources import (
    parse_item,
    parse_set,
    build_document_create,
    build_document_update,
    build_payment_create,
    body_from_fields,
)
from akt.cli import _build_parser


class FakeClient:
    """Stand-in for akt.client.Client with canned lookups."""

    def __init__(self, *, contacts=None, documents=None, settings=None, docs_list=None,
                 txns_list=None):
        self._contacts = contacts or {}
        self._documents = documents or {}
        self._settings = settings or {}
        self._docs_list = docs_list or []
        self._txns_list = txns_list or []

    def show(self, path, ident, *, type_scope=None):
        ident = str(ident)
        if path == "contacts":
            return self._contacts[ident]
        if path == "documents":
            return self._documents[ident]
        raise KeyError(path)

    def setting(self, key, default=None):
        return self._settings.get(key, default)

    def list(self, path, **kw):
        if path == "documents":
            return self._docs_list
        if path == "transactions":
            return self._txns_list
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
