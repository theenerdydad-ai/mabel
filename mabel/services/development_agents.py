"""Closed, server-configured development-agent dispatch for workflow handlers."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from mabel.services.development_workspace import (
    DevelopmentWorkspace,
    DevelopmentWorkspaceError,
    DevelopmentWorkspaceService,
)


class DevelopmentAgentError(ValueError):
    """A stable failure for trusted workflow-handler callers."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


AgentStatus = Literal["success", "blocked", "failed", "resource_limited"]


@dataclass(frozen=True)
class DevelopmentAgentRequest:
    agent_class: str
    workspace: DevelopmentWorkspace
    base_revision: str
    brief: Mapping[str, object]
    acceptance: Mapping[str, object]
    execution_id: str


@dataclass(frozen=True)
class DevelopmentProviderResult:
    status: AgentStatus
    validation: Mapping[str, object] | None = None
    assumptions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


@dataclass(frozen=True)
class DevelopmentAgentResult:
    status: AgentStatus
    evidence: dict[str, object]


class DevelopmentProvider(Protocol):
    async def execute(self, request: DevelopmentAgentRequest) -> DevelopmentProviderResult: ...


class DevelopmentAgentService:
    """Map one governed semantic class to one server-owned provider binding."""

    _SUPPORTED_CLASS = "code_development"

    def __init__(self, provider: DevelopmentProvider, workspaces: DevelopmentWorkspaceService) -> None:
        self._provider = provider
        self._workspaces = workspaces

    async def run(self, request: DevelopmentAgentRequest) -> DevelopmentAgentResult:
        if request.agent_class != self._SUPPORTED_CLASS:
            raise DevelopmentAgentError("unknown_agent_class")
        if request.workspace.base_revision != request.base_revision:
            raise DevelopmentAgentError("workspace_base_revision_mismatch")
        if not isinstance(request.brief, Mapping) or not isinstance(request.acceptance, Mapping):
            raise DevelopmentAgentError("invalid_development_request")
        try:
            provider_result = await self._provider.execute(request)
        except DevelopmentAgentError:
            raise
        except Exception as error:
            raise DevelopmentAgentError("provider_failed") from error
        if not isinstance(provider_result, DevelopmentProviderResult):
            raise DevelopmentAgentError("invalid_provider_result")
        if provider_result.status not in {"success", "blocked", "failed", "resource_limited"}:
            raise DevelopmentAgentError("invalid_provider_result")
        if provider_result.status != "success":
            return DevelopmentAgentResult(
                provider_result.status,
                _evidence(request, provider_result, {
                    "changed_files": [], "patch": "", "proposed_files": [],
                }),
            )
        try:
            changes = self._workspaces.collect_changes(request.workspace)
        except DevelopmentWorkspaceError as error:
            raise DevelopmentAgentError(error.code) from error
        return DevelopmentAgentResult(provider_result.status, _evidence(
            request, provider_result, changes.as_evidence()
        ))


