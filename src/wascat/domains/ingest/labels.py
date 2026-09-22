"""Loading the observer's labels, and a model's readings of the same frames.

Two tables, both keyed by the archive's own tag number:

*Labels* are what a person wrote down: ``WAS-V11-F1892, SC, 07`` - the tag, the
cloud genus as a standard abbreviation, and the total cloud cover in oktas.
This is the archive's ground truth, and it is per frame. It is deliberately
not derived from anything: the cloud cover the archive already holds is
measured from the segmentation mask and lives in its own column, so loading
these never overwrites a measurement, and the two can disagree.

*Predictions* are what a classifier said: for one frame, a probability for
every class, not a winning label. Loading them creates nothing that looks like
an observation - a model's reading is stored as a model's reading, attributed
to a named model and version.

Neither loader invents a vocabulary. A cloud code that ``cloud_types`` cannot
expand stops the run and names the row; a code it can expand gets a SKY_CLASS
term created for it on first use, so the vocabulary ends up being exactly the
set of codes the archive's own data uses.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.logging import get_logger
from wascat.domains.catalog.models import ImageClassPrediction, ImageRecord, PredictionModel
from wascat.domains.ingest import cloud_types
from wascat.domains.vocab import service as vocab_service
from wascat.domains.vocab.models import VocabKind, VocabularyTerm

log = get_logger(__name__)

#: A probability vector may miss 1 by this much before the row is rejected.
#: Wide enough for a softmax printed to four decimal places, narrow enough that
#: a genuinely truncated vector - a top-3 pasted out of a notebook - is caught
#: rather than silently stored as if it were the whole distribution.
PROBABILITY_TOLERANCE = Decimal("0.005")

#: First-cell values that mean "this row names the columns". Compared
#: case-insensitively, so a table with a header is not read as data.
HEADER_HINTS = frozenset({"tag", "id", "image", "record", "frame", "filename", "file"})

#: File extensions read through openpyxl rather than the csv module.
WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xlsm"})

#: Extensions stripped off a tag taken from a directory listing.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


class LabelFormatError(ValueError):
    """The table could not be read in the shape this loader expects."""


@dataclass(frozen=True, slots=True)
class LabelRow:
    tag: str
    code: str
    oktas: int | None
    line: int


@dataclass
class LabelReport:
    scanned: int = 0
    applied: int = 0
    unchanged: int = 0
    unknown_tags: list[str] = field(default_factory=list)
    terms_created: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.scanned:,} rows read, "
            f"{self.applied:,} records labelled, "
            f"{self.unchanged:,} already correct, "
            f"{_plural(len(self.unknown_tags), 'tag')} not in the archive"
        )


@dataclass
class PredictionReport:
    scanned: int = 0
    frames: int = 0
    classes: int = 0
    replaced: int = 0
    unknown_tags: list[str] = field(default_factory=list)
    terms_created: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.scanned:,} rows read, "
            f"{self.classes:,} class scores across {self.frames:,} frames "
            f"({self.replaced:,} earlier scores replaced), "
            f"{_plural(len(self.unknown_tags), 'tag')} not in the archive"
        )


# -- reading -----------------------------------------------------------------


def read_table(path: Path) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(line number, cells)`` for every non-empty row of a table.

    CSV, TSV and anything else the ``csv`` sniffer recognises are read
    directly. A spreadsheet goes through openpyxl when it is installed, and
    otherwise raises with instructions - guessing at a binary format is worse
    than asking.
    """
    if path.suffix.lower() in WORKBOOK_SUFFIXES:
        yield from _read_workbook(path)
        return

    text = path.read_text(encoding="utf-8-sig")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        # One column, or a file too uniform for the sniffer to call. Comma is
        # the safe default: a single-column file parses the same either way.
        dialect = csv.excel
    for line, cells in enumerate(csv.reader(text.splitlines(), dialect), start=1):
        stripped = [cell.strip() for cell in cells]
        if any(stripped):
            yield line, stripped


