"""Outbound mail, through Resend or any SMTP server.

Resend needs a sending domain verified there; SMTP (Gmail with an app
password, for instance) needs only an account, so it covers the time before
there is a domain. With both configured, Resend is used.

One function, awaited inline. A serverless platform may freeze the process
as soon as the response is sent, so a background task could be cut off
half-way through a send; awaiting it, with a short timeout, means the request
that triggered a message knows whether it went.

A failure never propagates. The caller gets the error text back to record
alongside whatever it was notifying about, so a mail outage costs a resend
from the dashboard rather than a lost data request.

With neither configured nothing can be sent. The message is logged, and
the call reports that as a failure rather than a success, so nothing records
an email as delivered that never left: the dashboard shows it did not go, and
it can be resent once mail is set up.
"""

from __future__ import annotations

import asyncio
import smtplib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal

import httpx

from wascat.core.config import Settings, get_settings
from wascat.core.logging import get_logger

RESEND_URL = "https://api.resend.com/emails"
NOT_CONFIGURED = (
    "mail is not configured (set WASCAT_MAIL_FROM and either WASCAT_RESEND_API_KEY "
    "or WASCAT_SMTP_HOST, WASCAT_SMTP_USERNAME and WASCAT_SMTP_PASSWORD)"
)
TIMEOUT_SECONDS = 8.0
#: The port on which SMTP is TLS from the first byte; any other uses STARTTLS.
IMPLICIT_TLS_PORT = 465

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Message:
    to: Sequence[str]
    subject: str
    text: str
    html: str


#: Replaced in tests to capture what would have been sent.
transport: httpx.AsyncBaseTransport | None = None
#: Opens an SMTP connection from (host, port, timeout); replaced in tests.
smtp_factory: Callable[[str, int, float], smtplib.SMTP] | None = None


def backend(settings: Settings | None = None) -> Literal["resend", "smtp"] | None:
    """Which service mail goes through, or None when it cannot be sent."""
    settings = settings or get_settings()
    if not settings.mail_from:
        return None
    if settings.resend_api_key:
        return "resend"
    if settings.smtp_host and settings.smtp_username and settings.smtp_password:
        return "smtp"
    return None


def mail_configured() -> bool:
    return backend() is not None


async def send(message: Message) -> str | None:
    """Send one message. Returns None on success, or why it failed."""
    if not message.to:
        return None
    settings = get_settings()
    via = backend(settings)
    if via is None:
        log.info(
            "mail.not_sent",
            reason="mail not configured",
            to=list(message.to),
            subject=message.subject,
        )
        return NOT_CONFIGURED

    error = await (_send_resend if via == "resend" else _send_smtp)(settings, message)
    if error is None:
        log.info("mail.sent", via=via, to_count=len(message.to), subject=message.subject)
    return error


async def _send_resend(settings: Settings, message: Message) -> str | None:
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
    return None


async def _send_smtp(settings: Settings, message: Message) -> str | None:
    email = EmailMessage()
    email["From"] = settings.mail_from
    email["To"] = ", ".join(message.to)
    email["Subject"] = message.subject
    if settings.mail_reply_to:
        email["Reply-To"] = settings.mail_reply_to
    email.set_content(message.text)
    email.add_alternative(message.html, subtype="html")

    # smtplib blocks, so it runs on a thread - still awaited, for the reason above.
    try:
        await asyncio.to_thread(_deliver, settings, email)
    except smtplib.SMTPAuthenticationError:
        error = "SMTP sign-in failed: check WASCAT_SMTP_USERNAME and the app password"
    except smtplib.SMTPRecipientsRefused:
        error = "the mail server refused the recipient"
    except (smtplib.SMTPException, OSError) as exc:
        # Only the type: a server's reply can be long, and is no use on the dashboard.
        error = f"{type(exc).__name__}: could not reach the mail server"
    else:
        return None
    log.warning("mail.failed", error=error, subject=message.subject)
    return error


def _deliver(settings: Settings, email: EmailMessage) -> None:
    host, port = settings.smtp_host, settings.smtp_port
    if smtp_factory is not None:
        connection = smtp_factory(host, port, TIMEOUT_SECONDS)
    elif port == IMPLICIT_TLS_PORT:
        connection = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS)
    else:
        connection = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
    with connection:
        if not isinstance(connection, smtplib.SMTP_SSL):
            connection.starttls()
        connection.login(settings.smtp_username, settings.smtp_password)
        connection.send_message(email)
