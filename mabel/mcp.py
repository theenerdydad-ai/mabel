"""Thin MCP adapter for MABEL's shared services."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from mabel.auth.models import ApiIdentity
from mabel.diagnostics import log_capability_diagnostic
from mabel.services.api_tokens import ApiTokenService, InvalidBearerTokenError
from mabel.services.configuration import ConfigurationService
from mabel.services.capabilities import CapabilityService, CapabilityServiceError, operation_failure
from mabel.services.health import HealthService
from mabel.services.context_bindings import ContextBindingCandidateService, candidate_failure


def create_mcp_server(
    health_service: HealthService,
    configuration_service: ConfigurationService,
    api_tokens: ApiTokenService,
    capabilities: CapabilityService,
    context_bindings: ContextBindingCandidateService | None = None,
) -> FastMCP:
    """Create MABEL's MCP server without duplicating service behavior."""
    mcp = FastMCP(
        "mabel",
        instructions="MABEL is an operational runtime. Use only its registered tools.",
    )

    @mcp.tool(name="get_health")
    async def get_health() -> dict[str, bool | str]:
        """Return the MABEL runtime's process-level health status."""
        _require_identity(api_tokens)
        return health_service.status()

    @mcp.tool(name="get_mabel_configuration")
    async def get_mabel_configuration() -> dict[str, object]:
        """Inspect MABEL's current validated manifest and operating instructions."""
        _require_identity(api_tokens)
        return configuration_service.load().as_dict()

    @mcp.tool(name="get_identity")
    async def get_identity() -> dict[str, str]:
        """Return the current authenticated MABEL account identity."""
        identity = _require_identity(api_tokens)
        return {"owner_id": identity.owner_id, "authentication": identity.authentication}

    @mcp.tool(name="get_mabel_manifest")
    async def get_mabel_manifest() -> dict[str, object]:
        """Return the authenticated owner's validated MABEL context manifest."""
        identity = _require_identity(api_tokens)
        return await _capability_call("get_mabel_manifest", capabilities.get_manifest, identity.owner_id)

    @mcp.tool(name="get_capability_index")
    async def get_capability_index() -> dict[str, object]:
        """Return declared capabilities from the authenticated owner's active context repository."""
        identity = _require_identity(api_tokens)
        return await _capability_call("get_capability_index", capabilities.get_capability_index, identity.owner_id)

    @mcp.tool(name="get_context_binding_candidate")
    async def get_context_binding_candidate() -> dict[str, object]:
        """Return non-secret proof material for an ACE portal binding ceremony.

        The authenticated MABEL bearer selects the owner. No account,
        repository, endpoint, or credential can be supplied by the caller.
        """
        identity = _require_identity(api_tokens)
        return candidate_failure(identity.owner_id, context_bindings)

    @mcp.tool(name="run_capability")
    async def run_capability(
        capability_id: str, arguments: object | None = None
    ) -> dict[str, object]:
        """Run a declared capability with an optional validated arguments object."""
        identity = _require_identity(api_tokens)
        try:
            return (await capabilities.run_capability(identity.owner_id, capability_id, arguments)).as_dict()
        except CapabilityServiceError as error:
            return operation_failure(error, capability_id).as_dict()

    return mcp


def _require_identity(api_tokens: ApiTokenService) -> ApiIdentity:
    """Resolve this MCP request through MABEL's shared bearer-token service."""
    authorization = get_http_headers(include={"authorization"}).get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise ToolError("Missing or invalid Authorization: Bearer <token> header.")
    try:
        return api_tokens.resolve_bearer_token(authorization[7:].strip())
    except InvalidBearerTokenError as error:
        raise ToolError("Missing or invalid Authorization: Bearer <token> header.") from error


async def _capability_call(operation: str, call: object, owner_id: str) -> dict[str, object]:
    try:
        return await call(owner_id)
    except CapabilityServiceError as error:
        log_capability_diagnostic(operation, "error_to_result_mapping", error)
        return operation_failure(error).as_dict()
