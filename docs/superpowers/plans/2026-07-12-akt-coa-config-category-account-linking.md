# COA Config-Driven Category ↔ Account Linking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-driven feature to akt so a chart-of-accounts config keeps GL accounts and their 1:1 mirror categories in sync, and `payment create` can be coded by account or by category with the other side filled automatically.

**Architecture:** A new `akt.coa` module parses an akt-defined COA config (TOML), derives a 1:1 category mirror (category type from a built-in DoubleEntry `type_id → class` table), and computes a create/rename/disable plan against live Akaunting data. New `akt coa diff`/`coa sync` commands apply it via existing primitives (`chart-of-accounts` web CRUD; `categories` /api CRUD). `build_payment_create` gains optional `--account`/`--category` flags that resolve through the loaded config to set `de_account_id` + `category_id` together. The feature is inert when no config is present.

**Tech Stack:** Python 3.12, argparse, `tomllib` (stdlib), `requests` (existing client), pytest (unit + gated live integration).

## Global Constraints

- Feature is **config-gated**: with no COA config found, all existing behavior is byte-for-byte unchanged.
- akt defines the schema; consumer files may be **supersets** (extra keys ignored).
- Config discovery precedence: `--coa <file>` → `AKT_COA_FILE` → `./coa.toml` → `~/.config/akt/coa.toml`.
- `type_id → category type` (built-in, DoubleEntry standard seeds): income = `{13, 14, 15}`; expense = `{11, 12}`; every other known type_id `{1,2,3,4,5,6,7,8,9,10,16,17}` → `other`; unknown type_id → `other` + a stderr warning.
- Category mirror **join key is the account name** (or per-account `category` override); mirror category type is the derived category type. `code` and mirror-category `name` must be unique — validated on load.
- `coa sync` = **create + rename only**; `--prune` opt-in **disables** (never deletes). `--prune` disables via the toggle routes.
- `payment create` with neither `--account` nor `--category` → unchanged legacy behavior. When a coa flag is used, an explicit `--set de_account_id=` / `--category-id` still wins (applied after).
- `--account` accepts a GL **code (numeric) or name**; codes are numeric and names are not, so they disambiguate by shape.
- Version bump to **0.4.0** (feature release).
- Follow existing patterns: declarative `Resource`/builders return dicts; unit tests use fake clients; live tests register every created record with the `tracker` fixture for teardown.

---

## File Structure

- **Create `src/akt/coa.py`** — config schema (`CoaAccount`, `CoaConfig`), TOML parse + validation + category-type derivation, discovery/loader (`load_coa`), sync planning (`plan_sync`, `CoaPlan`), sync apply (`apply_plan`), plan rendering (`render_plan`), and `payment create` coding resolution (`resolve_coding`). One responsibility: everything about the COA config and its reconciliation.
- **Create `tests/unit/test_coa.py`** — unit tests for all of `coa.py` (with a local fake client).
- **Modify `src/akt/config.py`** — add `coa_candidate_files()` (kept here beside credential discovery for symmetry) — *optional*: the plan places discovery in `coa.py` to keep the module self-contained; `config.py` is left unchanged. (See Task 1.)
- **Modify `src/akt/cli.py`** — top-level `--coa` flag; load config into `ns._coa` in `main`; `coa` subcommand group (`diff`/`sync`) wired as specials; `_run_special` handlers.
- **Modify `src/akt/registry.py`** — add `--account` / `--category` fields to `PAYMENT`.
- **Modify `src/akt/resources.py`** — `build_payment_create` consults `ns._coa` via `resolve_coding`.
- **Modify `tests/integration/test_live.py`** — live `coa sync` round-trip + `payment create --account` coding assertion.
- **Modify `pyproject.toml`** — version `0.3.0` → `0.4.0`.
- **Modify `README.md`** — document the COA config, `coa diff/sync`, and coded `payment create`.

---

## Task 1: COA config module — schema, parse, validate, load

**Files:**
- Create: `src/akt/coa.py`
- Test: `tests/unit/test_coa.py`

