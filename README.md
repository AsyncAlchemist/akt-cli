# akt — Akaunting CLI toolbox

[![PyPI](https://img.shields.io/pypi/v/akt-cli.svg)](https://pypi.org/project/akt-cli/)
[![CI](https://github.com/AsyncAlchemist/akt-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/AsyncAlchemist/akt-cli/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/AsyncAlchemist/akt-cli/graph/badge.svg)](https://codecov.io/gh/AsyncAlchemist/akt-cli)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`akt` drives an [Akaunting](https://akaunting.com) instance entirely from the
command line: full create / read / update / delete for customers, vendors,
items, invoices, bills, payments, accounts, categories, taxes, currencies and
transfers — plus a `raw` escape hatch for any other API endpoint.

Built and tested against Akaunting **3.1.x**; it should work with any 3.x
deployment that exposes the REST API.

## Install

From PyPI (the distribution is `akt-cli`; the command is `akt`):

```bash
uv tool install akt-cli     # installs the `akt` command globally
# or
pip install akt-cli
# or run without installing
uvx --from akt-cli akt --help
```

From a checkout (the project is managed with [uv](https://docs.astral.sh/uv/)):

```bash
uv sync                 # create .venv and install
uv run akt --help       # run without activating
uv tool install .       # install the `akt` command from source
```

## Configuration

`akt` needs a base URL, an admin email + password, and a company id. They are
resolved in this order (first wins):

1. CLI flags: `--base-url`, `--email`, `--password`, `--company` (given **before**
   the subcommand, e.g. `akt --company 2 customer list`).
2. Environment: `AKT_BASE_URL`, `AKT_EMAIL`, `AKT_PASSWORD`, `AKT_COMPANY`,
   `AKT_THROTTLE`.
3. A dotenv file — `$AKT_ENV_FILE`, then `./.env`, then `~/.config/akt/akt.env`.
   Akaunting's own install keys are recognised too: `APP_URL`,
   `AKAUNTING_ADMIN_EMAIL`, `AKAUNTING_ADMIN_PASSWORD`.

Minimal `~/.config/akt/akt.env`:

```ini
AKT_BASE_URL=https://accounting.example.com
AKT_EMAIL=admin@example.com
AKT_PASSWORD=your-password
AKT_COMPANY=1
```

Then:

```bash
uv run akt ping
uv run akt company
```

Authentication is HTTP Basic against your Akaunting admin user.

## Concepts mapped to Akaunting

Akaunting folds several nouns onto shared endpoints; `akt` hides that:

| akt noun   | API endpoint   | notes                                            |
|------------|----------------|--------------------------------------------------|
| `customer` | `contacts`     | contact of type `customer`                        |
| `vendor`   | `contacts`     | contact of type `vendor` (supplier)               |
| `invoice`  | `documents`    | document of type `invoice`                         |
| `bill`     | `documents`    | document of type `bill`                            |
| `payment`  | `transactions` | income (invoice) or expense (bill) transaction    |
| `item`, `account`, `category`, `tax`, `currency`, `transfer` | as named | |

> The `contacts` and `documents` endpoints derive their permission from a
> `search=type:<x>` query param. `akt` injects this automatically — calling them
> raw without it returns `403 necessary access rights`.

## Verbs

Every resource supports:

```
akt <noun> list      [--search 'field:value'] [--all] [--limit N] [--json]
akt <noun> get <id>
akt <noun> create    --field value ...
akt <noun> update <id> --field value ...
akt <noun> delete <id>
akt <noun> enable <id>      # where applicable
akt <noun> disable <id>
```

Output is a table by default; add `--json` (works before or after the verb) for
raw JSON suitable for piping into `jq`.

Three ways to set body fields on create/update:

* typed flags shown by `akt <noun> create --help`
* `--set key=value` (repeatable; values are JSON-coerced, so `--set enabled=0`)
* `--data '<json>'` or `--data @file.json` (merged last, wins over everything)

## Examples

```bash
# Contacts
akt customer create --name "Northwind Traders" --email ar@northwind.com --currency-code USD
akt vendor create   --name "Office Supply Co"  --email billing@osc.com
akt customer list --search 'name:Northwind'
akt customer update 12 --phone "555-2000"
akt customer disable 12

# Items, categories, taxes
akt item create --name "Consulting Hour" --sale-price 150 --purchase-price 0
akt category create --name "Services" --type income
akt tax create --name "Sales Tax" --rate 8.25

# Invoice with line items (totals computed server-side; number auto-generated)
akt invoice create --contact 12 \
    --item 'name=Consulting,price=150,quantity=10,item_id=2' \
    --item 'name=Setup fee,price=500,quantity=1' \
    --status sent

# Record a customer payment against that invoice (amount defaults to amount due)
akt payment create --invoice 34

# Partial payment of a specific amount via bank transfer
akt payment create --invoice 34 --amount 750 \
    --payment-method offline-payments.bank_transfer.2

# Bills and vendor payments work the same way
akt bill create --contact 13 --item 'name=Paper,price=40,quantity=5'
akt payment create --bill 41

# Anything else: raw API access
akt raw GET reports
akt raw POST items --data '{"name":"Ad-hoc","type":"service","sale_price":99}'
akt company
akt settings --search 'key:default.account'
```

## Akaunting gotchas `akt` handles for you

Driving Akaunting's API directly has sharp edges; `akt` papers over these:

* **Type-scoped ACL** — `contacts` and `documents` need `search=type:<x>` on
  *every* verb or the API returns `403 necessary access rights`.
* **Doubled totals** — Akaunting recomputes a document's total from its line
  items and *adds* it to the `amount` you send. `akt` always sends `amount: 0`
  so the server-computed total is authoritative.
* **Item `description`** — line items need a `description` key even when empty,
  or creation 500s with `Undefined array key "description"`.
* **Updates wipe items** — a document update deletes and recreates all line
  items from the request. `akt` resends the existing items on a partial update
  so they aren't lost.
* **Nested payment route** — paying a document must POST to
  `documents/{id}/transactions`; the flat `transactions` endpoint rejects it.
* **Full-replace updates** — Akaunting PUT re-validates required fields, so
  `akt` merges your changes onto the current record.

### Invoice creation may be gated by a plan check

In Akaunting 3.x, `CreateDocument::authorize()` gates **invoice** creation (only
`type == invoice`) behind a call to `api.akaunting.com/plans/limits` using the
`apps.api_key` setting. If that key is unset or the host can't reach
`api.akaunting.com`, invoice creation fails closed with
`500 Not able to create a new user` — in the **web UI too**, not just `akt`.
Bills, payments, contacts, items and transfers are unaffected. Fix it by setting
a valid `apps.api_key` (and allowing outbound HTTPS to `api.akaunting.com`).

## Host bot-protection / throttling

Some hosts (e.g. cPanel with Imunify360) greylist an IP that issues a burst of
automated requests, returning an `Access denied by … bot-protection` page or
timing out. `akt` retries throttle/WAF responses with backoff, and
`--throttle SECONDS` (or `AKT_THROTTLE`) enforces a minimum gap between calls —
use `--throttle 1` for bulk work. A durable fix is to whitelist your IP in the
host firewall.

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
> above); the rest of the suite must pass.

### CI / CD

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

### Releasing to PyPI

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

## License

[MIT](LICENSE) © AsyncAlchemist
