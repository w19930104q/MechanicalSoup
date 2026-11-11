import http.server
import os
import tempfile
import threading

import pytest
import setpath  # noqa:F401, must come before 'import mechanicalsoup'

import mechanicalsoup
from mechanicalsoup.utils import LinkNotFoundError


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Record every request and serve a configurable HTML page."""

    def do_GET(self):
        self._record()

    def do_POST(self):
        self._record()

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": self.rfile.read(length),
        })

        if self.server.page is not None:
            payload = self.server.page.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            payload = b"OK"
            content_type = "text/plain"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _RecordingHandler
    )
    httpd.requests = []
    httpd.page = None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def _base_url(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_selected_button_overrides_form(server):
    server.page = """
    <form method="get" action="/fallback">
      <input name="a" value="first" />
      <button name="go" value="Go" formaction="/submit"
              formmethod="post" formenctype="text/plain"></button>
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page")
    browser.select_form()
    browser.form.choose_submit("go")
    browser.submit_selected()

    request = server.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/submit"
    assert request["headers"]["Content-Type"] == "text/plain"
    assert request["body"] == b"a=first\r\ngo=Go\r\n"


def test_selected_input_overrides_form(server):
    server.page = """
    <form method="post" action="/fallback">
      <input name="a" value="first" />
      <input type="submit" name="go" value="Go" formaction="/submit"
             formmethod="get" formenctype="multipart/form-data" />
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.Browser()
    page = browser.get(base + "/page")
    response = browser.submit(page.soup.form, page.url)

    request = server.requests[-1]
    assert response.request.method == "GET"
    assert request["method"] == "GET"
    assert request["path"] == "/submit?a=first&go=Go"
    assert request["body"] == b""


def test_empty_action_submits_to_current_page(server):
    server.page = """
    <form method="post" action="">
      <input name="a" value="first" />
      <input type="submit" name="go" value="Go" formaction="" />
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page?x=1")
    browser.select_form()
    browser.submit_selected()

    request = server.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/page?x=1"
    assert request["body"] == b"a=first&go=Go"


def test_multipart_keeps_control_order_and_never_opens_paths(server):
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, b"SECRET")
        os.close(fd)
        filename = os.path.basename(path)
        server.page = f"""
        <form method="post" action="/upload"
              enctype="multipart/form-data">
          <input name="x" value="first" />
          <input type="file" name="x" value="{path}" />
          <input name="x" value="last" />
        </form>"""

        base = _base_url(server)
        browser = mechanicalsoup.StatefulBrowser()
        browser.open(base + "/page")
        browser.select_form()
        browser.submit_selected()

        request = server.requests[-1]
        body = request["body"]
        assert request["method"] == "POST"
        assert request["path"] == "/upload"
        assert b"multipart/form-data" in (
            request["headers"]["Content-Type"].encode()
        )
        first = body.index(b'name="x"\r\n\r\nfirst')
        file_part = body.index(
            f'name="x"; filename="{filename}"'.encode()
        )
        last = body.index(b'name="x"\r\n\r\nlast')
        assert first < file_part < last
        # The path in the form value must never be opened or uploaded.
        assert b"SECRET" not in body
    finally:
        os.remove(path)


def test_multipart_uploads_caller_opened_file_and_keeps_it_open(server):
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, b"OPENED")
        os.close(fd)
        server.page = """
        <form method="post" action="/upload"
              enctype="multipart/form-data">
          <input name="x" value="first" />
          <input type="file" name="x" />
          <input name="x" value="last" />
        </form>"""

        base = _base_url(server)
        browser = mechanicalsoup.StatefulBrowser()
        browser.open(base + "/page")
        browser.select_form()

        with open(path, "rb") as upload:
            browser.form.form.select(
                'input[type="file"]')[0]["value"] = upload
            browser.submit_selected()
            assert not upload.closed

        request = server.requests[-1]
        body = request["body"]
        filename = os.path.basename(path).encode()
        first = body.index(b'name="x"\r\n\r\nfirst')
        file_part = body.index(b'filename="' + filename + b'"')
        last = body.index(b'name="x"\r\n\r\nlast')
        assert first < file_part < last
        assert b"OPENED" in body
    finally:
        os.remove(path)


