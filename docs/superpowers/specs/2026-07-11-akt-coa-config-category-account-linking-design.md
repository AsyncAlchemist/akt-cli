# Config-driven category ↔ account linking in akt

- **Date:** 2026-07-11
- **Status:** Approved design (pre-implementation)
- **Component:** akt-cli
- **Related:** DoubleEntry Akaunting module; downstream consumer `MarketDataApp/accounting` (`tools/coa.toml`)

## Problem

Akaunting has two parallel classification systems that most users expect to be one:

- **Categories** (`ixh_categories`) — Akaunting core's income/expense buckets. `category_id`
  is **required** on every transaction (`app/Http/Requests/Banking/Transaction.php:50`,
  `'category_id' => 'required|integer'`).
- **General-ledger accounts** (chart of accounts, `ixh_double_entry_accounts`) — added by the
  DoubleEntry module; the real double-entry books.

These are **not linked**. There is no `account_category` mapping table (the module maps GL
accounts via `account_bank` / `account_contact` / `account_item` / `account_tax` and type-based
defaults — never via category). The DoubleEntry `Transaction` observer's `getAccountId()`
ignores `category_id` entirely. Verified empirically: a transaction created with category
"Sales" posts its item leg to GL 400 by the *default rule*, not because of the category; passing
`de_account_id` overrides it independently of the category.

Users coming from Xero (where a "category" *is* an account) want a single classification list.
Today, to classify the ledger correctly you must set `de_account_id` per transaction and
separately pick a required category that the ledger ignores — two lists that drift.

## Goals

- Let a user run Akaunting **Xero-style**: think in one list (the chart of accounts), with the
  required category filled automatically and always consistent with the GL account.
- Make this a **general, config-driven akt feature** — not specific to any one company. It
  activates only when the user supplies a COA config; with no config, akt behaves exactly as today.
- Two capabilities:
  1. **Sync** — reconcile Akaunting's accounts and their 1:1 mirror categories to the config.
  2. **Bidirectional auto-fill** — on `payment create`, specifying `--account` fills the
     matching `category_id`, and specifying `--category` fills the matching `de_account_id`.

## Non-goals (this spec)

- **Historical reclassification** of existing transactions — separate later effort.
- **Invoice/bill line-item coding** (documents also carry per-line `de_account_id`) — natural
  follow-on, not now.
- **Company-specific logic** (e.g. vendor→account auto-coding). That stays in the consumer's
  own config/tooling (`coa.toml` carries `vendors`/`people`/`paypro`; akt ignores those fields).
- No change to how the DoubleEntry module posts ledgers; akt only sets the inputs
  (`de_account_id`, `category_id`) the module already honors.

## Design

### 1. The COA config (akt-defined schema)

akt loads a COA config using the same precedence style as credential discovery
(`config.py`): `--coa <file>` flag → `AKT_COA_FILE` env → `./coa.toml` → `~/.config/akt/coa.toml`.
When none is found, the feature is inert and all existing behavior is unchanged.

Minimal schema — only what akt needs. A consumer's richer file (e.g. Market Data's `coa.toml`,
which also carries `vendors`, `people`, `paypro`, `note`, `status`) is a **superset**: akt reads
the fields below and ignores the rest.

```toml
[[account]]
code    = 400                       # GL code — stable key, required, unique
name    = "API Subscription Revenue"
type_id = 13                        # DoubleEntry account type id (belongs to a class)
# optional:
# category = "Revenue"              # override the mirror category name (default = account name)
# mirror   = false                 # skip mirroring (e.g. bank / A-R / A-P accounts you never hand-code)
```

- **Link key is the name.** Categories have no code, so the mirror category is matched to its
  account by name (`category = account.name` unless overridden). This is the only viable join.
- **Category type is derived** from the account's class via a built-in DoubleEntry
  `type_id → class → category type` table shipped in akt:
  - class Income → category type `income`
  - class Expenses (incl. direct costs) → category type `expense`
  - class Assets / Liabilities / Equity → category type `other`
  (These class ids are the DoubleEntry standard seeds: 1 Assets, 2 Liabilities, 3 Expenses,
  4 Income, 5 Equity; akt hard-codes the standard `type_id → class` map since DoubleEntry is the
  target module. Unknown `type_id` → `other` + a warning.)
- **Which accounts mirror:** by default every account with `mirror` unset is mirrored. Accounts
  you never hand-code onto a transaction (bank/cash, A/R, A/P) should set `mirror = false`.
- **Uniqueness (validated on load):** `code` must be unique, and every mirror category `name`
  (account name, or `category` override) must be unique — since name is the join key, a collision
  is unresolvable. akt errors out on load if either is violated.

### 2. `akt coa` — sync accounts + mirror categories

New resource group `coa` with two verbs; both are **idempotent** and match existing records by
GL code (accounts) and by name (categories):

- **`akt coa diff`** — read-only. Prints the reconciliation plan: accounts to create, accounts to
  rename, mirror categories to create/rename, and (if `--prune`) records to disable. No writes.
