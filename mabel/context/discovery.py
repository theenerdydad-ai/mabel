"""Manifest-first validation and discovery for user MABEL context repositories."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urldefrag, urlsplit

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing import Registry, Resource

from mabel.context.models import (
    CapabilityDefinition,
    CapabilitySummary,
    ContextManifest,
    LoadedContext,
    WorkflowDefinition,
    WorkflowSummary,
)
from mabel.context.workflow_plan import WorkflowRuntimePlanError, parse_workflow_runtime_plan
from mabel.repository import Repository, RepositoryError


class ContextRepositoryError(ValueError):
    """Raised when a user context repository is not a valid canonical MABEL context."""


class CapabilityContractError(ContextRepositoryError):
    """Raised when an invocation or result violates a declared capability contract."""


_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_-]*")
_VERSION = re.compile(r"[1-9][0-9]*\.[0-9]+")
_CALLABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NEW_REGISTRY_FORMAT = "1.0"
_AMIE_CONTEXT_OPERATIONS = frozenset({
    "get_manifest", "get_file", "create_draft", "get_draft", "commit_draft", "discard_draft",
})


@dataclass(frozen=True)
class SchemaPair:
    content_type: str
    version: str
    schema_path: PurePosixPath
    rules_path: PurePosixPath
    status: str


@dataclass(frozen=True)
class SchemaRegistry:
    pairs: dict[tuple[str, str], SchemaPair]
    current_versions: dict[str, str]
    schema_documents: dict[PurePosixPath, dict[str, Any]]
    governance_paths: tuple[PurePosixPath, ...]

    def resolve(self, content_type: str, version: str) -> SchemaPair:
        pair = self.pairs.get((content_type, version))
        if pair is None:
            raise ContextRepositoryError(
                f"Schema registry does not declare {content_type!r} version {version!r}."
            )
        if pair.status == "retired":
            raise ContextRepositoryError("Artifact references a retired schema version.")
        return pair


class MabelContextDiscovery:
    """Read only paths explicitly declared by supported context manifest generations."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def load(self) -> LoadedContext:
        manifest = self._load_root_manifest()
        if manifest.generation == "amie":
            return self._load_amie_context(manifest)
        self._repository.read_text(manifest.start_instruction_path)
        registry_document = self._read_json(manifest.schema_manifest_path)
        generation = self._detect_generation(registry_document)
        if generation == "legacy":
            self._validate_legacy_schema_registry(registry_document)
            capabilities = self._load_legacy_capability_index(manifest.capability_manifest_path)
            return LoadedContext(
                manifest=manifest,
                capabilities=tuple(capabilities),
                governance_paths=(
                    PurePosixPath("schemas/capability.schema.json"),
                    PurePosixPath("schemas/capability.rules.md"),
                ),
            )

        registry = self._load_schema_registry(registry_document)
        new_manifest = ContextManifest(
            manifest.start_instruction_path,
            manifest.schema_manifest_path,
            manifest.capability_manifest_path,
            manifest.raw,
            generation="registry_v1",
            workflow_manifest_path=manifest.workflow_manifest_path,
        )
        capabilities = self._load_new_capability_index(new_manifest.capability_manifest_path, registry)
        if new_manifest.workflow_manifest_path is None:
            raise ContextRepositoryError("Registry-v1 contexts must declare a workflow manifest.")
        try:
            workflows = self._load_new_workflow_index(new_manifest.workflow_manifest_path, registry)
            collection_errors: dict[str, str] | None = None
        except ContextRepositoryError as error:
            # Workflows are optional context guidance.  A broken collection must
            # not hide a valid capability registry.
            workflows = []
            collection_errors = {"workflows": str(error)}
        return LoadedContext(
            new_manifest, tuple(capabilities), registry.governance_paths, tuple(workflows), collection_errors
        )

    def load_capability(self, capability_id: str) -> CapabilityDefinition:
        context = self.load()
        summary = next((item for item in context.capabilities if item.id == capability_id), None)
        if summary is None:
            raise ContextRepositoryError("Capability is not declared by the context repository.")
        if context.manifest.generation == "legacy":
            return self._load_legacy_capability(summary)
        if context.manifest.generation == "amie":
            return self._load_amie_capability(summary)

        registry = self._load_schema_registry(self._read_json(context.manifest.schema_manifest_path))
        return self._load_new_capability(summary, registry)

    def load_workflow(self, workflow_id: str) -> WorkflowDefinition:
        context = self.load()
        summary = next((item for item in context.workflows if item.id == workflow_id), None)
        if summary is None:
            raise ContextRepositoryError("Workflow is not declared by the context repository.")
        if context.manifest.workflow_manifest_path is None:
            raise ContextRepositoryError("Legacy contexts do not declare workflows.")
        if context.manifest.generation == "amie":
            return self._load_amie_workflow(summary)
        registry = self._load_schema_registry(self._read_json(context.manifest.schema_manifest_path))
        return self._load_new_workflow(summary, registry)

    def _load_root_manifest(self) -> ContextManifest:
        root = _mapping(self._read_json(PurePosixPath("manifest.json")), "manifest.json")
        if "schema_version" not in root:
            return self._load_amie_root_manifest(root)
        if root.get("schema_version") != 1:
            raise ContextRepositoryError("manifest.json schema_version must be 1.")
        system = _mapping(root.get("system"), "manifest system")
        if system.get("role") != "user_operational_context":
            raise ContextRepositoryError("manifest system.role must be user_operational_context.")
        instructions = _mapping(root.get("instructions"), "manifest instructions")
        schemas = _mapping(root.get("schemas"), "manifest schemas")
        capabilities = _mapping(root.get("capabilities"), "manifest capabilities")
        start = _path(_string(instructions.get("start"), "manifest instructions.start"))
        schema_manifest = _path(_string(schemas.get("manifest"), "manifest schemas.manifest"))
        capability_manifest = _path(
            _string(capabilities.get("manifest"), "manifest capabilities.manifest")
        )
        workflow_manifest: PurePosixPath | None = None
        if "workflows" in root:
            workflows = _mapping(root.get("workflows"), "manifest workflows")
            workflow_manifest = _path(
                _string(workflows.get("manifest"), "manifest workflows.manifest")
            )
        if start != PurePosixPath("instructions/start.md"):
            raise ContextRepositoryError("Context start instruction must be instructions/start.md.")
        if schema_manifest != PurePosixPath("schemas/manifest.json"):
            raise ContextRepositoryError("Context schema manifest must be schemas/manifest.json.")
        if capability_manifest != PurePosixPath("capabilities/manifest.json"):
            raise ContextRepositoryError("Context capability manifest must be capabilities/manifest.json.")
        if workflow_manifest not in {None, PurePosixPath("workflows/manifest.json")}:
            raise ContextRepositoryError("Context workflow manifest must be workflows/manifest.json.")
        return ContextManifest(
            start, schema_manifest, capability_manifest, dict(root), workflow_manifest_path=workflow_manifest
        )

    def _load_amie_root_manifest(self, root: Mapping[str, Any]) -> ContextManifest:
        """Resolve AMIE collections by their manifest pointers, never guessed paths."""
        instructions_manifest = self._collection_manifest_path(root, "instructions")
        schema_manifest = self._collection_manifest_path(root, "schemas")
        capabilities_collection_manifest = self._collection_manifest_path(root, "capabilities")
        workflow_manifest = self._collection_manifest_path(root, "workflows", required=False)

        instructions = _mapping(self._read_json(instructions_manifest), "instructions manifest")
        start = self._entry_path(
            _list(instructions.get("instructions"), "instructions manifest instructions"), "start", "instruction"
        )
        capabilities_collection = _mapping(
            self._read_json(capabilities_collection_manifest), "capabilities collection manifest"
        )
        registries = _list(capabilities_collection.get("registries"), "capabilities registries")
        registry_entry = next(
            (
                _mapping(item, "capabilities registry")
                for item in registries
                if isinstance(item, Mapping)
                and item.get("content_type") in {"mabel_capability_catalog", "capabilities_manifest"}
            ),
            None,
        )
        if registry_entry is None:
            raise ContextRepositoryError("Capabilities collection does not declare a MABEL capability registry.")
        capability_manifest = _amie_path(
            _string(registry_entry.get("path"), "capabilities registry path")
        )
        # Reading the declared start artifact is part of bootstrap, as in AMIE.
        self._repository.read_text(start)
        return ContextManifest(
            start,
            schema_manifest,
            capability_manifest,
            dict(root),
            generation="amie",
            workflow_manifest_path=workflow_manifest,
        )

    def _collection_manifest_path(
        self, root: Mapping[str, Any], collection: str, required: bool = True
    ) -> PurePosixPath | None:
        raw_entries = root.get(collection)
        if raw_entries is None and not required:
            return None
        entries = _list(raw_entries, f"root {collection}")
        if len(entries) != 1:
            raise ContextRepositoryError(f"Root {collection} collection must have exactly one declaration.")
        entry = _mapping(entries[0], f"root {collection} collection")
        return _amie_path(_string(entry.get("manifest"), f"root {collection} manifest"))

    def _entry_path(self, entries: list[Any], identifier: str, location: str) -> PurePosixPath:
        entry = next(
            (
                _mapping(item, location)
                for item in entries
                if isinstance(item, Mapping) and item.get("id") == identifier
            ),
            None,
        )
        if entry is None:
            raise ContextRepositoryError(f"Declared {location} {identifier!r} is missing.")
        return _amie_path(_string(entry.get("path"), f"{location} path"))

    @staticmethod
    def _detect_generation(registry: Any) -> str:
        root = _mapping(registry, "schemas manifest")
        has_legacy = "schema_version" in root
        has_registry = "format_version" in root or "content_types" in root
        if has_legacy and has_registry:
            raise ContextRepositoryError("Context repository mixes legacy and registry schema formats.")
        if has_legacy:
            return "legacy"
        if root.get("format_version") == _NEW_REGISTRY_FORMAT:
            return "registry_v1"
        raise ContextRepositoryError("Context schema registry has an unsupported format.")

    def _validate_legacy_schema_registry(self, registry: Any) -> None:
        root = _mapping(registry, "schemas manifest")
        if root.get("schema_version") != 1:
            raise ContextRepositoryError("schemas/manifest.json schema_version must be 1.")
        schemas = _list(root.get("schemas"), "schemas manifest schemas")
        rules = _list(root.get("rules"), "schemas manifest rules")
        if not _declares_path(schemas, "capability", "schemas/capability.schema.json"):
            raise ContextRepositoryError("Context schema registry must declare the capability schema.")
        if not _declares_path(rules, "capability", "schemas/capability.rules.md"):
            raise ContextRepositoryError("Context schema registry must declare the capability rules.")
        self._repository.read_text(PurePosixPath("schemas/capability.schema.json"))
        self._repository.read_text(PurePosixPath("schemas/capability.rules.md"))

    def _load_schema_registry(self, raw: Any) -> SchemaRegistry:
        root = _mapping(raw, "schemas manifest")
        _validate_registry_bootstrap(root)
        pairs: dict[tuple[str, str], SchemaPair] = {}
        current_versions: dict[str, str] = {}
        governance_paths: list[PurePosixPath] = []
        for entry in _list(root.get("content_types"), "schema registry content_types"):
            record = _mapping(entry, "schema registry content type")
            content_type = _identifier(record.get("content_type"), "schema registry content_type")
            if _identifier(record.get("id"), "schema registry id") != content_type:
                raise ContextRepositoryError("Schema registry id must match content_type.")
            current_version = _version(record.get("current_version"), "schema registry current_version")
            if content_type in current_versions:
                raise ContextRepositoryError("Schema registry content types must be unique.")
            current_versions[content_type] = current_version
            versions = _list(record.get("versions"), "schema registry versions")
            found_current = False
            for version_entry in versions:
                item = _mapping(version_entry, "schema registry version")
                version = _version(item.get("version"), "schema registry version")
                schema_path = _governance_path(item.get("schema_path"), "schema_path", "schemas/schemas")
                rules_path = _governance_path(item.get("rules_path"), "rules_path", "schemas/rules")
                status = item.get("status")
                if status not in {"active", "deprecated", "retired"}:
                    raise ContextRepositoryError("Schema registry version status is invalid.")
                key = (content_type, version)
                if key in pairs:
                    raise ContextRepositoryError("Schema registry versions must be unique.")
                pairs[key] = SchemaPair(content_type, version, schema_path, rules_path, status)
                governance_paths.extend((schema_path, rules_path))
                found_current = found_current or version == current_version
            if not found_current:
                raise ContextRepositoryError("Schema registry current_version must be retained.")

        documents = {pair.schema_path: self._read_schema(pair.schema_path) for pair in pairs.values()}
        for pair in pairs.values():
            self._repository.read_text(pair.rules_path)
        registry = SchemaRegistry(pairs, current_versions, documents, tuple(dict.fromkeys(governance_paths)))
        self._validate_registry_document(root, registry)
        return registry

    def _validate_registry_document(self, raw: Mapping[str, Any], registry: SchemaRegistry) -> None:
        pair = registry.resolve("schema_registry", "1.0")
        self._validate_against_pair(raw, pair, registry, "schema registry")

    def _load_amie_context(self, manifest: ContextManifest) -> LoadedContext:
        """Load an AMIE-shaped context without mistaking content schemas for JSON Schema.

        AMIE schema records and paired rules govern repository meaning.  The
        only Draft JSON Schema documents interpreted here are the capability
        input/output contracts, which are machine runtime contracts.
        """
        governance_paths = self._load_amie_schema_pairs(manifest.schema_manifest_path)
        capabilities = self._load_amie_capability_index(manifest.capability_manifest_path)
        workflows: list[WorkflowSummary] = []
        collection_errors: dict[str, str] = {}
        if manifest.workflow_manifest_path is not None:
            try:
                workflows = self._load_amie_workflow_index(manifest.workflow_manifest_path)
            except ContextRepositoryError as error:
                collection_errors["workflows"] = str(error)
        return LoadedContext(
            manifest,
            tuple(capabilities),
            tuple(governance_paths),
            tuple(workflows),
            collection_errors or None,
        )

    def _load_amie_schema_pairs(self, path: PurePosixPath) -> list[PurePosixPath]:
        root = _mapping(self._read_json(path), "AMIE schemas manifest")
        if root.get("content_type") != "manifest":
            raise ContextRepositoryError("AMIE schemas manifest must have content_type 'manifest'.")
        schemas = _list(root.get("schemas"), "AMIE schemas manifest schemas")
        rules = _list(root.get("rules"), "AMIE schemas manifest rules")
        rule_paths: dict[str, PurePosixPath] = {}
        for raw_rule in rules:
            rule = _mapping(raw_rule, "AMIE schema rules entry")
            content_type = _identifier(rule.get("content_type"), "AMIE rules content_type")
            rule_paths[content_type] = _amie_path(_string(rule.get("path"), "AMIE rules path"))
        governance_paths: list[PurePosixPath] = []
        seen: set[str] = set()
        for raw_schema in schemas:
            schema_entry = _mapping(raw_schema, "AMIE schema entry")
            content_type = _identifier(schema_entry.get("content_type"), "AMIE schema content_type")
            if content_type in seen:
                raise ContextRepositoryError("AMIE schema content types must be unique.")
            seen.add(content_type)
            schema_path = _amie_path(_string(schema_entry.get("path"), "AMIE schema path"))
            rules_path = rule_paths.get(content_type)
            if rules_path is None:
                raise ContextRepositoryError(f"AMIE schema {content_type!r} has no paired rules entry.")
            schema = _mapping(self._read_json(schema_path), f"AMIE schema {content_type}")
            if schema.get("content_type") != content_type:
                raise ContextRepositoryError("AMIE schema record content_type does not match its manifest entry.")
            declared_rules = _amie_path(_string(schema.get("rules_path"), "AMIE schema rules_path"))
            if declared_rules != rules_path:
                raise ContextRepositoryError("AMIE schema record rules_path does not match its paired rules entry.")
            versions = _list(schema.get("versions"), "AMIE schema versions")
            if not versions:
                raise ContextRepositoryError("AMIE schema record must retain at least one version.")
            for version in versions:
                _version(_mapping(version, "AMIE schema version").get("schema_version"), "AMIE schema version")
            self._repository.read_text(rules_path)
            governance_paths.extend((schema_path, rules_path))
        required = {"manifest", "workflow", "mabel_capability_catalog", "mabel_capability"}
        missing = required - seen
        if missing:
            raise ContextRepositoryError("AMIE context is missing required MABEL schema/rules pairs.")
        return governance_paths

    def _load_amie_capability_index(self, path: PurePosixPath) -> list[CapabilitySummary]:
        root = _mapping(self._read_json(path), "MABEL capability catalog")
        if root.get("content_type") != "mabel_capability_catalog":
            raise ContextRepositoryError("MABEL capability catalog has an unsupported content_type.")
        result: list[CapabilitySummary] = []
        seen: set[str] = set()
        for raw_entry in _list(root.get("capabilities"), "MABEL capability catalog capabilities"):
            entry = _mapping(raw_entry, "MABEL capability catalog entry")
            capability_id = _identifier(entry.get("id"), "capability id")
            if capability_id in seen:
                raise ContextRepositoryError("Capability IDs must be unique.")
            entry_path = _amie_path(_string(entry.get("path"), "capability path"))
            expected = PurePosixPath("capabilities") / capability_id / "capability.json"
            if entry_path != expected:
                raise ContextRepositoryError("Capability entry must use its canonical capability.json path.")
            result.append(CapabilitySummary(
                capability_id,
                _string(entry.get("title"), "capability title"),
                entry_path,
                _version(entry.get("version"), "capability version"),
                _version(entry.get("schema_version"), "capability schema_version"),
            ))
            seen.add(capability_id)
        return result

    def _load_amie_capability(self, declared: CapabilitySummary) -> CapabilityDefinition:
        root = _mapping(self._read_json(declared.path), "MABEL capability definition")
        if root.get("content_type") != "mabel_capability":
            raise ContextRepositoryError("Capability definition content_type is invalid.")
        schema_version = _version(root.get("schema_version"), "capability schema_version")
        if schema_version != declared.schema_version or root.get("id") != declared.id:
            raise ContextRepositoryError("Capability definition does not match its declared catalog entry.")
        input_contract = dict(_mapping(root.get("input_contract"), "capability input_contract"))
        output_contract = dict(_mapping(root.get("output_contract"), "capability output_contract"))
        _validate_embedded_contract(input_contract, "input_contract")
        _validate_embedded_contract(output_contract, "output_contract")
        binding = dict(_mapping(root.get("runtime_binding"), "capability runtime_binding"))
        kind = binding.get("kind")
        invocation = binding.get("invocation")
        if invocation not in {"no_arguments", "object"}:
            raise ContextRepositoryError("Capability runtime binding invocation is invalid.")
        executor_path: PurePosixPath | None = None
        callable_name: str | None = None
        tests_path: PurePosixPath | None = None
        if kind == "context_python_callable":
            executor_path = _amie_path(_string(binding.get("path"), "capability runtime path"))
            callable_name = _callable_name(binding.get("callable"))
            verification = _mapping(root.get("verification"), "capability verification")
            tests_path = _amie_path(_string(verification.get("tests_path"), "capability tests_path"))
            self._validate_python_paths(declared, executor_path, tests_path)
        elif kind == "gateway_primitive":
            primitive = _identifier(binding.get("primitive"), "Gateway primitive")
            if primitive == "amie_context_repository":
                operation = binding.get("operation")
                if operation not in _AMIE_CONTEXT_OPERATIONS:
                    raise ContextRepositoryError("AMIE context Gateway primitive operation is unsupported.")
            elif primitive not in {"notification_delivery", "workflow_execution"}:
                raise ContextRepositoryError("Capability requests an unsupported Gateway primitive.")
        else:
            raise ContextRepositoryError("Capability runtime binding kind is unsupported.")
        summary = CapabilitySummary(
            declared.id,
            _string(root.get("title"), "capability title"),
            declared.path,
            _version(root.get("version"), "capability version"),
            schema_version,
        )
        effects = tuple(dict(_mapping(item, "capability effect")) for item in _list(root.get("effects"), "effects"))
        return CapabilityDefinition(
            summary, executor_path, callable_name, tests_path,
            {"input": input_contract, "output": output_contract},
            input_contract=input_contract, output_contract=output_contract, effects=effects,
            execution_kind=str(kind), invocation=str(invocation), runtime_binding=binding,
        )

    def _load_amie_workflow_index(self, path: PurePosixPath) -> list[WorkflowSummary]:
        root = _mapping(self._read_json(path), "AMIE workflows manifest")
        if root.get("content_type") != "workflow":
            raise ContextRepositoryError("AMIE workflows manifest must have content_type 'workflow'.")
        result: list[WorkflowSummary] = []
        seen: set[str] = set()
        for category, entries in root.items():
            if category == "content_type":
                continue
            if not isinstance(entries, list):
                raise ContextRepositoryError("AMIE workflow manifest categories must be arrays.")
            for raw_entry in entries:
                entry = _mapping(raw_entry, "AMIE workflow entry")
                workflow_id = _identifier(entry.get("id"), "workflow id")
                if workflow_id in seen:
                    raise ContextRepositoryError("Workflow IDs must be unique.")
                if entry.get("category") != category:
                    raise ContextRepositoryError("Workflow category must match its manifest collection.")
                workflow_path = _amie_path(_string(entry.get("path"), "workflow path"))
                if workflow_path.suffix != ".md":
                    raise ContextRepositoryError("Workflow path must reference an authoritative Markdown document.")
                self._repository.read_text(workflow_path)
                result.append(WorkflowSummary(
                    workflow_id,
                    _string(entry.get("title"), "workflow title"),
                    workflow_path,
                    _version(entry.get("version"), "workflow version"),
                    _optional_version(entry.get("schema_version"), "workflow schema_version"),
                ))
                seen.add(workflow_id)
        return result

    def _load_amie_workflow(self, summary: WorkflowSummary) -> WorkflowDefinition:
        # A workflow's Markdown remains its procedure. MABEL only validates
        # its optional, document-owned Runtime Plan here; it does not execute
        # or infer actions from the surrounding prose.
        markdown = self._repository.read_text(summary.path)
        try:
            runtime_plan = parse_workflow_runtime_plan(markdown)
        except WorkflowRuntimePlanError as error:
            raise ContextRepositoryError(f"Workflow Runtime Plan is invalid: {error}") from error
        return WorkflowDefinition(
            summary=summary, procedure=(), approval_boundary={}, runtime_plan=runtime_plan
        )

    def _load_legacy_capability_index(self, path: PurePosixPath) -> list[CapabilitySummary]:
        root = _mapping(self._read_json(path), "capabilities manifest")
        if root.get("schema_version") != 1:
            raise ContextRepositoryError("capabilities/manifest.json schema_version must be 1.")
        entries = _list(root.get("capabilities"), "capabilities manifest capabilities")
        result: list[CapabilitySummary] = []
        seen: set[str] = set()
        for raw_entry in entries:
            entry = _mapping(raw_entry, "capability manifest entry")
            capability_id = _identifier(entry.get("id"), "capability id")
            if not _CAPABILITY_ID.fullmatch(capability_id) or capability_id in seen:
                raise ContextRepositoryError("Capability IDs must be unique lowercase identifiers.")
            leaf_path = _path(_string(entry.get("path"), "capability path"))
            if entry.get("content_type") != "capability" or leaf_path != (
                PurePosixPath("capabilities") / capability_id / "capability.json"
            ):
                raise ContextRepositoryError("Capability entry must declare its canonical capability.json path.")
            result.append(CapabilitySummary(
                id=capability_id,
                title=_string(entry.get("title"), "capability title"),
                path=leaf_path,
                version=_version(entry.get("version"), "capability version"),
                schema_version="legacy",
            ))
            seen.add(capability_id)
        return result

    def _load_new_capability_index(
        self, path: PurePosixPath, registry: SchemaRegistry
    ) -> list[CapabilitySummary]:
        root = _mapping(self._read_json(path), "capability manifest")
        if "schema_version" not in root or "format_version" not in root:
            raise ContextRepositoryError("New schema registry requires a new-format capability manifest.")
        if root.get("format_version") != _NEW_REGISTRY_FORMAT or root.get("content_type") != "capability":
            raise ContextRepositoryError("Capability manifest format is invalid.")
        manifest_version = _version(root.get("schema_version"), "capability manifest schema_version")
        self._validate_against_pair(
            root, registry.resolve("capability_manifest", manifest_version), registry, "capability manifest"
        )
        result: list[CapabilitySummary] = []
        seen: set[str] = set()
        for raw_entry in _list(root.get("entries"), "capability manifest entries"):
            entry = _mapping(raw_entry, "capability manifest entry")
            capability_id = _identifier(entry.get("id"), "capability id")
            if not _CAPABILITY_ID.fullmatch(capability_id) or capability_id in seen:
                raise ContextRepositoryError("Capability IDs must be unique lowercase identifiers.")
            entry_path = _path(_string(entry.get("path"), "capability path"))
            if entry_path != PurePosixPath("capabilities") / capability_id / "capability.json":
                raise ContextRepositoryError("Capability entry must use its canonical capability.json path.")
            schema_version = _version(entry.get("schema_version"), "capability entry schema_version")
            registry.resolve("capability", schema_version)
            definition = self._load_new_capability(
                CapabilitySummary(capability_id, "", entry_path, "", schema_version), registry
            )
            result.append(definition.summary)
            seen.add(capability_id)
        return result

    def _load_new_workflow_index(
        self, path: PurePosixPath, registry: SchemaRegistry
    ) -> list[WorkflowSummary]:
        root = _mapping(self._read_json(path), "workflow manifest")
        if "schema_version" not in root or "format_version" not in root:
            raise ContextRepositoryError("Registry-v1 contexts require a new-format workflow manifest.")
        if root.get("format_version") != _NEW_REGISTRY_FORMAT or root.get("content_type") != "workflow":
            raise ContextRepositoryError("Workflow manifest format is invalid.")
        manifest_version = _version(root.get("schema_version"), "workflow manifest schema_version")
        self._validate_against_pair(
            root, registry.resolve("workflow_manifest", manifest_version), registry, "workflow manifest"
        )
        result: list[WorkflowSummary] = []
        seen: set[str] = set()
        for raw_entry in _list(root.get("entries"), "workflow manifest entries"):
            entry = _mapping(raw_entry, "workflow manifest entry")
            workflow_id = _identifier(entry.get("id"), "workflow id")
            if workflow_id in seen:
                raise ContextRepositoryError("Workflow IDs must be unique lowercase identifiers.")
            entry_path = _path(_string(entry.get("path"), "workflow path"))
            if entry_path != PurePosixPath("workflows") / workflow_id / "workflow.json":
                raise ContextRepositoryError("Workflow entry must use its canonical workflow.json path.")
            schema_version = _version(entry.get("schema_version"), "workflow entry schema_version")
            registry.resolve("workflow", schema_version)
            definition = self._load_new_workflow(
                WorkflowSummary(workflow_id, "", entry_path, "", schema_version), registry
            )
            result.append(definition.summary)
            seen.add(workflow_id)
        return result

    def _load_legacy_capability(self, summary: CapabilitySummary) -> CapabilityDefinition:
        root = _mapping(self._read_json(summary.path), "capability definition")
        if root.get("schema_version") != 1 or root.get("id") != summary.id:
            raise ContextRepositoryError("Capability definition does not match its declared entry.")
        executor = _mapping(root.get("executor"), "capability executor")
        contract = dict(_mapping(root.get("contract"), "capability contract"))
        _validate_legacy_contract(contract, summary)
        if executor.get("runtime") == "python_callable_v1":
            executor_path = _path(_string(executor.get("path"), "capability executor.path"))
            self._validate_python_paths(summary, executor_path, root.get("tests_path"))
            callable_name = _callable_name(executor.get("callable"))
            tests_path = _path(_string(root.get("tests_path"), "capability tests_path"))
            # Legacy `none` is the canonical declaration of a zero-input
            # capability. Normalize it at the source adapter so every live
            # index entry has the same explicit invocation-schema shape.
            return CapabilityDefinition(
                summary, executor_path, callable_name, tests_path, contract,
                input_contract=_empty_input_contract(),
            )
        if executor.get("runtime") == "gateway_primitive_v1":
            primitive = _identifier(executor.get("primitive"), "Gateway primitive")
            if primitive != "notification_delivery" or contract.get("input") != "notification_v1":
                raise ContextRepositoryError("Legacy Gateway primitive declaration is invalid.")
            input_contract = {
                "type": "object", "properties": {"message": {"type": "string"},
                "title": {"type": "string"}, "severity": {"type": "string"},
                "event_type": {"type": "string"}, "metadata": {"type": "object"}},
                "required": ["message"], "additionalProperties": False,
            }
            return CapabilityDefinition(
                summary, None, None, None, contract, input_contract=input_contract,
                execution_kind="gateway_primitive", invocation="object",
                runtime_binding={"kind": "gateway_primitive", "primitive": primitive, "invocation": "object"},
            )
        raise ContextRepositoryError("Capability executor runtime is unsupported.")

    def _load_new_capability(
        self, declared: CapabilitySummary, registry: SchemaRegistry
    ) -> CapabilityDefinition:
        root = _mapping(self._read_json(declared.path), "capability definition")
        schema_version = _version(root.get("schema_version"), "capability schema_version")
        if schema_version != declared.schema_version:
            raise ContextRepositoryError("Capability manifest and artifact schema_version must match.")
        self._validate_against_pair(
            root, registry.resolve("capability", schema_version), registry, "capability definition"
        )
        if root.get("id") != declared.id:
            raise ContextRepositoryError("Capability definition id does not match its manifest entry.")
        execution = _mapping(root.get("execution"), "capability execution")
        if execution.get("kind") != "python_callable":
            raise ContextRepositoryError("This Gateway supports only python_callable capabilities in registry v1.")
        executor_path = _path(_string(execution.get("path"), "capability execution.path"))
        verification = _mapping(root.get("verification"), "verification")
        self._validate_python_paths(declared, executor_path, verification.get("tests_path"))
        callable_name = _callable_name(execution.get("callable"))
        invocation = execution.get("invocation")
        if invocation not in {"no_arguments", "object"}:
            raise ContextRepositoryError("Capability execution invocation is invalid.")
        input_contract = dict(_mapping(root.get("input_contract"), "capability input_contract"))
        output_contract = dict(_mapping(root.get("output_contract"), "capability output_contract"))
        _validate_embedded_contract(input_contract, "input_contract")
        _validate_embedded_contract(output_contract, "output_contract")
        tests_path = _path(_string(verification.get("tests_path"), "verification.tests_path"))
        summary = CapabilitySummary(
            id=declared.id,
            title=_string(root.get("title"), "capability title"),
            path=declared.path,
            version=_version(root.get("version"), "capability version"),
            schema_version=schema_version,
        )
        effects = tuple(dict(_mapping(item, "capability effect")) for item in _list(root.get("effects"), "effects"))
        return CapabilityDefinition(
            summary, executor_path, callable_name, tests_path,
            {"input": input_contract, "output": output_contract},
            input_contract=input_contract,
            output_contract=output_contract,
            effects=effects,
            execution_kind="python_callable",
            invocation=invocation,
        )

    def _load_new_workflow(
        self, declared: WorkflowSummary, registry: SchemaRegistry
    ) -> WorkflowDefinition:
        root = _mapping(self._read_json(declared.path), "workflow definition")
        schema_version = _version(root.get("schema_version"), "workflow schema_version")
        if schema_version != declared.schema_version:
            raise ContextRepositoryError("Workflow manifest and artifact schema_version must match.")
        self._validate_against_pair(
            root, registry.resolve("workflow", schema_version), registry, "workflow definition"
        )
        if root.get("id") != declared.id:
            raise ContextRepositoryError("Workflow definition id does not match its manifest entry.")
        _validate_workflow_semantics(root)
        summary = WorkflowSummary(
            id=declared.id,
            title=_string(root.get("title"), "workflow title"),
            path=declared.path,
            version=_version(root.get("version"), "workflow version"),
            schema_version=schema_version,
        )
        procedure = tuple(
            dict(_mapping(item, "workflow procedure step"))
            for item in _list(root.get("procedure"), "workflow procedure")
        )
        return WorkflowDefinition(
            summary=summary,
            procedure=procedure,
            approval_boundary=dict(_mapping(root.get("approval_boundary"), "workflow approval_boundary")),
        )

    def _validate_python_paths(
        self, summary: CapabilitySummary, executor_path: PurePosixPath, raw_tests_path: object
    ) -> None:
        expected_root = PurePosixPath("capabilities") / summary.id
        if executor_path != expected_root / "executor.py":
            raise ContextRepositoryError("Capability executor must use the canonical safe executor path.")
        tests_path = (
            raw_tests_path
            if isinstance(raw_tests_path, PurePosixPath)
            else _path(_string(raw_tests_path, "capability tests_path"))
        )
        if tests_path != expected_root / "tests":
            raise ContextRepositoryError("Capability tests must use the canonical safe tests path.")
        self._repository.read_text(executor_path)
        if not self._repository.is_directory(tests_path):
            raise ContextRepositoryError("Capability tests directory is missing.")

    def _validate_against_pair(
        self, instance: Mapping[str, Any], pair: SchemaPair, registry: SchemaRegistry, location: str
    ) -> None:
        schema = registry.schema_documents[pair.schema_path]
        validator = _declared_validator(schema, pair.schema_path, registry.schema_documents)
        try:
            validator.validate(instance)
        except (ValidationError, SchemaError) as error:
            raise ContextRepositoryError(f"{location} does not satisfy its declared schema: {error.message}") from error
        except Exception as error:
            raise ContextRepositoryError(f"{location} schema references could not be resolved safely.") from error

    def _read_schema(self, path: PurePosixPath) -> dict[str, Any]:
        return dict(_mapping(self._read_json(path), f"schema {path.as_posix()}"))

    def _read_json(self, path: PurePosixPath) -> Any:
        try:
            return json.loads(self._repository.read_text(path))
        except (RepositoryError, json.JSONDecodeError) as error:
            raise ContextRepositoryError(str(error)) from error


