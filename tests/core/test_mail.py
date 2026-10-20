"""The mailer: which service it uses, and that a failure comes back as text."""

from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from wascat.core import mail
from wascat.core.config import Settings, get_settings

PASSWORD = "abcd efgh ijkl mnop"
MESSAGE = mail.Message(
    to=["ama@example.org"], subject="Your data request", text="Plain body", html="<p>HTML body</p>"
)


def configure(monkeypatch: pytest.MonkeyPatch, **values: Any) -> Settings:
    settings = get_settings().model_copy(
        update={
            "resend_api_key": "",
            "smtp_host": "",
            "smtp_username": "",
            "smtp_password": "",
            "mail_from": "",
            "mail_reply_to": "",
            **values,
        }
    )
    monkeypatch.setattr(mail, "get_settings", lambda: settings)
    return settings


GMAIL = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_username": "wascat.data@gmail.com",
    "smtp_password": PASSWORD,
    "mail_from": "WASCAT <wascat.data@gmail.com>",
    "mail_reply_to": "data@example.org",
}


class FakeSMTP(smtplib.SMTP):
    """Records the conversation instead of opening a socket."""

    fail_with: Exception | None = None
    connections: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.opened = (host, port, timeout)
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.connections.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        self.calls.append("starttls")
        return (220, b"ready")

    def login(self, user: str, password: str, **kwargs: Any) -> tuple[int, bytes]:
        self.calls.append(f"login {user} {password}")
        if self.fail_with is not None:
            raise self.fail_with
        return (235, b"ok")

    def send_message(self, msg: Any, *args: Any, **kwargs: Any) -> dict[str, tuple[int, bytes]]:
        self.calls.append("send")
        self.sent.append(msg)
        return {}


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.fail_with = None
    FakeSMTP.connections = []
    monkeypatch.setattr(mail, "smtp_factory", FakeSMTP)
    return FakeSMTP


class TestBackend:
    def test_nothing_without_a_from_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = configure(monkeypatch, **{**GMAIL, "mail_from": ""})
        assert mail.backend(settings) is None

    def test_smtp_needs_host_username_and_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert mail.backend(configure(monkeypatch, **GMAIL)) == "smtp"
        assert mail.backend(configure(monkeypatch, **{**GMAIL, "smtp_password": ""})) is None

    def test_resend_wins_when_both_are_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert mail.backend(configure(monkeypatch, **GMAIL, resend_api_key="re_test")) == "resend"


class TestSmtp:
    async def test_sends_both_parts_over_starttls(
        self, monkeypatch: pytest.MonkeyPatch, smtp: type[FakeSMTP]
    ) -> None:
        configure(monkeypatch, **GMAIL)
        assert await mail.send(MESSAGE) is None

        (connection,) = smtp.connections
        assert connection.opened == ("smtp.gmail.com", 587, mail.TIMEOUT_SECONDS)
        assert connection.calls == [
            "starttls",
            f"login wascat.data@gmail.com {PASSWORD}",
            "send",
            "quit",
        ]
        (sent,) = connection.sent
        assert sent["From"] == "WASCAT <wascat.data@gmail.com>"
        assert sent["To"] == "ama@example.org"
        assert sent["Subject"] == "Your data request"
        assert sent["Reply-To"] == "data@example.org"
        parts = {part.get_content_type(): part.get_content() for part in sent.iter_parts()}
        assert parts["text/plain"].strip() == "Plain body"
        assert parts["text/html"].strip() == "<p>HTML body</p>"

    async def test_a_rejected_sign_in_says_so_without_the_password(
        self, monkeypatch: pytest.MonkeyPatch, smtp: type[FakeSMTP]
    ) -> None:
        configure(monkeypatch, **GMAIL)
        smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        error = await mail.send(MESSAGE)
        assert error is not None
        assert error.startswith("SMTP sign-in failed")
        assert PASSWORD not in error

    async def test_an_unreachable_server_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(monkeypatch, **GMAIL)

        def refuse(host: str, port: int, timeout: float) -> smtplib.SMTP:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(mail, "smtp_factory", refuse)
        assert await mail.send(MESSAGE) == "ConnectionRefusedError: could not reach the mail server"


class TestResend:
    async def test_is_used_when_it_has_a_key(
        self, monkeypatch: pytest.MonkeyPatch, smtp: type[FakeSMTP]
    ) -> None:
        configure(monkeypatch, **GMAIL, resend_api_key="re_test")
        seen: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "email_1"})

        monkeypatch.setattr(mail, "transport", httpx.MockTransport(handle))
        assert await mail.send(MESSAGE) is None
        assert [body["to"] for body in seen] == [["ama@example.org"]]
        assert smtp.connections == []


async def test_with_nothing_configured_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    assert await mail.send(MESSAGE) == mail.NOT_CONFIGURED
