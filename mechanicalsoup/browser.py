from collections.abc import Mapping
import io
import os
import re
import tempfile
from typing import cast
import urllib.parse
import weakref
import webbrowser

import bs4
import bs4.dammit
import requests

from .__version__ import __title__, __version__
from .form import Form
from .utils import (
    LinkNotFoundError,
    is_disabled,
    is_submit_control,
)


_URLENCODED_ENCTYPE = "application/x-www-form-urlencoded"
_MULTIPART_ENCTYPE = "multipart/form-data"
_TEXT_PLAIN_ENCTYPE = "text/plain"


class _MultipartFields(list):
    """Ordered multipart fields that are truthy even when empty.

    Requests only switches to multipart encoding when its ``files``
    argument is truthy, but an empty form must still be sent with the
    multipart enctype.
    """

    def __bool__(self):
        return True


def _normalize_newlines(value):
    """Normalize line endings to CRLF, as required by form submission."""
    if not isinstance(value, str):
        return value
    return (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\r\n")
    )


def _to_text(value):
    """Convert a form value to text for text/plain serialization."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    return str(value)


def _basename(value):
    """Return a file control's filename without touching the filesystem."""
    if isinstance(value, io.IOBase):
        value = getattr(value, "name", "")
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, (str, bytes)):
        return os.path.basename(os.fsdecode(value))
    return ""


def _to_pairs(value):
    """Normalize a Requests data container into ordered (name, value) pairs."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(value.items())
    return list(value)


def _expand_pairs(pairs):
    """Flatten list/tuple values into repeated fields, like Requests does."""
    for name, value in pairs:
        if (
            isinstance(value, (list, tuple)) and
            not isinstance(value, (str, bytes))
        ):
            for item in value:
                yield name, item
        else:
            yield name, value


def _form_entries(form, submit):
    """Yield form controls in document order as (kind, name, value).

    ``kind`` is either ``"field"`` or ``"file"``. Disabled controls are
    omitted, and only the selected submit control contributes its value.
    """
    selector = ",".join(
        f"{tag}[name]" for tag in ("input", "button", "textarea", "select")
    )
    for tag in form.select(selector):
        name = tag.get("name")

        if is_disabled(tag):
            continue

        if tag.name == "input":
            tag_type = str(tag.get("type", "")).lower()
            if tag_type == "submit":
                # Skip submit controls that were not selected
                if tag is not submit:
                    continue
                value = tag.get("value", "")
            elif tag_type in ("button", "reset", "image"):
                # These input types are never successful controls on their
                # own. Only a selected submit control may contribute data.
                continue
            elif tag_type in ("radio", "checkbox"):
                if "checked" not in tag.attrs:
                    continue
                value = tag.get("value", "on")
            else:
                # browsers use empty string for inputs with missing values
                value = tag.get("value", "")

            if tag_type == "file":
                yield "file", name, value
            else:
                yield "field", name, value

        elif tag.name == "button":
            # Buttons are only included if they are the selected submit
            if tag is not submit or not is_submit_control(tag):
                continue
            yield "field", name, tag.get("value", "")

        elif tag.name == "textarea":
            yield "field", name, tag.text

        elif tag.name == "select":
            for value in _selected_values(tag):
                yield "field", name, value


def _selected_values(select):
    """Return the values a select control submits, in selection order."""
    options = []
    for option in select.select("option"):
        if is_disabled(option):
            continue
        if any(optgroup.has_attr("disabled")
               for optgroup in option.find_parents("optgroup")):
            continue
        options.append(option)

    selected = [
        option.get("value", option.text) for option in options
        if "selected" in option.attrs
    ]

    if "multiple" in select.attrs:
        return selected
    if selected:
        # A standard select element only allows one option to be selected,
        # but browsers pick the last one if somehow multiple are marked.
        return [selected[-1]]
    if options:
        # Selects the first option if none are selected
        return [options[0].get("value", options[0].text)]
    return []


def _submitter_attr(form, submit, attribute, form_attribute):
    """Return a submit control's attribute, falling back to the form's."""
    if (
        isinstance(submit, bs4.element.Tag) and
        is_submit_control(submit) and
        submit.has_attr(attribute)
    ):
        return submit.get(attribute)
    return form.get(form_attribute)


def _effective_method(form, submit):
    """Return the HTTP method, honoring the selected submit's override."""
    method = _submitter_attr(form, submit, "formmethod", "method")
    if method is None:
        return "get"
    method = str(method).strip()
    # Per the HTML specification, an empty or missing formmethod means GET.
    return method or "get"


