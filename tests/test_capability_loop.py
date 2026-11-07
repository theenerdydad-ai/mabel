import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from fastmcp import Client
from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings
from mabel.context.execution import BoundedCapabilityRunner
from mabel.context.projection import ContextProjectionService, GitHubGitCheckout
from mabel.mcp import create_mcp_server
from mabel.repository import LocalRepository
from mabel.repositories.credentials import ManagedRepositoryCredentialStore
from mabel.repositories.models import RepositoryVerification
from mabel.services.api_tokens import ApiTokenService
from mabel.services.capabilities import CapabilityService
from mabel.services.capability_controls import CapabilityControlService
from mabel.services.configuration import ConfigurationService
from mabel.services.health import HealthService
from mabel.services.managed_repositories import ManagedRepositoryService
from mabel.services.mail import MailDeliveryReceipt, MailMessage
from mabel.services.notifications import NotificationService
from mabel.state.api_tokens import SqliteApiTokenStore
from mabel.state.database import SqliteDatabase
from mabel.state.capability_controls import SqliteCapabilityControlStore
from mabel.state.managed_repositories import SqliteManagedRepositoryStore
from mabel.state.projections import SqliteRepositoryProjectionStore


@dataclass
class FakeGitHub:
    verification: RepositoryVerification = RepositoryVerification("123", "owner/mabel-context", "main")

    async def verify(self, locator, credential):
        return self.verification


class RecordingMailDelivery:
    def __init__(self) -> None:
        self.messages: list[MailMessage] = []

    async def send(self, message: MailMessage) -> MailDeliveryReceipt:
        self.messages.append(message)
        return MailDeliveryReceipt(provider="smtp", external_id="message-123")


