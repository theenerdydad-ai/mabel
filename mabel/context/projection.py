"""Trusted synchronization of a registered GitHub context repository to a commit."""

import os
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from mabel.context.discovery import MabelContextDiscovery
from mabel.context.models import LoadedContext
from mabel.connections.credentials import CredentialUnavailableError
from mabel.diagnostics import log_capability_diagnostic
from mabel.repositories.credentials import GitHubTokenCredential, ManagedRepositoryCredentialStore
from mabel.repositories.models import ManagedRepository
from mabel.repository import LocalRepository
from mabel.services.managed_repositories import ManagedRepositoryService
from mabel.state.projections import SqliteRepositoryProjectionStore


class ContextProjectionError(RuntimeError):
    """A bounded failure while resolving a context execution projection."""


@dataclass(frozen=True)
class GitCheckoutResult:
    root: Path
    commit_sha: str


@dataclass(frozen=True)
class ContextProjection:
    repository: ManagedRepository
    root: Path
    commit_sha: str
    context: LoadedContext
    discovery: MabelContextDiscovery
    synced_at: datetime


class Checkout(Protocol):
    def synchronize(
        self, repository: ManagedRepository, credential: GitHubTokenCredential, target: Path
    ) -> GitCheckoutResult: ...


class GitHubGitCheckout:
    """Fetch a GitHub repository with an invocation-scoped token and detached checkout."""

    def __init__(self, remote_url: Callable[[ManagedRepository], str] | None = None) -> None:
        self._remote_url = remote_url or _github_clone_url

    def synchronize(
        self, repository: ManagedRepository, credential: GitHubTokenCredential, target: Path
    ) -> GitCheckoutResult:
        remote = self._remote_url(repository)
        target.parent.mkdir(parents=True, exist_ok=True)
        _make_writable(target)
        try:
            with _git_auth_environment(credential.token) as environment:
                if not (target / ".git").is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    _git(["clone", "--no-checkout", remote, str(target)], environment, target.parent)
                else:
                    _git(["remote", "set-url", "origin", remote], environment, target)
                _git(
                    ["fetch", "--force", "--no-tags", "origin", f"refs/heads/{repository.default_branch}"],
                    environment,
                    target,
                )
                _git(["checkout", "--detach", "--force", "FETCH_HEAD"], environment, target)
                commit_sha = _git(["rev-parse", "HEAD"], environment, target).strip()
        except (OSError, subprocess.SubprocessError) as error:
            log_capability_diagnostic("context_projection", "git_checkout", error, context_root=target)
            raise ContextProjectionError("The MABEL context repository could not be synchronized.") from error
        if len(commit_sha) != 40 or any(character not in "0123456789abcdef" for character in commit_sha):
            error = ContextProjectionError("Git did not return a valid context repository commit.")
            log_capability_diagnostic("context_projection", "validate_checkout_commit", error, context_root=target)
            raise error
        _make_read_only(target)
        return GitCheckoutResult(root=target, commit_sha=commit_sha)


class ContextProjectionService:
    """Resolve one owner context repository, synchronize it, then validate its manifests."""

    def __init__(
        self,
        repositories: ManagedRepositoryService,
        credentials: ManagedRepositoryCredentialStore,
        checkout: Checkout,
        projection_root: Path,
        store: SqliteRepositoryProjectionStore,
    ) -> None:
        self._repositories = repositories
        self._credentials = credentials
        self._checkout = checkout
        self._projection_root = projection_root.resolve()
        self._store = store
        self._locks_guard = threading.Lock()
        self._repository_locks: dict[str, threading.Lock] = {}

    async def synchronize(self, owner_id: str) -> ContextProjection:
        try:
            repository = self._repositories.resolve_active_context_repository(owner_id)
        except Exception as error:
            log_capability_diagnostic("context_projection", "resolve_active_repository", error)
            raise
        try:
            credential = await self._credentials.resolve(str(repository.credential_id))
        except CredentialUnavailableError as error:
            log_capability_diagnostic("context_projection", "resolve_repository_credential", error)
            raise ContextProjectionError("Context repository credential is unavailable.") from error
        if not isinstance(credential, GitHubTokenCredential):
            error = ContextProjectionError("Context repository does not have a GitHub credential.")
            log_capability_diagnostic("context_projection", "validate_repository_credential", error)
            raise error
        target = self._projection_root / str(repository.id) / "checkout"
        try:
            with self._repository_lock(str(repository.id)):
                checkout = self._checkout.synchronize(repository, credential, target)
                log_capability_diagnostic(
                    "context_projection",
                    "checkout_loaded",
                    commit_sha=checkout.commit_sha,
                    context_root=checkout.root,
                )
                local_repository = LocalRepository(checkout.root)
                discovery = MabelContextDiscovery(local_repository)
                context = discovery.load()
        except ContextProjectionError as error:
            log_capability_diagnostic(
                "context_projection", "checkout_or_projection_failed", error, context_root=target
            )
            raise
        except Exception as error:
            log_capability_diagnostic(
                "context_projection",
                "context_discovery_load",
                error,
                commit_sha=checkout.commit_sha if "checkout" in locals() else None,
                context_root=checkout.root if "checkout" in locals() else target,
            )
            raise ContextProjectionError("MABEL context repository validation failed.") from error
        synced_at = datetime.now(timezone.utc)
        self._store.record_success(repository.id, checkout.commit_sha, synced_at)
        log_capability_diagnostic(
            "context_projection",
            "context_projection_recorded",
            commit_sha=checkout.commit_sha,
            context_root=checkout.root,
        )
        return ContextProjection(repository, checkout.root, checkout.commit_sha, context, discovery, synced_at)

    def _repository_lock(self, repository_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._repository_locks.setdefault(repository_id, threading.Lock())


def _git(arguments: list[str], environment: dict[str, str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise subprocess.SubprocessError("git command failed")
    return completed.stdout


def _github_clone_url(repository: ManagedRepository) -> str:
    return f"https://github.com/{repository.canonical_locator}.git"


class _git_auth_environment:
    """Use a short-lived askpass helper so a GitHub token is never in Git arguments."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> dict[str, str]:
        self._directory = tempfile.TemporaryDirectory(prefix="mabel-git-")
        helper = Path(self._directory.name) / "askpass"
        helper.write_text(
            "#!/bin/sh\ncase \"$1\" in\n*Username*) printf '%s\\n' x-access-token ;;\n*) "
            "printf '%s\\n' \"$MABEL_GIT_ASKPASS_TOKEN\" ;;\nesac\n",
            encoding="utf-8",
        )
        helper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return {
            "PATH": os.defpath,
            "GIT_ASKPASS": str(helper),
            "GIT_TERMINAL_PROMPT": "0",
            "MABEL_GIT_ASKPASS_TOKEN": self._token,
        }

    def __exit__(self, *_: object) -> None:
        if self._directory is not None:
            self._directory.cleanup()


def _make_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
        except OSError:
            continue


def _make_read_only(root: Path) -> None:
    for path in [*root.rglob("*"), root]:
        if path.is_symlink():
            continue
        try:
            path.chmod(path.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
        except OSError:
            continue