**Interfaces:**
- Produces:
  - `TYPE_ID_CATEGORY_TYPE: dict[int, str]`
  - `class CoaAccount` (frozen dataclass): `code: int`, `name: str`, `type_id: int`, `category: str`, `category_type: str`, `mirror: bool`
  - `class CoaConfig`: `.accounts: list[CoaAccount]`; methods `by_code(code: int) -> CoaAccount | None`, `by_name(name: str) -> CoaAccount | None`, `by_category(name: str) -> CoaAccount | None`; property `mirrored: list[CoaAccount]`
  - `parse_coa(text: str) -> CoaConfig`
  - `load_coa(path: str | None = None) -> CoaConfig | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_coa.py
"""Unit tests for the COA config module (parse, validate, derive, load)."""

from __future__ import annotations

import pytest

from akt.coa import CoaAccount, CoaConfig, parse_coa, load_coa

pytestmark = pytest.mark.unit

_MINIMAL = """
[[account]]
code = 400
name = "API Subscription Revenue"
type_id = 13

[[account]]
code = 628
name = "Other / Uncategorized"
type_id = 12

[[account]]
code = 310
name = "Owners Draw"
type_id = 16

[[account]]
code = 850
name = "BofA Checking"
type_id = 6
mirror = false
"""


def test_parse_derives_category_type_from_type_id():
    cfg = parse_coa(_MINIMAL)
    assert cfg.by_code(400).category_type == "income"   # revenue (13)
    assert cfg.by_code(628).category_type == "expense"  # expense (12)
    assert cfg.by_code(310).category_type == "other"    # equity (16)


def test_parse_default_mirror_category_is_account_name():
    cfg = parse_coa(_MINIMAL)
    assert cfg.by_code(400).category == "API Subscription Revenue"
    assert cfg.by_code(400).mirror is True


def test_parse_mirror_false_excluded_from_mirrored():
    cfg = parse_coa(_MINIMAL)
    codes = {a.code for a in cfg.mirrored}
    assert 850 not in codes          # mirror = false
    assert {400, 628, 310} <= codes


def test_parse_category_override():
    cfg = parse_coa("""
[[account]]
code = 400
name = "API Subscription Revenue"
type_id = 13
category = "Revenue"
""")
    assert cfg.by_code(400).category == "Revenue"
    assert cfg.by_category("Revenue").code == 400


def test_parse_superset_fields_ignored():
    cfg = parse_coa("""
[[account]]
code = 510
name = "Market Data Feeds"
type_id = 11
status = "new"
vendors = ["intrinio"]
note = "ignored by akt"
""")
    assert cfg.by_code(510).name == "Market Data Feeds"


def test_parse_rejects_duplicate_code():
    with pytest.raises(ValueError, match="duplicate code"):
        parse_coa("""
[[account]]
code = 400
name = "A"
type_id = 13
[[account]]
code = 400
name = "B"
type_id = 12
""")


def test_parse_rejects_duplicate_category_name():
    with pytest.raises(ValueError, match="duplicate category"):
        parse_coa("""
[[account]]
code = 400
name = "Same"
type_id = 13
[[account]]
code = 401
name = "Same"
type_id = 12
""")


def test_parse_requires_code_name_type_id():
    with pytest.raises(ValueError, match="code"):
        parse_coa('[[account]]\nname = "X"\ntype_id = 12\n')
    with pytest.raises(ValueError, match="name"):
        parse_coa('[[account]]\ncode = 400\ntype_id = 12\n')
    with pytest.raises(ValueError, match="type_id"):
        parse_coa('[[account]]\ncode = 400\nname = "X"\n')


def test_parse_unknown_type_id_defaults_other_with_warning(capsys):
    cfg = parse_coa('[[account]]\ncode = 900\nname = "Mystery"\ntype_id = 99\n')
    assert cfg.by_code(900).category_type == "other"
    assert "type_id 99" in capsys.readouterr().err


def test_load_coa_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AKT_COA_FILE", raising=False)
    monkeypatch.chdir(tmp_path)                 # no ./coa.toml
    monkeypatch.setenv("HOME", str(tmp_path))   # no ~/.config/akt/coa.toml
    assert load_coa() is None


def test_load_coa_reads_explicit_path(tmp_path):
    p = tmp_path / "chart.toml"
    p.write_text(_MINIMAL)
    cfg = load_coa(str(p))
    assert cfg is not None and cfg.by_code(400).name == "API Subscription Revenue"


def test_load_coa_explicit_missing_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_coa(str(tmp_path / "nope.toml"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'akt.coa'`

- [ ] **Step 3: Write the module**

```python
# src/akt/coa.py
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
    files: list[Path] = []
    if explicit:
        files.append(Path(explicit).expanduser())
    elif os.environ.get("AKT_COA_FILE"):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/akt/coa.py tests/unit/test_coa.py
git commit -m "feat(coa): COA config schema, parse/validate, and loader"
```

---

## Task 2: `--coa` flag + load config into the namespace

