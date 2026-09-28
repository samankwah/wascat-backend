"""Parsing the per-sequence provenance file."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from wascat.domains.ingest.provenance import ProvenanceError, parse_provenance


def write(tmp_path: Path, sequences: dict) -> Path:
    path = tmp_path / "provenance.json"
    path.write_text(json.dumps({"sequences": sequences}), encoding="utf-8")
    return path


def test_capture_time_is_derived_from_start_and_interval(tmp_path: Path) -> None:
    [entry] = parse_provenance(
        write(
            tmp_path,
            {
                "seq-001": {
                    "station": "Wa Synoptic Station",
                    "site": "Wa, Ghana",
                    "latitude": 10.083,
                    "longitude": -2.508,
                    "startedAt": "2026-08-30T14:30:00Z",
                    "frameIntervalSeconds": 2,
                }
            },
        )
    )
    assert entry.latitude == Decimal("10.083")
    assert entry.captured_at(0) == datetime(2026, 8, 30, 14, 30, tzinfo=UTC)
    assert entry.captured_at(900) == datetime(2026, 8, 30, 15, 0, tzinfo=UTC)


def test_start_without_interval_gives_no_capture_time(tmp_path: Path) -> None:
    [entry] = parse_provenance(
        write(tmp_path, {"seq-001": {"site": "Wa, Ghana", "startedAt": "2026-08-30T14:30:00Z"}})
    )
    assert entry.captured_at(10) is None


def test_empty_sequences_are_skipped(tmp_path: Path) -> None:
    assert parse_provenance(write(tmp_path, {"seq-001": {"site": None, "latitude": None}})) == []


@pytest.mark.parametrize(
    "meta",
    [
        {"latitude": 10.0},
        {"site": "Wa", "startedAt": "2026-08-30T14:30:00"},
        {"site": "Wa", "frameIntervalSeconds": 0},
    ],
)
def test_malformed_sequences_are_refused(tmp_path: Path, meta: dict) -> None:
    with pytest.raises(ProvenanceError):
        parse_provenance(write(tmp_path, {"seq-001": meta}))
