"""Tests for tasks 3.2 + 3.3 of add-mutf7-folder-codec: best-effort APPEND of
every successfully sent draft into the IMAP Sent folder ``寄件備份``.

Covers the delta requirement in
openspec/changes/add-mutf7-folder-codec/specs/outbound-send/spec.md and design
Decision 3 (add-mutf7-folder-codec/design.md): the APPEND runs AFTER SMTP
submission and marker/DB finalization, is strictly best-effort (never fails a
send, never changes the marker state or exit code), requires no IMAP
preflight, and refuses to append when the configured IMAP account does not
match the sending SMTP account.

Both SMTP and the IMAP client are mocked; no real network I/O.
"""

from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import mailattic.outbound as outbound_module
from mailattic.outbound import (
    SendRefused,
    SmtpConfig,
    _marker_path,
    send_batch,
    send_draft,
)
from mailattic.storage import initialize_database

SMTP_USER = "sean3808@mailattic.test"

# The wire form of 寄件備份, per design.md Decision 1/Context (verified live).
SENT_FOLDER_WIRE_NAME = "&W8RO9lCZTv0-"

_MATCHING_IMAP_ENV = {
    "IMAP_HOST": "mail.mailattic.test",
    "IMAP_PORT": "993",
    "IMAP_USER": SMTP_USER,
    "IMAP_PASSWORD": "imap-secret",
}


class _FakeImapClient:
    """Injectable fake standing in for NativeImapClient in the append seam."""

    instances: list[_FakeImapClient] = []

    def __init__(
        self,
        env,
        *,
        raise_on_append: Exception | None = None,
        timeout: float | None = None,
    ) -> None:
        self.env = env
        self._raise_on_append = raise_on_append
        self.timeout = timeout
        self.append_calls: list[tuple[str, bytes]] = []
        self.closed = False
        _FakeImapClient.instances.append(self)

    def append_message(self, folder_wire_name: str, rfc822: bytes) -> None:
        self.append_calls.append((folder_wire_name, rfc822))
        if self._raise_on_append is not None:
            raise self._raise_on_append

    def close(self) -> None:
        self.closed = True


def _reset_fake_client_registry() -> None:
    _FakeImapClient.instances = []


def _patch_imap_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_on_append: Exception | None = None,
) -> None:
    _reset_fake_client_registry()

    def _factory(env, *, timeout: float | None = None):
        return _FakeImapClient(env, raise_on_append=raise_on_append, timeout=timeout)

    monkeypatch.setattr(outbound_module, "NativeImapClient", _factory)


def _write_draft(tmp_path: Path, draft_id: str, message_id: str) -> Path:
    draft_dir = tmp_path / "outbound" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "Client <client@example.com>"
    msg["Subject"] = "Test outbound"
    msg["Message-ID"] = message_id
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello", charset="utf-8")
    draft_path = draft_dir / f"{draft_id}.eml"
    draft_path.write_bytes(msg.as_bytes())
    return draft_path


def _smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.com",
        port=465,
        user=SMTP_USER,
        password="secret",
    )


def _patch_smtp(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_smtp = MagicMock()
    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.return_value.__enter__.return_value = mock_smtp
    monkeypatch.setattr(
        "mailattic.outbound.smtplib.SMTP_SSL",
        mock_smtp_ssl,
    )
    return mock_smtp


def _read_marker_status(db_path: Path, message_id: str) -> str:
    return json.loads(_marker_path(db_path, message_id).read_text(encoding="utf-8"))[
        "status"
    ]


def _auto_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )


# ---------------------------------------------------------------------------
# Scenario 1: happy path -- successful send appends a copy with the Message-ID
# ---------------------------------------------------------------------------


