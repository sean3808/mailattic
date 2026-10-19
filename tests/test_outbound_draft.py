from __future__ import annotations

from email.header import decode_header, make_header
from email.parser import BytesParser
from pathlib import Path

import pytest

from mailattic.cli import build_parser
from mailattic.identity import add_self_address
from mailattic.models import PstSource, build_normalized_message
from mailattic.outbound import (
    build_thread_headers,
    generate_draft_message_id,
    reply_all_recipients,
)
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)


def test_reply_all_inbound_sender_to_and_cc_excludes_self() -> None:
    original = {
        "sender_name": None,
        "sender_email": "client@example.com",
        "to": [{"name": None, "email": "sean3808@mailattic.test"}],
        "cc": [{"name": None, "email": "colleague@example.com"}],
    }

    recipients = reply_all_recipients(original, {"sean3808@mailattic.test"})

    assert recipients["to"] == [(None, "client@example.com")]
    assert recipients["cc"] == [(None, "colleague@example.com")]


def test_reply_all_deduplicates_and_excludes_self() -> None:
    original = {
        "sender_name": None,
        "sender_email": "ext0@example.com",
        "to": [
            {"name": None, "email": "ext1@example.com"},
            {"name": None, "email": "ext2@example.com"},
            {"name": None, "email": "ext3@example.com"},
            {"name": None, "email": "ext4@example.com"},
            {"name": None, "email": "ext5@example.com"},
            {"name": None, "email": "sean3808@mailattic.test"},
        ],
        "cc": [
            {"name": None, "email": "ext6@example.com"},
            {"name": None, "email": "ext7@example.com"},
            {"name": None, "email": "ext8@example.com"},
            {"name": None, "email": "ext9@example.com"},
            {"name": None, "email": "EXT1@example.com"},
            {"name": None, "email": "SEAN3808@mailattic.test"},
        ],
    }

    recipients = reply_all_recipients(original, {"sean3808@mailattic.test"})
    emails = [email for bucket in ("to", "cc") for _, email in recipients[bucket]]

    assert len(emails) == 10
    assert len({email.casefold() for email in emails}) == 10
    assert "sean3808@mailattic.test" not in {email.casefold() for email in emails}


def test_reply_all_self_sent_excludes_sender_self() -> None:
    original = {
        "sender_name": None,
        "sender_email": "sean3808@mailattic.test",
        "to": [{"name": None, "email": "ext1@example.com"}],
        "cc": [{"name": None, "email": "ext2@example.com"}],
    }

    recipients = reply_all_recipients(original, {"sean3808@mailattic.test"})

    assert recipients["to"] == [(None, "ext1@example.com")]
    assert recipients["cc"] == [(None, "ext2@example.com")]


def test_reply_all_retains_defunct_address_without_warning() -> None:
    original = {
        "sender_name": None,
        "sender_email": "client@example.com",
        "to": [{"name": None, "email": "sean3808@mailattic.test"}],
        "cc": [{"name": None, "email": "defunct@gone.invalid"}],
    }

    recipients = reply_all_recipients(original, {"sean3808@mailattic.test"})

    assert (None, "defunct@gone.invalid") in recipients["cc"]


def test_build_thread_headers_continues_three_level_thread() -> None:
    headers = build_thread_headers(
        {
            "message_id": "<D@x>",
            "references_header": "<A@x> <B@x> <C@x>",
            "subject": "Subject",
        }
    )

    assert headers == {
        "in_reply_to": "<D@x>",
        "references": "<A@x> <B@x> <C@x> <D@x>",
        "subject": "Re: Subject",
    }


def test_build_thread_headers_does_not_duplicate_parent_at_references_end() -> None:
    headers = build_thread_headers(
        {
            "message_id": "<D@x>",
            "references_header": "<A@x> <B@x> <D@x>",
            "subject": "Subject",
        }
    )

    assert headers["references"] == "<A@x> <B@x> <D@x>"


def test_build_thread_headers_does_not_duplicate_re_prefix() -> None:
    headers = build_thread_headers(
        {
            "message_id": "<D@x>",
            "references_header": None,
            "subject": "Re: old topic",
        }
    )

    assert headers["subject"] == "Re: old topic"


def test_build_thread_headers_requires_message_id() -> None:
    with pytest.raises(ValueError, match="original message has no Message-ID"):
        build_thread_headers(
            {
                "message_id": None,
                "references_header": None,
                "subject": "Subject",
            }
        )


