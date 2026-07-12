# Bank Opening-Balance Date Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `akt bank create/update --opening-balance-date YYYY-MM-DD` so the opening-balance journal entry Akaunting auto-posts is dated to a chosen period boundary instead of the account's creation date.

**Architecture:** `akt bank` is a thin wrapper over Akaunting's `accounts` endpoint; the opening-balance journal entry (JE) is created *server-side* by the Double-Entry module's `Observers\Banking\Account`, which stamps it with `paid_at => $account->created_at` — there is no request field for the date. So the CLI keeps sending `opening_balance` (the Banking register stays correct) and then, as a post-write step, locates that auto-JE (by its stable `reference = "opening-balance:<coa_id>"` + `description` suffix `;<account name>`) and re-dates it through the existing `journal-entry` API (`PUT /journal-entry/{id}`). Re-dating via that route also rewrites each ledger's `issued_at` (what reports filter on), and the module's `updated()` path only rewrites ledger *amounts* on later edits, so the custom date is durable.

**Tech Stack:** Python 3.12+ stdlib (`argparse`, `dataclasses`), `requests`; `pytest` (unit + integration markers). No new dependencies.

## Global Constraints

- **Python floor:** `requires-python = ">=3.12"` — no syntax newer than 3.12.
- **No new runtime dependencies** — stdlib + `requests` only.
- **Server contract (verified against the Double-Entry module source):**
  - Auto-JE date = `$account->created_at` (`Observers/Banking/Account.php::created`), not `now()`.
  - Auto-JE `reference` = `"opening-balance:<coa_id>"`; `description` = `"<Opening Balance>;<account name>"`.
  - Auto-JE is created with **no `journal_number`** (`CreateJournalEntry` never assigns one), but `PUT /journal-entry/{id}` validates `journal_number => required` (`Http/Requests/Journal.php`). A re-date **must backfill** a journal number.
  - `PUT /journal-entry/{id}` is a full replace requiring `paid_at, description, journal_number, basis, currency_code, currency_rate` and `items` (≥2, each with `account_id`+`debit`+`credit`); `UpdateJournalEntry` sets every ledger's `issued_at => paid_at`.
  - The auto-JE is created **only if** `opening_balance > 0` **and** the `double-entry.accounts_owners_contribution` account exists.
