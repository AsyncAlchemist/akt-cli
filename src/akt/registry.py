"""The concrete set of resources akt exposes."""

from __future__ import annotations

from .resources import (
    Resource,
    f,
    build_document_create,
    build_document_update,
    build_payment_create,
    build_transfer_create,
    resolve_payment_delete,
    resolve_payment_update,
)

# Common column sets
_CONTACT_COLS = [
    ("ID", "id"), ("Name", "name"), ("Email", "email"),
    ("Phone", "phone"), ("Currency", "currency_code"), ("Enabled", "enabled"),
]
_DOC_COLS = [
    ("ID", "id"), ("Number", "document_number"), ("Contact", "contact_name"),
    ("Status", "status"), ("Total", "amount_formatted"),
    ("Due", "amount_due_formatted"), ("Issued", "issued_at"), ("Due date", "due_at"),
]
_TXN_COLS = [
    ("ID", "id"), ("Number", "number"), ("Type", "type"), ("Contact", "contact.data.name"),
    ("Amount", "amount_formatted"), ("Paid at", "paid_at"), ("Method", "payment_method"),
    ("Doc", "document_id"),
]


def _contact_fields() -> list:
    return [
        f("name", "Display name", required=True),
        f("email", "Email address"),
        f("phone", "Phone number"),
        f("tax-number", "Tax / VAT number"),
        f("website", "Website URL"),
        f("currency-code", "Currency code (e.g. USD)", default="USD"),
        f("reference", "Internal reference"),
        f("address", "Street address"),
        f("city", "City"),
        f("zip-code", "Postal / ZIP code"),
        f("state", "State / province"),
        f("country", "Country code (e.g. US)"),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ]


CUSTOMER = Resource(
    noun="customer",
    endpoint="contacts",
    type_scope="customer",
    body_type="customer",
    fields=_contact_fields(),
    columns=_CONTACT_COLS,
    help="Customers (sales contacts)",
)

VENDOR = Resource(
    noun="vendor",
    endpoint="contacts",
    type_scope="vendor",
    body_type="vendor",
    fields=_contact_fields(),
    columns=_CONTACT_COLS,
    help="Vendors / suppliers (purchase contacts)",
)

ITEM = Resource(
    noun="item",
    endpoint="items",
    fields=[
        f("name", "Item name", required=True),
        f("description", "Description"),
        f("type", "product or service", default="product", choices=["product", "service"]),
        f("sale-price", "Sale price"),
        f("purchase-price", "Purchase price"),
        f("category-id", "Category id"),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ],
    columns=[
        ("ID", "id"), ("Name", "name"), ("Type", "type"),
        ("Sale", "sale_price_formatted"), ("Purchase", "purchase_price_formatted"),
        ("Enabled", "enabled"),
    ],
    help="Products and services",
)

ACCOUNT = Resource(
    noun="account",
    endpoint="accounts",
    fields=[
        f("name", "Account name", required=True),
        f("number", "Account number", required=True),
        f("type", "Account type", default="bank"),
        f("currency-code", "Currency code", default="USD"),
        f("opening-balance", "Opening balance", default=0),
        f("bank-name", "Bank name"),
        f("bank-phone", "Bank phone"),
        f("bank-address", "Bank address"),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ],
    columns=[
        ("ID", "id"), ("Name", "name"), ("Number", "number"),
        ("Currency", "currency_code"), ("Balance", "current_balance_formatted"),
        ("Enabled", "enabled"),
    ],
    help="Bank / cash accounts",
)

CATEGORY = Resource(
    noun="category",
    endpoint="categories",
    fields=[
        f("name", "Category name", required=True),
        f("type", "income | expense | item | other", required=True,
          choices=["income", "expense", "item", "other"]),
        f("color", "Hex color", default="#00bcd4"),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ],
    columns=[
        ("ID", "id"), ("Name", "name"), ("Type", "type"),
        ("Color", "color"), ("Enabled", "enabled"),
    ],
    help="Income / expense / item categories",
)

TAX = Resource(
    noun="tax",
    endpoint="taxes",
    fields=[
        f("name", "Tax name", required=True),
        f("rate", "Tax rate (percent)", required=True),
        f("type", "normal | inclusive | compound | withholding | fixed",
          default="normal",
          choices=["normal", "inclusive", "compound", "withholding", "fixed"]),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ],
    columns=[
        ("ID", "id"), ("Name", "name"), ("Rate", "rate"),
        ("Type", "type"), ("Enabled", "enabled"),
    ],
    help="Tax rates",
)

