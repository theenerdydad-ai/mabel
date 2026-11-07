# Managed repository rules

`managed_repository` is owner-scoped live MABEL state for a registered user
MABEL context repository. Registration alone authorizes no Git execution.

- The initial supported combination is `provider: github` and
  `purpose: mabel_context_repository`.
- `provider_repository_id` is the provider's immutable identity and may never
  change after registration.
- `canonical_locator` and `default_branch` come only from a successful trusted
  provider verification of that same immutable identity.
- `credential_id` is an internal relationship to separately encrypted provider
  secret material. It must never be serialized to ordinary APIs, MCP, active
  intelligences, or executors.
- Registration and reconnect accept a GitHub fine-grained token only through
  trusted MABEL backend code. The token is verified before encryption and
  replacement, and is never returned after submission.
- Multiple context-repository records may exist for an owner. Capability
  discovery and execution require exactly one enabled, connected record.