def test_get_file_field_contributes_only_filename(server):
    server.page = """
    <form method="get" action="/search" enctype="multipart/form-data">
      <input name="a" value="1" />
      <input type="file" name="f" value="/tmp/dir/secret.txt" />
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page")
    browser.select_form()
    browser.submit_selected()

    request = server.requests[-1]
    assert request["method"] == "GET"
    assert request["path"] == "/search?a=1&f=secret.txt"
    assert request["body"] == b""


def test_urlencoded_preserves_order_and_encodes_utf8(server):
    server.page = """
    <form method="post" action="/post"
          enctype="application/x-www-form-urlencoded">
      <input name="x" value="first" />
      <input name="x" value="h\xe9llo" />
      <input type="file" name="x" value="/tmp/dir/f.txt" />
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page")
    browser.select_form()
    browser.submit_selected()

    request = server.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/post"
    assert ("application/x-www-form-urlencoded" in
            request["headers"]["Content-Type"])
    assert request["body"] == b"x=first&x=h%C3%A9llo&x=f.txt"


def test_text_plain_uses_utf8_and_crlf(server):
    server.page = """
    <form method="post" action="/post" enctype="text/plain">
      <input name="x" value="first" />
      <textarea name="y">line1
line2</textarea>
      <input type="file" name="z" value="/tmp/dir/f.txt" />
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page")
    browser.select_form()
    browser.submit_selected()

    request = server.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/post"
    assert request["headers"]["Content-Type"] == "text/plain"
    assert request["body"] == (
        b"x=first\r\ny=line1\r\nline2\r\nz=f.txt\r\n"
    )


def test_disabled_controls_are_skipped_but_explicit_selection_raises(server):
    server.page = """
    <form method="post" action="/post">
      <input name="a" value="1" disabled />
      <input type="submit" name="disabled" value="Disabled" disabled />
      <input type="submit" name="enabled" value="Enabled" />
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page")
    browser.select_form()

    # The disabled submit is not chosen by default.
    assert browser.form.choose_submit(None)["name"] == "enabled"

    with pytest.raises(LinkNotFoundError):
        browser.form.choose_submit("disabled")

    browser.submit_selected()
    request = server.requests[-1]
    assert request["body"] == b"enabled=Enabled"


def test_browser_and_stateful_browser_submit_identically(server):
    server.page = """
    <form method="get" action="/fallback">
      <input name="a" value="first" />
      <button name="go" value="Go" formaction="/submit"
              formmethod="post" formenctype="text/plain"></button>
    </form>"""

    base = _base_url(server)

    browser = mechanicalsoup.Browser()
    page = browser.get(base + "/page")
    browser.submit(page.soup.form, page.url)

    stateful = mechanicalsoup.StatefulBrowser()
    stateful.open(base + "/page")
    stateful.select_form()
    stateful.submit_selected()

    first, second = [r for r in server.requests if r["method"] == "POST"]
    assert first["method"] == second["method"] == "POST"
    assert first["path"] == second["path"] == "/submit"
    assert first["body"] == second["body"] == b"a=first\r\ngo=Go\r\n"


def test_repeated_submissions_do_not_accumulate_parameters(server):
    server.page = """
    <form method="post" action="/post">
      <input name="a" value="1" />
      <button type="submit" name="one" value="One">One</button>
      <input name="b" value="2" />
      <button type="submit" name="two" value="Two">Two</button>
    </form>"""

    base = _base_url(server)
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(base + "/page")
    browser.select_form()

    browser.form.choose_submit("one")
    browser.submit_selected()
    browser.select_form()
    browser.form.choose_submit("two")
    browser.submit_selected()

    first, second = [r for r in server.requests if r["method"] == "POST"]
    assert first["body"] == b"a=1&one=One&b=2"
    assert second["body"] == b"a=1&b=2&two=Two"
    # Submitting must not modify the form's DOM.
    browser.select_form()
    assert len(browser.form.form.select("button")) == 2
