"""Thin HTTP client for the Akaunting REST API.

Akaunting specifics baked in here:
  * HTTP Basic auth (admin email + password).
  * Every company-scoped request carries ``company_id`` as a query param.
  * The ``contacts`` and ``documents`` controllers derive their ACL permission
    from a ``search=type:<x>`` query param, so for those endpoints the caller
    must pass ``type_scope`` on *every* verb (GET/POST/PUT/DELETE) or the API
    returns 403 "necessary access rights".
  * Responses are JSON-API-ish: a single object under ``data`` for show/create,
    a list under ``data`` plus ``meta`` pagination for index.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterator
from urllib.parse import unquote

import requests

from .config import Config

# Imunify360 / generic WAF + throttle responses we transparently retry.
_RETRY_STATUS = {429, 503}
_WAF_MARKERS = ("imunify360", "bot-protection", "bot protection", "access denied by")
_RETRY_BACKOFF = [2.0, 5.0, 10.0, 20.0]


class ApiError(Exception):
    """An error returned by the Akaunting API (non-2xx)."""

    def __init__(self, status: int, message: str, errors: dict | None = None, body: Any = None):
        self.status = status
        self.message = message
        self.errors = errors or {}
        self.body = body
        super().__init__(self._format())

    def _format(self) -> str:
        out = f"HTTP {self.status}: {self.message}"
        for field, msgs in self.errors.items():
            if isinstance(msgs, list):
                for m in msgs:
                    out += f"\n  - {field}: {m}"
            else:
                out += f"\n  - {field}: {msgs}"
        return out


def _is_transient(resp: requests.Response) -> bool:
    """True for throttle / WAF responses worth retrying."""
    if resp.status_code in _RETRY_STATUS:
        return True
    body = (resp.text or "").lower()
    return any(m in body for m in _WAF_MARKERS)


class Client:
    def __init__(self, config: Config, *, timeout: float = 30.0, max_retries: int = 4,
                 throttle: float = 0.0):
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self.throttle = throttle  # min seconds between requests (anti-WAF)
        self._last_request = 0.0
        self._settings_cache: dict[str, Any] = {}
        self._settings_loaded = False
        self._web_authed = False  # whether a web (session-cookie) login has run
        self._session = requests.Session()
        self._session.auth = (config.email, config.password)
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "akt/0.2 (+akaunting-cli)",
            }
        )

    # ---- low level -----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        form: Any = None,
        files: Any = None,
        type_scope: str | None = None,
    ) -> Any:
        """Perform an API request.

        Bodies are mutually exclusive:
          * ``json_body`` — serialized as JSON (the default surface).
          * ``form`` + ``files`` — a multipart/form-data upload. ``form`` is an
            iterable of ``(key, value)`` pairs (repeated keys allowed for
            PHP-style ``attachment[]`` / ``items[0][name]`` encoding) and
            ``files`` an iterable of ``(field, (filename, bytes, mime))``. The
            hardcoded ``Content-Type: application/json`` session header is
            dropped for these so ``requests`` sets the multipart boundary.
        """
        url = f"{self.config.api_root}/{path.lstrip('/')}"
        query: dict[str, Any] = {"company_id": self.config.company_id}
        if type_scope:
            # merge into a search-string; preserve any caller-provided search
            existing = (params or {}).get("search", "")
            scope = f"type:{type_scope}"
            query["search"] = f"{scope} {existing}".strip() if existing else scope
        if params:
            for k, v in params.items():
                if v is None:
                    continue
                if k == "search" and type_scope:
                    continue  # already merged
                query[k] = v

        multipart = files is not None or form is not None
        data = None
        headers = None
        if multipart:
            data = list(form or [])
            # Drop the JSON content-type so requests builds the multipart body
            # (with its boundary) itself.
            headers = {"Content-Type": None}
        elif json_body is not None:
            data = json.dumps(json_body)

        attempt = 0
        while True:
            if self.throttle > 0:
                wait = self.throttle - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self._session.request(
                method.upper(),
                url,
                params=query,
                data=data,
                files=files,
                headers=headers,
                timeout=self.timeout,
            )
            if attempt < self.max_retries and _is_transient(resp):
                delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                time.sleep(delay)
                attempt += 1
                continue
            return self._handle(resp)

    @staticmethod
    def _waf_blocked(resp: requests.Response) -> bool:
        body = (resp.text or "").lower()
        return any(m in body for m in _WAF_MARKERS)

    def _handle(self, resp: requests.Response) -> Any:
        if self._waf_blocked(resp):
            raise ApiError(
                resp.status_code,
                "Blocked by Imunify360 bot-protection after retries. "
                "Whitelist this machine's public IP in the host's Imunify360 / "
                "cPanel firewall, or retry later.",
            )
        if resp.status_code == 204 or not resp.content:
            if resp.ok:
                return None
            raise ApiError(resp.status_code, resp.reason or "Request failed")
        try:
            payload = resp.json()
        except ValueError:
            if resp.ok:
                return resp.text
            raise ApiError(resp.status_code, resp.text[:500] or resp.reason or "Request failed")

        if not resp.ok:
            message = "Request failed"
            errors = None
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error") or message
                errors = payload.get("errors")
            raise ApiError(resp.status_code, message, errors, payload)
        return payload

    # ---- convenience verbs --------------------------------------------

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, json_body: Any, **kw) -> Any:
        return self.request("POST", path, json_body=json_body, **kw)

    def put(self, path: str, json_body: Any, **kw) -> Any:
        return self.request("PUT", path, json_body=json_body, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self.request("DELETE", path, **kw)

    def post_multipart(self, path: str, form: Any, files: Any, **kw) -> Any:
        """Create a record with a multipart body (e.g. carrying attachments)."""
        return self.request("POST", path, form=form, files=files, **kw)

    def put_multipart(self, path: str, form: Any, files: Any, **kw) -> Any:
        """Update an existing record with a multipart body.

        PHP does not populate ``$_FILES`` for a real PUT, so multipart updates
        are sent as POST with a spoofed ``_method=PATCH`` field (the same trick
        the Akaunting web UI uses)."""
        form = [("_method", "PATCH"), *form]
        return self.request("POST", path, form=form, files=files, **kw)

    # ---- attachment download (web-session surface) --------------------

    def _web_login(self) -> None:
        """Authenticate a browser-style session for the ``/uploads`` routes.

        Attachment bytes are only served by ``GET /{company}/uploads/{id}/download``
        behind the web ``auth`` guard — the ``/api`` Basic-auth surface exposes
        attachment *metadata* but not the file. So we log in the same way the web
        UI does (scrape the login form's CSRF ``_token``, POST credentials) and
        reuse the resulting session cookie."""
        if self._web_authed:
            return
        login_url = f"{self.config.web_root}/auth/login"
        # No JSON content-type on these calls: the login form is url-encoded and
        # the pages are HTML.
        resp = self._session.get(login_url, headers={"Content-Type": None},
                                 timeout=self.timeout)
        if self._waf_blocked(resp):
            raise ApiError(resp.status_code, "Blocked by bot-protection during web login.")
        token = self._csrf_token(resp.text)
        if not token:
            raise ApiError(resp.status_code,
                           "Could not find a login CSRF token; web login failed.")
        post = self._session.post(
            login_url,
            data={"_token": token, "email": self.config.email,
                  "password": self.config.password, "remember": "on"},
            headers={"Content-Type": None},
            allow_redirects=True,
            timeout=self.timeout,
        )
        if self._waf_blocked(post):
            raise ApiError(post.status_code, "Blocked by bot-protection during web login.")
        self._web_authed = True

    def _csrf_token(self, html: str) -> str | None:
        m = re.search(r'name="_token"[^>]*value="([^"]+)"', html)
        if m:
            return m.group(1)
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
        if m:
            return m.group(1)
        # Fall back to the XSRF-TOKEN cookie (Laravel accepts it as the token).
        cookie = self._session.cookies.get("XSRF-TOKEN")
        return unquote(cookie) if cookie else None

    def download_media(self, media_id: int | str) -> "tuple[str, bytes]":
        """Return ``(filename, content)`` for an attachment media id.

        Logs in a web session on first use (cached for the process)."""
        self._web_login()
        url = f"{self.config.web_root}/{self.config.company_id}/uploads/{media_id}/download"
        resp = self._session.get(url, headers={"Content-Type": None},
                                 allow_redirects=False, timeout=self.timeout)
        if resp.status_code in (301, 302, 303, 307, 308):
            raise ApiError(resp.status_code,
                           "Attachment download redirected (web session not "
                           "authenticated). Check the admin credentials.")
        if not resp.ok or not resp.content:
            raise ApiError(resp.status_code,
                           f"Attachment media {media_id} not found or empty.")
        filename = self._disposition_filename(resp.headers.get("Content-Disposition", ""))
        return filename or f"attachment-{media_id}", resp.content

    @staticmethod
    def _disposition_filename(disposition: str) -> str | None:
        name = None
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.IGNORECASE)
        if m:
            name = unquote(m.group(1).strip().strip('"'))
        else:
            m = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
        if not name:
            return None
        # Defense-in-depth: a server-supplied name must never carry a path.
        name = os.path.basename(name.replace("\\", "/"))
        return name or None

    # ---- higher level helpers -----------------------------------------

    def list(
        self,
        path: str,
        *,
        type_scope: str | None = None,
        search: str | None = None,
        params: dict | None = None,
        all_pages: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Return the ``data`` list. Optionally follow pagination."""
        p: dict[str, Any] = dict(params or {})
        if search:
            p["search"] = search
        if limit:
            p["limit"] = limit
        page = 1
        out: list[dict] = []
        while True:
            p["page"] = page
            payload = self.get(path, params=p, type_scope=type_scope)
            data = payload.get("data", []) if isinstance(payload, dict) else []
            out.extend(data)
            if not all_pages:
                break
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            last = meta.get("last_page", page)
            if page >= last:
                break
            page += 1
        return out

    def iter_pages(self, path: str, *, type_scope: str | None = None, search: str | None = None,
                   params: dict | None = None) -> Iterator[dict]:
        p: dict[str, Any] = dict(params or {})
        if search:
            p["search"] = search
        page = 1
        while True:
            p["page"] = page
            payload = self.get(path, params=p, type_scope=type_scope)
            for row in (payload.get("data", []) if isinstance(payload, dict) else []):
                yield row
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            last = meta.get("last_page", page)
            if page >= last:
                break
            page += 1

    def show(self, path: str, ident: str | int, *, type_scope: str | None = None) -> dict:
        payload = self.get(f"{path}/{ident}", type_scope=type_scope)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def setting(self, key: str, default: Any = None) -> Any:
        """Read a single company setting value by key (cached after first call)."""
        if not self._settings_loaded:
            rows = self.list("settings", all_pages=True)
            self._settings_cache = {str(r.get("key")): r.get("value") for r in rows}
            self._settings_loaded = True
        return self._settings_cache.get(key, default)
