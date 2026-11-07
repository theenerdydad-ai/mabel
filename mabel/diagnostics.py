"""Temporary, secret-safe diagnostics for MABEL context capability discovery.

Remove this module and its callers after the live projection failure is
identified.  Diagnostics are deliberately server-side only and must never
render into MCP or HTTP responses.
"""

import logging
import re
from pathlib import Path


logger = logging.getLogger(__name__)

_BEARER = re.compile(r"(?i)(bearer\s+)[^\s,;]+")
_URL_CREDENTIALS = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@")
_NAMED_SECRET = re.compile(
    r"(?i)\b(token|secret|password|authorization|credential)\b\s*([=:])\s*[^\s,;]+"
)
_KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|mabel_[A-Za-z0-9_-]+|ace_[A-Za-z0-9_-]+)\b")


def sanitized_exception_message(error: BaseException) -> str:
    """Bound an exception message after removing common credential forms."""
    message = str(error).replace("\n", " ").replace("\r", " ")
    message = _BEARER.sub(r"\1<redacted>", message)
    message = _URL_CREDENTIALS.sub(r"\1<redacted>@", message)
    message = _NAMED_SECRET.sub(r"\1\2<redacted>", message)
    message = _KNOWN_TOKEN.sub("<redacted>", message)
    return message[:600] or "<no message>"


def log_capability_diagnostic(
    operation: str,
    stage: str,
    error: BaseException | None = None,
    *,
    commit_sha: str | None = None,
    context_root: Path | None = None,
) -> None:
    """Emit one focused journald record without credentials or request data."""
    logger.warning(
        "mabel_capability_diagnostic operation=%s stage=%s exception_class=%s "
        "exception_message=%r projected_commit=%s projected_root=%s",
        operation,
        stage,
        type(error).__name__ if error is not None else "none",
        sanitized_exception_message(error) if error is not None else "none",
        commit_sha or "unknown",
        str(context_root) if context_root is not None else "unknown",
    )
