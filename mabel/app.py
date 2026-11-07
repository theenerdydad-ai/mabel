"""FastAPI application assembly for MABEL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans

from mabel.api.access import create_access_router
from mabel.api.authentication import create_bearer_identity_dependency
from mabel.api.capabilities import create_capability_router
from mabel.api.capability_controls import create_capability_control_router
from mabel.api.workflow_executions import create_workflow_execution_router
from mabel.api.connections import create_connection_router
from mabel.api.managed_repositories import create_managed_repository_router
from mabel.api.ownership import DevelopmentOwnerContext
from mabel.api.portal_auth import BrowserPortalAuth
from mabel.api.portal import mount_portal
from mabel.api.workspace import create_workspace_router
from mabel.config import Settings
from mabel.connections.credentials import AmieConnectionCredentialStore
from mabel.connections.readers import AmieMcpStreamableHttpReaderFactory
from mabel.mcp import create_mcp_server
from mabel.repository import LocalRepository
from mabel.services.configuration import ConfigurationService
from mabel.services.api_tokens import ApiTokenService
from mabel.services.connections import ConnectionService
from mabel.services.health import HealthService
from mabel.services.managed_repositories import ManagedRepositoryService
from mabel.state.connections import SqliteConnectionStore
from mabel.state.api_tokens import SqliteApiTokenStore
from mabel.state.database import SqliteDatabase
from mabel.state.managed_repositories import SqliteManagedRepositoryStore
from mabel.repositories.credentials import ManagedRepositoryCredentialStore
from mabel.repositories.github import GitHubRepositoryAdapter
from mabel.context.execution import BoundedCapabilityRunner
from mabel.context.projection import ContextProjectionService, GitHubGitCheckout
from mabel.services.capabilities import CapabilityService
from mabel.services.capability_controls import CapabilityControlService
from mabel.services.workspace import ContextWorkspaceService
from mabel.services.mail import SmtpMailDelivery
from mabel.services.notifications import NotificationService
from mabel.services.amie_context import AmieContextOperationService
from mabel.services.development_agents import CodexCliDevelopmentProvider, DevelopmentAgentService
from mabel.services.development_workspace import DevelopmentWorkspaceService, GitHubDevelopmentCheckout
from mabel.workflows.production import ProductionWorkflowHandlers, validate_create_capability_admission
from mabel.workflows.execution import WorkflowExecutionService
from mabel.workflows.capability import WorkflowExecutionCapability
from mabel.workflows.continuation import WorkflowContinuationService
from mabel.state.projections import SqliteRepositoryProjectionStore
from mabel.state.capability_controls import SqliteCapabilityControlStore
from mabel.state.workflow_executions import SqliteWorkflowExecutionStore
from mabel.services.workflow_operations import WorkflowExecutionOperationsService
from mabel.state.owner_identities import SqliteOwnerIdentityStore
from mabel.services.owner_identities import AmieOwnerMappingService
from mabel.services.context_bindings import ContextBindingCandidateService
from mabel.browser.amie_stack import AmieHttpStackClient, AmieStackClient


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize live-state storage before either front door accepts work."""
    app.state.database.initialize()
    if getattr(app.state, "owner_mappings", None) is not None:
        app.state.owner_mappings.configure()
    continuations = getattr(app.state, "workflow_continuations", None)
    if continuations is not None:
        continuations.start()
    try:
        yield
    finally:
        if continuations is not None:
            await asyncio.to_thread(continuations.shutdown)


