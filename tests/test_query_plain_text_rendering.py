"""Tests for plain text rendering with angle brackets and HTML cleaning (#240).

Legitimate plain text comparison conditions (e.g. 'width < 7 and height > 40 cm')
and angle-bracket email addresses / URLs (e.g. '<sales@example.test>') are preserved
in search snippets, full body, and MCP display, while HTML and CSS tags are cleaned.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from mailattic.ingest import import_maildir_sources
from mailattic.mcp_server import create_server
from mailattic.models import MaildirSource
from mailattic.query import (
    SearchFilters,
    get_message,
    search_messages,
)
from mailattic.storage import initialize_database


def _create_and_import_email(
    root: Path,
    *,
    subject: str,
    body: str,
    subtype: str = "plain",
    message_id: str = "msg-1@example.test",
    account_email: str = "buyer@example.test",
    db_name: str = "fixture",
) -> Path:
    folder = root / "maildir" / db_name / "INBOX"
    (folder / "new").mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = "supplier@example.test"
    msg["To"] = account_email
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg.set_content(body, subtype=subtype)
    (folder / "new" / f"{message_id}.eml").write_bytes(msg.as_bytes())

    db_path = root / f"{db_name}.db"
    initialize_database(db_path)
    source = MaildirSource(folder, account_email, "audit", "INBOX", folder.parent)
    import_maildir_sources(
        database_path=db_path,
        sources=[source],
        attachment_root=root / "cas",
    )
    return db_path


def test_plain_text_angle_bracket_comparisons_and_addresses_preserved(
    tmp_path: Path,
) -> None:
    """Issue #240 reproduction: comparisons and angle-bracket emails stay intact."""
    raw_plain_body = (
        "Specification: width < 7 and height > 40 cm. "
        "Contact <sales@example.test>. Ship approved."
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Product Specification",
        body=raw_plain_body,
        subtype="plain",
        message_id="spec-240@example.test",
    )

    # 1. Verify raw body stored in database is intact
    stored = get_message(db_path, 1, read_only=True)
    assert stored is not None
    assert "< 7 and height >" in str(stored["body_text"])
    assert "<sales@example.test>" in str(stored["body_text"])

    # 2. Verify search_messages snippet and full_body rendered_body_text
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="height"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    hit = hits[0]

    # snippet and rendered_body_text must contain the comparisons and
    # angle-bracket email
    assert "< 7 and height > 40" in str(hit["snippet"])
    assert "<sales@example.test>" in str(hit["snippet"])
    assert "< 7 and height > 40" in str(hit["rendered_body_text"])
    assert "<sales@example.test>" in str(hit["rendered_body_text"])

    # 3. Verify MCP display (mail_show) matches search rendering
    server = create_server(database_path=db_path)
    result = server._tool_manager.get_tool("mail_show").fn(message_id=1)
    payload = result.structured_content
    if set(payload) == {"result"}:
        payload = payload["result"]

    assert "< 7 and height > 40" in str(payload["rendered_body_text"])
    assert "<sales@example.test>" in str(payload["rendered_body_text"])
    assert payload["rendered_body_text"] == hit["rendered_body_text"]


