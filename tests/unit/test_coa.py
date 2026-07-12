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


def test_load_coa_env_var_reads_file(tmp_path, monkeypatch):
    p = tmp_path / "env-coa.toml"
    p.write_text(_MINIMAL)
    monkeypatch.setenv("AKT_COA_FILE", str(p))
    monkeypatch.chdir(tmp_path)                        # no ./coa.toml here
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no ~/.config/akt/coa.toml
    cfg = load_coa()
    assert cfg is not None and cfg.by_code(400).name == "API Subscription Revenue"


def test_load_coa_env_var_missing_falls_through_to_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("AKT_COA_FILE", str(tmp_path / "nope.toml"))
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "coa.toml").write_text(_MINIMAL)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = load_coa()
    assert cfg is not None and cfg.by_code(400).name == "API Subscription Revenue"


def test_load_coa_env_var_missing_and_no_fallback_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("AKT_COA_FILE", str(tmp_path / "nope.toml"))
    cwd = tmp_path / "work"
    cwd.mkdir()                                         # no ./coa.toml here
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # no ~/.config/akt/coa.toml
    assert load_coa() is None


def test_load_coa_discovers_cwd_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AKT_COA_FILE", raising=False)
    (tmp_path / "coa.toml").write_text(_MINIMAL)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = load_coa()
    assert cfg is not None and cfg.by_code(400).name == "API Subscription Revenue"


def test_load_coa_discovers_home_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AKT_COA_FILE", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)   # no ./coa.toml here
    home = tmp_path / "home"
    config_dir = home / ".config" / "akt"
    config_dir.mkdir(parents=True)
    (config_dir / "coa.toml").write_text(_MINIMAL)
    monkeypatch.setenv("HOME", str(home))
    cfg = load_coa()
    assert cfg is not None and cfg.by_code(400).name == "API Subscription Revenue"


def test_load_coa_precedence_explicit_beats_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[[account]]\ncode = 111\nname = "Explicit"\ntype_id = 13\n')
    env_file = tmp_path / "env.toml"
    env_file.write_text('[[account]]\ncode = 222\nname = "Env"\ntype_id = 13\n')
    monkeypatch.setenv("AKT_COA_FILE", str(env_file))
    cfg = load_coa(str(explicit))
    assert cfg is not None
    assert cfg.by_code(111) is not None    # explicit file won
    assert cfg.by_code(222) is None        # env file was not read


def test_load_coa_precedence_env_beats_cwd(tmp_path, monkeypatch):
    env_file = tmp_path / "env.toml"
    env_file.write_text('[[account]]\ncode = 222\nname = "Env"\ntype_id = 13\n')
    monkeypatch.setenv("AKT_COA_FILE", str(env_file))
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "coa.toml").write_text('[[account]]\ncode = 333\nname = "Cwd"\ntype_id = 13\n')
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = load_coa()
    assert cfg is not None
    assert cfg.by_code(222) is not None    # env file won
    assert cfg.by_code(333) is None        # cwd file was not read


def test_by_name_known_and_unknown():
    cfg = parse_coa(_MINIMAL)
    acct = cfg.by_name("API Subscription Revenue")
    assert acct is not None and acct.code == 400
    assert cfg.by_name("Nonexistent Account") is None


def test_by_code_and_by_category_none_when_absent():
    cfg = parse_coa(_MINIMAL)
    assert cfg.by_code(9999) is None
    assert cfg.by_category("Nonexistent Category") is None


from akt.cli import _build_parser


def test_coa_flag_parses_to_coa_file():
    ns = _build_parser().parse_args(["--coa", "/tmp/chart.toml", "payment", "list"])
    assert ns.coa_file == "/tmp/chart.toml"


def test_coa_group_no_verb_falls_to_help_guard():
    """`akt coa` (no verb) must fall to main()'s no-verb-help guard, not the
    BY_NOUN dispatch — regression for the KeyError crash."""
    from akt.registry import BY_NOUN
    ns = _build_parser().parse_args(["coa"])
    assert ns.resource == "coa"
    assert ns.resource not in BY_NOUN                 # not a resource noun
    assert getattr(ns, "_special", None) is None       # bare group sets no _special
    assert getattr(ns, "_handler", None) is None       # and no verb handler


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


def test_payment_create_explicit_category_id_wins_over_coa():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES,
                           settings={"default.account": "1"})
    body = build_payment_create(PAYMENT, client,
                                _payment_ns(account="400", category_id=99))
    assert body["category_id"] == 99            # explicit --category-id wins over coa
    assert body["de_account_id"] == 47           # de_account_id still comes from coa


def test_payment_create_coding_flag_without_coa_config_errors():
    ns = _payment_ns(account="400")
    ns._coa = None
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES,
                           settings={"default.account": "1"})
    with pytest.raises(ValueError, match="COA config"):
        build_payment_create(PAYMENT, client, ns)


def test_resolve_mirror_false_account_errors_clearly():
    client = CoaFakeClient(_LIVE_ACCOUNTS, _LIVE_CATEGORIES)
    with pytest.raises(ValueError, match="mirror=false"):
        resolve_coding(_cfg(), client, account_ref="850", category_ref=None)