def test_generate_draft_message_id_is_bracketed_and_unique() -> None:
    first = generate_draft_message_id()
    second = generate_draft_message_id()

    assert first.startswith("<")
    assert first.endswith(">")
    assert second.startswith("<")
    assert second.endswith(">")
    assert first != second


def test_generate_draft_message_id_uses_sender_domain() -> None:
    message_id = generate_draft_message_id("Operator <operator@example.org>")

    assert message_id.endswith("@example.org>")


def test_draft_reply_cli_writes_eml_with_thread_headers_and_bcc(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0

    draft_files = list((tmp_path / "outbound" / "drafts").glob("*.eml"))
    assert draft_files
    parsed = BytesParser().parsebytes(draft_files[0].read_bytes())
    draft_message_id = parsed["Message-ID"]

    assert draft_message_id is not None
    assert draft_message_id.startswith("<")
    assert draft_message_id.endswith(">")
    assert parsed["Bcc"] is not None
    assert "sean3808@mailattic.test" in parsed["Bcc"]
    assert "client@example.com" in parsed["To"]
    assert parsed["In-Reply-To"] == "<test-msg-123@mailattic.test>"
    assert parsed["Subject"] == "Re: Test subject"


def test_draft_reply_message_id_uses_sending_domain(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    env_file = tmp_path / "other-domain.env"
    settings = PLACEHOLDER_SMTP_SETTINGS.replace(
        "sean3808@mailattic.test", "operator@example.org"
    )
    env_file.write_text(settings, encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )

    assert _handle_draft_reply(args) == 0

    (draft_file,) = (tmp_path / "outbound" / "drafts").glob("*.eml")
    parsed = BytesParser().parsebytes(draft_file.read_bytes())
    assert "operator@example.org" in parsed["From"]
    assert parsed["Message-ID"].endswith("@example.org>")


def _draft_with_unreadable_settings(tmp_path: Path, *declared: str) -> int:
    """Run draft-reply with a settings file that names no account (#268)."""
    db_path = _seed_message(tmp_path)
    connection = connect_database(db_path)
    try:
        for address in declared:
            add_self_address(connection, address)
        connection.commit()
    finally:
        connection.close()
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(empty_env),
        ]
    )
    return _handle_draft_reply(args)


def test_draft_reply_cli_unreadable_settings_uses_the_one_declared_self_address(
    tmp_path: Path,
) -> None:
    assert _draft_with_unreadable_settings(tmp_path, "me@example.com") == 0

    (draft_file,) = (tmp_path / "outbound" / "drafts").glob("*.eml")
    assert BytesParser().parsebytes(draft_file.read_bytes())["From"] == "me@example.com"


@pytest.mark.parametrize("declared", [(), ("me@example.com", "alias@example.com")])
def test_draft_reply_cli_unreadable_settings_refused_without_one_self_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], declared: tuple[str, ...]
) -> None:
    """#268: there is no built-in sender address to fall back to."""
    assert _draft_with_unreadable_settings(tmp_path, *declared) == 2

    stderr = capsys.readouterr().err
    assert "SMTP_USER" in stderr
    assert "bes identity add <email>" in stderr
    assert not list((tmp_path / "outbound" / "drafts").glob("*.eml"))


def test_draft_reply_cli_adds_explicit_recipients_and_prints_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--to",
            "added-to@example.com",
            "--to",
            "second-to@example.com",
            "--cc",
            "added-cc@example.com",
            "--bcc",
            "audit@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0

    parsed = _only_draft(tmp_path)
    assert "client@example.com" in parsed["To"]
    assert "added-to@example.com" in parsed["To"]
    assert "second-to@example.com" in parsed["To"]
    assert "added-cc@example.com" in parsed["Cc"]
    assert "sean3808@mailattic.test" in parsed["Bcc"]
    assert "audit@example.com" in parsed["Bcc"]

    stdout = capsys.readouterr().out
    assert f"To: {parsed['To']}" in stdout
    assert f"Cc: {parsed['Cc']}" in stdout
    assert f"Bcc: {parsed['Bcc']}" in stdout


