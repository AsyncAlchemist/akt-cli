"""akt — command-line toolbox for an Akaunting accounting instance."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .client import ApiError, Client
from .config import ConfigError, load_config
from .commands import (
    cmd_attachments,
    cmd_create,
    cmd_delete,
    cmd_download_attachment,
    cmd_get,
    cmd_list,
    cmd_toggle,
    cmd_update,
)
from .output import emit
from .registry import RESOURCES, BY_NOUN
from .resources import Resource, load_data_arg
from .coa import load_coa, plan_sync, render_plan, apply_plan
from .ledger import resolve_account_id
from .verify import find_miscodings


def _add_field_args(p: argparse.ArgumentParser, res: Resource, *, for_update: bool) -> None:
    for fld in res.fields:
        flag = f"--{fld.name}"
        if fld.is_flag:
            grp = p.add_mutually_exclusive_group()
            grp.add_argument(f"--{fld.name}", dest=fld.dest, action="store_true",
                             default=None, help=fld.help)
            negname = "disabled" if fld.name == "enabled" else f"no-{fld.name}"
            grp.add_argument(f"--{negname}", dest=fld.dest, action="store_false",
                             default=None, help=argparse.SUPPRESS)
        else:
            req = fld.required and not for_update and res.build_create is None
            p.add_argument(flag, dest=fld.dest, metavar=fld.dest.upper(),
                           required=req, choices=fld.choices, help=fld.help)
    if res.endpoint == "documents":
        p.add_argument("--item", action="append", metavar="K=V,...",
                       help="line item, e.g. 'name=Widget,price=10,quantity=2,tax_id=1' (repeatable)")
    elif res.endpoint == "journal-entry":
        p.add_argument("--item", action="append", metavar="K=V,...",
                       help="ledger line (>= 2, must balance), e.g. "
                            "'account_id=10,debit=100' or 'account_id=20,credit=100' (repeatable)")
    if res.supports_attachments:
        p.add_argument("--attachment", action="append", metavar="PATH",
                       help="attach a file (pdf/jpg/png, repeatable); switches the "
                            "request to multipart upload")
        if for_update:
            p.add_argument("--remove-attachment", dest="remove_attachment",
                           action="store_true",
                           help="clear existing attachment(s) on this record")
    p.add_argument("--set", dest="set_", action="append", metavar="KEY=VALUE",
                   help="set an arbitrary body field (repeatable; value JSON-coerced)")
    p.add_argument("--data", metavar="JSON|@FILE",
                   help="merge raw JSON body (inline or @file) — wins over other flags")


def _build_parser() -> argparse.ArgumentParser:
    # --json lives on a parent parser shared by every subcommand so it works
    # both before the subcommand (akt --json customer list) and after it
    # (akt customer list --json). default=SUPPRESS stops subparser parsing from
    # clobbering a value supplied at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS,
                        help="Force raw JSON output")

    parser = argparse.ArgumentParser(
        prog="akt",
        parents=[common],
        description="Drive an Akaunting accounting instance from the command line.",
    )
    # Connection flags are top-level only (given before the subcommand) so their
    # option strings never collide with a resource's own --email / --currency-code
    # etc. Distinct conn_* dests also keep them out of the field namespace.
    parser.add_argument("--base-url", dest="conn_base_url",
                        help="API base URL (or AKT_BASE_URL / APP_URL)")
    parser.add_argument("--email", dest="conn_email", help="Admin email (or AKT_EMAIL)")
    parser.add_argument("--password", dest="conn_password", help="Admin password (or AKT_PASSWORD)")
    parser.add_argument("--company", dest="conn_company", help="Company id (default 1, or AKT_COMPANY)")
    parser.add_argument("--throttle", dest="conn_throttle", type=float, default=None,
                        metavar="SECONDS",
                        help="Min seconds between API calls (or AKT_THROTTLE). "
                             "Use a value like 1.0 to avoid tripping host bot-protection.")
    parser.add_argument("--coa", dest="coa_file", default=None, metavar="FILE",
                        help="COA config for category<->account linking "
                             "(or AKT_COA_FILE / ./coa.toml / ~/.config/akt/coa.toml)")

    sub = parser.add_subparsers(dest="resource", metavar="<resource>")

    for res in RESOURCES:
        rp = sub.add_parser(res.noun, help=res.help)
        verbs = rp.add_subparsers(dest="verb", metavar="<verb>")

        lp = verbs.add_parser("list", parents=[common], help=f"List {res.noun}s")
        lp.add_argument("--search", default="", help="search-string filter (e.g. 'name:Acme')")
        lp.add_argument("--all", action="store_true", help="fetch all pages")
        lp.add_argument("--limit", type=int, help="records per page")
        lp.set_defaults(_handler=lambda res, c, ns: cmd_list(res, c, ns))

        gp = verbs.add_parser("get", parents=[common], help=f"Show one {res.noun} by id")
        gp.add_argument("id")
        gp.set_defaults(_handler=lambda res, c, ns: cmd_get(res, c, ns))

        # Read-only resources (e.g. chart-of-accounts) expose only list/get; the
        # API has no create/update/delete route for them.
        if not res.read_only:
            cp = verbs.add_parser("create", parents=[common], help=f"Create a {res.noun}")
            _add_field_args(cp, res, for_update=False)
            cp.set_defaults(_handler=lambda res, c, ns: cmd_create(res, c, ns))

            up = verbs.add_parser("update", parents=[common], help=f"Update a {res.noun}")
            up.add_argument("id")
            _add_field_args(up, res, for_update=True)
            up.set_defaults(_handler=lambda res, c, ns: cmd_update(res, c, ns))

            dp = verbs.add_parser("delete", parents=[common], help=f"Delete a {res.noun}")
            dp.add_argument("id")
            dp.set_defaults(_handler=lambda res, c, ns: cmd_delete(res, c, ns))

        if res.supports_attachments:
            ap = verbs.add_parser("attachments", parents=[common],
                                  help=f"List attachments on a {res.noun}")
            ap.add_argument("id")
            ap.set_defaults(_handler=lambda res, c, ns: cmd_attachments(res, c, ns))

            dap = verbs.add_parser("download-attachment", parents=[common],
                                   help=f"Download attachment(s) from a {res.noun}")
            dap.add_argument("id")
            dap.add_argument("--out", metavar="DIR", help="output directory (default .)")
            dap.add_argument("--media-id", metavar="ID",
                             help="download only this media id (default: all)")
            dap.set_defaults(_handler=lambda res, c, ns: cmd_download_attachment(res, c, ns))

        if res.supports_toggle:
            ep = verbs.add_parser("enable", parents=[common], help=f"Enable a {res.noun}")
            ep.add_argument("id")
            ep.set_defaults(_handler=lambda res, c, ns: cmd_toggle(res, c, ns, True))
            xp = verbs.add_parser("disable", parents=[common], help=f"Disable a {res.noun}")
            xp.add_argument("id")
            xp.set_defaults(_handler=lambda res, c, ns: cmd_toggle(res, c, ns, False))

    # ---- non-resource utility commands ----
    pp = sub.add_parser("ping", parents=[common], help="Health check (unauthenticated)")
    pp.set_defaults(_special="ping")

    cp = sub.add_parser("company", parents=[common], help="List companies / show current")
    cp.set_defaults(_special="company")

    sp = sub.add_parser("settings", parents=[common], help="List company settings")
    sp.add_argument("--search", default="", help="e.g. 'key:default.account'")
    sp.set_defaults(_special="settings")

    rp = sub.add_parser("raw", parents=[common], help="Call an arbitrary API endpoint")
    rp.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE",
                                       "get", "post", "put", "patch", "delete"])
    rp.add_argument("path", help="endpoint path, e.g. 'items' or 'documents/5'")
    rp.add_argument("--data", metavar="JSON|@FILE", help="request body (inline JSON or @file)")
    rp.add_argument("--query", action="append", metavar="K=V", help="query param (repeatable)")
    rp.add_argument("--type-scope", help="search=type:X scope for contacts/documents")
    rp.set_defaults(_special="raw")

    coap = sub.add_parser("coa", parents=[common],
                          help="Sync chart-of-accounts <-> categories from a COA config")
    coav = coap.add_subparsers(dest="coa_verb", metavar="<verb>")
    cdp = coav.add_parser("diff", parents=[common], help="Preview the sync plan (read-only)")
    cdp.add_argument("--prune", action="store_true",
                     help="also show accounts/categories that --prune would disable")
    cdp.set_defaults(_special="coa_diff")
    csp = coav.add_parser("sync", parents=[common],
                          help="Apply: create/rename accounts + mirror categories")
    csp.add_argument("--prune", action="store_true",
                     help="disable accounts/categories absent from the config (never deletes)")
    csp.set_defaults(_special="coa_sync")

    lp = sub.add_parser("ledger", parents=[common],
                        help="Show general-ledger postings (needs the akt-api module)")
    lp.add_argument("--account", required=True, metavar="CODE|NAME",
                    help="Chart-of-accounts code or name to show postings for")
    lp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="Earliest issued_at")
    lp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="Latest issued_at")
    lp.set_defaults(_special="ledger")

    vp = sub.add_parser("verify", parents=[common],
                        help="Audit standalone income/expense postings vs their category "
                             "(needs akt-api + --coa)")
    vp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="Earliest paid_at")
    vp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="Latest paid_at")
    vp.set_defaults(_special="verify")

    return parser


def _run_special(name: str, client: Client, ns: Any) -> int:
    if name == "ping":
        emit(client.get("ping"), as_json=True)
        return 0
    if name == "company":
        rows = client.list("companies")
        cols = ["id", "name", "email", "currency", "enabled"]
        emit(rows, as_json=ns.json, columns=None if ns.json else cols,
             headers=["ID", "Name", "Email", "Currency", "Enabled"])
        return 0
    if name == "settings":
        rows = client.list("settings", search=ns.search or None, all_pages=True)
        cols = ["id", "key", "value"]
        emit(rows, as_json=ns.json, columns=None if ns.json else cols,
             headers=["ID", "Key", "Value"])
        return 0
    if name == "raw":
        params = dict(kv.split("=", 1) for kv in (ns.query or []))
        body = load_data_arg(ns.data) if ns.data else None
        result = client.request(ns.method.upper(), ns.path, params=params or None,
                                json_body=body, type_scope=ns.type_scope)
        emit(result, as_json=True)
        return 0
    if name == "coa_diff":
        coa = ns._coa
        if coa is None:
            raise ValueError("no COA config found (use --coa FILE or set AKT_COA_FILE)")
        live_accounts = client.list("chart-of-accounts", all_pages=True)
        live_categories = client.list("categories", all_pages=True)
        plan = plan_sync(coa, live_accounts, live_categories, prune=ns.prune)
        for line in render_plan(plan):
            print(line)
        return 0
    if name == "coa_sync":
        coa = ns._coa
        if coa is None:
            raise ValueError("no COA config found (use --coa FILE or set AKT_COA_FILE)")
        live_accounts = client.list("chart-of-accounts", all_pages=True)
        live_categories = client.list("categories", all_pages=True)
        plan = plan_sync(coa, live_accounts, live_categories, prune=ns.prune)
        for line in render_plan(plan):
            print(line)
        if plan.is_empty:
            return 0
        summary = apply_plan(client, plan, prune=ns.prune)
        print("applied: " + ", ".join(f"{k}={v}" for k, v in summary.items() if v))
        return 0
    if name == "ledger":
        if not client.has_ledger_api():
            raise ValueError("`akt ledger` needs the akt-api companion module — "
                             "install it into your Akaunting modules/ directory (see akt-api/README.md)")
        accounts = client.list("chart-of-accounts", all_pages=True)
        account_id = resolve_account_id(accounts, ns.account)
        params: dict = {"account_id": account_id}
        if ns.date_from:
            params["issued_from"] = ns.date_from
        if ns.date_to:
            params["issued_to"] = ns.date_to
        rows = client.list("akt/ledgers", params=params, all_pages=True)
        cols = ["issued_at", "entry_type", "debit", "credit", "ledgerable_type", "ledgerable_id"]
        emit(rows, as_json=ns.json, columns=None if ns.json else cols,
             headers=["Date", "Type", "Debit", "Credit", "Source", "Source ID"])
        return 0
    if name == "verify":
        coa = ns._coa
        if coa is None:
            raise ValueError("`akt verify` needs a COA config (use --coa FILE or set AKT_COA_FILE)")
        if not client.has_ledger_api():
            raise ValueError("`akt verify` needs the akt-api companion module — "
                             "install it into your Akaunting modules/ directory (see akt-api/README.md)")

        txns = [t for t in client.list("transactions", all_pages=True)
                if t.get("type") in ("income", "expense") and not t.get("document_id")]
        if ns.date_from:
            txns = [t for t in txns if str(t.get("paid_at", ""))[:10] >= ns.date_from]
        if ns.date_to:
            txns = [t for t in txns if str(t.get("paid_at", ""))[:10] <= ns.date_to]

        categories_by_id = {c["id"]: {"name": c.get("name"), "type": c.get("type")}
                            for c in client.list("categories", all_pages=True)}
        accounts = client.list("chart-of-accounts", all_pages=True)
        accounts_by_id = {a["id"]: {"code": a.get("code"), "name": a.get("name")} for a in accounts}
        accounts_by_code = {int(a["code"]): a["id"] for a in accounts if a.get("code") is not None}

        item_ledgers = client.list("akt/ledgers", all_pages=True, params={
            "ledgerable_type": "App\\Models\\Banking\\Transaction", "entry_type": "item"})
        item_account_by_txn = {int(l["ledgerable_id"]): int(l["account_id"]) for l in item_ledgers}

        findings = find_miscodings(txns, categories_by_id, accounts_by_id,
                                   accounts_by_code, item_account_by_txn, coa)
        cols = ["transaction_id", "paid_at", "amount", "category",
                "expected_code", "actual_code", "reason"]
        emit(findings, as_json=ns.json, columns=None if ns.json else cols,
             headers=["Txn", "Date", "Amount", "Category", "Expected", "Actual", "Reason"])
        return 0 if not findings else 1
    raise ValueError(name)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    if not getattr(ns, "resource", None):
        parser.print_help()
        return 1

    # Flags on the shared parent use default=SUPPRESS; backfill them here.
    ns.json = getattr(ns, "json", False)

    try:
        config = load_config(
            base_url=getattr(ns, "conn_base_url", None),
            email=getattr(ns, "conn_email", None),
            password=getattr(ns, "conn_password", None),
            company=getattr(ns, "conn_company", None),
        )
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    import os
    throttle = getattr(ns, "conn_throttle", None)
    if throttle is None:
        throttle = float(os.environ.get("AKT_THROTTLE", "0") or 0)
    client = Client(config, throttle=throttle)

    try:
        ns._coa = load_coa(getattr(ns, "coa_file", None))
    except (ValueError, OSError) as e:
        print(f"coa config error: {e}", file=sys.stderr)
        return 2

    try:
        special = getattr(ns, "_special", None)
        if special:
            return _run_special(special, client, ns)

        if ns.resource not in BY_NOUN:
            # command group (e.g. "coa") invoked without a verb
            sub = next(a for a in parser._subparsers._actions  # type: ignore[attr-defined]
                       if a.dest == "resource")
            sub.choices[ns.resource].print_help()  # type: ignore[union-attr]
            return 1

        res = BY_NOUN[ns.resource]
        handler = getattr(ns, "_handler", None)
        if handler is None:
            # resource given without a verb
            sub = next(a for a in parser._subparsers._actions  # type: ignore[attr-defined]
                       if a.dest == "resource")
            sub.choices[ns.resource].print_help()  # type: ignore[union-attr]
            return 1
        return handler(res, client, ns)
    except ApiError as e:
        print(str(e), file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
