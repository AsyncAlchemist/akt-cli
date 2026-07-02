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