def test_draft_reply_cli_replaces_recipients_without_changing_thread_anchor(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--replace-recipients",
            "--to",
            "replacement@example.com",
            "--cc",
            "replacement-cc@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0

    parsed = _only_draft(tmp_path)
    assert "replacement@example.com" in parsed["To"]
    assert "client@example.com" not in parsed["To"]
    assert "replacement-cc@example.com" in parsed["Cc"]
    assert parsed["In-Reply-To"] == "<test-msg-123@mailattic.test>"
    assert parsed["References"] == "<test-msg-123@mailattic.test>"


def test_draft_reply_cli_excludes_derived_recipient(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--exclude",
            "client@example.com",
            "--to",
            "current-contact@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0

    parsed = _only_draft(tmp_path)
    assert "client@example.com" not in parsed["To"]
    assert "current-contact@example.com" in parsed["To"]


def test_draft_reply_cli_excluding_only_derived_to_returns_2_without_draft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--exclude",
            "client@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    assert "at least one To recipient is required" in capsys.readouterr().err


def test_draft_reply_cli_explicit_to_overrides_derived_cc(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path, cc_addresses=["buyer@example.com"])
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--to",
            "buyer@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0
    parsed = _only_draft(tmp_path)
    assert "buyer@example.com" in parsed["To"]
    assert parsed["Cc"] is None or "buyer@example.com" not in parsed["Cc"]


def test_draft_reply_cli_duplicate_explicit_visible_placement_retains_to(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--to",
            "buyer@example.com",
            "--cc",
            "buyer@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0
    parsed = _only_draft(tmp_path)
    assert str(parsed["To"]).count("buyer@example.com") == 1
    assert parsed["Cc"] is None or "buyer@example.com" not in parsed["Cc"]


def test_draft_reply_cli_rejects_exclude_with_replace_before_database_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(tmp_path / "missing-body.txt"),
            "--replace-recipients",
            "--to",
            "blocked@example.com",
            "--exclude",
            "blocked@example.com",
            "--database",
            str(tmp_path / "missing.db"),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    assert (
        capsys.readouterr().err.strip()
        == "--exclude only filters reply-all-derived recipients; "
        "--replace-recipients skips derivation entirely. Remove --exclude, or drop "
        "--replace-recipients."
    )


@pytest.mark.parametrize("explicit_flag", ["--to", "--cc", "--bcc"])
def test_draft_reply_cli_rejects_excluded_explicit_recipient_before_database_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    explicit_flag: str,
) -> None:
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(tmp_path / "missing-body.txt"),
            explicit_flag,
            "Blocked@Example.com",
            "--exclude",
            "blocked@example.com",
            "--database",
            str(tmp_path / "missing.db"),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    assert (
        capsys.readouterr().err.strip()
        == "--exclude conflicts with explicit recipient address(es): "
        "blocked@example.com. Remove each address from --exclude or from "
        "--to/--cc/--bcc."
    )


def test_draft_reply_cli_reports_excluded_explicit_conflicts_deterministically(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(tmp_path / "missing-body.txt"),
            "--to",
            "zulu@example.com",
            "--bcc",
            "alpha@example.com",
            "--exclude",
            "ZULU@example.com",
            "--exclude",
            "Alpha@example.com",
            "--database",
            str(tmp_path / "missing.db"),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    assert (
        capsys.readouterr().err.strip()
        == "--exclude conflicts with explicit recipient address(es): "
        "alpha@example.com, zulu@example.com. Remove each address from --exclude "
        "or from --to/--cc/--bcc."
    )


def test_draft_reply_cli_allows_non_overlapping_exclusion_and_explicit_to(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path, cc_addresses=["former-contact@example.com"])
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--exclude",
            "former-contact@example.com",
            "--to",
            "approved@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0
    parsed = _only_draft(tmp_path)
    assert "client@example.com" in parsed["To"]
    assert "approved@example.com" in parsed["To"]
    assert parsed["Cc"] is None or "former-contact@example.com" not in parsed["Cc"]


def test_draft_reply_cli_header_parse_error_returns_2_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--to",
            "foo@example.com.",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    diagnostic = capsys.readouterr().err
    assert "--to requires one valid email address" in diagnostic
    assert "Traceback" not in diagnostic


@pytest.mark.parametrize("recipient_flag", ["--to", "--cc", "--bcc", "--exclude"])
@pytest.mark.parametrize(
    "recipient_value",
    [
        pytest.param(
            "Victim <victim@example.com",
            id="unclosed-angle-address",
        ),
        pytest.param("audit@例え.テスト", id="non-ascii-addr-spec"),
    ],
)
def test_draft_reply_cli_rejects_unserializable_recipient_before_database_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    recipient_flag: str,
    recipient_value: str,
) -> None:
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(tmp_path / "missing-body.txt"),
            recipient_flag,
            recipient_value,
            "--database",
            str(tmp_path / "missing.db"),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    diagnostic = capsys.readouterr().err
    assert f"{recipient_flag} requires one valid email address" in diagnostic
    assert "Traceback" not in diagnostic


def test_draft_reply_cli_accepts_unicode_display_name_with_ascii_addr_spec(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--to",
            "監査担当 <audit@example.com>",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0
    parsed = _only_draft(tmp_path)
    decoded_to = str(make_header(decode_header(str(parsed["To"]))))
    assert "監査担当 <audit@example.com>" in decoded_to


def test_draft_reply_cli_replace_without_to_returns_2_without_draft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--replace-recipients",
            "--cc",
            "cc-only@example.com",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()
    assert "at least one To recipient is required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("source_input_mode", "pst_source"),
    [
        pytest.param("archive", "legacy-export.eml", id="archive-input-mode"),
        pytest.param("", "archive:legacy-export.eml", id="archive-source-prefix"),
    ],
)
def test_draft_reply_cli_rejects_archive_anchor_before_writing_draft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_input_mode: str,
    pst_source: str,
) -> None:
    db_path = _seed_message(tmp_path)
    _set_message_provenance(
        db_path,
        source_input_mode=source_input_mode,
        pst_source=pst_source,
    )
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()

    diagnostic = capsys.readouterr().err.casefold()
    # #218: the refusal must not assert the disproven "one delivery batch" claim.
    assert "unverified" in diagnostic
    assert "delivery batch" not in diagnostic
    assert "inbound" in diagnostic
    assert "same thread" in diagnostic
    assert "verify" in diagnostic
    assert "to and cc" in diagnostic or "to/cc" in diagnostic


@pytest.mark.parametrize(
    ("source_input_mode", "pst_source"),
    [
        pytest.param("archive", "legacy-export.eml", id="archive-input-mode"),
        pytest.param("", "archive:legacy-export.eml", id="archive-source-prefix"),
    ],
)
def test_draft_reply_cli_allows_explicitly_verified_archive_anchor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_input_mode: str,
    pst_source: str,
) -> None:
    db_path = _seed_message(tmp_path)
    _set_message_provenance(
        db_path,
        source_input_mode=source_input_mode,
        pst_source=pst_source,
    )
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--allow-archive-anchor",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0

    parsed = _only_draft(tmp_path)
    assert parsed["In-Reply-To"] == "<test-msg-123@mailattic.test>"
    assert parsed["References"] == "<test-msg-123@mailattic.test>"

    captured = capsys.readouterr()
    assert f"To: {parsed['To']}" in captured.out
    diagnostic = captured.err.casefold()
    assert "warning" in diagnostic
    assert "archive" in diagnostic
    assert "incomplete" in diagnostic
    assert "verified" in diagnostic


