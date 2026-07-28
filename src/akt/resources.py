"""Declarative resource registry and generic CRUD handlers.

Each :class:`Resource` maps a CLI noun (``customer``, ``invoice`` …) to an
Akaunting API endpoint plus the metadata needed to build create/update bodies
and render list tables. Resources whose create/update bodies are non-trivial
(documents, payments) override the body builders.
"""

from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from . import fx
from .client import Client
from .coa import resolve_coding


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
    local: bool = False             # CLI flag only; never sent in the request body


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
    supports_attachments: bool = False     # --attachment upload + attachments/download verbs
    read_only: bool = False                # expose only list/get (no create/update/delete)
    # When set, create/update/delete are driven through the session-authenticated
    # web route (client.web_json) at this path instead of the /api surface — for
    # resources whose CRUD Akaunting exposes only on the web (e.g. chart-of-accounts).
    web_endpoint: str | None = None
    help: str = ""

    # hooks (override for documents/payments)
    build_create: Callable[["Resource", Client, Any], dict] | None = None
    build_update: Callable[["Resource", Client, Any, dict], dict] | None = None
    # returns (path, type_scope) for delete; lets payments use the nested route
    delete_resolver: Callable[["Resource", Client, str], "tuple[str, str | None]"] | None = None
    # returns (path, type_scope) for update; lets document-linked payments use
    # the nested route (the flat /transactions route 400s on any document_id)
    update_resolver: Callable[["Resource", Client, str, dict], "tuple[str, str | None]"] | None = None
    # runs after a successful create/update on the /api path, with the returned
    # record + the argparse namespace; used by bank to re-date the auto-posted
    # opening-balance journal entry.
    post_write: Callable[["Resource", Client, dict, Any], None] | None = None

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


# --------------------------------------------------------------------------
# multipart / attachment helpers
# --------------------------------------------------------------------------

# Mirror Akaunting's default config('filesystems.mimes') so we can give a clear
# client-side error instead of a generic 422. Overridable server-side via
# FILESYSTEM_MIMES, but pdf/jpeg/jpg/png is the stock allow-list.
_DEFAULT_ATTACHMENT_EXTS = {"pdf", "jpeg", "jpg", "png"}


