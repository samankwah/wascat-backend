"""Cross-cutting HTTP concerns.

The CORS policy is hand-rolled rather than delegated to ``CORSMiddleware``
because the service has two audiences with opposite needs on the same origin:

* ``/api/v1/...`` is a public read API. It is meant to be called from anywhere,
  so it answers ``Access-Control-Allow-Origin: *`` - exactly what the Next.js
  config did before the split.
* ``/api/v1/admin/...`` carries session cookies. It must echo a single
  allow-listed origin and set ``Allow-Credentials``.

Those two are mutually exclusive: a browser refuses credentialed requests
against a wildcard origin, and sending both would either break the dashboard
or expose it. One middleware that branches on the path prefix makes the
distinction explicit and impossible to get half-right.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from wascat.core.config import get_settings

ADMIN_PREFIX = "/api/v1/admin"

# Applied everywhere, mirroring what next.config.ts set for the site. The API
# is directly reachable now, so it has to set them itself.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}

PUBLIC_CACHE_CONTROL = "public, s-maxage=300, stale-while-revalidate=3600"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id so logs and audit rows can be correlated."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class CorsPolicyMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = get_settings()
        is_admin = request.url.path.startswith(ADMIN_PREFIX)
        origin = request.headers.get("origin")

        if request.method == "OPTIONS" and origin is not None:
            return self._preflight(is_admin, origin, settings.admin_origins)

        response = await call_next(request)

        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)

        if is_admin:
            if origin is not None and origin in settings.admin_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
            # The response depends on who is asking and what they carry, so a
            # shared cache must not reuse it.
            response.headers["Vary"] = "Origin, Cookie"
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers.setdefault("Cache-Control", PUBLIC_CACHE_CONTROL)

        return response

    @staticmethod
    def _preflight(is_admin: bool, origin: str, admin_origins: list[str]) -> Response:
        headers = {
            "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-CSRF-Token, X-Request-Id",
            "Access-Control-Max-Age": "600",
        }
        if is_admin:
            if origin not in admin_origins:
                return Response(status_code=403)
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Vary"] = "Origin"
        else:
            headers["Access-Control-Allow-Origin"] = "*"
        return Response(status_code=204, headers=headers)