- **Tests** are split `tests/unit/` (marker `unit`, offline) and `tests/integration/` (marker `integration`, live Akaunting, auto-skipped without `AKT_BASE_URL/AKT_EMAIL/AKT_PASSWORD`).
- **Commits:** conventional-commit style (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`), one per task, ending with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Run unit tests with: `uv run pytest tests/unit -q` (or `pytest tests/unit -q`).

---

## File Structure

- `src/akt/resources.py` — **modify.** Add `Field.local` flag; skip local fields in `body_from_fields`; add `Resource.post_write` hook attribute; add three module-level helpers: `_journal_reput_body`, `_find_opening_balance_je`, `redate_opening_balance`. Add `import sys`.
- `src/akt/commands.py` — **modify.** Invoke `res.post_write` at the end of `cmd_create` and `cmd_update` (the `/api` path).
- `src/akt/registry.py` — **modify.** Add the `opening-balance-date` field to `BANK` and set `BANK.post_write = redate_opening_balance`; import the hook.
- `tests/unit/test_builders.py` — **modify.** Extend `FakeClient` with recording `post`/`put`; add unit tests for the local-field skip, hook wiring, the two journal helpers, the re-date hook, and BANK wiring.
- `tests/integration/test_live.py` — **modify.** Add one end-to-end test that creates a bank with `--opening-balance-date` and asserts the JE's `paid_at`.
- `README.md` — **modify.** Document the flag.

---

### Task 1: `Field.local` + skip local fields in body assembly

Local fields become CLI flags but must never enter the request body sent to the resource's own endpoint (the account body must not carry `opening_balance_date`).

**Files:**
- Modify: `src/akt/resources.py` (`Field` dataclass ~26-34; `body_from_fields` ~198-232)
- Test: `tests/unit/test_builders.py`

**Interfaces:**
- Produces: `Field.local: bool = False` (dataclass attr, settable via `f(name, help, local=True)`). `body_from_fields(res, ns, *, for_update, current=None) -> dict` now omits any field with `local=True`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_builders.py`:

```python
def test_body_from_fields_skips_local_fields():
    from akt.resources import Resource, f, body_from_fields
    res = Resource(noun="x", endpoint="x", fields=[
        f("name", "n", required=True),
        f("ghost", "local only", local=True),
    ])
    ns = SimpleNamespace(name="Acme", ghost="2024-12-31", set_=None, data=None)
    body = body_from_fields(res, ns, for_update=False)
    assert body["name"] == "Acme"
    assert "ghost" not in body        # local field never enters the body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_builders.py::test_body_from_fields_skips_local_fields -q`
Expected: FAIL — `TypeError: f() got an unexpected keyword argument 'local'` (or `Field` has no attribute `local`).

- [ ] **Step 3: Write minimal implementation**

In `src/akt/resources.py`, add the attribute to `Field` (after `choices`):

```python
@dataclass
class Field:
    name: str
    dest: str
    help: str = ""
    required: bool = False
    default: Any = None
    is_flag: bool = False
    choices: list[str] | None = None
    local: bool = False             # CLI flag only; never sent in the request body
```

In `body_from_fields`, make the field loop skip local fields — change the loop start:

```python
    body: dict[str, Any] = {}
    for fld in res.fields:
        if fld.local:
            continue
        val = getattr(ns, fld.dest, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_builders.py::test_body_from_fields_skips_local_fields -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/akt/resources.py tests/unit/test_builders.py
git commit -m "feat(resources): add Field.local for CLI-only flags excluded from the body

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `Resource.post_write` hook, invoked by create/update

A resource may register a callback run after a successful create/update, receiving the returned record and the argparse namespace. Bank uses it to re-date the opening-balance JE.

**Files:**
- Modify: `src/akt/resources.py` (`Resource` dataclass hooks block ~64-71)
- Modify: `src/akt/commands.py` (`cmd_create` ~58-60; `cmd_update` ~106-108)
- Test: `tests/unit/test_builders.py`

**Interfaces:**
- Consumes: `Field.local` (Task 1) — unrelated but same file.
- Produces: `Resource.post_write: Callable[["Resource", Client, dict, Any], None] | None = None`. `cmd_create`/`cmd_update` call `res.post_write(res, client, data, ns)` after emitting is prepared, on the `/api` path (not the `web_endpoint` path).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_builders.py`:

```python
def test_cmd_create_invokes_post_write_hook():
    from akt.resources import Resource
    from akt.commands import cmd_create

    calls = []

    class RecordingClient(FakeClient):
        def post(self, endpoint, body, **kw):
            return {"data": {"id": 99, "name": body.get("name")}}

    def hook(res, client, record, ns):
        calls.append(record)

    res = Resource(noun="x", endpoint="x", fields=[], post_write=hook)
    ns = SimpleNamespace(set_=None, data=None, attachment=None, json=True)
    rc = cmd_create(res, RecordingClient(), ns)
    assert rc == 0
    assert calls == [{"id": 99, "name": None}]   # hook saw the created record
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_builders.py::test_cmd_create_invokes_post_write_hook -q`
Expected: FAIL — `TypeError: Resource.__init__() got an unexpected keyword argument 'post_write'`.

- [ ] **Step 3: Write minimal implementation**

In `src/akt/resources.py`, add to the `Resource` hooks block (after `update_resolver`):

```python
    # runs after a successful create/update on the /api path, with the returned
    # record + the argparse namespace; used by bank to re-date the auto-posted
    # opening-balance journal entry.
    post_write: Callable[["Resource", Client, dict, Any], None] | None = None
```

In `src/akt/commands.py`, `cmd_create`, replace the final block:

```python
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if res.post_write:
        res.post_write(res, client, data, ns)
    emit(data, as_json=True)
    return 0
```

In `cmd_update`, replace the final block the same way:

```python
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if res.post_write:
        res.post_write(res, client, data, ns)
    emit(data, as_json=True)
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_builders.py::test_cmd_create_invokes_post_write_hook -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/akt/resources.py src/akt/commands.py tests/unit/test_builders.py
git commit -m "feat(commands): add Resource.post_write hook run after create/update

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Journal re-date helpers (`_journal_reput_body`, `_find_opening_balance_je`)

Two pure-ish helpers: rebuild a full journal-entry PUT body from an existing entry with a new date, and locate the opening-balance JE for a bank record.

**Files:**
- Modify: `src/akt/resources.py` (near the journal builders, after `build_journal_update`)
- Test: `tests/unit/test_builders.py`

**Interfaces:**
- Consumes: existing `_journal_items_from_current(current) -> list[dict]` (preserves ledger `id`/`account_id`/`debit`/`credit`), `_next_journal_number(client) -> str`, `_normalize_date(str) -> str`.
- Produces:
  - `_journal_reput_body(current: dict, *, paid_at: str, journal_number: str) -> dict` — full PUT body: `paid_at, journal_number, description, basis, currency_code, currency_rate, amount=0, items` (from `_journal_items_from_current`), plus `reference` when present.
  - `_find_opening_balance_je(client: Client, record: dict) -> dict | None` — lists `journal-entry` (all pages), returns the newest entry whose `reference` starts with `"opening-balance:"` and whose `description` ends with `";" + record["name"]`, else `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_builders.py`:

```python
def test_journal_reput_body_preserves_ledgers_and_sets_date():
    from akt.resources import _journal_reput_body
    current = {
        "paid_at": "2026-07-12T00:00:00+00:00", "journal_number": None,
        "description": "Opening Balance;Checking", "basis": "accrual",
        "currency_code": "USD", "currency_rate": 1,
        "reference": "opening-balance:14",
        "ledgers": {"data": [
            {"id": 5, "account_id": 14, "debit": 500, "credit": None},
            {"id": 6, "account_id": 30, "debit": None, "credit": 500},
        ]},
    }
    body = _journal_reput_body(current, paid_at="2024-12-31 00:00:00",
                               journal_number="MJE-00009")
    assert body["paid_at"] == "2024-12-31 00:00:00"
    assert body["journal_number"] == "MJE-00009"   # backfilled by caller
    assert body["description"] == "Opening Balance;Checking"
    assert body["reference"] == "opening-balance:14"
    assert body["amount"] == 0
    assert body["items"] == [
        {"account_id": 14, "debit": 500.0, "credit": 0.0, "id": 5},
        {"account_id": 30, "debit": 0.0, "credit": 500.0, "id": 6},
    ]


def test_find_opening_balance_je_matches_and_picks_newest():
    from akt.resources import _find_opening_balance_je
    client = FakeClient(journals_list=[
        {"id": 1, "reference": "opening-balance:14", "description": "Opening Balance;Checking",
         "created_at": "2026-07-12T10:00:00+00:00"},
        {"id": 2, "reference": "opening-balance:14", "description": "Opening Balance;Checking",
         "created_at": "2026-07-12T11:00:00+00:00"},          # newer duplicate name
        {"id": 3, "reference": None, "description": "Some manual entry;Checking",
         "created_at": "2026-07-12T12:00:00+00:00"},          # not an opening balance
        {"id": 4, "reference": "opening-balance:99", "description": "Opening Balance;Savings",
         "created_at": "2026-07-12T09:00:00+00:00"},          # different account
    ])
    je = _find_opening_balance_je(client, {"id": 7, "name": "Checking"})
    assert je["id"] == 2                                       # newest matching
    assert _find_opening_balance_je(client, {"id": 8, "name": "Nonexistent"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_builders.py::test_journal_reput_body_preserves_ledgers_and_sets_date tests/unit/test_builders.py::test_find_opening_balance_je_matches_and_picks_newest -q`
Expected: FAIL — `ImportError: cannot import name '_journal_reput_body'` / `_find_opening_balance_je`.

- [ ] **Step 3: Write minimal implementation**

In `src/akt/resources.py`, after `build_journal_update` (ends ~730), add:

```python
def _journal_reput_body(current: dict, *, paid_at: str, journal_number: str) -> dict:
    """Full journal-entry PUT body that re-dates an existing entry, preserving
    its ledgers (by id) and amounts. ``journal_number`` is supplied by the caller
    (the server-created opening-balance entry carries none, but the API requires
    one)."""
    body: dict[str, Any] = {
        "paid_at": paid_at,
        "journal_number": journal_number,
        "description": current.get("description"),
        "basis": current.get("basis") or "accrual",
        "currency_code": current.get("currency_code") or "USD",
        "currency_rate": current.get("currency_rate", 1) or 1,
        "amount": 0,
        "items": _journal_items_from_current(current),
    }
    if current.get("reference"):
        body["reference"] = current["reference"]
    return body


def _find_opening_balance_je(client: Client, record: dict) -> dict | None:
    """Locate the opening-balance journal entry the Double-Entry module auto-posts
    for a bank/cash account. It carries ``reference = "opening-balance:<coa_id>"``
    and ``description`` ending ``";<account name>"``. The chart-of-accounts API
    doesn't expose the bank<->coa link, so match on those two fields and, if an
    account name is duplicated, take the newest by ``created_at``."""
    name = str(record.get("name") or "")
    suffix = ";" + name
    matches = [
        e for e in client.list("journal-entry", all_pages=True)
        if str(e.get("reference") or "").startswith("opening-balance:")
        and str(e.get("description") or "").endswith(suffix)
    ]
    if not matches:
        return None
    matches.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    return matches[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_builders.py::test_journal_reput_body_preserves_ledgers_and_sets_date tests/unit/test_builders.py::test_find_opening_balance_je_matches_and_picks_newest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/akt/resources.py tests/unit/test_builders.py
git commit -m "feat(resources): add opening-balance JE locate + re-date body helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `redate_opening_balance` post-write hook

Orchestrates the re-date: read the date flag, locate the JE, backfill a journal number if missing, PUT the new date, and print a note to stderr (so JSON on stdout stays clean). No-op when the flag is absent; a warning (not an error) when no JE is found, since the account itself was created successfully.

**Files:**
- Modify: `src/akt/resources.py` (add `import sys` near the top imports; add the hook after the helpers from Task 3)
- Test: `tests/unit/test_builders.py` (extend `FakeClient` with recording `put`)

**Interfaces:**
- Consumes: `_find_opening_balance_je`, `_journal_reput_body`, `_next_journal_number`, `_normalize_date`.
- Produces: `redate_opening_balance(res: Resource, client: Client, record: dict, ns: Any) -> None` — signature matches `Resource.post_write`. Reads `ns.opening_balance_date`; when set and a JE is found, calls `client.put("journal-entry/<id>", body)`.

- [ ] **Step 1: Write the failing test**

First extend `FakeClient.__init__` and add a `put` method (so tests can capture the re-date PUT). In `tests/unit/test_builders.py`, update `FakeClient`:

```python
    def __init__(self, *, contacts=None, documents=None, settings=None, docs_list=None,
                 txns_list=None, transactions=None, journals_list=None):
        self._contacts = contacts or {}
        self._documents = documents or {}
        self._settings = settings or {}
        self._docs_list = docs_list or []
        self._txns_list = txns_list or []
        self._transactions = transactions or {}
        self._journals_list = journals_list or []
        self.puts = []                       # recorded (path, body) PUT calls

    def put(self, path, json_body, **kw):
        self.puts.append((path, json_body))
        return {"data": json_body}
```

Then add the tests:

```python
def test_redate_opening_balance_puts_new_date_and_backfills_number():
    from akt.resources import redate_opening_balance
    client = FakeClient(
        settings={"double-entry.journal.number_prefix": "MJE-",
                  "double-entry.journal.number_digit": "5"},
        journals_list=[
            {"id": 42, "reference": "opening-balance:14",
             "description": "Opening Balance;Checking", "journal_number": None,
             "basis": "accrual", "currency_code": "USD", "currency_rate": 1,
             "created_at": "2026-07-12T10:00:00+00:00",
             "ledgers": {"data": [
                 {"id": 5, "account_id": 14, "debit": 500, "credit": None},
                 {"id": 6, "account_id": 30, "debit": None, "credit": 500},
             ]}},
        ],
    )
    ns = SimpleNamespace(opening_balance_date="2024-12-31")
    record = {"id": 7, "name": "Checking"}
    redate_opening_balance(BY_NOUN["bank"], client, record, ns)
    assert len(client.puts) == 1
    path, body = client.puts[0]
    assert path == "journal-entry/42"
    assert body["paid_at"] == "2024-12-31 00:00:00"       # normalized
    assert body["journal_number"] == "MJE-00001"          # backfilled (list has no numbers)
    assert body["reference"] == "opening-balance:14"
    assert len(body["items"]) == 2


def test_redate_opening_balance_noop_without_date():
    from akt.resources import redate_opening_balance
    client = FakeClient(journals_list=[])
    ns = SimpleNamespace(opening_balance_date=None)
    redate_opening_balance(BY_NOUN["bank"], client, {"id": 7, "name": "Checking"}, ns)
    assert client.puts == []


def test_redate_opening_balance_warns_when_no_je(capsys):
    from akt.resources import redate_opening_balance
    client = FakeClient(journals_list=[])                 # nothing to find
    ns = SimpleNamespace(opening_balance_date="2024-12-31")
    redate_opening_balance(BY_NOUN["bank"], client, {"id": 7, "name": "Checking"}, ns)
    assert client.puts == []
    assert "no opening-balance journal entry" in capsys.readouterr().err
```

Note: these reference `BY_NOUN["bank"]`, which already carries the field/hook only after Task 5. That's fine — the hook function ignores `res` except for the noun in messages; pass any resource. To keep Task 4 self-contained, the tests use `BY_NOUN["bank"]` purely as a `Resource` argument (its `.noun` is "bank"); the hook works regardless of whether BANK has the field yet.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_builders.py -k redate_opening_balance -q`
Expected: FAIL — `ImportError: cannot import name 'redate_opening_balance'`.

- [ ] **Step 3: Write minimal implementation**

In `src/akt/resources.py`, add `import sys` with the other stdlib imports (after `import os`):

```python
import os
import sys
```

Add the hook after `_find_opening_balance_je`:

```python
def redate_opening_balance(res: "Resource", client: Client, record: dict, ns: Any) -> None:
    """post_write hook for bank create/update: re-date the auto-posted
    opening-balance journal entry to ``--opening-balance-date``.

    The Double-Entry module stamps that entry with the account's created_at and
    exposes no date field, so we set the balance normally (keeping the Banking
    register correct) and rewrite the entry's date here. Re-dating via the
    journal-entry route also rewrites each ledger's issued_at (what reports
    filter on), and the module's account-update path only touches ledger
    amounts, so the date sticks across later edits."""
    date = getattr(ns, "opening_balance_date", None)
    if not date:
        return
    je = _find_opening_balance_je(client, record)
    if je is None:
        print(f"note: no opening-balance journal entry found for {res.noun} "
              f"{record.get('id')}; --opening-balance-date had no effect "
              f"(opening balance is 0, or the Double-Entry module / owners-"
              f"contribution account is not configured)", file=sys.stderr)
        return
    number = je.get("journal_number") or _next_journal_number(client)
    body = _journal_reput_body(je, paid_at=_normalize_date(date), journal_number=number)
    client.put(f"journal-entry/{je['id']}", body)
    print(f"note: re-dated opening-balance journal entry {je['id']} to {date}",
          file=sys.stderr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_builders.py -k redate_opening_balance -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/akt/resources.py tests/unit/test_builders.py
git commit -m "feat(resources): add redate_opening_balance post-write hook

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Wire the flag onto the BANK resource

Add the `--opening-balance-date` flag (local, so it never enters the account body) and attach the hook.

**Files:**
- Modify: `src/akt/registry.py` (imports at top; `BANK` definition ~95-115)
- Test: `tests/unit/test_builders.py`

**Interfaces:**
- Consumes: `redate_opening_balance` (Task 4), `Field.local` (Task 1), `Resource.post_write` (Task 2).
- Produces: `BY_NOUN["bank"]` has a field `opening-balance-date` (dest `opening_balance_date`, `local=True`) and `post_write == redate_opening_balance`. `akt bank create/update --opening-balance-date` parses.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_builders.py`:

```python
def test_bank_has_local_opening_balance_date_field_and_hook():
    from akt.resources import redate_opening_balance
    bank = BY_NOUN["bank"]
    fld = next((f for f in bank.fields if f.name == "opening-balance-date"), None)
    assert fld is not None
    assert fld.dest == "opening_balance_date"
    assert fld.local is True
    assert bank.post_write is redate_opening_balance


def test_bank_body_excludes_opening_balance_date():
    """The date flag must not be POSTed to the accounts endpoint."""
    bank = BY_NOUN["bank"]
    ns = SimpleNamespace(
        name="Checking", number="1001", type="bank", currency_code="USD",
        opening_balance="500", opening_balance_date="2024-12-31",
        bank_name=None, bank_phone=None, bank_address=None, enabled=None,
        set_=None, data=None,
    )
    body = body_from_fields(bank, ns, for_update=False)
    assert body["opening_balance"] == "500"
    assert "opening_balance_date" not in body


def test_bank_create_parses_opening_balance_date_flag():
    parser = _build_parser()
    ns = parser.parse_args(
        ["bank", "create", "--name", "Checking", "--number", "1001",
         "--opening-balance", "500", "--opening-balance-date", "2024-12-31"]
    )
    assert ns.opening_balance == "500"
    assert ns.opening_balance_date == "2024-12-31"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_builders.py -k "bank_has_local or bank_body_excludes or bank_create_parses" -q`
Expected: FAIL — no `opening-balance-date` field; `ns` has no `opening_balance_date`; `post_write` is `None`.

- [ ] **Step 3: Write minimal implementation**

In `src/akt/registry.py`, import the hook. Find the existing import of builders from `.resources` and add `redate_opening_balance` to it (matching the current import style — the file already imports `build_document_create`, `build_payment_create`, etc.):

```python
from .resources import (
    # ... existing names ...
    redate_opening_balance,
)
```

(If the builders are imported as `from .resources import *` or individually, add `redate_opening_balance` to that same statement.)

In the `BANK` definition, add the field after `opening-balance` and set the hook:

```python
BANK = Resource(
    noun="bank",
    endpoint="accounts",
    fields=[
        f("name", "Account name", required=True),
        f("number", "Account number", required=True),
        f("type", "Account type", default="bank"),
        f("currency-code", "Currency code", default="USD"),
        f("opening-balance", "Opening balance", default=0),
        f("opening-balance-date",
          "Date for the opening-balance journal entry (YYYY-MM-DD). Re-dates the "
          "entry the Double-Entry module auto-posts; needs a positive opening balance.",
          dest="opening_balance_date", local=True),
        f("bank-name", "Bank name"),
        f("bank-phone", "Bank phone"),
        f("bank-address", "Bank address"),
        f("enabled", "Enable the record", is_flag=True, default=1),
    ],
    columns=[
        ("ID", "id"), ("Name", "name"), ("Number", "number"),
        ("Currency", "currency_code"), ("Balance", "current_balance_formatted"),
        ("Enabled", "enabled"),
    ],
    help="Bank / cash accounts",
    post_write=redate_opening_balance,
)
```

- [ ] **Step 4: Run tests (targeted, then full unit suite)**

Run: `uv run pytest tests/unit/test_builders.py -k "bank_has_local or bank_body_excludes or bank_create_parses" -q`
Expected: PASS

Run: `uv run pytest tests/unit -q`
Expected: PASS (whole unit suite green — no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/akt/registry.py tests/unit/test_builders.py
git commit -m "feat(bank): add --opening-balance-date to re-date the opening JE

Closes #6

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Live integration test

End-to-end proof against a real Akaunting: create a bank with an opening balance and a back-dated `--opening-balance-date`, then confirm the auto-posted JE carries that date and a backfilled journal number. Skips cleanly when the Double-Entry module (or its owners-contribution account) isn't available.

**Files:**
- Modify: `tests/integration/test_live.py` (add one test near the banking/journal tests)

**Interfaces:**
- Consumes: the `akt` and `tracker` fixtures from `tests/integration/conftest.py`; module-level `RID`. `journal-entry` and `bank` already have `_DELETE_PRIORITY` entries (0 and 4), so registering both deletes the JE before the bank.

- [ ] **Step 1: Write the test**

Add to `tests/integration/test_live.py`:

```python
def test_bank_opening_balance_date_redates_journal_entry(akt, tracker):
    # Requires the Double-Entry module; skip if the journal-entry API isn't there.
    probe = akt("journal-entry", "list", raw=True)
    if probe.returncode != 0:
        pytest.skip("Double-Entry module not available on this instance")

    name = f"AKT-IT OB Bank {RID}"
    bank = akt("bank", "create", "--name", name, "--number", f"OB{RID}",
               "--currency-code", "USD", "--opening-balance", "500",
               "--opening-balance-date", "2024-12-31")
    tracker("bank", bank["id"])

    jes = akt("journal-entry", "list", "--all")
    je = next(
        (j for j in jes
         if str(j.get("reference") or "").startswith("opening-balance:")
         and str(j.get("description") or "").endswith(";" + name)),
        None,
    )
    if je is None:
        pytest.skip("no opening-balance JE created "
                    "(owners-contribution account not configured)")
    tracker("journal-entry", je["id"])

    assert str(je["paid_at"]).startswith("2024-12-31")   # re-dated to the period boundary
    assert je["journal_number"]                          # backfilled, non-empty
```

- [ ] **Step 2: Run the test (or confirm skip)**

Run (against a live instance): `uv run pytest tests/integration/test_live.py::test_bank_opening_balance_date_redates_journal_entry -q`
Expected: PASS with live creds + Double-Entry module. Without creds/module: **SKIPPED** (never fails offline).
Also confirm the suite still collects offline: `uv run pytest tests/integration -q` → all integration tests skipped, no collection errors.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_live.py
git commit -m "test(integration): assert --opening-balance-date re-dates the opening JE

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Document the flag in the README

**Files:**
- Modify: `README.md` (the bank/cash section, ~149-151)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the README**

Replace the bank/cash example block (currently around lines 149-151):

```markdown
# Bank / cash accounts (the money side — see the COA section below for GL accounts)
akt bank create --name "Business Checking" --number 1001 --currency-code USD
akt bank list
```

with:

```markdown
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
```

- [ ] **Step 2: Verify the docs render / no broken references**

Run: `git diff --stat README.md`
Expected: `README.md` shows the added lines; skim the diff to confirm the example is inside the bank section and fenced correctly.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document bank --opening-balance-date

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec/issue coverage** (issue #6):
- *"add an `--opening-balance-date YYYY-MM-DD` flag"* → Tasks 1–5 (flag + re-date). ✅
- *"opening dated 2024-12-31 lands in prior period"* → re-date rewrites ledger `issued_at` (verified: `UpdateJournalEntry` sets `issued_at => paid_at`); integration test asserts `paid_at`. ✅
- *"balances excluded from prior-period reports"* → fixed because reports filter `issued_at`, now moved. ✅
- *"register understated" workaround* → dissolved: we keep `opening_balance` (register correct) **and** re-date. ✅
- *"honor a company financial-year-start setting" (the "and/or")* → **not implemented.** Deliberately out of scope: the per-account `--opening-balance-date` flag is the precise lever the issue leads with; a global FY-start setting is a coarser, separate feature. Noted here so it's a conscious gap, not an omission. If wanted, it's a follow-up that defaults `--opening-balance-date` from a setting.

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step shows complete code. ✅

**3. Type consistency:** `redate_opening_balance(res, client, record, ns)` matches `Resource.post_write` and the `cmd_create`/`cmd_update` call site. `_journal_reput_body(current, *, paid_at, journal_number)` and `_find_opening_balance_je(client, record)` names/signatures are identical across Tasks 3–5. `Field.local` used consistently. `FakeClient.put(path, json_body)` matches the real `Client.put(path, json_body, **kw)` (commands call `client.put(path, body, ...)` positionally). ✅

## Notes / risks (for the implementer)

- **Durability:** re-dating survives later `bank update`s (the module's `updated()` path rewrites only ledger amounts). The one exception: setting `--opening-balance 0` deletes the JE, and raising it again recreates it at `created_at`. This is inherent server behavior; not worth guarding in code, but worth a line in a future help/README note if it surprises anyone.
- **Duplicate account names:** the locator disambiguates by newest `created_at`. Two banks with the identical name created in the same second is the only ambiguous case; acceptable.
- **Release step (not a task):** per repo convention a release bumps `pyproject.toml` version and syncs `uv.lock` in a separate `chore:` commit. Do that when cutting the release, not as part of this feature.
