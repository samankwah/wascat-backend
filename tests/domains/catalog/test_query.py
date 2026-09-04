"""The query parser reproduces Zod's accepted values *and* its rejections.

The error strings are contract: they were recorded from the TypeScript
implementation and a client may show them to a user. Rather than assert them
by hand, the first test replays every rejection the recorder captured.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from wascat.core.errors import InvalidQueryError
from wascat.domains.catalog.query import DEFAULT_LIMIT, ImageQuery, parse_image_query


def _params(path: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(path).query, keep_blank_values=True))


class TestRecordedRejections:
    """Every 400 the pre-migration API returned, replayed."""

    def test_replays_every_recorded_rejection(self, contract_cases: list[dict[str, Any]]) -> None:
        rejections = [
            case
            for case in contract_cases
            if case["status"] == 400 and case["request"]["path"].startswith("/api/v1/images?")
        ]
        assert rejections, "no recorded rejections found; re-run the contract recorder"

        for case in rejections:
            expected = case["body"]["error"]
            with pytest.raises(InvalidQueryError) as caught:
                parse_image_query(_params(case["request"]["path"]))

            error = caught.value
            assert error.code == expected["code"], case["name"]
            assert error.message == expected["message"], case["name"]
            assert error.details == expected["details"], (
                f"{case['name']}: {error.details!r} != {expected['details']!r}"
            )

    def test_reports_every_bad_parameter_at_once(self) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"oktas": "9", "limit": "101", "sequence": "nope"})
        # Key order follows the schema's declaration order, as Zod's
        # flatten().fieldErrors does.
        assert list(caught.value.details or {}) == ["sequence", "oktas", "limit"]


class TestDefaults:
    def test_empty_query_uses_the_documented_defaults(self) -> None:
        query = parse_image_query({})
        assert query == ImageQuery()
        assert query.limit == DEFAULT_LIMIT
        assert query.sort == "newest"

    def test_unknown_parameters_are_ignored_not_rejected(self) -> None:
        # A Zod object strips what it does not declare.
        query = parse_image_query({"nonsense": "1", "utm_source": "newsletter"})
        assert query == ImageQuery()


class TestCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0", 0), ("8", 8), ("4", 4), (" 5 ", 5)],
    )
    def test_accepts_valid_oktas(self, raw: str, expected: int) -> None:
        assert parse_image_query({"oktas": raw}).oktas == expected

    def test_empty_numeric_string_coerces_to_zero(self) -> None:
        # Number("") is 0 in JavaScript, so `?oktas=` asks for a clear sky
        # rather than being a validation error. Surprising, but contract.
        assert parse_image_query({"oktas": ""}).oktas == 0

    @pytest.mark.parametrize("raw", ["abc", "1.5.2", "one"])
    def test_rejects_unparseable_numbers(self, raw: str) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"oktas": raw})
        assert caught.value.details == {"oktas": ["Expected number, received nan"]}

    def test_rejects_a_fractional_okta(self) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"oktas": "3.5"})
        assert caught.value.details == {"oktas": ["Expected integer, received float"]}

    def test_segmented_becomes_a_boolean(self) -> None:
        assert parse_image_query({"segmented": "true"}).segmented is True
        assert parse_image_query({"segmented": "false"}).segmented is False
        assert parse_image_query({}).segmented is None


class TestStringFields:
    def test_trims_whitespace(self) -> None:
        assert parse_image_query({"q": "  vid1  "}).q == "vid1"

    def test_blank_string_is_treated_as_absent(self) -> None:
        # An empty q must not filter everything out.
        assert parse_image_query({"q": ""}).q is None
        assert parse_image_query({"q": "   "}).q is None

    def test_enforces_length_bounds(self) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"q": "x" * 101})
        assert caught.value.details == {"q": ["String must contain at most 100 character(s)"]}

    @pytest.mark.parametrize("sequence", ["seq-001", "seq-011", "seq-20260904-001"])
    def test_accepts_sequence_identifiers(self, sequence: str) -> None:
        assert parse_image_query({"sequence": sequence}).sequence == sequence

    @pytest.mark.parametrize(
        "sequence", ["vid1", "seq-1", "SEQ-001", "seq-0011", "seq-001x", "seq"]
    )
    def test_rejects_malformed_sequence_identifiers(self, sequence: str) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"sequence": sequence})
        assert caught.value.details == {"sequence": ["Invalid"]}


class TestDates:
    def test_accepts_a_calendar_date(self) -> None:
        assert parse_image_query({"from": "2026-03-14"}).date_from == "2026-03-14"

    @pytest.mark.parametrize(
        "value",
        ["notadate", "2026-13-01", "2026-02-30", "2026-3-4", "2026-03-14T09:00:00Z", "20260314"],
    )
    def test_rejects_anything_else(self, value: str) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"from": value})
        assert caught.value.details == {"from": ["Invalid date"]}


class TestCursor:
    def test_a_malformed_cursor_is_accepted_here_and_handled_downstream(self) -> None:
        # Validation must not reject it: lib/search.ts decoded cursors with a
        # try/catch that fell back to the start, so a stale bookmark returns
        # page one rather than a 400.
        assert parse_image_query({"cursor": "not-a-real-cursor"}).cursor == "not-a-real-cursor"

    def test_rejects_an_absurdly_long_cursor(self) -> None:
        with pytest.raises(InvalidQueryError) as caught:
            parse_image_query({"cursor": "x" * 201})
        assert caught.value.details == {"cursor": ["String must contain at most 200 character(s)"]}
