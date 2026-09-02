"""Test L4 - okta bucketing reproduces the shipped catalogue exactly.

This is the cheapest of the golden tests and the one that catches the most
dangerous class of porting bug: Python and NumPy round half to even, while
JavaScript rounds half up. Every cloud fraction sitting on an eighth boundary
would land in a different bucket under the wrong rule, and the difference is
invisible in a spot check.

It needs no imagery - it reads the fractions and the buckets the TypeScript
implementation already committed to and checks that we agree on all of them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from wascat.core.jsformat import js_round, okta_label, oktas_from_fraction


@pytest.mark.golden
def test_reproduces_every_measured_okta(measured_images: list[dict[str, Any]]) -> None:
    assert len(measured_images) == 1360, "fixture drift: expected 1,360 measured records"

    mismatches = [
        (image["id"], image["cloudFraction"], image["cloudCoverOktas"], recomputed)
        for image in measured_images
        if (recomputed := oktas_from_fraction(image["cloudFraction"])) != image["cloudCoverOktas"]
    ]
    assert mismatches == [], f"{len(mismatches)} records bucket differently, e.g. {mismatches[:5]}"


@pytest.mark.golden
def test_every_measured_record_is_in_range(measured_images: list[dict[str, Any]]) -> None:
    for image in measured_images:
        assert 0 <= image["cloudCoverOktas"] <= 8
        assert 0.0 <= image["cloudFraction"] <= 1.0


class TestJsRound:
    """``Math.round`` rounds half toward +infinity; Python rounds half to even."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.5, 1),  # Python's round() gives 0
            (1.5, 2),
            (2.5, 3),  # Python's round() gives 2
            (3.5, 4),
            (-0.5, 0),  # Math.round(-0.5) is -0, i.e. 0
            (0.4999999, 0),
            (0.5000001, 1),
            (0.0, 0),
            (8.0, 8),
        ],
    )
    def test_matches_javascript(self, value: float, expected: int) -> None:
        assert js_round(value) == expected

    def test_differs_from_python_round_where_it_matters(self) -> None:
        # Guards the reason this helper exists. If a future refactor swaps in
        # the builtin, these stop agreeing and the test fails loudly.
        assert round(0.5) == 0
        assert js_round(0.5) == 1
        assert round(2.5) == 2
        assert js_round(2.5) == 3


class TestOktaBoundaries:
    """Each eighth boundary lands on the upper bucket, as Math.round does."""

    @pytest.mark.parametrize(
        ("fraction", "expected"),
        [
            (0.0, 0),
            (0.0625, 1),  # exactly half an eighth
            (0.06249, 0),
            (0.1875, 2),
            (0.3125, 3),
            (0.4375, 4),
            (0.5625, 5),
            (0.6875, 6),
            (0.8125, 7),
            (0.9375, 8),
            (1.0, 8),
        ],
    )
    def test_boundary(self, fraction: float, expected: int) -> None:
        assert oktas_from_fraction(fraction) == expected

    def test_clamps_outside_the_unit_interval(self) -> None:
        assert oktas_from_fraction(-0.5) == 0
        assert oktas_from_fraction(2.0) == 8

    def test_accepts_decimal_as_well_as_float(self) -> None:
        assert oktas_from_fraction(Decimal("0.525588")) == 4
        assert oktas_from_fraction(0.525588) == 4


class TestOktaLabel:
    @pytest.mark.parametrize(
        ("okta", "expected"),
        [
            (0, "0/8 · Clear"),
            (1, "1/8"),
            (4, "4/8"),
            (7, "7/8"),
            (8, "8/8 · Overcast"),
        ],
    )
    def test_label(self, okta: int, expected: str) -> None:
        assert okta_label(okta) == expected

    def test_separator_is_a_middle_dot(self) -> None:
        # U+00B7, not a hyphen and not a full stop. It reaches the browser via
        # the record's `tags` array, so it is contract.
        assert "·" in okta_label(0)
        assert "·" in okta_label(8)