- **`akt coa sync`** — applies the plan:
  - **create** missing accounts via `chart-of-account create` (web-CRUD route; sets `code`,
    `name`, `type_id`),
  - **rename** accounts whose live name differs from the config via `chart-of-account update`,
  - **create/rename** the 1:1 mirror categories via `category create` / `category update` with the
    derived type.

**Deletion policy (decided):** sync **never deletes** and, by default, never disables. It only
creates and renames. A `--prune` flag *disables* (sets `enabled = 0`, not a hard delete) accounts
and categories present in Akaunting but absent from the config, so aggressiveness is strictly
opt-in. `coa diff --prune` shows exactly what `--prune` would disable.

### 3. Bidirectional auto-fill on `payment create`

Only active when a COA config is loaded. akt builds a `code ↔ name ↔ category` map from the config
and resolves the live ids it needs (`chart-of-account` id for `de_account_id`; `category` id for
`category_id`) via lookups, cached per invocation.

New flags on `payment create` (in addition to today's `--category-id` / `--set de_account_id=`):

- `--account <code|name>` → resolve to the GL account; set `de_account_id` **and** fill the
  mirrored `category_id`.
- `--category <name|id>` → resolve to the category; set `category_id` **and** fill the
  corresponding `de_account_id`.

Resolution rules:

| Inputs | Behavior |
|---|---|
| `--account` only | set `de_account_id` + mirrored `category_id` |
| `--category` only | set `category_id` + corresponding `de_account_id` |
| both, consistent per config | set both as given |
| both, **conflict** | error, unless an explicit `--set de_account_id=` / `--category-id` is present, which **wins with a warning** |
| neither | **today's behavior** — no auto-fill; the module applies its own default account, and the user supplies a category as before (decided: akt does *not* force a config-coded value when none is asked for) |
| requested code/name not in config | error with the closest match suggested; no silent fallback |

### Category-type / balance-sheet handling

A transaction coded to a balance-sheet account (e.g. an owner draw → Equity "Owners Draw") still
needs a category. Its mirror category is created with type `other` (per §1), which satisfies the
required field. akt does not attempt to constrain which category types Akaunting's UI shows for a
given transaction direction; it sets `category_id` directly via the API, which accepts it.

## Affected code (akt-cli)

- **`src/akt/coa.py` (new)** — load/parse the config, build the code↔name↔category map, the
  `type_id → class → category-type` table, `diff`/`sync` planning, and id resolution against the
  live API.
- **`src/akt/config.py`** — add COA-file discovery (`--coa` / `AKT_COA_FILE` / `./coa.toml` /
  `~/.config/akt/coa.toml`), mirroring the existing credential-discovery pattern.
- **`src/akt/registry.py` / `resources.py`** — extend `build_payment_create` to consult the loaded
  config and set `de_account_id` + `category_id`; add the `--account` / `--category` flags.
- **`src/akt/cli.py`** — register the `coa` subcommand group (`diff`, `sync`) and the global
  `--coa` option.

Reuses existing primitives: `chart-of-account create/update`, `category create/update`,
`chart-of-account list`, `category list`, and the payment body builder.

## Testing

- **Unit** (`tests/unit`):
  - config parse: minimal + superset (extra fields ignored), missing/duplicate `code`, `mirror`
    and `category` overrides.
  - `type_id → category-type` mapping incl. unknown type_id → `other` + warning.
  - `diff` planning: create/rename/prune sets from a synthetic live snapshot.
  - `payment create` body building for each row of the resolution table, including the conflict and
    not-in-config error cases.
- **Integration** (`tests/integration/test_live.py`, gated like existing live tests):
  - `coa sync` from a tiny fixture config → assert accounts + mirror categories exist; re-run →
    no-op (idempotent).
  - `payment create --account <code>` → fetch the transaction and assert both `de_account_id` and
    `category_id` are set consistently; `--category <name>` the reverse. Clean up (delete).

## Consumer adoption (MarketDataApp/accounting) — informational

- `tools/coa.toml` already provides `[[account]]` `code`/`name`/`type_id`; it conforms to the akt
  schema as a superset. Point akt at it with `AKT_COA_FILE=tools/coa.toml` (or `--coa`), since the
  default discovery looks for `./coa.toml`, not `tools/`. `akt coa sync` then makes it real in
  Akaunting (creates the `new` accounts, applies `relabel` renames, builds the mirror categories).
- Reminder (separate from this feature): a transaction only reaches the ledger if its **bank
  account is mapped** to a GL account (`account_bank`). That mapping is out of scope here; the
  consumer handles it (their `coa.toml` `[[money_account]]` blocks).

## Decisions on record

1. Feature lives in akt-cli, config-gated; akt defines the schema (consumer files may be supersets).
2. Sync = create + rename only; `--prune` opt-in to *disable* (never delete).
3. `payment create` with neither `--account` nor `--category` → unchanged legacy behavior.
4. History reclassification and document line-item coding are out of scope for this spec.
5. Command surface uses akt's `<resource> <verb>` grammar: `akt coa diff` / `akt coa sync`
   (not a verb-first `sync-coa`).
6. `--account` accepts a GL **code or name** (unambiguous — codes are numeric, names are not);
   the code is the canonical key. Mirror categories use the pure account name as the join key,
   guarded by the load-time uniqueness validation (§1).
