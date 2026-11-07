import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from mabel.context.execution import BoundedCapabilityRunner
from mabel.context.models import CapabilityDefinition, CapabilitySummary
from mabel.config import Settings
from mabel.services.mail import (
    MailDeliveryReceipt,
    MailMessage,
    MailRejectedError,
    MailTimeoutError,
    MailUnavailableError,
    SmtpMailDelivery,
)
from mabel.services.notifications import NotificationArgumentsError, NotificationService


class RecordingDelivery:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.messages: list[MailMessage] = []

    async def send(self, message: MailMessage) -> MailDeliveryReceipt:
        self.messages.append(message)
        if self.failure is not None:
            raise self.failure
        return MailDeliveryReceipt("smtp", "provider-id")


def test_notification_uses_the_existing_amie_smtp_environment_names(monkeypatch) -> None:
    monkeypatch.setenv("MABEL_ENVIRONMENT", "development")
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USERNAME", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("SMTP_FROM", "MABEL <mabel@example.test>")
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    monkeypatch.setenv("MABEL_NOTIFICATION_TO", "owner@example.test")

    settings = Settings.from_environment()

    assert (settings.email_backend, settings.smtp_host, settings.smtp_port) == (
        "smtp", "smtp.example.test", 2525,
    )
    assert settings.notification_recipient == "owner@example.test"
    assert settings.smtp_starttls is False


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"message": "ready", "severity": "urgent"},
        {"message": "ready", "recipient": "attacker@example.test"},
        {"message": "ready", "smtp_password": "not-allowed"},
        {"message": "ready", "title": "MABEL\r\nBcc: attacker@example.test"},
        {"message": "ready", "metadata": {"bad": {1, 2}}},
    ],
)
async def test_notification_arguments_are_strict_and_never_accept_transport_fields(arguments) -> None:
    service = NotificationService(RecordingDelivery(), "owner@example.test")
    with pytest.raises(NotificationArgumentsError):
        await service.send(arguments)


async def test_notification_uses_the_trusted_mail_path_with_fixed_destination() -> None:
    delivery = RecordingDelivery()
    service = NotificationService(delivery, "owner@example.test")

    result = await service.send({
        "title": "Workflow complete", "message": "Done.", "severity": "success",
        "event_type": "workflow.completed", "metadata": {"run": 4},
    })

    assert result.as_dict() == {
        "delivered": True, "provider": "smtp", "external_id": "provider-id", "retryable": False,
    }
    assert delivery.messages == [MailMessage(
        "owner@example.test", "Workflow complete",
        'Done.\n\nSeverity: success\nEvent: workflow.completed\n\nMetadata:\n{\n  "run": 4\n}',
    )]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, {"delivered": False, "provider": "smtp", "retryable": False, "error": "notification_not_configured"}),
        (MailRejectedError("smtp credential: super-secret"), {"delivered": False, "provider": "smtp", "retryable": False, "error": "provider_rejected"}),
        (MailTimeoutError("smtp credential: super-secret"), {"delivered": False, "provider": "smtp", "retryable": True, "error": "provider_timeout"}),
        (MailUnavailableError("smtp credential: super-secret"), {"delivered": False, "provider": "smtp", "retryable": True, "error": "provider_unavailable"}),
        (RuntimeError("smtp credential: super-secret"), {"delivered": False, "provider": "smtp", "retryable": True, "error": "provider_unavailable"}),
    ],
)
async def test_notification_failures_are_bounded_and_do_not_leak_transport_details(failure, expected) -> None:
    recipient = None if failure is None else "owner@example.test"
    service = NotificationService(RecordingDelivery(failure), recipient)

    result = await service.send({"message": "A safe notification"})

    assert result.as_dict() == expected
    assert "super-secret" not in repr(result.as_dict())


async def test_smtp_delivery_reuses_starttls_login_and_sender_conventions(monkeypatch) -> None:
    calls: list[object] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def starttls(self):
            calls.append("starttls")

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["From"], message["To"], message["Subject"], message.get_content()))

    monkeypatch.setattr("mabel.services.mail.smtplib.SMTP", FakeSmtp)
    delivery = SmtpMailDelivery(
        backend="smtp", host="smtp.example.test", port=587, username="mailer",
        password="smtp-secret", from_address="MABEL <mabel@example.test>", starttls=True,
    )

    result = await delivery.send(MailMessage("owner@example.test", "Subject", "Body"))

    assert result == MailDeliveryReceipt("smtp")
    assert calls == [
        ("connect", "smtp.example.test", 587, 20), "starttls", ("login", "mailer", "smtp-secret"),
        ("send", "MABEL <mabel@example.test>", "owner@example.test", "Subject", "Body\n"),
    ]


def test_capability_child_environment_excludes_smtp_secrets(tmp_path: Path, monkeypatch) -> None:
    executor = tmp_path / "capabilities" / "echo" / "executor.py"
    executor.parent.mkdir(parents=True)
    executor.write_text(
        "import os\ndef inspect_environment():\n"
        "    return {'smtp_values': [value for key, value in os.environ.items() if key.startswith('SMTP_')]}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    capability = CapabilityDefinition(
        CapabilitySummary("echo", "Echo", PurePosixPath("capabilities/echo/capability.json"), "1.0"),
        PurePosixPath("capabilities/echo/executor.py"), "inspect_environment",
        PurePosixPath("capabilities/echo/tests"), {"input": "none", "output": "json_object"},
    )

    result = BoundedCapabilityRunner().run(SimpleNamespace(root=tmp_path), capability)

    assert result == {"smtp_values": []}
    assert os.environ["SMTP_PASSWORD"] == "smtp-secret"
