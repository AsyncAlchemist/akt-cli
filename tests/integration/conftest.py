"""Integration test harness.

These tests drive the *real* `akt` CLI (via `python -m akt.cli`) against a live
Akaunting 3.x instance. They are skipped unless AKT_BASE_URL / AKT_EMAIL /
AKT_PASSWORD are present in the environment (set as GitHub Actions secrets in
the release workflow). Every resource a test creates is registered with the
`tracker` fixture and deleted on teardown — including on failure — so no
invoices, bills, payments or contacts are left behind.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

# Prefer the installed console script (cleaner than `python -m akt.cli`, which
# warns because the package __init__ imports cli). Falls back to -m.
_AKT_BIN = shutil.which("akt")
_AKT_CMD = [_AKT_BIN] if _AKT_BIN else [sys.executable, "-m", "akt.cli"]

_REQUIRED = ("AKT_BASE_URL", "AKT_EMAIL", "AKT_PASSWORD")

# Delete order: children before parents (payments/transfers before documents,
# documents before contacts, items/accounts last). Lower number = deleted first.
_DELETE_PRIORITY = {
    "payment": 0,
    "journal-entry": 0,
    "transfer": 1,
    "invoice": 2,
    "bill": 2,
    "customer": 3,
    "vendor": 3,
    "item": 4,
    "account": 4,
    "category": 4,
    "tax": 4,
    "currency": 4,
    "chart-of-account": 4,
}


def pytest_collection_modifyitems(config, items):
    integration_dir = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(integration_dir):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def akt_env():
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        pytest.skip("integration tests require env: " + ", ".join(missing))
    env = os.environ.copy()
    # be gentle on host bot-protection / rate limits during CI
    env.setdefault("AKT_THROTTLE", os.environ.get("AKT_THROTTLE", "0.5"))
    return env


@pytest.fixture
def akt(akt_env):
    """Run the akt CLI in a subprocess.

    Returns parsed JSON by default. Pass parse=False for non-JSON output,
    check=False to tolerate a non-zero exit, raw=True to get the CompletedProcess.
    """

    def run(*args, parse=True, check=True, raw=False):
        argv = list(args)
        if parse and not raw and "--json" not in argv:
            argv = ["--json", *argv]
        proc = subprocess.run(
            [*_AKT_CMD, *argv],
            capture_output=True, text=True, env=akt_env,
        )
        if raw:
            return proc
        if check and proc.returncode != 0:
            raise AssertionError(
                f"`akt {' '.join(args)}` failed (rc={proc.returncode}):\n{proc.stderr.strip()}"
            )
        if parse and proc.stdout.strip():
            return json.loads(proc.stdout)
        return proc.stdout

    return run


@pytest.fixture
def tracker(akt):
    """Register created resources as (noun, id); delete them all on teardown."""
    created: list[tuple[str, object]] = []

    def add(noun: str, ident) -> object:
        created.append((noun, ident))
        return ident

    yield add

    # children first, then within a tier reverse creation order
    ordered = sorted(
        enumerate(created),
        key=lambda e: (_DELETE_PRIORITY.get(e[1][0], 9), -e[0]),
    )
    failures = []
    for _, (noun, ident) in ordered:
        # retry: never leave hanging invoices/bills/payments on a transient blip
        last_err = "unknown error"
        for _attempt in range(3):
            proc = akt(noun, "delete", str(ident), raw=True)
            if proc.returncode == 0:
                break
            last_err = proc.stderr.strip()
            time.sleep(1)
        else:
            failures.append(f"{noun} {ident}: {last_err}")
    if failures:
        raise AssertionError("teardown failed to delete:\n" + "\n".join(failures))
