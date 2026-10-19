"""Vercel's entrypoint, at the repository root where Vercel looks for it.

The package lives under ``src/`` and Vercel resolves ``[tool.vercel]
entrypoint`` from the root, so this puts ``src/`` on the path and re-exports
the same app ``uvicorn wascat.main:app`` serves everywhere else.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wascat.main import app

__all__ = ["app"]
