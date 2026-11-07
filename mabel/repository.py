"""Repository access for MABEL's durable configuration."""

from pathlib import Path, PurePosixPath
from typing import Protocol


class RepositoryError(RuntimeError):
    """Base error for repository access failures."""


class RepositoryPathError(RepositoryError):
    """Raised when a path falls outside the configured repository root."""


class RepositoryFileNotFoundError(RepositoryError):
    """Raised when a requested repository file is absent."""


class Repository(Protocol):
    """The read-only file access MABEL configuration services require."""

    def read_text(self, path: PurePosixPath) -> str:
        """Read a UTF-8 file addressed relative to the repository root."""

    def is_directory(self, path: PurePosixPath) -> bool:
        """Return whether an exact safe repository-relative directory exists."""


class LocalRepository:
    """Read durable MABEL files from one local repository root."""

    def __init__(self, root: Path) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise RepositoryError(f"Repository root is not a directory: {resolved_root}")
        self._root = resolved_root

    def read_text(self, path: PurePosixPath) -> str:
        """Read a repository-relative UTF-8 text file without path escape."""
        candidate = self.resolve_path(path)

        try:
            return candidate.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise RepositoryFileNotFoundError(f"Repository file not found: {path}") from error

    def is_directory(self, path: PurePosixPath) -> bool:
        return self.resolve_path(path).is_dir()

    def resolve_path(self, path: PurePosixPath) -> Path:
        """Resolve one safe repository-relative path for trusted local execution setup."""
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise RepositoryPathError(f"Path must remain inside the repository: {path}")
        candidate = (self._root / Path(*path.parts)).resolve()
        if not candidate.is_relative_to(self._root):
            raise RepositoryPathError(f"Path must remain inside the repository: {path}")
        return candidate
