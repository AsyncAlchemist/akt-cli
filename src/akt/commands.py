"""Generic command handlers shared by every resource."""

from __future__ import annotations

from typing import Any

from .client import Client
from .output import emit
from .resources import Resource, body_from_fields


def cmd_list(res: Resource, client: Client, ns: Any) -> int:
    search = ns.search
    if res.search_default:
        search = f"{res.search_default} {search}".strip() if search else res.search_default
    rows = client.list(
        res.endpoint,
        type_scope=res.type_scope,
        search=search or None,
        all_pages=ns.all,
        limit=ns.limit,
    )
    cols = [c[1] for c in res.columns]
    heads = [c[0] for c in res.columns]
    emit(rows, as_json=ns.json, columns=cols if not ns.json else None, headers=heads)
    return 0


def cmd_get(res: Resource, client: Client, ns: Any) -> int:
    row = client.show(res.endpoint, ns.id, type_scope=res.type_scope)
    emit(row, as_json=True)
    return 0


def cmd_create(res: Resource, client: Client, ns: Any) -> int:
    if res.build_create:
        body = res.build_create(res, client, ns)
    else:
        body = body_from_fields(res, ns, for_update=False)
        _require(res, body)
    # A builder may override the target route via reserved keys (e.g. paying a
    # document goes to documents/{id}/transactions).
    endpoint = body.pop("__endpoint__", res.endpoint)
    type_scope = body.pop("__type_scope__", res.type_scope)
    payload = client.post(endpoint, body, type_scope=type_scope)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    emit(data, as_json=True)
    return 0


def cmd_update(res: Resource, client: Client, ns: Any) -> int:
    if res.build_update:
        current = client.show(res.endpoint, ns.id, type_scope=res.type_scope)
        body = res.build_update(res, client, ns, current)
    else:
        # PUT is a full replace in Akaunting, so merge changes onto the current
        # record to satisfy required-field validation.
        current = client.show(res.endpoint, ns.id, type_scope=res.type_scope)
        body = body_from_fields(res, ns, for_update=True, current=current)
    payload = client.put(f"{res.endpoint}/{ns.id}", body, type_scope=res.type_scope)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    emit(data, as_json=True)
    return 0


def cmd_delete(res: Resource, client: Client, ns: Any) -> int:
    if res.delete_resolver:
        path, type_scope = res.delete_resolver(res, client, str(ns.id))
    else:
        path, type_scope = f"{res.endpoint}/{ns.id}", res.type_scope
    client.delete(path, type_scope=type_scope)
    print(f"deleted {res.noun} {ns.id}")
    return 0


def cmd_toggle(res: Resource, client: Client, ns: Any, enable: bool) -> int:
    # Akaunting exposes GET enable/disable endpoints
    action = "enable" if enable else "disable"
    payload = client.get(f"{res.endpoint}/{ns.id}/{action}", type_scope=res.type_scope)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    emit(data, as_json=True)
    return 0


def _require(res: Resource, body: dict) -> None:
    missing = [fld.dest for fld in res.fields if fld.required and fld.dest not in body]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