**Files:**
- Modify: `src/akt/cli.py` (`_build_parser` top-level args; `main`)
- Test: `tests/unit/test_coa.py` (append)

**Interfaces:**
- Consumes: `load_coa` (Task 1).
- Produces: `ns.coa_file` (parsed value of `--coa`); `ns._coa` (a `CoaConfig | None`) set in `main` before dispatch, readable by any handler/builder.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_coa.py
from akt.cli import _build_parser


def test_coa_flag_parses_to_coa_file():
    ns = _build_parser().parse_args(["--coa", "/tmp/chart.toml", "payment", "list"])
    assert ns.coa_file == "/tmp/chart.toml"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_coa.py::test_coa_flag_parses_to_coa_file -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'coa_file'`

- [ ] **Step 3: Add the flag and loader wiring**

In `src/akt/cli.py`, add to the top-level `parser` (next to `--throttle`, after line 86):

```python
    parser.add_argument("--coa", dest="coa_file", default=None, metavar="FILE",
                        help="COA config for category<->account linking "
                             "(or AKT_COA_FILE / ./coa.toml / ~/.config/akt/coa.toml)")
```

Add the import near the other module imports (after line 23):

```python
from .coa import load_coa
```

In `main`, immediately after `client = Client(config, throttle=throttle)` (line 217), before the dispatch `try`:

```python
    ns._coa = load_coa(getattr(ns, "coa_file", None))
```

Move this inside a small guard so a bad config surfaces cleanly — replace the line above with:

```python
    try:
        ns._coa = load_coa(getattr(ns, "coa_file", None))
    except (ValueError, OSError) as e:
        print(f"coa config error: {e}", file=sys.stderr)
        return 2
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: PASS

- [ ] **Step 5: Run the full unit suite (no regressions)**

Run: `uv run pytest tests/unit -q`
Expected: PASS (all existing tests still green)

- [ ] **Step 6: Commit**

```bash
git add src/akt/cli.py tests/unit/test_coa.py
git commit -m "feat(coa): --coa flag; load config into ns._coa"
```

---

## Task 3: `coa diff` — sync planning + read-only command

**Files:**
- Modify: `src/akt/coa.py` (add `CoaPlan`, `plan_sync`, `render_plan`)
- Modify: `src/akt/cli.py` (`coa` subparser; `_run_special` handler for `coa_diff`)
- Test: `tests/unit/test_coa.py` (append)

