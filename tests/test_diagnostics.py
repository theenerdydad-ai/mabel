import logging
from pathlib import Path

from mabel.diagnostics import log_capability_diagnostic


def test_capability_diagnostics_redact_credentials_and_keep_projection_identity(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="mabel.diagnostics"):
        log_capability_diagnostic(
            "get_capability_index",
            "context_discovery_load",
            RuntimeError(
                "Bearer live-token password=mail-secret https://user:git-secret@example.test ghp_exampletoken"
            ),
            commit_sha="a" * 40,
            context_root=Path("/var/lib/mabel/context/checkout"),
        )

    record = caplog.records[0].message
    assert "get_capability_index" in record
    assert "context_discovery_load" in record
    assert "RuntimeError" in record
    assert "a" * 40 in record
    assert "/var/lib/mabel/context/checkout" in record
    assert "live-token" not in record
    assert "mail-secret" not in record
    assert "git-secret" not in record
    assert "ghp_exampletoken" not in record
