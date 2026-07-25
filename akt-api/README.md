# akt-api — Akaunting companion module for akt-cli

A tiny, **read-only** Akaunting module that exposes the double-entry general
ledger over the API so [`akt-cli`](https://github.com/AsyncAlchemist/akt-cli)
can read what actually posted to the ledger. It unlocks `akt ledger` and
`akt verify`.

Akaunting's DoubleEntry app publishes only `journal-entry` and
`chart-of-accounts` over the API — the `double_entry_ledger` table (where every
transaction's postings live) has no endpoint. This module adds one:

```
GET /api/akt/ledgers
```

## What it does

- Registers a single read-only route, `GET /api/akt/ledgers`, namespaced under
  `/api/akt/` (via the core `Route::api('akt', …)` macro) so it can never
  collide with core or other modules' routes.
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

Config via env vars (defaults shown): `AKT_API_SSH_HOST=akaunting-host`,
`AKT_API_REMOTE=akaunting`, `AKT_API_COMPANY=1`,
`AKT_API_APP_URL=https://akaunting.example.com`.

### Manual

1. Copy this directory into your Akaunting install as **`modules/AktApi`** — the
   directory name must be `AktApi` (StudlyCase) so Akaunting autoloads the
   `Modules\AktApi\` namespace:

   ```bash
   rsync -az akt-api/ /path/to/akaunting/modules/AktApi/
   ```

2. Enable it. Akaunting tracks module status per-company in the `modules` DB
   table; the canonical command takes the **alias** (`akt`) and a company id,
   then clear caches:

   ```bash
   php artisan module:enable akt 1
   php artisan cache:clear && php artisan config:clear && php artisan route:clear
   ```

   (Or **Apps → My Apps** in the UI.)

> **Why the alias is `akt`, not `akt-api`.** The core `Route::api($alias, …)`
> macro uses the alias *both* as the URL prefix and to resolve the module's
> controller namespace (`module($alias)->getStudlyName()`). So the alias must
> equal the `akt` in `/api/akt/ledgers`; the install directory stays `AktApi`.

## Verify

```bash
curl -s -u "$AKT_EMAIL:$AKT_PASSWORD" \
  "$APP_URL/api/akt/ledgers?company_id=1&entry_type=item&limit=3"
```

A `200` with a `data` array means it's live. A `404` means the module isn't
enabled (which is exactly what `akt`'s capability probe checks for).

## License

MIT — this module is independent of, and does not redistribute, the DoubleEntry
app; it only reads a database table.
