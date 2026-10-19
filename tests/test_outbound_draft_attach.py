"""Tests for draft-reply --attach and the send-draft attachment preview.

Covers openspec/changes/add-draft-reply-attach/specs/outbound-send/spec.md.
"""

from __future__ import annotations

import hashlib
import mimetypes
import pathlib
import smtplib
from email import message_from_bytes, policy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mailattic.cli import build_parser
from mailattic.models import PstSource, build_normalized_message
from mailattic.outbound import SmtpConfig, send_draft
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)
from tests.test_outbound_draft import PLACEHOLDER_SMTP_SETTINGS

SMTP_USER = "sean3808@mailattic.test"
ORIGINAL_MESSAGE_ID = "<test-msg-123@mailattic.test>"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _seed_message(tmp_path: Path) -> Path:
    db_path = initialize_database(tmp_path / "evidence.db")
    message = build_normalized_message(
        {
            "subject": "Test subject",
            "sender": {"name": None, "email": "client@example.com"},
            "to": [{"name": None, "email": "sean3808@mailattic.test"}],
            "message_id": ORIGINAL_MESSAGE_ID,
            "references": None,
            "body_text": "original body",
        },
        source=PstSource(
            path=Path("C:/archive/mailbox.pst"),
            input_mode="file",
            requested_from=Path("C:/archive/mailbox.pst"),
        ),
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(db_path)
    try:
        persist_message(connection, message)
        connection.commit()
    finally:
        connection.close()
    return db_path


def _draft_reply_args(
    tmp_path: Path,
    db_path: Path,
    body_file: Path,
    attach_paths: list[Path] | None = None,
):
    # Just enough settings to name the sending account (the draft's From);
    # these tests are unrelated to SMTP config resolution.
    env_file = tmp_path / "placeholder.env"
    if not env_file.exists():
        env_file.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    argv = [
        "draft-reply",
        ORIGINAL_MESSAGE_ID,
        "--body-from",
        str(body_file),
        "--database",
        str(db_path),
        "--env-file",
        str(env_file),
    ]
    for attach_path in attach_paths or []:
        argv.extend(["--attach", str(attach_path)])
    return build_parser().parse_args(argv)


def _draft_dir(tmp_path: Path) -> Path:
    return tmp_path / "outbound" / "drafts"


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


# ---------------------------------------------------------------------------
# Scenario 1: single attachment -> multipart/mixed, headers unchanged
# ---------------------------------------------------------------------------


def test_single_attachment_produces_multipart_mixed_with_matching_headers(
    tmp_path: Path,
) -> None:
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline_db = _seed_message(baseline_dir)
    baseline_body = baseline_dir / "body.txt"
    baseline_body.write_text("My reply text", encoding="utf-8")
    baseline_args = _draft_reply_args(baseline_dir, baseline_db, baseline_body)
    assert _handle_draft_reply(baseline_args) == 0
    baseline_eml = next(_draft_dir(baseline_dir).glob("*.eml"))
    baseline_parsed = message_from_bytes(
        baseline_eml.read_bytes(), policy=policy.default
    )

    attach_dir = tmp_path / "attach"
    attach_dir.mkdir()
    attach_db = _seed_message(attach_dir)
    attach_body = attach_dir / "body.txt"
    attach_body.write_text("My reply text", encoding="utf-8")
    report_pdf = attach_dir / "report.pdf"
    report_pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    attach_args = _draft_reply_args(attach_dir, attach_db, attach_body, [report_pdf])
    assert _handle_draft_reply(attach_args) == 0
    attach_eml = next(_draft_dir(attach_dir).glob("*.eml"))
    attach_parsed = message_from_bytes(attach_eml.read_bytes(), policy=policy.default)

    assert attach_parsed.get_content_type() == "multipart/mixed"

    for header in ("To", "Cc", "In-Reply-To", "References", "Bcc", "Subject"):
        assert attach_parsed[header] == baseline_parsed[header]

    attachment_parts = list(attach_parsed.iter_attachments())
    assert len(attachment_parts) == 1
    attachment = attachment_parts[0]
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == "report.pdf"
    assert attachment.get_content_disposition() == "attachment"

    body_part = attach_parsed.get_body(preferencelist=("plain",))
    assert body_part is not None
    assert "My reply text" in body_part.get_content()


# ---------------------------------------------------------------------------
# Scenario 2: non-ASCII filename round-trips without mojibake
# ---------------------------------------------------------------------------


def test_non_ascii_attachment_filename_round_trips_without_mojibake(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    filename = "甲公司 Q-TEST-001 INV-0042.pdf"
    attachment_path = tmp_path / filename
    attachment_path.write_bytes(b"%PDF-1.4 fake pdf content")

    args = _draft_reply_args(tmp_path, db_path, body_file, [attachment_path])
    assert _handle_draft_reply(args) == 0

    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    parsed = message_from_bytes(eml_path.read_bytes(), policy=policy.default)
    attachment_parts = list(parsed.iter_attachments())

    assert len(attachment_parts) == 1
    assert attachment_parts[0].get_filename() == filename


# ---------------------------------------------------------------------------
# Scenario 3: multiple attachments all included with correct MIME types
# ---------------------------------------------------------------------------


def test_multiple_attachments_all_present_with_correct_mime_types(
    tmp_path: Path,
    blind_host_mime_map: None,
) -> None:
    # The host extension map is blinded for the whole case. Without this the
    # assertion below is satisfied by the sending machine having Office
    # installed rather than by anything this repository does -- which is the
    # defect in #65, not an incidental detail of the fixture.
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    a_pdf = tmp_path / "a.pdf"
    a_pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    b_xlsx = tmp_path / "b.xlsx"
    b_xlsx.write_bytes(b"fake xlsx bytes")

    args = _draft_reply_args(tmp_path, db_path, body_file, [a_pdf, b_xlsx])
    assert _handle_draft_reply(args) == 0

    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    parsed = message_from_bytes(eml_path.read_bytes(), policy=policy.default)
    attachment_parts = {part.get_filename(): part for part in parsed.iter_attachments()}

    assert set(attachment_parts) == {"a.pdf", "b.xlsx"}
    assert attachment_parts["a.pdf"].get_content_type() == "application/pdf"
    assert (
        attachment_parts["b.xlsx"].get_content_type()
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ---------------------------------------------------------------------------
# Scenario 3b: a pinned type survives a host that knows nothing about it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected_content_type"),
    [
        (
            "quotation.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("presentation.ppt", "application/vnd.ms-powerpoint"),
    ],
)
def test_pinned_content_type_survives_a_host_without_office(
    tmp_path: Path,
    blind_host_mime_map: None,
    filename: str,
    expected_content_type: str,
) -> None:
    """The end-to-end half of the #65 guarantee.

    The unit cases in tests/test_outbound_attachment_mime_table.py assert the
    resolver; this asserts the bytes that would actually go over the wire, on
    a machine whose extension map answers nothing -- the Windows Server 2025
    state measured in the #60 spike.
    """
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    attachment_path = tmp_path / filename
    attachment_path.write_bytes(b"fake attachment bytes")

    args = _draft_reply_args(tmp_path, db_path, body_file, [attachment_path])
    assert _handle_draft_reply(args) == 0

    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    parsed = message_from_bytes(eml_path.read_bytes(), policy=policy.default)
    attachment_parts = list(parsed.iter_attachments())

    assert len(attachment_parts) == 1
    assert attachment_parts[0].get_filename() == filename
    assert attachment_parts[0].get_content_type() == expected_content_type


# ---------------------------------------------------------------------------
# Scenario 4: unknown extension falls back to application/octet-stream
# ---------------------------------------------------------------------------


def test_unknown_extension_falls_back_to_octet_stream(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    # Use an extension mimetypes.guess_type() cannot map (returns None) so the
    # code's application/octet-stream fallback branch is actually exercised.
    # (".bin" would NOT test the fallback — Python maps it to octet-stream directly.)
    unknown = tmp_path / "data.q7z"
    assert mimetypes.guess_type(unknown.name)[0] is None
    unknown.write_bytes(b"\x00\x01\x02binary")

    args = _draft_reply_args(tmp_path, db_path, body_file, [unknown])
    assert _handle_draft_reply(args) == 0

    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    parsed = message_from_bytes(eml_path.read_bytes(), policy=policy.default)
    attachment_parts = list(parsed.iter_attachments())

    assert len(attachment_parts) == 1
    assert attachment_parts[0].get_content_type() == "application/octet-stream"


# ---------------------------------------------------------------------------
# Scenario 5: missing attachment file aborts with a non-zero exit
# ---------------------------------------------------------------------------


def test_missing_attachment_file_exits_2_and_writes_no_eml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    missing_path = tmp_path / "missing.pdf"

    args = _draft_reply_args(tmp_path, db_path, body_file, [missing_path])
    exit_code = _handle_draft_reply(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert str(missing_path) in captured.err
    draft_dir = _draft_dir(tmp_path)
    assert not draft_dir.exists() or not list(draft_dir.glob("*.eml"))


# ---------------------------------------------------------------------------
# Scenario 6: directory path / mocked PermissionError abort with no draft
# ---------------------------------------------------------------------------


def test_directory_attachment_exits_2_and_writes_no_eml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    somedir = tmp_path / "somedir"
    somedir.mkdir()

    args = _draft_reply_args(tmp_path, db_path, body_file, [somedir])
    exit_code = _handle_draft_reply(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert str(somedir) in captured.err
    draft_dir = _draft_dir(tmp_path)
    assert not draft_dir.exists() or not list(draft_dir.glob("*.eml"))


def test_unreadable_attachment_permission_error_exits_2_and_writes_no_eml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    locked_pdf = tmp_path / "locked.pdf"
    locked_pdf.write_bytes(b"%PDF-1.4 fake pdf content")

    original_read_bytes = pathlib.Path.read_bytes

    def _read_bytes_maybe_denied(self: pathlib.Path) -> bytes:
        if self.name == "locked.pdf":
            raise PermissionError(f"simulated permission error: {self}")
        return original_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _read_bytes_maybe_denied)

    args = _draft_reply_args(tmp_path, db_path, body_file, [locked_pdf])
    exit_code = _handle_draft_reply(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert str(locked_pdf) in captured.err
    draft_dir = _draft_dir(tmp_path)
    assert not draft_dir.exists() or not list(draft_dir.glob("*.eml"))


# ---------------------------------------------------------------------------
# Scenario 7: a later unreadable attachment leaves no partial draft
# ---------------------------------------------------------------------------


def test_later_unreadable_attachment_leaves_no_partial_draft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    good_pdf = tmp_path / "good.pdf"
    good_pdf.write_bytes(b"%PDF-1.4 good pdf content")
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 bad pdf content")

    original_read_bytes = pathlib.Path.read_bytes

    def _read_bytes_maybe_denied(self: pathlib.Path) -> bytes:
        if self.name == "bad.pdf":
            raise OSError(f"simulated read failure: {self}")
        return original_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _read_bytes_maybe_denied)

    args = _draft_reply_args(tmp_path, db_path, body_file, [good_pdf, bad_pdf])
    exit_code = _handle_draft_reply(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert str(bad_pdf) in captured.err
    draft_dir = _draft_dir(tmp_path)
    assert not draft_dir.exists() or not list(draft_dir.glob("*.eml"))


def test_is_file_oserror_aborts_cleanly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Path.is_file() itself can raise OSError (e.g. an inaccessible parent dir
    # or network share). That must produce the same exit-2 / named-error /
    # no-partial-draft contract, not an unhandled traceback.
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    blocked = tmp_path / "blocked.pdf"
    blocked.write_bytes(b"%PDF-1.4 content")

    original_is_file = pathlib.Path.is_file

    def _is_file_maybe_denied(self: pathlib.Path) -> bool:
        if self.name == "blocked.pdf":
            raise OSError(f"simulated stat failure: {self}")
        return original_is_file(self)

    monkeypatch.setattr(pathlib.Path, "is_file", _is_file_maybe_denied)

    args = _draft_reply_args(tmp_path, db_path, body_file, [blocked])
    exit_code = _handle_draft_reply(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert str(blocked) in captured.err
    draft_dir = _draft_dir(tmp_path)
    assert not draft_dir.exists() or not list(draft_dir.glob("*.eml"))


# ---------------------------------------------------------------------------
# Scenario 8: send-draft confirmation preview lists attachment count/names
# ---------------------------------------------------------------------------


def test_send_draft_preview_lists_attachment_count_and_filenames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    report_pdf = tmp_path / "report.pdf"
    report_pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    data_xlsx = tmp_path / "data.xlsx"
    data_xlsx.write_bytes(b"fake xlsx bytes")

    draft_args = _draft_reply_args(
        tmp_path, db_path, body_file, [report_pdf, data_xlsx]
    )
    assert _handle_draft_reply(draft_args) == 0

    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    draft_id = eml_path.stem

    captured_summaries: list[str] = []

    def _fake_authorization(summary: str) -> str:
        captured_summaries.append(summary)
        return "YES"

    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        _fake_authorization,
    )
    mock_smtp = _patch_smtp(monkeypatch)

    result = send_draft(draft_id, db_path, _smtp_config(), {})

    assert result["draft_id"] == draft_id
    assert len(captured_summaries) == 1
    summary = captured_summaries[0]
    assert "Attachments: 2" in summary
    assert "report.pdf" in summary
    assert "data.xlsx" in summary

    mock_smtp.sendmail.assert_called_once()
    sent_bytes = mock_smtp.sendmail.call_args.args[2]
    sent_msg = message_from_bytes(sent_bytes, policy=policy.default)
    sent_attachment_names = {
        part.get_filename() for part in sent_msg.iter_attachments()
    }
    assert sent_attachment_names == {"report.pdf", "data.xlsx"}

    body = sent_msg.get_body(preferencelist=("plain",))
    assert body is not None
    assert "My reply text" in body.get_content()

    connection = connect_database(db_path)
    try:
        rows = connection.execute(
            "SELECT attachment_name FROM message_attachments ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 2
    assert {r["attachment_name"] for r in rows} == {"report.pdf", "data.xlsx"}


# ---------------------------------------------------------------------------
# #195: draft-reply stdout carries what the user reviews before a fingerprint send
# ---------------------------------------------------------------------------


def test_draft_reply_stdout_lists_attachment_sizes_and_file_fingerprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    report_pdf = tmp_path / "報價單.pdf"
    report_pdf.write_bytes(b"%PDF-1.4 fake pdf content")

    args = _draft_reply_args(tmp_path, db_path, body_file, [report_pdf])
    assert _handle_draft_reply(args) == 0

    out = capsys.readouterr().out
    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    assert f"draft_id: {eml_path.stem}" in out
    assert "Attachments: 1\n  - 報價單.pdf (25 bytes)\n" in out
    assert f"fingerprint: {hashlib.sha256(eml_path.read_bytes()).hexdigest()}\n" in out


# #199: the sending account must be visible at review time, before Subject.
def test_draft_reply_stdout_shows_from_before_subject(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")

    args = _draft_reply_args(tmp_path, db_path, body_file)
    assert _handle_draft_reply(args) == 0

    out = capsys.readouterr().out
    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    raw = eml_path.read_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    assert f"From: {parsed['From']}\n" in out
    assert out.index("From:") < out.index("Subject:")


def test_fingerprint_send_submits_exactly_the_reviewed_draft_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("您好，附上報價單，請查收。\n" * 40, encoding="utf-8")
    quote = tmp_path / "甲公司 Q-TEST-001 報價單.pdf"
    quote.write_bytes(bytes(range(256)) * 64)
    args = _draft_reply_args(tmp_path, db_path, body_file, [quote])
    assert _handle_draft_reply(args) == 0
    fingerprint = next(
        line.removeprefix("fingerprint: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("fingerprint: ")
    )
    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    mock_smtp = _patch_smtp(monkeypatch)

    send_draft(eml_path.stem, db_path, _smtp_config(), {}, fingerprint=fingerprint)

    mock_smtp.sendmail.assert_called_once()
    sent_bytes = mock_smtp.sendmail.call_args.args[2]
    assert sent_bytes == _strip_bcc_and_use_crlf(eml_path.read_bytes())


class _WireCapturingSMTP(smtplib.SMTP):
    """Runs real SMTP serialisation; only the network is cut.

    ``sendmail`` receives the flattened message before ``data()`` applies
    transport-only dot-stuffing, which the server reverses.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__()  # no host -> no connection
        self.wire: list[bytes] = []

    def login(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        return (235, b"ok")

    def ehlo_or_helo_if_needed(self) -> None:
        return None

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):  # type: ignore[override]
        self.wire.append(msg)
        return {}


def _strip_bcc_and_use_crlf(raw: bytes) -> bytes:
    head, sep, body = raw.replace(b"\r\n", b"\n").partition(b"\n\n")
    kept: list[bytes] = []
    dropping = False
    for line in head.split(b"\n"):
        if line[:1] not in (b" ", b"\t"):
            dropping = line.lower().startswith(b"bcc:")
        if not dropping:
            kept.append(line)
    return (b"\n".join(kept) + sep + body).replace(b"\n", b"\r\n")


def test_fingerprint_send_wire_payload_is_draft_bytes_minus_bcc_with_crlf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("您好，附上報價單，請查收。\n" * 40, encoding="utf-8")
    quote = tmp_path / "甲公司 Q-TEST-001 報價單.pdf"
    quote.write_bytes(bytes(range(256)) * 64)
    args = _draft_reply_args(tmp_path, db_path, body_file, [quote])
    assert _handle_draft_reply(args) == 0
    fingerprint = next(
        line.removeprefix("fingerprint: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("fingerprint: ")
    )
    eml_path = next(_draft_dir(tmp_path).glob("*.eml"))
    raw = eml_path.read_bytes()
    assert b"\nBcc:" in raw.replace(b"\r\n", b"\n")
    smtp = _WireCapturingSMTP()
    monkeypatch.setattr(
        "mailattic.outbound.smtplib.SMTP_SSL",
        lambda *args, **kwargs: smtp,
    )

    send_draft(eml_path.stem, db_path, _smtp_config(), {}, fingerprint=fingerprint)

    assert smtp.wire == [_strip_bcc_and_use_crlf(raw)]


def test_draft_reply_stdout_escapes_control_characters_in_attachment_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    # C1 CSI (U+009B) is legal in an NTFS file name, unlike C0 controls.
    hostile = tmp_path / "invoice\u009b2J.pdf"
    hostile.write_bytes(b"x")

    args = _draft_reply_args(tmp_path, db_path, body_file, [hostile])
    assert _handle_draft_reply(args) == 0

    out = capsys.readouterr().out
    assert "\u009b" not in out
    assert "  - invoice\\x9b2J.pdf (1 bytes)\n" in out


def test_draft_reply_stdout_says_no_attachments(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")

    assert _handle_draft_reply(_draft_reply_args(tmp_path, db_path, body_file)) == 0

    assert "Attachments: none\n" in capsys.readouterr().out
