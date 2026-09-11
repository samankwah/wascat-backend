"""Replay every response recorded from the pre-migration API.

This is the test that decides whether the port preserved the contract. The
fixtures in tests/fixtures/contract were captured from the Next.js route
handlers before anything moved, by calling them directly and normalising only
``meta.generatedAt``. Everything else - key sets, ordering, the strings the
site prints, the shape of a rejection, which fields are absent rather than
null - is compared exactly.

Counting records would not do the same job: the full-archive ingest will change
every total the old tests asserted. These fixtures pin behaviour instead of
size, so they stay meaningful as the archive grows.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.contract]

REPO_ROOT = Path(__file__).resolve().parents[2]

# The recorder ran against this origin, and links.self echoes it. Asset URLs
# were relative, because the bundled catalogue served frames out of public/.
RECORDED_ORIGIN = "https://wascat.example.org"


@pytest.fixture(scope="session", autouse=True)
def _contract_settings() -> Iterator[None]:
    """Point the app at the origin the fixtures were recorded against."""
    from wascat.core.config import get_settings

    previous = {
        key: os.environ.get(key)
        for key in ("WASCAT_PUBLIC_BASE_URL", "WASCAT_PUBLIC_ASSET_BASE_URL")
    }
    os.environ["WASCAT_PUBLIC_BASE_URL"] = RECORDED_ORIGIN
    os.environ["WASCAT_PUBLIC_ASSET_BASE_URL"] = ""
    get_settings.cache_clear()
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture(scope="session")
async def seeded_engine(engine):  # type: ignore[no-untyped-def]
    """Load the published catalogue into the test database, once.

    Object storage is skipped: the contract covers the JSON the API returns,
    and the URLs in it are derived from the object keys rather than fetched.
    """
    from wascat.domains.ingest.legacy_import import import_catalogue

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await import_catalogue(
            session,
            catalog_path=REPO_ROOT / "seed" / "catalog.generated.json",
            provenance_path=REPO_ROOT / "seed" / "provenance.json",
            frames_root=None,
            store=None,
            publish=True,
            replace=True,
        )
        await session.commit()
    return engine


@pytest.fixture(scope="session")
async def client(seeded_engine) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    from wascat.core.db import get_session
    from wascat.main import app

    maker = async_sessionmaker(seeded_engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=RECORDED_ORIGIN) as http:
        yield http
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# The deliberate differences
# ---------------------------------------------------------------------------
#
# 0. Okta labels name the whole scale. The recorded implementation named only
#    its two ends - "0/8 · Clear" and "8/8 · Overcast" - and printed 1-7 as
#    bare fractions, so most of the archive reported a ratio where it meant an
#    observation. They now carry the synoptic terms: Few (1-2), Scattered
#    (3-4), Broken (5-7). The two ends are byte-identical to what was recorded.
#
#    This one is unlike the others below: the fixtures were *rewritten* rather
#    than exempted, because the strings are still contract and still asserted
#    exactly - they simply assert a corrected value. The rewrite touched 41
#    strings across 10 files and changed nothing but the label, so a future
#    drift in `alt`, `tags` or a facet label still fails here.
#
# 1. Cursors are opaque. The old implementation encoded {"offset": n} and
#    sliced an in-memory array; this one encodes a keyset over (sort_key, id)
#    so paging stays cheap as the archive grows past 17,000 frames and does
#    not skip or repeat rows when a record is inserted mid-scroll. No client
#    reads a cursor - they are round-tripped verbatim - so the *value* is not
#    contract. Whether one is issued at all is, and so is where following it
#    lands; both are asserted below.
#
# 2. `publishedAt` on a release. The old API synthesised a release object with
#    no publication date because it had nowhere to keep one. Releases are real
#    rows now and do carry the date, so the field appears. It is additive, the
#    frontend's Release type already declares it optional, and suppressing a
#    true fact to match a fixture would be the wrong way round.
#
# Anything not listed here is compared exactly.

OPAQUE_FIELDS = {"nextCursor", "next"}
# 3. Two aggregates on a collection: `meanCloudCoverOktas` and
#    `maskRegistration`. The collection page derived both by walking the whole
#    bundled catalogue, which it no longer holds; the database produces them in
#    the query that already counts the frames. Additive, and the alternative
#    was shipping every record to the browser to average it there.
# 4. `skyClasses` on the facets response, and `skyClass` on a record.
#    Sky classification did not exist in the recorded implementation at all -
#    there was nothing to record. Both are new and additive, the latter
#    absent rather than present until a record actually carries a sky_class.
ADDITIVE_FIELDS = {
    "publishedAt",
    "meanCloudCoverOktas",
    "maskRegistration",
    "skyClasses",
    "skyClass",
}


def normalise(value: Any) -> Any:
    """Blank the one field that legitimately differs between two calls."""
    if isinstance(value, list):
        return [normalise(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<GENERATED_AT>" if key == "generatedAt" else normalise(item)
            for key, item in value.items()
        }
    return value


def _describe(expected: Any, actual: Any, path: str = "") -> str:
    """First difference, located, so a failure names the field."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key not in actual:
                return f"{path}.{key}: missing (expected {expected[key]!r})"
            if key in OPAQUE_FIELDS:
                # Compare presence, not value.
                if (expected[key] is None) != (actual[key] is None):
                    return (
                        f"{path}.{key}: "
                        f"{'issued' if actual[key] else 'absent'}, expected "
                        f"{'one' if expected[key] else 'none'}"
                    )
                continue
            found = _describe(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
        for key in actual:
            if key not in expected and key not in ADDITIVE_FIELDS:
                return f"{path}.{key}: unexpected (got {actual[key]!r})"
        return ""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(actual)}, expected {len(expected)}"
        for index, (want, got) in enumerate(zip(expected, actual, strict=True)):
            found = _describe(want, got, f"{path}[{index}]")
            if found:
                return found
        return ""
    if expected != actual:
        return f"{path}: {actual!r}, expected {expected!r}"
    return ""


