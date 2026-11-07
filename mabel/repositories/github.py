"""Trusted GitHub repository verification; no Git execution lives here."""

import re
from typing import Protocol

import httpx

from mabel.repositories.credentials import GitHubTokenCredential
from mabel.repositories.models import RepositoryVerification


class RepositoryProviderError(RuntimeError):
    error_code = "provider_unavailable"


class RepositoryAuthenticationError(RepositoryProviderError):
    error_code = "authentication_failed"


class RepositoryNotFoundError(RepositoryProviderError):
    error_code = "repository_not_found"


class RepositoryResponseError(RepositoryProviderError):
    error_code = "invalid_provider_response"


class RepositoryProvider(Protocol):
    async def verify(
        self, locator: str, credential: GitHubTokenCredential
    ) -> RepositoryVerification: ...


class GitHubRepositoryAdapter:
    """Verify one GitHub repository through the provider API only."""

    _API_VERSION = "2022-11-28"

    async def verify(
        self, locator: str, credential: GitHubTokenCredential
    ) -> RepositoryVerification:
        owner, repository = parse_github_locator(locator)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repository}",
                    headers={
                        "Authorization": f"Bearer {credential.token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": self._API_VERSION,
                        "User-Agent": "MABEL",
                    },
                )
        except httpx.HTTPError as error:
            raise RepositoryProviderError("GitHub could not be reached.") from error
        if response.status_code in {401, 403}:
            raise RepositoryAuthenticationError("GitHub rejected repository access.")
        if response.status_code == 404:
            raise RepositoryNotFoundError("GitHub could not find this repository.")
        if response.status_code != 200:
            raise RepositoryProviderError("GitHub could not verify this repository.")
        try:
            data = response.json()
        except ValueError as error:
            raise RepositoryResponseError("GitHub returned an invalid repository response.") from error
        repository_id = data.get("id")
        canonical_locator = data.get("full_name")
        default_branch = data.get("default_branch")
        if (
            not isinstance(repository_id, int)
            or not isinstance(canonical_locator, str)
            or not isinstance(default_branch, str)
            or not canonical_locator
            or not default_branch
        ):
            raise RepositoryResponseError("GitHub returned an invalid repository response.")
        return RepositoryVerification(str(repository_id), canonical_locator, default_branch)


def parse_github_locator(value: str) -> tuple[str, str]:
    parts = value.strip().strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise RepositoryResponseError("Repository must be formatted as owner/repository.")
    return parts[0], parts[1]