def _read_workbook(path: Path) -> Iterator[tuple[int, list[str]]]:
    try:
        from openpyxl import load_workbook  # type: ignore[import-untyped] # noqa: PLC0415
    except ModuleNotFoundError:
        raise LabelFormatError(
            f"{path.name} is a spreadsheet and openpyxl is not installed. "
            f"Either add openpyxl to the project, or save the sheet as CSV "
            f"and pass that instead."
        ) from None

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        if sheet is None:  # pragma: no cover - only an empty workbook
            return
        for line, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            cells = ["" if value is None else str(value).strip() for value in row]
            if any(cells):
                yield line, cells
    finally:
        workbook.close()


def _is_header(cells: Sequence[str]) -> bool:
    """True when this row names its columns rather than holding data.

    The tag is the giveaway: the archive's identifier format is strict enough
    that every data row starts with one, so a first cell that is neither a tag
    nor empty is a header.
    """
    if not cells:
        return False
    first = cells[0].strip().lower()
    return first in HEADER_HINTS or not first.startswith("was-")


def normalise_tag(value: str) -> str:
    """The archive's identifier, as written in someone else's spreadsheet.

    Tolerates a lowercase tag and a trailing image extension, because a label
    table is often built from a directory listing. Nothing else is coerced, so
    a genuinely wrong tag is reported rather than quietly matched to some
    other record.
    """
    tag = value.strip()
    for suffix in IMAGE_SUFFIXES:
        if tag.lower().endswith(suffix):
            tag = tag[: -len(suffix)]
            break
    return tag.upper()


def parse_oktas(value: str, *, line: int) -> int | None:
    """Column 3: total cloud cover in eighths, often zero-padded ("07")."""
    text = value.strip()
    if not text:
        return None
    try:
        oktas = int(text)
    except ValueError:
        raise LabelFormatError(f"line {line}: {value!r} is not a cloud cover in oktas") from None
    if not 0 <= oktas <= 8:
        raise LabelFormatError(
            f"line {line}: cloud cover is measured in eighths, so {oktas} is out of range"
        )
    return oktas


def parse_labels(path: Path) -> list[LabelRow]:
    """Read the label table into rows, failing on anything ambiguous."""
    rows: list[LabelRow] = []
    for line, cells in read_table(path):
        if not rows and _is_header(cells):
            continue
        if len(cells) < 2:
            raise LabelFormatError(
                f"line {line}: expected tag, cloud type and oktas; got {cells!r}"
            )
        tag = normalise_tag(cells[0])
        code = cloud_types.normalise(cells[1])
        # Validated here rather than at write time, so an unknown code stops
        # the run before a single record has been touched.
        cloud_types.label_for(code)
        oktas = parse_oktas(cells[2], line=line) if len(cells) > 2 else None
        rows.append(LabelRow(tag=tag, code=code, oktas=oktas, line=line))
    return rows


def parse_predictions(path: Path) -> list[tuple[str, str, Decimal]]:
    """Read a probability table as ``(tag, code, probability)`` triples.

    Two shapes are accepted, because both are what a notebook actually writes:
    long - one row per class, ``tag, code, probability`` - and wide, one column
    per class with the codes in the header. The header is what tells them
    apart, so a wide table must have one.
    """
    table = list(read_table(path))
    if not table:
        return []

    _, header = table[0]
    wide_codes = _wide_header(header)
    if wide_codes is not None:
        return _parse_wide(table[1:], wide_codes)
    body = table[1:] if _is_header(header) else table
    return _parse_long(body)


def _wide_header(header: Sequence[str]) -> list[str] | None:
    """The class codes of a wide table's header, or None if it is not one."""
    if len(header) < 3:
        return None
    codes = [cloud_types.normalise(cell) for cell in header[1:]]
    if not all(code in cloud_types.CLOUD_GENERA for code in codes):
        return None
    return codes


