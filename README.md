<div align="center">

# akt — Akaunting CLI toolbox
### Drive your Akaunting accounting instance entirely from the command line

>`akt` gives you full create / read / update / delete for customers, vendors, items, invoices, bills, payments, banks, categories, taxes, currencies and transfers — plus double-entry journal entries and the chart of accounts, and a `raw` escape hatch for any other endpoint. Built and tested against [Akaunting](https://akaunting.com) **3.1.x**; works with any 3.x deployment that exposes the REST API.

[![PyPI Version](https://img.shields.io/pypi/v/akt-cli.svg?style=flat-square)](https://pypi.org/project/akt-cli/)
[![Tests](https://img.shields.io/github/actions/workflow/status/AsyncAlchemist/akt-cli/ci.yml?branch=main&label=tests&style=flat-square)](https://github.com/AsyncAlchemist/akt-cli/actions/workflows/ci.yml)
[![Integration](https://img.shields.io/github/actions/workflow/status/AsyncAlchemist/akt-cli/release.yml?label=integration&style=flat-square)](https://github.com/AsyncAlchemist/akt-cli/actions/workflows/release.yml)
[![Publish](https://img.shields.io/github/actions/workflow/status/AsyncAlchemist/akt-cli/publish.yml?label=publish&style=flat-square)](https://github.com/AsyncAlchemist/akt-cli/actions/workflows/publish.yml)
[![Codecov](https://codecov.io/gh/AsyncAlchemist/akt-cli/graph/badge.svg)](https://codecov.io/github/AsyncAlchemist/akt-cli)

[![GitHub Release](https://img.shields.io/github/v/release/AsyncAlchemist/akt-cli?style=flat-square)](https://github.com/AsyncAlchemist/akt-cli/releases)
[![Downloads](https://img.shields.io/pypi/dm/akt-cli.svg?style=flat-square&label=downloads)](https://pypi.org/project/akt-cli/)
[![Python Version](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue.svg?style=flat-square)](https://pypi.org/project/akt-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg?style=flat-square)](LICENSE)

</div>

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
| `journal-entry` | `journal-entry` | double-entry general-ledger entry (module)     |
| `account`  | `chart-of-accounts` | GL accounts (general ledger) — read via API, CRUD via web |
| `bank`     | `accounts`     | bank / cash accounts (the money, not the GL)      |
| `item`, `category`, `tax`, `currency`, `transfer` | as named | |

> `journal-entry` and `account` require the **Double-Entry** module
> installed on the instance. The module publishes chart-of-accounts read-only on
> the `/api` surface (index/show); its create/update/delete live only on the
> session/CSRF **web** route. `akt account` gives you the full verb set
> anyway — `list`/`get` hit `/api`, while `create`/`update`/`delete` transparently
> drive the web CRUD with your admin session (the same mechanism
> `download-attachment` already uses).

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

Bills, invoices and payments additionally support **attachments** (scanned bills,
receipts, PDFs):

```
akt <noun> create ... --attachment ./file.pdf        # repeatable; upload on create
akt <noun> update <id> --attachment ./file.pdf       # attach to an existing record
akt <noun> update <id> --remove-attachment           # clear existing attachment(s)
akt <noun> attachments <id>                           # list attached files (id, name, size)
akt <noun> download-attachment <id> [--out DIR] [--media-id ID]
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

# Bank / cash accounts (the money side — see the COA section below for GL accounts)
akt bank create --name "Business Checking" --number 1001 --currency-code USD
akt bank list

# Open a book with a prior-period opening balance. The Double-Entry module
# auto-posts an opening-balance journal entry dated to the account's creation
# date; --opening-balance-date re-dates it to the period boundary so it lands in
# the right financial year. Needs a positive --opening-balance.
akt bank create --name "Business Checking" --number 1001 --currency-code USD \
    --opening-balance 5000 --opening-balance-date 2024-12-31
# Re-date the opening entry of an existing account:
akt bank update 3 --opening-balance-date 2024-12-31

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

# Attachments: upload the source PDF/scan and fetch it back later
akt bill create --contact 13 --item 'name=Paper,price=40,quantity=5' \
    --attachment ./supplier-bill.pdf
akt payment update 57 --attachment ./receipt.pdf   # attach to an existing payment
akt bill attachments 41                             # list attached files
akt bill download-attachment 41 --out ./downloads   # save to disk
akt payment update 57 --remove-attachment           # clear attachments

# Double-entry general ledger (requires the Double-Entry module)
akt account list                                       # read the chart of accounts
akt account get 12

# Build the chart of accounts as code (create/update/delete run via the web
# session; type-id is the double-entry account-type id — copy it from an
# existing account's `type_id`)
akt account create --name "Cash on Hand" --code 1010 --type-id 6
akt account create --name "Petty Cash" --code 1011 --type-id 6 --parent-id 12
akt account update 12 --code 1000 --description "Operating cash"
akt account delete 12

# Post a balanced journal entry (>= 2 lines; debits must equal credits;
# journal number auto-generated, basis defaults to accrual)
akt journal-entry create --description "Owner capital contribution" \
    --item 'account=105,debit=5000' \
    --item 'account=300,credit=5000'
akt journal-entry list
akt journal-entry update 4 --description "Corrected memo"
akt journal-entry create --description "Vendor bill accrual" --basis accrual \
    --item 'account=510,debit=250' --item 'account=200,credit=250' \
    --attachment ./invoice.pdf

## COA config: link categories and accounts (Xero-style)

Akaunting keeps *categories* (required on every transaction) separate from the
double-entry *chart of accounts*. Point akt at a COA config and it keeps them in
lockstep: one list to maintain (accounts), a 1:1 mirror of categories generated
from it, and `payment create` coded by `--account` — with the mirror category
filled in automatically.

akt finds the config at `--coa FILE` → `AKT_COA_FILE` → `./coa.toml` →
`~/.config/akt/coa.toml`. Minimal schema (extra keys are ignored):

```toml
[[account]]
code    = 400
name    = "API Subscription Revenue"
type_id = 13            # DoubleEntry account type; income/expense class -> category type
# optional: category = "Revenue"   (override the mirror name)
# optional: mirror   = false        (skip mirroring, e.g. bank/AR/AP accounts)
```

```bash
akt coa diff                 # preview: accounts/categories to create or rename
akt coa sync                 # apply (create + rename; idempotent)
akt coa sync --prune         # also DISABLE accounts/categories absent from the config

# code a transaction by GL account — akt auto-fills the mirror category:
akt payment create --type expense --bank 1 --amount 120 --account 628
# ...or by category name — akt reverse-fills the matching GL account:
akt payment create --type expense --bank 1 --amount 120 --category "Cloud Hosting"
```

`--account`/`--category` are two ends of the same coin: give either and akt fills
the other from the COA config, so the category report and the COA report always
agree. `--account` takes a GL code or name; `--category` a mirror-category name
(see `--bank` for the bank/cash account). An explicit `--set de_account_id=` still
wins.

### Split one transaction across several GL accounts

`--split` posts **one bank transaction to multiple GL accounts at once** — the way
an invoice's line items each hit their own account. Repeat it per leg as
`account=<code|name>,debit=<x>` or `account=<code|name>,credit=<x>`; the legs must
net to `--amount` (income legs credit-heavy, expense legs debit-heavy):

```bash
# one $13,400 PayPro deposit that books gross revenue, a refund and a fee together
akt payment create --type income --bank 20 --amount 13400 \
    --split 'account=400,credit=13686' \
    --split 'account=545,debit=280' \
    --split 'account=605,debit=6'
```

Needs the [`akt-api` companion module](akt-api/) — it writes the extra DoubleEntry
item legs (a standalone transaction can't natively carry more than one). The
transaction keeps a **single `category_id`**, a label just like a multi-item
invoice's, so Akaunting's native category report shows it under one bucket; the
**DoubleEntry GL (the item legs) is the source of truth**.

**Enforcement (when a `--coa` config is loaded):** akt refuses to create a
*standalone* income/expense transaction that has no GL account — you must pass
`--account`, `--category`, or an explicit `--set de_account_id=`. This makes the
"category set but GL account left to the module's 628/400 default" mistake
impossible. Bill/invoice payments (which post to A/P–A/R) are exempt, and with no
`--coa` config the old behaviour is unchanged. Use `akt verify` to catch any
transactions (e.g. web-UI entries) that slipped past.

### Refunds — mind the report's type-guard

Akaunting's DoubleEntry chart-of-accounts P&L report only counts a posting when
the **transaction type matches the account class** — an `income` transaction on an
*expense* account (or an `expense` on an *income* account) is **silently dropped**
from the report even though it balances in the ledger. This bites refunds: a
vendor refund is money *in*, so Akaunting makes it an `income` transaction — but if
you code it onto the expense account it was reversing, the report ignores it and
overstates that expense. (Swapping the debit/credit to keep it "on the expense
account" doesn't help: Akaunting picks Dr/Cr from the type, rejects negative
amounts, and re-posts on edit.)

Book a refund so its type matches its account's class:

* **To a matching-class account** — a money-in refund → an *income*-class account
  (e.g. a dedicated "Vendor Refunds" income account). Net profit is unchanged; the
  original expense stays gross.
* **As a journal entry** (`Dr bank / Cr expense`) — the report's separate journal
  path nets it by account class regardless of type. Caveat: a JE that touches a
  bank account also spawns a bank-register transaction.

**`akt verify` flags any standalone income/expense transaction posted to an
opposite-class account** (and exits non-zero), so you can gate CI on it and never
ship a refund the report will quietly ignore.

# Anything else: raw API access
akt raw GET reports
akt raw POST items --data '{"name":"Ad-hoc","type":"service","sale_price":99}'
akt company
akt settings --search 'key:default.account'
```

## Foreign currency

Akaunting stores every transaction in its own currency plus a `currency_rate`
snapshot (foreign units per 1 unit of the company default currency); the base
value is `amount ÷ currency_rate`. In the web UI that rate is auto-filled from
the currency's live rate — but over the API you must supply it, and akt used to
default it to `1`, silently mis-stating any foreign transaction. akt now fetches
the right rate for you, **for the transaction's own date**, from keyless public
feeds:

* **majors** (~30 ECB currencies) — [Frankfurter](https://frankfurter.dev)
* **ARS** (Argentine peso, which ECB doesn't publish) — historical by date from
  [ArgentinaDatos](https://argentinadatos.com), latest from
  [dolarapi.com](https://dolarapi.com), with a selectable *casa*
  (oficial/blue/bolsa[MEP]/contadoconliqui[CCL]/…) and price side.

```bash
# Auto-fills currency_rate for the payment's date (paid_at). No extra flags.
akt payment create --type expense --bank 3 --amount 100 --currency-code EUR

# Back-dated entry books at that day's historical rate:
akt bill create --contact 13 --currency-code EUR \
    --item 'name=Hosting,price=100,quantity=1' --issued-at 2025-06-02

# Argentine peso, defaulting to the MEP (bolsa) rate at the mid price:
akt payment create --type expense --bank 5 --amount 500000 --currency-code ARS
# ...or pick the rate type / side per transaction:
akt payment create --type expense --bank 5 --amount 500000 --currency-code ARS \
    --ars-casa blue --ars-side venta

# Override or opt out:
akt payment create ... --currency-code EUR --currency-rate 0.905   # pin it yourself
akt payment create ... --currency-code EUR --no-auto-rate          # store 1 (legacy)
akt payment create ... --currency-code EUR --rate-date 2025-06-30   # a different rate date
```

The currency must first exist in Akaunting (it rejects a transaction in an
unconfigured currency): add it once with `akt currency create --name Euro --code
EUR --rate 0.9` (any rate — each transaction stores its own).

If a rate can't be resolved (offline, feed down, an unsupported currency, or a
future date) akt **fails loudly** rather than booking at `1` — pass
`--currency-rate` to set it manually. Historical rates are cached under
`~/.cache/akt/fx/`; set `AKT_FX_DISABLE=1` to force fully offline.

Preview or convert a rate without touching a transaction:

```bash
akt fx EUR                          # latest EUR per 1 default-currency, and the inverse
akt fx ARS --on 2025-06-02          # historical, MEP/mid by default
akt fx ARS --ars-casa blue --amount 500000   # convert 500,000 ARS to the base currency
```

**Reporting.** With the companion module updated (see below), akt's reports
mirror Akaunting exactly: `trial-balance`, `report profit-loss`, `report
balance-sheet` and `balance` are computed **in the company default currency**,
converting each ledger leg at its historical rate the way Akaunting's own
statements do — so a EUR or ARS posting is no longer summed at face value.
`akt ledger` shows each leg **as posted** (its own currency) alongside a
converted column, so nothing is silently mislabelled. Defaults: ARS casa `bolsa`
(MEP), price `mid`; override globally with `AKT_FX_ARS_CASA` / `AKT_FX_ARS_SIDE`.

> The reporting fix lives in the **akt-api** companion module — update it in your
> Akaunting `modules/` directory (see `akt-api/README.md`) for base-currency
> reports. The rate-input feature above needs only the CLI.

## Financial reports (needs the akt-api module)

Akaunting's trial balance, P&L, and balance sheet aren't in its JSON API. With
the companion module installed, akt computes them from the ledger:

```bash
akt trial-balance --to 2025-12-31                 # every account, debits = credits
akt report profit-loss --from 2025-01-01 --to 2025-12-31
akt report balance-sheet --to 2025-12-31
akt balance --account 105 --to 2025-12-31         # one account's balance
akt balance --account 200 --to 2025-12-31 --expected 0   # reconcile: exit 1 on mismatch
```

`--expected` turns `akt balance` into a reconciliation primitive (compare an
account to an external figure, non-zero exit on a mismatch). Balances aggregate
through Akaunting's ledger relation, so soft-deleted transactions are excluded
just as its own reports do.

## Reading the ledger: the `akt-api` companion module

Akaunting's Double-Entry app exposes only `journal-entry` and `chart-of-accounts`
over the API — the ledger itself (where every transaction's postings land) has no
endpoint. The optional **`akt-api`** companion module (in `akt-api/`, install it
into your Akaunting `modules/` directory — see `akt-api/README.md`) adds a
read-only `GET /api/akt/ledgers`. When it's present, extra commands light up; when
it's not, they print an install hint and everything else works unchanged.

```bash
# Show what actually posted to a GL account (by code or name):
akt ledger --account 628 --from 2025-01-01 --to 2025-12-31

# Audit: every standalone income/expense transaction whose ACTUAL posted GL
# account differs from the account that mirrors its category. Needs a --coa
# config (to know the mapping) and the companion module (to read the ledger).
# Exits non-zero when it finds mis-postings — handy in CI / a pre-close gate.
akt --coa coa.toml verify --from 2025-01-01 --to 2025-12-31
```

Because the Double-Entry module posts to the ledger by `de_account_id`, not by
category, a transaction can carry a tidy category yet post to the wrong GL
account (e.g. the module's default "Other / Uncategorized"). `akt verify` is how
you catch that — the category report and the COA report only agree when it comes
back clean.

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
  The same applies to *updating* a document-linked payment (e.g. attaching a
  file to it) — `akt` picks the nested route automatically.
* **Multipart uploads** — attachments switch the request from JSON to
  `multipart/form-data` with the `attachment[]` field; updates are sent as
  `POST` + `_method=PATCH` because PHP won't populate `$_FILES` on a real `PUT`.
* **Attachment download isn't on `/api`** — Akaunting only serves attachment
  bytes from the session-authenticated web route `/{company}/uploads/{id}/download`.
  `akt download-attachment` transparently logs in a web session with your admin
  credentials (reused for the process) to fetch the file; metadata (id, name,
  size) comes from the `/api` record itself.
* **Full-replace updates** — Akaunting PUT re-validates required fields, so
  `akt` merges your changes onto the current record.
* **Journal entries must balance** — a `journal-entry` needs >= 2 lines whose
  debits equal its credits; `akt` validates this client-side (clear error)
  before hitting the API. Each line carries both a `debit` and a `credit` key
  (the unused side sent as `0`) because Akaunting validates both as required.
* **Journal updates re-derive ledgers** — like documents, a journal update
  deletes any ledger line absent from the request. `akt` resends the existing
  lines (with their ledger ids) so an update that only changes a field doesn't
  wipe the entry, and auto-generates the `journal_number` from the module's
  `double-entry.journal.number_*` settings when you don't pass one.
* **Chart-of-accounts CRUD is web-only** — the Double-Entry module exposes
  accounts read-only on `/api`; create/update/delete exist solely on the
  session/CSRF web route. `akt account create|update|delete` logs in a
  web session (reusing your admin credentials, cached for the process), attaches
  the CSRF token, and unwraps Akaunting's `{success, error, data, message}` AJAX
  envelope — so a server-side block (e.g. *deleting an account that has ledgers*)
  surfaces as a normal error. Updates resend `name` (required by Akaunting on
  update) from the current record when you don't pass one.

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

## Contributing

Developing, testing, or releasing `akt`? See [CONTRIBUTING.md](CONTRIBUTING.md)
for the test suite, CI/CD, release process, and a map of the source files.

## License

[MIT](LICENSE) © AsyncAlchemist
