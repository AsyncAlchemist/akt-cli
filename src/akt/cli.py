"""akt — command-line toolbox for an Akaunting accounting instance."""

from __future__ import annotations

import argparse
import datetime as _dt
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
from .verify import find_miscodings, find_report_dropped, find_unposted, build_recode_plan
from . import reports


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
    elif res.endpoint == "transactions" and not for_update:
        p.add_argument("--split", action="append", metavar="account=CODE,debit|credit=X",
                       help="split the GL posting across multiple accounts (repeatable): "
                            "one bank transaction that posts N GL item legs, the way an "
                            "invoice's line items do. e.g. 'account=400,credit=13686' "
                            "'account=545,debit=280'. Legs must net to --amount; needs a "
                            "--coa config and the akt-api companion module. category_id "
                            "stays a single label — the DoubleEntry GL (item legs) is the "
                            "source of truth.")
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
                        help="Audit standalone income/expense postings: wrong GL account for "
                             "their category, AND type/class mismatches the COA report silently "
                             "drops (e.g. a refund booked as income onto an expense account). "
                             "Needs akt-api + --coa")
    vp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="Earliest paid_at")
    vp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="Latest paid_at")
    vp.set_defaults(_special="verify")

    rcp = sub.add_parser("recode", parents=[common],
                         help="Repost mis-coded standalone income/expense txns to their "
                              "category's GL account (needs akt-api + --coa)")
    rcp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="Earliest paid_at")
    rcp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="Latest paid_at")
    rcp.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="show the plan without writing anything")
    rcp.set_defaults(_special="recode")

    bp = sub.add_parser("balance", parents=[common],
                        help="Show one account's balance (needs the akt-api module)")
    bp.add_argument("--account", required=True, metavar="CODE|NAME")
    bp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    bp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    bp.add_argument("--expected", type=float, metavar="AMOUNT",
                    help="reconcile: compare to this figure, exit 1 on mismatch")
    bp.set_defaults(_special="balance")

    tp = sub.add_parser("trial-balance", parents=[common],
                        help="Trial balance (needs the akt-api module)")
    tp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    tp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    tp.set_defaults(_special="trial_balance")

    rpp = sub.add_parser("report", parents=[common], help="Financial reports (needs the akt-api module)")
    rpv = rpp.add_subparsers(dest="report_verb", metavar="<report>")
    for verb, sp in (("profit-loss", "report_pnl"), ("balance-sheet", "report_bs")):
        p = rpv.add_parser(verb, parents=[common])
        p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
        p.set_defaults(_special=sp)

    fxp = sub.add_parser("fx", parents=[common],
                         help="Look up / preview an exchange rate (ECB majors + ARS feeds)")
    fxp.add_argument("code", metavar="CODE", help="currency code, e.g. EUR or ARS")
    fxp.add_argument("--on", metavar="YYYY-MM-DD", help="rate date (default: latest)")
    fxp.add_argument("--to", metavar="CODE",
                     help="base currency to price against (default: company default currency)")
    fxp.add_argument("--amount", type=float, metavar="N",
                     help="convert N units of CODE into the base currency")
    fxp.add_argument("--ars-casa", dest="ars_casa", metavar="CASA",
                     help="ARS dollar rate type (default bolsa/MEP)")
    fxp.add_argument("--ars-side", dest="ars_side", metavar="SIDE",
                     choices=["mid", "venta", "compra"], help="ARS price side (default mid)")
    fxp.set_defaults(_special="fx")

    gp = sub.add_parser("gc-ledger", parents=[common],
                        help="Report/remove orphaned ledger rows (needs the akt-api module)")
    gp.add_argument("--apply", action="store_true",
                    help="delete the orphaned rows (default: report only)")
    gp.set_defaults(_special="gc_ledger")

    return parser


def _need_akt_api(client: Client, cmd: str) -> None:
    if not client.has_ledger_api():
        raise ValueError(f"`akt {cmd}` needs the akt-api companion module — "
                         "install it into your Akaunting modules/ directory (see akt-api/README.md)")


