"""Shared fixtures.

The golden catalogue under ``seed/`` is the record of what the pre-migration
TypeScript implementation measured and served. It is the reference for both
the ingest pipeline port and the public API, so it is loaded once per session
and shared read-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# Registers every mapped class. Relationships reference each other by name
# ("VocabularyTerm"), so a test module that imports only catalog models
# would fail to configure its mappers.
import wascat.models  # noqa: F401

# Database fixtures live in tests/db.py; re-exported so any test can ask
# for `session` without importing anything.
from tests.db import engine, session  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = REPO_ROOT / "seed"
CONTRACT_DIR = REPO_ROOT / "tests" / "fixtures" / "contract"


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    """The generated catalogue the TypeScript pipeline produced."""
    with (SEED_DIR / "catalog.generated.json").open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


@pytest.fixture(scope="session")
def golden_images(golden: dict[str, Any]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = golden["images"]
    return images


@pytest.fixture(scope="session")
def measured_images(golden_images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records that carry a mask, and therefore a measured cloud cover."""
    return [image for image in golden_images if "cloudFraction" in image]


@pytest.fixture(scope="session")
def contract_cases() -> list[dict[str, Any]]:
    """Responses recorded from the pre-migration Next.js route handlers."""
    index_path = CONTRACT_DIR / "index.json"
    if not index_path.exists():
        pytest.skip("contract fixtures not recorded")
    with index_path.open(encoding="utf-8") as handle:
        index = json.load(handle)
    cases = []
    for entry in index["cases"]:
        with (CONTRACT_DIR / entry["file"]).open(encoding="utf-8") as handle:
            cases.append(json.load(handle))
    return cases