class TestRecordedResponses:
    async def test_every_case_matches(
        self, client: httpx.AsyncClient, contract_cases: list[dict[str, Any]]
    ) -> None:
        assert contract_cases, "no contract fixtures found; run npm run record:contract"

        mismatches: list[str] = []
        for case in contract_cases:
            response = await client.get(case["request"]["path"])

            if response.status_code != case["status"]:
                mismatches.append(
                    f"{case['name']}: status {response.status_code}, expected {case['status']}"
                )
                continue

            difference = _describe(normalise(case["body"]), normalise(response.json()))
            if difference:
                mismatches.append(f"{case['name']}: {difference.lstrip('.')}")

        assert not mismatches, "\n".join(f"  - {line}" for line in mismatches)

    async def test_rate_limit_headers_on_success_and_not_on_errors(
        self, client: httpx.AsyncClient, contract_cases: list[dict[str, Any]]
    ) -> None:
        # The TypeScript success path set them and its error paths did not.
        # Both halves are recorded, so both are asserted.
        for case in contract_cases:
            response = await client.get(case["request"]["path"])
            expected = case["headers"].get("x-ratelimit-limit")
            actual = response.headers.get("x-ratelimit-limit")
            assert actual == expected, f"{case['name']}: X-RateLimit-Limit {actual!r}"

    async def test_following_the_cursor_chain_returns_the_recorded_records(
        self, client: httpx.AsyncClient, contract_cases: list[dict[str, Any]]
    ) -> None:
        """The cursor encoding changed; where it leads must not have.

        The recorder walked three pages of ``/images?limit=2`` and saved each.
        This walks the same chain through the new keyset cursors and checks it
        visits the same records in the same order.
        """
        pages = sorted(
            (case for case in contract_cases if case["name"].startswith("images-cursor-page-")),
            key=lambda case: case["name"],
        )
        assert pages, "cursor chain fixtures missing"

        path = "/api/v1/images?limit=2"
        for expected in pages:
            response = await client.get(path)
            assert response.status_code == 200
            body = response.json()

            assert [item["id"] for item in body["data"]] == [
                item["id"] for item in expected["body"]["data"]
            ], f"{expected['name']}: different records"
            assert body["meta"]["total"] == expected["body"]["meta"]["total"]

            cursor = body["meta"]["nextCursor"]
            assert cursor is not None, f"{expected['name']}: chain ended early"
            path = f"/api/v1/images?limit=2&cursor={cursor}"

    async def test_a_cursor_never_repeats_or_skips_a_record(
        self, client: httpx.AsyncClient
    ) -> None:
        """Page right through a filter and check the result is a partition."""
        seen: list[str] = []
        path = "/api/v1/images?sequence=seq-009&limit=7"
        total: int | None = None
        for _ in range(50):
            body = (await client.get(path)).json()
            total = body["meta"]["total"]
            seen.extend(item["id"] for item in body["data"])
            cursor = body["meta"]["nextCursor"]
            if cursor is None:
                break
            path = f"/api/v1/images?sequence=seq-009&limit=7&cursor={cursor}"

        assert total is not None
        assert len(seen) == total, "paging did not cover the result exactly once"
        assert len(set(seen)) == len(seen), "a record appeared on two pages"

    async def test_links_self_echoes_the_public_url(self, client: httpx.AsyncClient) -> None:
        # Behind the frontend's proxy the service sees an internal host; the
        # link the client is handed must be the one it asked for.
        response = await client.get("/api/v1/images?limit=1")
        assert response.json()["links"]["self"] == f"{RECORDED_ORIGIN}/api/v1/images?limit=1"
