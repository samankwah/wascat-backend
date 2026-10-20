"""The four messages a data request sends.

Plain text first, then the same words as minimal HTML: no images, no
tracking, nothing a mail client needs to fetch. Everything the requester typed
is escaped before it goes into the HTML, so a name cannot carry markup into an
administrator's inbox.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

from wascat.core.config import get_settings
from wascat.core.mail import Message
from wascat.domains.requests.models import DataRequest

SIGNATURE = "WASCAT - West African Sky Cloud Atlas"


def _site(path: str) -> str:
    return f"{get_settings().public_base_url}{path}"


def _when(moment: datetime) -> str:
    return moment.strftime("%d %B %Y, %H:%M UTC")


def _html(
    paragraphs: Sequence[str], link: tuple[str, str] | None = None, after: Sequence[str] = ()
) -> str:
    """Paragraphs are already-escaped HTML; the link's label and URL are escaped
    here. ``after`` follows the link."""
    body = "".join(f'<p style="margin:0 0 14px">{paragraph}</p>' for paragraph in paragraphs)
    if link:
        label, url = link
        body += (
            f'<p style="margin:22px 0"><a href="{escape(url, quote=True)}" '
            'style="background:#1c6d99;color:#ffffff;padding:11px 18px;border-radius:8px;'
            f'text-decoration:none;font-weight:600">{escape(label)}</a></p>'
        )
    body += "".join(f'<p style="margin:0 0 14px">{paragraph}</p>' for paragraph in after)
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;'
        f'color:#102433;max-width:560px">{body}'
        f'<p style="margin:28px 0 0;color:#5d7080;font-size:13px">{escape(SIGNATURE)}</p></div>'
    )


def _lines(rows: Sequence[tuple[str, str]]) -> str:
    return "<br>".join(
        f"<strong>{escape(label)}:</strong> {escape(value)}" for label, value in rows
    )


def admin_notification(request: DataRequest, recipients: Sequence[str]) -> Message:
    link = _site(f"/admin/requests/{request.id}")
    details = [
        ("Reference", request.reference),
        ("Name", request.name),
        ("Email", request.email),
        ("Organisation", request.organisation),
        ("Collections", ", ".join(request.collections)),
    ]
    text = (
        "A new data request is waiting for review.\n\n"
        + "\n".join(f"{label}: {value}" for label, value in details)
        + f"\n\nIntended use:\n{request.purpose}\n\nReview it: {link}\n\n{SIGNATURE}\n"
    )
    html = _html(
        [
            "A new data request is waiting for review.",
            _lines(details),
            f"<strong>Intended use:</strong><br>{escape(request.purpose)}",
        ],
        ("Review the request", link),
    )
    return Message(
        to=recipients,
        subject=f"New data request {request.reference} from {request.name}",
        text=text,
        html=html,
    )


def acknowledgement(request: DataRequest) -> Message:
    text = (
        f"Dear {request.name},\n\n"
        f"We have received your request for WASCAT data (reference {request.reference}).\n"
        f"Collections: {', '.join(request.collections)}\n\n"
        "An administrator will review it and email you at this address with a download link, "
        "or with an explanation if it cannot be approved.\n\n"
        f"{SIGNATURE}\n"
    )
    html = _html(
        [
            f"Dear {escape(request.name)},",
            f"We have received your request for WASCAT data (reference <strong>{escape(request.reference)}</strong>).",
            f"<strong>Collections:</strong> {escape(', '.join(request.collections))}",
            "An administrator will review it and email you at this address with a download link, "
            "or with an explanation if it cannot be approved.",
        ]
    )
    return Message(
        to=[request.email],
        subject=f"We received your data request {request.reference}",
        text=text,
        html=html,
    )


def approval(request: DataRequest, *, token: str) -> Message:
    if request.access_expires_at is None:
        raise ValueError("an approval email needs the access expiry set first")
    link = _site(f"/data/{token}")
    expires = _when(request.access_expires_at)
    note = request.decision_note
    text = (
        f"Dear {request.name},\n\n"
        f"Your request {request.reference} has been approved.\n\n"
        + (f"A note from the reviewer:\n{note}\n\n" if note else "")
        + f"Download the data here: {link}\n"
        f"The link is private to you and works until {expires}.\n\n"
        "Please cite WASCAT and follow each collection's licence, shown on the download page.\n\n"
        f"{SIGNATURE}\n"
    )
    paragraphs = [
        f"Dear {escape(request.name)},",
        f"Your request <strong>{escape(request.reference)}</strong> has been approved.",
    ]
    if note:
        paragraphs.append(f"<strong>A note from the reviewer:</strong><br>{escape(note)}")
    paragraphs.append(
        f"The link below is private to you and works until <strong>{escape(expires)}</strong>."
    )
    html = _html(
        paragraphs,
        ("Download the data", link),
        after=[
            "Please cite WASCAT and follow each collection's licence, shown on the download page."
        ],
    )
    return Message(
        to=[request.email],
        subject=f"Your WASCAT data is ready ({request.reference})",
        text=text,
        html=html,
    )


def decline(request: DataRequest) -> Message:
    note = request.decision_note or ""
    text = (
        f"Dear {request.name},\n\n"
        f"Your request {request.reference} could not be approved.\n\n"
        f"Reason:\n{note}\n\n"
        "You are welcome to reply to this message or submit a new request.\n\n"
        f"{SIGNATURE}\n"
    )
    html = _html(
        [
            f"Dear {escape(request.name)},",
            f"Your request <strong>{escape(request.reference)}</strong> could not be approved.",
            f"<strong>Reason:</strong><br>{escape(note)}",
            "You are welcome to reply to this message or submit a new request.",
        ]
    )
    return Message(
        to=[request.email],
        subject=f"About your WASCAT data request {request.reference}",
        text=text,
        html=html,
    )
