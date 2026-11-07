# Connection rules

`connection` is generic, owner-scoped live MABEL state. It is not a durable
AMIE artifact and does not grant arbitrary repository access.

- `owner_id` is assigned by trusted MABEL ownership context; API callers do not
  prove or select it with a request field.
- `credential_reference` is an opaque secret-store reference. It must never
  contain a token, password, or credential value.
- A disabled connection cannot be tested or used to construct a reader.
- The first supported combination is `connection_type: amie` with
  `transport: mcp_streamable_http`. Other combinations must be rejected until
  MABEL registers a trusted implementation for them.
- Endpoint authentication is resolved only by trusted transport code for the
  lifetime of an invocation. It is not returned by MABEL APIs or MCP tools.