def _parse_wide(
    rows: list[tuple[int, list[str]]], codes: list[str]
) -> list[tuple[str, str, Decimal]]:
    out: list[tuple[str, str, Decimal]] = []
    for line, cells in rows:
        tag = normalise_tag(cells[0])
        for code, cell in zip(codes, cells[1:], strict=False):
            if not cell:
                continue
            out.append((tag, code, _parse_probability(cell, line=line)))
    return out


def _parse_long(rows: list[tuple[int, list[str]]]) -> list[tuple[str, str, Decimal]]:
    out: list[tuple[str, str, Decimal]] = []
    for line, cells in rows:
        if len(cells) < 3:
            raise LabelFormatError(
                f"line {line}: expected tag, cloud type and probability; got {cells!r}"
            )
        code = cloud_types.normalise(cells[1])
        cloud_types.label_for(code)
        out.append((normalise_tag(cells[0]), code, _parse_probability(cells[2], line=line)))
    return out


def _parse_probability(value: str, *, line: int) -> Decimal:
    """A probability, written as a fraction or as a percentage.

    A cell over 1 is read as a percentage rather than rejected: "80%" and "80"
    are both what a notebook prints, and a probability above 1 has no other
    sensible reading.
    """
    raw = value.strip()
    percent = raw.endswith("%")
    try:
        number = Decimal(raw.rstrip("%").strip())
    except InvalidOperation:
        raise LabelFormatError(f"line {line}: {value!r} is not a probability") from None
    if percent or number > 1:
        number = number / 100
    if not 0 <= number <= 1:
        raise LabelFormatError(f"line {line}: probability {value!r} is outside 0..1")
    # Matches the column's scale, so what is stored is what sums to 1.
    return number.quantize(Decimal("0.00001"))


# -- writing -----------------------------------------------------------------


async def _sky_class_terms(
    session: AsyncSession, codes: set[str], *, created: list[str]
) -> dict[str, VocabularyTerm]:
    """Resolve every code to a SKY_CLASS term, creating the missing ones.

    SKY_CLASS is not one of the vocabularies the public query schema validates
    as a closed set, so adding to it is an editorial act rather than a contract
    change - which is why this may create terms where a SEASON loader could
    not. It is also why the vocabulary is seeded from here and not from a
    migration: the terms that exist are the codes the data actually uses.
    """
    terms: dict[str, VocabularyTerm] = {}
    for code in sorted(codes):
        label = cloud_types.label_for(code)
        term = await vocab_service.resolve(session, VocabKind.SKY_CLASS, label)
        if term is None:
            term = await vocab_service.create_term(
                session,
                kind=VocabKind.SKY_CLASS,
                label=label,
                description=f"Cloud genus, observer code {code}.",
            )
            created.append(label)
        terms[code] = term
    return terms


async def apply_labels(
    session: AsyncSession,
    rows: Sequence[LabelRow],
    *,
    dry_run: bool = False,
    allow_published: bool = False,
) -> LabelReport:
    """Write the observer's genus and okta count onto the records named.

    A tag with no record is reported, never created: the label table describes
    the archive, it does not extend it. Re-running is a no-op, so a corrected
    table can simply be loaded again.

    Every record in the archive sits in a published release, and a published
    release is immutable, so the database refuses this write unless the guard
    is lifted. `allow_published` is what lifts it - deliberately a parameter
    rather than something this function assumes, since what it permits is a
    write to published rows. It is safe to permit here in a way it would not
    be for a re-measurement: the columns touched are the observer's
    classification, which the release never claimed to contain, and neither
    `cloud_fraction` nor `cloud_cover_oktas` nor any artifact is among them.
    The audit trail is the label table itself, which is kept.
    """
    report = LabelReport(scanned=len(rows))
    if not rows:
        return report

    if allow_published and not dry_run:
        # SET LOCAL, so it lasts exactly as long as this transaction.
        await session.execute(text("SET LOCAL wascat.allow_published_writes = 'on'"))

    created: list[str] = []
    terms = await _sky_class_terms(session, {row.code for row in rows}, created=created)
    report.terms_created = created

    # Last row wins for a repeated tag, which is how a curator reads a table
    # they have appended a correction to.
    wanted = {row.tag: row for row in rows}
    records = (
        (await session.execute(select(ImageRecord).where(ImageRecord.id.in_(wanted))))
        .scalars()
        .all()
    )
    found = {record.id for record in records}
    report.unknown_tags = sorted(tag for tag in wanted if tag not in found)

    for record in records:
        row = wanted[record.id]
        term = terms[row.code]
        if record.sky_class_id == term.id and record.observed_cloud_cover_oktas == row.oktas:
            report.unchanged += 1
            continue
        if not dry_run:
            record.sky_class_id = term.id
            record.sky_class_label = term.label
            record.observed_cloud_cover_oktas = row.oktas
        report.applied += 1

    if dry_run:
        # The terms were created in this transaction too, and a dry run has to
        # leave nothing behind.
        await session.rollback()
    else:
        await session.flush()
    return report


