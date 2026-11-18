"""#136: a reply quotes the original's own HTML, with the original .eml as oracle.

The customer's message as it arrived (the RFC822 bytes on the IMAP server) is
the acceptance standard: the quoted block must keep its images, tables and
paragraphs, and every ``cid:`` image it references must travel in the draft.
When the original cannot be fetched the draft falls back to the stored text
and says so; it never falls back silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from mailattic.cli import build_parser
from mailattic.original_export import imap_original_fetcher
from mailattic.outbound import (
    _prepare_draft_for_send,
    assemble_draft_reply,
    build_quoted_reply_bodies,
)
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.reply_quote import (
    OriginalUnavailable,
    quotable_original_from_rfc822,
    verify_quoted_html,
)
from tests.test_original_export import FakeImapClient, FakeImapConnection
from tests.test_outbound_draft import PLACEHOLDER_SMTP_SETTINGS, _seed_message

SEEDED_MESSAGE_ID = "<test-msg-123@mailattic.test>"
OWN = "sean3808@mailattic.test"
LOGO_PNG = b"\x89PNG\r\n\x1a\n" + b"logo-bytes" * 8

ORIGINAL_HTML = """<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<style><!--
p.MsoNormal {margin:0cm; font-size:11.0pt; font-family:"Calibri",sans-serif;}
--></style></head>
<body lang="EN-US" style="word-wrap:break-word">
<div class="WordSection1">
<p class="MsoNormal">Dear Sean</p>
<p class="MsoNormal">Please find the revised invoice as requested.</p>
<table border="1" cellpadding="0">
<tr><td><p class="MsoNormal">No</p></td><td><p class="MsoNormal">Item</p></td>
<td><p class="MsoNormal">Qty</p></td></tr>
<tr><td><p class="MsoNormal">1</p></td><td><p class="MsoNormal">RED DYE</p></td>
<td><p class="MsoNormal">310</p></td></tr></table>
<p class="MsoNormal">Best regards</p>
<p class="MsoNormal">
<img width="120" src="cid:image001.png@01DB0000.00000000" alt="logo"></p>
<p class="MsoNormal"><img src="https://example.com/sig/line.png" alt=""></p>
<p class="MsoNormal">Alice Chen<br>Logistics</p>
</div></body></html>"""


def _original_eml(html_body: str = ORIGINAL_HTML, *, with_html: bool = True) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Alice Chen <client@example.com>"
    msg["To"] = OWN
    msg["Subject"] = "Test subject"
    msg["Message-ID"] = SEEDED_MESSAGE_ID
    msg.set_content(
        "Dear Sean\n\nPlease find the revised invoice as requested.\n\n"
        "[https://example.com/sig/line.png]\nAlice Chen\n"
    )
    if with_html:
        msg.add_alternative(html_body, subtype="html")
        msg.get_body(preferencelist=("html",)).add_related(
            LOGO_PNG,
            "image",
            "png",
            cid="<image001.png@01DB0000.00000000>",
            filename="image001.png",
        )
    return msg.as_bytes()


def _fetch_returning(payload: bytes):
    def fetch(_message: Mapping[str, object]) -> bytes:
        return payload

    return fetch


def _fetch_raising(reason: str):
    def fetch(_message: Mapping[str, object]) -> bytes:
        raise OriginalUnavailable(reason)

    return fetch


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _reply(tmp_path: Path, fetch, **kwargs):
    return assemble_draft_reply(
        _seed_message(tmp_path),
        SEEDED_MESSAGE_ID,
        own_address=OWN,
        draft_dir=tmp_path / "outbound" / "drafts",
        body_text="Thanks, noted.",
        fetch_original=fetch,
        **kwargs,
    )


def _draft(result) -> EmailMessage:
    return message_from_bytes(
        Path(result.draft_path).read_bytes(), policy=policy.default
    )


def _quoted_soup(draft: EmailMessage) -> BeautifulSoup:
    html_body = draft.get_body(preferencelist=("html",)).get_content()
    return BeautifulSoup(html_body.split("<hr>", 1)[1], "html.parser")


def _body_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    return soup.body or soup


# --- the original .eml as oracle ---------------------------------------------


def test_reply_quotes_the_originals_html_structure(tmp_path: Path) -> None:
    result = _reply(tmp_path, _fetch_returning(_original_eml()))

    original = _body_soup(ORIGINAL_HTML)
    quoted = _quoted_soup(_draft(result))
    # Same visible text, same images in the same order, same table cells.
    assert quoted.get_text(" ", strip=True).endswith(original.get_text(" ", strip=True))
    assert [img["src"] for img in quoted.find_all("img")] == [
        img["src"] for img in original.find_all("img")
    ]
    assert [td.get_text(strip=True) for td in quoted.find_all("td")] == [
        td.get_text(strip=True) for td in original.find_all("td")
    ]
    assert len(quoted.find_all("p", class_="MsoNormal")) == len(
        original.find_all("p", class_="MsoNormal")
    )
    # The sender's paragraph styles travel with the quote.
    assert "p.MsoNormal" in str(quoted.find("style"))
    # The flattened text never reaches the customer.
    assert "[https://" not in str(quoted)
    assert result.quote_source == "original"


def test_every_quoted_cid_image_travels_in_the_draft(tmp_path: Path) -> None:
    result = _reply(tmp_path, _fetch_returning(_original_eml()))

    draft = _draft(result)
    cids = {
        img["src"][4:]
        for img in _quoted_soup(draft).find_all("img")
        if img["src"].startswith("cid:")
    }
    parts = {
        str(part["Content-ID"]).strip("<>"): part
        for part in draft.walk()
        if part.get("Content-ID")
    }
    assert cids == {"image001.png@01DB0000.00000000"}
    assert cids <= parts.keys()
    image = parts["image001.png@01DB0000.00000000"]
    assert image.get_content() == LOGO_PNG
    assert image.get_content_disposition() == "inline"
    # The image sits next to the HTML it belongs to, not in the attachment list.
    assert draft.get_body(preferencelist=("related",)) is not None
    assert list(draft.iter_attachments()) == []


def test_plain_alternative_still_quotes_the_stored_text(tmp_path: Path) -> None:
    result = _reply(tmp_path, _fetch_returning(_original_eml()))

    plain = _draft(result).get_body(preferencelist=("plain",)).get_content()
    assert "Thanks, noted." in plain
    assert "<p" not in plain
    assert result.body_text.startswith("Thanks, noted.")


def test_draft_with_quoted_original_prepares_for_send(tmp_path: Path) -> None:
    result = _reply(
        tmp_path,
        _fetch_returning(_original_eml()),
        attachments=[_write(tmp_path / "invoice.pdf", b"%PDF-1.4 x")],
    )

    prepared = _prepare_draft_for_send(result.draft_id, tmp_path / "evidence.db")

    assert prepared.fingerprint == result.fingerprint
    assert [p.get_filename() for p in prepared.msg.iter_attachments()] == [
        "invoice.pdf"
    ]
    assert prepared.msg.get_body(preferencelist=("related",)) is not None


def test_font_style_does_not_restyle_the_quoted_original() -> None:
    quotable = quotable_original_from_rfc822(_original_eml())
    assert quotable is not None
    original = {
        "sender_name": "Alice Chen",
        "sender_email": "client@example.com",
        "sent_at": "2026-05-26T09:03:07",
        "subject": "Test subject",
        "body_text": "flattened",
        "to": [],
        "cc": [],
    }

    _, html_body = build_quoted_reply_bodies(
        original,
        "My reply",
        font_style="font-family: 'Aptos', sans-serif",
        quotable=quotable,
    )

    wrapper, _, after = html_body.partition("</div><!-- reply -->")
    assert wrapper.startswith('<div style="font-family:')
    assert "My reply" in wrapper and "<b>From:</b>" in wrapper
    assert "Dear Sean" in after and "Dear Sean" not in wrapper
    assert "flattened" not in html_body


# --- extraction and sanitising -----------------------------------------------


def test_active_content_is_removed_and_everything_else_kept() -> None:
    html = (
        '<html><body onload="steal()"><script>alert(1)</script>'
        '<p onclick="x()" style="color:red">Hi <a href="javascript:x()">a</a> '
        '<a href="https://example.com/q">b</a></p>'
        '<iframe src="https://example.com"></iframe><object></object><embed>'
        '<form action="https://example.com"><input></form>'
        '<base href="https://example.com/"><!--[if gte mso 9]><x:o/><![endif]-->'
        "</body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    fragment = quotable.html_fragment
    for gone in (
        "<script",
        "alert(1)",
        "onload",
        "onclick",
        "javascript:",
        "<iframe",
        "<object",
        "<embed",
        "<form",
        "<input",
        "<base",
    ):
        assert gone not in fragment
    for kept in ('style="color:red"', 'href="https://example.com/q"', "[if gte mso 9]"):
        assert kept in fragment


def test_meta_refresh_is_removed_even_without_a_body() -> None:
    html = '<meta http-equiv="refresh" content="0;url=https://example.com/x"><p>Hi</p>'

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    assert "refresh" not in quotable.html_fragment
    assert "<p>Hi</p>" in quotable.html_fragment


def test_script_urls_are_refused_in_any_attribute() -> None:
    html = (
        "<html><body>"
        '<svg><set attributeName="href" to="javascript:alert(1)"/></svg>'
        '<a href="data:text/html,x">d</a>'
        '<p style="width: expression(alert(1))">e</p>'
        '<img src="data:image/png;base64,AAAA">'
        '<p style="color: red">kept</p>'
        "</body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    fragment = quotable.html_fragment
    for gone in ("javascript:", "data:text/html", "expression("):
        assert gone not in fragment
    assert 'src="data:image/png;base64,AAAA"' in fragment
    assert 'style="color: red"' in fragment


def test_conditional_comment_carrying_active_content_is_removed() -> None:
    html = (
        "<html><body><p>Hi</p>"
        "<!--[if mso]><script>alert(1)</script><![endif]-->"
        '<!--[if gte mso 9]><p onclick="x()">y</p><![endif]-->'
        "<!--[if gte vml 1]><v:shape><v:imagedata src='x.png'/></v:shape><![endif]-->"
        "</body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    fragment = quotable.html_fragment
    assert "alert(1)" not in fragment and "onclick" not in fragment
    assert "<p>y</p>" in fragment
    assert "<v:imagedata" in fragment  # Outlook's own VML signature stays
    assert (
        "<!--[if gte vml 1]><v:shape><v:imagedata src='x.png'/></v:shape><![endif]-->"
        in fragment
    )


def test_head_conditional_comments_travel_with_the_head_styles() -> None:
    html = (
        "<html><head><style>p{margin:0}</style>"
        "<!--[if gte mso 9]><xml><o:shapedefaults v:ext='edit'/></xml><![endif]-->"
        "</head><body><p>Hi</p></body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    assert "o:shapedefaults" in quotable.html_fragment
    assert quotable.html_fragment.index("p{margin:0}") < quotable.html_fragment.index(
        "o:shapedefaults"
    )


def test_cid_inside_a_conditional_comment_must_resolve(tmp_path: Path) -> None:
    vml = (
        "<p><!--[if gte vml 1]><v:imagedata src='cid:vml@x'/><![endif]-->"
        '<img src="cid:logo@x"></p>'
    )
    eml = _eml_with_cid(vml, "logo@x")

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "stored_text"
    assert result.quote_note is not None and "vml@x" in result.quote_note


def _eml_with_cid(html_body: str, content_id: str | None) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = SEEDED_MESSAGE_ID
    msg.set_content("x")
    msg.add_alternative(html_body, subtype="html")
    if content_id is not None:
        msg.get_body(preferencelist=("html",)).add_related(
            LOGO_PNG, "image", "png", cid=f"<{content_id}>", filename="a.png"
        )
    return msg.as_bytes()


def test_cid_with_an_html_special_character_still_resolves(tmp_path: Path) -> None:
    eml = _eml_with_cid('<p><img src="cid:img&amp;1@x"></p>', "img&1@x")

    result = _reply(tmp_path, _fetch_returning(eml))

    draft = _draft(result)
    assert result.quote_source == "original"
    assert {str(p["Content-ID"]) for p in draft.walk() if p.get("Content-ID")} == {
        "<img&1@x>"
    }


def test_unresolved_cid_falls_back_to_stored_text(tmp_path: Path) -> None:
    eml = _eml_with_cid('<p><img src="cid:gone@x"></p>', None)

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "stored_text"
    assert result.quote_note is not None and "gone@x" in result.quote_note
    assert "cid:" not in str(_quoted_soup(_draft(result)))


def test_plain_only_original_has_nothing_to_quote_as_html() -> None:
    assert quotable_original_from_rfc822(_original_eml(with_html=False)) is None


def test_mislabelled_charset_is_decoded_like_ingest() -> None:
    msg = EmailMessage()
    msg["Message-ID"] = SEEDED_MESSAGE_ID
    msg.set_content("x")
    msg.add_alternative(
        "<html><body><p>染廠來電，仍未收到藥劑</p></body></html>".encode("big5"),
        "text",
        "html",
        cte="base64",
    )
    html_part = msg.get_body(preferencelist=("html",))
    html_part.set_param("charset", "big5")

    quotable = quotable_original_from_rfc822(msg.as_bytes())

    assert quotable is not None
    assert "染廠來電，仍未收到藥劑" in quotable.html_fragment


# --- fallback is never silent ------------------------------------------------


@pytest.mark.parametrize(
    ("fetch", "note", "expected_notice_phrase"),
    [
        (None, "original fetch not configured", "fix the send settings"),
        (
            _fetch_raising("Original message not found on IMAP server."),
            "Original message not found on IMAP server.",
            "regenerate the draft later, or reply from Outlook",
        ),
        (
            _fetch_returning(_original_eml(with_html=False)),
            "original has no HTML body",
            "the original was plain text",
        ),
    ],
)
def test_unavailable_original_falls_back_to_stored_text_and_says_why(
    tmp_path: Path, fetch, note: str, expected_notice_phrase: str
) -> None:
    result = _reply(tmp_path, fetch)

    assert result.quote_source == "stored_text"
    assert result.quote_note == note
    assert result.quote_notice is not None
    assert expected_notice_phrase in result.quote_notice
    assert "original body" in str(_quoted_soup(_draft(result)))


def test_cid_inside_a_conditional_comment_is_attached(tmp_path: Path) -> None:
    vml = "<p><!--[if gte vml 1]><v:imagedata src='cid:vml@x'/><![endif]-->Hi</p>"
    eml = _eml_with_cid(vml, "vml@x")

    result = _reply(tmp_path, _fetch_returning(eml))

    draft = _draft(result)
    assert result.quote_source == "original"
    assert result.quote_notice is None
    assert {str(p["Content-ID"]) for p in draft.walk() if p.get("Content-ID")} == {
        "<vml@x>"
    }


def test_ordinary_comment_with_cid_does_not_cause_fallback(tmp_path: Path) -> None:
    html = '<html><body><!-- <img src="cid:old@x"> --><p>Hi</p></body></html>'
    eml = _eml_with_cid(html, None)

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "original"
    assert result.quote_notice is None
    assert (
        "cid:old@x" in _draft(result).get_body(preferencelist=("html",)).get_content()
    )


def test_style_tag_import_and_expression_are_caught() -> None:
    html = (
        "<html><head>"
        "<style>@import url(https://evil.example/a.css);</style>"
        "<style>p{width:expression(alert(1))}</style>"
        "<style>p{color:blue}</style>"
        "</head><body><p>Hi</p></body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    assert "@import" not in quotable.html_fragment
    assert "expression(" not in quotable.html_fragment
    assert "color:blue" in quotable.html_fragment
    assert (
        verify_quoted_html("<style>@import 'x.css';</style><p>Hi</p>", ()) is not None
    )
    assert (
        verify_quoted_html("<style>p{width:expression(1)}</style><p>Hi</p>", ())
        is not None
    )


@pytest.mark.parametrize(
    "css",
    [
        r"@\69mport url(https://example.com/a.css);",
        r"@\000069 mport url(https://example.com/a.css);",
        r"@i\mport url(https://example.com/a.css);",
        "@imp/**/ort url(https://example.com/a.css);",
        r"p{width:e\78pression(alert(1))}",
        "p{width:expr/* x */ession(alert(1))}",
        r"p{background:url(java\73 cript:alert(1))}",
    ],
)
def test_css_escapes_and_comments_do_not_hide_active_style(css: str) -> None:
    # A renderer decodes CSS escapes and drops comments before reading the rule.
    assert verify_quoted_html(f"<style>{css}</style><p>Hi</p>", ()) is not None
    assert verify_quoted_html(f'<p style="{css}">Hi</p>', ()) is not None
    html = f"<html><head><style>{css}</style></head><body><p>Hi</p></body></html>"
    quotable = quotable_original_from_rfc822(_original_eml(html))
    assert quotable is not None and quotable.rejection_reason is None
    assert css not in quotable.html_fragment


def test_word_vml_behavior_rule_and_escaped_selector_are_kept() -> None:
    # Every Word-generated mail carries this; it must not count as active.
    css = r"v\:* {behavior:url(#default#VML);} o\:* {behavior:url(#default#VML);}"
    html = f"<html><head><style>{css}</style></head><body><p>Hi</p></body></html>"

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None and quotable.rejection_reason is None
    assert css in quotable.html_fragment


def test_escaped_cid_in_css_must_resolve(tmp_path: Path) -> None:
    html = (
        r'<html><body><p style="background:url(c\69 d:bg@example.com)">'
        "x</p></body></html>"
    )

    result = _reply(tmp_path, _fetch_returning(_eml_with_cid(html, None)))

    assert result.quote_source == "stored_text"
    assert result.quote_note is not None and "bg@example.com" in result.quote_note


def test_body_presentation_travels_with_the_quote() -> None:
    html = (
        '<html><body class="blue" lang="ZH-TW" bgcolor="#FFEECC" text="navy"'
        ' link="#0563C1" vlink="#954F72" background="https://example.com/bg.png"'
        ' style="word-wrap:break-word"><p>Hi</p></body></html>'
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None and quotable.rejection_reason is None
    soup = BeautifulSoup(quotable.html_fragment, "html.parser")
    wrapper = soup.find("div")
    assert wrapper is not None
    assert "blue" in wrapper["class"]
    assert wrapper["lang"] == "ZH-TW"
    style = str(wrapper["style"])
    for kept in (
        "background-color:#FFEECC",
        "color:navy",
        'background-image:url("https://example.com/bg.png")',
        "word-wrap:break-word",
    ):
        assert kept in style
    rules = soup.find("style")
    assert rules is not None
    assert "a:link{color:#0563C1}" in rules.get_text()
    assert "a:visited{color:#954F72}" in rules.get_text()


def test_body_attribute_values_cannot_break_out_of_css() -> None:
    html = (
        '<html><body bgcolor="red;background:url(https://example.com/x)"'
        ' link="red}*{color:red"'
        ' background="https://example.com/a.png&quot;);color:red;x:(&quot;"'
        "><p>Hi</p></body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None and quotable.rejection_reason is None
    wrapper = BeautifulSoup(quotable.html_fragment, "html.parser").find("div")
    assert wrapper is not None
    style = str(wrapper["style"])
    assert "background-color" not in style
    assert style.count('url("') == 1 and '");color:red' not in style
    assert "<style>" not in quotable.html_fragment


def test_outlook_xml_namespace_declaration_is_kept() -> None:
    pi = (
        '<?xml:namespace prefix = "o" ns = "urn:schemas-microsoft-com:office:office" />'
    )
    html = f"<html><body><p>Hi{pi}<o:p></o:p></p></body></html>"

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None and quotable.rejection_reason is None
    assert "<?xml:namespace prefix" in quotable.html_fragment
    assert verify_quoted_html("<?php echo 1 ?><p>Hi</p>", ()) is not None


def test_gate_rejects_unclosed_nested_and_unknown_declarations(
    tmp_path: Path,
) -> None:
    assert verify_quoted_html("<!--[if mso]><p>unclosed", ()) is not None
    assert (
        verify_quoted_html(
            "<!--[if mso]><!--[if gte mso 9]><p>x</p><![endif]--><![endif]-->", ()
        )
        is not None
    )
    assert verify_quoted_html("<!unknown foo><p>Hi</p>", ()) is not None

    eml = _eml_with_cid("<!--[if mso]><p>unclosed", None)
    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "stored_text"
    assert result.quote_note is not None and "unclosed" in result.quote_note
    assert result.quote_notice is not None
    assert "could not be quoted safely" in result.quote_notice


def test_downlevel_revealed_conditional_is_sanitised_and_scanned() -> None:
    html = "<![if !mso]><p onclick='x()'>Hi</p><![endif]>"

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    assert quotable.rejection_reason is None
    assert "onclick" not in quotable.html_fragment
    assert "<p>Hi</p>" in quotable.html_fragment


def test_unresolved_cid_in_link_href_still_quotes_original(tmp_path: Path) -> None:
    html = (
        '<html><body><p><a href="cid:missing@example.com">link</a>'
        '<map><area href="cid:area@example.com" alt="a"></map></p></body></html>'
    )
    eml = _eml_with_cid(html, None)

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "original"
    assert result.quote_notice is None
    draft_html = _draft(result).get_body(preferencelist=("html",)).get_content()
    assert 'href="cid:missing@example.com"' in draft_html
    assert 'href="cid:area@example.com"' in draft_html


def test_matching_cid_in_link_href_still_attaches_part(tmp_path: Path) -> None:
    html = '<html><body><p><a href="cid:doc@example.com">open</a></p></body></html>'
    eml = _eml_with_cid(html, "doc@example.com")

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "original"
    assert result.quote_notice is None
    draft = _draft(result)
    assert {str(p["Content-ID"]) for p in draft.walk() if p.get("Content-ID")} == {
        "<doc@example.com>"
    }


def test_unresolved_cid_in_non_href_attribute_still_falls_back(tmp_path: Path) -> None:
    html = '<html><body><p><a o:href="cid:missing@example.com">x</a></p></body></html>'
    eml = _eml_with_cid(html, None)

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "stored_text"
    assert result.quote_note is not None and "missing@example.com" in result.quote_note


@pytest.mark.parametrize(
    ("opener", "closer"),
    [
        ("<!--[if !mso]><!-->", "<!--<![endif]-->"),
        ("<!--[if !mso]><!-- -->", "<!--<![endif]-->"),
        ("<!--[if gte mso 9]><!-->", "<!--<![endif]-->"),
    ],
)
def test_hybrid_conditional_comments_quote_original_byte_identical(
    tmp_path: Path, opener: str, closer: str
) -> None:
    html = f"<html><body>{opener}<p>Non-Outlook</p>{closer}</body></html>"
    eml = _eml_with_cid(html, None)

    result = _reply(tmp_path, _fetch_returning(eml))

    assert result.quote_source == "original"
    assert result.quote_notice is None
    draft_html = _draft(result).get_body(preferencelist=("html",)).get_content()
    assert f"{opener}<p>Non-Outlook</p>{closer}" in draft_html


def test_active_content_between_hybrid_markers_is_removed() -> None:
    html = (
        "<html><body>"
        "<!--[if !mso]><!--><p onclick='x()'>Hi</p>"
        "<script>alert(1)</script><!--<![endif]-->"
        "</body></html>"
    )

    quotable = quotable_original_from_rfc822(_original_eml(html))

    assert quotable is not None
    assert quotable.rejection_reason is None
    assert "onclick" not in quotable.html_fragment
    assert "<script" not in quotable.html_fragment
    assert "<!--[if !mso]><!--><p>Hi</p><!--<![endif]-->" in quotable.html_fragment


@pytest.mark.parametrize(
    "bad_html",
    [
        "<!--[if !mso]><!--><p>unclosed</p>",
        "<!--[if !mso]><!-- --><p>unclosed</p>",
        "<p>unmatched</p><!--<![endif]-->",
        "<![if !mso]><!--[if !mso]><!--><p>nested</p><!--<![endif]--><![endif]>",
        "<!--[if !mso]><!--><![if !mso]><p>nested</p><![endif]><!--<![endif]-->",
    ],
)
def test_gate_rejects_malformed_or_nested_hybrid_conditionals(bad_html: str) -> None:
    assert verify_quoted_html(bad_html, ()) is not None


def test_cli_reports_the_quote_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _seed_message(tmp_path)
    body = _write(tmp_path / "body.txt", b"Thanks, noted.")
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            SEEDED_MESSAGE_ID,
            "--body-from",
            str(body),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )

    assert _handle_draft_reply(args) == 0

    out = capsys.readouterr().out
    assert "Quote: stored text (" in out
    assert "Quote notice:" in out
    assert "regenerating will not change this" in out


# --- the IMAP fetcher --------------------------------------------------------

IMAP_ENV = {
    "IMAP_HOST": "imap.example.com",
    "IMAP_PORT": "993",
    "IMAP_USER": OWN,
    "IMAP_PASSWORD": "placeholder",
}
IMAP_ANCHOR = {
    "message_id": SEEDED_MESSAGE_ID,
    "pst_source": f"imap:{OWN}:INBOX",
    "source_input_mode": "maildir",
    "folder_path": "INBOX",
    "logical_locator": "INBOX/uid-7",
}


def test_fetcher_returns_the_verified_original_from_imap() -> None:
    original = _original_eml()
    fake = FakeImapClient(FakeImapConnection({7: original}))
    fetch = imap_original_fetcher(IMAP_ENV, client_factory=lambda _env: fake)

    assert fetch(IMAP_ANCHOR) == original
    assert fake.close_calls == 1


def test_fetcher_finds_a_moved_original_by_message_id() -> None:
    original = _original_eml()
    fake = FakeImapClient(
        FakeImapConnection({42: original}, {"test-msg-123@mailattic.test": [42]})
    )
    fetch = imap_original_fetcher(IMAP_ENV, client_factory=lambda _env: fake)

    assert fetch(IMAP_ANCHOR) == original


def test_fetcher_reports_an_original_gone_from_the_server() -> None:
    fake = FakeImapClient(FakeImapConnection({}))
    fetch = imap_original_fetcher(IMAP_ENV, client_factory=lambda _env: fake)

    with pytest.raises(OriginalUnavailable, match="not found"):
        fetch(IMAP_ANCHOR)
    assert fake.close_calls == 1


def test_fetcher_refuses_without_imap_settings() -> None:
    fetch = imap_original_fetcher({"SMTP_USER": OWN})

    with pytest.raises(OriginalUnavailable, match="IMAP"):
        fetch(IMAP_ANCHOR)


def test_fetcher_does_not_reach_outlook_for_a_pst_anchor() -> None:
    fetch = imap_original_fetcher(
        IMAP_ENV, client_factory=lambda _env: pytest.fail("must not connect")
    )
    pst_anchor = {
        **IMAP_ANCHOR,
        "pst_source": "C:/archive/mailbox.pst",
        "source_input_mode": "file",
    }

    with pytest.raises(OriginalUnavailable, match="PST"):
        fetch(pst_anchor)


def test_fetcher_turns_connection_failure_into_unavailable() -> None:
    def broken(_env):
        raise OSError("connection refused")

    fetch = imap_original_fetcher(IMAP_ENV, client_factory=broken)

    with pytest.raises(OriginalUnavailable, match="connection refused") as exc_info:
        fetch(IMAP_ANCHOR)
    assert exc_info.value.category == "temporary"


def test_mcp_draft_reply_returns_quote_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from mailattic.mcp_server import create_server

    db_path = _seed_message(tmp_path)
    env_file = tmp_path / "smtp.env"
    env_file.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    monkeypatch.setenv("MAILATTIC_SMTP_ENV_FILE", str(env_file))

    server = create_server(database_path=db_path)
    res = asyncio.run(
        server.call_tool(
            "mail_draft_reply",
            {"message_id": SEEDED_MESSAGE_ID, "body_text": "Noted."},
        )
    )
    data = res.structured_content
    assert data is not None
    assert data["quote_source"] == "stored_text"
    assert data["quote_notice"] is not None
    assert "regenerating will not change this" in data["quote_notice"]
