# CI integration instance runbook

`release.yml` runs the full `tests/integration` suite against a **dedicated,
disposable Akaunting instance** — never a shared/production one. This is a
maintainer runbook for standing that instance up and keeping it healthy. The
instance's URL and throwaway admin credentials live only in the repo secrets
`AKT_BASE_URL` / `AKT_EMAIL` / `AKT_PASSWORD`; nothing here points at real books.

## What the instance is

A stock **Akaunting 3.1.x** install on its own subdomain + its own database,
plus the paid **Double-Entry** app and the in-repo **akt-api** companion module,
seeded to a single known company (`company_id = 1`) with a throwaway admin. It
holds no real data, so `coa sync` chart-of-accounts mutations and the suite's
create/delete churn are harmless.

## Provisioning (once)

1. **PHP 8.1+** for both the web vhost and CLI (Akaunting 3.x refuses < 8.1).
2. **Database + user**, granted on that DB only.
3. **Install Akaunting**: extract the official prebuilt release
   (`Akaunting_3.1.x-Stable.zip`, which bundles `vendor/` + compiled assets — no
   composer/npm needed) into the docroot, then run the headless installer:
   ```
   php artisan install --db-host=… --db-name=… --db-username=… --db-password=… \
     --company-name=… --company-email=… --admin-email=… --admin-password=… \
     --locale=en-GB -n
   ```
4. **Point `.env` at the instance** and quiet the noisy defaults:
   ```
   APP_URL=https://<your-subdomain>
   FIREWALL_ENABLED=false      # the CLI-detected "bot protection"; off for CI
   MODEL_CACHE_ENABLED=false   # avoids stale-read flakiness on rapid delete/re-read
   MAIL_MAILER=log
   ```
5. **Double-Entry**: copy the paid module into `modules/DoubleEntry`, enable it
   (upsert a `modules` row for `alias='double-entry', company_id=1, enabled=1`),
   run `php artisan migrate --force`, then seed it:
   `php artisan company:seed 1 --class="Modules\DoubleEntry\Database\Seeds\Install"`.
   (Your Akaunting licence covers installing an app you've purchased on as many of
   your own installations as you like; the module does no runtime licence check.)
6. **Run Double-Entry's `CopyData` install job.** DE posts a transaction's ledger
   legs only if that transaction's bank has a `double_entry_account_bank` row (see
   `Observers/Banking/Transaction::created` — it bails when the bank isn't mapped).
   DE normally maps every existing bank (and backfills the ledger for pre-existing
   transactions/invoices/bills) via `Jobs\Install\CopyData`, dispatched by its
   `ModuleEnabled` / `FinishInstallation` listeners — but only when DE is enabled
   through Akaunting's module-enable **event**. Enabling it via a direct `modules`-row
   upsert (as in step 5) skips that, so the **default "Cash" bank, created at install
   before DE, is never mapped** — every payment on it then silently posts nothing,
   which breaks `payment --split` (its fan-out needs the one item leg). Run the job
   explicitly with company context — the faithful fix, not a hand-rolled mapping:
   ```
   php artisan tinker --execute='
     \App\Models\Common\Company::find(1)->makeCurrent();
     dispatch_sync(new \Modules\DoubleEntry\Jobs\Install\CopyData());'
   ```
   `akt verify` now flags this (unmapped banks + transactions that posted no
   ledger legs), so you can run it to confirm the mapping took.
7. **akt-api**: `scripts/deploy-akt-api.sh` rsyncs + enables it — or copy it to
   `modules/AktApi`, upsert its `modules` row, and clear caches.
8. **Skip the setup wizard** (it 302-redirects every route until done):
   upsert the setting `wizard.completed = 1` for `company_id = 1`, then
   `php artisan cache:clear`.
9. **Set the repo secrets** `AKT_BASE_URL` / `AKT_EMAIL` / `AKT_PASSWORD`.

## Gotchas (learned the hard way)

- **The PHP version is selected by an `AddHandler` block in the docroot
  `.htaccess`.** Extracting Akaunting overwrites `.htaccess`, silently reverting
  HTTP to the account-default PHP (Akaunting then serves a bare *"use PHP 8.1+"*
  error with 200). **Re-apply the vhost PHP version after any re-extract** so the
  handler block is rewritten.
- **API rate limit** is ~60 req/min. The suite paces itself via `AKT_THROTTLE`
  (release.yml sets `1.0`); with no throttle it 429s partway through.
- **Invoice creation is plan-gated** without an akaunting.com `apps.api_key`, so
  `test_invoice_flow` is an expected `xfail` on this instance (by design — the
  test detects the gate). Connect an API key to lift it if full invoice coverage
  is wanted.

## Maintenance

- Akaunting **soft-deletes**, so each run leaves orphaned `double_entry_ledger`
  rows. release.yml sweeps them with `akt gc-ledger --apply` after every run.
- If a run is interrupted mid-suite, its records won't be torn down; clear the
  residue (transactions / documents / ledger / `AKT%`-named accounts, categories,
  banks) or the next run's `coa sync`/teardown can trip over it.