def validate_capability_contract(
    contract: Mapping[str, object], value: object, location: str
) -> None:
    """Validate one already-declared, self-contained capability contract."""
    try:
        _validate_embedded_contract(contract, location)
        Draft202012Validator(contract).validate(value)
    except ContextRepositoryError as error:
        raise CapabilityContractError(
            f"Capability {location} violates its declared contract."
        ) from error
    except (ValidationError, SchemaError) as error:
        raise CapabilityContractError(f"Capability {location} violates its declared contract.") from error


def _validate_registry_bootstrap(root: Mapping[str, Any]) -> None:
    if set(root) != {"format_version", "content_type", "content_types"}:
        raise ContextRepositoryError("Schema registry bootstrap fields are invalid.")
    if root.get("format_version") != _NEW_REGISTRY_FORMAT or root.get("content_type") != "schema_registry":
        raise ContextRepositoryError("Schema registry bootstrap format is invalid.")
    if not isinstance(root.get("content_types"), list):
        raise ContextRepositoryError("Schema registry content_types must be an array.")


def _declared_validator(
    schema: dict[str, Any], schema_path: PurePosixPath, documents: Mapping[PurePosixPath, dict[str, Any]]
) -> Draft202012Validator:
    allowed = set(documents)
    resources = Registry()
    normalized: dict[PurePosixPath, dict[str, Any]] = {}
    for path, document in documents.items():
        _validate_reference_safety(document, path, allowed)
        copied = dict(document)
        uri = _schema_uri(path)
        if "$id" in copied and copied["$id"] != uri:
            raise ContextRepositoryError("Schema $id must be its canonical local schema URI.")
        copied["$id"] = uri
        normalized[path] = copied
        resources = resources.with_resource(uri, Resource.from_contents(copied))
    return Draft202012Validator(normalized[schema_path], registry=resources)


