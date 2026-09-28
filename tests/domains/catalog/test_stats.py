"""The shape rules of the statistics payload, which need no database."""

from __future__ import annotations

from wascat.domains.catalog.stats import COVERAGE_BINS, coverage_bins, cumulative, hour_buckets


def test_every_hour_is_present_even_at_zero() -> None:
    buckets = hour_buckets({7: 3, 23: 1})
    assert [bucket["hour"] for bucket in buckets] == list(range(24))
    assert buckets[7]["count"] == 3
    assert buckets[23]["count"] == 1
    assert sum(bucket["count"] for bucket in buckets) == 4


def test_every_coverage_bin_is_present_in_order() -> None:
    bins = coverage_bins({4: 2})
    assert [item["label"] for item in bins] == list(COVERAGE_BINS)
    assert [item["count"] for item in bins] == [0, 0, 0, 0, 2]


def test_growth_accumulates_oldest_first() -> None:
    series = cumulative([("2026-03", 5), ("2026-01", 2), ("2026-02", 0)])
    assert [point["month"] for point in series] == ["2026-01", "2026-02", "2026-03"]
    assert [point["cumulative"] for point in series] == [2, 2, 7]


def test_empty_growth_is_an_empty_series() -> None:
    assert cumulative([]) == []