def test_successful_send_appends_copy_carrying_message_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<append-happy@mailattic.test>"
    _write_draft(tmp_path, "draft-happy", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    result = send_draft("draft-happy", db_path, _smtp_config(), env=_MATCHING_IMAP_ENV)

    assert result["message_id"] == message_id
    assert len(_FakeImapClient.instances) == 1
    client = _FakeImapClient.instances[0]
    assert len(client.append_calls) == 1
    folder_wire_name, rfc822 = client.append_calls[0]
    assert folder_wire_name == SENT_FOLDER_WIRE_NAME
    assert message_id.encode() in rfc822
    assert client.closed is True
    assert _read_marker_status(db_path, message_id) == "sent"


# ---------------------------------------------------------------------------
# The append client must be constructed with a bounded (non-None) socket
# timeout, so a server that accepts the TCP connection but never responds
# cannot hang the append -- and therefore a completed send -- forever
# (Codex semantic review, design Decision 3). A real socket-level hang is
# awkward to exercise in a unit test; asserting the timeout argument reaches
# the client constructor is the acceptance criterion's stated fallback.
# ---------------------------------------------------------------------------


def test_append_client_is_constructed_with_a_bounded_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<bounded-timeout@mailattic.test>"
    _write_draft(tmp_path, "draft-timeout", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    send_draft("draft-timeout", db_path, _smtp_config(), env=_MATCHING_IMAP_ENV)

    assert len(_FakeImapClient.instances) == 1
    client = _FakeImapClient.instances[0]
    assert client.timeout is not None
    assert client.timeout == outbound_module._APPEND_IMAP_TIMEOUT_SECONDS


def test_plain_sync_setting_does_not_change_sent_append_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<append-stays-tls@mailattic.test>"
    _write_draft(tmp_path, "draft-append-stays-tls", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)
    plain_sync_env = _MATCHING_IMAP_ENV | {"IMAP_SECURITY": "plain"}

    send_draft(
        "draft-append-stays-tls",
        db_path,
        _smtp_config(),
        env=plain_sync_env,
    )

    assert len(_FakeImapClient.instances) == 1
    assert _FakeImapClient.instances[0].env["IMAP_SECURITY"] == "tls"


# ---------------------------------------------------------------------------
# Scenario 2: IMAP client raises -- send still reported sent, marker still
# sent, exit code unaffected (exercised at the CLI layer separately), warning
# on stderr naming the draft.
# ---------------------------------------------------------------------------


def test_append_failure_does_not_fail_the_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<append-raises@mailattic.test>"
    _write_draft(tmp_path, "draft-append-fail", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch, raise_on_append=RuntimeError("APPEND rejected"))

    result = send_draft(
        "draft-append-fail", db_path, _smtp_config(), env=_MATCHING_IMAP_ENV
    )

    assert result["message_id"] == message_id
    assert _read_marker_status(db_path, message_id) == "sent"
    captured = capsys.readouterr()
    assert "draft-append-fail" in captured.err
    assert "APPEND rejected" in captured.err


# ---------------------------------------------------------------------------
# Scenario 3: env mapping has no IMAP_* keys -- no append attempted, send
# still succeeds, warning printed.
# ---------------------------------------------------------------------------


def test_missing_imap_settings_skips_append_send_still_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<no-imap-settings@mailattic.test>"
    _write_draft(tmp_path, "draft-no-imap", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    smtp_only_env = {"SMTP_HOST": "smtp.example.com"}
    result = send_draft("draft-no-imap", db_path, _smtp_config(), env=smtp_only_env)

    assert result["message_id"] == message_id
    assert _FakeImapClient.instances == []
    captured = capsys.readouterr()
    assert "draft-no-imap" in captured.err


# ---------------------------------------------------------------------------
# #114: ``env`` is required. Without it the mail would still go out but never
# reach the Sent folder -- a silent visibility gap whose stderr warning invites
# a resend. So a missing mapping refuses before SMTP and before any marker.
# ---------------------------------------------------------------------------


def test_send_draft_requires_env_argument() -> None:
    with pytest.raises(TypeError, match="env"):
        send_draft("draft-no-env", "unused.db", _smtp_config())  # type: ignore[call-arg]


def test_send_batch_requires_env_argument() -> None:
    with pytest.raises(TypeError, match="env"):
        send_batch(["draft-no-env"], "unused.db", _smtp_config())  # type: ignore[call-arg]


def test_send_draft_env_none_refused_before_smtp_and_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<env-none-single@mailattic.test>"
    _write_draft(tmp_path, "draft-env-none", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    with pytest.raises(SendRefused, match="env"):
        send_draft("draft-env-none", db_path, _smtp_config(), None)  # type: ignore[arg-type]

    mock_smtp.sendmail.assert_not_called()
    assert not _marker_path(db_path, message_id).exists()


def test_send_batch_env_none_refuses_whole_batch_before_smtp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<env-none-batch@mailattic.test>"
    _write_draft(tmp_path, "draft-env-none-batch", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    with pytest.raises(SendRefused, match="env"):
        send_batch(["draft-env-none-batch"], db_path, _smtp_config(), None)  # type: ignore[arg-type]

    mock_smtp.sendmail.assert_not_called()
    assert not _marker_path(db_path, message_id).exists()


def test_submit_prepared_draft_env_none_refused_before_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared submission step (MCP ``mail_send`` routes here) guards too."""
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<env-none-submit@mailattic.test>"
    _write_draft(tmp_path, "draft-env-none-submit", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    prepared = outbound_module._prepare_draft_for_send("draft-env-none-submit", db_path)

    with pytest.raises(SendRefused, match="env"):
        outbound_module._submit_prepared_draft(
            prepared,
            db_path,
            _smtp_config(),
            None,  # type: ignore[arg-type]
        )

    mock_smtp.sendmail.assert_not_called()
    assert not _marker_path(db_path, message_id).exists()


# ---------------------------------------------------------------------------
# Scenario 4: IMAP_USER names a different account than the SMTP user -- no
# append attempted, mismatch warning, send still succeeds.
# ---------------------------------------------------------------------------


def test_account_mismatch_skips_append_send_still_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<account-mismatch@mailattic.test>"
    _write_draft(tmp_path, "draft-mismatch", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    mismatched_env = {**_MATCHING_IMAP_ENV, "IMAP_USER": "someone-else@mailattic.test"}
    result = send_draft("draft-mismatch", db_path, _smtp_config(), env=mismatched_env)

    assert result["message_id"] == message_id
    assert _FakeImapClient.instances == []
    captured = capsys.readouterr()
    assert "draft-mismatch" in captured.err
    assert "someone-else@mailattic.test" in captured.err


def test_account_match_is_case_insensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<case-insensitive@mailattic.test>"
    _write_draft(tmp_path, "draft-case", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    upper_env = {**_MATCHING_IMAP_ENV, "IMAP_USER": SMTP_USER.upper()}
    send_draft("draft-case", db_path, _smtp_config(), env=upper_env)

    assert len(_FakeImapClient.instances) == 1
    assert len(_FakeImapClient.instances[0].append_calls) == 1


# ---------------------------------------------------------------------------
# Scenario 5: a draft whose SMTP submission fails is never appended.
# ---------------------------------------------------------------------------


def test_smtp_failure_is_never_appended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<smtp-fail-no-append@mailattic.test>"
    _write_draft(tmp_path, "draft-smtp-fail", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    mock_smtp.sendmail.side_effect = smtplib.SMTPException("boom")
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    with pytest.raises(SendRefused, match="SMTP submission failed"):
        send_draft("draft-smtp-fail", db_path, _smtp_config(), env=_MATCHING_IMAP_ENV)

    assert _FakeImapClient.instances == []


# ---------------------------------------------------------------------------
# No IMAP preflight: a bad/absent IMAP config must never block SMTP submission
# itself, i.e. SMTP is attempted even though the IMAP settings are unusable.
# ---------------------------------------------------------------------------


def test_no_imap_preflight_smtp_still_attempted_with_bad_imap_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<no-preflight@mailattic.test>"
    _write_draft(tmp_path, "draft-no-preflight", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    bad_imap_env = {"IMAP_HOST": "", "IMAP_USER": "", "IMAP_PASSWORD": ""}
    result = send_draft("draft-no-preflight", db_path, _smtp_config(), env=bad_imap_env)

    mock_smtp.sendmail.assert_called_once()
    assert result["message_id"] == message_id
    assert _FakeImapClient.instances == []


# ---------------------------------------------------------------------------
# Batch isolation: send_batch appends each successfully sent draft
# independently; an append failure on one draft must not affect the others.
# ---------------------------------------------------------------------------


def test_send_batch_appends_each_draft_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    mids = {
        "draft-a": "<batch-append-a@mailattic.test>",
        "draft-b": "<batch-append-b@mailattic.test>",
        "draft-c": "<batch-append-c@mailattic.test>",
    }
    for draft_id, mid in mids.items():
        _write_draft(tmp_path, draft_id, mid)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    monkeypatch.setattr(
        outbound_module, "_wait_for_batch_interval", lambda _seconds: None
    )

    _reset_fake_client_registry()

    def _factory(env, *, timeout: float | None = None):
        client = _FakeImapClient(env, timeout=timeout)
        if len(_FakeImapClient.instances) == 1:
            # First draft submitted (draft-a) raises on append.
            client._raise_on_append = RuntimeError("append boom for draft-a")
        return client

    monkeypatch.setattr(outbound_module, "NativeImapClient", _factory)

    results = send_batch(
        list(mids.keys()), db_path, _smtp_config(), env=_MATCHING_IMAP_ENV
    )

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-a"]["outcome"] == "sent"
    assert by_id["draft-b"]["outcome"] == "sent"
    assert by_id["draft-c"]["outcome"] == "sent"
    # Every sendable draft still attempted its own append.
    assert len(_FakeImapClient.instances) == 3


# ---------------------------------------------------------------------------
# The send marker / _PostSubmitError contract is unchanged: append only
# happens after finalization, so a persist/marker finalization failure must
# never reach the append step.
# ---------------------------------------------------------------------------


def test_persist_failure_never_reaches_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<persist-fail-no-append@mailattic.test>"
    _write_draft(tmp_path, "draft-persist-fail", message_id)
    _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    monkeypatch.setattr(
        "mailattic.outbound.persist_message",
        MagicMock(side_effect=RuntimeError("db boom")),
    )

    from mailattic.outbound import _PostSubmitError

    with pytest.raises(_PostSubmitError):
        send_draft(
            "draft-persist-fail", db_path, _smtp_config(), env=_MATCHING_IMAP_ENV
        )

    assert _FakeImapClient.instances == []
    assert _read_marker_status(db_path, message_id) == "sending"


# ---------------------------------------------------------------------------
# Serialization is inside the best-effort boundary: a failure to serialize
# the message for APPEND must never fail the send (Codex semantic review,
# Fix 3). Regression for the bug where `rfc822=msg.as_bytes()` was evaluated
# BEFORE the try/except in `_submit_prepared_draft`, so a serialization
# exception propagated after SMTP submission, DB persist, and the marker
# write had already completed.
# ---------------------------------------------------------------------------


def test_serialization_failure_does_not_fail_the_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<serialize-fail@mailattic.test>"
    _write_draft(tmp_path, "draft-serialize-fail", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    _auto_authorize(monkeypatch)
    _patch_imap_client(monkeypatch)

    original_as_bytes = EmailMessage.as_bytes

    def _raising_as_bytes(self, *args, **kwargs):
        if mock_smtp.sendmail.called and str(self.get("Message-ID", "")) == message_id:
            raise RuntimeError("serialization boom")
        return original_as_bytes(self, *args, **kwargs)

    monkeypatch.setattr(EmailMessage, "as_bytes", _raising_as_bytes)

    result = send_draft(
        "draft-serialize-fail", db_path, _smtp_config(), env=_MATCHING_IMAP_ENV
    )

    assert result["message_id"] == message_id
    assert _read_marker_status(db_path, message_id) == "sent"
    # Serialization failed before the IMAP client was ever constructed.
    assert _FakeImapClient.instances == []
    captured = capsys.readouterr()
    assert "draft-serialize-fail" in captured.err
    assert "serialization boom" in captured.err
