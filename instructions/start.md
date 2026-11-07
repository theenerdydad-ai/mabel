# MABEL operating instructions

MABEL is the operational sibling of AMIE.

## AMIE

AMIE — Another Mundane Intelligence Environment — is a repository-backed
context and workflow system that gives an intelligence durable access to a
user's structured world.

AMIE stores and relates projects, manifests, instructions, workflows, sessions,
source material, generated artifacts, and other durable context independently
of any single intelligence or conversation.

AMIE workflows define coherent units of work: their goals, required context,
process, governance, and completion contracts.

AMIE makes the user's world portable between intelligences.

## MABEL

MABEL — Mundane Architecture, Boringly Effective Logic — provides the
operational environment in which governed work can be executed.

MABEL can receive work defined through AMIE, maintain execution state, provide
bounded capabilities, dispatch authorized operations, and preserve enough
durable state for work to continue or move between intelligences.

MABEL makes active work portable between intelligences.

## Ownership boundary

- AMIE owns the user's durable world, workflow meaning, source artifacts,
  relationships, and completion contracts.
- MABEL Gateway owns the operational platform, service behavior, live run
  state, authorization, capability discovery, and capability dispatch.
- A user's MABEL context repository owns that user's durable capability
  definitions, executor code, tests, and capability-specific contracts.
- An active intelligence provides reasoning but is not itself the durable
  environment or source of truth.
- MABEL does not redefine AMIE workflows. It operationalizes their execution.
- Do not treat an intelligence, generated code, or a Python virtual environment
  as a security boundary.
- Never expose provider credentials to an active intelligence or untrusted
  executor.
- Expose only capability and execution surfaces registered and authorized by
  MABEL.

## Repository boundary

This repository is the durable source of truth for MABEL Gateway identity,
instructions, core schemas/rules, and runtime implementation behavior. It is
not the source of truth for any user's capability definitions or executor code.

`manifest.json` is MABEL's root durable index. Read only the paths and nested
manifests it explicitly declares; do not infer repository structure from AMIE
or from filesystem layout.

User capability definitions, executable code, tests, and operational contracts
belong in the registered user MABEL context repository. Live user state,
credentials, execution state, and run history belong in MABEL's live database,
not either durable Gateway configuration file set.
