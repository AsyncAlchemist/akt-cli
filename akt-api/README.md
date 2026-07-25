# akt-api — Akaunting companion module for akt-cli

A tiny, **read-only** Akaunting module that exposes the double-entry general
ledger over the API so [`akt-cli`](https://github.com/AsyncAlchemist/akt-cli)
can read what actually posted to the ledger. It unlocks `akt ledger` and
`akt verify`.

Akaunting's DoubleEntry app publishes only `journal-entry` and
`chart-of-accounts` over the API — the `double_entry_ledger` table (where every
transaction's postings live) has no endpoint. This module adds one:

```
GET   /api/akt-api/ledgers            # read GL postings
PATCH /api/akt-api/ledgers/{id}       # recode: repoint one item leg's GL account
POST  /api/akt-api/ledgers/{id}/split # split: fan one item leg into N item legs
```

## What it does

- Registers routes under `/api/akt-api/` (via the core `Route::api('akt-api', …)`
  macro) so they can never collide with core or other modules' routes: a read
  route (`GET .../ledgers`) plus two targeted item-leg writes — **recode**
  (`PATCH .../ledgers/{id}`, repoint one item leg's GL account) and **split**
  (`POST .../ledgers/{id}/split`, fan one item leg out into N item legs so a bank
  transaction can post to several GL accounts, the way an invoice's line items do).
- Reads the `double_entry_ledger` table via the query builder only — it does
  **not** import or modify any DoubleEntry code, so it survives DoubleEntry
  updates. (It couples only to that table's column names.)
- Inherits Akaunting's core `api` middleware for auth: **HTTP Basic** (the same
  admin email/password every other `/api/...` call uses) + `permission:read-api`
  + company scoping. No separate key or token.

### Query parameters

| param | meaning |
|---|---|
| `account_id` | only postings to this GL account id |
| `ledgerable_type` | e.g. `App\Models\Banking\Transaction` |
| `ledgerable_id` | the source record id |
| `entry_type` | `item` (the income/expense leg) or `total` (the bank leg) |
| `issued_from` / `issued_to` | `YYYY-MM-DD` date bounds on `issued_at` |
| `limit` / `page` | pagination (default 100 per page) |

## Requirements

- Akaunting with the paid **Double-Entry** app installed and enabled. This
  module declares `"requires": ["double-entry"]` and will not load without it.

## Install

### Automated (recommended)

Use the repeatable deploy script (`scripts/deploy-akt-api.sh` in the akt repo) —
it rsyncs the module, enables it, clears caches, verifies the endpoint,
health-checks the app, and rolls back on failure:

```bash
AKT_EMAIL=… AKT_PASSWORD=… scripts/deploy-akt-api.sh
```

Config via env vars: `AKT_API_SSH_HOST` (your ssh host/alias, required),
`AKT_API_APP_URL` (your Akaunting base URL, required), `AKT_API_REMOTE=akaunting`,
`AKT_API_COMPANY=1`.

### Manual

1. Copy this directory into your Akaunting install as **`modules/AktApi`** — the
   directory name must be `AktApi` (StudlyCase) so Akaunting autoloads the
   `Modules\AktApi\` namespace:

   ```bash
   rsync -az akt-api/ /path/to/akaunting/modules/AktApi/
   ```

2. Enable it. Akaunting tracks module status per-company in the `modules` DB
   table; the canonical command takes the **alias** (`akt-api`) and a company id.
   **Clear caches first** — Akaunting caches the scanned-modules list, so a
   freshly-copied module is invisible to `module:enable` until the cache is
   flushed:

   ```bash
   php artisan cache:clear && php artisan config:clear && php artisan route:clear
   php artisan module:enable akt-api 1
   php artisan route:clear
   ```

   (Or **Apps → My Apps** in the UI. Or just use the deploy script above.)

> **Why the alias is `akt-api`.** The core `Route::api($alias, …)` macro uses the
> alias as the `/api/<alias>/` URL prefix **and** to build the controller
> namespace as `Modules\<Str::studly($alias)>\…`. `Str::studly('akt-api')` =
> `AktApi`, which matches this module's namespace *and* its install directory
> (`modules/AktApi`). So the alias, the studly name, and the directory all agree.

## Verify

```bash
curl -s -u "$AKT_EMAIL:$AKT_PASSWORD" \
  "$APP_URL/api/akt-api/ledgers?company_id=1&entry_type=item&limit=3"
```

A `200` with a `data` array means it's live. A `404` means the module isn't
enabled (which is exactly what `akt`'s capability probe checks for).

## License

MIT — this module is independent of, and does not redistribute, the DoubleEntry
app; it only reads a database table.
