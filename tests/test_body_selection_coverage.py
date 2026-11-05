"""Token-level coverage guard on switching to the HTML body (#14, spec-1e).

When an HTML part renders a data table and non-empty plain parts exist, the
body switches to the HTML-derived text only if that text covers the plain part.
Both sides are tokenised the same way (NFKC + casefold; a run of ASCII
letters/digits is one token, every other alphanumeric such as a CJK character
is its own token; a token with a digit is a "number token"). The HTML side also
contributes attribute values the extractor saw (``href`` on any tag, ``src`` /
``alt`` on ``<img>``, never ``data:`` values). With ``missing`` = the multiset
difference ``plain - html``: any missing number token -> not covered; other
missing tokens are tolerated up to ``max(2, len(plain_tokens) // 100)``. Not
covered -> the plain body preceded by ``PLAIN_TABLE_FIDELITY_WARNING`` as its
own line.

Everything goes through ``parser._extract_body_text`` so the internals may be
restructured. Test status against the code BEFORE spec-1e (character-bag rule):
tests marked "pins unchanged behaviour" are green already; every other test
encodes new behaviour and must be red first (HTML chosen where plain is
expected, or the reverse).

Assumptions encoded: the warning and the plain body are separated by a newline
(extra leading blank lines in the remainder are tolerated).
"""

from __future__ import annotations

import time
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

import mailattic.parser as parser

PREFIX = "[表格失真提示]"

TABLE_HTML = (
    "<table>"
    "<tr><th>Item</th><th>Qty</th><th>Note</th></tr>"
    "<tr><td>PEN-100</td><td></td><td>5</td></tr>"
    "</table>"
)
TABLE_FLATTENED = "Item\nQty\nNote\nPEN-100\n5"


def _parse(msg: EmailMessage) -> str:
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    return parser._extract_body_text(parsed)


