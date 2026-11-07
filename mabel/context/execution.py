"""Bounded subprocess execution for the initial declared echo capability."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mabel.context.discovery import CapabilityContractError, validate_capability_contract
from mabel.context.models import CapabilityDefinition
from mabel.context.projection import ContextProjection
from mabel.repository import LocalRepository


class CapabilityExecutionError(RuntimeError):
    """A bounded, non-secret failure from a capability child process."""


@dataclass(frozen=True)
class CapabilityExecutionReport:
    """Bounded child-process evidence suitable for a trusted caller to shape."""

    status: str
    elapsed_ms: int
    result: dict[str, object] | None
    stdout: str
    stderr: str
    error: str | None = None


class BoundedCapabilityRunner:
    """Run declared code outside the Gateway process with a small, clean environment."""

    _TIMEOUT_SECONDS = 10
    _MAX_STREAM_BYTES = 64 * 1024

    def run(
        self,
        projection: ContextProjection,
        capability: CapabilityDefinition,
        arguments: dict[str, object] | None = None,
    ) -> dict[str, object]:
        report = self.run_report(projection, capability, arguments)
        if report.status != "succeeded" or report.result is None:
            raise CapabilityExecutionError(report.error or "Capability execution failed.")
        return report.result

    def run_report(
        self,
        projection: ContextProjection,
        capability: CapabilityDefinition,
        arguments: dict[str, object] | None = None,
    ) -> CapabilityExecutionReport:
        repository = LocalRepository(projection.root)
        executor_path = repository.resolve_path(capability.executor_path)
        workdir = repository.resolve_path(capability.executor_path.parent)
        runner_path = Path(__file__).with_name("runner.py")
        process = subprocess.Popen(
            [sys.executable, "-I", str(runner_path), str(executor_path), capability.callable_name],
            cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(arguments, separators=(",", ":")).encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError:
            pass
        stdout, stderr, exceeded, timed_out, elapsed_ms = _collect_bounded_output(
            process, self._TIMEOUT_SECONDS, self._MAX_STREAM_BYTES
        )
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if timed_out:
            return CapabilityExecutionReport(
                "timed_out", elapsed_ms, None, stdout_text, stderr_text,
                "Capability execution timed out.",
            )
        if exceeded:
            return CapabilityExecutionReport(
                "failed", elapsed_ms, None, stdout_text, stderr_text,
                "Capability execution exceeded its output limit.",
            )
        if process.returncode != 0:
            return CapabilityExecutionReport(
                "failed", elapsed_ms, None, stdout_text, stderr_text,
                "Capability execution failed.",
            )
        try:
            result = json.loads(stdout_text)
        except json.JSONDecodeError:
            return CapabilityExecutionReport(
                "failed", elapsed_ms, None, stdout_text, stderr_text,
                "Capability did not return a JSON result.",
            )
        if not isinstance(result, dict):
            return CapabilityExecutionReport(
                "failed", elapsed_ms, None, stdout_text, stderr_text,
                "Capability result must be a JSON object.",
            )
        if capability.output_contract is not None:
            try:
                validate_capability_contract(capability.output_contract, result, "output_contract")
            except CapabilityContractError:
                return CapabilityExecutionReport(
                    "failed", elapsed_ms, None, stdout_text, stderr_text,
                    "Capability result violates its declared output contract.",
                )
        return CapabilityExecutionReport("succeeded", elapsed_ms, result, stdout_text, stderr_text)


def _collect_bounded_output(
    process: subprocess.Popen[bytes], timeout: float, maximum: int
) -> tuple[bytes, bytes, bool, bool, int]:
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()

    def drain(name: str, stream: object) -> None:
        reader = stream
        while True:
            chunk = reader.read(4096)
            if not chunk:
                return
            if len(buffers[name]) + len(chunk) > maximum:
                exceeded.set()
                continue
            buffers[name].extend(chunk)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    started_at = time.monotonic()
    timed_out = False
    while process.poll() is None:
        if exceeded.is_set() or time.monotonic() - started_at >= timeout:
            timed_out = not exceeded.is_set()
            os.killpg(process.pid, signal.SIGKILL)
            break
        time.sleep(0.01)
    process.wait()
    for thread in threads:
        thread.join(timeout=1)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), exceeded.is_set(), timed_out, elapsed_ms
