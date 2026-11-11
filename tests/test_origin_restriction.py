"""Tests for the per-browser allowed-origin restriction.

The network tests use two independent local HTTP servers (two distinct
origins) so that "no packet was sent to the rejected destination" can be
asserted via per-server hit counters, without any external network access.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import urlsplit

import pytest
import requests
import requests_mock
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from utils import prepare_mock_browser

import mechanicalsoup
from mechanicalsoup.origin import OriginNotAllowedError, origin_of


class _OriginHandler(BaseHTTPRequestHandler):
    """Request handler backed by a per-server dict of path -> behavior.

    A behavior is either a string (HTML body returned with 200) or a tuple
    ``(status, location)`` producing a redirect.
    """

    def log_message(self, format, *args):  # silence test output
        pass

    def _route(self):
        server = self.server
        with server.lock:
            server.hits[self.path] = server.hits.get(self.path, 0) + 1
            behavior = server.routes.get(self.path)
        if behavior is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if isinstance(behavior, tuple):
            status, location = behavior
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            if self.path == "/set-cookie":
                self.send_header("Set-Cookie", "flavor=choco; Path=/")
            self.end_headers()
            return
        body = behavior.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/set-cookie":
            self.send_header("Set-Cookie", "flavor=choco; Path=/")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _route
    do_POST = _route


class _OriginServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OriginHandler)
        self.httpd.routes = {}
        self.httpd.hits = {}
        self.httpd.lock = threading.Lock()
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.url = f"http://{host}:{port}"
        self.origin = origin_of(self.url)

    def set_route(self, path, behavior):
        self.httpd.routes[path] = behavior

    def hits(self, path):
        return self.httpd.hits.get(path, 0)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


@pytest.fixture
def origins():
    """Two independent local HTTP origins, each tracking per-path hits."""
    primary = _OriginServer()
    secondary = _OriginServer()
    primary.set_route("/page", "<html><body>primary</body></html>")
    secondary.set_route("/page", "<html><body>secondary</body></html>")
    try:
        yield primary, secondary
    finally:
        primary.stop()
        secondary.stop()


def _restricted_browser(allowed):
    return mechanicalsoup.StatefulBrowser(allowed_origins=allowed)


# ---------------------------------------------------------------------------
# Origin normalization and configuration validation (no I/O)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("http://example.com", "http://example.com:80"),
    ("http://example.com:80/", "http://example.com:80"),
    ("http://Example.COM/path?x=1#f", "http://example.com:80"),
    ("HTTP://EXAMPLE.COM:8080", "http://example.com:8080"),
    ("https://example.com", "https://example.com:443"),
    ("https://Example.COM:443/a", "https://example.com:443"),
    ("https://example.com:8443", "https://example.com:8443"),
])
def test_origin_normalization(raw, expected):
    assert origin_of(raw) == expected


@pytest.mark.parametrize("raw", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "mock://example.com",
    "http://",
    "not-a-url",
    "://example.com",
])
def test_origin_of_rejects_non_http_or_malformed(raw):
    with pytest.raises(ValueError):
        origin_of(raw)


@pytest.mark.parametrize("config", [
    ["ftp://example.com"],
    ["file:///tmp/x"],
    ["http://good.example", "bad-scheme://x"],
    ["http://"],
    [42],
    [None],
])
def test_invalid_config_rejected_before_io(config):
    with pytest.raises((ValueError, TypeError)):
        mechanicalsoup.Browser(allowed_origins=config)
    with pytest.raises((ValueError, TypeError)):
        mechanicalsoup.StatefulBrowser(allowed_origins=config)


def test_default_disabled_uses_plain_session():
    browser = mechanicalsoup.Browser()
    assert browser.allowed_origins is None
    assert type(browser.session) is requests.Session
    browser.close()


def test_empty_allow_set_blocks_everything():
    browser = mechanicalsoup.Browser(allowed_origins=[])
    assert browser.allowed_origins == frozenset()
    with pytest.raises(OriginNotAllowedError):
        browser.get("http://anything.example/")
    browser.close()


def test_allowed_origins_property_normalized():
    browser = mechanicalsoup.Browser(
        allowed_origins=["http://Example.COM:80", "https://other.example/x"])
    assert browser.allowed_origins == frozenset([
        "http://example.com:80", "https://other.example:443"])
    browser.close()


def test_add_allowed_origin_runtime_and_validation():
    browser = mechanicalsoup.Browser(allowed_origins=["http://a.example"])
    browser.add_allowed_origin("https://B.Example:443/")
    assert browser.allowed_origins == frozenset([
        "http://a.example:80", "https://b.example:443"])
    with pytest.raises(ValueError):
        browser.add_allowed_origin("ftp://nope.example")
    browser.close()

    disabled = mechanicalsoup.Browser()
    with pytest.raises(RuntimeError):
        disabled.add_allowed_origin("http://a.example")
    disabled.close()


# ---------------------------------------------------------------------------
# Every entry point is guarded
# ---------------------------------------------------------------------------

def test_blocked_initial_request_sends_nothing(origins):
    primary, secondary = origins
    browser = _restricted_browser([primary.origin])
    with pytest.raises(OriginNotAllowedError) as exc:
        browser.get(secondary.url + "/page")
    err = exc.value
    assert err.url == secondary.url + "/page"
    assert err.origin == secondary.origin
    assert secondary.hits("/page") == 0
    browser.close()


@pytest.mark.parametrize("verb", ["get", "post", "put", "request"])
def test_browser_verbs_are_guarded(origins, verb):
    primary, secondary = origins
    browser = mechanicalsoup.Browser(allowed_origins=[primary.origin])
    method = "POST" if verb == "request" else verb.upper()
    with pytest.raises(OriginNotAllowedError):
        if verb == "request":
            browser.request(method, secondary.url + "/page")
        else:
            getattr(browser, verb)(secondary.url + "/page")
    assert secondary.hits("/page") == 0
    browser.close()


def test_stateful_open_and_open_relative_guarded(origins):
    primary, secondary = origins
    browser = _restricted_browser([primary.origin])
    with pytest.raises(OriginNotAllowedError):
        browser.open(secondary.url + "/page")
    browser.open(primary.url + "/page")
    with pytest.raises(OriginNotAllowedError):
        browser.open_relative(secondary.url + "/page")
    assert secondary.hits("/page") == 0
    browser.close()


def test_follow_link_guarded(origins):
    primary, secondary = origins
    primary.set_route(
        "/links",
        f'<html><body><a href="{secondary.url}/page">out</a></body></html>')
    browser = _restricted_browser([primary.origin])
    browser.open(primary.url + "/links")
    with pytest.raises(OriginNotAllowedError):
        browser.follow_link(link_text="out")
    assert secondary.hits("/page") == 0
    browser.close()


def test_download_link_guarded_and_partial_file_untouched(origins, tmp_path):
    primary, secondary = origins
    primary.set_route(
        "/links",
        f'<html><body><a href="{secondary.url}/page">out</a></body></html>')
    target = tmp_path / "download.bin"
    target.write_bytes(b"pre-existing content")
    browser = _restricted_browser([primary.origin])
    browser.open(primary.url + "/links")
    with pytest.raises(OriginNotAllowedError):
        browser.download_link(link_text="out", file=str(target))
    # No request sent, and any previously downloaded file is preserved.
    assert secondary.hits("/page") == 0
    assert target.read_bytes() == b"pre-existing content"
    browser.close()


def test_refresh_guarded(origins):
    primary, secondary = origins
    browser = _restricted_browser([primary.origin])
    browser.open(primary.url + "/page")
    # Tamper the stored request to point at the disallowed origin.
    browser._StatefulBrowser__state.request.prepare_url(
        secondary.url + "/page", None)
    with pytest.raises(OriginNotAllowedError):
        browser.refresh()
    assert secondary.hits("/page") == 0
    browser.close()


def test_form_submission_guarded(origins):
    primary, secondary = origins
    primary.set_route(
        "/form",
        f'<html><body><form method="post" action="{secondary.url}/submit">'
        '<input name="field" value="value"><button type="submit">'
        '</button></form></body></html>')
    browser = _restricted_browser([primary.origin])
    browser.open(primary.url + "/form")
    browser.select_form()
    with pytest.raises(OriginNotAllowedError):
        browser.submit_selected()
    assert secondary.hits("/submit") == 0
    browser.close()


def test_non_http_scheme_blocked_when_enabled():
    # When the restriction is enabled, only http(s) targets are accepted at
    # all: even a mounted custom-scheme adapter is rejected before I/O.
    browser, adapter = prepare_mock_browser()
    restricted = mechanicalsoup.StatefulBrowser(
        session=browser.session, allowed_origins=["http://only.example"])
    with pytest.raises((OriginNotAllowedError, ValueError)):
        restricted.open("mock://example.com")
    # The shared, unrestricted session still serves the mock scheme
    # directly (see test_shared_session_direct_use_unrestricted below).
    browser.close()


# ---------------------------------------------------------------------------
# Redirect handling: per-hop checks, multi-hop and bounce-back
# ---------------------------------------------------------------------------

def test_redirect_hop_blocked_sends_nothing(origins):
    primary, secondary = origins
    primary.set_route("/redir", (302, secondary.url + "/page"))
    browser = _restricted_browser([primary.origin])
    with pytest.raises(OriginNotAllowedError) as exc:
        browser.get(primary.url + "/redir")
    assert exc.value.url == secondary.url + "/page"
    assert primary.hits("/redir") == 1
    assert secondary.hits("/page") == 0
    browser.close()


def test_multi_hop_chain_allowed_then_blocked(origins):
    primary, secondary = origins
    # primary/redir -> secondary/redir2 -> secondary/landing (both hops on
    # the disallowed origin must never be requested).
    secondary.set_route("/redir2", (302, secondary.url + "/landing"))
    secondary.set_route("/landing", "nope")
    primary.set_route("/redir", (302, secondary.url + "/redir2"))
    browser = _restricted_browser([primary.origin])
    with pytest.raises(OriginNotAllowedError):
        browser.get(primary.url + "/redir")
    assert primary.hits("/redir") == 1
    assert secondary.hits("/redir2") == 0
    assert secondary.hits("/landing") == 0
    browser.close()


def test_bounce_back_chain_succeeds(origins):
    primary, secondary = origins
    # A same-origin multi-hop chain bouncing back through the allowed origin.
    primary.set_route("/bounce", (302, primary.url + "/landing"))
    primary.set_route("/landing", "<html><body>landed</body></html>")
    browser = _restricted_browser([primary.origin, secondary.origin])
    resp = browser.get(primary.url + "/bounce")
    assert resp.status_code == 200
    assert urlsplit(resp.url).path == "/landing"
    assert [urlsplit(r.url).path for r in resp.history] == ["/bounce"]

    # And the cross-origin bounce works when both origins are allowed.
    primary.set_route("/cross", (302, secondary.url + "/page"))
    resp = browser.get(primary.url + "/cross")
    assert resp.url == secondary.url + "/page"
    browser.close()


@pytest.mark.parametrize("status, method_after", [
    (301, "GET"),
    (302, "GET"),
    (303, "GET"),
    (307, "POST"),
    (308, "POST"),
])
def test_redirect_method_semantics_preserved(origins, status, method_after):
    primary, secondary = origins

    class _MethodHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _handle(self):
            if self.path == "/redirect":
                body = b""
                self.send_response(status)
                self.send_header("Location", "/landing")
            else:
                # Echo the request verb so the test can observe which method
                # requests used for the redirected hop.
                body = self.command.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _handle
        do_POST = _handle

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MethodHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        browser = mechanicalsoup.Browser(allowed_origins=[origin_of(base)])
        resp = browser.post(base + "/redirect", data={"k": "v"})
        assert resp.text == method_after
        browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_allow_redirects_false_returns_raw_response(origins):
    primary, secondary = origins
    primary.set_route("/redir", (302, secondary.url + "/page"))
    browser = _restricted_browser([primary.origin])
    resp = browser.get(primary.url + "/redir", allow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == secondary.url + "/page"
    # The blocked target was never contacted.
    assert secondary.hits("/page") == 0
    browser.close()


# ---------------------------------------------------------------------------
# State preservation / continuation after a block
# ---------------------------------------------------------------------------

def test_state_preserved_after_block(origins):
    primary, secondary = origins
    primary.set_route(
        "/form",
        '<html><body><form method="get" action="/search">'
        '<input name="q" value="mechanicalsoup">'
        '</form></body></html>')
    browser = _restricted_browser([primary.origin])
    browser.open(primary.url + "/form")
    page, url = browser.page, browser.url
    browser.select_form()
    form = browser.form
    with pytest.raises(OriginNotAllowedError):
        browser.open(secondary.url + "/page")
    # page, url and the selected form are all retained.
    assert browser.page is page
    assert browser.url == url
    assert browser.form is form
    # ... and the browser keeps working on the allowed origin.
    resp = browser.open(primary.url + "/page")
    assert resp.status_code == 200
    browser.close()


def test_cookies_from_allowed_hop_kept_after_later_block(origins):
    primary, secondary = origins
    # Allowed hop sets a cookie, then redirects to the blocked origin.
    primary.set_route("/set-cookie", (302, secondary.url + "/page"))
    browser = _restricted_browser([primary.origin])
    with pytest.raises(OriginNotAllowedError):
        browser.get(primary.url + "/set-cookie")
    # Cookies are not rolled back: the allowed hop's cookie remains.
    assert browser.get_cookiejar().get("flavor") == "choco"
    assert secondary.hits("/page") == 0
    # The cookie is still sent on subsequent allowed requests.
    resp = browser.get(primary.url + "/page")
    assert resp.status_code == 200
    assert primary.hits("/page") == 1
    browser.close()


def test_adapters_request_params_and_cookies_preserved(origins):
    primary, secondary = origins
    adapter = requests_mock.Adapter()
    session = requests.Session()
    session.cookies.set("sessionid", "abc123")
    browser = mechanicalsoup.StatefulBrowser(
        session=session,
        allowed_origins=[primary.origin],
        requests_adapters={"mock://": adapter})
    # The mounted adapter is reachable on both sessions (shared adapters
    # dict); the mock scheme itself is rejected on the restricted browser.
    adapter.register_uri("GET", "mock://mocked/thing", text="mocked-body")
    with pytest.raises((OriginNotAllowedError, ValueError)):
        browser.open("mock://mocked/thing")
    assert session.get("mock://mocked/thing").text == "mocked-body"
    # ... while cookies remain shared with the original session.
    assert session.cookies.get("sessionid") == "abc123"
    assert browser.get_cookiejar().get("sessionid") == "abc123"
    # Request parameters (timeout) still flow through on an allowed origin.
    resp = browser.get(primary.url + "/page", timeout=30)
    assert resp.status_code == 200
    browser.close()


# ---------------------------------------------------------------------------
# Shared session: per-browser policies and direct use stay independent
# ---------------------------------------------------------------------------

def test_shared_session_independent_policies(origins):
    primary, secondary = origins
    shared = requests.Session()
    only_primary = mechanicalsoup.Browser(
        session=shared, allowed_origins=[primary.origin])
    both = mechanicalsoup.Browser(
        session=shared, allowed_origins=[primary.origin, secondary.origin])

    assert only_primary.allowed_origins == frozenset([primary.origin])
    assert both.allowed_origins == frozenset(
        [primary.origin, secondary.origin])

    with pytest.raises(OriginNotAllowedError):
        only_primary.get(secondary.url + "/page")
    # The second browser's wider policy is unaffected by the first's.
    resp = both.get(secondary.url + "/page")
    assert resp.status_code == 200
    assert secondary.hits("/page") == 1

    # Adding an origin to one does not leak into the other.
    only_primary.add_allowed_origin(secondary.origin)
    assert secondary.origin in only_primary.allowed_origins
    assert len(both.allowed_origins) == 2
    only_primary.close()
    both.close()


def test_shared_session_direct_use_unrestricted(origins):
    primary, secondary = origins
    shared = requests.Session()
    browser = mechanicalsoup.Browser(
        session=shared, allowed_origins=[primary.origin])
    # The restricted browser blocks the secondary origin...
    with pytest.raises(OriginNotAllowedError):
        browser.get(secondary.url + "/page")
    # ... but the caller's own session remains fully usable and unrestricted.
    assert type(shared) is requests.Session
    resp = shared.get(secondary.url + "/page")
    assert resp.status_code == 200
    assert secondary.hits("/page") == 1
    browser.close()


def test_shared_session_state_is_shared(origins):
    primary, _ = origins
    primary.set_route("/set-cookie", (302, primary.url + "/page"))
    shared = requests.Session()
    browser = mechanicalsoup.Browser(
        session=shared, allowed_origins=[primary.origin])
    browser.get(primary.url + "/set-cookie")
    # Cookies written through the restricted session land in the shared jar.
    assert shared.cookies.get("flavor") == "choco"
    # Note: don't call browser.close() — Browser.close() clears the cookies
    # of the underlying session by design (pre-existing behavior).
    browser.session = None  # prevent close() from touching the shared jar


# ---------------------------------------------------------------------------
# Historical default behavior regression
# ---------------------------------------------------------------------------

def test_default_behavior_unchanged(origins):
    primary, secondary = origins
    # No allowed_origins: cross-origin navigation and redirects just work.
    primary.set_route("/cross", (302, secondary.url + "/page"))
    browser = mechanicalsoup.StatefulBrowser()
    assert browser.allowed_origins is None
    resp = browser.open(primary.url + "/cross")
    assert resp.url == secondary.url + "/page"
    assert browser.url == secondary.url + "/page"
    browser.close()

    plain = mechanicalsoup.Browser()
    resp = plain.get(secondary.url + "/page")
    assert resp.status_code == 200
    plain.close()


def test_default_browser_still_supports_mock_scheme():
    # Existing tests rely on the custom mock:// adapter without restriction.
    browser, adapter = prepare_mock_browser()
    adapter.register_uri("GET", "mock://example.com/x", text="mock-body")
    resp = browser.get("mock://example.com/x")
    assert resp.text == "mock-body"
    browser.close()
