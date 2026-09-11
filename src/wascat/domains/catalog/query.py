"""The public image query.

A faithful port of ``imageQuerySchema`` in lib/search.ts, including the exact
error strings Zod produces. Those strings are contract - they are recorded in
tests/fixtures/contract and a client may render them - so this module does its
own validation rather than translating Pydantic's errors, which are worded
differently and grouped differently.

Two behaviours are easy to "improve" by accident and must not be:

* ``z.coerce.number()`` runs JavaScript's ``Number()``, and ``Number("")`` is
  ``0``. So ``?oktas=`` is a request for zero oktas, not a validation error.
* Unknown parameters are ignored, because a Zod object strips what it does not
  declare rather than rejecting it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from wascat.core.errors import FieldErrors, InvalidQueryError
from wascat.domains.catalog.models import SEQUENCE_ID_PATTERN

# Mirrors lib/vocab.ts. These are the values the public API validates against;
# the vocabulary tables may hold more, but widening this set widens the
# contract and is gated behind WASCAT_ALLOW_VOCAB_EXPANSION.
ARTIFACT_TYPES: tuple[str, ...] = ("source", "mask")
SEASONS: tuple[str, ...] = ("Harmattan", "Dry season", "Wet season", "Transition")
TIMES_OF_DAY: tuple[str, ...] = ("Morning", "Midday", "Afternoon", "Evening")
SORT_VALUES: tuple[str, ...] = ("newest", "oldest")
SEGMENTED_VALUES: tuple[str, ...] = ("true", "false")

DEFAULT_LIMIT = 24
MAX_LIMIT = 100

Sort = Literal["newest", "oldest"]


@dataclass(frozen=True, slots=True)
class ImageQuery:
    """A validated query. Field order matches the Zod schema, because the
    order of keys in ``error.details`` follows it."""

    q: str | None = None
    collection: str | None = None
    release: str | None = None
    sequence: str | None = None
    oktas: int | None = None
    oktas_min: int | None = None
    oktas_max: int | None = None
    segmented: bool | None = None
    season: str | None = None
    time: str | None = None
    location: str | None = None
    sky_class: str | None = None
    artifact: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    sort: Sort = "newest"
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# Zod-compatible primitives
# ---------------------------------------------------------------------------


class _InvalidValueError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _js_number(raw: str) -> float:
    """``Number(value)`` semantics.

    Empty and whitespace-only strings become 0; anything unparseable becomes
    NaN. Python's ``float()`` raises on both and accepts ``"nan"``/``"inf"``,
    which JavaScript's ``Number`` also accepts for "Infinity" but not "nan",
    so the special cases are handled explicitly.
    """
    text = raw.strip()
    if not text:
        return 0.0
    if text in {"Infinity", "+Infinity"}:
        return math.inf
    if text == "-Infinity":
        return -math.inf
    try:
        # Reject the spellings float() accepts but Number() does not.
        lowered = text.lower().lstrip("+-")
        if lowered in {"nan", "inf", "infinity"} or "_" in text:
            return math.nan
        return float(text)
    except ValueError:
        return math.nan


def _coerce_int(raw: str, *, minimum: int | None, maximum: int | None) -> int:
    value = _js_number(raw)
    if math.isnan(value):
        raise _InvalidValueError("Expected number, received nan")
    if math.isinf(value) or value != int(value):
        raise _InvalidValueError("Expected integer, received float")
    number = int(value)
    if minimum is not None and number < minimum:
        raise _InvalidValueError(f"Number must be greater than or equal to {minimum}")
    if maximum is not None and number > maximum:
        raise _InvalidValueError(f"Number must be less than or equal to {maximum}")
    return number


def _string(raw: str, *, max_length: int) -> str:
    value = raw.strip()
    if len(value) > max_length:
        raise _InvalidValueError(f"String must contain at most {max_length} character(s)")
    return value


def _enum(raw: str, allowed: Sequence[str]) -> str:
    value = raw.strip()
    if value not in allowed:
        expected = " | ".join(f"'{option}'" for option in allowed)
        raise _InvalidValueError(f"Invalid enum value. Expected {expected}, received '{value}'")
    return value


def _regex(raw: str, pattern: str, *, max_length: int) -> str:
    value = _string(raw, max_length=max_length)
    if not re.fullmatch(pattern, value):
        # Zod emits a bare "Invalid" for a failed .regex() with no custom message.
        raise _InvalidValueError("Invalid")
    return value


def _iso_date(raw: str) -> str:
    """``z.string().date()`` - a calendar date, not a datetime."""
    value = raw.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise _InvalidValueError("Invalid date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise _InvalidValueError("Invalid date") from exc
    return value


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Parser:
    params: Mapping[str, str]
    errors: FieldErrors = field(default_factory=dict)

    def read(self, name: str, parse: Any) -> Any:
        raw = self.params.get(name)
        if raw is None:
            return None
        try:
            return parse(raw)
        except _InvalidValueError as exc:
            self.errors.setdefault(name, []).append(exc.message)
            return None


def parse_image_query(params: Mapping[str, str]) -> ImageQuery:
    """Validate raw query parameters, or raise :class:`InvalidQueryError`.

    Every field is attempted even after an earlier one fails, so a request with
    several bad parameters reports all of them at once - matching Zod's
    ``safeParse`` plus ``flatten().fieldErrors``.
    """
    parser = _Parser(params)

    # Declaration order is significant: it decides the key order of
    # error.details, which the recorded fixtures pin.
    q = parser.read("q", lambda raw: _string(raw, max_length=100))
    collection = parser.read("collection", lambda raw: _string(raw, max_length=80))
    release = parser.read("release", lambda raw: _string(raw, max_length=20))
    sequence = parser.read("sequence", lambda raw: _regex(raw, SEQUENCE_ID_PATTERN, max_length=80))
    oktas = parser.read("oktas", lambda raw: _coerce_int(raw, minimum=0, maximum=8))
    oktas_min = parser.read("oktasMin", lambda raw: _coerce_int(raw, minimum=0, maximum=8))
    oktas_max = parser.read("oktasMax", lambda raw: _coerce_int(raw, minimum=0, maximum=8))
    segmented = parser.read("segmented", lambda raw: _enum(raw, SEGMENTED_VALUES))
    season = parser.read("season", lambda raw: _enum(raw, SEASONS))
    time_of_day = parser.read("time", lambda raw: _enum(raw, TIMES_OF_DAY))
    location = parser.read("location", lambda raw: _string(raw, max_length=80))
    sky_class = parser.read("skyClass", lambda raw: _string(raw, max_length=80))
    artifact = parser.read("artifact", lambda raw: _enum(raw, ARTIFACT_TYPES))
    date_from = parser.read("from", _iso_date)
    date_to = parser.read("to", _iso_date)
    sort = parser.read("sort", lambda raw: _enum(raw, SORT_VALUES))
    cursor = parser.read("cursor", lambda raw: _string(raw, max_length=200))
    limit = parser.read("limit", lambda raw: _coerce_int(raw, minimum=1, maximum=MAX_LIMIT))

    if parser.errors:
        raise InvalidQueryError(details=parser.errors)

    return ImageQuery(
        q=q or None,
        collection=collection or None,
        release=release or None,
        sequence=sequence,
        oktas=oktas,
        oktas_min=oktas_min,
        oktas_max=oktas_max,
        segmented=None if segmented is None else segmented == "true",
        season=season,
        time=time_of_day,
        location=location or None,
        sky_class=sky_class or None,
        artifact=artifact,
        date_from=date_from,
        date_to=date_to,
        sort=sort or "newest",
        cursor=cursor,
        limit=DEFAULT_LIMIT if limit is None else limit,
    )
