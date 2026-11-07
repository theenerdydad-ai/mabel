"""Commit-pinned, manifest-bounded read operations for the MABEL portal workspace."""

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
from typing import Any

from mabel.context.projection import ContextProjection, ContextProjectionError, ContextProjectionService
from mabel.repository import LocalRepository, RepositoryError
from mabel.services.capabilities import CapabilityExecutionReport, CapabilityService, CapabilityServiceError


class WorkspaceServiceError(ValueError):
    """Safe application failure for the read-only workspace."""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    repository: dict[str, object]
    projection: dict[str, object]
    capabilities: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "projection": self.projection,
            "capabilities": self.capabilities,
        }


class ContextWorkspaceService:
    """Read only declared context content from a freshly validated exact projection."""

    def __init__(
        self, projections: ContextProjectionService, capabilities: CapabilityService
    ) -> None:
        self._projections = projections
        self._capabilities = capabilities

    async def snapshot(self, owner_id: str) -> WorkspaceSnapshot:
        projection = await self._projection(owner_id)
        return WorkspaceSnapshot(
            repository={
                "id": str(projection.repository.id),
                "display_name": projection.repository.display_name,
                "canonical_locator": projection.repository.canonical_locator,
                "default_branch": projection.repository.default_branch,
            },
            projection={
                "status": "synchronized",
                "commit_sha": projection.commit_sha,
                "synced_at": projection.synced_at.isoformat(),
                "read_only": True,
            },
            capabilities=[
                {**summary.as_dict(), "status": "available"}
                for summary in projection.context.capabilities
            ],
        )

    async def tree(self, owner_id: str, commit_sha: str) -> dict[str, object]:
        projection = await self._at_commit(owner_id, commit_sha)
        allowed = self._allowed_paths(projection)
        return {
            "repository_commit": projection.commit_sha,
            "tree": _build_tree(allowed, self._tree_metadata(projection, allowed)),
        }

    async def read_file(
        self, owner_id: str, commit_sha: str, raw_path: str
    ) -> dict[str, object]:
        projection = await self._at_commit(owner_id, commit_sha)
        path = _safe_path(raw_path)
        allowed = self._allowed_paths(projection)
        if path not in allowed:
            raise WorkspaceServiceError("File is not declared by the MABEL context manifest.")
        try:
            content = LocalRepository(projection.root).read_text(path)
        except RepositoryError as error:
            raise WorkspaceServiceError("Declared workspace file could not be read safely.") from error
        return {
            "repository_commit": projection.commit_sha,
            "path": path.as_posix(),
            "content": content,
            "language": _language_for(path),
            "read_only": True,
        }

    async def capability_detail(
        self, owner_id: str, commit_sha: str, capability_id: str
    ) -> dict[str, object]:
        projection = await self._at_commit(owner_id, commit_sha)
        try:
            definition = projection.discovery.load_capability(capability_id)
        except Exception as error:
            raise WorkspaceServiceError("Capability is not declared by the MABEL context repository.") from error
        return {
            "repository_commit": projection.commit_sha,
            "id": definition.summary.id,
            "title": definition.summary.title,
            "version": definition.summary.version,
            "status": "available",
            "runtime": "python_callable_v1",
            "executor": {
                "path": definition.executor_path.as_posix(),
                "callable": definition.callable_name,
            },
            "tests": {"path": definition.tests_path.as_posix(), "declared": True},
        }

    async def run_capability(self, owner_id: str, capability_id: str) -> CapabilityExecutionReport:
        try:
            return await self._capabilities.run_capability_report(owner_id, capability_id)
        except CapabilityServiceError as error:
            raise WorkspaceServiceError(str(error)) from error

    async def _at_commit(self, owner_id: str, requested_commit: str) -> ContextProjection:
        projection = await self._projection(owner_id)
        if requested_commit != projection.commit_sha:
            raise WorkspaceServiceError("Requested repository commit is stale. Refresh the workspace.")
        return projection

    async def _projection(self, owner_id: str) -> ContextProjection:
        try:
            return await self._projections.synchronize(owner_id)
        except ContextProjectionError as error:
            raise WorkspaceServiceError(str(error)) from error
        except Exception as error:
            raise WorkspaceServiceError("No active MABEL context repository is available for the workspace.") from error

    def _allowed_paths(self, projection: ContextProjection) -> set[PurePosixPath]:
        """Derive every readable file from validated declarations, never user input."""
        allowed = {
            PurePosixPath("manifest.json"),
            projection.context.manifest.start_instruction_path,
            projection.context.manifest.schema_manifest_path,
            projection.context.manifest.capability_manifest_path,
        }
        if projection.context.manifest.workflow_manifest_path is not None:
            allowed.add(projection.context.manifest.workflow_manifest_path)
        allowed.update(projection.context.governance_paths)
        repository = LocalRepository(projection.root)
        for summary in projection.context.capabilities:
            definition = projection.discovery.load_capability(summary.id)
            allowed.add(summary.path)
            if definition.executor_path is not None:
                allowed.add(definition.executor_path)
            if definition.tests_path is not None:
                tests_root = repository.resolve_path(definition.tests_path)
                for child in tests_root.rglob("*"):
                    if not child.is_file():
                        continue
                    relative = PurePosixPath(child.relative_to(projection.root).as_posix())
                    try:
                        repository.resolve_path(relative)
                    except RepositoryError as error:
                        raise WorkspaceServiceError("Declared workspace file could not be read safely.") from error
                    allowed.add(relative)
        for summary in projection.context.workflows:
            allowed.add(summary.path)
        return allowed

    def _tree_metadata(
        self, projection: ContextProjection, allowed: set[PurePosixPath]
    ) -> dict[PurePosixPath, dict[str, str]]:
        """Return optional manifest display data without expanding readable scope.

        ``path`` remains the sole access key.  Titles and content types are
        presentation metadata from already validated manifests, never a means
        to discover arbitrary repository content.
        """
        metadata: dict[PurePosixPath, dict[str, str]] = {
            PurePosixPath("manifest.json"): {"content_type": "manifest"},
            projection.context.manifest.start_instruction_path: {
                "title": "Start instructions",
                "content_type": "instructions",
            },
            projection.context.manifest.schema_manifest_path: {
                "title": "Schema registry",
                "content_type": "manifest",
            },
            projection.context.manifest.capability_manifest_path: {
                "title": "Capability index",
                "content_type": "manifest",
            },
        }
        if projection.context.manifest.workflow_manifest_path is not None:
            metadata[projection.context.manifest.workflow_manifest_path] = {
                "title": "Workflow index",
                "content_type": "manifest",
            }
        for governance_path in projection.context.governance_paths:
            metadata[governance_path] = {
                "content_type": "rules" if governance_path.suffix == ".md" else "schema",
            }
        repository = LocalRepository(projection.root)
        self._apply_manifest_entry_metadata(
            repository,
            projection.context.manifest.schema_manifest_path,
            ("schemas", "rules"),
            allowed,
            metadata,
        )
        for summary in projection.context.capabilities:
            metadata[summary.path] = {
                "title": summary.title,
                "content_type": "capability",
                "id": summary.id,
            }
            definition = projection.discovery.load_capability(summary.id)
            capability_root = summary.path.parent
            metadata[capability_root] = {
                "content_type": "capability",
                "id": summary.id,
            }
            if definition.executor_path is not None:
                metadata[definition.executor_path] = {"content_type": "executor"}
            if definition.tests_path is not None:
                metadata[definition.tests_path] = {"content_type": "tests"}
        for summary in projection.context.workflows:
            metadata[summary.path] = {
                "title": summary.title,
                "content_type": "workflow",
                "id": summary.id,
            }
            metadata[summary.path.parent] = {
                "content_type": "workflow",
                "id": summary.id,
            }
        return metadata

    def _apply_manifest_entry_metadata(
        self,
        repository: LocalRepository,
        manifest_path: PurePosixPath,
        collections: tuple[str, ...],
        allowed: set[PurePosixPath],
        metadata: dict[PurePosixPath, dict[str, str]],
    ) -> None:
        """Copy safe display fields from declared schema/rule manifest entries."""
        try:
            raw = json.loads(repository.read_text(manifest_path))
        except (RepositoryError, json.JSONDecodeError) as error:
            raise WorkspaceServiceError("Declared workspace manifest could not be read safely.") from error
        if not isinstance(raw, dict):
            return
        for collection in collections:
            entries = raw.get(collection)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                try:
                    path = _safe_path(entry["path"])
                except WorkspaceServiceError:
                    continue
                if path not in allowed:
                    continue
                item = {
                    key: value
                    for key in ("id", "title", "content_type")
                    if isinstance((value := entry.get(key)), str) and value.strip()
                }
                if item:
                    metadata[path] = item


