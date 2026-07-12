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
