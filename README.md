# MABEL

**Mundane Architecture, Boringly Effective Logic.**

MABEL is the operational sibling to AMIE. AMIE owns the user's durable world
and workflow meaning; MABEL provides the runtime surface that will eventually
operate within a bounded slice of that world.

This first milestone is intentionally small:

- a FastAPI application with `GET /health`;
- an embedded Streamable HTTP MCP server at `/mcp`;
- one shared `HealthService`, used by both front doors; and
- a validated repository-backed MABEL manifest and operating instructions; and
- owner-scoped registered AMIE connections backed by local SQLite state; and
- one owner-scoped API/MCP bearer token, stored only as a hash.

MABEL Gateway is runtime infrastructure. User-owned capability definitions,
executor code, tests, and contracts live in a separately registered Git-backed
MABEL context repository, never in this Gateway repository.

It deliberately does not include AMIE writes, source-repository access, Git
writes, branches/worktrees, autonomous workflow runs, or containerized
execution.

## Local development

Requires Python 3.10 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
uv run python -m mabel
```

The application binds to `127.0.0.1:8100` by default. Override either value
with `MABEL_HOST` or `MABEL_PORT` if needed.

```bash
curl http://127.0.0.1:8100/health
```

Expected response:

```json
{"ok": true, "service": "mabel"}
```

The MCP Streamable HTTP endpoint is `http://127.0.0.1:8100/mcp`. Every MCP
tool requires `Authorization: Bearer mabel_…`; it exposes `get_identity`,
`get_health`, Gateway `get_mabel_configuration`, user-context
`get_mabel_manifest`, `get_capability_index`, and `run_capability`.

## Registered connections

The local management API is available at `/api/connections`. It is deliberately
development-only: MABEL assigns the configured `MABEL_DEVELOPMENT_OWNER_ID`
(default: `local-development`) and does not accept owner IDs from requests.
This is not production authentication.

## Browser access through AMIE

In production, set `MABEL_BROWSER_AUTH_ENABLED=true`. MABEL does not hold a
username, password, 2FA secret, or AMIE browser cookie. It redirects to AMIE,
receives a short-lived single-use `mabel` handoff, stores only an opaque
host-only MABEL session reference, and introspects that reference against
AMIE on every protected request.

Required MABEL service environment:

```text
MABEL_BROWSER_AUTH_ENABLED=true
MABEL_PUBLIC_URL=https://mabel.example
# AMIE base used only to begin the server-authenticated stack handoff.
MABEL_AMIE_PUBLIC_URL=https://www.theenerdydad.com/ace
# AMIE workspace destination exposed by MABEL's authenticated app switcher.
MABEL_AMIE_PORTAL_URL=https://www.theenerdydad.com/ace/app/
MABEL_AMIE_INTERNAL_URL=http://127.0.0.1:8000
MABEL_AMIE_INTERNAL_SECRET=<same random value configured in AMIE>
MABEL_AMIE_OWNER_BINDINGS={"<immutable AMIE User.id>":"<existing MABEL owner_id>"}
MABEL_PORTAL_COOKIE_SECURE=true
```

`MABEL_AMIE_OWNER_BINDINGS` is an operator-controlled migration/configuration
step; a browser request cannot claim or select an owner. JSON portal APIs
return a JSON `401` with the configured MABEL login URL. Unsafe portal
requests require the host-only `mabel_csrf` double-submit header. Machine
`mabel_…` bearer tokens and MCP authentication are unchanged.

AMIE needs `MABEL_PUBLIC_URL`, `MABEL_PORTAL_URL`,
`AMIE_MABEL_INTERNAL_SECRET`, and optionally `MABEL_STACK_HANDOFF_SECONDS=60`.
Use separate AMIE and MABEL hosts (or otherwise separate host-only cookies);
never configure a shared cookie Domain. Caddy must deny AMIE's private stack
endpoints before public proxying, while MABEL calls AMIE over its loopback URL:

```caddy
handle /ace/_internal/* { respond 404 }
handle_path /ace/* { reverse_proxy 127.0.0.1:8000 { flush_interval -1 } }
mabel.example { reverse_proxy 127.0.0.1:8100 { flush_interval -1 } }
```

Local state defaults to `.mabel/state.db`, which is ignored by Git. Override
the path with `MABEL_STATE_DATABASE_PATH`. Set `MABEL_ENVIRONMENT=production`
to disable this development owner context; management then requires a future
authenticated owner integration.

## API and MCP access

The portal at `/portal` can generate or regenerate one API token for the
current owner. Its plaintext value is returned only in that generation
response; local state retains only a SHA-256 hash. Regeneration atomically
replaces the prior hash, invalidating the previous token immediately.

The same token authenticates machine-facing REST requests and MCP. For
example:

```bash
curl -H "Authorization: Bearer mabel_…" http://127.0.0.1:8100/api/machine/identity
```

The portal's current development owner context remains separate from bearer
authentication. Production browser/session authentication is intentionally not
part of this milestone.

AMIE connections may hold an encrypted per-owner `ace_…` AMIE API token in a
dedicated credential record. Connection metadata retains only an internal
relationship to that record; the portal and ordinary APIs return only whether
a token is configured. Trusted MABEL transport code decrypts it transiently
to send `Authorization: Bearer …` to AMIE MCP. Existing legacy opaque
credential references are cleared by the v6 migration and must be entered
again through the AMIE API token field.

MABEL context repositories use a separate encrypted GitHub-token record. Set a
stable `MABEL_SERVER_SECRET` (at least 32 characters) before production use.
MABEL derives the credential encryption key from that server secret, matching
ACE Gateway's `ConnectionCredential` approach. Do not rotate the secret while
encrypted repository credentials exist without a future credential-rotation
migration.

## User MABEL context repository

Register a GitHub repository with `purpose: mabel_context_repository`. Multiple
records can exist for an owner, but exactly one enabled, connected repository
must be active before discovery or execution. MABEL synchronizes that
repository to a detached commit under `MABEL_CONTEXT_PROJECTION_ROOT` (default:
`.mabel/context-repositories`) and validates its declared manifests before use.

The first supported capabilities are the declared `echo` Python callable and
the trusted `send_notification` primitive. `run_capability` accepts an optional
`arguments` object; `echo` still uses no arguments. `send_notification` accepts
only `title`, required `message`, `severity` (`info`, `success`, `warning`, or
`error`), optional stable `event_type`, and JSON `metadata`. Unknown fields,
recipients, provider endpoints, and credential fields are rejected.

`send_notification` is manifest-declared by the user context repository, but
the one SMTP attempt happens only in trusted Gateway code. Its fixed recipient
comes from the non-secret `MABEL_NOTIFICATION_TO` setting. It reuses AMIE's
existing SMTP deployment settings without a second credential store:

```text
EMAIL_BACKEND=smtp
SMTP_HOST=...
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM=...
SMTP_STARTTLS=true
MABEL_NOTIFICATION_TO=owner@example.com
```

`MABEL_NOTIFICATION_TO` is the only new setting. SMTP configuration remains
server configuration and is not supplied to, exposed to, or inherited by a
capability subprocess. A missing destination or SMTP setup produces the
bounded result error `notification_not_configured`; provider failures are
classified as `provider_rejected`, `provider_timeout`, or
`provider_unavailable`, with no provider response details.

Capability run failures that prevent execution use the same bounded response
on both MCP and `/api/machine`: `{"ok": false, "capability_id": "…",
"error": {"code": "invalid_arguments"}}`. Current codes are
`invalid_arguments`, `capability_unavailable`, and `execution_failed`.
Transport or bearer-authentication failures remain protocol/HTTP failures;
they are not represented as a capability result.

MABEL runs regular Python capabilities in a bounded child process with a clean
environment, explicit capability working directory, timeout, process-group
termination, and bounded JSON-only output. No MABEL, AMIE, database, GitHub,
or SMTP secret is passed to the child process.

## Tests

```bash
uv run pytest
```