def _alternative(plain: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def _split_warning(body: str) -> tuple[str, str]:
    first, _, rest = body.partition("\n")
    return first, rest.lstrip("\n")


def _assert_html_won(body: str) -> None:
    assert "PEN-100 |  | 5" in body.splitlines()  # HTML-derived grid, slot kept
    assert PREFIX not in body


def _assert_plain_fallback(body: str, plain: str) -> None:
    first, rest = _split_warning(body)
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert rest == plain.strip()


def _word(i: int) -> str:
    """Distinct 4-letter all-alphabetic word (one non-number token)."""
    a, b = divmod(i, 26 * 26)
    b, c = divmod(b, 26)
    return "q" + "".join(chr(97 + n) for n in (a, b, c))


def _tolerance_message(total: int, missing: int) -> tuple[EmailMessage, str]:
    """Plain = `total` word tokens; the HTML text lacks exactly the last `missing`."""
    words = [_word(i) for i in range(total)]
    plain = " ".join(words)
    html = (
        f"<html><body>{TABLE_HTML}<p>{' '.join(words[: total - missing])}</p>"
        "</body></html>"
    )
    return _alternative(plain, html), plain


# --- HTML text wins when it covers the plain part ---------------------------


def test_html_covering_plain_text_is_used_when_plain_has_a_url() -> None:
    # pins unchanged behaviour (old rule stripped URLs; the new rule needs the
    # href values to cover them, so this guards against over-tightening).
    plain = (
        TABLE_FLATTENED
        + "\nSee https://example.test/path?id=123 for details\n"
        + "Contact mailto:someone@example.test or <https://example.test/x>\n"
    )
    html = (
        "<html><body><p>See "
        '<a href="https://example.test/path?id=123">link</a> for details</p>'
        f"{TABLE_HTML}"
        '<p>Contact <a href="mailto:someone@example.test">me</a> or '
        '<a href="https://example.test/x">x</a></p></body></html>'
    )
    _assert_html_won(_parse(_alternative(plain, html)))


@pytest.mark.parametrize(
    ("plain_line", "html_fragment"),
    [
        (
            "Text <https://example.test/p>",
            '<a href="https://example.test/p">Text</a>',
        ),
        (
            "owner@example.test<mailto:owner@example.test>",
            '<a href="mailto:owner@example.test">owner@example.test</a>',
        ),
        (
            "Owner [mailto:owner@example.test]",
            '<a href="mailto:owner@example.test">Owner</a>',
        ),
        (
            "[cid:image001.png@01DA0000.00000000]",
            '<img src="cid:image001.png@01DA0000.00000000">',
        ),
    ],
)
def test_outlook_plain_shapes_covered_by_attributes_use_html(
    plain_line: str, html_fragment: str
) -> None:
    # pins unchanged behaviour (old rule stripped these shapes outright; the new
    # rule must still accept them, via href / src attribute tokens)
    plain = f"{TABLE_FLATTENED}\n{plain_line}\n"
    html = f"<html><body>{TABLE_HTML}<p>{html_fragment}</p></body></html>"
    _assert_html_won(_parse(_alternative(plain, html)))


def test_img_alt_text_supplies_tokens() -> None:
    plain = f"{TABLE_FLATTENED}\nCompany logo banner image\n"
    html = (
        f'<html><body>{TABLE_HTML}<img alt="Company logo banner image" '
        'src="cid:x"></body></html>'
    )
    _assert_html_won(_parse(_alternative(plain, html)))


def test_alt_on_a_non_img_tag_supplies_no_tokens() -> None:
    # pins unchanged behaviour (attributes were never read before spec-1e)
    plain = f"{TABLE_FLATTENED}\nCompany logo banner image\n"
    html = (
        f'<html><body>{TABLE_HTML}<span alt="Company logo banner image">x</span>'
        "</body></html>"
    )
    _assert_plain_fallback(_parse(_alternative(plain, html)), plain)


# --- plain parts the HTML does not cover fall back --------------------------


def test_plain_with_extra_paragraph_falls_back_to_plain_with_warning() -> None:
    extra = "Please confirm the delivery schedule before the end of the week."
    plain = f"{TABLE_FLATTENED}\n\n{extra}\n"
    html = f"<html><body>{TABLE_HTML}</body></html>"
    body = _parse(_alternative(plain, html))
    _assert_plain_fallback(body, plain)
    assert extra in body
    assert " | " not in body  # no HTML-derived grid


@pytest.mark.parametrize(
    ("plain_line", "href"),
    [
        (
            "owner@example.test,URGENT,HOLD,SHIPMENT",
            "mailto:owner@example.test",
        ),
        (
            "URGENT:HOLD:SHIPMENT:owner@example.test",
            "mailto:owner@example.test",
        ),
        (
            "http://x.test/a,SECRET,HOLD,SHIPMENT",
            "http://x.test/a",
        ),
    ],
)
def test_instruction_words_glued_to_an_address_or_url_trigger_fallback(
    plain_line: str, href: str
) -> None:
    # The address / URL is covered by the href, the glued words are not. Three
    # missing words so the case sits above the 2-token tolerance floor. Red now
    # except the "URGENT:..." variant (old rule already caught prefix glue).
    plain = f"{TABLE_FLATTENED}\n{plain_line}\n"
    html = f'<html><body>{TABLE_HTML}<p><a href="{href}">link</a></p></body></html>'
    _assert_plain_fallback(_parse(_alternative(plain, html)), plain)


def test_data_uri_src_never_supplies_tokens() -> None:
    # pins unchanged behaviour (attributes were never read before spec-1e)
    paragraph = "Please confirm the delivery schedule before the end of the week"
    plain = f"{TABLE_FLATTENED}\n{paragraph}\n"
    filler = "QUJD" * 5000  # long base64-looking payload
    html = (
        f"<html><body>{TABLE_HTML}"
        f'<img src="DATA:text/plain;base64,{filler},{paragraph}">'
        "</body></html>"
    )
    _assert_plain_fallback(_parse(_alternative(plain, html)), plain)


def test_tracking_link_letters_cannot_cover_a_missing_sentence() -> None:
    # pins unchanged behaviour. The href holds every letter of the sentence but
    # only as single-letter tokens, so word tokens and the number token "2"
    # stay missing.
    sentence = "Do not ship to warehouse 2"
    plain = f"{TABLE_FLATTENED}\n{sentence}\n"
    letters = "/".join("donotshiptowarehouse2")
    html = (
        f"<html><body>{TABLE_HTML}"
        f'<a href="https://track.example.test/r/{letters}">more</a>'
        "</body></html>"
    )
    _assert_plain_fallback(_parse(_alternative(plain, html)), plain)


def test_missing_number_token_is_never_tolerated() -> None:
    # HTML has 1, 2, 5, 0 elsewhere (like a colspan repeat) but not 1250. Every
    # other plain token is present, so only the number rule can decide this.
    words = " ".join(_word(i) for i in range(300))
    plain = f"Total 1250\n{words}\n"
    html = (
        "<html><body><table>"
        "<tr><th>a</th><th>b</th><th>c</th><th>d</th></tr>"
        "<tr><td>1</td><td>2</td><td>5</td><td>0</td></tr>"
        f"</table><p>Total {words}</p></body></html>"
    )
    _assert_plain_fallback(_parse(_alternative(plain, html)), plain)


# --- non-number tolerance boundary: max(2, total // 100) --------------------


def test_200_tokens_missing_two_uses_html() -> None:
    # pins unchanged behaviour (old rule: ~1% character loss also passed)
    body = _parse(_tolerance_message(200, 2)[0])
    _assert_html_won(body)


def test_200_tokens_missing_three_falls_back() -> None:
    msg, plain = _tolerance_message(200, 3)
    _assert_plain_fallback(_parse(msg), plain)


def test_1000_tokens_missing_ten_uses_html() -> None:
    # pins unchanged behaviour
    _assert_html_won(_parse(_tolerance_message(1000, 10)[0]))


def test_1000_tokens_missing_eleven_falls_back() -> None:
    msg, plain = _tolerance_message(1000, 11)
    _assert_plain_fallback(_parse(msg), plain)


# --- attribute values are entity-unescaped ----------------------------------


def test_href_numeric_entity_is_unescaped_before_tokenising() -> None:
    # pins unchanged behaviour (old rule stripped the URL). "&#49;23" must read
    # as "123", a number token only the href supplies.
    plain = f"{TABLE_FLATTENED}\nref https://example.test/?id=123\n"
    html = (
        f'<html><body>{TABLE_HTML}<p>ref <a href="https://example.test/?id=&#49;23">'
        "link</a></p></body></html>"
    )
    _assert_html_won(_parse(_alternative(plain, html)))


def test_href_ampersand_entity_matches_plain_ampersand() -> None:
    # pins unchanged behaviour
    plain = f"{TABLE_FLATTENED}\nsee https://example.test/?a&b\n"
    html = (
        f'<html><body>{TABLE_HTML}<p>see <a href="https://example.test/?a&amp;b">'
        "link</a></p></body></html>"
    )
    _assert_html_won(_parse(_alternative(plain, html)))


# --- warning constants ------------------------------------------------------


def test_warning_text_is_the_exact_spec_string() -> None:
    assert parser.PLAIN_TABLE_FIDELITY_WARNING == (
        "[表格失真提示] 本信的表格在純文字版中可能已經攤平，"
        "欄位對應無法確定，請勿依文字判斷數值，請對照原信"
    )


def test_both_warning_constants_share_the_prefix() -> None:
    assert parser.TABLE_FIDELITY_WARNING.startswith(PREFIX)
    assert parser.PLAIN_TABLE_FIDELITY_WARNING.startswith(PREFIX)


# --- CJK: every character is its own token ----------------------------------

CJK_PLAIN = "".join(chr(0x4E00 + i) for i in range(100))  # 100 distinct CJK chars


def _cjk_message(kept: int) -> EmailMessage:
    html = f"<html><body>{TABLE_HTML}<p>{CJK_PLAIN[:kept]}</p></body></html>"
    return _alternative(CJK_PLAIN, html)


def test_cjk_paragraph_missing_from_html_triggers_fallback() -> None:
    paragraph = "這是一段預留的測試文字內容用來佔位"
    plain = TABLE_FLATTENED + "\n" + paragraph
    msg = _alternative(plain, f"<html><body>{TABLE_HTML}</body></html>")
    _assert_plain_fallback(_parse(msg), plain)


def test_cjk_two_missing_characters_stay_within_tolerance() -> None:
    # pins unchanged behaviour: 100 tokens -> tolerance max(2, 1) = 2
    _assert_html_won(_parse(_cjk_message(kept=98)))


def test_cjk_three_missing_characters_fall_back() -> None:
    first, rest = _split_warning(_parse(_cjk_message(kept=97)))
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert rest == CJK_PLAIN


# --- big5-labelled HTML part with an invalid byte pair ----------------------

BIG5_PARAGRAPHS = [
    "這是第一段預留文字內容",
    "這是第二段預留文字內容",
    "這是第三段預留文字內容",
]


def test_big5_html_with_invalid_bytes_keeps_plain_paragraphs_and_warns() -> None:
    plain = "\n\n".join(BIG5_PARAGRAPHS)
    # Markup-heavy, sparse-CJK HTML (like sender-client output): with one invalid
    # byte pair, strict big5 fails and charset detection mis-decodes the whole
    # part (probed against the current decoder: every CJK paragraph is lost).
    markup = (
        '<div style="font-family:Arial;font-size:12pt;color:#000">'
        '<span style="mso-line-height-rule:exactly">'
    ) * 20
    body = (
        markup
        + "<table><tr><td>Item</td><td>Qty</td></tr>"
        + "<tr><td>PEN-100</td><td>5</td></tr></table>"
        + "".join(f"<p>{p}</p>" for p in BIG5_PARAGRAPHS)
    ).encode("big5")
    cut = len(markup) + 10
    html_bytes = body[:cut] + b"\xff\xff" + body[cut:]  # invalid big5 byte pair
    raw = (
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="BND"\r\n\r\n'
        b"--BND\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        + plain.encode("utf-8")
        + b"\r\n--BND\r\n"
        b"Content-Type: text/html; charset=big5\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + html_bytes + b"\r\n--BND--\r\n"
    )
    parsed = message_from_bytes(raw, policy=policy.default)
    body = parser._extract_body_text(parsed)
    first, rest = _split_warning(body)
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert rest == plain
    for paragraph in BIG5_PARAGRAPHS:
        assert paragraph in rest


# --- pins unchanged behaviour: no data table / no plain ---------------------


def test_layout_only_table_keeps_plain_without_warning() -> None:
    # pins unchanged behaviour
    html = (
        "<html><body><table><tr><td>Layout one</td></tr>"
        "<tr><td>Layout two</td></tr></table></body></html>"
    )
    body = _parse(_alternative("Plain wins and says more\n", html))
    assert body == "Plain wins and says more"


def test_no_table_keeps_plain_without_warning() -> None:
    # pins unchanged behaviour
    body = _parse(_alternative("Plain wins and says more\n", "<p>Other</p>"))
    assert body == "Plain wins and says more"


def test_html_only_data_table_has_no_plain_warning() -> None:
    # pins unchanged behaviour
    msg = EmailMessage()
    msg.set_content(f"<html><body>{TABLE_HTML}</body></html>", subtype="html")
    body = _parse(msg)
    assert "PEN-100 |  | 5" in body.splitlines()
    assert PREFIX not in body


# --- linear time (300k-character bodies decided in < 2 s) -------------------


def test_matching_large_bodies_are_decided_in_linear_time() -> None:
    # pins unchanged behaviour
    words = " ".join(f"word{i}" for i in range(40_000))  # ~ 300k characters
    plain = f"{words}\n{TABLE_FLATTENED}"
    html = f"<html><body><p>{words}</p>{TABLE_HTML}</body></html>"
    msg = _alternative(plain, html)
    start = time.perf_counter()
    body = _parse(msg)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    _assert_html_won(body)


def test_near_miss_noise_runs_stay_linear() -> None:
    # PC-231: long runs that almost look like an address or URL (no "@", no
    # "://") must not make the decision quadratic. Same run on both sides.
    run = "a" * 200_000 + " " + ("x" * 50 + ".") * 4_000
    plain = f"{run}\n{TABLE_FLATTENED}"
    html = f"<html><body><p>{run}</p>{TABLE_HTML}</body></html>"
    msg = _alternative(plain, html)
    start = time.perf_counter()
    body = _parse(msg)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    _assert_html_won(body)


def test_many_distinct_missing_tokens_fall_back_in_linear_time() -> None:
    words = " ".join(f"word{i}" for i in range(40_000))
    plain = f"{words}\n{TABLE_FLATTENED}"
    html = f"<html><body>{TABLE_HTML}</body></html>"
    msg = _alternative(plain, html)
    start = time.perf_counter()
    body = _parse(msg)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    _assert_plain_fallback(body, plain)


def test_short_plain_body_cannot_be_dropped_whole() -> None:
    # The non-number tolerance never exceeds 10% of the plain tokens, so a
    # two-word plain body missing from the HTML is not "covered".
    html = "<table><tr><td>A</td><td>B</td></tr></table>"
    msg = EmailMessage()
    msg.set_content("Hold shipment\n")
    msg.add_alternative(html, subtype="html")
    body = _parse(msg)
    assert body.splitlines()[0] == parser.PLAIN_TABLE_FIDELITY_WARNING


def test_inline_tags_splitting_letters_from_digits_still_cover() -> None:
    # The HTML text joins inline text nodes with spaces ("NTD 210",
    # "27 th"); letter and digit runs are separate tokens on both sides.
    html = (
        "<p>Price NTD<b>210</b> due 27<sup>th</sup></p>"
        "<table><tr><td>A</td><td>B</td></tr></table>"
    )
    msg = EmailMessage()
    msg.set_content("Price NTD210 due 27th\nA\nB\n")
    msg.add_alternative(html, subtype="html")
    body = _parse(msg)
    assert not body.startswith("[表格失真提示]")
    assert "A | B" in body


def test_number_split_by_an_inline_tag_is_still_covered() -> None:
    # The HTML text turns "2609<span>080</span>" into "2609 080"; the
    # number must still count as present.
    html = (
        "<p>Order NW2609<span>080</span></p>"
        "<table><tr><td>A</td><td>B</td></tr></table>"
    )
    msg = EmailMessage()
    msg.set_content("Order NW2609080\nA\nB\n")
    msg.add_alternative(html, subtype="html")
    body = _parse(msg)
    assert not body.startswith("[表格失真提示]")
    assert "A | B" in body


def test_href_on_a_non_anchor_tag_supplies_nothing() -> None:
    # Review: a <link href> is not visible content and must not cover a
    # number missing from the HTML text.
    html = (
        '<link href="/123"><table><tr><td>Item</td><td>Qty</td></tr>'
        "<tr><td>X</td><td>2</td></tr></table>"
    )
    msg = EmailMessage()
    msg.set_content("Item\nQty\n2\n123\n")
    msg.add_alternative(html, subtype="html")
    body = _parse(msg)
    assert body.splitlines()[0] == parser.PLAIN_TABLE_FIDELITY_WARNING