def _fetch_balances(client: Client, ns: Any):
    # One big page instead of paginating (a chart of accounts is small); saves
    # round-trips, which matters when a script runs many balance/report calls.
    accounts = client.list("chart-of-accounts", all_pages=True, params={"limit": 1000})
    accts_by_id = {a["id"]: {"code": a.get("code"), "name": a.get("name"),
                             "type_id": a.get("type_id")} for a in accounts}
    params: dict[str, Any] = {}
    if getattr(ns, "date_from", None):
        params["date_from"] = ns.date_from
    if getattr(ns, "date_to", None):
        params["date_to"] = ns.date_to
    rows = client.list("akt-api/balances", all_pages=True, params=params or None)
    return reports.balances_by_id(rows), accts_by_id, accounts


def _fetch_type_class(client: Client) -> "dict[int, int]":
    """type_id -> class_id, pulled from the installation (akt-api/account-types).
    Empty when the module lacks the endpoint — reports fall back to the seeds."""
    try:
        rows = client.list("akt-api/account-types")
    except ApiError:
        return {}
    out: dict[int, int] = {}
    for r in rows:
        try:
            out[int(r["type_id"])] = int(r["class_id"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _convert_amount(client: Client, amount: float, code: str, rate: float) -> "float | None":
    """Base-currency value of ``amount`` in ``code`` at ``rate`` (= amount / rate,
    Akaunting's convertToDefault). Prefer the installation's own trait via the
    akt-api convert endpoint so the number mirrors Akaunting exactly; fall back to
    local arithmetic when the module (or that endpoint) isn't present."""
    if client.has_ledger_api():
        try:
            r = client.request("GET", "akt-api/convert", params={
                "amount": amount, "currency_code": code, "currency_rate": rate})
            data = r.get("data", r) if isinstance(r, dict) else r
            if isinstance(data, dict) and data.get("base") is not None:
                return float(data["base"])
        except ApiError:
            pass  # older akt-api without /convert — fall back
    return round(amount / rate, 2) if rate else None


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
        # convert=1 makes akt-api annotate each row with its source currency and the
        # base-currency amount, so we show foreign legs AS POSTED plus an unambiguous
        # converted column (rather than mislabelling foreign face values as base).
        params: dict = {"account_id": account_id, "convert": 1}
        if ns.date_from:
            params["issued_from"] = ns.date_from
        if ns.date_to:
            params["issued_to"] = ns.date_to
        rows = client.list("akt-api/ledgers", params=params, all_pages=True)
        cols = ["issued_at", "currency_code", "debit", "credit",
                "debit_converted", "credit_converted", "entry_type"]
        emit(rows, as_json=ns.json, columns=None if ns.json else cols,
             headers=["Date", "Cur", "Debit", "Credit", "Debit(base)", "Credit(base)", "Type"])
        return 0
    if name == "verify":
        coa = ns._coa
        if not client.has_ledger_api():
            raise ValueError("`akt verify` needs the akt-api companion module — "
                             "install it into your Akaunting modules/ directory (see akt-api/README.md)")

        txns = [t for t in client.list("transactions", all_pages=True)
                if t.get("type") in ("income", "expense") and not t.get("document_id")]
        if ns.date_from:
            txns = [t for t in txns if str(t.get("paid_at", ""))[:10] >= ns.date_from]
        if ns.date_to:
            txns = [t for t in txns if str(t.get("paid_at", ""))[:10] <= ns.date_to]

        accounts = client.list("chart-of-accounts", all_pages=True)
        accounts_by_id = {a["id"]: {"code": a.get("code"), "name": a.get("name"),
                                    "type_id": a.get("type_id")} for a in accounts}
        item_ledgers = client.list("akt-api/ledgers", all_pages=True, params={
            "ledgerable_type": "App\\Models\\Banking\\Transaction", "entry_type": "item"})
        item_account_by_txn = {int(l["ledgerable_id"]): int(l["account_id"]) for l in item_ledgers}
        banks_by_id = {b["id"]: {"name": b.get("name")}
                       for b in client.list("accounts", all_pages=True)}

        # COA-independent ledger-health checks (always run).
        findings = find_unposted(txns, item_account_by_txn, banks_by_id)
        rep = client.get("akt-api/banks/unmapped")
        for b in (rep.get("data", rep) if isinstance(rep, dict) else rep):
            findings.append({
                "transaction_id": None, "paid_at": None, "amount": None,
                "bank": b.get("name"), "category": None,
                "expected_code": None, "actual_code": None,
                "reason": "bank has no Double-Entry ledger mapping — run "
                          "DoubleEntry's CopyData install job",
            })

        # COA-dependent coding checks (only when a COA config is present).
        if coa is not None:
            categories_by_id = {c["id"]: {"name": c.get("name"), "type": c.get("type")}
                                for c in client.list("categories", all_pages=True)}
            accounts_by_code = {int(a["code"]): a["id"] for a in accounts if a.get("code") is not None}
            findings += find_miscodings(txns, categories_by_id, accounts_by_id,
                                        accounts_by_code, item_account_by_txn, coa)
            findings += find_report_dropped(txns, item_account_by_txn, accounts_by_id,
                                            _fetch_type_class(client))

        for f in findings:
            f.setdefault("bank", None)
        cols = ["transaction_id", "paid_at", "amount", "bank", "category",
                "expected_code", "actual_code", "reason"]
        emit(findings, as_json=ns.json, columns=None if ns.json else cols,
             headers=["Txn", "Date", "Amount", "Bank", "Category", "Expected", "Actual", "Reason"])
        return 0 if not findings else 1
    if name == "recode":
        coa = ns._coa
        if coa is None:
            raise ValueError("`akt recode` needs a COA config (use --coa FILE or set AKT_COA_FILE)")
        if not client.has_ledger_api():
            raise ValueError("`akt recode` needs the akt-api companion module — "
                             "install it into your Akaunting modules/ directory (see akt-api/README.md)")

        txns = [t for t in client.list("transactions", all_pages=True)
                if t.get("type") in ("income", "expense") and not t.get("document_id")]
        if ns.date_from:
            txns = [t for t in txns if str(t.get("paid_at", ""))[:10] >= ns.date_from]
        if ns.date_to:
            txns = [t for t in txns if str(t.get("paid_at", ""))[:10] <= ns.date_to]
        category_by_txn = {int(t["id"]): t.get("category_id") for t in txns}

        categories_by_id = {c["id"]: {"name": c.get("name"), "type": c.get("type")}
                            for c in client.list("categories", all_pages=True)}
        accounts = client.list("chart-of-accounts", all_pages=True)
        accounts_by_id = {a["id"]: {"code": a.get("code"), "name": a.get("name")} for a in accounts}
        accounts_by_code = {int(a["code"]): a["id"] for a in accounts if a.get("code") is not None}
        item_ledgers = [{"id": l["id"], "ledgerable_id": int(l["ledgerable_id"]),
                         "account_id": int(l["account_id"])}
                        for l in client.list("akt-api/ledgers", all_pages=True, params={
                            "ledgerable_type": "App\\Models\\Banking\\Transaction",
                            "entry_type": "item"})]

        plan = build_recode_plan(item_ledgers, category_by_txn, categories_by_id,
                                 accounts_by_id, accounts_by_code, coa)
        cols = ["transaction_id", "category", "from_code", "to_code", "ledger_id"]
        if ns.dry_run or not plan:
            emit(plan, as_json=ns.json, columns=None if ns.json else cols,
                 headers=["Txn", "Category", "From", "To", "LedgerRow"])
            if not ns.json:
                print(f"\n{len(plan)} transaction(s) would be recoded (dry-run)."
                      if plan else "nothing to recode — all coded correctly.")
            return 0
        done = 0
        for p in plan:
            client.request("PATCH", f"akt-api/ledgers/{p['ledger_id']}",
                           json_body={"account_id": p["to_account_id"]})
            done += 1
        print(f"recoded {done} transaction(s).")
        return 0
    if name == "balance":
        _need_akt_api(client, "balance")
        balances, accts_by_id, accounts = _fetch_balances(client, ns)
        account_id = resolve_account_id(accounts, ns.account)
        bal = balances.get(account_id, 0.0)
        if ns.expected is not None:
            r = reports.reconcile(bal, ns.expected)
            emit(r, as_json=ns.json,
                 columns=None if ns.json else ["actual", "expected", "diff", "ok"],
                 headers=["Balance", "Expected", "Diff", "OK"])
            return 0 if r["ok"] else 1
        emit({"account_id": account_id, "balance": round(bal, 2)}, as_json=ns.json,
             columns=None if ns.json else ["account_id", "balance"],
             headers=["Account", "Balance"])
        return 0
    if name == "trial_balance":
        _need_akt_api(client, "trial-balance")
        balances, accts_by_id, _ = _fetch_balances(client, ns)
        tb = reports.build_trial_balance(balances, accts_by_id)
        if ns.json:
            emit(tb, as_json=True)
        else:
            emit(tb["rows"], as_json=False, columns=["code", "name", "debit", "credit"],
                 headers=["Code", "Account", "Debit", "Credit"])
            print(f"  {'TOTAL':>34}  {tb['total_debit']:>12,.2f}  {tb['total_credit']:>12,.2f}")
            print("  BALANCED" if tb["balanced"] else "  *** OUT OF BALANCE")
        return 0 if tb["balanced"] else 1
    if name == "report_pnl":
        _need_akt_api(client, "report profit-loss")
        balances, accts_by_id, _ = _fetch_balances(client, ns)
        pl = reports.build_profit_loss(balances, accts_by_id, _fetch_type_class(client))
        if ns.json:
            emit(pl, as_json=True)
        else:
            print("INCOME")
            emit(pl["income"], as_json=False, columns=["code", "name", "amount"], headers=["Code", "Account", "Amount"])
            print(f"  {'Total income':>40}  {pl['total_income']:>12,.2f}\n\nEXPENSES")
            emit(pl["expense"], as_json=False, columns=["code", "name", "amount"], headers=["Code", "Account", "Amount"])
            print(f"  {'Total expenses':>40}  {pl['total_expense']:>12,.2f}")
            print(f"  {'NET PROFIT':>40}  {pl['net_profit']:>12,.2f}")
        return 0
    if name == "report_bs":
        _need_akt_api(client, "report balance-sheet")
        balances, accts_by_id, _ = _fetch_balances(client, ns)
        bs = reports.build_balance_sheet(balances, accts_by_id, _fetch_type_class(client))
        if ns.json:
            emit(bs, as_json=True)
        else:
            for title, key, tot in (("ASSETS", "assets", "total_assets"),
                                    ("LIABILITIES", "liabilities", "total_liabilities"),
                                    ("EQUITY", "equity", "total_equity")):
                print(title)
                emit(bs[key], as_json=False, columns=["code", "name", "amount"], headers=["Code", "Account", "Amount"])
                print(f"  {'Total ' + title.lower():>40}  {bs[tot]:>12,.2f}\n")
            print(f"  {'Net income to date':>40}  {bs['net_income']:>12,.2f}")
            print("  BALANCED" if bs["balanced"] else "  *** DOES NOT BALANCE")
        return 0 if bs["balanced"] else 1
    if name == "fx":
        from . import fx as _fx
        # Only consult the installation for the base currency when --to is absent.
        base = (ns.to or client.setting("default.currency", "USD") or "USD").upper()
        code = ns.code.upper()
        on = _dt.date.fromisoformat(ns.on) if ns.on else None
        rate = _fx.resolve_rate(base, code, on,
                                ars_casa=ns.ars_casa or "bolsa",
                                ars_side=ns.ars_side or "mid",
                                cache_dir=str(_fx.default_cache_dir()))
        out: dict[str, Any] = {
            "code": code, "base": base, "on": ns.on or "latest",
            "rate": float(rate),                       # CODE per 1 base (== currency_rate)
            "inverse": round(1 / float(rate), 6) if rate else None,  # base per 1 CODE
        }
        cols = ["code", "base", "on", "rate", "inverse"]
        heads = ["Currency", "Base", "Date", "Rate", "Inverse"]
        if ns.amount is not None:
            out["amount"] = ns.amount
            out["amount_base"] = _convert_amount(client, ns.amount, code, float(rate))
            cols += ["amount", "amount_base"]
            heads += ["Amount", f"In {base}"]
        # Wrap in a list so the built columns/headers apply — emit renders a bare
        # dict as a generic field/value dump, ignoring columns.
        if ns.json:
            emit(out, as_json=True)
        else:
            emit([out], as_json=False, columns=cols, headers=heads)
        return 0
    if name == "gc_ledger":
        _need_akt_api(client, "gc-ledger")
        rep = client.get("akt-api/ledgers/orphans")
        data = rep.get("data", rep) if isinstance(rep, dict) else rep
        total = data.get("total", 0)
        if not ns.apply:
            if ns.json:
                emit(data, as_json=True)
            else:
                for t, c in (data.get("by_type") or {}).items():
                    print(f"  {c:>6}  {t}")
                print(f"{total} orphaned ledger row(s). Run with --apply to delete.")
            return 0
        res = client.request("DELETE", "akt-api/ledgers/orphans")
        d = res.get("data", res) if isinstance(res, dict) else res
        print(f"deleted {d.get('deleted', 0)} orphaned ledger row(s).")
        return 0
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
