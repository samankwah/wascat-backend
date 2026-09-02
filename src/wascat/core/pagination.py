"""Opaque cursors.

The pre-migration API encoded ``{"offset": N}`` as base64url and sliced an
in-memory array. Offset paging is fine over 2,422 bundled records and wrong
over an archive that is about to grow to ~17,900 and keep going: the deeper
the page, the more rows Postgres walks and discards, and a row inserted
mid-scroll shifts every later page by one.

So the cursor becomes a keyset over ``(sort_key, id)`` - the exact ordering
the API already promises - while staying an opaque base64url blob. Legacy
offset cursors are still honoured, because a client may hold one.

Two behaviours are contract and covered by recorded fixtures:
  * a malformed cursor silently restarts from the beginning, it does not 400;
  * ``links.next`` and ``meta.nextCursor`` are present-but-null on the last
    page rather than omitted.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Self

import orjson


def _b64encode(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(payload)).rstrip(b"=").decode()


def _b64decode(raw: str) -> Any:
    padded = raw + "=" * (-len(raw) % 4)
    return orjson.loads(base64.urlsafe_b64decode(padded))


@dataclass(frozen=True, slots=True)
class Cursor:
    """Where the next page starts.

    ``offset`` is only populated for legacy cursors, or as a running position
    carried along for display; the query itself uses ``sort_key``/``record_id``
    whenever they are set.
    """

    sort_key: str | None = None
    record_id: str | None = None
    offset: int = 0

    @classmethod
    def start(cls) -> Self:
        return cls()

    @property
    def is_keyset(self) -> bool:
        return self.sort_key is not None and self.record_id is not None

    def encode(self) -> str:
        if self.is_keyset:
            return _b64encode({"k": self.sort_key, "i": self.record_id, "o": self.offset})
        return _b64encode({"offset": self.offset})


def encode_cursor(sort_key: str, record_id: str, offset: int = 0) -> str:
    return Cursor(sort_key=sort_key, record_id=record_id, offset=offset).encode()


def encode_offset_cursor(offset: int) -> str:
    """Legacy form, kept so the encoder round-trips what the decoder accepts."""
    return Cursor(offset=offset).encode()


def decode_cursor(raw: str | None) -> Cursor:
    """Parse a cursor, falling back to the start on anything unexpected.

    Mirrors ``decodeCursor`` in lib/search.ts, which swallows every parse
    failure and returns offset 0. Raising here would turn a stale bookmark
    into a 400 and change the contract.
    """
    if not raw:
        return Cursor.start()
    try:
        payload = _b64decode(raw)
    except Exception:
        return Cursor.start()
    if not isinstance(payload, dict):
        return Cursor.start()

    keyset_key = payload.get("k")
    keyset_id = payload.get("i")
    if isinstance(keyset_key, str) and isinstance(keyset_id, str):
        offset = payload.get("o")
        return Cursor(
            sort_key=keyset_key,
            record_id=keyset_id,
            offset=offset if isinstance(offset, int) and offset >= 0 else 0,
        )

    offset = payload.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
        return Cursor(offset=offset)
    return Cursor.start()
