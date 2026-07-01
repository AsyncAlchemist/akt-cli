"""Offline tests for the client's multipart-upload and attachment-download paths.

No network: a fake ``requests``-style session records the calls the client makes
so we can assert the multipart body assembly, the PUT-via-POST method spoof, and
the web-session login flow used to fetch attachment bytes.
"""

from __future__ import annotations

import pytest

from types import SimpleNamespace

from akt.client import Client
from akt.commands import cmd_download_attachment
from akt.config import Config
from akt.registry import PAYMENT

pytestmark = pytest.mark.unit


def _config(base_url="https://acct.example.com/api"):
    return Config(base_url=base_url, email="admin@x.com", password="secret", company_id=1)


class _Resp:
    def __init__(self, status=200, content=b'{"data":{"id":1}}', headers=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.content = content
        self.reason = "OK"
        self.headers = headers or {}
        self._text = text

    @property
    def text(self):
        return self._text

    def json(self):
        import json
        return json.loads(self.content)


class _FakeSession:
    """Records request()/get()/post() calls; returns queued responses."""

    def __init__(self):
        self.headers = {}
        self.auth = None
        self.cookies = {}
        self.calls = []          # (method, url, kwargs) via request()
        self.gets = []
        self.posts = []
        self._queue = []

    def queue(self, resp):
        self._queue.append(resp)

    def _next(self, default):
        return self._queue.pop(0) if self._queue else default

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._next(_Resp())

    def get(self, url, **kw):
        self.gets.append((url, kw))
        return self._next(_Resp())

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self._next(_Resp())


def _client_with_fake():
    c = Client(_config())
    c._session = _FakeSession()  # type: ignore[assignment]
    return c, c._session


# --------------------------------------------------------------------------
# multipart upload
# --------------------------------------------------------------------------

def test_config_web_root_strips_api_suffix():
    assert _config("https://acct.example.com/api").web_root == "https://acct.example.com"
    assert _config("https://acct.example.com").web_root == "https://acct.example.com"
    assert _config("https://acct.example.com/api/").web_root == "https://acct.example.com"


def test_post_multipart_drops_json_content_type_and_passes_files():
    c, sess = _client_with_fake()
    files = [("attachment[]", ("a.pdf", b"x", "application/pdf"))]
    form = [("type", "income"), ("amount", "5")]
    c.post_multipart("transactions", form, files, type_scope="invoice")
    method, url, kw = sess.calls[-1]
    assert method == "POST"
    assert url.endswith("/transactions")
    # JSON content-type is dropped so requests sets the multipart boundary
    assert kw["headers"] == {"Content-Type": None}
    assert kw["files"] == files
    assert kw["data"] == form
    # company_id always injected; type_scope merged into search
    assert kw["params"]["company_id"] == 1
    assert kw["params"]["search"] == "type:invoice"


def test_put_multipart_spoofs_method_as_patch():
    c, sess = _client_with_fake()
    files = [("attachment[]", ("a.pdf", b"x", "application/pdf"))]
    c.put_multipart("documents/7/transactions/16", [("type", "expense")], files,
                    type_scope="bill")
    method, url, kw = sess.calls[-1]
    assert method == "POST"                       # real PUT would drop $_FILES
    assert kw["data"][0] == ("_method", "PATCH")  # spoof prepended first
    assert ("type", "expense") in kw["data"]
    assert url.endswith("/documents/7/transactions/16")


# --------------------------------------------------------------------------
# attachment download (web-session surface)
# --------------------------------------------------------------------------

def test_download_media_logs_in_then_streams_bytes():
    c, sess = _client_with_fake()
    # 1) GET /auth/login -> HTML with a CSRF token
    sess.queue(_Resp(text='<input name="_token" value="TOK123">'))
    # 2) POST /auth/login -> ok
    sess.queue(_Resp())
    # 3) GET /1/uploads/9/download -> the file bytes
    sess.queue(_Resp(content=b"PDFDATA",
                     headers={"Content-Disposition": 'attachment; filename="receipt.pdf"'}))

    name, content = c.download_media(9)
    assert name == "receipt.pdf"
    assert content == b"PDFDATA"

    # login happened against the web root (no /api), download is company-scoped
    assert sess.gets[0][0] == "https://acct.example.com/auth/login"
    assert sess.posts[0][0] == "https://acct.example.com/auth/login"
    assert sess.posts[0][1]["data"]["_token"] == "TOK123"
    assert sess.gets[-1][0] == "https://acct.example.com/1/uploads/9/download"
    # download must not follow redirects (a 302 => not authenticated)
    assert sess.gets[-1][1]["allow_redirects"] is False


def test_download_media_login_is_cached_across_calls():
    c, sess = _client_with_fake()
    sess.queue(_Resp(text='<input name="_token" value="TOK">'))  # login GET
    sess.queue(_Resp())                                          # login POST
    sess.queue(_Resp(content=b"A", headers={"Content-Disposition": "attachment; filename=a.pdf"}))
    sess.queue(_Resp(content=b"B", headers={"Content-Disposition": "attachment; filename=b.pdf"}))

    c.download_media(1)
    c.download_media(2)
    # only ONE login round-trip for both downloads
    assert len(sess.posts) == 1
    assert [g[0] for g in sess.gets] == [
        "https://acct.example.com/auth/login",
        "https://acct.example.com/1/uploads/1/download",
        "https://acct.example.com/1/uploads/2/download",
    ]


def test_download_media_raises_when_redirected_to_login():
    c, sess = _client_with_fake()
    sess.queue(_Resp(text='<input name="_token" value="TOK">'))
    sess.queue(_Resp())
    sess.queue(_Resp(status=302))  # redirect => session not authenticated
    from akt.client import ApiError
    with pytest.raises(ApiError):
        c.download_media(9)


def test_disposition_filename_variants():
    assert Client._disposition_filename('attachment; filename="a b.pdf"') == "a b.pdf"
    assert Client._disposition_filename("attachment; filename=plain.png") == "plain.png"
    assert Client._disposition_filename("attachment; filename*=UTF-8''r%C3%A9.pdf") == "ré.pdf"
    assert Client._disposition_filename("attachment") is None


def test_disposition_filename_strips_path_traversal():
    # a server-supplied name must never carry a path (defense-in-depth)
    assert Client._disposition_filename('attachment; filename="../../etc/passwd"') == "passwd"
    assert Client._disposition_filename('attachment; filename="..\\\\..\\\\evil.pdf"') == "evil.pdf"
    assert Client._disposition_filename("attachment; filename*=UTF-8''%2e%2e%2f%2e%2e%2fx.pdf") == "x.pdf"


class _DownloadClient:
    """Stub Client for cmd_download_attachment: canned record + a hostile filename."""

    def __init__(self, filename):
        self._filename = filename

    def show(self, path, ident, *, type_scope=None):
        return {"attachment": [{"id": 5, "filename": "x", "extension": "pdf"}]}

    def download_media(self, media_id):
        return self._filename, b"DATA"


def test_download_attachment_neutralizes_traversal_filename(tmp_path):
    """A server that returns filename='../../pwned' must not escape --out."""
    out = tmp_path / "out"
    ns = SimpleNamespace(id="1", out=str(out), media_id=None, json=False)
    cmd_download_attachment(PAYMENT, _DownloadClient("../../pwned.pdf"), ns)  # type: ignore[arg-type]
    # nothing written outside out/, and the basename lands safely inside it
    assert (out / "pwned.pdf").exists()
    assert not (tmp_path / "pwned.pdf").exists()
    assert not (tmp_path.parent / "pwned.pdf").exists()


def test_download_attachment_falls_back_when_name_is_dotdot(tmp_path):
    out = tmp_path / "out"
    ns = SimpleNamespace(id="1", out=str(out), media_id=None, json=False)
    cmd_download_attachment(PAYMENT, _DownloadClient(".."), ns)  # type: ignore[arg-type]
    assert (out / "attachment-5").exists()


def test_csrf_token_falls_back_to_xsrf_cookie():
    c, sess = _client_with_fake()
    assert c._csrf_token('<meta name="csrf-token" content="META">') == "META"
    assert c._csrf_token('<input name="_token" value="FORM">') == "FORM"
    sess.cookies = {"XSRF-TOKEN": "cookie%2Dval"}
    assert c._csrf_token("<html>no token</html>") == "cookie-val"


# --------------------------------------------------------------------------
# web_json — session/CSRF web-surface CRUD (chart-of-accounts)
# --------------------------------------------------------------------------

def _login_queue(sess):
    # GET /auth/login (form token) then POST /auth/login. Akaunting (Laravel)
    # sets an encrypted XSRF-TOKEN cookie the frontend echoes back as a header;
    # simulate that cookie being present after login.
    sess.queue(_Resp(text='<input name="_token" value="TOK">'))
    sess.queue(_Resp())
    sess.cookies = {"XSRF-TOKEN": "enc%2Dtoken"}      # url-encoded encrypted token


def test_web_json_logs_in_attaches_csrf_and_unwraps_envelope():
    c, sess = _client_with_fake()
    _login_queue(sess)
    sess.queue(_Resp(content=b'{"success":true,"error":false,'
                             b'"data":{"id":99,"code":1010},"message":""}'))

    data = c.web_json("POST", "double-entry/chart-of-accounts",
                      [("name", "Cash"), ("code", "1010")])
    assert data == {"id": 99, "code": 1010}          # envelope unwrapped to .data

    method, url, kw = sess.calls[-1]
    assert method == "POST"
    assert url == "https://acct.example.com/1/double-entry/chart-of-accounts"
    assert kw["data"] == [("name", "Cash"), ("code", "1010")]
    assert kw["allow_redirects"] is False
    h = kw["headers"]
    # Laravel decrypts the XSRF-TOKEN cookie echoed in X-XSRF-TOKEN (url-decoded)
    assert h["X-XSRF-TOKEN"] == "enc-token"
    assert h["X-Requested-With"] == "XMLHttpRequest"
    assert h["Accept"] == "application/json"
    assert h["Content-Type"] is None                 # let requests url-encode


def test_web_json_patch_and_delete_paths():
    c, sess = _client_with_fake()
    _login_queue(sess)
    sess.queue(_Resp(content=b'{"success":true,"error":false,"data":{"id":5},"message":""}'))
    c.web_json("PATCH", "double-entry/chart-of-accounts/5", [("name", "Cash")])
    method, url, _ = sess.calls[-1]
    assert method == "PATCH"                          # real method, no _method spoof
    assert url.endswith("/1/double-entry/chart-of-accounts/5")


def test_web_json_raises_on_business_rule_failure_envelope():
    """A 200 with {success:false, error:true} (e.g. account has ledgers) must raise."""
    from akt.client import ApiError
    c, sess = _client_with_fake()
    _login_queue(sess)
    sess.queue(_Resp(content=b'{"success":false,"error":true,"data":null,'
                             b'"message":"Cannot delete: has ledgers"}'))
    with pytest.raises(ApiError, match="has ledgers"):
        c.web_json("DELETE", "double-entry/chart-of-accounts/5")


def test_web_json_raises_on_redirect():
    """A 302 means the session isn't authenticated / CSRF was rejected."""
    from akt.client import ApiError
    c, sess = _client_with_fake()
    _login_queue(sess)
    sess.queue(_Resp(status=302))
    with pytest.raises(ApiError, match="redirect"):
        c.web_json("POST", "double-entry/chart-of-accounts", [("name", "X")])


def test_web_json_login_cached_across_calls():
    c, sess = _client_with_fake()
    _login_queue(sess)
    sess.queue(_Resp(content=b'{"success":true,"error":false,"data":{"id":1},"message":""}'))
    sess.queue(_Resp(content=b'{"success":true,"error":false,"data":{"id":2},"message":""}'))
    c.web_json("POST", "double-entry/chart-of-accounts", [("name", "A")])
    c.web_json("POST", "double-entry/chart-of-accounts", [("name", "B")])
    assert len(sess.posts) == 1                       # single login round-trip
