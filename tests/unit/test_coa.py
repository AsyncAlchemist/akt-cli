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
name = "Operating Checking"
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
name = "Cloud Hosting"
type_id = 11
status = "new"
vendors = ["acme"]
note = "ignored by akt"
""")
    assert cfg.by_code(510).name == "Cloud Hosting"


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
