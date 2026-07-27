# Silent-Posting Detection in `akt verify` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `akt verify` flag transactions that silently posted no ledger legs and banks with no Double-Entry mapping — read-only, without requiring a COA config.

**Architecture:** Two COA-independent checks added to the existing `verify` handler. Check A is a pure Python function over data verify already fetches (plus the bank list). Check B is a new read-only akt-api endpoint (`GET /api/akt-api/banks/unmapped`). The existing COA-coding checks run only when a COA config is present.

**Tech Stack:** Python 3.12+ (stdlib + `requests`), pytest; PHP (Akaunting module, Laravel query builder).

## Global Constraints

- Python: `requires-python >=3.12`; runtime deps limited to `requests` (test-only deps go in the `dev` dependency-group).
- akt-api reads DoubleEntry tables via the **query builder only** — import no DoubleEntry code.
- The CI instance runs artisan under PHP 8.1 (`/opt/cpanel/ea-php81/root/usr/bin/php`); its default `php` is 7.4.
- Findings are plain dicts; `akt verify` exits `1` when any finding is present, `0` otherwise.
- Integration tests need `AKT_BASE_URL` / `AKT_EMAIL` / `AKT_PASSWORD` (the CI instance) and skip without them.

---

### Task 1: `find_unposted` pure function

**Files:**
- Modify: `src/akt/verify.py`
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Produces: `find_unposted(transactions: list[dict], item_account_by_txn: dict[int, int], banks_by_id: dict[int, dict]) -> list[dict]` — one finding dict per transaction whose `id` is absent from `item_account_by_txn`. Finding keys: `transaction_id, paid_at, amount, bank, category, expected_code, actual_code, reason`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_verify.py`:

```python
from akt.verify import find_unposted

_BANKS = {1: {"name": "Cash"}, 2: {"name": "Checking"}}


def _paytxn(tid, account_id, amount=10.0):
    return {"id": tid, "paid_at": "2025-06-01", "amount": amount, "account_id": account_id}


def test_unposted_flags_txn_with_no_item_leg():
    txns = [_paytxn(1, account_id=1)]
    out = find_unposted(txns, item_account_by_txn={}, banks_by_id=_BANKS)
    assert len(out) == 1
    assert out[0]["transaction_id"] == 1
    assert out[0]["bank"] == "Cash"
    assert "unmapped" in out[0]["reason"]


def test_unposted_skips_txn_that_posted():
    txns = [_paytxn(1, account_id=1)]
    assert find_unposted(txns, item_account_by_txn={1: 90}, banks_by_id=_BANKS) == []