def flatten_form(body: dict, *, exclude: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Flatten a nested JSON body into PHP-style multipart form pairs.

    ``{"items": [{"name": "W", "tax_id": [1, 2]}]}`` becomes
    ``[("items[0][name]", "W"), ("items[0][tax_id][0]", "1"), ...]`` so a
    multipart upload carries the same structure the JSON surface would. Keys in
    ``exclude`` (and any reserved ``__…__`` routing keys) are skipped. ``None``
    values and booleans are coerced the way the API expects (``1``/``0``)."""
    out: list[tuple[str, str]] = []

    def emit(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            out.append((key, "1" if value else "0"))
        elif isinstance(value, dict):
            for k, v in value.items():
                emit(f"{key}[{k}]", v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                emit(f"{key}[{i}]", v)
        else:
            out.append((key, str(value)))

    for k, v in body.items():
        if k in exclude or (k.startswith("__") and k.endswith("__")):
            continue
        emit(k, v)
    return out


def load_attachments(paths: list[str] | None, *, field: str = "attachment[]"
                     ) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Read each ``--attachment`` path into a requests ``files`` tuple.

    ``field`` is the multipart key. Almost every Akaunting route loops over the
    array field ``attachment[]``; the sole exception is the nested transaction
    *update* job (``UpdateBankingDocumentTransaction``), which reads a single
    ``attachment`` file and crashes on an array — callers pass ``field="attachment"``
    for that route."""
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for path in paths or []:
        if not os.path.isfile(path):
            raise ValueError(f"attachment not found: {path}")
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext not in _DEFAULT_ATTACHMENT_EXTS:
            raise ValueError(
                f"attachment {path!r}: extension {ext!r} not allowed "
                f"(default allowed: {', '.join(sorted(_DEFAULT_ATTACHMENT_EXTS))})"
            )
        with open(path, "rb") as fh:
            content = fh.read()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        files.append((field, (os.path.basename(path), content, mime)))
    return files


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


def _date_opt(value: Any) -> "_dt.date | None":
    """First 10 chars of a date/datetime string -> date, else None (=> latest rate)."""
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def resolve_currency_rate(client: Client, ns: Any, *, currency_code: str | None,
                          on_date: Any) -> float:
    """The ``currency_rate`` to store on a foreign-currency transaction.

    Explicit ``--currency-rate`` wins; a transaction in the company default
    currency is always 1; otherwise the rate is fetched from the FX feed for the
    transaction's own date (or ``--rate-date``). ``--no-auto-rate`` restores the
    legacy "assume 1" behaviour. Mirrors the web UI, which copies the currency's
    live rate into the record — but date-aware, so back-dated entries book at the
    historical rate. Hard-fails (via fx.FxError) rather than silently booking at 1.
    """
    explicit = getattr(ns, "currency_rate", None)
    if explicit:
        return float(explicit)
    default = client.setting("default.currency", "USD") or "USD"
    if not currency_code or currency_code.upper() == default.upper():
        return 1.0
    if getattr(ns, "no_auto_rate", False):
        return 1.0
    rate_date = getattr(ns, "rate_date", None)
    if rate_date:
        try:
            on = _dt.date.fromisoformat(str(rate_date)[:10])
        except ValueError:
            raise ValueError(f"invalid --rate-date {rate_date!r}; use YYYY-MM-DD")
    else:
        on = _date_opt(on_date)
    rate = fx.resolve_rate(
        default, currency_code, on,
        ars_casa=getattr(ns, "ars_casa", None) or "bolsa",
        ars_side=getattr(ns, "ars_side", None) or "mid",
        cache_dir=str(fx.default_cache_dir()),
    )
    return float(rate)


def body_from_fields(res: Resource, ns: Any, *, for_update: bool,
                     current: dict | None = None) -> dict:
    """Assemble a request body from declared fields + --set + --data.

    On update, Akaunting validates required fields on PUT (it's a full replace,
    not a patch), so when ``current`` is supplied unspecified fields fall back to
    the existing record's values.
    """
    body: dict[str, Any] = {}
    for fld in res.fields:
        if fld.local:
            continue
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
    currency = (getattr(ns, "currency_code", None) or contact.get("currency_code")
                or client.setting("default.currency", "USD"))

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
        "currency_rate": resolve_currency_rate(client, ns, currency_code=currency, on_date=issued),
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

def parse_split_leg(spec: str) -> dict:
    """Parse one ``--split`` leg: ``account=<code|name>,debit=<x>`` or
    ``account=<code|name>,credit=<x>``. Exactly one of debit/credit; ``account`` is a
    GL code or name resolved against the COA (like ``--account``). Returns
    ``{'account': str, 'debit'|'credit': float}``."""
    item: dict[str, Any] = {}
    for part in spec.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"--split field must be key=value, got {part!r}")
        k, _, v = part.partition("=")
        item[k.strip()] = v.strip()
    if not item.get("account"):
        raise ValueError(f"--split leg requires an account=<code|name> field: {spec!r}")
    has_d = bool(item.get("debit"))
    has_c = bool(item.get("credit"))
    if has_d == has_c:
        raise ValueError(f"--split leg requires exactly one of debit=/credit=: {spec!r}")
    leg = {"account": item["account"]}
    if has_d:
        leg["debit"] = float(item["debit"])
    else:
        leg["credit"] = float(item["credit"])
    return leg