**Interfaces:**
- Consumes: `CoaConfig`, `CoaAccount` (Task 1); `Client.list` (existing).
- Produces:
  - `class CoaPlan` (dataclass): `accounts_create: list[CoaAccount]`, `accounts_rename: list[tuple[CoaAccount, dict]]`, `categories_create: list[CoaAccount]`, `categories_rename: list[tuple[CoaAccount, dict]]`, `accounts_disable: list[dict]`, `categories_disable: list[dict]`
  - `plan_sync(config: CoaConfig, live_accounts: list[dict], live_categories: list[dict], *, prune: bool) -> CoaPlan`
  - `render_plan(plan: CoaPlan) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_coa.py
from akt.coa import CoaPlan, plan_sync, render_plan


def _cfg():
    return parse_coa(_MINIMAL)


def test_plan_creates_missing_accounts_and_categories():
    # live has only code 400 (as seeded name "Sales"); nothing else exists
    live_accounts = [{"id": 1, "code": 400, "name": "Sales", "type_id": 13, "enabled": 1}]
    live_categories = []
    plan = plan_sync(_cfg(), live_accounts, live_categories, prune=False)
    create_codes = {a.code for a in plan.accounts_create}
    assert create_codes == {628, 310, 850}          # 400 exists, others missing
    rename = {a.code for a, _ in plan.accounts_rename}
    assert rename == {400}                            # "Sales" -> "API Subscription Revenue"
    cat_create = {a.category for a in plan.categories_create}
    assert cat_create == {"API Subscription Revenue", "Other / Uncategorized", "Owners Draw"}
    assert all(a.code != 850 for a in plan.categories_create)   # mirror=false


def test_plan_no_op_when_in_sync():
    live_accounts = [
        {"id": 1, "code": 400, "name": "API Subscription Revenue", "type_id": 13, "enabled": 1},
        {"id": 2, "code": 628, "name": "Other / Uncategorized", "type_id": 12, "enabled": 1},
        {"id": 3, "code": 310, "name": "Owners Draw", "type_id": 16, "enabled": 1},
        {"id": 4, "code": 850, "name": "BofA Checking", "type_id": 6, "enabled": 1},
    ]
    live_categories = [
        {"id": 10, "name": "API Subscription Revenue", "type": "income", "enabled": 1},
        {"id": 11, "name": "Other / Uncategorized", "type": "expense", "enabled": 1},
        {"id": 12, "name": "Owners Draw", "type": "other", "enabled": 1},
    ]
    plan = plan_sync(_cfg(), live_accounts, live_categories, prune=False)
    assert not any([plan.accounts_create, plan.accounts_rename,
                    plan.categories_create, plan.categories_rename])


def test_plan_prune_disables_extras_only_when_requested():
    live_accounts = [{"id": 9, "code": 999, "name": "Legacy", "type_id": 12, "enabled": 1}]
    live_categories = [{"id": 20, "name": "Legacy Cat", "type": "expense", "enabled": 1}]
    no_prune = plan_sync(_cfg(), live_accounts, live_categories, prune=False)
    assert no_prune.accounts_disable == [] and no_prune.categories_disable == []
    pruned = plan_sync(_cfg(), live_accounts, live_categories, prune=True)
    assert [a["code"] for a in pruned.accounts_disable] == [999]
    assert [c["name"] for c in pruned.categories_disable] == ["Legacy Cat"]


def test_plan_prune_ignores_already_disabled():
    live_accounts = [{"id": 9, "code": 999, "name": "Legacy", "type_id": 12, "enabled": 0}]
    pruned = plan_sync(_cfg(), live_accounts, [], prune=True)
    assert pruned.accounts_disable == []      # already disabled -> nothing to do


def test_render_plan_is_human_readable():
    live_accounts = [{"id": 1, "code": 400, "name": "Sales", "type_id": 13, "enabled": 1}]
    lines = render_plan(plan_sync(_cfg(), live_accounts, [], prune=False))
    joined = "\n".join(lines)
    assert "create account 628" in joined
    assert "rename account 400" in joined and "Sales" in joined
    assert "create category" in joined
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: FAIL — `ImportError: cannot import name 'CoaPlan'`

- [ ] **Step 3: Add planning to `src/akt/coa.py`**

Append to `src/akt/coa.py`:

```python
@dataclass
class CoaPlan:
    accounts_create: list[CoaAccount] = field(default_factory=list)
    accounts_rename: list[tuple[CoaAccount, dict]] = field(default_factory=list)
    categories_create: list[CoaAccount] = field(default_factory=list)
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

    for acct in config.mirrored:
        live = live_cat.get((acct.category, acct.category_type))
        if live is None:
            plan.categories_create.append(acct)
        # name+type already matched -> nothing to rename (name is the join key)

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
```

- [ ] **Step 4: Wire the `coa diff` command in `src/akt/cli.py`**

In `_build_parser`, after the `raw` subparser block (after line 160), add:

```python
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
```

Add the imports (extend the `from .coa import ...` line from Task 2):

```python
from .coa import load_coa, plan_sync, render_plan
```

In `_run_special`, add a branch (before the final `raise ValueError(name)`):

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/akt/coa.py src/akt/cli.py tests/unit/test_coa.py
git commit -m "feat(coa): coa diff — sync planning + read-only command"
```

---

## Task 4: `coa sync` — apply the plan

**Files:**
- Modify: `src/akt/coa.py` (add `apply_plan`)
- Modify: `src/akt/cli.py` (`_run_special` handler for `coa_sync`)
- Test: `tests/unit/test_coa.py` (append)

**Interfaces:**
- Consumes: `CoaPlan`, `CoaConfig` (Task 3); a client exposing `post(path, body)`, `put(path, body)`, `get(path)`, `web_json(method, path, form)`, and `flatten_form` from `akt.resources`.
- Produces: `apply_plan(client, plan: CoaPlan, *, prune: bool) -> dict` — returns a summary count dict `{"accounts_created", "accounts_renamed", "categories_created", "accounts_disabled", "categories_disabled"}`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_coa.py
from akt.coa import apply_plan


class RecordingClient:
    """Records web_json / api calls apply_plan makes; returns benign values."""

    def __init__(self):
        self.calls: list[tuple] = []

    def web_json(self, method, path, form=None):
        self.calls.append(("web_json", method, path, dict(form or [])))
        return {"id": 999}

    def post(self, path, body, **kw):
        self.calls.append(("post", path, body))
        return {"data": {"id": 888}}

    def put(self, path, body, **kw):
        self.calls.append(("put", path, body))
        return {"data": {"id": body.get("id")}}

    def get(self, path, **kw):
        self.calls.append(("get", path))
        return {"data": {}}


