"""#203: `draft-reply --subject` overrides the reply subject, nothing else."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from mailattic.cli import build_parser
from mailattic.outbound import (
    DraftReplyError,
    assemble_draft_reply,
    draft_fingerprint,
)
from mailattic.outbound_commands import _handle_draft_reply
from tests.test_outbound_draft import _placeholder_env_file, _seed_message

ANCHOR = "<test-msg-123@mailattic.test>"


def _run(tmp_path: Path, db_path: Path, *extra: str) -> tuple[int, list[Path]]:
    body_file = tmp_path / "body.txt"
    body_file.write_text("煩請提供型錄", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            ANCHOR,
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
            *extra,
        ]
    )
    code = _handle_draft_reply(args)
    drafts = sorted((tmp_path / "outbound" / "drafts").glob("*.eml"))
    return code, drafts


def test_custom_subject_replaces_reply_subject_and_keeps_thread(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _seed_message(tmp_path)

    code, drafts = _run(tmp_path, db_path, "--subject", "索取型錄（5 支產品）")

    assert code == 0
    assert len(drafts) == 1
    raw = drafts[0].read_bytes()
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    # Used as given: no automatic "Re: ".
    assert msg["Subject"] == "索取型錄（5 支產品）"
    # Threading still comes from the anchor.
    assert msg["In-Reply-To"] == ANCHOR
    assert ANCHOR in msg["References"]
    stdout = capsys.readouterr().out
    assert "Subject: 索取型錄（5 支產品）\n" in stdout
    # The reviewed fingerprint covers the file that carries the new subject.
    assert f"fingerprint: {draft_fingerprint(raw)}" in stdout


def test_non_ascii_subject_is_encoded_on_the_wire(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)

    code, drafts = _run(tmp_path, db_path, "--subject", "索取型錄")

    assert code == 0
    header_section = drafts[0].read_bytes().split(b"\r\n\r\n", 1)[0]
    subject_line = next(
        line for line in header_section.splitlines() if line.startswith(b"Subject:")
    )
    assert subject_line.isascii()
    assert b"=?utf-8?" in subject_line.lower()


def test_without_subject_keeps_re_prefixed_original(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)

    code, drafts = _run(tmp_path, db_path)

    assert code == 0
    msg = BytesParser(policy=policy.default).parsebytes(drafts[0].read_bytes())
    assert msg["Subject"] == "Re: Test subject"


@pytest.mark.parametrize("subject", ["", "   ", "\t"])
def test_blank_subject_is_refused_without_a_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], subject: str
) -> None:
    db_path = _seed_message(tmp_path)

    code, drafts = _run(tmp_path, db_path, "--subject", subject)

    assert code == 2
    assert drafts == []
    assert "--subject must not be empty" in capsys.readouterr().err


@pytest.mark.parametrize(
    "subject",
    [
        "Line one\r\nBcc: someone@example.com",
        "a\nb",
        "a\rb",
        "a\x1b[2Jb",
        "a\u2028b",
        "a\u2029b",
    ],
)
def test_subject_with_line_breaks_or_controls_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], subject: str
) -> None:
    # A line break would otherwise surface as an unhandled ValueError from
    # the email package (or, elsewhere, as header injection).
    db_path = _seed_message(tmp_path)

    code, drafts = _run(tmp_path, db_path, "--subject", subject)

    assert code == 2
    assert drafts == []
    assert "--subject must be a single line" in capsys.readouterr().err


def test_library_subject_override(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)

    result = assemble_draft_reply(
        db_path,
        ANCHOR,
        draft_dir=tmp_path / "drafts",
        own_address="sean3808@mailattic.test",
        body_text="body",
        subject="New topic",
    )

    assert result.subject == "New topic"


def test_library_blank_subject_is_refused_before_database_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(DraftReplyError, match="--subject must not be empty"):
        assemble_draft_reply(
            tmp_path / "missing.db",
            ANCHOR,
            draft_dir=tmp_path / "drafts",
            own_address="sean3808@mailattic.test",
            body_text="body",
            subject=" ",
        )


def test_reply_docs_publish_subject_flag() -> None:
    # AGENTS.md: a CLI-surface change updates the skill in the same change.
    skill_root = Path(__file__).resolve().parent.parent / "skills"
    skill_root = skill_root / "mailattic"
    skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    reply_text = (skill_root / "references" / "capability-c-reply.md").read_text(
        encoding="utf-8"
    )

    assert "--subject" in skill_text
    assert "--subject" in reply_text