def _write_context_repository(path: Path) -> None:
    files = {
        "manifest.json": '''{
  "schema_version": 1,
  "system": {"role": "user_operational_context"},
  "instructions": {"start": "instructions/start.md"},
  "schemas": {"manifest": "schemas/manifest.json"},
  "capabilities": {"manifest": "capabilities/manifest.json"}
}''',
        "instructions/start.md": "# Context instructions\n",
        "schemas/manifest.json": '''{
  "schema_version": 1,
  "schemas": [{"id": "capability", "path": "schemas/capability.schema.json"}],
  "rules": [{"id": "capability", "path": "schemas/capability.rules.md"}]
}''',
        "schemas/capability.schema.json": "{}\n",
        "schemas/capability.rules.md": "# Capability rules\n",
        "capabilities/manifest.json": '''{
  "schema_version": 1,
  "capabilities": [
    {"id": "echo", "title": "Echo", "content_type": "capability", "path": "capabilities/echo/capability.json", "version": "1.0"},
    {"id": "send_notification", "title": "Send Notification", "content_type": "capability", "path": "capabilities/send_notification/capability.json", "version": "1.0"}
  ]
}''',
        "capabilities/echo/capability.json": '''{
  "schema_version": 1,
  "id": "echo",
  "executor": {"runtime": "python_callable_v1", "path": "capabilities/echo/executor.py", "callable": "echo"},
  "tests_path": "capabilities/echo/tests",
  "contract": {"input": "none", "output": "json_object"}
}''',
        "capabilities/echo/executor.py": 'def echo():\n    return {"message": "hello world"}\n',
        "capabilities/echo/tests/test_echo.py": "def test_echo():\n    assert True\n",
        "capabilities/send_notification/capability.json": '''{
  "schema_version": 1,
  "id": "send_notification",
  "executor": {"runtime": "gateway_primitive_v1", "primitive": "notification_delivery"},
  "contract": {"input": "notification_v1", "output": "notification_delivery_v1"}
}''',
    }
    for relative_path, content in files.items():
        target = path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _git(arguments: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


async def test_authenticated_owner_syncs_a_commit_discovers_and_runs_echo(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_context_repository(source)
    _git(["init", "-b", "main"], source)
    _git(["config", "user.email", "test@example.invalid"], source)
    _git(["config", "user.name", "Test"], source)
    _git(["add", "."], source)
    _git(["commit", "-m", "context"], source)
    commit = _git(["rev-parse", "HEAD"], source)
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["remote", "add", "origin", str(remote)], source)
    _git(["push", "origin", "main"], source)

    database_path = tmp_path / "state.db"
    database = SqliteDatabase(database_path)
    database.initialize()
    credentials = ManagedRepositoryCredentialStore(database, Fernet.generate_key().decode("utf-8"))
    repositories = ManagedRepositoryService(
        SqliteManagedRepositoryStore(database), credentials, FakeGitHub()
    )
    await repositories.register(
        owner_id="test-owner", display_name="Context", provider="github",
        purpose="mabel_context_repository", repository="owner/mabel-context", github_token="token",
    )
    delivery = RecordingMailDelivery()
    projections = ContextProjectionService(
        repositories, credentials, GitHubGitCheckout(lambda _: str(remote)),
        tmp_path / "projections", SqliteRepositoryProjectionStore(database),
    )
    notifications = NotificationService(delivery, "owner@example.test")
    controls = CapabilityControlService(projections, SqliteCapabilityControlStore(database), notifications)
    capabilities = CapabilityService(projections, BoundedCapabilityRunner(), notifications, controls=controls)
    app = create_app(
        managed_repository_service=repositories,
        capability_service=capabilities,
        settings=Settings(state_database_path=database_path, development_owner_id="test-owner"),
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            token = (await client.post("/api/access/token")).json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            manifest = await client.get("/api/machine/mabel-manifest", headers=headers)
            index = await client.get("/api/machine/capability-index", headers=headers)
            run = await client.post("/api/machine/capabilities/echo/run", headers=headers)
            notification = await client.post(
                "/api/machine/capabilities/send_notification/run",
                headers=headers,
                json={"arguments": {
                    "title": "Workflow complete", "message": "The workflow finished.",
                    "severity": "success", "event_type": "workflow.completed", "metadata": {"run": 7},
                }},
            )
            invalid_notification = await client.post(
                "/api/machine/capabilities/send_notification/run",
                headers=headers,
                json={"arguments": {"message": "Nope", "recipient": "attacker@example.test"}},
            )
            malformed_notification = await client.post(
                "/api/machine/capabilities/send_notification/run",
                headers=headers,
                json={"arguments": "smtp-secret-that-must-not-be-reflected"},
            )

    api_tokens = ApiTokenService(SqliteApiTokenStore(database))
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {token}"},
    )
    mcp = create_mcp_server(
        HealthService(), ConfigurationService(LocalRepository(source)), api_tokens, capabilities
    )
    async with Client(mcp) as client:
        mcp_run = await client.call_tool("run_capability", {"capability_id": "echo"})
        mcp_notification = await client.call_tool("run_capability", {
            "capability_id": "send_notification",
            "arguments": {
                "message": "MABEL send_notification verification from AMIE",
                "title": "MABEL capability test",
                "severity": "info",
                "event_type": "capability.verification",
            },
        })

    assert manifest.status_code == 200
    assert manifest.json()["repository_commit"] == commit
    assert index.json() == {
        "repository_commit": commit,
        "capabilities": [
            {
                "id": "echo", "title": "Echo", "path": "capabilities/echo/capability.json", "version": "1.0",
                "input_contract": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
            {
                "id": "send_notification", "title": "Send Notification", "path": "capabilities/send_notification/capability.json", "version": "1.0",
                "input_contract": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"}, "title": {"type": "string"},
                        "severity": {"type": "string"}, "event_type": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["message"], "additionalProperties": False,
                },
            },
        ],
    }
    assert run.json() == {
        "ok": True, "capability_id": "echo", "repository_commit": commit,
        "result": {"message": "hello world"},
    }
    assert mcp_run.data == run.json()
    assert notification.json() == {
        "ok": True, "capability_id": "send_notification", "repository_commit": commit,
        "result": {"delivered": True, "provider": "smtp", "external_id": "message-123", "retryable": False},
    }
    assert mcp_notification.data["result"] == {
        "delivered": True, "provider": "smtp", "external_id": "message-123", "retryable": False,
    }
    assert [message.to_address for message in delivery.messages] == ["owner@example.test", "owner@example.test"]
    assert invalid_notification.status_code == 200
    assert invalid_notification.json() == {
        "ok": False,
        "capability_id": "send_notification",
        "error": {"code": "invalid_arguments"},
    }
    assert malformed_notification.status_code == 200
    assert malformed_notification.json() == {
        "ok": False,
        "capability_id": "send_notification",
        "error": {"code": "invalid_arguments"},
    }