def build_payment_create(res: Resource, client: Client, ns: Any) -> dict:
    invoice_id = getattr(ns, "invoice", None)
    bill_id = getattr(ns, "bill", None)
    ptype = getattr(ns, "type", None)
    document = None
    document_id = getattr(ns, "document_id", None)
    contact_id = getattr(ns, "contact_id", None)
    category_id = getattr(ns, "category_id", None)

    coa = getattr(ns, "_coa", None)
    account_ref = getattr(ns, "account", None)
    category_ref = getattr(ns, "category", None)
    de_account_id = None
    if account_ref and category_ref:
        raise ValueError("pass only one of --account / --category")
    if account_ref or category_ref:
        if coa is None:
            raise ValueError(
                "--account/--category requires a COA config (pass --coa FILE or set AKT_COA_FILE)")
        if account_ref:
            de_account_id, coa_category_id = resolve_coding(coa, client, account_ref=account_ref)
        else:                               # --category NAME -> reverse-resolve the GL account
            de_account_id, coa_category_id = resolve_coding(coa, client, category_ref=category_ref)
        if category_id is None:            # explicit --category-id wins
            category_id = coa_category_id

    # --split: one bank transaction posting N GL item legs (like an invoice's line
    # items). Resolve each leg's account to a de_account_id; the placeholder item
    # leg uses the first leg's account, then the split_payment_legs post_write hook
    # fans it out into the N legs via the akt-api endpoint. category_id stays a
    # single representative label (mirrors invoice behavior); the GL is the truth.
    split_specs = getattr(ns, "split", None)
    split_legs = None
    if split_specs:
        if account_ref or category_ref:
            raise ValueError("pass either --split or --account/--category, not both")
        if coa is None:
            raise ValueError("--split requires a COA config (pass --coa FILE or set AKT_COA_FILE)")
        split_legs = []
        for spec in split_specs:
            leg = parse_split_leg(spec)
            leg_de_account_id, leg_category_id = resolve_coding(coa, client, account_ref=leg["account"])
            resolved = {"account_id": leg_de_account_id}
            if "debit" in leg:
                resolved["debit"] = leg["debit"]
            else:
                resolved["credit"] = leg["credit"]
            split_legs.append(resolved)
            if category_id is None:        # first leg's category = representative label
                category_id = leg_category_id
        de_account_id = split_legs[0]["account_id"]   # placeholder item leg
        ns._split_resolved = split_legs

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

    # Config-gated enforcement (revises the COA spec's Decision #3): with a COA
    # config loaded, a STANDALONE income/expense payment must resolve a GL account
    # — via --account, --category, or an explicit --set/--data de_account_id.
    # Bill/invoice payments (document_id set) post to A/P–A/R and are exempt.
    # With no COA config, behaviour is unchanged (legacy).
    overrides = {**parse_set(getattr(ns, "set_", None)),
                 **load_data_arg(getattr(ns, "data", None))}
    if (coa is not None and document_id is None and de_account_id is None
            and "de_account_id" not in overrides):
        raise ValueError(
            "standalone income/expense payment needs a GL account — pass --account "
            "CODE|NAME or --category NAME (with a COA config loaded, akt won't create "
            "an un-GL-coded transaction). Use --set de_account_id=<id> to override.")

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

    # --split balance: the legs must net to the transaction's single item leg, which
    # mirrors --amount and the bank leg. Income posts the item leg as a credit of
    # --amount (net -amount); expense as a debit (net +amount). Fail fast client-side.
    if split_legs is not None:
        expected = float(amount) if ptype == "expense" else -float(amount)
        net = sum(l.get("debit", 0) - l.get("credit", 0) for l in split_legs)
        if abs(net - expected) > 0.005:
            raise ValueError(
                f"--split legs net {net:.2f} but --amount {amount} (type {ptype}) needs "
                f"{expected:.2f} — income legs must be credit-heavy, expense debit-heavy")

    account_id = getattr(ns, "account_id", None)
    if account_id is None:
        val = client.setting("default.account")
        account_id = int(val) if val else 1

    currency = getattr(ns, "currency_code", None)
    if currency is None and document is not None:
        currency = document.get("currency_code")
    currency = currency or client.setting("default.currency", "USD")

    number = getattr(ns, "number", None) or _next_transaction_number(client)
    paid = _normalize_date(getattr(ns, "paid_at", None) or now_dt())

    body: dict[str, Any] = {
        "type": ptype,
        "number": number,
        "account_id": int(account_id),
        "paid_at": paid,
        "amount": amount,
        "currency_code": currency,
        "currency_rate": resolve_currency_rate(client, ns, currency_code=currency, on_date=paid),
        "category_id": int(category_id),
        "payment_method": getattr(ns, "payment_method", None) or "offline-payments.cash.1",
    }
    if contact_id:
        body["contact_id"] = int(contact_id)
    if getattr(ns, "reference", None):
        body["reference"] = ns.reference
    if getattr(ns, "description", None):
        body["description"] = ns.description
    if de_account_id is not None:
        body["de_account_id"] = de_account_id
    body.update(overrides)   # --set / --data win (computed once, above)

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


