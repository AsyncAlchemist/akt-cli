# Design: silent-posting detection in `akt verify`

- **Date:** 2026-07-27
- **Status:** Approved
- **Related:** issue #10 / PR #11 (dedicated CI instance); `akt-api` companion module

## Problem

Akaunting's Double-Entry (DE) module posts a transaction's ledger legs only if
the transaction's bank has a `double_entry_account_bank` mapping (see
`Observers/Banking/Transaction::created`, which bails when the bank is unmapped).
DE creates that mapping via its `Jobs\Install\CopyData` job — dispatched by its
`ModuleEnabled` / `FinishInstallation` listeners — but only when DE is enabled
through Akaunting's module-enable **event**. A bank that predates DE (e.g. the
default "Cash" bank created at install) is therefore never mapped, and **every
payment on it silently posts nothing to the ledger**: reports come up short and
`payment --split` finds no item leg to fan out. The failure is invisible until
someone notices a number is wrong.

`akt verify` today only audits *coding correctness* (posted GL account vs the COA
mirror of a category), hard-requires a COA config, and reports the symptom
per-transaction ("not posted to the ledger") without ever naming the root cause
(the silent bank). It cannot run without a COA file.

## Goals

- Detect the two faces of this failure, **read-only**:
  - **Symptom:** income/expense transactions that posted no ledger legs.
  - **Root cause:** banks with no DE ledger mapping (catchable even before any
    transaction exists on the bank).
- Make `akt verify` useful **without** a COA config (the new checks are
  COA-independent).
- Point at the fix in the output (run DE's `CopyData`), without performing it —
  fixing is DE's job.

## Non-goals (YAGNI)

- **No auto-fix.** Detection only; the fix is DE's `CopyData` install job.
- **No documents/transfers** in the symptom check — the unmapped-bank endpoint
  gives the comprehensive bank-level view, so the symptom check stays on the
  standalone income/expense set `verify` already fetches.
- **No new top-level command** — the checks extend `akt verify`.

## Design

### Overview: what `akt verify` becomes

Two new COA-independent ledger-health checks run **always**. The existing
COA-coding checks (`find_miscodings`, `find_report_dropped`) run **only when a COA
config is present**.

- `akt verify` (no COA) → runs Check A + Check B.
- `akt verify` (COA present) → Check A + Check B **plus** today's coding audit.
- verify no longer errors on a missing COA. It still errors if akt-api is absent
  (every check needs the ledger API). Exit `1` if any finding — unchanged.

### Check A — unposted transactions (client-side, pure)

A new pure function in `src/akt/verify.py`:

```python
def find_unposted(transactions: list[dict],
                  item_account_by_txn: dict[int, int],
                  banks_by_id: dict[int, dict]) -> list[dict]: ...
```

`verify` already fetches the standalone income/expense txn set and
`item_account_by_txn` (txn id → posted item-leg account). A txn whose id is **not**
in that map posted no item leg → unposted. Each finding names the txn's bank
(`transactions[].account_id` → `banks_by_id`) as the likely culprit:

> `posted no ledger legs — bank '<name>' is likely unmapped; run DE's CopyData`

One extra fetch beyond what verify already does: the bank list
(`client.list("accounts", all_pages=True)` — `BANK` resource, endpoint `accounts`)
to resolve `account_id` → name.

### Check B — unmapped banks (new akt-api endpoint)

New read-only endpoint, mirroring the existing route/controller pattern:

```
GET /api/akt-api/banks/unmapped   ->   { "data": [ { "id": <bank_id>, "name": <str> } ] }
```

- New controller `Modules\AktApi\Http\Controllers\Api\Banks` with an `unmapped`
  method (mirrors the `Balances` controller); route added in
  `akt-api/Routes/api.php` under the existing `api.` group.
- Query: `App\Models\Banking\Account` (banks) whose id has **no** row in
  `double_entry_account_bank` for the current company. Scoped to `company_id()`.
- Gated on `permission:read-double-entry-chart-of-accounts` (same as the other
  read routes; declared in the controller's constructor, no `parent::__construct`).
- **Deliberate coupling note:** this is the one place akt-api reads a DE table
  other than `double_entry_ledger` (`double_entry_account_bank`). Documented as the
  intentional exception in the akt-api README, because root-cause detection
  requires it. Reads via the query builder only; imports no DE code.

verify surfaces each unmapped bank as a finding:

> `bank '<name>' has no Double-Entry ledger mapping — run DE's CopyData install job`

### CLI flow (`verify` handler in `cli.py`)

1. Require akt-api (`has_ledger_api()`); do **not** require a COA.
2. Fetch txns (existing), item ledgers → `item_account_by_txn` (existing), banks →
   `banks_by_id` (new).
3. `findings = find_unposted(txns, item_account_by_txn, banks_by_id)`.
4. `findings += [format for b in client.get("akt-api/banks/unmapped")["data"]]`.
5. If `ns._coa` is present: `findings += find_miscodings(...) + find_report_dropped(...)`
   (today's behavior).
6. Emit findings; exit `1` if any.

### Output

All findings share verify's existing table. Add a `bank` column (populated for the
two new finding types, blank for the coding ones). The `reason` string carries the
fix guidance in every case. `--json` emits the full finding dicts.

## Error handling

- akt-api missing → clear error (as today, reworded: verify now needs only akt-api,
  not a COA).
- `banks/unmapped` non-200 → surface the API error (existing `client` error path).
- No COA → run the health checks silently (no error, no coding findings).

## Testing

- **`find_unposted`** → unit tests (pure, offline), same style as the existing
  `find_miscodings` / `find_report_dropped` tests: posted txn (skipped), unposted
  txn with a known bank (flagged, bank named), unposted txn with unknown bank
  (flagged, falls back to `account_id`).
- **`banks/unmapped` endpoint** → integration test asserting the contract (200,
  `data` is a list of `{id,name}`). Note: forcing a *live* unmapped bank is not
  practical (creating a bank via the API triggers DE's observer, which maps it), so
  the test asserts the endpoint's shape/response rather than forcing an unmapped
  state. The finding-formatting logic is covered by the unit tests.
- **verify end-to-end** → existing integration coverage exercises the verify path;
  add a light assertion that `akt verify` runs without a COA and returns a findings
  list.

## Files touched

- `src/akt/verify.py` — add `find_unposted`.
- `src/akt/cli.py` — verify handler: drop the COA hard-requirement, wire Checks A/B.
- `akt-api/Http/Controllers/Api/Banks.php` — new controller.
- `akt-api/Routes/api.php` — new route.
- `akt-api/README.md` — document the `banks/unmapped` endpoint + the deliberate
  second-table coupling.
- `tests/unit/…` — `find_unposted` unit tests.
- `tests/integration/…` — endpoint shape + COA-less verify assertions.
- `docs/ci-integration-instance.md` — cross-reference: `akt verify` now flags this.
