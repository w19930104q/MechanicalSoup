"""Per-browser restriction of the network destinations MechanicalSoup is
allowed to contact.

When a :class:`~mechanicalsoup.Browser` is constructed with a non-empty (or
even empty, see below) ``allowed_origins`` argument, every request it issues
goes through an :class:`_OriginRestrictedSession`: the origin of the target
URL is checked against the browser's own allow-set *before* any network I/O,
both for the initial request and for each hop of a redirect chain.

The feature is opt-in: a browser without ``allowed_origins`` keeps using a
plain :class:`requests.Session` and behaves exactly as before.
"""

from collections.abc import Iterable
from urllib.parse import urlsplit

import requests


# Ports implied by a scheme when the URL does not specify one explicitly.
_DEFAULT_PORTS = {
    'http': 80,
    'https': 443,
}


class OriginNotAllowedError(Exception):
    """Raised when a request targets an origin outside the browser's
    allow-set.

    The exception is raised before any network I/O happens: no request is
    sent to the rejected destination, including during redirect handling.

    :ivar url: The full URL that was rejected.
    :ivar origin: The normalized ``scheme://host:port`` origin extracted
        from that URL.
    """

    def __init__(self, url: str, origin: str):
        self.url = url
        self.origin = origin
        super().__init__(
            f"Origin {origin!r} (from URL {url!r}) is not in the browser's "
            f"allowed origins"
        )


def origin_of(url: str) -> str:
    """Return the normalized origin (``scheme://host:port``) of ``url``.

    Comparison is performed on the scheme, the lower-cased host and the
    effective port (the scheme's default port when none is given). The path,
    query and fragment of ``url`` are ignored.

    :raises ValueError: if ``url`` is not an absolute http(s) URL.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(
            f"Only http and https origins are allowed, got scheme "
            f"{parts.scheme!r} in {url!r}"
        )
    hostname = parts.hostname
    if not hostname:
        raise ValueError(f"Cannot extract an origin from URL {url!r}")
    port = parts.port or _DEFAULT_PORTS[scheme]
    return f"{scheme}://{hostname.lower()}:{port}"


def _parse_allowed_origins(origins: Iterable[str]) -> frozenset[str]:
    """Validate and normalize an iterable of origin/URL strings into a
    frozenset of normalized origins.

    Invalid configuration is rejected here, before any I/O can happen.
    """
    allowed = set()
    for entry in origins:
        if not isinstance(entry, str):
            raise TypeError(
                "allowed_origins entries must be strings (origin or URL), "
                f"got {entry!r}"
            )
        allowed.add(origin_of(entry))
    return frozenset(allowed)


class _OriginRestrictedSession(requests.Session):
    """A requests session that only sends requests to a fixed set of origins.

    The session shares its mutable state (cookies, headers, adapters, ...)
    with a prototype session supplied by the caller, so restrictions remain
    per-browser: two browsers built on the same user session get distinct
    restricted sessions (and therefore distinct allow-sets) while still
    sharing cookies and connection adapters, and the user's own session
    object is never mutated or restricted.
    """

    def __init__(self, prototype: requests.Session,
                 allowed_origins: frozenset[str]):
        # Bypass requests.Session.__init__ on purpose: every attribute is
        # taken from (and, for the mutable containers, shared with) the
        # prototype instead of creating a second independent cookie jar,
        # adapter pool, header dict, ...
        for attr in requests.Session.__attrs__:
            setattr(self, attr, getattr(prototype, attr))
        self._allowed_origins = frozenset(allowed_origins)

    def add_allowed_origin(self, origin: str) -> None:
        """Allow one additional origin (given as an origin or URL string)."""
        self._allowed_origins = self._allowed_origins | {origin_of(origin)}

    @property
    def allowed_origins(self) -> frozenset[str]:
        """The normalized origins this session may contact."""
        return self._allowed_origins

    def send(self, request: requests.PreparedRequest, **kwargs):
        # requests' type stubs mark PreparedRequest.url as optional, but a
        # prepared request always carries a URL.
        url = request.url or ""
        origin = origin_of(url)
        if origin not in self._allowed_origins:
            raise OriginNotAllowedError(url, origin)
        return super().send(request, **kwargs)