def resolve_payment_delete(res: Resource, client: Client, ident: str) -> "tuple[str, str | None]":
    """A payment linked to a document must be deleted via the nested route
    DELETE /documents/{doc}/transactions/{id} (the flat /transactions endpoint
    rejects it). Standalone payments/transfers delete via /transactions/{id}."""
    try:
        txn = client.show("transactions", ident)
    except Exception:
        return f"transactions/{ident}", None
    doc_id = txn.get("document_id")
    if doc_id:
        scope = "invoice" if txn.get("type") == "income" else "bill"
        return f"documents/{doc_id}/transactions/{ident}", scope
    return f"transactions/{ident}", None


def resolve_payment_update(res: Resource, client: Client, ident: str,
                           current: dict) -> "tuple[str, str | None]":
    """A payment linked to a document must be updated via the nested route
    PUT /documents/{doc}/transactions/{id}; the flat /transactions route 400s on
    any request carrying document_id. Mirrors :func:`resolve_payment_delete` but
    reuses the already-fetched ``current`` record to avoid a second GET."""
    doc_id = current.get("document_id")
    if doc_id:
        scope = "invoice" if current.get("type") == "income" else "bill"
        return f"documents/{doc_id}/transactions/{ident}", scope
    return f"transactions/{ident}", None


def split_payment_legs(res: Resource, client: Client, record: dict, ns: Any) -> None:
    """post_write hook for ``payment create --split``: after the transaction is
    created (with a single placeholder item leg), fan that leg out into the N
    resolved legs via the akt-api ``POST /ledgers/{id}/split`` endpoint. No-op unless
    ``--split`` was given. Requires the akt-api companion module."""
    legs = getattr(ns, "_split_resolved", None)
    if not legs:
        return
    if not client.has_ledger_api():
        raise ValueError(
            "--split needs the akt-api companion module (GET /api/akt-api/ledgers is "
            "404). Deploy it (scripts/deploy-akt-api.sh) and retry.")
    txn_id = record.get("id")
    payload = client.get("akt-api/ledgers", params={
        "ledgerable_type": "App\\Models\\Banking\\Transaction",
        "ledgerable_id": txn_id,
        "entry_type": "item",
    })
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if len(rows) != 1:
        raise ValueError(
            f"expected exactly one item leg on transaction {txn_id} to split, found "
            f"{len(rows)}")
    client.post(f"akt-api/ledgers/{rows[0]['id']}/split", {"legs": legs})


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
    # Seed the running max ONCE per client (scanning the ledger is O(N) — doing it
    # per create is O(N^2) over a batch). Subsequent creates increment in-process.
    if getattr(client, "_txn_number_max", None) is None:
        maxn = 0
        for row in client.list("transactions", all_pages=True, limit=100):
            tail = "".join(ch for ch in str(row.get("number", "")) if ch.isdigit())
            if tail:
                maxn = max(maxn, int(tail))
        client._txn_number_max = maxn
    client._txn_number_max += 1
    return f"{pre}{client._txn_number_max:05d}"


# --------------------------------------------------------------------------
# journal entry (double-entry) body builder
# --------------------------------------------------------------------------

def parse_journal_item(spec: str) -> dict:
    """Parse one journal line: ``account_id=10,debit=100`` or
    ``account_id=20,credit=100`` (optional ``id=`` on update to target an
    existing ledger, optional ``notes=``). A line carries a debit *or* a
    credit; the omitted side defaults to 0."""
    item: dict[str, Any] = {}
    for part in spec.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"--item field must be key=value, got {part!r}")
        k, _, v = part.partition("=")
        item[k.strip()] = _coerce(v.strip())
    if "account_id" not in item:
        raise ValueError(f"--item requires an account_id=… field: {spec!r}")
    if "debit" not in item and "credit" not in item:
        raise ValueError(f"--item requires a debit=… or credit=… field: {spec!r}")
    return item


def _normalize_journal_items(items: list[dict]) -> list[dict]:
    """Coerce account_id/id to int and ensure both debit & credit keys exist
    (Akaunting validates ``items.*.debit`` and ``items.*.credit`` as required)."""
    for it in items:
        if it.get("account_id") is not None:
            it["account_id"] = int(it["account_id"])
        if it.get("id") is not None:
            it["id"] = int(it["id"])
        it.setdefault("debit", 0)
        it.setdefault("credit", 0)
    return items


