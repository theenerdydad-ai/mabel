"""Controlled mapping from AMIE's immutable user ID to MABEL's owner boundary."""

from uuid import UUID

from mabel.state.owner_identities import SqliteOwnerIdentityStore


class UnmappedAmieIdentityError(RuntimeError):
    """Raised instead of claiming an owner from browser-controlled input."""


class AmieOwnerMappingService:
    """Apply operator-configured bindings; browser flows can only resolve them."""

    provider = "amie"

    def __init__(self, store: SqliteOwnerIdentityStore, configured_bindings: dict[str, str] | None) -> None:
        self._store = store
        self._bindings = configured_bindings or {}

    def configure(self) -> None:
        for subject, owner_id in self._bindings.items():
            self._validate_subject(subject)
            if not owner_id.strip():
                raise ValueError("MABEL AMIE owner bindings require a non-empty MABEL owner ID.")
            self._store.bind(self.provider, subject, owner_id)

    def resolve(self, subject: str) -> str:
        self._validate_subject(subject)
        owner_id = self._store.owner_for(self.provider, subject)
        if owner_id is None:
            raise UnmappedAmieIdentityError("This AMIE account is not bound to a MABEL owner.")
        return owner_id

    def subject_for_owner(self, owner_id: str) -> str:
        """Return the ACE UUID explicitly bound to a MABEL owner.

        This reverse lookup is intentionally used only by trusted runtime
        handshakes. It prevents a bearer token from being treated as proof of
        an arbitrary ACE account relationship.
        """
        subject = self._store.subject_for(self.provider, owner_id)
        if subject is None:
            raise UnmappedAmieIdentityError("This MABEL owner is not bound to an AMIE account.")
        self._validate_subject(subject)
        return subject

    @staticmethod
    def _validate_subject(subject: str) -> None:
        try:
            UUID(subject)
        except ValueError as error:
            raise ValueError("AMIE identity subjects must be immutable UUID user IDs.") from error
