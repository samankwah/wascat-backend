"""Cloud-genus abbreviations, as an observer writes them.

The archive's label table names a genus by its standard two-letter
abbreviation - `SC` for stratocumulus, `CB` for cumulonimbus - and the site
needs the word. This module is the one place that translation happens, so the
migration that seeds the vocabulary and the ingest that resolves a row against
it can never disagree about what `SC` means.

The list is the ten genera of the WMO International Cloud Atlas, plus the two
non-genus conditions an all-sky record still has to be able to say: a sky with
no cloud in it, and the Harmattan haze the archive's SEASON vocabulary already
names. Nothing else is here. A code the table uses that is not in this map is
an error the operator is shown, not a term this module invents a label for -
seeding a guessed vocabulary in advance is how the previous sky-class list
ended up describing data nobody had looked at.
"""

from __future__ import annotations

#: Abbreviation -> the label stored on the vocabulary term.
#: Insertion order is the order terms are seeded in, which is the order the
#: dashboard's Sky class dropdown shows them: high cloud first, then middle,
#: then low, then the convective and non-genus entries - the order the Cloud
#: Atlas itself uses, and the one an observer reads a coding table in.
CLOUD_GENERA: dict[str, str] = {
    "CI": "Cirrus",
    "CC": "Cirrocumulus",
    "CS": "Cirrostratus",
    "AC": "Altocumulus",
    "AS": "Altostratus",
    "NS": "Nimbostratus",
    "SC": "Stratocumulus",
    "ST": "Stratus",
    "CU": "Cumulus",
    "CB": "Cumulonimbus",
    "CL": "Clear sky",
    "HZ": "Haze / Dust",
}


class UnknownCloudCodeError(ValueError):
    """A label table used an abbreviation this module cannot expand."""

    def __init__(self, code: str) -> None:
        known = ", ".join(CLOUD_GENERA)
        super().__init__(
            f"unknown cloud type code {code!r}. Known codes: {known}. "
            f"Add it to CLOUD_GENERA with the label it should carry - the "
            f"ingest will not guess one."
        )
        self.code = code


def normalise(code: str) -> str:
    """The canonical form of a code as written in a label table.

    Tables are typed by people: `sc`, `Sc` and `SC ` are all the same genus,
    and none of them should be a distinct vocabulary term.
    """
    return code.strip().upper()


def label_for(code: str) -> str:
    """Expand an abbreviation, or say which ones exist.

    Raises rather than falling back, because a silent fallback here is how a
    typo becomes a cloud type in the public API.
    """
    key = normalise(code)
    try:
        return CLOUD_GENERA[key]
    except KeyError:
        raise UnknownCloudCodeError(key) from None