def test_apply_creates_accounts_via_web_and_categories_via_api():
    live_accounts = [{"id": 1, "code": 400, "name": "Sales", "type_id": 13, "enabled": 1}]
    plan = plan_sync(_cfg(), live_accounts, [], prune=False)
    client = RecordingClient()
    summary = apply_plan(client, plan, prune=False)

    # accounts created + renamed go through the web CRUD route
    web = [c for c in client.calls if c[0] == "web_json"]
    assert any(m == "POST" and p == "double-entry/chart-of-accounts" for _, m, p, _ in web)
    assert any(m == "PATCH" and p.startswith("double-entry/chart-of-accounts/") for _, m, p, _ in web)
    # first account to create (config order 628, 310, 850) carries code/name/type_id as strings
    create_form = next(f for _, m, p, f in web if m == "POST")
    assert create_form["code"] == "628"
    assert create_form["name"] == "Other / Uncategorized"
    assert create_form["type_id"] == "12"
    assert create_form["is_sub_account"] == "false"

    # categories created via /api POST with derived type
    cat_posts = [c for c in client.calls if c[0] == "post" and c[1] == "categories"]
    types = {c[2]["name"]: c[2]["type"] for c in cat_posts}
    assert types["API Subscription Revenue"] == "income"
    assert types["Other / Uncategorized"] == "expense"
    assert types["Owners Draw"] == "other"

    assert summary["accounts_created"] == 3
    assert summary["accounts_renamed"] == 1
    assert summary["categories_created"] == 3


def test_apply_prune_disables_via_toggle_routes():
    live_accounts = [{"id": 9, "code": 999, "name": "Legacy", "type_id": 12, "enabled": 1}]
    live_categories = [{"id": 20, "name": "Legacy Cat", "type": "expense", "enabled": 1}]
    plan = plan_sync(_cfg(), live_accounts, live_categories, prune=True)
    client = RecordingClient()
    summary = apply_plan(client, plan, prune=True)
    # account disable -> web GET disable route; category disable -> api GET disable
    assert ("web_json", "GET", "double-entry/chart-of-accounts/9/disable", {}) in client.calls
    assert ("get", "categories/20/disable") in client.calls
    assert summary["accounts_disabled"] == 1 and summary["categories_disabled"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_coa.py -k apply -v`
Expected: FAIL — `ImportError: cannot import name 'apply_plan'`

- [ ] **Step 3: Add `apply_plan` to `src/akt/coa.py`**

Append to `src/akt/coa.py`. The `flatten_form` import is **function-local** on purpose:
Task 5 makes `resources.py` import from `coa.py`, so a top-level `from .resources import
flatten_form` here would create a circular import at module load. A local import avoids it.

```python
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
               "categories_created": 0, "accounts_disabled": 0,
               "categories_disabled": 0}

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

    if prune:
        for live in plan.accounts_disable:
            client.web_json("GET", f"double-entry/chart-of-accounts/{live['id']}/disable")
            summary["accounts_disabled"] += 1
        for live in plan.categories_disable:
            client.get(f"categories/{live['id']}/disable")
            summary["categories_disabled"] += 1

    return summary
```

- [ ] **Step 4: Wire the `coa sync` command in `src/akt/cli.py`**

Extend the `from .coa import ...` line:

```python
from .coa import load_coa, plan_sync, render_plan, apply_plan
```

In `_run_special`, add after the `coa_diff` branch:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/akt/coa.py src/akt/cli.py tests/unit/test_coa.py
git commit -m "feat(coa): coa sync — apply accounts + mirror categories; --prune"
```

---

## Task 5: Bidirectional auto-fill on `payment create`

**Files:**
- Modify: `src/akt/coa.py` (add `resolve_coding`)
- Modify: `src/akt/registry.py` (add `--account` / `--category` to `PAYMENT.fields`)
- Modify: `src/akt/resources.py` (`build_payment_create` consults the config)
- Test: `tests/unit/test_coa.py` (append)

**Interfaces:**
- Consumes: `CoaConfig`, `CoaAccount` (Task 1); a client exposing `list("chart-of-accounts")` and `list("categories")`.
- Produces: `resolve_coding(config: CoaConfig, client, *, account_ref: str | None, category_ref: str | None) -> tuple[int, int]` returning `(de_account_id, category_id)` (live ids). Raises `ValueError` on unknown ref, disagreement, or not-yet-synced records.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_coa.py
from types import SimpleNamespace
from akt.coa import resolve_coding
from akt.registry import PAYMENT
from akt.resources import build_payment_create


