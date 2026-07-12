"""Config-driven chart-of-accounts <-> category linking.

akt defines a small COA config schema. When present, it lets `coa sync`
reconcile the double-entry chart of accounts and a 1:1 mirror of Akaunting
categories, and lets `payment create` code by account/category with the other
side filled in automatically. The feature is inert when no config is found.
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

    def by_category(self, name: str) -> CoaAccount | None:
        for a in self.accounts:
            if a.mirror and a.category == name:
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
        mirror = bool(row.get("mirror", True))
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
    # Always empty by design: categories are matched by (name, type) in
    # plan_sync, so a match means there is nothing to rename.
    categories_rename: list[tuple[CoaAccount, dict]] = field(default_factory=list)
    accounts_disable: list[dict] = field(default_factory=list)
    categories_disable: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any([self.accounts_create, self.accounts_rename,
                        self.categories_create, self.categories_rename,
                        self.accounts_disable, self.categories_disable])


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

    # Mirror-category names are unique by construction (parse_coa rejects
    # duplicate category names among mirrored accounts), so this loop can
    # never enqueue the same categories_create entry twice.
    for acct in config.mirrored:
        live = live_cat.get((acct.category, acct.category_type))
        if live is None:
            plan.categories_create.append(acct)
        # name+type already matched -> nothing to rename (name is the join key).
        # categories_rename therefore stays empty by design: a live category is
        # only ever matched by (name, type), so a match means there's nothing
        # to rename — see CoaPlan.categories_rename below.

    if prune:
        config_codes = {a.code for a in config.accounts}
        for live in live_accounts:
            code = live.get("code")
            if code is None:
                continue
            if int(code) not in config_codes and int(live.get("enabled", 1)) == 1:
                plan.accounts_disable.append(live)
        wanted_cats = {(a.category, a.category_type) for a in config.mirrored}
        for live in live_categories:
            key = (live.get("name"), live.get("type"))
            if key not in wanted_cats and int(live.get("enabled", 1)) == 1:
                plan.categories_disable.append(live)
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
    for live in plan.categories_disable:
        lines.append(f"disable category {live.get('name')!r}")
    if not lines:
        lines.append("in sync — nothing to do")
    return lines
