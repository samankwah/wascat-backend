"""The public response envelope.

Reproduces lib/api.ts and the hand-rolled variant in
app/api/v1/images/route.ts. Both are pinned by recorded fixtures under
tests/fixtures/contract, so treat every detail here as contract:

  * ``meta.apiVersion`` is the string "1.0", not the app version.
  * ``meta.generatedAt`` is millisecond-precision ISO with a literal Z.
  * ``links.self`` is the URL the *client* asked for, not the one this
    process received behind the frontend's proxy.
  * The rate-limit headers are present on success responses and absent on
    error responses, because the TypeScript error paths never set them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import orjson
from fastapi import Request
from starlette.responses import JSONResponse

from wascat.core.config import get_settings
from wascat.core.jsformat import iso_z

API_VERSION = "1.0"

RATE_LIMIT_HEADERS = {
    "X-RateLimit-Limit": "120",
    "X-RateLimit-Remaining": "119",
}


class CanonicalJSONResponse(JSONResponse):
    """JSON rendered by orjson, with the contract's exact byte output.

    Deliberately a Starlette JSONResponse rather than FastAPI's ORJSONResponse
    (deprecated) or Pydantic-driven serialisation: the envelope's key order,
    its omission of unmeasured fields, and Decimal rendering are all contract,
    so the response body is built as a plain dict and serialised verbatim
    rather than round-tripped through a response model.

    Every datetime reaching this point has already been rendered by
    :func:`wascat.core.jsformat.iso_z`, so orjson never sees one.
    """

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, default=_fallback)


def _fallback(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Shortest round-tripping form, matching how JSON.stringify renders a
        # JS number. See jsformat.js_number and risk R5.
        return float(value)
    raise TypeError(f"Cannot serialise {type(value).__name__}")


def public_url(request: Request) -> str:
    """The absolute URL the browser requested.

    Behind the frontend rewrite this process sees something like
    ``http://api:8000/api/v1/images?limit=5``. Echoing that into ``links.self``
    would leak an internal hostname and break every recorded fixture, so the
    public origin is taken from configuration when one is set.
    """
    settings = get_settings()
    if not settings.public_base_url:
        return str(request.url)
    query = request.url.query
    return f"{settings.public_base_url}{request.url.path}" + (f"?{query}" if query else "")


def envelope(
    request: Request,
    data: Any,
    *,
    meta: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    status_code: int = 200,
) -> CanonicalJSONResponse:
    """Wrap a payload in the public envelope."""
    return CanonicalJSONResponse(
        {
            "data": data,
            "meta": {"apiVersion": API_VERSION, "generatedAt": iso_z(), **(meta or {})},
            "links": {"self": public_url(request), **(links or {})},
        },
        status_code=status_code,
        headers=dict(RATE_LIMIT_HEADERS),
    )