class CodexCliDevelopmentProvider:
    """A fixed noninteractive Codex binding; callers never control its argv."""

    _TIMEOUT_SECONDS = 20 * 60
    _MAX_PROMPT_BYTES = 48 * 1024
    _MAX_OUTPUT_BYTES = 128 * 1024

    def __init__(self, executable: str, model: str | None = None, codex_home: Path | None = None) -> None:
        if not executable.strip():
            raise ValueError("Development-agent executable is required.")
        self._executable = executable
        self._model = model.strip() if model is not None and model.strip() else None
        self._codex_home = codex_home.resolve() if codex_home is not None else None

    async def execute(self, request: DevelopmentAgentRequest) -> DevelopmentProviderResult:
        prompt = self._prompt(request)

        with tempfile.TemporaryDirectory(prefix="mabel-development-agent-") as temporary:
            final_message = Path(temporary) / "final-message.txt"

            arguments = [
                self._executable,
                "exec",
                "--sandbox",
                "workspace-write",
                "--ephemeral",
                "--output-last-message",
                str(final_message),
                "-C",
                str(request.workspace.root),
                "-",
            ]

            if self._model is not None:
                arguments[2:2] = ["--model", self._model]

            try:
                status, elapsed_ms = _run_bounded(
                    arguments,
                    prompt,
                    request.workspace.root,
                    self._TIMEOUT_SECONDS,
                    self._MAX_OUTPUT_BYTES,
                    self._codex_home,
                )
            except TimeoutError:
                return DevelopmentProviderResult(
                    "resource_limited",
                    validation={
                        "provider": "codex_cli",
                        "reason": "timeout",
                    },
                )
            except _OutputLimitError:
                return DevelopmentProviderResult(
                    "resource_limited",
                    validation={
                        "provider": "codex_cli",
                        "reason": "output_limit",
                    },
                )
            except OSError:
                return DevelopmentProviderResult(
                    "failed",
                    validation={
                        "provider": "codex_cli",
                        "reason": "unavailable",
                    },
                )

            message = ""
            try:
                message = final_message.read_text(encoding="utf-8")[:4096]
            except OSError:
                pass

            if status != 0:
                return DevelopmentProviderResult(
                    "failed",
                    validation={
                        "provider": "codex_cli",
                        "exit_code": status,
                        "elapsed_ms": elapsed_ms,
                        "final_message": message,
                    },
                )

            return DevelopmentProviderResult(
                "success",
                validation={
                    "provider": "codex_cli",
                    "elapsed_ms": elapsed_ms,
                    "final_message": message,
                },
            )

    def _prompt(self, request: DevelopmentAgentRequest) -> bytes:
        # The provider receives semantic data, not a caller-supplied shell
        # command or model/provider control plane.
        document = {
            "task": "Perform the governed development work in this workspace only.",
            "base_revision": request.base_revision,
            "allowed_paths": [str(path) for path in request.workspace.allowed_paths],
            "brief": dict(request.brief),
            "acceptance": dict(request.acceptance),
            "instructions": [
                "Do not commit, push, or alter Git remotes.",
                "Change only allowed paths.",
                "Run only bounded, relevant validation.",
                "Return a concise implementation summary.",
            ],
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._MAX_PROMPT_BYTES:
            raise DevelopmentAgentError("development_brief_too_large")
        return encoded


class _OutputLimitError(RuntimeError):
    pass


def _evidence(
    request: DevelopmentAgentRequest,
    provider_result: DevelopmentProviderResult,
    changes: dict[str, object],
) -> dict[str, object]:
    return {
        "base_revision": request.base_revision,
        "workspace_id": request.workspace.workspace_id,
        "changes": changes,
        "validation": _safe_mapping(provider_result.validation or {}),
        "assumptions": _safe_strings(provider_result.assumptions),
        "blockers": _safe_strings(provider_result.blockers),
        "risks": _safe_strings(provider_result.risks),
    }


_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(token|secret|credential|password|api[_-]?key)\b\s*([:=])\s*[^\s,;]+"
)


def _safe_mapping(value: Mapping[str, object], depth: int = 0) -> dict[str, object]:
    """Keep structured diagnostics bounded and redact provider configuration."""
    if depth > 3:
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        normalized = str(key)
        if any(marker in normalized.lower() for marker in ("secret", "token", "credential", "password", "endpoint")):
            result[normalized] = "[redacted]"
        elif isinstance(item, Mapping):
            result[normalized] = _safe_mapping(item, depth + 1)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            maximum = 4096 if normalized == "final_message" else 2048
            result[normalized] = _safe_text(item, maximum) if isinstance(item, str) else item
        elif isinstance(item, (list, tuple)):
            result[normalized] = [str(entry)[:512] for entry in item[:32]]
    return result


def _safe_text(value: str, maximum: int) -> str:
    return _SENSITIVE_VALUE.sub(r"\1\2[redacted]", value)[:maximum]


def _safe_strings(values: tuple[str, ...]) -> list[str]:
    return [str(value)[:1024] for value in values[:32]]


def _run_bounded(
    arguments: list[str], prompt: bytes, workspace: Path, timeout: float, maximum: int, codex_home: Path | None
) -> tuple[int, int]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", ""),
    }
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home)
    process = subprocess.Popen(
        arguments,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Codex authentication remains in its server-configured environment;
        # workflow input is never permitted to add or override variables.
        env=environment,
        start_new_session=True,
    )
    assert process.stdin is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass
    streams = [process.stdout, process.stderr]
    assert all(stream is not None for stream in streams)
    captured = 0
    exceeded = threading.Event()

    def drain(stream: object) -> None:
        nonlocal captured
        reader = stream
        while True:
            chunk = reader.read(4096)
            if not chunk:
                return
            captured += len(chunk)
            if captured > maximum:
                exceeded.set()

    threads = [threading.Thread(target=drain, args=(stream,), daemon=True) for stream in streams]
    for thread in threads:
        thread.start()
    started = time.monotonic()
    while process.poll() is None:
        if exceeded.is_set():
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise _OutputLimitError()
        if time.monotonic() - started >= timeout:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise TimeoutError()
        time.sleep(0.02)
    for thread in threads:
        thread.join(timeout=1)
    return process.returncode, int((time.monotonic() - started) * 1000)