class CoaFakeClient:
    def __init__(self, accounts, categories, settings=None):
        self._accounts = accounts
        self._categories = categories
        self._settings = settings or {}

    def list(self, path, **kw):
        if path == "chart-of-accounts":
            return self._accounts
        if path == "categories":
            return self._categories
        return []

    def setting(self, key, default=None):
        return self._settings.get(key, default)

    def show(self, path, ident, **kw):
        raise KeyError(path)


_LIVE_ACCOUNTS = [
    {"id": 47, "code": 400, "name": "API Subscription Revenue", "type_id": 13},
    {"id": 61, "code": 628, "name": "Other / Uncategorized", "type_id": 12},
]
_LIVE_CATEGORIES = [
    {"id": 10, "name": "API Subscription Revenue", "type": "income"},
    {"id": 11, "name": "Other / Uncategorized", "type": "expense"},
]


def test_resolve_by_account_code():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    de_id, cat_id = resolve_coding(_cfg(), client, account_ref="400", category_ref=None)
    assert (de_id, cat_id) == (47, 10)


def test_resolve_by_account_name():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    de_id, cat_id = resolve_coding(_cfg(), client,
                                   account_ref="Other / Uncategorized", category_ref=None)
    assert (de_id, cat_id) == (61, 11)


def test_resolve_by_category_name():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    de_id, cat_id = resolve_coding(_cfg(), client, account_ref=None,
                                   category_ref="API Subscription Revenue")
    assert (de_id, cat_id) == (47, 10)


def test_resolve_conflicting_account_and_category_errors():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    with pytest.raises(ValueError, match="disagree"):
        resolve_coding(_cfg(), client, account_ref="400",
                       category_ref="Other / Uncategorized")


def test_resolve_unknown_ref_errors():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    with pytest.raises(ValueError, match="not in the COA config"):
        resolve_coding(_cfg(), client, account_ref="777", category_ref=None)


def test_resolve_account_not_synced_errors():
    # config has 310 Owners Draw but it isn't live yet
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    with pytest.raises(ValueError, match="coa sync"):
        resolve_coding(_cfg(), client, account_ref="310", category_ref=None)


def _payment_ns(**over):
    base = dict(invoice=None, bill=None, type="income", document_id=None,
                contact_id=None, category_id=None, amount=1.0, account_id=1,
                paid_at="2026-07-12", currency_code=None, currency_rate=None,
                payment_method=None, number="TXN-1", reference=None,
                description="x", account=None, category=None, set_=None, data=None)
    base.update(over)
    ns = SimpleNamespace(**base)
    ns._coa = _cfg()
    return ns


def test_payment_create_autofills_both_sides_from_account():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES,
                           settings={"default.account": "1"})
    body = build_payment_create(PAYMENT, client, _payment_ns(account="400"))
    assert body["de_account_id"] == 47
    assert body["category_id"] == 10


def test_payment_create_explicit_set_de_account_wins():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES,
                           settings={"default.account": "1"})
    body = build_payment_create(PAYMENT, client,
                                _payment_ns(account="400", set_=["de_account_id=99"]))
    assert body["de_account_id"] == 99          # explicit --set overrides
    assert body["category_id"] == 10


def test_payment_create_no_coa_flag_is_legacy():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES,
                           settings={"default.income_category": "2", "default.account": "1"})
    body = build_payment_create(PAYMENT, client, _payment_ns(category_id=2))
    assert "de_account_id" not in body          # untouched
    assert body["category_id"] == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_coding'` (or, once that's added, `--account` attribute wiring).

- [ ] **Step 3: Add `resolve_coding` to `src/akt/coa.py`**

Append to `src/akt/coa.py`:

```python
def _find_account(config: CoaConfig, ref: str) -> CoaAccount:
    acct = config.by_code(int(ref)) if str(ref).lstrip("-").isdigit() else config.by_name(ref)
    if acct is None:
        raise ValueError(f"account {ref!r} is not in the COA config")
    return acct


def resolve_coding(config: CoaConfig, client, *, account_ref: str | None,
                   category_ref: str | None) -> tuple[int, int]:
    """Resolve --account / --category to live (de_account_id, category_id).

    Both flags (if given) must resolve to the same config account. The account
    and its mirror category must already exist in Akaunting (run `coa sync`)."""
    acct = None
    if account_ref is not None:
        acct = _find_account(config, account_ref)
    if category_ref is not None:
        by_cat = config.by_category(category_ref)
        if by_cat is None:
            raise ValueError(f"category {category_ref!r} is not in the COA config")
        if acct is not None and by_cat.code != acct.code:
            raise ValueError(
                f"--account and --category disagree: {account_ref!r} -> {acct.code}, "
                f"{category_ref!r} -> {by_cat.code}")
        acct = by_cat

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
```

