"""Generic command handlers shared by every resource."""

from __future__ import annotations

import os
from typing import Any

from .client import Client
from .output import emit
from .resources import Resource, body_from_fields, flatten_form, load_attachments


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
    files = load_attachments(getattr(ns, "attachment", None))
    if files:
        payload = client.post_multipart(endpoint, flatten_form(body), files,
                                        type_scope=type_scope)
    else:
        payload = client.post(endpoint, body, type_scope=type_scope)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    emit(data, as_json=True)
    return 0


def cmd_update(res: Resource, client: Client, ns: Any) -> int:
    current = client.show(res.endpoint, ns.id, type_scope=res.type_scope)
    if res.build_update:
        body = res.build_update(res, client, ns, current)
    else:
        # PUT is a full replace in Akaunting, so merge changes onto the current
        # record to satisfy required-field validation.
        body = body_from_fields(res, ns, for_update=True, current=current)

    # A resolver may redirect to a nested route (a document-linked payment must
    # update via documents/{doc}/transactions/{id}; the flat route 400s on it).
    if res.update_resolver:
        path, type_scope = res.update_resolver(res, client, str(ns.id), current)
    else:
        path, type_scope = f"{res.endpoint}/{ns.id}", res.type_scope

    # The nested transaction-update job (documents/{doc}/transactions/{id}) reads
    # a SINGLE `attachment` file and 500s on the usual `attachment[]` array; every
    # other route loops the array. Detect that route by its shape.
    nested_txn = "/transactions/" in path
    att_paths = getattr(ns, "attachment", None)
    if nested_txn and att_paths and len(att_paths) > 1:
        raise ValueError(
            "a document-linked payment accepts only one attachment per update "
            "(Akaunting's nested update route takes a single file)"
        )
    files = load_attachments(att_paths, field="attachment" if nested_txn else "attachment[]")
    remove = getattr(ns, "remove_attachment", False)
    if files:
        form = flatten_form(body)
        if remove:
            form.append(("remove_attachment", "1"))
        payload = client.put_multipart(path, form, files, type_scope=type_scope)
    else:
        if remove:
            body["remove_attachment"] = 1
        payload = client.put(path, body, type_scope=type_scope)
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


def _attachment_rows(record: dict) -> list[dict]:
    """Normalize a record's ``attachment`` field (an array of media, or ``False``
    when empty) into display rows with a derived ``name`` (filename.extension)."""
    raw = record.get("attachment")
    if not raw or not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for m in raw:
        name = m.get("basename") or m.get("filename", "")
        ext = m.get("extension")
        if ext and not str(name).endswith(f".{ext}"):
            name = f"{name}.{ext}"
        rows.append({
            "id": m.get("id"),
            "name": name,
            "size": m.get("size"),
            "mime_type": m.get("mime_type"),
        })
    return rows


def cmd_attachments(res: Resource, client: Client, ns: Any) -> int:
    record = client.show(res.endpoint, ns.id, type_scope=res.type_scope)
    rows = _attachment_rows(record)
    cols = ["id", "name", "size", "mime_type"]
    emit(rows, as_json=ns.json, columns=None if ns.json else cols,
         headers=["ID", "Name", "Size", "Type"])
    return 0


def cmd_download_attachment(res: Resource, client: Client, ns: Any) -> int:
    record = client.show(res.endpoint, ns.id, type_scope=res.type_scope)
    rows = _attachment_rows(record)
    if getattr(ns, "media_id", None):
        rows = [r for r in rows if str(r["id"]) == str(ns.media_id)]
        if not rows:
            raise ValueError(f"no attachment with media id {ns.media_id} on {res.noun} {ns.id}")
    if not rows:
        print(f"{res.noun} {ns.id} has no attachments")
        return 0
    out_dir = getattr(ns, "out", None) or "."
    os.makedirs(out_dir, exist_ok=True)
    real_out = os.path.realpath(out_dir)
    saved: list[str] = []
    for r in rows:
        filename, content = client.download_media(r["id"])
        # The filename comes from the server's Content-Disposition, so treat it
        # as untrusted: strip any path components and reject traversal so a
        # malicious/compromised response can't write outside --out.
        safe = os.path.basename(filename)
        if not safe or safe in (".", "..") or "/" in safe or "\\" in safe:
            safe = f"attachment-{r['id']}"
        dest = os.path.join(out_dir, safe)
        if os.path.exists(dest):  # avoid clobbering same-named attachments
            dest = os.path.join(out_dir, f"{r['id']}-{safe}")
        if os.path.commonpath([real_out, os.path.realpath(dest)]) != real_out:
            raise ValueError(f"refusing to write attachment outside {out_dir}")
        with open(dest, "wb") as fh:
            fh.write(content)
        saved.append(dest)
    if ns.json:
        emit([{"media_id": r["id"], "path": p} for r, p in zip(rows, saved)], as_json=True)
    else:
        for p in saved:
            print(f"saved {p}")
    return 0


def _require(res: Resource, body: dict) -> None:
    missing = [fld.dest for fld in res.fields if fld.required and fld.dest not in body]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
