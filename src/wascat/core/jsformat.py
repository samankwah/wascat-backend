"""JavaScript-compatible formatting and arithmetic.

The public API contract was established by a TypeScript implementation. Several
of its outputs are not merely "a number" or "a date" but a specific rendering
that clients and recorded fixtures depend on. Anything in this module exists
because Python's obvious equivalent produces a *different* result.

Nothing here is styling. All of it is contract.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

__all__ = [
    "bytes_to_size",
    "iso_z",
    "js_number",
    "js_round",
    "locale_int",
    "okta_label",
    "oktas_from_fraction",
]


def js_round(value: float) -> int:
    """Round half toward positive infinity, the way ``Math.round`` does.

    Python's :func:`round` and :func:`numpy.round` both round half to even
    (banker's rounding), so ``round(0.5) == 0`` and ``round(2.5) == 2`` where
    JavaScript gives ``1`` and ``3``. Every okta bucket and every mask sample
    coordinate in the ingest pipeline is derived through this, so using the
    wrong one silently shifts measurements at each half-way boundary.
    """
    return math.floor(value + 0.5)


def oktas_from_fraction(fraction: float | Decimal) -> int:
    """Cloud cover in eighths of sky, the synoptic convention.

    Mirrors ``oktasFromFraction`` in lib/vocab.ts:
    ``Math.min(8, Math.max(0, Math.round(fraction * 8)))``.
    """
    return min(8, max(0, js_round(float(fraction) * 8)))


def okta_label(okta: int) -> str:
    """Human label for an okta bucket. Mirrors ``oktaLabel`` in lib/vocab.ts.

    The separator is U+00B7 MIDDLE DOT, not a hyphen or a full stop.
    """
    if okta == 0:
        return "0/8 · Clear"
    if okta == 8:
        return "8/8 · Overcast"
    return f"{okta}/8"


def js_number(value: float | Decimal) -> int | float:
    """Render a stored decimal the way ``JSON.stringify`` renders a JS number.

    Two rules, both observable in the recorded fixtures:

    * The generator wrote ``Number(fraction.toFixed(6))``, so ``0.552600``
      serialises as ``0.5526`` and never as ``0.552600`` or the float-noise
      ``0.5525999999999999``. Python's ``float`` repr is already the shortest
      string that round-trips, the same rule V8 uses.
    * JavaScript has one number type, so an integral value has no trailing
      ``.0``: ``maskScale`` is ``1``, not ``1.0``. A registered sequence's
      ``1.146497`` still renders in full.
    """
    number = float(value)
    return int(number) if number.is_integer() else number


def locale_int(value: int) -> str:
    """Group thousands with commas, as ``Number.prototype.toLocaleString`` does
    for the ``en`` locales the site renders under."""
    return f"{value:,}"


def bytes_to_size(num_bytes: int) -> str:
    """Mirror ``bytesToSize`` in lib/catalog.ts.

    Binary units with a decimal-looking suffix, one decimal place for GB and
    MB, none for KB, and no lower bound - 400 bytes renders as "0 KB".
    """
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.1f} GB"
    if num_bytes >= 1024**2:
        return f"{num_bytes / 1024**2:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def iso_z(moment: datetime | None = None) -> str:
    """Format a timestamp the way ``Date.prototype.toISOString`` does.

    Always exactly three fractional digits and a literal ``Z``. Python's
    :meth:`datetime.isoformat` emits six digits and ``+00:00``, neither of
    which matches the recorded ``meta.generatedAt``.
    """
    moment = moment.astimezone(UTC) if moment else datetime.now(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
