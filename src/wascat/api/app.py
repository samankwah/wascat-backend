"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute

# Importing the registry configures every mapper; relationships reference each
# other by name, so a partial import fails at first query rather than at start.
import wascat.models  # noqa: F401
from wascat.api.middleware import CorsPolicyMiddleware, RequestContextMiddleware
from wascat.api.routes import admin_public_router, admin_router, public_router
from wascat.core.config import get_settings
from wascat.core.db import dispose_engine
from wascat.core.envelope import CanonicalJSONResponse
from wascat.core.errors import register_exception_handlers

TAGS_METADATA = [
    {
        "name": "catalog",
        "description": (
            "Public, unauthenticated access to the archive: image records, "
            "collections, releases and filter facets."
        ),
    },
    {
        "name": "admin:auth",
        "description": "Signing in and out of the dashboard.",
    },
    {"name": "admin", "description": "Authenticated dashboard operations."},
]


def unique_operation_id(route: APIRoute) -> str:
    """Readable, stable operation ids for the frontend's type generation."""
    tag = str(route.tags[0]).replace(":", "_") if route.tags else "default"
    return f"{tag}_{route.name}"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="WASCAT API",
        version="1.0.0",
        description=(
            "The West African Sky Archive: all-sky camera frames paired with "
            "binary cloud-segmentation masks, and the cloud cover measured "
            "from them."
        ),
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
        openapi_tags=TAGS_METADATA,
        generate_unique_id_function=unique_operation_id,
        default_response_class=CanonicalJSONResponse,
        # One schema per model instead of the Foo-Input/Foo-Output pairs
        # FastAPI emits by default, which makes the generated TypeScript far
        # easier to consume.
        separate_input_output_schemas=False,
        servers=[{"url": settings.public_base_url or "/"}],
        lifespan=lifespan,
        debug=settings.debug,
    )

    # Outermost first: the request id should be attached before anything else
    # can log, and CORS headers should be applied to whatever comes back.
    app.add_middleware(CorsPolicyMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(public_router)
    app.include_router(admin_public_router)
    app.include_router(admin_router)

    @app.get("/api/v1/health", tags=["catalog"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
