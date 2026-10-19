"""HTML-only mails: style/script/title text is not body text (#294).

Import side: the HTML-to-text conversion skips style/script/title and keeps
line breaks between block elements. Reply side: the quote block of
``draft-reply`` goes through the display cleaner (``render_text``), so bodies
stored before the import fix are not quoted with their style text either.
"""

from __future__ import annotations

from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

from mailattic.cli import build_parser
from mailattic.ingest import import_maildir_sources
from mailattic.models import MaildirSource, PstSource, build_normalized_message
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.query import SearchFilters, get_message, search_messages
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)
from tests.test_outbound_quoted_reply import _placeholder_env_file

HTML_ONLY = """<html><head><title>Quotation 42</title>
<style>
<!--
p.MsoNormal, li.MsoNormal {margin:0cm; font-family:Calibri; font-size:11.0pt;}
a.MsoHyperlink {text-decoration:underline}
-->
</style>
<script>var tracker = 1; alert('hello');</script></head>
<body><p class=MsoNormal>Dear Owner,</p>
<p class=MsoNormal>The paper ships on Monday.</p>
<table><tr><td>Item</td><td>Qty</td></tr><tr><td>Notebook</td><td>10</td></tr></table>
<p>Best regards,<br>Ana</p></body></html>
"""


def _import(root: Path, msg: EmailMessage) -> Path:
    folder = root / "maildir" / "fixture" / "INBOX"
    (folder / "new").mkdir(parents=True, exist_ok=True)
    msg["From"] = "supplier@example.test"
    msg["To"] = "buyer@example.test"
    msg["Subject"] = "Quotation"
    msg["Message-ID"] = "<html-only-294@example.test>"
    (folder / "new" / "m.eml").write_bytes(msg.as_bytes())
    db_path = root / "fixture.db"
    initialize_database(db_path)
    source = MaildirSource(
        folder, "buyer@example.test", "audit", "INBOX", folder.parent
    )
    import_maildir_sources(
        database_path=db_path, sources=[source], attachment_root=root / "cas"
    )
    return db_path


def _stored_body(db_path: Path) -> str:
    stored = get_message(db_path, 1, read_only=True)
    assert stored is not None
    return str(stored["body_text"])


def test_html_only_import_drops_style_script_title_and_keeps_breaks(
    tmp_path: Path,
) -> None:
    msg = EmailMessage()
    msg.set_content(HTML_ONLY, subtype="html")
    db_path = _import(tmp_path, msg)

    body = _stored_body(db_path)
    for leaked in (
        "margin",
        "Calibri",
        "MsoNormal",
        "tracker",
        "alert",
        "Quotation 42",
    ):
        assert leaked not in body
    assert body.splitlines() == [
        "Dear Owner,",
        "The paper ships on Monday.",
        "Item Qty",
        "Notebook 10",
        "Best regards,",
        "Ana",
    ]

    for css_word in ("MsoNormal", "Calibri", "margin"):
        hits = search_messages(
            database_path=db_path,
            filters=SearchFilters(keyword=css_word),
            read_only=True,
        )
        assert hits == []
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="notebook"),
        read_only=True,
    )
    assert len(hits) == 1


def test_healthy_inline_html_text_is_unchanged(tmp_path: Path) -> None:
    """Pin: inline text nodes are still joined by single spaces (as before)."""
    msg = EmailMessage()
    msg.set_content("<p>Hello   <b>big</b>\n world &amp; more</p>", subtype="html")
    db_path = _import(tmp_path, msg)
    assert _stored_body(db_path) == "Hello big world & more"


def test_text_plus_html_mail_is_stored_as_the_plain_part(tmp_path: Path) -> None:
    msg = EmailMessage()
    msg.set_content("Plain part line 1\n\nPlain part line 2\n")
    msg.add_alternative(HTML_ONLY, subtype="html")
    db_path = _import(tmp_path, msg)
    assert _stored_body(db_path) == "Plain part line 1\n\nPlain part line 2"


def _draft_parts(tmp_path: Path, stored_body: str) -> tuple[str, str]:
    db_path = initialize_database(tmp_path / "evidence.db")
    message = build_normalized_message(
        {
            "subject": "Quotation",
            "sender": {"name": "Ana Test", "email": "ana@example.com"},
            "to": [{"name": "Sean Example", "email": "sean3808@mailattic.test"}],
            "cc": [],
            "message_id": "<quote-294@mailattic.test>",
            "references": None,
            "body_text": stored_body,
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
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<quote-294@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_draft_reply(args) == 0
    raw = next((tmp_path / "outbound" / "drafts").glob("*.eml")).read_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    plain = parsed.get_body(preferencelist=("plain",))
    html_part = parsed.get_body(preferencelist=("html",))
    assert plain is not None and html_part is not None
    return str(plain.get_content()), str(html_part.get_content())


def test_reply_quote_of_already_stored_style_text_has_no_style(
    tmp_path: Path,
) -> None:
    stored = (
        "Quotation <!-- p.MsoNormal, li.MsoNormal {margin:0cm; font-size:11.0pt;} "
        "a.MsoHyperlink {text-decoration:underline} --> "
        "<style>td {color: red}</style> Dear Owner, The paper ships on Monday."
    )
    plain, html_part = _draft_parts(tmp_path, stored)
    for part in (plain, html_part):
        assert "Dear Owner, The paper ships on Monday." in part
        assert "margin" not in part
        assert "MsoNormal" not in part
        assert "color: red" not in part


def test_reply_quote_keeps_plain_text_braces_and_url_params(tmp_path: Path) -> None:
    stored = "Set {name} in the template, see https://example.com/a?x=1&copy=2&reg=3 ok"
    plain, html_part = _draft_parts(tmp_path, stored)
    assert stored in plain
    assert "{name}" in html_part
    assert "https://example.com/a?x=1&amp;copy=2&amp;reg=3" in html_part


def test_unclosed_style_or_title_does_not_swallow_the_body() -> None:
    # Malformed HTML: the skipped element never closes. Dropping "its content"
    # would drop the rest of the mail, so that text is kept as before.
    from mailattic.parser import _html_to_text

    for html in (
        "<style>p {margin:0}<p>Dear Owner, price 450</p>",
        "<title>Quotation<body><p>Dear Owner, price 450</p></body>",
    ):
        assert "Dear Owner, price 450" in _html_to_text(html)
    # A doubled opening tag with one close must not leave skipping switched on.
    assert (
        _html_to_text("<style>a{}<style>b{}</style><p>Dear Owner</p>") == "Dear Owner"
    )