CURRENCY = Resource(
    noun="currency",
    endpoint="currencies",
    fields=[
        f("name", "Currency name", required=True),
        f("code", "ISO code (e.g. EUR)", required=True),
        f("rate", "Exchange rate vs default", required=True),
        f("precision", "Decimal precision", default=2),
        f("symbol", "Symbol"),
        f("symbol-first", "Symbol before amount (1/0)", default=1),
        f("decimal-mark", "Decimal mark", default="."),
        f("thousands-separator", "Thousands separator", default=","),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ],
    columns=[
        ("ID", "id"), ("Name", "name"), ("Code", "code"),
        ("Rate", "rate"), ("Enabled", "enabled"),
    ],
    help="Currencies",
)

# Documents -----------------------------------------------------------------

_DOC_FIELDS = [
    f("contact", "Contact id (customer for invoice / vendor for bill)"),
    f("number", "Document number (auto-generated if omitted)"),
    f("status", "draft | sent | received | paid | cancelled ..."),
    f("issued-at", "Issue date (YYYY-MM-DD)"),
    f("due-at", "Due date (YYYY-MM-DD)"),
    f("currency-code", "Currency code"),
    f("currency-rate", "Currency rate"),
    f("category-id", "Category id"),
    f("order-number", "Order number"),
    f("notes", "Notes"),
]

INVOICE = Resource(
    noun="invoice",
    endpoint="documents",
    type_scope="invoice",
    body_type="invoice",
    fields=_DOC_FIELDS,
    columns=_DOC_COLS,
    build_create=build_document_create,
    build_update=build_document_update,
    supports_attachments=True,
    help="Sales invoices",
)

BILL = Resource(
    noun="bill",
    endpoint="documents",
    type_scope="bill",
    body_type="bill",
    fields=_DOC_FIELDS,
    columns=_DOC_COLS,
    build_create=build_document_create,
    build_update=build_document_update,
    supports_attachments=True,
    help="Purchase bills",
)

# Payments (transactions) ---------------------------------------------------

PAYMENT = Resource(
    noun="payment",
    endpoint="transactions",
    fields=[
        f("type", "income | expense", choices=["income", "expense"]),
        f("invoice", "Invoice id to apply an income payment to"),
        f("bill", "Bill id to apply an expense payment to"),
        f("document-id", "Linked document id (advanced)"),
        f("contact-id", "Contact id"),
        f("amount", "Payment amount"),
        f("account-id", "Bank/cash account id"),
        f("category-id", "Category id"),
        f("paid-at", "Payment date/time (YYYY-MM-DD)"),
        f("currency-code", "Currency code"),
        f("currency-rate", "Currency rate"),
        f("payment-method", "Payment method code", default="offline-payments.cash.1"),
        f("number", "Transaction number (auto if omitted)"),
        f("reference", "Reference"),
        f("description", "Description"),
    ],
    columns=_TXN_COLS,
    supports_toggle=False,
    supports_attachments=True,
    build_create=build_payment_create,
    delete_resolver=resolve_payment_delete,
    update_resolver=resolve_payment_update,
    help="Payments / transactions (income & expense)",
)

TRANSFER = Resource(
    noun="transfer",
    endpoint="transfers",
    fields=[
        f("from-account-id", "Source account id", required=True),
        f("to-account-id", "Destination account id", required=True),
        f("amount", "Amount", required=True),
        f("transferred-at", "Transfer date/time (YYYY-MM-DD)"),
        f("payment-method", "Payment method code", default="offline-payments.cash.1"),
        f("reference", "Reference"),
        f("description", "Description"),
    ],
    columns=[
        ("ID", "id"), ("From", "from_account.data.name"), ("To", "to_account.data.name"),
        ("Amount", "amount"),
    ],
    supports_toggle=False,
    build_create=build_transfer_create,
    help="Transfers between accounts",
)


RESOURCES: list[Resource] = [
    CUSTOMER, VENDOR, ITEM, ACCOUNT, CATEGORY, TAX, CURRENCY,
    INVOICE, BILL, PAYMENT, TRANSFER,
]

BY_NOUN = {r.noun: r for r in RESOURCES}