def _safe_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if not raw_path or path.is_absolute() or not path.parts or ".." in path.parts:
        raise WorkspaceServiceError("Workspace paths must remain repository-relative.")
    return path


def _build_tree(
    paths: set[PurePosixPath], metadata: dict[PurePosixPath, dict[str, str]]
) -> list[dict[str, Any]]:
    root: dict[str, Any] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        node = root
        for part in path.parts[:-1]:
            node = node.setdefault(part, {})
        node[path.name] = None

    def render(mapping: dict[str, Any], prefix: PurePosixPath = PurePosixPath(".")) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        ordered_entries = sorted(
            mapping.items(), key=lambda item: (item[1] is None, item[0].casefold())
        )
        for name, child in ordered_entries:
            path = prefix / name
            display = metadata.get(path, {})
            if child is None:
                entries.append({
                    "name": name,
                    "path": path.as_posix(),
                    "type": "file",
                    **({"title": display["title"]} if "title" in display else {}),
                    **({"content_type": display["content_type"]} if "content_type" in display else {}),
                    **({"id": display["id"]} if "id" in display else {}),
                })
            else:
                entries.append({
                    "name": name,
                    "path": path.as_posix(),
                    "type": "directory",
                    **({"title": display["title"]} if "title" in display else {}),
                    **({"content_type": display["content_type"]} if "content_type" in display else {}),
                    **({"id": display["id"]} if "id" in display else {}),
                    "children": render(child, path),
                })
        return entries

    return render(root)


def _language_for(path: PurePosixPath) -> str:
    if path.suffix == ".json":
        return "json"
    if path.suffix == ".md":
        return "markdown"
    if path.suffix == ".py":
        return "python"
    return "text"
