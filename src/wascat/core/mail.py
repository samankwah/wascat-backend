"""Outbound mail, through Resend.

One function, awaited inline. A serverless platform may freeze the process
as soon as the response is sent, so a background task could be cut off
half-way through a send; awaiting it, with a short timeout, means the request
that triggered a message knows whether it went.

A failure never propagates. The caller gets the error text back to record
alongside whatever it was notifying about, so a mail outage costs a resend
from the dashboard rather than a lost data request.

With no API key configured the message is logged instead, which is what local
development and the tests want.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from wascat.core.config import get_settings
from wascat.core.logging import get_logger

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 8.0

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Message:
    to: Sequence[str]
    subject: str
    text: str
    html: str


#: Replaced in tests to capture what would have been sent.
transport: httpx.AsyncBaseTransport | None = None


def mail_configured() -> bool:
    settings = get_settings()
    return bool(settings.resend_api_key and settings.mail_from)


async def send(message: Message) -> str | None:
    """Send one message. Returns None on success, or why it failed."""
    if not message.to:
        return None
    settings = get_settings()
    if not mail_configured():
        log.info(
            "mail.not_sent",
            reason="mail not configured",
            to=list(message.to),
            subject=message.subject,
        )
        return None

    payload: dict[str, object] = {
        "from": settings.mail_from,
        "to": list(message.to),
        "subject": message.subject,
        "text": message.text,
        "html": message.html,
    }
    if settings.mail_reply_to:
        payload["reply_to"] = settings.mail_reply_to

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(
                RESEND_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
    except httpx.HTTPError as exc:
        log.warning("mail.failed", error=type(exc).__name__, subject=message.subject)
        return f"{type(exc).__name__}: could not reach the mail service"

    if response.status_code >= 400:
        # Resend explains itself in {"message": ...}; never echo the request,
        # which carries the API key in its headers.
        try:
            detail = str(response.json().get("message", ""))
        except ValueError:
            detail = ""
        log.warning(
            "mail.rejected", status=response.status_code, detail=detail, subject=message.subject
        )
        return f"mail service returned {response.status_code}{f': {detail}' if detail else ''}"

    log.info("mail.sent", to_count=len(message.to), subject=message.subject)
    return None