def _effective_enctype(form, submit):
    """Return the form encoding, honoring the selected submit's override."""
    enctype = _submitter_attr(form, submit, "formenctype", "enctype")
    if enctype is None:
        return _URLENCODED_ENCTYPE
    enctype = str(enctype).strip().lower()
    if enctype in (
        _URLENCODED_ENCTYPE, _MULTIPART_ENCTYPE, _TEXT_PLAIN_ENCTYPE
    ):
        return enctype
    # Invalid enctypes fall back to the default form encoding.
    return _URLENCODED_ENCTYPE


def _effective_action(form, submit):
    """Return the form action, honoring the selected submit's override."""
    return _submitter_attr(form, submit, "formaction", "action")


def _multipart_file_value(value):
    """Return a multipart (filename, content) pair for a file control.

    Only file objects explicitly opened by the caller are uploaded. A
    string (e.g. a malicious default value supplied by a server) is never
    opened; it only contributes its basename as the filename.
    """
    filename = _basename(value)
    if isinstance(value, io.IOBase):
        return filename, value
    return filename, b""


def _file_field_value(value):
    """Return the value a file control contributes without multipart."""
    return _basename(value)


class ResponseWithSoup(requests.Response):
    soup: bs4.BeautifulSoup | None


class Browser:
    """Builds a low-level Browser.

    It is recommended to use :class:`StatefulBrowser` for most applications,
    since it offers more advanced features and conveniences than Browser.

    :param session: Attach a pre-existing requests Session instead of
        constructing a new one.
    :param soup_config: Configuration passed to BeautifulSoup to affect
        the way HTML is parsed. Defaults to ``{'features': 'lxml'}``.
        Select the HTML parser with the ``features`` key, for example
        ``soup_config={'features': 'html.parser'}`` to use Python's built-in
        parser. Other choices include ``'lxml'`` and ``'html5lib'``;
        the corresponding parser package must be installed (``lxml`` is
        installed as a MechanicalSoup dependency).
        See BeautifulSoup's `parser comparison
        <https://www.crummy.com/software/BeautifulSoup/bs4/doc/#installing-a-parser>`__
        and `differences between parsers
        <https://www.crummy.com/software/BeautifulSoup/bs4/doc/#differences-between-parsers>`__
        for help choosing a parser.
        If overridden, it is highly recommended to `specify a parser
        <https://www.crummy.com/software/BeautifulSoup/bs4/doc/#specifying-the-parser-to-use>`__.
        Otherwise, BeautifulSoup will issue a warning and pick one for
        you, but the parser it chooses may be different on different
        machines.
    :param requests_adapters: Configuration passed to requests, to affect
        the way HTTP requests are performed.
    :param raise_on_404: If True, raise :class:`LinkNotFoundError`
        when visiting a page triggers a 404 Not Found error.
    :param user_agent: Set the user agent header to this value.

    """
    def __init__(
        self,
        session: requests.Session | None = None,
        soup_config=(('features', 'lxml'),),
        requests_adapters=None,
        raise_on_404=False,
        user_agent=None,
    ):
        self.raise_on_404 = raise_on_404
        self.session = session or requests.Session()

        self._finalize = weakref.finalize(self.session, self.close)

        self.set_user_agent(user_agent)

        if requests_adapters is not None:
            for adaptee, adapter in requests_adapters.items():
                self.session.mount(adaptee, adapter)

        self.soup_config = dict(soup_config or ())

    # Regex identifying html-like response content. Allow for optional BOM
    # (UTF-8, UTF-16 little endian, or UTF-16 big endian) prefix.
    _HTML_MARKER_RE = re.compile(
        rb'\s*(?:\xef\xbb\xbf|\xff\xfe|\xfe\xff)?\s*(?:<html|<!doctype)',
        re.IGNORECASE
    )

    @staticmethod
    def __looks_like_html(response: requests.Response) -> bool:
        """Guesses entity type when Content-Type header is missing.
        Since Content-Type is not strictly required, some servers leave it out.
        """
        # Match on the raw leading bytes: decoding response.text means
        # decoding (and charset-sniffing) the whole body, which is very
        # slow for large binary responses such as images or PDFs.
        prefix = response.content[:200]
        return bool(
            Browser._HTML_MARKER_RE.match(prefix)
            # UTF-16/UTF-32 encoded markup, whose ASCII characters are
            # interleaved with NUL bytes.
            or Browser._HTML_MARKER_RE.match(prefix.replace(b'\x00', b''))
        )

    @staticmethod
    def add_soup(response: requests.Response, soup_config) -> ResponseWithSoup:
        """Attaches a soup object to a requests response."""
        response.__class__ = ResponseWithSoup
        response = cast(ResponseWithSoup, response)
        if ("text/html" in response.headers.get("Content-Type", "") or
                Browser.__looks_like_html(response)):
            # Note: By default (no charset provided in HTTP headers), requests
            # returns 'ISO-8859-1' which is the default for HTML4, even if HTML
            # code specifies a different encoding. In this case, we want to
            # resort to bs4 sniffing, hence the special handling here.
            http_encoding = (
                response.encoding
                if 'charset' in response.headers.get("Content-Type", "")
                else None
            )
            html_encoding = bs4.dammit.EncodingDetector.find_declared_encoding(
                response.content,
                is_html=True
            )
            # See https://www.w3.org/International/questions/qa-html-encoding-declarations.en#httphead  # noqa: E501
            # > The HTTP header has a higher precedence than the in-document
            # > meta declarations.
            encoding = http_encoding if http_encoding else html_encoding
            response.soup = bs4.BeautifulSoup(
                response.content,
                from_encoding=encoding,
                **soup_config
            )
        else:
            response.soup = None
        return response

    def set_cookiejar(self, cookiejar):
        """Replaces the current cookiejar in the requests session. Since the
        session handles cookies automatically without calling this function,
        only use this when default cookie handling is insufficient.

        :param cookiejar: Any `http.cookiejar.CookieJar
          <https://docs.python.org/3/library/http.cookiejar.html#http.cookiejar.CookieJar>`__
          compatible object.
        """
        self.session.cookies = cookiejar

    def get_cookiejar(self):
        """Gets the cookiejar from the requests session."""
        return self.session.cookies

    def set_user_agent(self, user_agent):
        """Replaces the current user agent in the requests session headers."""
        # set a default user_agent if not specified
        if user_agent is None:
            requests_ua = requests.utils.default_user_agent()
            user_agent = f'{requests_ua} ({__title__}/{__version__})'

        # the requests module uses a case-insensitive dict for session headers
        self.session.headers['User-agent'] = user_agent

    def request(self, *args, **kwargs) -> ResponseWithSoup:
        """Straightforward wrapper around `requests.Session.request
        <http://docs.python-requests.org/en/master/api/#requests.Session.request>`__.

        :return: `requests.Response
            <http://docs.python-requests.org/en/master/api/#requests.Response>`__
            object with a *soup*-attribute added by :func:`add_soup`.

        This is a low-level function that should not be called for
        basic usage (use :func:`get` or :func:`post` instead). Use it if you
        need an HTTP verb that MechanicalSoup doesn't manage (e.g. MKCOL) for
        example.
        """
        response = self.session.request(*args, **kwargs)
        return Browser.add_soup(response, self.soup_config)

    def get(self, *args, **kwargs) -> ResponseWithSoup:
        """Straightforward wrapper around `requests.Session.get
        <http://docs.python-requests.org/en/master/api/#requests.Session.get>`__.

        :return: `requests.Response
            <http://docs.python-requests.org/en/master/api/#requests.Response>`__
            object with a *soup*-attribute added by :func:`add_soup`.
        """
        response = self.session.get(*args, **kwargs)
        if self.raise_on_404 and response.status_code == 404:
            raise LinkNotFoundError()
        return Browser.add_soup(response, self.soup_config)

    def post(self, *args, **kwargs) -> ResponseWithSoup:
        """Straightforward wrapper around `requests.Session.post
        <http://docs.python-requests.org/en/master/api/#requests.Session.post>`__.

        :return: `requests.Response
            <http://docs.python-requests.org/en/master/api/#requests.Response>`__
            object with a *soup*-attribute added by :func:`add_soup`.
        """
        response = self.session.post(*args, **kwargs)
        return Browser.add_soup(response, self.soup_config)

    def put(self, *args, **kwargs) -> ResponseWithSoup:
        """Straightforward wrapper around `requests.Session.put
        <http://docs.python-requests.org/en/master/api/#requests.Session.put>`__.

        :return: `requests.Response
            <http://docs.python-requests.org/en/master/api/#requests.Response>`__
            object with a *soup*-attribute added by :func:`add_soup`.
        """
        response = self.session.put(*args, **kwargs)
        return Browser.add_soup(response, self.soup_config)

    @staticmethod
    def _get_request_kwargs(method, url, **kwargs):
        """This method exists to raise a TypeError when a method or url is
        specified in the kwargs.
        """
        request_kwargs = {"method": method, "url": url}
        request_kwargs.update(kwargs)
        return request_kwargs

    @classmethod
    def get_request_kwargs(cls, form, url=None, submit=None, **kwargs):
        """Extract input data from the form."""
        if (
            isinstance(submit, bs4.element.Tag) and
            is_submit_control(submit) and
            is_disabled(submit)
        ):
            raise LinkNotFoundError(
                "The selected submit element is disabled and cannot be used "
                "to submit the form."
            )

        method = _effective_method(form, submit)
        action = _effective_action(form, submit)

        # An empty action (from the form or the selected submit) refers to
        # the current page. If there is no current page either, there is no
        # URL to submit to.
        if action is None:
            action = url
        target = urllib.parse.urljoin(url, action)
        if not target:
            raise ValueError('no URL to submit to')

        is_get = method.lower() == "get"
        if is_get:
            params = _to_pairs(kwargs.pop("params", None))
            caller_files = kwargs.pop("files", None)
        else:
            enctype = _effective_enctype(form, submit)
            data = _to_pairs(kwargs.pop("data", None))
            caller_files = kwargs.pop("files", None)

        entries = _form_entries(form, submit)

        if is_get:
            # File controls only contribute their filename in a GET request,
            # even if the form uses a multipart enctype.
            for kind, name, value in entries:
                if kind == "file":
                    value = _file_field_value(value)
                params.append((name, value))
            return cls._get_request_kwargs(
                method, target,
                params=params,
                files=caller_files if caller_files is not None else {},
                **kwargs,
            )

        if enctype == _TEXT_PLAIN_ENCTYPE:
            fields = list(_expand_pairs(data))
            for kind, name, value in entries:
                if kind == "file":
                    value = _file_field_value(value)
                fields.append((name, value))
            body = "".join(
                f"{_to_text(name)}="
                f"{_normalize_newlines(_to_text(value))}\r\n"
                for name, value in fields
            ).encode("utf-8")
            headers = dict(kwargs.get("headers") or {})
            for header in list(headers):
                if header.lower() == "content-type":
                    del headers[header]
            headers["Content-Type"] = _TEXT_PLAIN_ENCTYPE
            kwargs["headers"] = headers
            return cls._get_request_kwargs(
                method, target,
                data=body,
                files={},
                **kwargs,
            )

        if enctype == _MULTIPART_ENCTYPE:
            # Build one ordered list of parts so that normal and file fields
            # with the same name (including interleaved ones) are preserved
            # in control order. Requests encodes list entries in order.
            fields = _MultipartFields()
            for name, value in _expand_pairs(data):
                fields.append((name, (None, value)))
            if caller_files is not None:
                fields.extend(_to_pairs(caller_files))
            for kind, name, value in entries:
                if kind == "file":
                    fields.append((name, _multipart_file_value(value)))
                else:
                    fields.append(
                        (name, (None, _normalize_newlines(value)))
                    )
            return cls._get_request_kwargs(
                method, target,
                files=fields,
                **kwargs,
            )

        # application/x-www-form-urlencoded
        for kind, name, value in entries:
            if kind == "file":
                value = _file_field_value(value)
            data.append((name, value))
        return cls._get_request_kwargs(
            method, target,
            data=data,
            files=caller_files if caller_files is not None else {},
            **kwargs,
        )

    def _request(
        self,
        form,
        url=None,
        submit=None,
        **kwargs,
    ) -> requests.Response:
        """Extract input data from the form to pass to a Requests session."""
        request_kwargs = self.get_request_kwargs(form, url, submit, **kwargs)
        return self.session.request(**request_kwargs)

    def submit(
        self,
        form,
        url=None,
        btnName=None,
        **kwargs,
    ) -> ResponseWithSoup:
        """Prepares and sends a form request.

        NOTE: To submit a form with a :class:`StatefulBrowser` instance, it is
        recommended to use :func:`StatefulBrowser.submit_selected` instead of
        this method so that the browser state is correctly updated.

        :param form: The filled-out form.
        :param url: URL of the page the form is on. If the form action is a
            relative path, then this must be specified.
        :param btnName: Passed to :func:`Form.choose_submit` to choose the
            element of the form to use for submission. If not ``None``, will
            override any existing chosen submit in the form.
        :param \\*\\*kwargs: Arguments forwarded to `requests.Session.request
            <http://docs.python-requests.org/en/master/api/#requests.Session.request>`__.
            If `files`, `params` (with GET), or `data` (with POST) are
            specified, they will be appended to by the contents of `form`.

        :return: `requests.Response
            <http://docs.python-requests.org/en/master/api/#requests.Response>`__
            object with a *soup*-attribute added by :func:`add_soup`.
        """
        if not isinstance(form, Form):
            form = Form(form)
        submit = form.submit_chosen
        if (submit is None) or (btnName is not None):
            submit = form.choose_submit(btnName)

        response = self._request(form.form, url, submit, **kwargs)
        return Browser.add_soup(response, self.soup_config)

    def launch_browser(self, soup):
        """Launch a browser to display a page, for debugging purposes.

        :param: soup: Page contents to display, supplied as a bs4 soup object.
        """
        with tempfile.NamedTemporaryFile(delete=False, suffix='.html') as file:
            file.write(soup.encode())
        webbrowser.open('file://' + file.name)

    def close(self):
        """Close the current session, if still open."""
        if self.session is not None:
            self.session.cookies.clear()
            self.session.close()
            self.session = None

    def __del__(self):
        self._finalize()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
