"""Regression tests for #288: one undecodable field must not fail the whole mail.

An RFC 2231 attachment-name parameter that declares a stateful charset with
invalid bytes, or a text part whose declared codec decodes to a lone surrogate,
used to fail the entire message (``parse-maildir`` / ``persist-message``) and
so block the folder's sync. The mail must be stored with only that field
degraded. All names and addresses below are invented.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mailattic.ingest import import_maildir_message, import_maildir_sources
from mailattic.models import MaildirSource
from mailattic.parser import _parse_eml_file
from mailattic.storage import connect_database, initialize_database

U_FFFD = "�"


def _mail(
    *,
    message_id: str,
    part_headers: str = 'Content-Disposition: attachment; filename="a.pdf"\r\n',
    text_headers: str = "Content-Type: text/plain; charset=utf-8\r\n",
    text_body: bytes = b"hello body",
) -> bytes:
    head = (
        "From: sender@example.com\r\nTo: recipient@example.com\r\n"
        f"Subject: quote\r\nMessage-ID: <{message_id}>\r\n"
        "Date: Mon, 05 Oct 2026 10:00:00 +0000\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
    ).encode("ascii")
    text = b"--B\r\n" + text_headers.encode("ascii") + b"\r\n" + text_body + b"\r\n"
    # A second Content-Type would be ignored, hiding a bad ``name*=``.
    default_type = (
        "" if "Content-Type" in part_headers else "Content-Type: application/pdf\r\n"
    )
    attachment = (
        f"--B\r\n{default_type}{part_headers}"
        "Content-Transfer-Encoding: base64\r\n\r\nAAAA\r\n--B--\r\n"
    ).encode("ascii")
    return head + text + attachment


# iso-2022-jp: ESC $ B starts JIS X 0208 and "!" alone is half a character.
_BAD_ISO2022 = "iso-2022-jp''%1B%24B%21%1B%28B.pdf"
_BAD_UTF16 = "utf-16''%41.pdf"
_FILENAME_STAR = "Content-Disposition: attachment; filename*={}\r\n"
_NAME_STAR = "Content-Type: application/pdf; name*={}\r\n"

BAD_NAME_HEADERS = [
    pytest.param(_FILENAME_STAR.format(_BAD_ISO2022), id="filename-star-iso2022jp"),
    pytest.param(_FILENAME_STAR.format(_BAD_UTF16), id="filename-star-utf16"),
    pytest.param(_NAME_STAR.format(_BAD_ISO2022), id="name-star-iso2022jp"),
    pytest.param(_NAME_STAR.format(_BAD_UTF16), id="name-star-utf16"),
]


class Env:
    def __init__(self, root: Path) -> None:
        self.db = initialize_database(root / "t.db")
        self.cas = root / "cas"
        self.folder = root / "maildir" / "acct" / "INBOX"
        (self.folder / "new").mkdir(parents=True)
        self.source = MaildirSource(
            path=self.folder,
            account_email="recipient@example.com",
            account_name="acct",
            folder_name="INBOX",
            requested_from=self.folder.parent,
        )

    def write(self, name: str, raw: bytes) -> Path:
        path = self.folder / "new" / name
        path.write_bytes(raw)
        return path

    def import_one(self, path: Path):
        connection = connect_database(self.db)
        try:
            summary = import_maildir_message(
                connection=connection,
                source=self.source,
                message_path=path,
                attachment_root=self.cas,
            )
            connection.commit()
            return summary
        finally:
            connection.close()

    def rows(self, sql: str) -> list[sqlite3.Row]:
        connection = connect_database(self.db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _failures(env: Env) -> list[tuple[str, str]]:
    return [
        (r["error_stage"], r["error_type"])
        for r in env.rows("SELECT error_stage, error_type FROM ingest_failures")
    ]


@pytest.mark.parametrize("part_headers", BAD_NAME_HEADERS)
def test_undecodable_attachment_name_still_stores_the_mail(
    env: Env, part_headers: str
) -> None:
    path = env.write(
        "1.eml", _mail(message_id="n1@example.com", part_headers=part_headers)
    )

    summary = env.import_one(path)

    assert summary.imported_messages == 1
    assert _failures(env) == []
    assert [r["body_text"] for r in env.rows("SELECT body_text FROM messages")] == [
        "hello body"
    ]
    (name,) = [
        r["attachment_name"]
        for r in env.rows("SELECT attachment_name FROM message_attachments")
    ]
    assert U_FFFD in name


def test_text_part_decoding_to_lone_surrogate_still_stores_the_mail(env: Env) -> None:
    # utf-7 "+2AA-" is U+D800 on its own: valid utf-7, unencodable in SQLite.
    raw = _mail(
        message_id="n2@example.com",
        text_headers="Content-Type: text/plain; charset=utf-7\r\n",
        text_body=b"before +2AA- after",
    )
    path = env.write("1.eml", raw)

    summary = env.import_one(path)

    assert summary.imported_messages == 1
    assert _failures(env) == []
    (body,) = [r["body_text"] for r in env.rows("SELECT body_text FROM messages")]
    assert body == f"before {U_FFFD} after"


def test_html_part_decoding_to_lone_surrogate_still_stores_the_mail(env: Env) -> None:
    raw = _mail(
        message_id="n3@example.com",
        text_headers="Content-Type: text/html; charset=utf-7\r\n",
        text_body=b"<p>before +2AA- after</p>",
    )

    summary = env.import_one(env.write("1.eml", raw))

    assert summary.imported_messages == 1
    assert _failures(env) == []
    (body,) = [r["body_text"] for r in env.rows("SELECT body_text FROM messages")]
    assert body == f"before {U_FFFD} after"


def test_healthy_mail_after_a_poisoned_one_is_stored_too(env: Env) -> None:
    env.write(
        "1.eml",
        _mail(
            message_id="bad1@example.com", part_headers=BAD_NAME_HEADERS[0].values[0]
        ),
    )
    env.write(
        "2.eml",
        _mail(
            message_id="bad2@example.com",
            text_headers="Content-Type: text/plain; charset=utf-7\r\n",
            text_body=b"+2AA-",
        ),
    )
    env.write("3.eml", _mail(message_id="good@example.com"))

    import_maildir_sources(
        database_path=env.db, sources=[env.source], attachment_root=env.cas
    )

    ids = {r["message_id"] for r in env.rows("SELECT message_id FROM messages")}
    assert ids == {"<bad1@example.com>", "<bad2@example.com>", "<good@example.com>"}
    assert _failures(env) == []


# ---------------------------------------------------------------------------
# Fields that decode today stay exactly as they were
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "plain ascii",
        "emoji \U0001f600 and CJK Extension B \U00020bb7 text",
        f"already {U_FFFD} replaced",
    ],
)
def test_body_without_lone_surrogate_round_trips_unchanged(
    tmp_path: Path, body: str
) -> None:
    path = tmp_path / "m.eml"
    path.write_bytes(_mail(message_id="n4@example.com", text_body=body.encode("utf-8")))

    assert _parse_eml_file(path)["body_text"] == body


def test_well_formed_rfc2231_filename_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "m.eml"
    path.write_bytes(
        _mail(
            message_id="n5@example.com",
            part_headers=_FILENAME_STAR.format("utf-8''%E5%A0%B1%E5%83%B9.pdf"),
        )
    )

    assert _parse_eml_file(path)["attachment_names"] == ["報價.pdf"]


# utf-7 "+2AA-" is U+D800 on its own. In a header the stdlib's own sanitiser
# raises UnicodeEncodeError on it -- a different exception from the invalid-bytes
# shapes above, raised from the same header parse.
_SURROGATE_NAME_MAILS = [
    pytest.param(
        _mail(
            message_id="s1@example.com",
            part_headers=_FILENAME_STAR.format("utf-7''%2B2AA-.pdf"),
        ),
        id="filename-star-utf7-surrogate",
    ),
    pytest.param(
        _mail(message_id="s2@example.com").replace(
            b"From: sender@example.com",
            b"From: =?utf-7?Q?+2AA-?= <sender@example.com>",
        ),
        id="from-name-utf7-surrogate",
    ),
    pytest.param(
        _mail(message_id="s3@example.com").replace(
            b"To: recipient@example.com",
            b"To: =?utf-7?Q?+2AA-?= <recipient@example.com>",
        ),
        id="to-name-utf7-surrogate",
    ),
    pytest.param(
        _mail(message_id="s4@example.com").replace(
            b"Subject: quote", b"Subject: =?utf-7?Q?+2AA-?= quote"
        ),
        id="subject-utf7-surrogate",
    ),
]


@pytest.mark.parametrize("raw", _SURROGATE_NAME_MAILS)
def test_header_decoding_to_lone_surrogate_still_stores_the_mail(
    env: Env, raw: bytes
) -> None:
    summary = env.import_one(env.write("1.eml", raw))

    assert summary.imported_messages == 1
    assert _failures(env) == []
    assert [
        r["sender_email"] for r in env.rows("SELECT sender_email FROM messages")
    ] == ["sender@example.com"]
