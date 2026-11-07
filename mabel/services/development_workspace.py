"""Isolated, commit-pinned writable workspaces for governed development."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from mabel.connections.credentials import CredentialUnavailableError
from mabel.context.projection import _git_auth_environment
from mabel.repositories.credentials import GitHubTokenCredential, ManagedRepositoryCredentialStore
from mabel.repositories.models import ManagedRepository
from mabel.services.managed_repositories import ManagedRepositoryService


class DevelopmentWorkspaceError(ValueError):
    """A stable, non-secret failure while preparing or inspecting a workspace."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DevelopmentWorkspace:
    """An internal-only handle; its path must never be returned by an API."""

    workspace_id: str
    root: Path
    owner_id: str
    repository_id: str
    base_revision: str
    allowed_paths: tuple[PurePosixPath, ...]
    execution_id: str | None = None


@dataclass(frozen=True)
class DevelopmentWorkspaceChanges:
    changed_files: tuple[PurePosixPath, ...]
    patch: str
    proposed_files: tuple[dict[str, object], ...]

    def as_evidence(self) -> dict[str, object]:
        return {
            "changed_files": [str(path) for path in self.changed_files],
            "patch": self.patch,
            "proposed_files": [dict(item) for item in self.proposed_files],
        }


class DevelopmentCheckout(Protocol):
    def create(
        self,
        repository: ManagedRepository,
        credential: GitHubTokenCredential,
        base_revision: str,
        target: Path,
    ) -> str: ...


class GitHubDevelopmentCheckout:
    """A temporary detached clone, deliberately separate from a projection."""

    def __init__(self, remote_url: Callable[[ManagedRepository], str] | None = None) -> None:
        self._remote_url = remote_url or (lambda repository: f"https://github.com/{repository.canonical_locator}.git")

    def create(
        self,
        repository: ManagedRepository,
        credential: GitHubTokenCredential,
        base_revision: str,
        target: Path,
    ) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        try:
            with _git_auth_environment(credential.token) as environment:
                _git(["clone", "--no-checkout", self._remote_url(repository), str(target)], environment, target.parent)
                # Fetching the requested object, rather than a branch tip, is
                # what makes a workflow's provenance exact.
                try:
                    _git(["fetch", "--force", "--no-tags", "origin", base_revision], environment, target)
                    _git(["checkout", "--detach", "--force", "FETCH_HEAD"], environment, target)
                    return _git(["rev-parse", "HEAD"], environment, target).strip()
                except subprocess.SubprocessError as error:
                    raise DevelopmentWorkspaceError("base_revision_unavailable") from error
        except (OSError, subprocess.SubprocessError) as error:
            _remove_tree(target)
            raise DevelopmentWorkspaceError("workspace_checkout_failed") from error


class DevelopmentWorkspaceService:
    """Create, inspect, and remove workspace-scoped development state."""

    _MAX_CHANGED_FILES = 64
    _MAX_PATCH_BYTES = 128 * 1024

    def __init__(
        self,
        repositories: ManagedRepositoryService,
        credentials: ManagedRepositoryCredentialStore,
        checkout: DevelopmentCheckout,
        workspace_root: Path,
        *,
        projection_root: Path | None = None,
    ) -> None:
        self._repositories = repositories
        self._credentials = credentials
        self._checkout = checkout
        self._workspace_root = workspace_root.resolve()
        self._projection_root = projection_root.resolve() if projection_root is not None else None
        if self._projection_root is not None and self._workspace_root == self._projection_root:
            raise ValueError("Development workspaces must not share the projection root.")

    async def create(
        self,
        owner_id: str,
        base_revision: str,
        allowed_paths: Iterable[str | PurePosixPath],
        *,
        execution_id: str | None = None,
    ) -> DevelopmentWorkspace:
        if not _commit(base_revision):
            raise DevelopmentWorkspaceError("invalid_base_revision")
        scopes = _scopes(allowed_paths)
        repository = self._repositories.resolve_active_context_repository(owner_id)
        try:
            credential = await self._credentials.resolve(str(repository.credential_id))
        except CredentialUnavailableError as error:
            raise DevelopmentWorkspaceError("repository_credential_unavailable") from error
        if not isinstance(credential, GitHubTokenCredential):
            raise DevelopmentWorkspaceError("repository_credential_unavailable")
        workspace_id = str(uuid4())
        target = (self._workspace_root / str(repository.id) / workspace_id).resolve()
        if not _within(target, self._workspace_root):
            raise DevelopmentWorkspaceError("invalid_workspace")
        actual = self._checkout.create(repository, credential, base_revision, target)
        if actual != base_revision:
            _remove_tree(target)
            raise DevelopmentWorkspaceError("base_revision_unavailable")
        if self._projection_root is not None and _within(target, self._projection_root):
            _remove_tree(target)
            raise DevelopmentWorkspaceError("runtime_projection_reused")
        return DevelopmentWorkspace(
            workspace_id, target, owner_id, str(repository.id), actual, scopes, execution_id
        )

    def collect_changes(self, workspace: DevelopmentWorkspace) -> DevelopmentWorkspaceChanges:
        self._validate_workspace(workspace)
        try:
            changed = _changed_paths(workspace.root)
        except subprocess.SubprocessError as error:
            raise DevelopmentWorkspaceError("workspace_inspection_failed") from error
        if len(changed) > self._MAX_CHANGED_FILES:
            raise DevelopmentWorkspaceError("changed_file_limit_exceeded")
        unauthorized = [path for path in changed if not _permitted(path, workspace.allowed_paths)]
        if unauthorized:
            raise DevelopmentWorkspaceError("unauthorized_workspace_change")
        patch = _bounded_patch(workspace.root, changed, self._MAX_PATCH_BYTES)
        return DevelopmentWorkspaceChanges(tuple(changed), patch, _proposed_files(workspace.root, changed, self._MAX_PATCH_BYTES))

    def cleanup(self, workspace: DevelopmentWorkspace) -> None:
        """Idempotently remove an internal workspace and nothing else."""
        target = workspace.root.resolve()
        if not self._valid_handle(workspace, target):
            raise DevelopmentWorkspaceError("invalid_workspace")
        _remove_tree(target)

    def _validate_workspace(self, workspace: DevelopmentWorkspace) -> None:
        root = workspace.root.resolve()
        if not self._valid_handle(workspace, root) or not (root / ".git").exists():
            raise DevelopmentWorkspaceError("invalid_workspace")
        if self._projection_root is not None and _within(root, self._projection_root):
            raise DevelopmentWorkspaceError("runtime_projection_reused")
        actual = _git(["rev-parse", "HEAD"], os.environ.copy(), root).strip()
        if actual != workspace.base_revision:
            raise DevelopmentWorkspaceError("workspace_revision_drift")

    def _valid_handle(self, workspace: DevelopmentWorkspace, root: Path) -> bool:
        return (
            _within(root, self._workspace_root)
            and root.name == workspace.workspace_id
            and root.parent.name == workspace.repository_id
            and root.parent.parent == self._workspace_root
        )