- [ ] **Step 4: Add the `--account` / `--category` flags in `src/akt/registry.py`**

In the `PAYMENT` resource `fields` list (registry.py), add these two entries (place them right after the `f("bill", ...)` line):

```python
        f("account", "GL/chart-of-accounts code or name to post to (the double-entry "
                     "account — NOT the bank --account-id). Requires a --coa config; "
                     "auto-fills the mirrored category."),
        f("category", "Mirror category name to post under (requires a --coa config); "
                      "auto-fills the corresponding GL account."),
```

- [ ] **Step 5: Consult the config in `build_payment_create` (`src/akt/resources.py`)**

Add the import at the top of `resources.py` (after `from .client import Client`):

```python
from .coa import resolve_coding
```

In `build_payment_create`, right after the line `category_id = getattr(ns, "category_id", None)` (line 437), insert:

```python
    coa = getattr(ns, "_coa", None)
    account_ref = getattr(ns, "account", None)
    category_ref = getattr(ns, "category", None)
    de_account_id = None
    if coa is not None and (account_ref or category_ref):
        de_account_id, category_id = resolve_coding(
            coa, client, account_ref=account_ref, category_ref=category_ref)
```

Then, in the same function, immediately before `body.update(parse_set(getattr(ns, "set_", None)))` (line 495), insert:

```python
    if de_account_id is not None:
        body["de_account_id"] = de_account_id
```

(Placing it before the `--set`/`--data` merge means an explicit `--set de_account_id=` / raw `--data` still wins.)

- [ ] **Step 6: Verify no import cycle**

`resources.py` now imports `resolve_coding` from `coa`, and `coa` imports `flatten_form`
from `resources` only lazily (function-local, added in Task 4), so there is no cycle at
module load. Confirm:

