# Foreign-Currency Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** akt-cli books foreign-currency transactions at the correct historical rate (fetched from ECB/Argentina feeds) and reports them in base currency by mirroring Akaunting's own conversion logic server-side.

**Architecture:** Phase 1 adds a Python rate resolver (`fx.py`) that auto-fills `currency_rate` on the four transaction builders. Phase 2 makes the `akt-api` PHP module return base-currency-converted figures (reusing Akaunting's `castDebit/castCredit`) plus account classification, so `reports.py`/`verify.py` become thin renderers. akt-cli does no FX arithmetic; the only local step is constructing the raw rate from an external feed.

**Tech Stack:** Python 3.12+, `requests` (existing dep, no new deps), PHP/Laravel (akt-api module), pytest.

## Global Constraints

- No new Python dependencies — use `requests` (already pinned `>=2.31`).
- Base currency read from `client.setting("default.currency", "USD")` — never hardcoded.
- `currency_rate` convention = foreign units per 1 unit of default currency; base = `amount ÷ currency_rate`.
- Hard-fail (never silently rate=1) when a foreign rate can't be resolved and no `--currency-rate` given.
- Rate feeds are keyless: Frankfurter (majors), ArgentinaDatos (ARS historical) + dolarapi.com (ARS latest).
- ARS defaults: casa `bolsa`, side `mid` = `(compra+venta)/2`.
- Tests marked `unit` must not hit the network (monkeypatch/fixture the HTTP layer).
- Mirror Akaunting exactly; pull computed values from the installation via akt-api (memory `akt-mirror-akaunting-server-side`).

---

## Phase 1 — FX input (rate resolution)

### File structure
- Create `src/akt/fx.py` — rate acquisition + cache + provider seam. One responsibility: given (currency, date, ars opts) → a `Decimal` rate in Akaunting convention.
- Modify `src/akt/resources.py` — one `resolve_currency_rate(...)` helper; call it from `build_payment`, `build_document_create`, `build_journal_create`, and their update builders.
- Modify `src/akt/registry.py` — add `--rate-date`, `--ars-casa`, `--ars-side`, `--no-auto-rate` fields to the four nouns.
- Modify `src/akt/cli.py` — add the `akt fx` command (`_special="fx"`).
- Create `tests/unit/test_fx.py` — provider + resolver + cache tests (no network).
- Modify `tests/unit/test_builders.py` — auto-fill behavior on builders.

### Task 1: fx.py — Frankfurter provider (majors)

**Files:**
- Create: `src/akt/fx.py`
- Test: `tests/unit/test_fx.py`

**Interfaces:**
- Produces: `class FxError(Exception)`; `class FrankfurterProvider` with `usd_per(code: str, on: date | None) -> Decimal | None` (foreign units per 1 USD; `None` if unsupported). HTTP via an injectable `get_json(url)` callable (default wraps `requests`), so tests pass a fake.
- Base URL from `AKT_FX_FRANKFURTER_URL` env (default `https://api.frankfurter.dev/v1`).

- [ ] Step 1: Write failing test: `FrankfurterProvider(get_json=fake).usd_per("EUR", date(2025,6,2))` where `fake` returns `{"base":"USD","date":"2025-05-30","rates":{"EUR":0.88}}` → asserts `Decimal("0.88")`, and the requested URL is `.../2025-06-02?base=USD&symbols=EUR`.
- [ ] Step 2: Run `uv run pytest tests/unit/test_fx.py -v` → FAIL (no module).
- [ ] Step 3: Implement `FrankfurterProvider`: build URL (`/latest` when `on` is None or today, else `/{YYYY-MM-DD}`), query `base=USD&symbols={code}`, return `Decimal(str(rates[code]))` or `None` if absent.
- [ ] Step 4: Run test → PASS.
- [ ] Step 5: Add a direction-guard test: EUR usd_per is < 1 for a USD base (sanity that we return CODE-per-USD, not USD-per-CODE). Commit `feat(fx): Frankfurter provider for major currencies`.

### Task 2: fx.py — Argentina provider (ARS)

**Files:** Modify `src/akt/fx.py`; Test `tests/unit/test_fx.py`

**Interfaces:**
- Produces: `class ArgentinaProvider(casa="bolsa", side="mid", get_json=...)` with `usd_per(code, on) -> Decimal | None` (returns `None` for non-ARS codes). Historical base URL `AKT_FX_ARGENTINADATOS_URL` (default `https://api.argentinadatos.com/v1`); latest base URL `AKT_FX_DOLARAPI_URL` (default `https://dolarapi.com/v1`).
- `side` ∈ {`mid`,`venta`,`compra`}; `mid` = `(compra+venta)/2`.

- [ ] Step 1: Write failing tests: historical `usd_per("ARS", date(2025,6,2))` with fake returning `{"compra":1150,"venta":1200}` from `.../cotizaciones/dolares/bolsa/2025/06/02` → `Decimal("1175")` (mid); `side="venta"` → `1200`; non-ARS code → `None`; latest (on=None) hits `.../dolares/bolsa`.
- [ ] Step 2: Run → FAIL.
- [ ] Step 3: Implement: reject non-ARS; pick historical vs latest by `on`; format date `YYYY/MM/DD`; compute side from compra/venta as `Decimal`.
- [ ] Step 4: Run → PASS. Commit `feat(fx): Argentina ARS provider (ArgentinaDatos + dolarapi)`.

### Task 3: fx.py — resolver + router + cache

**Files:** Modify `src/akt/fx.py`; Test `tests/unit/test_fx.py`

**Interfaces:**
- Produces: `resolve_rate(default_code: str, foreign_code: str, on: date | None, *, ars_casa="bolsa", ars_side="mid", cache_dir=None, providers=None) -> Decimal`. Returns `Decimal(1)` if `foreign_code == default_code`. Routes ARS→ArgentinaProvider else FrankfurterProvider. USD hub: for `default_code=="USD"`, rate = `provider.usd_per(foreign_code, on)`. For a non-USD default, cross: `usd_per(foreign)/usd_per(default)`. Raises `FxError` (actionable message naming the provider + suggesting `--currency-rate`) when a provider returns `None` or HTTP fails.
- Cache: historical dates (`on < today`) memoized to `{cache_dir}/{provider}-{casa}-{side}-{from}-{to}-{date}.json`; latest/today never cached. `cache_dir` default `~/.cache/akt/fx` (XDG `XDG_CACHE_HOME` aware). `AKT_FX_DISABLE=1` → skip network, raise `FxError`.

- [ ] Step 1: Failing tests: same-currency → `Decimal(1)` with no provider call; EUR routes to Frankfurter; ARS routes to Argentina; provider `None` → `FxError`; historical result is cached (second call makes no HTTP call — assert fake call count); `AKT_FX_DISABLE` raises.
- [ ] Step 2: Run → FAIL.
- [ ] Step 3: Implement router, USD-hub/cross logic, file cache, disable flag.
- [ ] Step 4: Run → PASS. Commit `feat(fx): rate resolver with routing + historical cache`.

### Task 4: auto-fill on the four transaction builders

**Files:** Modify `src/akt/resources.py`; Test `tests/unit/test_builders.py`

**Interfaces:**
- Consumes: `fx.resolve_rate`.
- Produces: `resolve_currency_rate(client, ns, *, currency_code, on_date) -> float` helper in `resources.py`. Logic: if explicit `ns.currency_rate` → return it; if `currency_code == client.setting("default.currency","USD")` → `1`; if `getattr(ns,"no_auto_rate",False)` → `1`; else `float(fx.resolve_rate(default, currency_code, rate_date, ars_casa=..., ars_side=...))` where `rate_date = ns.rate_date or on_date`.
- Wire into `build_document_create` (on_date=issued_at), `build_payment` (paid_at), `build_journal_create` (paid_at), replacing the `getattr(ns,"currency_rate",None) or 1` expressions. Update builders keep the stored rate unless `--currency-rate` given.

- [ ] Step 1: Failing tests (monkeypatch `fx.resolve_rate` to return `Decimal("0.9")` and stub `client.setting` → "USD"): a EUR payment gets `currency_rate == 0.9`; explicit `--currency-rate 0.5` wins (no resolver call); a USD payment gets `1` with no resolver call; `--no-auto-rate` on EUR gives `1`.
- [ ] Step 2: Run → FAIL.
- [ ] Step 3: Implement `resolve_currency_rate` + wire into the three create builders (+ update builders where a currency change is provided).
- [ ] Step 4: Run `uv run pytest tests/unit -v` → PASS. Commit `feat(fx): auto-fill currency_rate on transaction input`.

### Task 5: CLI flags + `akt fx` command + `/api/akt/convert` client

**Files:** Modify `src/akt/registry.py`, `src/akt/cli.py`; Test `tests/unit/test_fx.py`

**Interfaces:**
- Add fields `rate-date`, `ars-casa`, `ars-side`, and flag `no-auto-rate` to the payment/document/journal nouns in `registry.py`.
- Add `akt fx <CODE> [--on DATE] [--amount N] [--to CODE] [--ars-casa X] [--ars-side X] [--json]` → `_special="fx"`. Prints rate (CODE-per-USD + inverse) and, with `--amount`, the converted value obtained from `GET akt-api/convert?amount=&currency_code=&currency_rate=` when the module is present, else computed as `amount/rate` with a note.

- [ ] Step 1: Failing test: `_run_special("fx", ...)` for `EUR --on 2025-06-02` (monkeypatched resolver) emits a dict with `code`, `rate`, `inverse`, `on`.
- [ ] Step 2: Run → FAIL.
- [ ] Step 3: Implement flags + `fx` handler.
- [ ] Step 4: Run → PASS. Commit `feat(fx): akt fx helper + rate-date/ars flags`.

---

## Phase 2 — reporting parity (mirror Akaunting, server-side)

### File structure
- Modify `akt-api/Http/Controllers/Api/Balances.php` — convert per-leg via Akaunting's cast; return converted debit/credit (same response shape).
- Create `akt-api/Http/Controllers/Api/Meta.php` + `akt-api/Http/Controllers/Api/Convert.php` — type→class map and `convertToDefault` wrapper.
- Modify `akt-api/Http/Controllers/Api/Ledgers.php` + `akt-api/Http/Resources/Ledger.php` — add `currency_code` + converted debit/credit per row.
- Modify `akt-api/Routes/api.php` — register `akt-api/account-types` and `akt-api/convert`.
- Modify `src/akt/reports.py` — accept an injected `type_class` map; drop hardcoded `TYPE_ID_CLASS`.
- Modify `src/akt/cli.py` — `_fetch_balances` fetches the type→class map; `ledger` shows currency + converted columns.
- Modify `src/akt/verify.py` — base-currency amounts already come converted; confirm thresholds.
- Modify `tests/unit/test_reports.py`, `tests/unit/test_verify.py`.

### Task 6: Balances returns converted debit/credit (PHP)

**Files:** Modify `akt-api/Http/Controllers/Api/Balances.php`

**Interfaces:** Same route/response shape (`[{account_id, debit, credit}]`) but values are base-currency.

- [ ] Step 1: Replace the raw `selectRaw('SUM(debit)...')` aggregate with: hydrate the filtered `Ledger` rows `->with('ledgerable')`, per row call `$ledger->castDebit(); $ledger->castCredit();` (the DoubleEntry `DefaultCurrency` cast — same path as `Account::calculateBalance`), accumulate converted `debit`/`credit` per `account_id`. Keep the `whereHasMorph('ledgerable','*')` orphan filter and date-window filters. Return one row per account with converted totals.
- [ ] Step 2: (verified in the integration suite — Task 10 — since PHP needs the live instance) Add an integration assertion.
- [ ] Step 3: Commit `fix(akt-api): convert ledger legs to base currency in /balances (mirror Akaunting)`.

### Task 7: type→class map + convert endpoints (PHP)

**Files:** Create `akt-api/Http/Controllers/Api/Meta.php`, `Convert.php`; Modify `akt-api/Routes/api.php`

**Interfaces:**
- `GET akt-api/account-types` → `{data:[{type_id, class_id, class_name}]}` from `double_entry_types` joined to `double_entry_classes`.
- `GET akt-api/convert?amount=&currency_code=&currency_rate=` → `{data:{base}}` using a class that `use`s Akaunting's `App\Traits\Currencies` and calls `convertToDefault($amount,$currency_code,$currency_rate)`.

- [ ] Step 1: Implement both controllers + routes (gate on `permission:read-double-entry-chart-of-accounts`, no `parent::__construct`, per the existing pattern).
- [ ] Step 2: Commit `feat(akt-api): account-types class map + convert endpoint`.

### Task 8: Ledgers rows carry currency + converted (PHP)

**Files:** Modify `akt-api/Http/Controllers/Api/Ledgers.php`, `akt-api/Http/Resources/Ledger.php`

**Interfaces:** Each ledger row additionally exposes `currency_code`, `debit_converted`, `credit_converted`. For the `akt ledger` path (single account, date-bounded), load rows via the Eloquent `Ledger` model with `->with('ledgerable')` and cast for the converted columns; raw `debit`/`credit` stay as posted.

- [ ] Step 1: Implement; keep existing filters/params.
- [ ] Step 2: Commit `feat(akt-api): expose currency + converted amounts on /ledgers`.

### Task 9: reports.py + cli + verify consume server figures

**Files:** Modify `src/akt/reports.py`, `src/akt/cli.py`, `src/akt/verify.py`; Test `tests/unit/test_reports.py`, `tests/unit/test_verify.py`

**Interfaces:**
- `reports.account_class(type_id, type_class: dict[int,int])` — no hardcoded map; `type_class` injected from the new endpoint. `_fetch_balances` builds `type_class` from `GET akt-api/account-types` and enriches `accts_by_id` with `class_id`; `build_profit_loss`/`build_balance_sheet` read `class_id` off the account dict.
- `cli.py` `ledger` handler columns become `["issued_at","currency","debit","credit","debit_converted","credit_converted","entry_type"]`.

- [ ] Step 1: Update `test_reports.py` to pass a `type_class` map (or `class_id` on accounts) instead of relying on the removed constant; add a case with a converted (foreign-origin) balance to prove reports use server numbers verbatim.
- [ ] Step 2: Run → FAIL.
- [ ] Step 3: Implement: delete `TYPE_ID_CLASS`/rework `account_class`; thread `class_id` through `reports.py`; update `_fetch_balances` + `ledger` columns; verify.py continues to consume converted amounts (already base after Task 6).
- [ ] Step 4: Run `uv run pytest tests/unit -v` → PASS. Commit `refactor(reports): render server-converted, server-classified figures`.

### Task 10: Integration coverage (live)

**Files:** Modify `tests/integration/test_live.py` (+ a new `test_fx_live.py` opt-in)

- [ ] Step 1: Integration test: create a EUR transaction with an explicit `--currency-rate`, assert `/balances` returns its converted (÷rate) contribution and that `akt trial-balance` matches Akaunting's own for the same data; `/convert` parity.
- [ ] Step 2: Opt-in live smoke for `akt fx EUR` + `akt fx ARS` (real feeds), gated like existing live tests.
- [ ] Step 3: Commit `test(integration): multi-currency balances + fx smoke`.

---

## Release
- Bump `pyproject.toml` version `0.10.1` → `0.11.0`.
- Update `README.md` (foreign-currency section: `akt fx`, auto-fill, ARS casa/side, the reporting-parity note) and `akt-api/README.md` (new endpoints).
- Merge `feat/foreign-currency` → `main`; publish GitHub Release `v0.11.0` (runs integration + OIDC PyPI publish).

## Self-review notes
- Spec coverage: input auto-fill (T4/T5), providers incl. ARS (T1-T3), hard-fail (T3), akt fx + convert (T5/T7), /balances conversion (T6), class-from-install (T7/T9), ledger labeling (T8/T9), verify base-currency (T9), tests (all), non-goals unchanged. ✓
- Type consistency: `usd_per`/`resolve_rate`/`resolve_currency_rate`/`account_class(type_id,type_class)` names used consistently across tasks. ✓
- PHP tasks (6-8) are verified in the live integration suite (Task 10), not local unit tests, because they require a running Akaunting instance.
