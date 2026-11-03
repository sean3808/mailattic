from __future__ import annotations

import email
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mailattic.cli import build_parser
from mailattic.outbound import (
    SendRefused,
    SmtpConfig,
    _marker_path,
    draft_fingerprint,
    send_draft,
)
from mailattic.outbound_commands import _handle_draft_new
from mailattic.storage import initialize_database

SMTP_USER = "sean3808@mailattic.test"


class _FakeStdin:
    def __init__(self, line: str, *, tty: bool) -> None:
        self._line = line
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._line


class _FakeStdout:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def test_new_thread_draft_fingerprint_mismatch_refused_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    env_file = tmp_path / "smtp.env"
    env_file.write_text(
        "SMTP_HOST=smtp.example.com\n"
        "SMTP_PORT=465\n"
        f"SMTP_USER={SMTP_USER}\n"
        "SMTP_PASSWORD=secret\n",
        encoding="utf-8",
    )
    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello from new thread", encoding="utf-8")

    parser = build_parser()
    draft_args = parser.parse_args(
        [
            "draft-new",
            "--to",
            "Client <client@example.com>",
            "--subject",
            "New Topic Subject",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )

    exit_code = _handle_draft_new(draft_args)
    assert exit_code == 0

    (draft_path,) = (tmp_path / "outbound" / "drafts").glob("*.eml")
    draft_id = draft_path.stem
    initial_bytes = draft_path.read_bytes()
    reviewed = draft_fingerprint(initial_bytes)

    # Tamper with the draft bytes after review
    draft_path.write_bytes(
        initial_bytes.replace(b"New Topic Subject", b"Altered Topic Subject")
    )

    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("YES\n", tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=True))

    msg = email.message_from_bytes(draft_path.read_bytes())
    message_id = str(msg["Message-ID"])

    smtp_config = SmtpConfig(
        host="smtp.example.com",
        port=465,
        user=SMTP_USER,
        password="secret",
    )
    env = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "465",
        "SMTP_USER": SMTP_USER,
        "SMTP_PASSWORD": "secret",
    }

    with pytest.raises(SendRefused, match="fingerprint mismatch"):
        send_draft(draft_id, db_path, smtp_config, env, fingerprint=reviewed)

    mock_smtp_ssl.assert_not_called()
    assert not _marker_path(db_path, message_id).exists()
