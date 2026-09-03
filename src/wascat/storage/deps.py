"""The store, as a FastAPI dependency.

Separate from ``factory`` so a domain router can take the annotation without
importing the API layer, which would invert the architecture.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from wascat.storage.base import ObjectStore
from wascat.storage.factory import get_store

StoreDep = Annotated[ObjectStore, Depends(get_store)]