def test_plain_text_additional_angle_bracket_patterns(tmp_path: Path) -> None:
    """Math conditions, angle URLs, and placeholders are preserved in plain text."""
    body = (
        "Check range: 0 < x < 10 and y > 20. "
        "Also 1 < 2 > 0. "
        "Docs at <https://example.com/api?id=1&debug=true>. "
        "Placeholders: <username> and <target_dir>."
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Math and URL test",
        body=body,
        subtype="plain",
        message_id="math-url@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="range"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    assert "0 < x < 10 and y > 20" in rendered
    assert "1 < 2 > 0" in rendered
    assert "<https://example.com/api?id=1&debug=true>" in rendered
    assert "<username>" in rendered
    assert "<target_dir>" in rendered


def test_html_control_and_css_cleaning_preserved(tmp_path: Path) -> None:
    """HTML tags and CSS noise are cleaned consistently."""
    html_body = (
        "<style>body { font-family: Arial; }</style>"
        "<p>Specification: width below 7 and height above 40 cm.</p>"
        "<p>Contact sales at <b>Acme</b>.</p>"
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="HTML control",
        body=html_body,
        subtype="html",
        message_id="html-control@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="height"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    assert "<style>" not in rendered
    assert "<p>" not in rendered
    assert "<b>" not in rendered
    assert "Specification: width below 7 and height above 40 cm." in rendered
    assert "Contact sales at Acme" in rendered


def test_documented_boundary_plain_text_with_valid_html_tag_name(
    tmp_path: Path,
) -> None:
    """Documented boundary: literal HTML tag names in plain text are stripped.

    Per issue #240: when a plain-text email literally writes standard HTML tag names
    such as <b> or <p>, render_text cannot distinguish them from HTML markup without
    out-of-band MIME structure at display time. The strategy treats them as HTML tags.
    """
    plain_with_html_tag = (
        "Instructions: In HTML, the bold tag is written as <b>bold text</b>."
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Tag boundary",
        body=plain_with_html_tag,
        subtype="plain",
        message_id="boundary@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Instructions"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    # <b> and </b> are recognized as HTML tags and stripped per documented boundary
    assert "<b>" not in rendered
    assert "</b>" not in rendered
    assert "bold text" in rendered


def test_office_xml_markup_cleaned(tmp_path: Path) -> None:
    """F1: <xml> and </xml> Office markup tags are cleaned."""
    body = (
        "<xml>\n"
        "<o:OfficeDocumentSettings>\n"
        "  <o:PixelsPerInch>96</o:PixelsPerInch>\n"
        "</o:OfficeDocumentSettings>\n"
        "</xml>\n"
        "Actual mail content."
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Office XML Test",
        body=body,
        subtype="plain",
        message_id="xml-test@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Actual"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    assert "<xml>" not in rendered
    assert "</xml>" not in rendered
    assert "Actual mail content." in rendered


def test_long_tag_attributes_cleaned(tmp_path: Path) -> None:
    """F2 (shape 1): Tag attributes longer than 2048 chars are stripped."""
    long_attrs = "data-extra='" + ("x" * 3150) + "'"
    body = (
        f'<html xmlns:v="urn:schemas-microsoft-com:vml" {long_attrs}>'
        "<p>Long tag content</p></html>"
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Long Attribute Test",
        body=body,
        subtype="plain",
        message_id="long-attr@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Long"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    assert "<html" not in rendered
    assert "data-extra" not in rendered
    assert "Long tag content" in rendered


def test_inline_base64_image_tag_cleaned(tmp_path: Path) -> None:
    """F2 (shape 2): <img> with large base64 data URI (>2048 chars) is stripped."""
    base64_data = "A" * 16400
    body = (
        "<p>Photo header</p>"
        f"<img alt='inline' src='data:image/png;base64,{base64_data}'>"
        "<p>Photo footer</p>"
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Base64 Img Test",
        body=body,
        subtype="plain",
        message_id="img-base64@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Photo"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    assert "<img" not in rendered
    assert "data:image/png" not in rendered
    assert "A" * 50 not in rendered
    assert "Photo header" in rendered
    assert "Photo footer" in rendered


def test_office_blob_long_attributes_cleaned(tmp_path: Path) -> None:
    """F2 (shape 3): Blobs with recognized tags and long attributes (>2048 chars)."""
    long_css = "font-family: Arial; " * 300  # ~6000 chars
    body = f"<font size='2' style='{long_css}'>Announced message</font>"
    db_path = _create_and_import_email(
        tmp_path,
        subject="Office Blob Test",
        body=body,
        subtype="plain",
        message_id="blob-test@example.test",
    )

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Announced"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    rendered = str(hits[0]["rendered_body_text"])
    assert "<font" not in rendered
    assert "font-family: Arial" not in rendered
    assert "Announced message" in rendered


def test_plain_text_braces_and_urls_preserved_end_to_end(tmp_path: Path) -> None:
    """Issue #289: text/plain braces and URLs preserved; match_field reports body."""
    body = (
        "Hi Owner,\n\n"
        "Please confirm the quote: unit price 100 USD per box, "
        "minimum 20 boxes {Pickup Riverside}.\n"
        "Track it here: https://portal.example.com/track?id=5&region=xx&timestamp=1700&currency=USD&quote_id=77\n"
        "Reference: https://portal.example.com/api?item=9&copy=1&lang=zh&not=1&para=2&sect=3\n"
        "Regards,\nDana\n"
    )
    db_path = _create_and_import_email(
        tmp_path,
        subject="Quote terms",
        body=body,
        subtype="plain",
        message_id="quote-terms-289@example.test",
    )

    stored = get_message(db_path, 1, read_only=True)
    assert stored is not None
    assert "{Pickup Riverside}" in str(stored["body_text"])
    assert "&region=xx&timestamp=1700" in str(stored["body_text"])

    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Riverside"),
        full_body=True,
        read_only=True,
    )
    assert len(hits) == 1
    hit = hits[0]
    assert hit["rendered_body_text"] == body.strip()
    assert "&region=xx&timestamp=1700" in str(hit["rendered_body_text"])
    assert "&copy=1" in str(hit["rendered_body_text"])
    assert "{Pickup Riverside}" in str(hit["rendered_body_text"])
    assert hit["match_field"] == "body"
    assert hit["match_in"] == "new"
    assert "100 USD per box" in str(hit["snippet"])
    assert "Riverside" in str(hit["snippet"])
