# Contributing to `akt`

Notes for developing, testing, and releasing the CLI. If you only want to *use*
`akt`, see the [README](README.md).

## Testing

Tests are split in two:

* **`tests/unit/`** — offline tests for the body builders and arg parsing. No
  network. This is what CI runs by default and what gates pull requests.
* **`tests/integration/`** — drive the real `akt` CLI against a live Akaunting
  instance, exercising the full surface (contacts, items, bill → payment →
  paid, transfers, …). Every record they create is deleted on teardown — even
  on failure — so no invoices, bills or payments are left behind.

```bash
uv run pytest tests/unit                 # fast, offline (default)
uv run pytest                            # integration tests auto-skip without creds

# Run integration tests against a deployment:
AKT_BASE_URL=https://accounting.example.com \
AKT_EMAIL=admin@example.com \
AKT_PASSWORD=… \
uv run pytest tests/integration -v
```

> Invoice creation is `xfail`-ed when the host's plan-limit gate is active (see
> the README's Akaunting gotchas); the rest of the suite must pass.

## CI / CD

* **CI** (`.github/workflows/ci.yml`) runs on every push and PR: unit tests +
  coverage, uploaded to [Codecov](https://codecov.io/gh/AsyncAlchemist/akt-cli).
* **Release** (`.github/workflows/release.yml`) runs the live integration suite
  on published releases (and via *Run workflow*). Connection details come from
  GitHub Actions **secrets** (`AKT_BASE_URL`, `AKT_EMAIL`, `AKT_PASSWORD`) — they
  are never committed and are masked in logs.
* **Publish** (`.github/workflows/publish.yml`) builds the sdist + wheel and
  uploads them via **Trusted Publishing (OIDC)** — no API token is stored.
  *Run workflow* publishes to **TestPyPI**; a published release publishes to
  **PyPI**.

## Releasing to PyPI

One-time setup — add a *pending publisher* on each index
(Account → Publishing → *Add a pending publisher*) with:

| Field | TestPyPI | PyPI |
|-------|----------|------|
| Project Name | `akt-cli` | `akt-cli` |
| Owner | `AsyncAlchemist` | `AsyncAlchemist` |
| Repository name | `akt-cli` | `akt-cli` |
| Workflow name | `publish.yml` | `publish.yml` |
| Environment name | `testpypi` | `pypi` |

Then:

* **Verify** — *Actions → Publish (PyPI) → Run workflow* uploads the current
  version to TestPyPI.
* **Release** — bump `version` in `pyproject.toml`, push, and publish a GitHub
  Release. That runs the live integration suite and publishes to PyPI.

## Reference: the Akaunting source

`akt` is reverse-engineered from a running Akaunting instance, and the server
source is checked out alongside this repo as the ground truth for API behavior —
route definitions, controller/FormRequest validation, the AJAX envelope, and how
the Double-Entry module posts and aggregates ledgers. **Read it before guessing**
when a response shape, validation rule, or posting side-effect is unclear.

* **Akaunting core** (Laravel app) — the `akaunting-src` symlink at the repo root
  (→ `../accounting/source`). Routes live in `routes/api.php`; controllers under
  `app/Http/Controllers/Api/`; API response shapes under `app/Http/Resources/`.
* **Double-Entry module** — the sibling `akaunting-modules/DoubleEntry/`. Notable
  paths: `Routes/api.php` (only `journal-entry` + read-only `chart-of-accounts`
  are published on `/api` — there is **no** ledger endpoint), `Models/Ledger.php`
  and `Models/AccountBank.php` (the bank↔GL-account link), `Observers/` (how each
  transaction posts its double-entry ledgers), and `Reports/` (`TrialBalance`,
  `BalanceSheet`, `GeneralLedger`, `JournalReport` — how the module aggregates
  ledgers into the reports we mirror).

> The symlink target is machine-relative; if it dangles, the checkout lives at
> `…/accounting/{source,akaunting-modules/DoubleEntry}` next to this repo's parent.

## Project layout

The code is small and declarative:

| file            | purpose                                                  |
|-----------------|----------------------------------------------------------|
| `config.py`     | credential resolution (flags / env / dotenv)             |
| `client.py`     | HTTP, auth, company scoping, pagination, retries         |
| `resources.py`  | field specs + body builders (documents, payments)        |
| `registry.py`   | the concrete list of resources and their columns         |
| `commands.py`   | generic list/get/create/update/delete/toggle handlers    |
| `cli.py`        | argparse wiring and entrypoint                            |
| `output.py`     | JSON / table rendering                                    |
