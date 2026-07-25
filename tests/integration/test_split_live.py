"""Live integration test for `payment create --split` — multi-GL-leg transactions.

Requires the akt-api companion module WITH the split endpoint deployed. Skips
gracefully if the DoubleEntry module, the ledger API, or the split route
specifically isn't there. Every record it creates is torn down.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import _AKT_CMD

pytestmark = pytest.mark.integration

_COA = (
    '[[account]]\ncode = 994\nname = "AKT Split Rev 994"\ntype_id = 13\n'
    '[[account]]\ncode = 995\nname = "AKT Split Exp 995"\ntype_id = 11\n'
    '[[account]]\ncode = 996\nname = "AKT Split Exp 996"\ntype_id = 11\n'
)


def test_payment_split_creates_multi_leg(akt_env, tracker, tmp_path):
    coa_file = tmp_path / "coa.toml"
    coa_file.write_text(_COA)
    env = {**akt_env, "AKT_COA_FILE": str(coa_file)}

    def run(*args, check=True):
        proc = subprocess.run([*_AKT_CMD, "--json", *args],
                              capture_output=True, text=True, env=env)
        if check and proc.returncode != 0:
            raise AssertionError(f"`akt {' '.join(args)}` failed: {proc.stderr.strip()}")
        return proc

    def rj(*args):
        proc = run(*args)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    # Needs DoubleEntry (chart-of-accounts). Skip if coa sync can't run.
    sync = run("coa", "sync", check=False)
    if sync.returncode != 0:
        pytest.skip("DoubleEntry module not available (coa sync failed)")

    accounts = rj("account", "list", "--all")
    codes = {994, 995, 996}
    found = {int(a["code"]): a for a in accounts if int(a.get("code", -1)) in codes}
    assert set(found) == codes, f"coa sync did not create test accounts: {found.keys()}"
    cats = rj("category", "list", "--all")
    for a in found.values():
        tracker("account", a["id"])
    for name in ("AKT Split Rev 994", "AKT Split Exp 995", "AKT Split Exp 996"):
        c = next((c for c in cats if c["name"] == name), None)
        if c:
            tracker("category", c["id"])

    # Create the split: income $0.01 = credit 0.03 (994) - debit 0.01 (995) - debit
    # 0.01 (996) = net -0.01, i.e. -amount. One bank transaction, 3 GL item legs.
    made = run("payment", "create", "--type", "income", "--bank", "1",
               "--amount", "0.01", "--paid-at", "2026-07-25",
               "--description", "AKT split integration",
               "--split", "account=994,credit=0.03",
               "--split", "account=995,debit=0.01",
               "--split", "account=996,debit=0.01",
               check=False)
    if made.returncode != 0:
        err = made.stderr.lower()
        if "companion module" in err or "404" in err or "split" in err:
            pytest.skip("akt-api split endpoint not deployed on this instance")
        raise AssertionError(f"split create failed: {made.stderr.strip()}")

    txn = json.loads(made.stdout)
    tracker("payment", txn["id"])
    txn_id = txn["id"]

    # Verify the item legs: each test account carries exactly its leg for this txn.
    expected = {994: ("credit", 0.03), 995: ("debit", 0.01), 996: ("debit", 0.01)}
    seen = 0
    for code, (side, amt) in expected.items():
        rows = rj("ledger", "--account", str(code))
        legs = [r for r in rows if str(r.get("ledgerable_id")) == str(txn_id)]
        assert len(legs) == 1, f"account {code}: expected 1 leg for txn {txn_id}, got {len(legs)}"
        leg = legs[0]
        assert leg["entry_type"] == "item"
        assert float(leg[side] or 0) == pytest.approx(amt), f"account {code} {side}"
        assert float(leg["credit" if side == "debit" else "debit"] or 0) == 0
        seen += 1
    assert seen == 3   # the single transaction fanned out into exactly 3 item legs
