"""Which caches may keep which responses.

Public responses are cacheable downstream but never by Vercel's own CDN: the
frontend revalidates by refetching, and a platform copy would answer that
refetch with the data the revalidation was meant to replace.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Response

from wascat.api.middleware import (
    PLATFORM_CDN_CACHE_CONTROL,
    PUBLIC_CACHE_CONTROL,
    CorsPolicyMiddleware,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorsPolicyMiddleware)

    @app.get("/api/v1/images")
    async def public() -> dict[str, str]:
        return {}

    @app.get("/api/v1/frames/immutable")
    async def immutable(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return {}

    @app.get("/api/v1/admin/images")
    async def admin() -> dict[str, str]:
        return {}

    return app


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get(path)


async def test_public_responses_skip_the_platform_cdn() -> None:
    response = await _get("/api/v1/images")
    assert response.headers["Cache-Control"] == PUBLIC_CACHE_CONTROL
    assert response.headers["Vercel-CDN-Cache-Control"] == PLATFORM_CDN_CACHE_CONTROL


async def test_an_explicit_cache_policy_is_left_to_every_cache() -> None:
    response = await _get("/api/v1/frames/immutable")
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "Vercel-CDN-Cache-Control" not in response.headers


async def test_admin_responses_are_never_stored() -> None:
    response = await _get("/api/v1/admin/images")
    assert response.headers["Cache-Control"] == "no-store"
    assert "Vercel-CDN-Cache-Control" not in response.headers
