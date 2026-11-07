"""The small trusted notification primitive used by registered capabilities."""

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from mabel.services.mail import (
    MailDelivery,
    MailNotConfiguredError,
    MailRejectedError,
    MailTimeoutError,
    MailUnavailableError,
    MailMessage,
)


class NotificationArgumentsError(ValueError):
    """A stable, non-sensitive input-contract rejection."""

    code = "invalid_arguments"


@dataclass(frozen=True)
class NotificationArguments:
    title: str | None
    message: str
    severity: str
    event_type: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class NotificationResult:
    delivered: bool
    provider: str
    retryable: bool
    external_id: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "delivered": self.delivered,
            "provider": self.provider,
            "retryable": self.retryable,
        }
        if self.external_id is not None:
            result["external_id"] = self.external_id
        if self.error is not None:
            result["error"] = self.error
        return result


class NotificationService:
    """Validate content and make exactly one trusted delivery attempt."""

    _ALLOWED_KEYS = frozenset({"title", "message", "severity", "event_type", "metadata"})
    _SEVERITIES = frozenset({"info", "success", "warning", "error"})
    _EVENT_TYPE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
    _MAX_MESSAGE = 8_000
    _MAX_TITLE = 256
    _MAX_METADATA_BYTES = 16_384

    def __init__(self, delivery: MailDelivery, recipient: str | None) -> None:
        self._delivery = delivery
        self._recipient = recipient

    def readiness_error_code(self) -> str | None:
        """Return only the bounded prerequisite state needed for discovery.

        This is intentionally a preflight, not a delivery attempt: MABEL must
        never send a notification merely to decide whether it is discoverable.
        """
        if not self._recipient:
            return "notification_not_configured"
        configured = getattr(self._delivery, "is_configured", None)
        if callable(configured) and not configured():
            return "notification_not_configured"
        return None

    def validate_arguments(self, raw: Mapping[str, object] | None) -> NotificationArguments:
        if not isinstance(raw, Mapping) or set(raw) - self._ALLOWED_KEYS:
            raise NotificationArgumentsError()
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > self._MAX_MESSAGE:
            raise NotificationArgumentsError()
        title = raw.get("title")
        if title is not None and (
            not isinstance(title, str)
            or len(title) > self._MAX_TITLE
            or "\r" in title
            or "\n" in title
        ):
            raise NotificationArgumentsError()
        severity = raw.get("severity", "info")
        if severity not in self._SEVERITIES:
            raise NotificationArgumentsError()
        event_type = raw.get("event_type")
        if event_type is not None and (
            not isinstance(event_type, str) or not self._EVENT_TYPE.fullmatch(event_type)
        ):
            raise NotificationArgumentsError()
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping) or not all(isinstance(key, str) for key in metadata):
            raise NotificationArgumentsError()
        try:
            serialized_metadata = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise NotificationArgumentsError() from error
        if len(serialized_metadata.encode("utf-8")) > self._MAX_METADATA_BYTES:
            raise NotificationArgumentsError()
        return NotificationArguments(title, message, severity, event_type, dict(metadata))

    async def send(self, raw: Mapping[str, object] | None) -> NotificationResult:
        arguments = self.validate_arguments(raw)
        if not self._recipient:
            return NotificationResult(False, "smtp", False, error="notification_not_configured")
        try:
            receipt = await self._delivery.send(self._mail_message(arguments))
        except MailNotConfiguredError:
            return NotificationResult(False, "smtp", False, error="notification_not_configured")
        except MailRejectedError:
            return NotificationResult(False, "smtp", False, error="provider_rejected")
        except MailTimeoutError:
            return NotificationResult(False, "smtp", True, error="provider_timeout")
        except MailUnavailableError:
            return NotificationResult(False, "smtp", True, error="provider_unavailable")
        except Exception:
            return NotificationResult(False, "smtp", True, error="provider_unavailable")
        return NotificationResult(True, receipt.provider, False, external_id=receipt.external_id)

    def _mail_message(self, arguments: NotificationArguments) -> MailMessage:
        subject = arguments.title or "MABEL notification"
        lines = [arguments.message, "", f"Severity: {arguments.severity}"]
        if arguments.event_type:
            lines.append(f"Event: {arguments.event_type}")
        if arguments.metadata:
            lines.extend(("", "Metadata:", json.dumps(arguments.metadata, sort_keys=True, indent=2)))
        return MailMessage(self._recipient or "", subject, "\n".join(lines))
