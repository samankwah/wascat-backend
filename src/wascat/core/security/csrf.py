"""Cross-site request forgery protection.

The dashboard reaches the API through a same-origin rewrite, which is what
makes its session cookies work without CORS - and also what makes CSRF real:
the browser attaches those cookies to any request another site provokes.

Double-submit is the defence. A random value is set in a cookie readable by
JavaScript and echoed back in a header on every mutation. A cross-site page can
cause the cookie to be *sent*, but the same-origin policy stops it from
*reading* the cookie, so it cannot produce the matching header.

`SameSite` already blocks the common cases; this is the layer that does not
depend on getting cookie attributes right in every browser.

Requests authenticated by a bearer token skip the check: they carry no ambient
credential, so there is nothing for another site to ride on.
"""

from __future__ import annotations

import secrets

CSRF_COOKIE_NAME = "wascat_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

CSRF_TOKEN_BYTES = 32

#: Methods that cannot change state, and so need no token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def tokens_match(cookie_value: str | None, header_value: str | None) -> bool:
    """Compare in constant time, and never treat a missing value as a match."""
    if not cookie_value or not header_value:
        return False
    return secrets.compare_digest(cookie_value, header_value)