async def upsert_model(
    session: AsyncSession, *, slug: str, name: str, version: str, description: str | None = None
) -> PredictionModel:
    """Find the model, or record it. Its identity is the slug."""
    model = (
        (await session.execute(select(PredictionModel).where(PredictionModel.slug == slug)))
        .scalars()
        .first()
    )
    if model is None:
        model = PredictionModel(slug=slug, name=name, version=version, description=description)
        session.add(model)
        await session.flush()
        return model
    # A retrained model keeps its slug and gains a version. Nothing else about
    # an existing row is overwritten by a load, so a curator's edits to the
    # name or description survive the next ingest.
    model.version = version
    model.retired_at = None
    return model


async def apply_predictions(
    session: AsyncSession,
    triples: Sequence[tuple[str, str, Decimal]],
    *,
    model: PredictionModel,
    dry_run: bool = False,
) -> PredictionReport:
    """Store one model's probability vectors, replacing any it had before.

    Replacement is per frame, not per class: a model that used to score eleven
    classes and now scores nine must not leave the other two behind, which is
    exactly what a class-by-class upsert would do.
    """
    report = PredictionReport(scanned=len(triples))
    if not triples:
        return report

    created: list[str] = []
    terms = await _sky_class_terms(session, {code for _, code, _ in triples}, created=created)
    report.terms_created = created

    by_tag: dict[str, dict[str, Decimal]] = {}
    for tag, code, probability in triples:
        by_tag.setdefault(tag, {})[code] = probability

    for tag, vector in by_tag.items():
        total = sum(vector.values(), Decimal(0))
        if abs(total - 1) > PROBABILITY_TOLERANCE:
            raise LabelFormatError(
                f"{tag}: the {len(vector)} probabilities given sum to {total}, not 1. "
                f"A partial vector would be stored as if it were the whole distribution."
            )

    known = set(
        (await session.execute(select(ImageRecord.id).where(ImageRecord.id.in_(by_tag))))
        .scalars()
        .all()
    )
    report.unknown_tags = sorted(tag for tag in by_tag if tag not in known)

    if not dry_run and known:
        replaced = await session.execute(
            delete(ImageClassPrediction).where(
                ImageClassPrediction.model_id == model.id,
                ImageClassPrediction.image_id.in_(known),
            )
        )
        report.replaced = cast("CursorResult[Any]", replaced).rowcount or 0

    for tag in sorted(known):
        report.frames += 1
        for code, probability in by_tag[tag].items():
            report.classes += 1
            if dry_run:
                continue
            term = terms[code]
            session.add(
                ImageClassPrediction(
                    image_id=tag,
                    model_id=model.id,
                    sky_class_id=term.id,
                    sky_class_label=term.label,
                    probability=probability,
                )
            )

    if dry_run:
        await session.rollback()
    else:
        await session.flush()
    return report
