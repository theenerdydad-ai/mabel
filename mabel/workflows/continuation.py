"""Process-local continuation ownership for accepted workflow executions."""

from __future__ import annotations

import asyncio
import threading
import time

from mabel.workflows.execution import WorkflowAdmission, WorkflowExecutionError, WorkflowExecutionService


class WorkflowContinuationError(RuntimeError):
    """An accepted workflow could not be handed to the local continuation runtime."""


class WorkflowContinuationService:
    """Run one accepted workflow outside the ASGI request event loop.

    This intentionally owns only in-process threads.  It is not a durable queue
    and does not recover work after a process exit.
    """

    def __init__(self, execution: WorkflowExecutionService) -> None:
        self._execution = execution
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._accepting = False

    def start(self) -> None:
        with self._lock:
            self._accepting = True

    def submit(self, admission: WorkflowAdmission) -> None:
        """Synchronously register a continuation before a caller is acknowledged."""
        execution_id = admission.execution.execution_id
        with self._lock:
            if not self._accepting or execution_id in self._threads:
                raise WorkflowContinuationError("Workflow continuation is unavailable.")
            thread = threading.Thread(
                target=self._run,
                args=(admission,),
                name=f"mabel-workflow-{execution_id}",
                daemon=True,
            )
            self._threads[execution_id] = thread
            try:
                thread.start()
            except Exception as error:
                self._threads.pop(execution_id, None)
                raise WorkflowContinuationError("Workflow continuation could not be scheduled.") from error

    def shutdown(self, timeout: float = 0.25) -> None:
        """Stop accepting work and join only for a bounded amount of time."""
        with self._lock:
            self._accepting = False
            threads = tuple(self._threads.values())
        deadline = time.monotonic() + max(timeout, 0.0)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    async def wait_for_idle(self, timeout: float = 1.0) -> bool:
        """Test-facing bounded wait; production status comes from persisted executions."""
        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._threads:
                    return True
            await asyncio.sleep(0.01)
        with self._lock:
            return not self._threads

    def _run(self, admission: WorkflowAdmission) -> None:
        try:
            asyncio.run(self._execution.advance_admission(admission))
        except WorkflowExecutionError as error:
            if error.code != "already_claimed":
                self._fail(admission, "continuation_failed")
        except Exception:
            self._fail(admission, "continuation_failed")
        finally:
            with self._lock:
                self._threads.pop(admission.execution.execution_id, None)

    def _fail(self, admission: WorkflowAdmission, code: str) -> None:
        try:
            self._execution.fail_continuation(
                admission.execution.owner_id, admission.execution.execution_id, code
            )
        except Exception:
            # The normal execution store remains the only status authority.
            pass
