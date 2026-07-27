# akt-cli foreign-currency support — design

**Date:** 2026-07-27
**Status:** approved design, pending spec review → implementation plan
**Scope:** correct multi-currency **input** and **reporting** in akt-cli, mirroring
Akaunting's own logic and pulling computed figures from the installation (via the
`akt-api` module) rather than recomputing in Python.

---

## 1. Problem & ground truth

All claims below were verified first-hand against the local Akaunting source
(the `akaunting-src` symlink and its sibling `akaunting-modules/DoubleEntry`, per
CONTRIBUTING.md).

### How Akaunting handles foreign currency
- Every money-bearing row (`transactions`, `documents`, journal legs) stores its
  `amount`/totals in **its own `currency_code`**, plus a **`currency_rate`
  snapshot** captured at entry. Direction: `currency_rate` = *foreign units per 1
  unit of the company default currency*. Base value = `amount ÷ currency_rate`
  (`app/Traits/Currencies.php` → `Money::divide`). Default currency is
  `setting('default.currency')`, pinned at `rate = 1`.
- The GL ledger (`double_entry_ledger`) has only `debit`/`credit` — **no currency
  columns**. Observers write the **raw foreign** amount
  (`Observers/Banking/Transaction.php:53,63`). Conversion is deferred to **read
  time** via the `DefaultCurrency` cast (`Casts/DefaultCurrency.php:30-31`), which
  reads the linked record's **historical** `currency_rate`.

### Which reports are correct (verified)
| Report | Converts? | Evidence |
|---|---|---|
| Trial Balance | **Yes** | `TrialBalance.php:103` → `Account::calculateBalance` → per-leg `castDebit/castCredit` (`Traits/Accounts.php:184-186`) |
| Balance Sheet | **Yes** | `BalanceSheet.php:76` → `balance_without_subaccounts` → same path |
| Profit & Loss family | **Yes** | core reports fed via listener, `convertToDefault` per leg |
| General Ledger | **No** (defect) | `GeneralLedger.php:109,115-116` sums raw `$ledger->debit/credit`; mixes a *converted* opening balance with *raw* foreign movements |
| Journal report | **No** (defect) | `JournalReport.php:83-84` `money($ledger_items->sum('debit'), default_currency)` on raw foreign |
| FX gain/loss | N/A | nothing posts it; accounts 810/815/820 are dead seeds — a **limitation**, not a bug |

**Conclusion:** Akaunting's *financial statements* are correct for FX. Its two
*detail* reports (General Ledger, Journal) display unconverted foreign amounts under
a base-currency label — a narrow display defect that only manifests once an account
carries mixed/non-base-currency postings. Not a server product we fix here.

### What is actually broken — in akt-cli (our code)
1. **Input:** `resources.py` hard-defaults `currency_rate` to `1` on
   `payment`/`invoice`/`bill`/`journal-entry`. Akaunting's web UI fills the real
   rate (from the currency, kept fresh by the paid Live Currency app); akt does
   not. So a foreign transaction entered via akt is booked at rate 1 → its base
   value is silently wrong.
2. **Reporting:** the `akt-api` companion module's `/balances`
   (`Balances.php:49`) returns raw SQL `SUM(debit), SUM(credit)` with **no
   conversion**. This feeds *every* akt report (`trial-balance`, `report
   profit-loss`, `report balance-sheet`, `balance`) and `verify`. So akt's reports
   sum foreign face values as if base — and `trial-balance` still prints
   `balanced: true` because each transaction's two legs share a currency and net.
   A silent magnitude error, the same class `akt verify` was built to catch.

### The Argentina wrinkle
The company transacts in **ARS**. ECB/Frankfurter (our default rate feed for
majors) **does not publish ARS**, and Argentina has many simultaneous USD/ARS rates
(oficial, blue, MEP/bolsa, CCL, tarjeta, cripto) that diverge widely. Proper input
needs an ARS feed with **historical, by-date** data and a **selectable rate type**.

---

## 2. Decisions (settled)

- **Base currency:** USD (read from `client.setting("default.currency", "USD")`;
  design keeps a cross-via-USD path for a non-USD base, but USD is the live case).
- **Rate feeds (keyless, no API key):**
  - Majors (~30 ECB currencies): **Frankfurter** — latest + historical by date.
  - **ARS:** historical by date via **ArgentinaDatos**
    (`GET /v1/cotizaciones/dolares/{casa}/{YYYY/MM/DD}` → `{compra, venta}`);
    latest via **dolarapi.com** (`GET /v1/dolares/{casa}`).
- **ARS defaults:** casa = `bolsa` (MEP); price = **midpoint** `(compra+venta)/2`.
  Both overridable (`--ars-casa`/`AKT_FX_ARS_CASA`, `--ars-side`/`AKT_FX_ARS_SIDE`).
- **Auto-fill ON** for foreign currencies; `--currency-rate` overrides,
  `--rate-date` pins the date, `--no-auto-rate` opts out.
- **Hard-fail** (never fall back to 1) when a rate can't be resolved: clear,
  actionable message pointing at `--currency-rate`.