def _validate_reference_safety(value: object, base: PurePosixPath, allowed: set[PurePosixPath]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"}:
                if not isinstance(child, str):
                    raise ContextRepositoryError("JSON Schema references must be strings.")
                raw_path, _ = urldefrag(child)
                parsed = urlsplit(raw_path)
                if parsed.scheme or parsed.netloc or raw_path.startswith("/"):
                    raise ContextRepositoryError("JSON Schema references must remain local to declared schemas.")
                if raw_path:
                    candidate = PurePosixPath(raw_path)
                    if ".." in candidate.parts:
                        raise ContextRepositoryError("JSON Schema references may not traverse paths.")
                    target = base.parent / candidate
                else:
                    target = base
                if target not in allowed:
                    raise ContextRepositoryError("JSON Schema reference is not a declared schema path.")
            _validate_reference_safety(child, base, allowed)
    elif isinstance(value, list):
        for child in value:
            _validate_reference_safety(child, base, allowed)


def _validate_embedded_contract(contract: Mapping[str, object], location: str) -> None:
    _reject_embedded_contract_references(contract, location)
    try:
        Draft202012Validator.check_schema(contract)
    except SchemaError as error:
        raise ContextRepositoryError(f"Capability {location} is not valid JSON Schema.") from error
    if contract.get("type") != "object":
        raise ContextRepositoryError(f"Capability {location} must describe an object.")


def _validate_workflow_semantics(workflow: Mapping[str, Any]) -> None:
    procedure = _list(workflow.get("procedure"), "workflow procedure")
    declared_sources: set[str] = set()
    for raw_source in _list(workflow.get("required_sources"), "workflow required_sources"):
        source = _mapping(raw_source, "workflow required source")
        source_id = _identifier(source.get("id"), "workflow required source id")
        if source_id in declared_sources:
            raise ContextRepositoryError("Workflow required source IDs must be unique.")
        declared_sources.add(source_id)
    step_ids: set[str] = set()
    transitions: dict[str, set[str]] = {}
    has_stop = False
    for raw_step in procedure:
        step = _mapping(raw_step, "workflow procedure step")
        step_id = _identifier(step.get("id"), "workflow step id")
        if step_id in step_ids:
            raise ContextRepositoryError("Workflow procedure step IDs must be unique.")
        step_ids.add(step_id)
        sources = _list(step.get("sources", []), "workflow step sources")
        if any(_identifier(source, "workflow step source") not in declared_sources for source in sources):
            raise ContextRepositoryError("Workflow step sources must be declared required sources.")
        targets: set[str] = set()
        if "next" in step:
            targets.add(_identifier(step.get("next"), "workflow step next"))
        if step.get("kind") == "decide":
            outcomes = _mapping(step.get("outcomes"), "workflow decision outcomes")
            targets.update(_identifier(target, "workflow decision transition") for target in outcomes.values())
        transitions[step_id] = targets
        has_stop = has_stop or step.get("kind") == "stop"
    if not has_stop:
        raise ContextRepositoryError("Workflow procedure must contain a stop step.")
    for targets in transitions.values():
        unknown = targets - step_ids
        if unknown:
            raise ContextRepositoryError("Workflow transition targets must name declared procedure steps.")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ContextRepositoryError("Workflow procedure transitions may not form a cycle in v1.")
        if step_id in visited:
            return
        visiting.add(step_id)
        for target in transitions[step_id]:
            visit(target)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in transitions:
        visit(step_id)


def _reject_embedded_contract_references(value: object, location: str) -> None:
    if isinstance(value, Mapping):
        if "$ref" in value or "$dynamicRef" in value:
            raise ContextRepositoryError(f"Capability {location} may not use unresolved references.")
        for child in value.values():
            _reject_embedded_contract_references(child, location)
    elif isinstance(value, list):
        for child in value:
            _reject_embedded_contract_references(child, location)


def _schema_uri(path: PurePosixPath) -> str:
    return f"mabel-context:///{path.as_posix()}"


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextRepositoryError(f"{location} must be an object.")
    return value


def _list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContextRepositoryError(f"{location} must be an array.")
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextRepositoryError(f"{location} must be a non-empty string.")
    return value.strip()


def _identifier(value: Any, location: str) -> str:
    result = _string(value, location)
    if not _CAPABILITY_ID.fullmatch(result):
        raise ContextRepositoryError(f"{location} must be a lowercase identifier.")
    return result


def _version(value: Any, location: str) -> str:
    result = _string(value, location)
    if not _VERSION.fullmatch(result):
        raise ContextRepositoryError(f"{location} must use major.minor version syntax.")
    return result


def _optional_version(value: Any, location: str) -> str | None:
    """Validate a Gateway-owned stamp when present without requiring it on reads."""
    if value is None:
        return None
    return _version(value, location)


def _callable_name(value: Any) -> str:
    result = _string(value, "capability executor callable")
    if not _CALLABLE.fullmatch(result):
        raise ContextRepositoryError("Capability executor callable is invalid.")
    return result


def _path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ContextRepositoryError("Context paths must be repository-relative.")
    return path


def _amie_path(value: str) -> PurePosixPath:
    """Normalize an AMIE manifest path while retaining repository containment."""
    candidate = PurePosixPath(value)
    if not candidate.is_absolute():
        return _path(value)
    relative = PurePosixPath(*candidate.parts[1:])
    if not relative.parts or ".." in relative.parts:
        raise ContextRepositoryError("AMIE paths must remain inside the context repository.")
    return relative


def _governance_path(value: object, location: str, root: str) -> PurePosixPath:
    path = _path(_string(value, location))
    root_path = PurePosixPath(root)
    if path.parent != root_path:
        raise ContextRepositoryError(f"{location} must be directly declared beneath {root}/.")
    return path


def _declares_path(entries: list[Any], identifier: str, path: str) -> bool:
    return any(
        isinstance(entry, Mapping) and entry.get("id") == identifier and entry.get("path") == path
        for entry in entries
    )


def _validate_legacy_contract(contract: Mapping[str, Any], summary: CapabilitySummary) -> None:
    """Keep legacy contexts executable until each has migrated to a declared contract."""
    del summary
    allowed = {
        ("none", "json_object"),
        ("notification_v1", "notification_delivery_v1"),
    }
    if (contract.get("input"), contract.get("output")) not in allowed:
        raise ContextRepositoryError("Capability uses an unsupported legacy contract.")


def _empty_input_contract() -> dict[str, object]:
    """Return the explicit JSON Schema form of legacy no-input invocation."""
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