def _scopes(values: Iterable[str | PurePosixPath]) -> tuple[PurePosixPath, ...]:
    paths: list[PurePosixPath] = []
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
            raise DevelopmentWorkspaceError("invalid_allowed_path")
        paths.append(path)
    if not paths:
        raise DevelopmentWorkspaceError("invalid_allowed_path")
    return tuple(dict.fromkeys(paths))


def _changed_paths(root: Path) -> list[PurePosixPath]:
    raw = _git(["diff", "--name-status", "-z", "-M", "HEAD"], os.environ.copy(), root).encode("utf-8")
    parts = raw.split(b"\0")
    paths: list[PurePosixPath] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index].decode("ascii", errors="strict")
        index += 1
        count = 2 if status[:1] in {"R", "C"} else 1
        for _ in range(count):
            if index >= len(parts) or not parts[index]:
                raise subprocess.SubprocessError("invalid git status output")
            paths.append(_repository_path(parts[index]))
            index += 1
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], os.environ.copy(), root).encode("utf-8")
    paths.extend(_repository_path(part) for part in untracked.split(b"\0") if part)
    return sorted(set(paths), key=str)


def _bounded_patch(root: Path, changed: list[PurePosixPath], maximum: int) -> str:
    chunks: list[bytes] = []
    tracked = _git_bytes(["diff", "--binary", "--no-ext-diff", "HEAD", "--"], root, allow_failure=False)
    chunks.append(tracked)
    tracked_paths = set(
        _repository_path(part)
        for part in _git(["diff", "--name-only", "-z", "HEAD"], os.environ.copy(), root).encode("utf-8").split(b"\0")
        if part
    )
    for path in changed:
        if path in tracked_paths:
            continue
        result = _git_bytes(["diff", "--binary", "--no-index", "--", "/dev/null", str(path)], root, allow_failure=True)
        chunks.append(result)
        if sum(len(chunk) for chunk in chunks) > maximum:
            raise DevelopmentWorkspaceError("patch_limit_exceeded")
    patch = b"".join(chunks)
    if len(patch) > maximum:
        raise DevelopmentWorkspaceError("patch_limit_exceeded")
    return patch.decode("utf-8", errors="replace")


def _proposed_files(root: Path, changed: list[PurePosixPath], maximum: int) -> tuple[dict[str, object], ...]:
    """Capture bounded complete replacements for the later governed draft step."""
    files: list[dict[str, object]] = []
    total = 0
    for path in changed:
        target = root / path
        if not target.exists():
            files.append({"path": str(path), "change": "delete"})
            continue
        if not target.is_file() or target.is_symlink():
            raise DevelopmentWorkspaceError("unsupported_workspace_change")
        content = target.read_text(encoding="utf-8")
        total += len(content.encode("utf-8"))
        if total > maximum:
            raise DevelopmentWorkspaceError("proposed_content_limit_exceeded")
        files.append({"path": str(path), "change": "replace", "content": content})
    return tuple(files)


def _repository_path(raw: bytes) -> PurePosixPath:
    value = raw.decode("utf-8", errors="strict")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise subprocess.SubprocessError("unsafe repository path")
    return path


def _permitted(path: PurePosixPath, scopes: tuple[PurePosixPath, ...]) -> bool:
    return any(path == scope or scope in path.parents for scope in scopes)


def _commit(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _git(arguments: list[str], environment: dict[str, str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise subprocess.SubprocessError("git command failed")
    return completed.stdout


def _git_bytes(arguments: list[str], cwd: Path, *, allow_failure: bool) -> bytes:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        raise DevelopmentWorkspaceError("workspace_inspection_failed")
    return completed.stdout


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for entry in [*path.rglob("*"), path]:
        if entry.is_symlink():
            continue
        try:
            entry.chmod(entry.stat().st_mode | stat.S_IWUSR)
        except OSError:
            continue
    shutil.rmtree(path, ignore_errors=True)