def test_unposted_falls_back_to_account_id_when_bank_unknown():
    txns = [_paytxn(1, account_id=99)]
    out = find_unposted(txns, item_account_by_txn={}, banks_by_id=_BANKS)
    assert len(out) == 1
    assert out[0]["bank"] is None
    assert "99" in out[0]["reason"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_verify.py -k unposted -v`
Expected: FAIL with `ImportError: cannot import name 'find_unposted'`.

- [ ] **Step 3: Implement `find_unposted`**

Append to `src/akt/verify.py`:

```python
def find_unposted(transactions: list[dict], item_account_by_txn: dict[int, int],
                  banks_by_id: dict[int, dict]) -> list[dict]:
    """Flag standalone income/expense transactions that posted no item leg.

    DoubleEntry's Transaction observer posts a transaction's ledger legs only if
    its bank has a ``double_entry_account_bank`` mapping; a payment on an unmapped
    bank silently posts nothing. A transaction whose id is absent from
    ``item_account_by_txn`` (txn id -> posted item-leg GL account) has no item leg.
    Pure — all lookups pre-fetched."""
    findings: list[dict] = []
    for t in transactions:
        if t["id"] in item_account_by_txn:
            continue                                   # posted an item leg — fine
        bank = banks_by_id.get(t.get("account_id"))
        bank_label = bank["name"] if bank else t.get("account_id")
        findings.append({
            "transaction_id": t["id"],
            "paid_at": str(t.get("paid_at", ""))[:10],
            "amount": t.get("amount"),
            "bank": bank["name"] if bank else None,
            "category": None,
            "expected_code": None,
            "actual_code": None,
            "reason": f"posted no ledger legs — bank '{bank_label}' is likely "
                      "unmapped; run DoubleEntry's CopyData install job",
        })
    return findings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_verify.py -v`
Expected: PASS (new + existing verify tests).

- [ ] **Step 5: Commit**

```bash
git add src/akt/verify.py tests/unit/test_verify.py
git commit -m "feat(verify): find_unposted — flag transactions that posted no ledger legs"
```

---

### Task 2: akt-api `banks/unmapped` endpoint

**Files:**
- Create: `akt-api/Http/Controllers/Api/Banks.php`
- Modify: `akt-api/Routes/api.php`
- Modify: `akt-api/README.md`
- Test: `tests/integration/test_live.py`

**Interfaces:**
- Produces: `GET /api/akt-api/banks/unmapped` → `{"data": [{"id": <int>, "name": <str>}, ...]}` — banks with no `double_entry_account_bank` row for the current company.

- [ ] **Step 1: Write the controller**

Create `akt-api/Http/Controllers/Api/Banks.php`:

```php
<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use App\Models\Banking\Account;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

class Banks extends ApiController
{
    /**
     * Gate on DoubleEntry read access (see Ledgers controller for why we declare
     * our own middleware instead of calling parent::__construct()).
     */
    public function __construct()
    {
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('unmapped');
    }

    /**
     * Banking accounts (banks) with NO double_entry_account_bank mapping for the
     * current company. DoubleEntry posts a transaction's ledger legs only when its
     * bank is mapped (see the module's Transaction observer), so an unmapped bank
     * silently posts nothing. This is the one place akt-api reads a DoubleEntry
     * table other than double_entry_ledger; it does so via the query builder and
     * imports no DoubleEntry code.
     */
    public function unmapped(Request $request)
    {
        $mapped = DB::table('double_entry_account_bank')
            ->where('company_id', company_id())
            ->whereNull('deleted_at')
            ->pluck('bank_id')
            ->all();

        $banks = Account::where('company_id', company_id())
            ->when(! empty($mapped), fn ($q) => $q->whereNotIn('id', $mapped))
            ->get(['id', 'name']);

        return response()->json(['data' => $banks->map(fn ($b) => [
            'id' => $b->id,
            'name' => $b->name,
        ])->values()->all()]);
    }
}
```

- [ ] **Step 2: Add the route**

In `akt-api/Routes/api.php`, inside the `['as' => 'api.']` group (next to the ledgers routes), add:

```php
        // Banks with no DoubleEntry ledger mapping — an unmapped bank silently
        // posts nothing (powers `akt verify`). Read-only diagnostic.
        Route::get('akt-api/banks/unmapped', 'Banks@unmapped')->name('akt-api.banks.unmapped');
```

- [ ] **Step 3: Deploy the updated module to the CI instance**

Redeploy `akt-api/` to the CI instance's `modules/AktApi/` and refresh routes/caches with the instance's PHP 8.1 binary (host details in `docs/ci-integration-instance.md` / session memory — keep them out of committed files):

```bash
rsync -az akt-api/ <instance>:public_html/modules/AktApi/
ssh <instance> 'cd public_html && \
  /opt/cpanel/ea-php81/root/usr/bin/php artisan route:clear && \
  /opt/cpanel/ea-php81/root/usr/bin/php artisan cache:clear'
```

- [ ] **Step 4: Verify the endpoint responds (shape)**

Run (against the CI instance):

```bash
curl -s -u "$AKT_EMAIL:$AKT_PASSWORD" "$AKT_BASE_URL/api/akt-api/banks/unmapped?company_id=1"
```
Expected: HTTP 200 with `{"data": [...]}` (a list — empty on a fully-mapped instance).

- [ ] **Step 5: Write the integration test**

Append to `tests/integration/test_live.py`:

```python
def test_akt_api_banks_unmapped_shape(akt):
    """The banks/unmapped diagnostic returns a JSON list of {id,name}."""
    probe = akt("raw", "GET", "akt-api/banks/unmapped", raw=True)
    if probe.returncode != 0 or "404" in probe.stderr:
        pytest.skip("akt-api banks/unmapped endpoint not deployed")
    import json
    resp = json.loads(probe.stdout)
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    assert isinstance(data, list)
    for b in data:
        assert "id" in b and "name" in b
```

- [ ] **Step 6: Run the integration test**

Run: `uv run pytest tests/integration/test_live.py::test_akt_api_banks_unmapped_shape -v`
Expected: PASS (with `AKT_*` env set to the instance). If `akt raw GET` doesn't scope the company, append `?company_id=1` to the path in the test.

- [ ] **Step 7: Document the endpoint in the README**

In `akt-api/README.md`, add `banks/unmapped` to the endpoint list and a short note that it is the one deliberate read of a DoubleEntry table other than `double_entry_ledger` (`double_entry_account_bank`), justified because root-cause detection needs it.

- [ ] **Step 8: Commit**

```bash
git add akt-api/Http/Controllers/Api/Banks.php akt-api/Routes/api.php akt-api/README.md tests/integration/test_live.py
git commit -m "feat(akt-api): banks/unmapped endpoint — banks with no DoubleEntry ledger mapping"
```

---

### Task 3: Wire the checks into `akt verify`

**Files:**
- Modify: `src/akt/cli.py` (import line ~26; verify handler ~333-368)
- Modify: `docs/ci-integration-instance.md`
- Test: `tests/integration/test_live.py`

**Interfaces:**
- Consumes: `find_unposted(...)` (Task 1); `GET akt-api/banks/unmapped` (Task 2).

- [ ] **Step 1: Add the import**

In `src/akt/cli.py`, change the verify import (line ~26) to include `find_unposted`:

```python
from .verify import find_miscodings, find_report_dropped, find_unposted, build_recode_plan
```

- [ ] **Step 2: Rewrite the verify handler**

Replace the `if name == "verify":` block (through its `return`) with:

```python
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
            findings += find_report_dropped(txns, item_account_by_txn, accounts_by_id)

        for f in findings:
            f.setdefault("bank", None)
        cols = ["transaction_id", "paid_at", "amount", "bank", "category",
                "expected_code", "actual_code", "reason"]
        emit(findings, as_json=ns.json, columns=None if ns.json else cols,
             headers=["Txn", "Date", "Amount", "Bank", "Category", "Expected", "Actual", "Reason"])
        return 0 if not findings else 1
```

- [ ] **Step 3: Verify unit tests still pass**

Run: `uv run pytest tests/unit -q`
Expected: PASS (no unit test exercises the handler directly; this confirms no import/syntax breakage).

- [ ] **Step 4: Exercise `akt verify` against the instance, without a COA**

Run (AKT_* env set, no `AKT_COA_FILE`):

```bash
uv run akt --json verify
```
Expected: exits `0` or `1` (not an error), prints a JSON findings list. On the fully-mapped, clean instance this is `[]` (exit 0). Confirms verify no longer requires a COA.

- [ ] **Step 5: Write the integration test**

Append to `tests/integration/test_live.py`:

```python
def test_verify_runs_without_coa(akt_env):
    """`akt verify` runs the COA-independent health checks with no COA config."""
    import json, subprocess
    from conftest import _AKT_CMD
    env = {k: v for k, v in akt_env.items() if k != "AKT_COA_FILE"}
    proc = subprocess.run([*_AKT_CMD, "--json", "verify"],
                          capture_output=True, text=True, env=env)
    if proc.returncode not in (0, 1):
        raise AssertionError(f"verify errored: {proc.stderr.strip()}")
    findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    assert isinstance(findings, list)
```

- [ ] **Step 6: Run the integration test**

Run: `uv run pytest tests/integration/test_live.py::test_verify_runs_without_coa -v`
Expected: PASS.

- [ ] **Step 7: Cross-reference in the runbook**

In `docs/ci-integration-instance.md`, under the default-bank step, add one line: `akt verify` now flags this (unmapped banks + transactions that posted no ledger legs), so it can be used to confirm the mapping took.

- [ ] **Step 8: Commit**

```bash
git add src/akt/cli.py docs/ci-integration-instance.md tests/integration/test_live.py
git commit -m "feat(verify): run COA-free ledger-health checks (unposted txns + unmapped banks)"
```

---

### Task 4: Full-suite verification + PR

- [ ] **Step 1: Run the unit suite**

Run: `uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 2: Run the full integration suite against the instance**

Run: `AKT_THROTTLE=1.0 uv run pytest tests/integration -v`
Expected: previously-green tests still pass; the two new tests pass. (1 xfail expected — the invoice plan-gate.)

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/verify-silent-posting
gh pr create --title "feat(verify): detect silent postings + unmapped banks" --body "<summary + verification>"
```

## Self-Review

- **Spec coverage:** Check A → Task 1 + Task 3; Check B → Task 2 + Task 3; verify COA-optional → Task 3; output/bank column → Task 3; README coupling note → Task 2 Step 7; tests → Tasks 1/2/3; docs cross-ref → Task 3 Step 7. All covered.
- **Placeholders:** none (endpoint response, handler body, and tests are concrete). The one runtime-confirmable spot (whether `akt raw GET` scopes the company) has an explicit fallback in Task 2 Step 6.
- **Type consistency:** `find_unposted` signature identical in Task 1 (definition), plan interfaces, and Task 3 (call site). Finding dicts carry the same keys everywhere; the handler's `setdefault("bank", None)` guards the coding findings that omit it.
