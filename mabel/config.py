"""Small, explicit configuration for running MABEL locally."""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8100
    state_database_path: Path = Path(".mabel/state.db")
    environment: str = "development"
    development_owner_id: str | None = "local-development"
    # This is deliberately a server secret rather than a user-supplied Fernet
    # key.  Repository credentials derive their encryption key from it, just as
    # ACE Gateway derives ConnectionCredential encryption from PORTAL_SECRET.
    server_secret: str = "mabel-local-development-server-secret-change-before-production"
    context_projection_root: Path = Path(".mabel/context-repositories")
    # Development automation is disabled until an operator deliberately
    # configures its server-owned provider binding.
    development_workspace_root: Path = Path(".mabel/development-workspaces")
    development_agent_enabled: bool = False
    development_agent_provider: str = "codex_cli"
    development_agent_executable: str = "codex"
    development_agent_model: str | None = None
    development_agent_codex_home: Path | None = None
    browser_auth_enabled: bool = False
    amie_public_url: str | None = None
    amie_portal_url: str | None = None
    amie_internal_url: str | None = None
    public_url: str | None = None
    amie_mabel_internal_secret: str | None = None
    portal_cookie_secure: bool = False
    amie_owner_bindings: dict[str, str] | None = None
    # These intentionally use the same environment names as AMIE's existing
    # account-email delivery. They are server-owned and never capability input.
    email_backend: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    notification_recipient: str | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        host = os.environ.get("MABEL_HOST", cls.host)
        raw_port = os.environ.get("MABEL_PORT", str(cls.port))
        try:
            port = int(raw_port)
        except ValueError as error:
            raise ValueError("MABEL_PORT must be an integer.") from error
        if not 1 <= port <= 65535:
            raise ValueError("MABEL_PORT must be between 1 and 65535.")
        environment = os.environ.get("MABEL_ENVIRONMENT", cls.environment).strip().lower()
        if environment not in {"development", "production"}:
            raise ValueError("MABEL_ENVIRONMENT must be development or production.")
        raw_owner_id = os.environ.get("MABEL_DEVELOPMENT_OWNER_ID")
        if environment == "production":
            if raw_owner_id:
                raise ValueError("MABEL_DEVELOPMENT_OWNER_ID cannot be used in production.")
            development_owner_id = None
        else:
            development_owner_id = raw_owner_id or cls.development_owner_id
        raw_server_secret = os.environ.get("MABEL_SERVER_SECRET")
        if environment == "production" and not raw_server_secret:
            raise ValueError("MABEL_SERVER_SECRET is required in production.")
        server_secret = raw_server_secret or cls.server_secret
        if len(server_secret) < 32:
            raise ValueError("MABEL_SERVER_SECRET must be at least 32 characters long.")
        browser_auth_enabled = os.environ.get(
            "MABEL_BROWSER_AUTH_ENABLED", "true" if environment == "production" else "false"
        ).strip().lower() in {"1", "true", "yes"}
        raw_bindings = os.environ.get("MABEL_AMIE_OWNER_BINDINGS", "{}")
        try:
            bindings = json.loads(raw_bindings)
        except json.JSONDecodeError as error:
            raise ValueError("MABEL_AMIE_OWNER_BINDINGS must be a JSON object.") from error
        if not isinstance(bindings, dict) or not all(
            isinstance(subject, str) and isinstance(owner_id, str)
            for subject, owner_id in bindings.items()
        ):
            raise ValueError("MABEL_AMIE_OWNER_BINDINGS must map AMIE UUID strings to MABEL owner IDs.")
        amie_public_url = os.environ.get("MABEL_AMIE_PUBLIC_URL", "").strip().rstrip("/") or None
        # This is a browser navigation URL, not a base used for path joins.
        # Preserve a configured trailing slash such as `/ace/app/`.
        amie_portal_url = os.environ.get("MABEL_AMIE_PORTAL_URL", "").strip() or amie_public_url
        amie_internal_url = os.environ.get("MABEL_AMIE_INTERNAL_URL", "").strip().rstrip("/") or None
        public_url = os.environ.get("MABEL_PUBLIC_URL", "").strip().rstrip("/") or None
        internal_secret = os.environ.get("MABEL_AMIE_INTERNAL_SECRET")
        development_agent_enabled = os.environ.get("MABEL_DEVELOPMENT_AGENT_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
        development_agent_provider = os.environ.get("MABEL_DEVELOPMENT_AGENT_PROVIDER", "codex_cli").strip()
        if development_agent_provider != "codex_cli":
            raise ValueError("MABEL_DEVELOPMENT_AGENT_PROVIDER must be 'codex_cli'.")
        development_agent_executable = os.environ.get("MABEL_DEVELOPMENT_AGENT_EXECUTABLE", "codex").strip()
        if not development_agent_executable:
            raise ValueError("MABEL_DEVELOPMENT_AGENT_EXECUTABLE is required.")
        development_agent_model = os.environ.get("MABEL_DEVELOPMENT_AGENT_MODEL", "").strip() or None
        development_agent_codex_home = (
            Path(os.environ["MABEL_DEVELOPMENT_AGENT_CODEX_HOME"])
            if os.environ.get("MABEL_DEVELOPMENT_AGENT_CODEX_HOME", "").strip()
            else None
        )
        if environment == "production" and not browser_auth_enabled:
            raise ValueError("MABEL_BROWSER_AUTH_ENABLED must be enabled in production.")
        if browser_auth_enabled and not all((amie_public_url, amie_internal_url, public_url, internal_secret, bindings)):
            raise ValueError("Browser auth requires AMIE public/internal URLs, MABEL public URL, shared secret, and explicit AMIE owner bindings.")
        if browser_auth_enabled and internal_secret is not None and len(internal_secret) < 32:
            raise ValueError("MABEL_AMIE_INTERNAL_SECRET must be at least 32 characters long.")
        email_backend = os.environ.get(
            "EMAIL_BACKEND", "disabled" if environment == "production" else "console"
        ).strip().lower()
        if email_backend not in {"console", "disabled", "smtp"}:
            raise ValueError("EMAIL_BACKEND must be 'console', 'disabled', or 'smtp'.")
        smtp_host = os.environ.get("SMTP_HOST", "").strip()
        smtp_from = os.environ.get("SMTP_FROM", "").strip()
        try:
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        except ValueError as error:
            raise ValueError("SMTP_PORT must be an integer.") from error
        if not 1 <= smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535.")
        if email_backend == "smtp" and (not smtp_host or not smtp_from):
            raise ValueError("SMTP_HOST and SMTP_FROM are required when EMAIL_BACKEND=smtp.")
        return cls(
            host=host,
            port=port,
            state_database_path=Path(
                os.environ.get("MABEL_STATE_DATABASE_PATH", str(cls.state_database_path))
            ),
            environment=environment,
            development_owner_id=development_owner_id,
            server_secret=server_secret,
            context_projection_root=Path(
                os.environ.get("MABEL_CONTEXT_PROJECTION_ROOT", str(cls.context_projection_root))
            ),
            development_workspace_root=Path(
                os.environ.get("MABEL_DEVELOPMENT_WORKSPACE_ROOT", str(cls.development_workspace_root))
            ),
            development_agent_enabled=development_agent_enabled,
            development_agent_provider=development_agent_provider,
            development_agent_executable=development_agent_executable,
            development_agent_model=development_agent_model,
            development_agent_codex_home=development_agent_codex_home,
            browser_auth_enabled=browser_auth_enabled,
            amie_public_url=amie_public_url,
            amie_portal_url=amie_portal_url,
            amie_internal_url=amie_internal_url,
            public_url=public_url,
            amie_mabel_internal_secret=internal_secret,
            portal_cookie_secure=os.environ.get(
                "MABEL_PORTAL_COOKIE_SECURE", "true" if environment == "production" else "false"
            ).strip().lower() in {"1", "true", "yes"},
            amie_owner_bindings=bindings,
            email_backend=email_backend,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_username=os.environ.get("SMTP_USERNAME", "").strip(),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=smtp_from,
            smtp_starttls=os.environ.get("SMTP_STARTTLS", "true").strip().lower() == "true",
            notification_recipient=os.environ.get("MABEL_NOTIFICATION_TO", "").strip() or None,
        )
