"""Declarative resource registry and generic CRUD handlers.

Each :class:`Resource` maps a CLI noun (``customer``, ``invoice`` …) to an
Akaunting API endpoint plus the metadata needed to build create/update bodies
and render list tables. Resources whose create/update bodies are non-trivial
(documents, payments) override the body builders.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import Client


# --------------------------------------------------------------------------
# field specs
# --------------------------------------------------------------------------

@dataclass
class Field:
    name: str                       # CLI flag (without --), e.g. "currency-code"
    dest: str                       # body key, e.g. "currency_code"
    help: str = ""
    required: bool = False          # required on create
    default: Any = None             # default applied on create when omitted
    is_flag: bool = False           # store_true boolean flag
    choices: list[str] | None = None


def f(name: str, help: str = "", **kw) -> Field:
    dest = kw.pop("dest", name.replace("-", "_"))
    return Field(name=name, dest=dest, help=help, **kw)


# --------------------------------------------------------------------------
# resource definition
# --------------------------------------------------------------------------

@dataclass
class Resource:
    noun: str
    endpoint: str
    fields: list[Field] = field(default_factory=list)
    type_scope: str | None = None          # search=type:X for ACL (contacts/documents)
    body_type: str | None = None           # inject {"type": ...} into body
    columns: list[tuple[str, str]] = field(default_factory=list)  # (header, dotted path)
    search_default: str | None = None      # always-applied search filter
    supports_toggle: bool = True           # enable/disable verbs
    help: str = ""

    # hooks (override for documents/payments)
    build_create: Callable[["Resource", Client, Any], dict] | None = None
    build_update: Callable[["Resource", Client, Any, dict], dict] | None = None

    def contact_scope(self) -> str:
        """ACL scope of the contact tied to a document resource."""
        return "customer" if self.body_type == "invoice" else "vendor"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_set(values: list[str] | None) -> dict:
    """Parse repeated ``--set key=value`` into a dict (values JSON-coerced)."""
    out: dict[str, Any] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"--set expects key=value, got {item!r}")
        k, _, v = item.partition("=")
        out[k.strip()] = _coerce(v.strip())
    return out


def _coerce(v: str) -> Any:
    try:
        return json.loads(v)
    except ValueError:
        return v


def load_data_arg(value: str | None) -> dict:
    """Parse ``--data`` which is inline JSON or @path/to/file.json."""
    if not value:
        return {}
    if value.startswith("@"):
        with open(value[1:]) as fh:
            return json.load(fh)
    return json.loads(value)


def now_dt() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_dt() -> str:
    return _dt.date.today().strftime("%Y-%m-%d 00:00:00")


def add_days(date_str: str, days: int) -> str:
    base = _dt.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    return (base + _dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_date(value: str) -> str:
    """Accept YYYY-MM-DD or full datetime; return Akaunting datetime string."""
    value = value.strip()
    if len(value) == 10:
        return value + " 00:00:00"
    return value


def body_from_fields(res: Resource, ns: Any, *, for_update: bool,
                     current: dict | None = None) -> dict:
    """Assemble a request body from declared fields + --set + --data.

    On update, Akaunting validates required fields on PUT (it's a full replace,
    not a patch), so when ``current`` is supplied unspecified fields fall back to
    the existing record's values.
    """
    body: dict[str, Any] = {}
    for fld in res.fields:
        val = getattr(ns, fld.dest, None)
        if fld.is_flag:
            # tri-state: only include if explicitly toggled
            if val is None:
                if for_update and current is not None and fld.dest in current:
                    body[fld.dest] = 1 if current.get(fld.dest) else 0
                elif not for_update and fld.default is not None:
                    body[fld.dest] = fld.default
                continue
            body[fld.dest] = 1 if val else 0
            continue
        if val is None:
            if for_update and current is not None and current.get(fld.dest) is not None:
                body[fld.dest] = current.get(fld.dest)
            elif not for_update and fld.default is not None:
                body[fld.dest] = fld.default
            continue
        body[fld.dest] = val

    body.update(parse_set(getattr(ns, "set_", None)))
    body.update(load_data_arg(getattr(ns, "data", None)))

    if res.body_type and "type" not in body:
        body["type"] = res.body_type
    return body


# --------------------------------------------------------------------------
# document (invoice / bill) body builder
# --------------------------------------------------------------------------

def parse_item(spec: str) -> dict:
    """Parse ``--item 'name=Widget,price=10,quantity=2,tax_id=1'``."""
    item: dict[str, Any] = {"quantity": 1}
    for part in spec.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"--item field must be key=value, got {part!r}")
        k, _, v = part.partition("=")
        item[k.strip()] = _coerce(v.strip())
    if "name" not in item:
        raise ValueError(f"--item requires a name=… field: {spec!r}")
    if "price" not in item:
        raise ValueError(f"--item requires a price=… field: {spec!r}")
    # Akaunting reads $item['description'] without a default when item_id is
    # absent (CreateDocumentItemsAndTotals), so it must always be present.
    item.setdefault("description", "")
    return item


def _normalize_items(items: list[dict]) -> list[dict]:
    """Ensure every line item carries the keys Akaunting accesses unguarded."""
    for it in items:
        it.setdefault("description", "")
        it.setdefault("quantity", 1)
    return items


def _document_default_category(client: Client, doc_type: str) -> int | None:
    key = "default.income_category" if doc_type == "invoice" else "default.expense_category"
    val = client.setting(key)
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _next_document_number(client: Client, res: Resource, prefix: str) -> str:
    """Best-effort unique document number: prefix + zero-padded max+1."""
    setting_prefix = "invoice" if res.body_type == "invoice" else "bill"
    pre = client.setting(f"{setting_prefix}.number_prefix") or prefix
    digit = client.setting(f"{setting_prefix}.number_digit")
    try:
        width = int(digit)
    except (TypeError, ValueError):
        width = 5
    existing = client.list(res.endpoint, type_scope=res.type_scope, all_pages=True)
    maxn = 0
    for row in existing:
        num = str(row.get("document_number", ""))
        tail = "".join(ch for ch in num if ch.isdigit())
        if tail:
            maxn = max(maxn, int(tail))
    return f"{pre}{maxn + 1:0{width}d}"


def build_document_create(res: Resource, client: Client, ns: Any) -> dict:
    doc_type = res.body_type or "invoice"  # invoice | bill
    contact_id = getattr(ns, "contact", None)
    if contact_id is None:
        raise ValueError(f"--contact <id> is required to create a {res.noun}")

    contact = client.show("contacts", contact_id, type_scope=res.contact_scope())
    currency = getattr(ns, "currency_code", None) or contact.get("currency_code") or "USD"

    items_specs = getattr(ns, "item", None) or []
    items = [parse_item(s) for s in items_specs]
    extra = load_data_arg(getattr(ns, "data", None))
    if not items and "items" not in extra:
        raise ValueError("at least one --item 'name=…,price=…' is required (or supply --data with items)")

    issued = _normalize_date(getattr(ns, "issued_at", None) or today_dt())
    due = getattr(ns, "due_at", None)
    due = _normalize_date(due) if due else add_days(issued, 30)

    category_id = getattr(ns, "category_id", None) or _document_default_category(client, doc_type)
    if category_id is None:
        raise ValueError("no category_id given and no default category configured; pass --category-id")

    number = getattr(ns, "number", None)
    if not number:
        number = _next_document_number(client, res, "INV-" if doc_type == "invoice" else "BILL-")

    body: dict[str, Any] = {
        "type": doc_type,
        "document_number": number,
        "status": getattr(ns, "status", None) or "draft",
        "issued_at": issued,
        "due_at": due,
        "currency_code": currency,
        "currency_rate": getattr(ns, "currency_rate", None) or 1,
        "contact_id": int(contact_id),
        "contact_name": contact.get("name", ""),
        "contact_email": contact.get("email"),
        "contact_tax_number": contact.get("tax_number"),
        "contact_phone": contact.get("phone"),
        "contact_address": contact.get("address"),
        "category_id": int(category_id),
        # Akaunting recomputes the document total from the line items and ADDS it
        # to whatever `amount` we send (CreateDocumentItemsAndTotals: amount +=
        # actual_total). Send 0 so the server-computed total is authoritative.
        "amount": 0,
        "items": items,
    }
    if getattr(ns, "notes", None):
        body["notes"] = ns.notes
    if getattr(ns, "order_number", None):
        body["order_number"] = ns.order_number
    body.update(parse_set(getattr(ns, "set_", None)))
    body.update(extra)
    if isinstance(body.get("items"), list):
        _normalize_items(body["items"])
    return body


def _items_from_current(current: dict) -> list[dict]:
    """Rebuild request items from a fetched document so an update that doesn't
    touch line items doesn't wipe them (UpdateDocument deletes & recreates all
    items from the request)."""
    out: list[dict] = []
    for it in current.get("items", {}).get("data", []):
        price = float(it.get("price") or 0)
        total = float(it.get("total") or 0)
        qty = it.get("quantity")
        if qty is None:
            qty = round(total / price, 4) if price else 1
        row = {
            "name": it.get("name"),
            "description": it.get("description") or "",
            "price": price,
            "quantity": qty,
        }
        if it.get("item_id"):
            row["item_id"] = int(it["item_id"])
        tax_ids = [t.get("tax_id") for t in it.get("taxes", {}).get("data", []) if t.get("tax_id")]
        if tax_ids:
            row["tax_id"] = tax_ids
        out.append(row)
    return out


def _normalize_dt_field(value: str) -> str:
    return _normalize_date(value[:19].replace("T", " "))


def build_document_update(res: Resource, client: Client, ns: Any, current: dict) -> dict:
    """Full update: Akaunting recreates items & totals from the request, so we
    resend the whole document, overlaying any provided fields."""
    body: dict[str, Any] = {
        "type": current.get("type", res.body_type),
        "document_number": current.get("document_number"),
        "status": current.get("status"),
        "issued_at": _normalize_dt_field(current.get("issued_at") or today_dt()),
        "due_at": _normalize_dt_field(current.get("due_at") or today_dt()),
        "currency_code": current.get("currency_code"),
        "currency_rate": current.get("currency_rate", 1),
        "contact_id": current.get("contact_id"),
        "contact_name": current.get("contact_name"),
        "contact_email": current.get("contact_email"),
        "category_id": current.get("category_id"),
        # send 0: server recomputes total from items and adds to amount
        "amount": 0,
        "items": _items_from_current(current),
    }
    if current.get("notes"):
        body["notes"] = current["notes"]

    for attr, key in [
        ("status", "status"), ("issued_at", "issued_at"), ("due_at", "due_at"),
        ("number", "document_number"), ("category_id", "category_id"),
        ("currency_code", "currency_code"), ("currency_rate", "currency_rate"),
        ("notes", "notes"), ("order_number", "order_number"),
    ]:
        v = getattr(ns, attr, None)
        if v is not None:
            body[key] = _normalize_date(v) if key in ("issued_at", "due_at") else v

    items_specs = getattr(ns, "item", None) or []
    if items_specs:
        body["items"] = [parse_item(s) for s in items_specs]

    body.update(parse_set(getattr(ns, "set_", None)))
    body.update(load_data_arg(getattr(ns, "data", None)))
    if isinstance(body.get("items"), list):
        _normalize_items(body["items"])
    return body


# --------------------------------------------------------------------------
# payment (transaction) body builder
# --------------------------------------------------------------------------

def build_payment_create(res: Resource, client: Client, ns: Any) -> dict:
    invoice_id = getattr(ns, "invoice", None)
    bill_id = getattr(ns, "bill", None)
    ptype = getattr(ns, "type", None)
    document = None
    document_id = getattr(ns, "document_id", None)
    contact_id = getattr(ns, "contact_id", None)
    category_id = getattr(ns, "category_id", None)

    if invoice_id:
        document = client.show("documents", invoice_id, type_scope="invoice")
        ptype = ptype or "income"
        document_id = document_id or int(invoice_id)
    elif bill_id:
        document = client.show("documents", bill_id, type_scope="bill")
        ptype = ptype or "expense"
        document_id = document_id or int(bill_id)
    ptype = ptype or "income"

    if document is not None:
        contact_id = contact_id or document.get("contact_id")
        category_id = category_id or document.get("category_id")

    if category_id is None:
        key = "default.income_category" if ptype == "income" else "default.expense_category"
        val = client.setting(key)
        category_id = int(val) if val else None
    if category_id is None:
        raise ValueError("no --category-id and no default category configured")

    amount = getattr(ns, "amount", None)
    if amount is None and document is not None:
        amount = document.get("amount_due", document.get("amount"))
    if amount is None:
        raise ValueError("--amount is required")

    account_id = getattr(ns, "account_id", None)
    if account_id is None:
        val = client.setting("default.account")
        account_id = int(val) if val else 1

    currency = getattr(ns, "currency_code", None)
    if currency is None and document is not None:
        currency = document.get("currency_code")
    currency = currency or "USD"

    number = getattr(ns, "number", None) or _next_transaction_number(client)

    body: dict[str, Any] = {
        "type": ptype,
        "number": number,
        "account_id": int(account_id),
        "paid_at": _normalize_date(getattr(ns, "paid_at", None) or now_dt()),
        "amount": amount,
        "currency_code": currency,
        "currency_rate": getattr(ns, "currency_rate", None) or 1,
        "category_id": int(category_id),
        "payment_method": getattr(ns, "payment_method", None) or "offline-payments.cash.1",
    }
    if contact_id:
        body["contact_id"] = int(contact_id)
    if getattr(ns, "reference", None):
        body["reference"] = ns.reference
    if getattr(ns, "description", None):
        body["description"] = ns.description
    body.update(parse_set(getattr(ns, "set_", None)))
    body.update(load_data_arg(getattr(ns, "data", None)))

    # A payment tied to a document must be posted to the nested route
    # POST /documents/{id}/transactions (the flat /transactions endpoint rejects
    # document_id). That route's ACL is derived from the document type, so it
    # needs the matching search=type:<invoice|bill> scope. Routing is conveyed
    # to cmd_create via reserved __endpoint__ / __type_scope__ keys.
    if document_id:
        doc_scope = "invoice" if ptype == "income" else "bill"
        body["__endpoint__"] = f"documents/{int(document_id)}/transactions"
        body["__type_scope__"] = doc_scope
    return body


def build_transfer_create(res: Resource, client: Client, ns: Any) -> dict:
    body = body_from_fields(res, ns, for_update=False)
    # Transfers validate transferred_at as date-only (Y-m-d), unlike transactions.
    raw = str(body.get("transferred_at") or _dt.date.today().isoformat())
    body["transferred_at"] = raw[:10]
    body.setdefault("payment_method", "offline-payments.cash.1")
    for key in ("from_account_id", "to_account_id"):
        if key in body:
            body[key] = int(body[key])
    return body


def _next_transaction_number(client: Client) -> str:
    pre = client.setting("transaction.number_prefix") or "PAY-"
    existing = client.list("transactions", all_pages=True)
    maxn = 0
    for row in existing:
        tail = "".join(ch for ch in str(row.get("number", "")) if ch.isdigit())
        if tail:
            maxn = max(maxn, int(tail))
    return f"{pre}{maxn + 1:05d}"