Run: `uv run python -c "import akt.cli"`
Expected: no output, exit 0.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_coa.py -v`
Expected: PASS

- [ ] **Step 8: Run the full unit suite (no regressions)**

Run: `uv run pytest tests/unit -q`
Expected: PASS (all)

- [ ] **Step 9: Commit**

```bash
git add src/akt/coa.py src/akt/registry.py src/akt/resources.py tests/unit/test_coa.py
git commit -m "feat(coa): bidirectional --account/--category coding on payment create"
```

---

## Task 6: Live integration test

**Files:**
- Modify: `tests/integration/test_live.py`

**Interfaces:**
- Consumes: the `akt` and `tracker` fixtures (conftest.py).

- [ ] **Step 1: Write the integration test**

Append to `tests/integration/test_live.py` (uses a temp COA fixture pointed at via `--coa`; every created record is tracked for teardown):

```python
def test_coa_sync_and_coded_payment(akt, tracker, akt_env, tmp_path):
    """coa sync creates an account + mirror category; a coded payment lands on both."""
    import subprocess

    # A unique code/name so the test is idempotent across runs.
    coa_file = tmp_path / "coa.toml"
    coa_file.write_text(
        '[[account]]\n'
        'code = 991\n'
        'name = "AKT Test Revenue 991"\n'
        'type_id = 13\n'
    )
    env = {**akt_env, "AKT_COA_FILE": str(coa_file)}

    def run_json(*args):
        proc = subprocess.run([*_AKT_CMD, "--json", *args],
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def run_text(*args):
        proc = subprocess.run([*_AKT_CMD, *args], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    # sync (idempotent) then locate the created account + category
    run_text("coa", "sync")
    accounts = run_json("chart-of-account", "list")
    acct = next(a for a in accounts if int(a["code"]) == 991)
    cats = run_json("category", "list")
    cat = next(c for c in cats if c["name"] == "AKT Test Revenue 991" and c["type"] == "income")
    tracker("chart-of-account", acct["id"])
    tracker("category", cat["id"])

    # a payment coded by account fills BOTH de_account_id and category_id
    txn = run_json("payment", "create", "--type", "income", "--account-id", "1",
                   "--amount", "0.01", "--paid-at", "2026-07-12",
                   "--description", "coa coded smoke", "--account", "991")
    tracker("payment", txn["id"])
    assert str(txn["category_id"]) == str(cat["id"])
    # de_account_id surfaces on the transaction's ledgers as the item-side account
    assert any(int(l.get("account_id", 0)) == int(acct["id"])
               for l in txn.get("ledgers", {}).get("data", []))
```

- [ ] **Step 2: Run the integration test (requires live creds)**

Run: `AKT_THROTTLE=0.5 uv run pytest tests/integration/test_live.py::test_coa_sync_and_coded_payment -v`
Expected: PASS when `AKT_BASE_URL`/`AKT_EMAIL`/`AKT_PASSWORD` are set; SKIP otherwise. Teardown deletes the payment, then the category and account.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_live.py
git commit -m "test(coa): live sync + coded-payment round-trip"
```

---

## Task 7: Version bump + README

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] **Step 1: Bump the version**

In `pyproject.toml`, change `version = "0.3.0"` to:

```toml
version = "0.4.0"
```

- [ ] **Step 2: Document the feature in README.md**

Add a new section (place it after the existing double-entry / chart-of-accounts material):

````markdown
## COA config: link categories and accounts (Xero-style)

Akaunting keeps *categories* (required on every transaction) separate from the
double-entry *chart of accounts*. Point akt at a COA config and it keeps them in
lockstep: one list to maintain (accounts), a 1:1 mirror of categories generated
from it, and `payment create` coded by either — with the other side filled in.

akt finds the config at `--coa FILE` → `AKT_COA_FILE` → `./coa.toml` →
`~/.config/akt/coa.toml`. Minimal schema (extra keys are ignored):

```toml
[[account]]
code    = 400
name    = "API Subscription Revenue"
type_id = 13            # DoubleEntry account type; income/expense class -> category type
# optional: category = "Revenue"   (override the mirror name)
# optional: mirror   = false        (skip mirroring, e.g. bank/AR/AP accounts)
```

```bash
akt coa diff                 # preview: accounts/categories to create or rename
akt coa sync                 # apply (create + rename; idempotent)
akt coa sync --prune         # also DISABLE accounts/categories absent from the config

# code a transaction by GL account OR by category — akt fills the other:
akt payment create --type expense --account-id 1 --amount 120 --account 628
akt payment create --type income  --account-id 1 --amount 500 --category "API Subscription Revenue"
```

`--account` takes a GL code or name (the double-entry account — not the bank
`--account-id`). An explicit `--set de_account_id=` still wins.
````

- [ ] **Step 3: Verify the whole suite + a smoke of the CLI**

Run: `uv run pytest -q` and `uv run akt coa diff --help`
Expected: unit tests PASS (integration SKIP without creds); the `coa diff` help renders.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml README.md
git commit -m "chore: release 0.4.0 — COA config category<->account linking"
```

---

## Self-Review

**1. Spec coverage:**
- Config schema + discovery precedence → Task 1 (parse/load) + Task 2 (`--coa`). ✓
- `type_id → category type` built-in table incl. unknown→other+warning → Task 1. ✓
- `mirror`/`category` overrides, name/code uniqueness validation → Task 1. ✓
- `coa diff` / `coa sync`, create+rename, `--prune` disable-only → Tasks 3–4. ✓
- Bidirectional auto-fill, resolution/conflict table, explicit-set-wins, not-synced error → Task 5. ✓
- Balance-sheet accounts get an `other` category → Task 1 (type map) + Task 4 (create with derived type). ✓
- Live integration (sync + coded payment) → Task 6. ✓
- Consumer note (`tools/coa.toml` is a superset) → covered by superset test (Task 1) + README (Task 7). ✓
- Non-goals (history reclass, document line-items, vendor auto-coding) → not implemented, by design. ✓

**2. Placeholder scan:** No TBD/TODO; every code and test step contains complete content. ✓

**3. Type consistency:** `CoaAccount` fields (`code:int`, `name`, `type_id:int`, `category`, `category_type`, `mirror`) are used consistently across `plan_sync`, `apply_plan`, `resolve_coding`. `plan_sync(config, live_accounts, live_categories, *, prune)` and `apply_plan(client, plan, *, prune)` signatures match their call sites in `cli.py`. `resolve_coding(config, client, *, account_ref, category_ref)` matches its call in `build_payment_create`. Live-record matching uses `int(code)` on both sides. ✓

**Note on `config.py`:** the File Structure lists `config.py` as optionally modified; the plan keeps discovery inside `coa.py` (`_coa_candidate_files`) for module self-containment, so `config.py` is left unchanged. No task depends on editing it.