- **Mirror Akaunting exactly; pull from the installation.** All base-currency math
  moves into the `akt-api` module reusing Akaunting's own traits/models; akt-cli is
  a thin renderer that does no FX arithmetic of its own. The only unavoidable local
  step is constructing the raw rate from the external feed (Akaunting has no
  historical FX data). See memory `akt-mirror-akaunting-server-side`.
- **`akt ledger` labeling:** mirror General Ledger's numbers (amounts **as
  posted**, i.e. foreign) but add a `currency` column and a converted column, so it
  is unambiguous rather than silently mislabeled. (Deliberate deviation from a
  literal mirror — the only one.)

---

## 3. Architecture

Two workstreams, sequenced. Workstream 1 (input) is the first delivery and a
prerequisite for correct reporting (reports are only right if stored `currency_rate`
is right). Each workstream gets its own implementation plan.

### Workstream 1 — FX input (rate resolution)

**New module `src/akt/fx.py`.** Provider seam, hub currency = USD.

```
class RateProvider(Protocol):
    def usd_per(self, foreign_code: str, on: date) -> Decimal | None: ...
        # foreign units per 1 USD, or None if unsupported

class FrankfurterProvider:   # ECB majors, latest + historical by date
class ArgentinaProvider:     # ARS only: ArgentinaDatos (historical) + dolarapi (latest)
                             # honours casa + side (mid|venta|compra)

def resolve_rate(client, foreign_code, on, *, ars_casa, ars_side) -> Decimal:
    # 1. default = client.setting("default.currency","USD")
    # 2. if foreign_code == default -> Decimal(1)
    # 3. route by currency: ARS -> ArgentinaProvider; else FrankfurterProvider
    # 4. currency_rate = foreign_per_USD  (USD base). Non-USD base: cross via USD.
    # 5. raise FxError(actionable msg) if unresolved
```

- **Direction is load-bearing and unverified live** → a test asserts a EUR record
  in a USD book yields `currency_rate ≈ 0.9x` (not its inverse), and an ARS record
  yields ~ the ARS/USD figure. Exact Frankfurter host/params pinned during
  implementation behind one live smoke + this direction assertion.
- **Cache:** `~/.cache/akt/fx/` (XDG-aware), keyed by
  `(provider, casa, side, from, to, date)`. Historical (date < today) cached
  permanently; latest/today not cached.
- **Config/env:** `AKT_FX_ARS_CASA`, `AKT_FX_ARS_SIDE`, per-provider base-URL
  overrides for mocking (`AKT_FX_FRANKFURTER_URL`, `AKT_FX_ARGENTINADATOS_URL`,
  `AKT_FX_DOLARAPI_URL`), `AKT_FX_DISABLE` (offline → foreign w/o explicit rate errors).
- Uses `requests` (already a dependency). No new deps.

**Auto-fill integration.** One shared helper wired into `build_payment`,
`build_document_create`, `build_journal_create` (+ their update builders):
- date source = `issued_at` (documents) / `paid_at` (payments, journals),
  overridable by `--rate-date`.
- if `currency_code == default` → `currency_rate = 1`, no network call.
- if explicit `--currency-rate` → use it (no fetch).
- else `currency_rate = resolve_rate(...)`; on failure raise (no silent 1).
- Removes the existing silent `or 1` for the foreign case.
- New flags added in `registry.py`/`cli.py`: `--rate-date`, `--ars-casa`,
  `--ars-side`, `--no-auto-rate` on the four transaction nouns.

**akt does no base-amount math.** It only sets `currency_code` + `currency_rate`;
Akaunting computes base on read. Any base figure akt *shows* (e.g. a create
confirmation, `akt fx --amount`) is obtained from the installation via the new
convert endpoint (below), never computed in Python.

**New command `akt fx`.**
`akt fx <CODE> [--on DATE] [--amount N] [--to CODE] [--ars-casa X] [--ars-side X] [--json]`
— prints the rate (CODE-per-USD and inverse) and, with `--amount`, the converted
value (via `/api/akt/convert`). Sanity/scripting aid.

