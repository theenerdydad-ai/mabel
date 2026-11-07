from pathlib import Path, PurePosixPath

import pytest

from mabel.repository import LocalRepository, RepositoryPathError
from mabel.services.configuration import ConfigurationError, ConfigurationService
from mabel.config import Settings


def write_repository(tmp_path: Path, manifest: str, start_instruction: str = "# Start\n") -> None:
    (tmp_path / "instructions").mkdir()
    (tmp_path / "manifest.json").write_text(manifest, encoding="utf-8")
    (tmp_path / "instructions" / "start.md").write_text(start_instruction, encoding="utf-8")


def test_loads_manifest_and_declared_operating_instruction(tmp_path: Path) -> None:
    write_repository(
        tmp_path,
        """{
  "schema_version": 1,
  "system": {"name": "MABEL", "expansion": "Mundane Architecture, Boringly Effective Logic", "role": "operational_runtime"},
  "instructions": {"start": "instructions/start.md"},
  "schemas": {"path": "schemas", "manifest": "schemas/manifest.json"}
}""",
    )

    configuration = ConfigurationService(LocalRepository(tmp_path)).load()

    assert configuration.manifest.schema_version == 1
    assert configuration.manifest.system.name == "MABEL"
    assert configuration.start_instruction.path.as_posix() == "instructions/start.md"
    assert configuration.manifest.schema_registry.manifest_path.as_posix() == "schemas/manifest.json"
    assert configuration.start_instruction.content == "# Start\n"


@pytest.mark.parametrize(
    "manifest, error_message",
    [
        (
            """{
  "schema_version": 2,
  "system": {"name": "MABEL", "expansion": "Mundane Architecture, Boringly Effective Logic", "role": "operational_runtime"},
  "instructions": {"start": "instructions/start.md"},
  "schemas": {"path": "schemas", "manifest": "schemas/manifest.json"}
}""",
            "schema_version",
        ),
        (
            """{
  "schema_version": 1,
  "system": {"name": "MABEL", "expansion": "Mundane Architecture, Boringly Effective Logic", "role": "operational_runtime"},
  "instructions": {"start": "../outside.md"},
  "schemas": {"path": "schemas", "manifest": "schemas/manifest.json"}
}""",
            "repository-relative",
        ),
    ],
)
def test_rejects_invalid_manifest(tmp_path: Path, manifest: str, error_message: str) -> None:
    write_repository(tmp_path, manifest)

    with pytest.raises(ConfigurationError, match=error_message):
        ConfigurationService(LocalRepository(tmp_path)).load()


def test_repository_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(RepositoryPathError, match="inside the repository"):
        LocalRepository(tmp_path).read_text(PurePosixPath("../outside.md"))


def test_production_requires_server_secret_and_browser_auth_configuration(monkeypatch) -> None:
    monkeypatch.setenv("MABEL_ENVIRONMENT", "production")
    monkeypatch.delenv("MABEL_SERVER_SECRET", raising=False)

    with pytest.raises(ValueError, match="MABEL_SERVER_SECRET is required"):
        Settings.from_environment()

    monkeypatch.setenv("MABEL_SERVER_SECRET", "mabel-production-server-secret-at-least-32-characters")
    with pytest.raises(ValueError, match="Browser auth requires"):
        Settings.from_environment()
    monkeypatch.setenv("MABEL_AMIE_PUBLIC_URL", "https://amie.example")
    monkeypatch.setenv("MABEL_AMIE_PORTAL_URL", "https://amie.example/ace/app/")
    monkeypatch.setenv("MABEL_AMIE_INTERNAL_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("MABEL_PUBLIC_URL", "https://mabel.example")
    monkeypatch.setenv("MABEL_AMIE_INTERNAL_SECRET", "shared-stack-secret-at-least-32-characters")
    monkeypatch.setenv("MABEL_AMIE_OWNER_BINDINGS", '{"00000000-0000-0000-0000-000000000001":"production-owner"}')
    settings = Settings.from_environment()
    assert settings.server_secret.startswith("mabel-production")
    assert settings.amie_portal_url == "https://amie.example/ace/app/"


def test_development_agent_binding_is_server_configured_and_closed(monkeypatch) -> None:
    monkeypatch.setenv("MABEL_DEVELOPMENT_AGENT_ENABLED", "true")
    monkeypatch.setenv("MABEL_DEVELOPMENT_AGENT_PROVIDER", "codex_cli")
    monkeypatch.setenv("MABEL_DEVELOPMENT_AGENT_EXECUTABLE", "/srv/bin/codex")
    monkeypatch.setenv("MABEL_DEVELOPMENT_AGENT_MODEL", "server-model")

    settings = Settings.from_environment()

    assert settings.development_agent_enabled is True
    assert settings.development_agent_executable == "/srv/bin/codex"
    assert settings.development_agent_model == "server-model"
    monkeypatch.setenv("MABEL_DEVELOPMENT_AGENT_PROVIDER", "arbitrary-shell")
    with pytest.raises(ValueError, match="codex_cli"):
        Settings.from_environment()