def _require_balanced(items: list[dict]) -> None:
    """A journal entry needs >= 2 lines whose debits equal its credits."""
    if len(items) < 2:
        raise ValueError("a journal entry needs at least 2 line items")
    debit = sum(float(it.get("debit") or 0) for it in items)
    credit = sum(float(it.get("credit") or 0) for it in items)
    if abs(debit - credit) > 1e-4:
        raise ValueError(
            f"journal entry does not balance: debits {debit:.2f} != credits {credit:.2f}"
        )


def _next_journal_number(client: Client) -> str:
    pre = client.setting("double-entry.journal.number_prefix") or "MJE-"
    digit = client.setting("double-entry.journal.number_digit")
    try:
        width = int(digit)
    except (TypeError, ValueError):
        width = 5
    existing = client.list("journal-entry", all_pages=True)
    maxn = 0
    for row in existing:
        tail = "".join(ch for ch in str(row.get("journal_number", "")) if ch.isdigit())
        if tail:
            maxn = max(maxn, int(tail))
    return f"{pre}{maxn + 1:0{width}d}"


def build_journal_create(res: Resource, client: Client, ns: Any) -> dict:
    extra = load_data_arg(getattr(ns, "data", None))
    items = [parse_journal_item(s) for s in (getattr(ns, "item", None) or [])]
    if not items and "items" not in extra:
        raise ValueError(
            "at least two --item 'account_id=…,debit=…|credit=…' lines are "
            "required (or supply --data with items)"
        )

    description = getattr(ns, "description", None)
    if not description:
        raise ValueError("--description is required to create a journal-entry")

    paid = _normalize_date(getattr(ns, "paid_at", None) or today_dt())
    currency = getattr(ns, "currency_code", None) or client.setting("default.currency", "USD")
    body: dict[str, Any] = {
        "paid_at": paid,
        "journal_number": getattr(ns, "journal_number", None),
        "description": description,
        "basis": getattr(ns, "basis", None) or "accrual",
        "currency_code": currency,
        "currency_rate": resolve_currency_rate(client, ns, currency_code=currency, on_date=paid),
        # Akaunting recomputes the entry amount from the ledger debits; send 0.
        "amount": 0,
        "items": items,
    }
    if getattr(ns, "reference", None):
        body["reference"] = ns.reference
    body.update(parse_set(getattr(ns, "set_", None)))
    body.update(extra)
    if isinstance(body.get("items"), list):
        _normalize_journal_items(body["items"])
        _require_balanced(body["items"])
    # Assign the journal number last — after client-side validation — so an
    # invalid entry never burns a lookup (and the imbalance error surfaces
    # without a network round-trip).
    if not body.get("journal_number"):
        body["journal_number"] = _next_journal_number(client)
    return body


def _journal_items_from_current(current: dict) -> list[dict]:
    """Rebuild request items from a fetched entry's ledgers so an update that
    doesn't touch lines doesn't delete them (UpdateJournalEntry deletes any
    ledger whose id is absent from the request). Each line carries its ledger
    ``id`` so the existing row is updated in place rather than recreated."""
    out: list[dict] = []
    for led in current.get("ledgers", {}).get("data", []):
        row = {
            "account_id": led.get("account_id"),
            "debit": float(led.get("debit") or 0),
            "credit": float(led.get("credit") or 0),
        }
        if led.get("id"):
            row["id"] = int(led["id"])
        out.append(row)
    return out


def build_journal_update(res: Resource, client: Client, ns: Any, current: dict) -> dict:
    """Full update: Akaunting re-derives ledgers & amount from the request, so
    resend the whole entry, overlaying any provided fields."""
    body: dict[str, Any] = {
        "paid_at": _normalize_dt_field(current.get("paid_at") or today_dt()),
        "journal_number": current.get("journal_number"),
        "description": current.get("description"),
        "basis": current.get("basis") or "accrual",
        "currency_code": current.get("currency_code"),
        "currency_rate": current.get("currency_rate", 1),
        "amount": 0,
        "items": _journal_items_from_current(current),
    }
    if current.get("reference"):
        body["reference"] = current["reference"]

    for attr, key in [
        ("paid_at", "paid_at"), ("journal_number", "journal_number"),
        ("description", "description"), ("basis", "basis"),
        ("currency_code", "currency_code"), ("currency_rate", "currency_rate"),
        ("reference", "reference"),
    ]:
        v = getattr(ns, attr, None)
        if v is not None:
            body[key] = _normalize_date(v) if key == "paid_at" else v

    items_specs = getattr(ns, "item", None) or []
    if items_specs:
        body["items"] = [parse_journal_item(s) for s in items_specs]

    body.update(parse_set(getattr(ns, "set_", None)))
    body.update(load_data_arg(getattr(ns, "data", None)))
    if isinstance(body.get("items"), list):
        _normalize_journal_items(body["items"])
        _require_balanced(body["items"])
    return body