def test_draft_reply_cli_allows_outbound_bes_anchor(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    _set_message_provenance(
        db_path,
        source_input_mode="outbound",
        pst_source="outbound-bes",
    )
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 0
    assert list((tmp_path / "outbound" / "drafts").glob("*.eml"))


def test_draft_reply_cli_unknown_message_id_returns_2(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<does-not-exist@nowhere>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    assert _handle_draft_reply(args) == 2
    assert not (tmp_path / "outbound" / "drafts").exists()


def _set_message_provenance(
    db_path: Path,
    *,
    source_input_mode: str,
    pst_source: str,
) -> None:
    connection = connect_database(db_path)
    try:
        connection.execute(
            "UPDATE messages SET source_input_mode = ?, pst_source = ?",
            (source_input_mode, pst_source),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_message(
    tmp_path: Path,
    *,
    cc_addresses: list[str] | None = None,
) -> Path:
    db_path = initialize_database(tmp_path / "evidence.db")
    message = build_normalized_message(
        {
            "subject": "Test subject",
            "sender": {"name": None, "email": "client@example.com"},
            "to": [{"name": None, "email": "sean3808@mailattic.test"}],
            "cc": [{"name": None, "email": address} for address in cc_addresses or []],
            "message_id": "<test-msg-123@mailattic.test>",
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


def _placeholder_env_file(tmp_path: Path) -> Path:
    # Just enough settings to name the sending account (the draft's From);
    # these tests are unrelated to SMTP config resolution.
    env_file = tmp_path / "placeholder.env"
    if not env_file.exists():
        env_file.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    return env_file


PLACEHOLDER_SMTP_SETTINGS = (
    "SMTP_HOST=smtp.example.com\n"
    "SMTP_PORT=465\n"
    "SMTP_USER=sean3808@mailattic.test\n"
    "SMTP_PASSWORD=placeholder\n"
)


def _only_draft(tmp_path: Path):
    draft_files = list((tmp_path / "outbound" / "drafts").glob("*.eml"))
    assert len(draft_files) == 1
    return BytesParser().parsebytes(draft_files[0].read_bytes())