def create_app(
    health_service: HealthService | None = None,
    configuration_service: ConfigurationService | None = None,
    connection_service: ConnectionService | None = None,
    managed_repository_service: ManagedRepositoryService | None = None,
    api_token_service: ApiTokenService | None = None,
    capability_service: CapabilityService | None = None,
    notification_service: NotificationService | None = None,
    workspace_service: ContextWorkspaceService | None = None,
    capability_control_service: CapabilityControlService | None = None,
    amie_stack_client: AmieStackClient | None = None,
    repository_root: Path | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Create MABEL's HTTP and MCP front doors over shared services."""
    runtime_settings = settings or Settings.from_environment()
    health = health_service or HealthService()
    configuration = configuration_service or ConfigurationService(
        LocalRepository(repository_root or Path.cwd())
    )
    database = SqliteDatabase(runtime_settings.state_database_path)
    amie_connection_credentials = AmieConnectionCredentialStore(
        database, runtime_settings.server_secret
    )
    connections = connection_service or ConnectionService(
        SqliteConnectionStore(database),
        AmieMcpStreamableHttpReaderFactory(amie_connection_credentials),
        amie_connection_credentials,
    )
    repository_credentials = ManagedRepositoryCredentialStore(
        database, runtime_settings.server_secret
    )
    managed_repositories = managed_repository_service or ManagedRepositoryService(
        SqliteManagedRepositoryStore(database),
        repository_credentials,
        GitHubRepositoryAdapter(),
    )
    api_tokens = api_token_service or ApiTokenService(SqliteApiTokenStore(database))
    projections = ContextProjectionService(
            managed_repositories,
            repository_credentials,
            GitHubGitCheckout(),
            runtime_settings.context_projection_root,
            SqliteRepositoryProjectionStore(database),
    )
    notifications = notification_service or NotificationService(
        SmtpMailDelivery(
            backend=runtime_settings.email_backend,
            host=runtime_settings.smtp_host,
            port=runtime_settings.smtp_port,
            username=runtime_settings.smtp_username,
            password=runtime_settings.smtp_password,
            from_address=runtime_settings.smtp_from,
            starttls=runtime_settings.smtp_starttls,
        ),
        runtime_settings.notification_recipient,
    )
    amie_context = AmieContextOperationService(connections, amie_connection_credentials)
    capability_controls = capability_control_service or CapabilityControlService(
        projections,
        SqliteCapabilityControlStore(database),
        notifications,
        amie_context,
    )
    capabilities = capability_service or CapabilityService(
        projections,
        BoundedCapabilityRunner(),
        notifications,
        amie_context,
        capability_controls,
    )
    workspace = workspace_service or ContextWorkspaceService(projections, capabilities)
    development_workspaces = DevelopmentWorkspaceService(
        managed_repositories,
        repository_credentials,
        GitHubDevelopmentCheckout(),
        runtime_settings.development_workspace_root,
        projection_root=runtime_settings.context_projection_root,
    )
    development_agents = (
        DevelopmentAgentService(
            CodexCliDevelopmentProvider(
                runtime_settings.development_agent_executable,
                runtime_settings.development_agent_model,
                runtime_settings.development_agent_codex_home,
            ),
            development_workspaces,
        )
        if runtime_settings.development_agent_enabled
        else None
    )
    workflow_handlers = ProductionWorkflowHandlers(
        projections, development_workspaces, development_agents, amie_context, capabilities
    ).handlers()
    workflow_execution_store = SqliteWorkflowExecutionStore(database)
    workflow_execution = WorkflowExecutionService(
        projections,
        workflow_execution_store,
        workflow_handlers,
        {"create_capability": validate_create_capability_admission},
    )
    workflow_continuations = WorkflowContinuationService(workflow_execution)
    workflow_operations = WorkflowExecutionOperationsService(workflow_execution_store)
    capabilities.configure_workflow_execution(
        WorkflowExecutionCapability(workflow_execution, workflow_continuations)
    )
    owner_mappings = AmieOwnerMappingService(
        SqliteOwnerIdentityStore(database), runtime_settings.amie_owner_bindings
    )
    browser_auth = None
    if runtime_settings.browser_auth_enabled:
        if not all((
            runtime_settings.amie_public_url, runtime_settings.public_url,
            runtime_settings.amie_internal_url, runtime_settings.amie_mabel_internal_secret,
        )):
            raise ValueError("Browser auth is enabled without its required configured URLs and secret.")
        stack_client = amie_stack_client or AmieHttpStackClient(
            runtime_settings.amie_internal_url, runtime_settings.amie_mabel_internal_secret
        )
        browser_auth = BrowserPortalAuth(
            amie_public_url=runtime_settings.amie_public_url,
            amie_portal_url=runtime_settings.amie_portal_url or runtime_settings.amie_public_url,
            mabel_public_url=runtime_settings.public_url,
            client=stack_client,
            mappings=owner_mappings,
            cookie_secure=runtime_settings.portal_cookie_secure,
        )
        owner_context = browser_auth
        require_csrf = browser_auth.require_csrf
    else:
        owner_context = DevelopmentOwnerContext(runtime_settings.development_owner_id)
        require_csrf = None
    require_bearer_identity = create_bearer_identity_dependency(api_tokens)
    context_bindings = ContextBindingCandidateService(managed_repositories)
    mcp = create_mcp_server(health, configuration, api_tokens, capabilities, context_bindings)
    mcp_app = mcp.http_app(path="/")

    app = FastAPI(
        title="MABEL",
        description="Mundane Architecture, Boringly Effective Logic.",
        lifespan=combine_lifespans(application_lifespan, mcp_app.lifespan),
    )
    app.state.database = database
    app.state.owner_mappings = owner_mappings
    app.state.workflow_continuations = workflow_continuations
    app.state.services = {
        "health": health,
        "configuration": configuration,
        "connections": connections,
        "managed_repositories": managed_repositories,
        "api_tokens": api_tokens,
        "capabilities": capabilities,
        "capability_controls": capability_controls,
        "notifications": notifications,
        "workspace": workspace,
        "development_workspaces": development_workspaces,
        "development_agents": development_agents,
        "workflow_handlers": workflow_handlers,
        "workflow_execution": workflow_execution,
        "workflow_continuations": workflow_continuations,
        "workflow_operations": workflow_operations,
    }
    app.state.mcp = mcp

    @app.get("/health", operation_id="get_health")
    async def health_check() -> dict[str, bool | str]:
        return health.status()

    app.mount("/mcp", mcp_app)
    app.include_router(create_connection_router(connections, owner_context, require_csrf))
    app.include_router(create_managed_repository_router(managed_repositories, owner_context, require_csrf))
    app.include_router(create_access_router(api_tokens, owner_context, require_bearer_identity, require_csrf))
    app.include_router(create_capability_router(capabilities, require_bearer_identity))
    app.include_router(create_capability_control_router(capability_controls, owner_context, require_csrf))
    app.include_router(create_workflow_execution_router(workflow_operations, owner_context))
    app.include_router(create_workspace_router(workspace, owner_context, require_csrf))
    mount_portal(app, browser_auth)
    return app
