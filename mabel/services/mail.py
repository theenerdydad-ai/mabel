"""Trusted server-side SMTP delivery for MABEL-owned mail operations."""

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol


class MailDeliveryError(RuntimeError):
    """A safe classification boundary around a mail transport failure."""


class MailNotConfiguredError(MailDeliveryError):
    pass


class MailRejectedError(MailDeliveryError):
    pass


class MailTimeoutError(MailDeliveryError):
    pass


class MailUnavailableError(MailDeliveryError):
    pass


@dataclass(frozen=True)
class MailMessage:
    to_address: str
    subject: str
    body: str


@dataclass(frozen=True)
class MailDeliveryReceipt:
    provider: str
    external_id: str | None = None


class MailDelivery(Protocol):
    async def send(self, message: MailMessage) -> MailDeliveryReceipt: ...


class SmtpMailDelivery:
    """The AMIE-compatible SMTP configuration boundary for trusted MABEL code.

    It deliberately reads no values from capability arguments or child-process
    environments.  The setting names mirror AMIE's proven 2FA mail path:
    ``EMAIL_BACKEND``, ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USERNAME``,
    ``SMTP_PASSWORD``, ``SMTP_FROM``, and ``SMTP_STARTTLS``.
    """

    def __init__(
        self,
        *,
        backend: str,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        starttls: bool,
    ) -> None:
        self._backend = backend
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._starttls = starttls

    def is_configured(self) -> bool:
        return self._backend == "smtp" and bool(self._host) and bool(self._from_address)

    async def send(self, message: MailMessage) -> MailDeliveryReceipt:
        if not self.is_configured():
            raise MailNotConfiguredError()
        try:
            await asyncio.to_thread(self._send_smtp_message, message)
        except TimeoutError as error:
            raise MailTimeoutError() from error
        except smtplib.SMTPRecipientsRefused as error:
            raise MailRejectedError() from error
        except smtplib.SMTPResponseException as error:
            raise MailRejectedError() from error
        except (OSError, smtplib.SMTPException) as error:
            raise MailUnavailableError() from error
        return MailDeliveryReceipt(provider="smtp")

    def _send_smtp_message(self, delivery: MailMessage) -> None:
        message = EmailMessage()
        message["From"] = self._from_address
        message["To"] = delivery.to_address
        message["Subject"] = delivery.subject
        message.set_content(delivery.body)
        with smtplib.SMTP(self._host, self._port, timeout=20) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)
