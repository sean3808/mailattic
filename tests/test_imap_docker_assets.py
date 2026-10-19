from __future__ import annotations

from pathlib import Path

import pytest

from mailattic.imap_runtime import (
    render_mbsyncrc,
    validate_sync_environment,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_mbsync_dockerfile_installs_runtime_dependencies() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.mbsync").read_text(
        encoding="utf-8"
    )

    assert "python:3.13-slim-bookworm" in dockerfile
    assert "cron" in dockerfile
    assert "gettext-base" in dockerfile
    assert "isync" in dockerfile
    assert "COPY entrypoint.sh" in dockerfile
    assert "COPY run_sync.py" in dockerfile


def test_default_dockerfile_matches_primary_definition() -> None:
    primary = (REPO_ROOT / "docker" / "Dockerfile.mbsync").read_text(encoding="utf-8")
    alias = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert alias == primary


def test_compose_uses_account_env_file_and_maildir_work_root() -> None:
    compose_text = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert "../.env.sean3808" in compose_text
    assert "/workspace/.mailattic-work/maildir" in compose_text
    assert "../.mailattic-work:/workspace/.mailattic-work" in compose_text
    assert 'restart: "no"' in compose_text


def test_render_mbsyncrc_stays_read_only() -> None:
    template = (REPO_ROOT / "docker" / "mbsyncrc.template").read_text(encoding="utf-8")
    rendered = render_mbsyncrc(
        template,
        env={
            "IMAP_HOST": "mail.mailattic.test",
            "IMAP_PORT": "993",
            "IMAP_USER": "sean3808@mailattic.test",
            "IMAP_PASSWORD": "placeholder",
            "IMAP_ACCOUNT_NAME": "sean3808",
        },
        maildir_root=Path("/workspace/.mailattic-work/maildir"),
        folder="Sent",
    )

    assert "Expunge None" in rendered
    assert "Sync Pull" in rendered
    assert 'Patterns "Sent"' in rendered
    assert "Sync Push" not in rendered
    assert "/workspace/.mailattic-work/maildir/sean3808/" in rendered


def test_missing_password_is_rejected_before_startup() -> None:
    with pytest.raises(ValueError, match="IMAP_PASSWORD"):
        validate_sync_environment(
            {
                "IMAP_HOST": "mail.mailattic.test",
                "IMAP_PORT": "993",
                "IMAP_USER": "sean3808@mailattic.test",
                "IMAP_ACCOUNT_NAME": "sean3808",
            }
        )
