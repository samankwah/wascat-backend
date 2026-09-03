"""ASGI entrypoint: ``uvicorn wascat.main:app``."""

from wascat.api.app import create_app

app = create_app()