**New `akt-api` endpoint `GET /api/akt/convert`.**
Params `amount`, `currency_code`, `currency_rate` → returns
`Currencies::convertToDefault(...)` (Akaunting's own trait). Lets previews mirror
Akaunting exactly.

### Workstream 2 — reporting parity (mirror Akaunting, server-side)

**`akt-api` `/balances` rework.** Replace the raw SQL `SUM(debit/credit)` with
Akaunting-native conversion, preserving the existing orphan-exclusion
(`whereHasMorph('*')`) and soft-delete semantics:
- For each account, hydrate date-windowed ledgers with `->with('ledgerable')`,
  apply `castDebit()/castCredit()` (the `DefaultCurrency` cast) per leg — exactly
  the loop `Account::calculateBalance` uses (`Traits/Accounts.php:184-186`) — and
  accumulate **converted `debit` and `credit` separately**. Return converted
  `debit`/`credit` per account, **preserving the current `/balances` response
  shape** (so `reports.py` keeps its `debit − credit` net; only the numbers become
  base-currency). Reuse `calculateBalance`/model methods directly if drivable
  headlessly; else replicate the exact cast loop so the *conversion math* is
  Akaunting's own.
- Trial balance / P&L / balance sheet then build from these exactly as today —
  they already reduce to a per-account net and split by sign
  (`TrialBalance.php:103-114`).
- Performance: per-leg casting loads each ledgerable (Akaunting accepts this).
  Eager-load; note chunking as a future optimization.

**Account classification from the installation.** Extend the akt-api payload so each
account carries its `class_id` (Assets/Liabilities/Expenses/Income/Equity) sourced
from `double_entry_classes`/`double_entry_types` — so `reports.py` can **delete its
hardcoded `TYPE_ID_CLASS` map** (`reports.py:12-16`) and stop guessing class locally.

**`/api/akt/ledgers` enrichment.** Add per-row `currency_code` (from the ledgerable)
and converted `debit`/`credit` alongside the raw as-posted values, so `akt ledger`
can mirror General Ledger's numbers while labeling currency honestly.

**akt-cli consumers become renderers.**
- `reports.py`: consume converted net balances + `class_id` from akt-api; build TB /
  P&L / BS from already-base, already-classified figures. Keep rounding/formatting.
  Drop local FX assumptions and the class map.
- `verify.py`: mis-posting / silent-posting / opposite-class detection operates on
  **converted (base)** amounts; thresholds are base-currency. Confirm the existing
  detections still hold under multi-currency.
- `ledger` command: add `currency` + converted columns.

---

## 4. Component boundaries

- `fx.py` — pure rate acquisition + caching. Input: currency, date, ars options.
  Output: a `Decimal` rate. No knowledge of transactions or Akaunting bodies.
  Testable in isolation with a fake HTTP layer.
- `resources.py` builders — call one `resolve_currency_rate(...)` helper; unchanged
  otherwise. The helper is the only new coupling point to `fx.py`.
- `akt-api` (PHP) — owns all conversion + classification, reusing Akaunting models.
  Endpoints: `/balances` (converted), `/ledgers` (raw + converted + currency),
  `/convert` (amount+rate → base). akt-cli treats these as the source of truth.
- `reports.py`/`verify.py` — formatting/report assembly over server-provided base
  figures. No FX math.

---

## 5. Error handling

- Unresolvable rate (offline, provider down, unsupported currency, future date
  beyond feed) with no explicit `--currency-rate` → **abort the create** with e.g.
  `couldn't resolve EUR→USD for 2025-06-01 (Frankfurter): <reason>; pass
  --currency-rate to set it manually`.
- Unsupported currency on a provider (e.g. an exotic code Frankfurter lacks) →
  same hard-fail, message names the provider and suggests `--currency-rate` +
  notes the provider seam is extensible.
- `akt-api` conversion endpoints degrade gracefully when the module is absent
  (existing pattern: print an install hint), but a foreign posting entered while the
  module is present must never be summed unconverted.

---

## 6. Testing strategy

- **fx.py unit tests** (no network; fake HTTP / monkeypatched `requests`):
  Frankfurter direction + historical-by-date; ARS mid computation; casa/side
  overrides; cache hit/permanence for historical vs no-cache for latest; hard-fail;
  offline mode.
- **Builder tests:** rate filled from resolver across all four nouns; explicit
  `--currency-rate` wins; default currency stays `1` with no fetch; `--rate-date`
  and `--no-auto-rate` behave.
- **akt-api (integration, release.yml against the disposable staging instance):**
  post EUR and ARS transactions, assert `/balances` converted totals match
  Akaunting's own `TrialBalance` for the same data (expected computed from the known
  rate); `/convert` parity with `convertToDefault`; `/ledgers` returns
  currency + converted. Respect the ~60/min throttle (`AKT_THROTTLE`).
- **End-to-end (FX mocked):** `akt payment create --currency-code EUR` →
  stored `currency_rate` correct → `akt trial-balance` shows the converted amount
  matching Akaunting.
- One opt-in **live smoke** for `akt fx` (real Frankfurter + ArgentinaDatos) to
  catch endpoint/direction drift, gated like the existing live tests.

---

## 7. Non-goals (explicit)

- Automatic realized/unrealized **FX gain/loss** posting or period-end
  **revaluation** (Akaunting doesn't; potential future `akt fx-revalue` helper).
- Pushing rates back into Akaunting's `currencies` table (Live Currency's job).
- Fixing Akaunting's own General Ledger / Journal detail-report non-conversion
  (server product, out of our scope; `akt ledger` labels honestly instead).
- Intraday rates or currencies beyond provider coverage (hard-fail + guidance; the
  provider seam is the extension point).

---

## 8. Rollout

1. **Phase 1 — FX input:** `fx.py` + providers + auto-fill + `akt fx` + cache +
   `/api/akt/convert`. Ships correct input immediately.
2. **Phase 2 — reporting parity:** `akt-api` `/balances` conversion + account class
   + `/ledgers` enrichment; `reports.py`/`verify.py`/`ledger` consume server figures.
   Ships correct akt reports.

Each phase: TDD, its own plan, its own review + integration run before merge.
