"""Config-driven chart-of-accounts <-> category linking.

akt defines a small COA config schema. When present, it lets `coa sync`
reconcile the double-entry chart of accounts and a 1:1 mirror of Akaunting
categories, and lets `payment create` code by --account with the mirror
category filled in automatically. The feature is inert when no config is found.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# DoubleEntry standard seeds: account type_id -> Akaunting category type.
# Income classes -> "income", Expense classes -> "expense", everything else
# (assets/liabilities/equity) -> "other".
TYPE_ID_CATEGORY_TYPE: dict[int, str] = {
    13: "income", 14: "income", 15: "income",          # revenue, sales, other_income
    11: "expense", 12: "expense",                       # direct_costs, expense
    1: "other", 2: "other", 3: "other", 4: "other",     # asset types
    5: "other", 6: "other", 10: "other",                # prepayment, bank_cash, depreciation
    7: "other", 8: "other", 9: "other", 17: "other",    # liability types + tax
    16: "other",                                        # equity
}


@dataclass(frozen=True)
class CoaAccount:
    code: int
    name: str
    type_id: int
    category: str        # mirror category name (== name unless overridden)
    category_type: str   # "income" | "expense" | "other"
    mirror: bool         # whether to create/keep a mirror category


@dataclass
class CoaConfig:
    accounts: list[CoaAccount] = field(default_factory=list)

    def by_code(self, code: int) -> CoaAccount | None:
        for a in self.accounts:
            if a.code == int(code):
                return a
        return None

    def by_name(self, name: str) -> CoaAccount | None:
        for a in self.accounts:
            if a.name == name:
                return a
        return None

    def by_category(self, category: str, category_type: str | None = None) -> CoaAccount | None:
        """Reverse of by_name: the mirrored account whose mirror category matches
        (name, and type when given). Non-mirrored accounts have no category."""
        for a in self.accounts:
            if a.mirror and a.category == category and (
                category_type is None or a.category_type == category_type
            ):
                return a
        return None

    @property
    def mirrored(self) -> list[CoaAccount]:
        return [a for a in self.accounts if a.mirror]


def parse_coa(text: str) -> CoaConfig:
    doc = tomllib.loads(text)
    raw_accounts = doc.get("account", [])
    accounts: list[CoaAccount] = []
    seen_codes: set[int] = set()
    seen_categories: set[str] = set()
    for i, row in enumerate(raw_accounts):
        if "code" not in row:
            raise ValueError(f"account #{i + 1}: missing required 'code'")
        if "name" not in row:
            raise ValueError(f"account #{i + 1} (code {row['code']}): missing required 'name'")
        if "type_id" not in row:
            raise ValueError(f"account '{row['name']}': missing required 'type_id'")
        code = int(row["code"])
        name = str(row["name"])
        type_id = int(row["type_id"])
        mirror = row.get("mirror", True)
        if not isinstance(mirror, bool):
            raise ValueError(
                f"account '{name}': 'mirror' must be a boolean (true/false), "
                f"got {mirror!r} — did you quote it?")
        category = str(row.get("category", name))
        if code in seen_codes:
            raise ValueError(f"duplicate code {code}")
        seen_codes.add(code)
        if type_id not in TYPE_ID_CATEGORY_TYPE:
            print(f"warning: unknown type_id {type_id} for account '{name}'; "
                  f"mirror category type defaulted to 'other'", file=sys.stderr)
        category_type = TYPE_ID_CATEGORY_TYPE.get(type_id, "other")
        if mirror:
            if category in seen_categories:
                raise ValueError(f"duplicate category name {category!r} "
                                 f"(mirror category names must be unique)")
            seen_categories.add(category)
        accounts.append(CoaAccount(code=code, name=name, type_id=type_id,
                                   category=category, category_type=category_type,
                                   mirror=mirror))
    return CoaConfig(accounts=accounts)


def _coa_candidate_files(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    files: list[Path] = []
    if os.environ.get("AKT_COA_FILE"):
        files.append(Path(os.environ["AKT_COA_FILE"]).expanduser())
    files.append(Path.cwd() / "coa.toml")
    files.append(Path.home() / ".config" / "akt" / "coa.toml")
    return files


def load_coa(path: str | None = None) -> CoaConfig | None:
    """Find and parse the COA config; return None if none is present.

    An explicitly-passed ``path`` that does not exist is an error (the user
    asked for a specific file); discovered defaults simply fall through."""
    if path and not Path(path).expanduser().is_file():
        raise ValueError(f"COA config not found: {path}")
    for f in _coa_candidate_files(path):
        if f.is_file():
            return parse_coa(f.read_text())
    return None


@dataclass
class CoaPlan:
    accounts_create: list[CoaAccount] = field(default_factory=list)
    accounts_rename: list[tuple[CoaAccount, dict]] = field(default_factory=list)
    categories_create: list[CoaAccount] = field(default_factory=list)
    categories_rename: list[tuple[CoaAccount, dict]] = field(default_factory=list)
    accounts_disable: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any([self.accounts_create, self.accounts_rename,
                        self.categories_create, self.categories_rename,
                        self.accounts_disable])


def plan_sync(config: CoaConfig, live_accounts: list[dict],
              live_categories: list[dict], *, prune: bool) -> CoaPlan:
    """Diff the config against live Akaunting data. Pure: no I/O."""
    plan = CoaPlan()
    live_by_code = {int(a["code"]): a for a in live_accounts if a.get("code") is not None}
    # (name, type) -> live category, so a same-named category of another type
    # never gets clobbered.
    live_cat = {(c.get("name"), c.get("type")): c for c in live_categories}

    for acct in config.accounts:
        live = live_by_code.get(acct.code)
        if live is None:
            plan.accounts_create.append(acct)
        elif str(live.get("name")) != acct.name:
            plan.accounts_rename.append((acct, live))

    # A renamed mirrored account's mirror category currently lives under the account's
    # OLD name; rename it instead of creating a fresh one (which would orphan the old).
    rename_targets = {}   # (new category, type) -> (acct, live_old_category)
    for acct, live in plan.accounts_rename:
        if not acct.mirror:
            continue
        old = live_cat.get((str(live.get("name")), acct.category_type))
        if old is not None and old.get("name") != acct.category:
            rename_targets[(acct.category, acct.category_type)] = (acct, old)

    # Mirror-category names are unique by construction (parse_coa rejects
    # duplicate category names among mirrored accounts), so these loops can
    # never enqueue the same categories_create/categories_rename entry twice.
    for acct in config.mirrored:
        key = (acct.category, acct.category_type)
        if live_cat.get(key) is not None:
            continue                      # already correct
        if key in rename_targets:
            a, old = rename_targets[key]
            plan.categories_rename.append((a, old))   # rename old -> a.category
        else:
            plan.categories_create.append(acct)

    if prune:
        config_codes = {a.code for a in config.accounts}
        for live in live_accounts:
            code = live.get("code")
            if code is None:
                continue
            if int(code) not in config_codes and int(live.get("enabled", 1)) == 1:
                plan.accounts_disable.append(live)
    return plan


def render_plan(plan: CoaPlan) -> list[str]:
    lines: list[str] = []
    for a in plan.accounts_create:
        lines.append(f"create account {a.code} {a.name} (type_id {a.type_id})")
    for a, live in plan.accounts_rename:
        lines.append(f"rename account {a.code}: {live.get('name')!r} -> {a.name!r}")
    for a in plan.categories_create:
        lines.append(f"create category {a.category!r} [{a.category_type}]")
    for a, live in plan.categories_rename:
        lines.append(f"rename category {live.get('name')!r} -> {a.category!r}")
    for live in plan.accounts_disable:
        lines.append(f"disable account {live.get('code')} {live.get('name')!r}")
    if not lines:
        lines.append("in sync — nothing to do")
    return lines


def _account_form(acct: CoaAccount) -> list[tuple[str, str]]:
    """The web-CRUD form body for a chart-of-accounts create/update."""
    from .resources import flatten_form  # local import: avoids resources<->coa cycle
    return flatten_form({
        "code": acct.code,
        "name": acct.name,
        "type_id": acct.type_id,
        "enabled": 1,
        "is_sub_account": "false",
    })


def apply_plan(client, plan: "CoaPlan", *, prune: bool) -> dict:
    """Execute the plan against Akaunting. Accounts use the web CRUD route
    (chart-of-accounts is read-only on /api); categories use /api."""
    summary = {"accounts_created": 0, "accounts_renamed": 0,
               "categories_created": 0, "categories_renamed": 0,
               "accounts_disabled": 0}

    for acct in plan.accounts_create:
        client.web_json("POST", "double-entry/chart-of-accounts", _account_form(acct))
        summary["accounts_created"] += 1

    for acct, live in plan.accounts_rename:
        client.web_json("PATCH", f"double-entry/chart-of-accounts/{live['id']}",
                        _account_form(acct))
        summary["accounts_renamed"] += 1

    for acct in plan.categories_create:
        client.post("categories", {
            "name": acct.category,
            "type": acct.category_type,
            "color": "#00bcd4",
            "enabled": 1,
        })
        summary["categories_created"] += 1

    for acct, live in plan.categories_rename:
        client.put(f"categories/{live['id']}", {
            "name": acct.category,
            "type": acct.category_type,
            "color": live.get("color") or "#00bcd4",
            "enabled": 1,
        })
        summary["categories_renamed"] += 1

    if prune:
        for live in plan.accounts_disable:
            client.web_json("GET", f"double-entry/chart-of-accounts/{live['id']}/disable")
            summary["accounts_disabled"] += 1

    return summary


def _find_account(config: CoaConfig, ref: str) -> CoaAccount:
    acct = config.by_code(int(ref)) if str(ref).lstrip("-").isdigit() else config.by_name(ref)
    if acct is None:
        raise ValueError(f"account {ref!r} is not in the COA config")
    return acct


def resolve_coding(config: CoaConfig, client, *, account_ref: str | None = None,
                   category_ref: str | None = None) -> tuple[int, int]:
    """Resolve --account OR --category to live (de_account_id, category_id).

    Pass exactly one ref. Both directions require the account to be mirrored and
    both the account and its mirror category to already exist (run `coa sync`)."""
    if (account_ref is None) == (category_ref is None):
        raise ValueError("resolve_coding needs exactly one of account_ref / category_ref")

    if account_ref is not None:
        acct = _find_account(config, account_ref)
    else:
        acct = next((a for a in config.accounts if a.mirror and a.category == category_ref), None)
        if acct is None:
            raise ValueError(f"category {category_ref!r} has no mirrored account in the COA config")

    if not acct.mirror:
        raise ValueError(
            f"account {acct.code} ({acct.name}) has mirror=false — it has no mirror "
            f"category, so it can't be coded onto a payment via --account "
            f"(only mirrored income/expense accounts can be)")

    live_accounts = client.list("chart-of-accounts", all_pages=True)
    de_id = next((a["id"] for a in live_accounts if int(a.get("code", -1)) == acct.code), None)
    if de_id is None:
        raise ValueError(f"account {acct.code} ({acct.name}) is not in Akaunting yet — "
                         f"run `akt coa sync` first")
    live_categories = client.list("categories", all_pages=True)
    cat_id = next((c["id"] for c in live_categories
                   if c.get("name") == acct.category and c.get("type") == acct.category_type),
                  None)
    if cat_id is None:
        raise ValueError(f"mirror category {acct.category!r} [{acct.category_type}] is not in "
                         f"Akaunting yet — run `akt coa sync` first")
    return int(de_id), int(cat_id)
