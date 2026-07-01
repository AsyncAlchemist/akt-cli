"""Configuration loading for akt.

Resolution order (highest priority first):
  1. Explicit CLI flags (--base-url, --email, --password, --company)
  2. Environment variables (AKT_BASE_URL, AKT_EMAIL, AKT_PASSWORD, AKT_COMPANY)
  3. A dotenv file: $AKT_ENV_FILE, or ./.env, or ~/.config/akt/akt.env
     Recognised keys: APP_URL / AKT_BASE_URL, AKAUNTING_ADMIN_EMAIL / AKT_EMAIL,
     AKAUNTING_ADMIN_PASSWORD / AKT_PASSWORD, AKT_COMPANY.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_dotenv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            data[key] = val
    return data


def _candidate_env_files() -> list[Path]:
    files: list[Path] = []
    if os.environ.get("AKT_ENV_FILE"):
        files.append(Path(os.environ["AKT_ENV_FILE"]).expanduser())
    files.append(Path.cwd() / ".env")
    files.append(Path.home() / ".config" / "akt" / "akt.env")
    return files


class ConfigError(Exception):
    pass


@dataclass
class Config:
    base_url: str
    email: str
    password: str
    company_id: int

    @property
    def api_root(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/api"):
            return base
        return base + "/api"

    @property
    def web_root(self) -> str:
        """The web (non-API) origin, used for the session-authenticated upload
        download route ``/{company}/uploads/{id}/download`` which lives outside
        the ``/api`` surface."""
        base = self.base_url.rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        return base.rstrip("/")


def load_config(
    *,
    base_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    company: int | str | None = None,
) -> Config:
    """Merge CLI args, environment, and dotenv files into a Config."""
    filevals: dict[str, str] = {}
    for f in _candidate_env_files():
        for k, v in _parse_dotenv(f).items():
            filevals.setdefault(k, v)  # earlier files win

    def pick(cli, *keys, default=None):
        if cli is not None and cli != "":
            return cli
        for k in keys:
            if os.environ.get(k):
                return os.environ[k]
        for k in keys:
            if filevals.get(k):
                return filevals[k]
        return default

    resolved_base = pick(base_url, "AKT_BASE_URL", "APP_URL")
    resolved_email = pick(email, "AKT_EMAIL", "AKAUNTING_ADMIN_EMAIL")
    resolved_password = pick(password, "AKT_PASSWORD", "AKAUNTING_ADMIN_PASSWORD")
    resolved_company = pick(company, "AKT_COMPANY", default="1")

    missing = [
        name
        for name, val in [
            ("base url", resolved_base),
            ("email", resolved_email),
            ("password", resolved_password),
        ]
        if not val
    ]
    if missing:
        raise ConfigError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ".\nSet them via flags (--base-url/--email/--password), env vars "
            "(AKT_BASE_URL/AKT_EMAIL/AKT_PASSWORD), or a .env file."
        )

    try:
        company_id = int(resolved_company)
    except (TypeError, ValueError):
        raise ConfigError(f"Invalid company id: {resolved_company!r}")

    assert resolved_base and resolved_email and resolved_password  # guarded above
    return Config(
        base_url=resolved_base,
        email=resolved_email,
        password=resolved_password,
        company_id=company_id,
    )