# --------------------------------------------------------------------------
# opening-balance journal entry re-dating (bank post_write hook)
# --------------------------------------------------------------------------

def _journal_reput_body(current: dict, *, paid_at: str, journal_number: str) -> dict:
    """Full journal-entry PUT body that re-dates an existing entry, preserving
    its ledgers (by id) and amounts. ``journal_number`` is supplied by the caller
    (the server-created opening-balance entry carries none, but the API requires
    one)."""
    body: dict[str, Any] = {
        "paid_at": paid_at,
        "journal_number": journal_number,
        "description": current.get("description"),
        "basis": current.get("basis") or "accrual",
        "currency_code": current.get("currency_code") or "USD",
        "currency_rate": current.get("currency_rate", 1) or 1,
        "amount": 0,
        "items": _journal_items_from_current(current),
    }
    if current.get("reference"):
        body["reference"] = current["reference"]
    return body


def _find_opening_balance_je(client: Client, record: dict) -> dict | None:
    """Locate the opening-balance journal entry the Double-Entry module auto-posts
    for a bank/cash account. It carries ``reference = "opening-balance:<coa_id>"``
    and ``description`` ending ``";<account name>"``. The chart-of-accounts API
    doesn't expose the bank<->coa link, so match on those two fields and, if an
    account name is duplicated, take the newest by ``created_at``."""
    name = str(record.get("name") or "")
    suffix = ";" + name
    matches = [
        e for e in client.list("journal-entry", all_pages=True)
        if str(e.get("reference") or "").startswith("opening-balance:")
        and str(e.get("description") or "").endswith(suffix)
    ]
    if not matches:
        return None
    matches.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    return matches[0]


def redate_opening_balance(res: "Resource", client: Client, record: dict, ns: Any) -> None:
    """post_write hook for bank create/update: re-date the auto-posted
    opening-balance journal entry to ``--opening-balance-date``.

    The Double-Entry module stamps that entry with the account's created_at and
    exposes no date field, so we set the balance normally (keeping the Banking
    register correct) and rewrite the entry's date here. Re-dating via the
    journal-entry route also rewrites each ledger's issued_at (what reports
    filter on), and the module's account-update path only touches ledger
    amounts, so the date sticks across later edits."""
    date = getattr(ns, "opening_balance_date", None)
    if not date:
        return
    je = _find_opening_balance_je(client, record)
    if je is None:
        print(f"note: no opening-balance journal entry found for {res.noun} "
              f"{record.get('id')}; --opening-balance-date had no effect "
              f"(opening balance is 0, or the Double-Entry module / owners-"
              f"contribution account is not configured)", file=sys.stderr)
        return
    number = je.get("journal_number") or _next_journal_number(client)
    body = _journal_reput_body(je, paid_at=_normalize_date(date), journal_number=number)
    client.put(f"journal-entry/{je['id']}", body)
    print(f"note: re-dated opening-balance journal entry {je['id']} to {date}",
          file=sys.stderr)


# --------------------------------------------------------------------------
# chart-of-accounts (double-entry) — web-surface CRUD body builder
# --------------------------------------------------------------------------

def _apply_sub_account(body: dict) -> None:
    """The web CRUD form carries an ``is_sub_account`` toggle; a parent
    ``account_id`` is only honored when it's ``"true"`` and is cleared when
    ``"false"``. Derive it from whether a parent was supplied."""
    body["is_sub_account"] = "true" if body.get("account_id") else "false"


def build_account_create(res: Resource, client: Client, ns: Any) -> dict:
    body = body_from_fields(res, ns, for_update=False)
    if not body.get("name"):
        raise ValueError("--name is required to create an account")
    _apply_sub_account(body)
    return body


def build_account_update(res: Resource, client: Client, ns: Any, current: dict) -> dict:
    # A full replace: Akaunting's Account request re-validates `name` on update,
    # so backfill unspecified fields from the current record.
    body = body_from_fields(res, ns, for_update=True, current=current)
    _apply_sub_account(body)
    return body
