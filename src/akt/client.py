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
                 throttle: float = 0.0, debug: bool = False):
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self.throttle = throttle  # min seconds between requests (anti-WAF)
        self.debug = debug  # log each request attempt (status/timing/retries) to stderr
        self._last_request = 0.0
        self._settings_cache: dict[str, Any] = {}
        self._settings_loaded = False
        self._capabilities: dict[str, bool] = {}  # optional-module probes (e.g. akt-api)
        self._ref_cache: dict[str, list] = {}  # in-process cache for static reference lists
        self._txn_number_max: int | None = None  # seeded once, incremented per create in a batch
        self._web_authed = False  # whether a web (session-cookie) login has run
        self._session = requests.Session()
        self._session.auth = (config.email, config.password)
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "akt/0.3 (+akaunting-cli)",
            }
        )
        # Secret shared with the AktApi server module: requests bearing it skip
        # the API rate limit (60/min throttle:api) — akt is a trusted admin CLI
        # doing bulk entry, not the browser UI the limit is meant for.
        if getattr(config, "bypass_token", None):
            self._session.headers["X-Akt-Bypass"] = config.bypass_token

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
        started = time.monotonic()
        body_len = len(data) if isinstance(data, (str, bytes)) else None
        while True:
            if self.throttle > 0:
                wait = self.throttle - (time.monotonic() - self._last_request)
                if wait > 0:
                    if self.debug and wait > 0.01:
                        self._dbg(f"throttle sleep {wait:.2f}s")
                    time.sleep(wait)
            self._last_request = time.monotonic()
            t0 = time.monotonic()
            resp = self._session.request(
                method.upper(),
                url,
                params=query,
                data=data,
                files=files,
                headers=headers,
                timeout=self.timeout,
            )
            dur = time.monotonic() - t0
            transient = _is_transient(resp)
            will_retry = attempt < self.max_retries and transient
            if self.debug:
                extra = ""
                if transient or not resp.ok:
                    snippet = " ".join((resp.text or "").split())[:140]
                    extra = f" body={snippet!r}"
                note = ""
                if will_retry:
                    delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    note = f" -> retry {attempt + 1}/{self.max_retries} after {delay:.1f}s backoff"
                size = f" reqbytes={body_len}" if body_len is not None else ""
                self._dbg(
                    f"{method.upper()} {path} attempt={attempt} status={resp.status_code} "
                    f"{dur * 1000:.0f}ms transient={transient}{size}{note}{extra}"
                )
                # On any retry/failure dump the full exchange so the WAF/rate-limit
                # response (Retry-After, rule id, markers) is visible. Creds redacted.
                if transient or not resp.ok:
                    self._dbg_exchange(resp)
            if will_retry:
                delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                time.sleep(delay)
                attempt += 1
                continue
            if self.debug and attempt:
                self._dbg(
                    f"{method.upper()} {path} settled status={resp.status_code} after "
                    f"{attempt} retr{'y' if attempt == 1 else 'ies'}, total {time.monotonic() - started:.1f}s"
                )
            return self._handle(resp)

    def _dbg(self, msg: str) -> None:
        """Write a debug line to stderr (enabled by --debug / AKT_DEBUG)."""
        import sys
        sys.stderr.write(f"[akt-debug] {msg}\n")
        sys.stderr.flush()

    _REDACT_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}

    def _dbg_exchange(self, resp: requests.Response) -> None:
        """Dump the full request + response (headers/body) for a retried/failed
        call so the WAF / rate-limit response is fully visible. Credential-bearing
        headers are redacted."""
        def _fmt(items) -> str:
            out = []
            for k, v in items:
                if k.lower() in self._REDACT_HEADERS:
                    v = "<redacted>"
                out.append(f"      {k}: {v}")
            return "\n".join(out) if out else "      (none)"

        req = resp.request
        body = req.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        lines = [
            "full exchange:",
            f"    -> {req.method} {req.url}",
            "    request headers:",
            _fmt(req.headers.items()),
        ]
        if body:
            lines.append(f"    request body: {str(body)[:2000]}")
        lines += [
            f"    <- HTTP {resp.status_code} {resp.reason} (elapsed {resp.elapsed.total_seconds():.2f}s)",
            "    response headers:",
            _fmt(resp.headers.items()),
            f"    response body: {' '.join((resp.text or '').split())[:1200]}",
        ]
        self._dbg("\n".join(lines))

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

    def has_ledger_api(self) -> bool:
        """True if the akt-api companion module is installed (GET /api/akt-api/ledgers
        is reachable). A 404 means the module is absent. Cached per client."""
        if "ledger_api" not in self._capabilities:
            try:
                self.get("akt-api/ledgers", params={"limit": 1})
                self._capabilities["ledger_api"] = True
            except ApiError as e:
                if e.status == 404:
                    self._capabilities["ledger_api"] = False
                else:
                    raise
        return self._capabilities["ledger_api"]

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

    def _xsrf_header(self) -> dict:
        """CSRF header for a session web request.

        Laravel accepts the *encrypted* ``XSRF-TOKEN`` cookie value echoed back in
        the ``X-XSRF-TOKEN`` header (it decrypts that to the session token) — this
        is exactly what Akaunting's axios frontend does, and is far more robust
        than scraping a per-page ``_token`` (Akaunting emits no ``csrf-token``
        meta tag). ``requests`` already stores the cookie; we just surface it as a
        header."""
        cookie = self._session.cookies.get("XSRF-TOKEN")
        return {"X-XSRF-TOKEN": unquote(cookie)} if cookie else {}

    def web_json(self, method: str, path: str,
                 form: "list[tuple[str, str]] | None" = None) -> Any:
        """Call a session-authenticated web (non-``/api``) route that answers JSON.

        Some module features are exposed only on the CSRF-protected web surface,
        not the Basic-auth ``/api`` one — notably the Double-Entry
        chart-of-accounts CRUD (``apiResource`` publishes it read-only). This logs
        in a web session (cached for the process), attaches the CSRF token plus the
        ``X-Requested-With``/``Accept`` headers Laravel needs to reply with JSON
        instead of an HTML redirect, and unwraps Akaunting's
        ``{success, error, data, message}`` AJAX envelope. ``path`` is relative to
        ``{web_root}/{company_id}/``; use a real ``PATCH``/``DELETE`` method (the
        resource controller and its FormRequest honor it directly)."""
        self._web_login()
        url = f"{self.config.web_root}/{self.config.company_id}/{path.lstrip('/')}"
        headers = {
            # Drop the JSON content-type so requests url-encodes the form itself.
            "Content-Type": None,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            **self._xsrf_header(),
        }
        resp = self._session.request(
            method.upper(), url, data=list(form or []), headers=headers,
            allow_redirects=False, timeout=self.timeout,
        )
        if self._waf_blocked(resp):
            raise ApiError(resp.status_code, "Blocked by bot-protection on web route.")
        if resp.status_code in (301, 302, 303, 307, 308):
            raise ApiError(
                resp.status_code,
                "Web route redirected instead of returning JSON — the session "
                "isn't authenticated or the CSRF token was rejected. Check the "
                "admin credentials.",
            )
        payload = self._handle(resp)  # raises ApiError (with validation errors) on non-2xx
        # Akaunting's AJAX envelope: a 200 can still carry a business-rule failure
        # (e.g. deleting an account that has ledgers) as {success:false, error:true}.
        if isinstance(payload, dict) and payload.get("error"):
            raise ApiError(400, payload.get("message") or "Request failed")
        if isinstance(payload, dict) and "data" in payload and "success" in payload:
            return payload["data"]
        return payload

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

    def list_ref(self, path: str, *, type_scope: str | None = None) -> list[dict]:
        """Cached ``list(all_pages=True)`` for STATIC reference data
        (chart-of-accounts, categories, …). Cached for this client's lifetime,
        so a batch of creates fetches each reference table once instead of
        re-pulling it per row. Not for mutable data (transactions, documents)."""
        if path not in self._ref_cache:
            self._ref_cache[path] = self.list(path, type_scope=type_scope, all_pages=True)
        return self._ref_cache[path]

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
