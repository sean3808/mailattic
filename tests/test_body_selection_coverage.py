"""Coverage guard on switching to the HTML body (#14, addendum 1c).

When an HTML part renders a data table and non-empty plain parts exist, the
body switches to the HTML-derived text only if that text covers the plain part:
coverage = 1 - |bag(plain) - bag(html_text)| / |bag(plain)|, where a bag is the
multiset of ``str.isalnum()`` characters of the lowercased text after removing
URL-like tokens and e-mail addresses. Coverage >= 0.95 -> HTML text; below ->
the plain body preceded by ``PLAIN_TABLE_FIDELITY_WARNING`` as its own line.

Tests marked "pins unchanged behaviour" are expected to be green against the
code before this change. Every other test encodes new behaviour and must be
red first: it either reads ``parser.PLAIN_TABLE_FIDELITY_WARNING`` (missing
today -> AttributeError inside the test body) or expects plain where today's
1b selection chooses HTML.

Assumptions encoded (spec-1c is silent): the warning and the plain body are
separated by a newline (extra leading blank lines in the remainder are
tolerated); a coverage of exactly 0.95 counts as covered (">= 0.95").
"""

from __future__ import annotations

import time
from collections import Counter
from email import message_from_bytes, policy
from email.message import EmailMessage

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


def _cells_html(total_chars: int) -> tuple[str, list[str]]:
    """A 2x2 data table whose cells hold `total_chars` alphanumerics in total."""
    base, extra = divmod(total_chars, 4)
    sizes = [base + (1 if i < extra else 0) for i in range(4)]
    letters = "abcd"
    cells = ["".join([letters[i]] * sizes[i]) for i in range(4)]
    return (
        "<html><body><table>"
        f"<tr><td>{cells[0]}</td><td>{cells[1]}</td></tr>"
        f"<tr><td>{cells[2]}</td><td>{cells[3]}</td></tr>"
        "</table></body></html>"
    ), cells


def _boundary_message(missing: int) -> tuple[EmailMessage, str]:
    """Plain part has exactly 100 alnum chars; the HTML lacks exactly `missing`.

    The missing chars are 'z', which never occurs in the HTML text.
    """
    html, cells = _cells_html(100 - missing)
    plain = "\n".join(cells) + "\n" + "z" * missing
    assert sum(ch.isalnum() for ch in plain) == 100
    return _alternative(plain, html), plain.strip()


# --- new behaviour: HTML text when it covers the plain part -----------------


def test_html_covering_plain_text_is_used_even_when_plain_has_a_url() -> None:
    plain = (
        TABLE_FLATTENED
        + "\nSee https://example.test/path?id=123 for details\n"
        + "Contact mailto:someone@example.test or <https://example.test/x>\n"
    )
    html = (
        f"<html><body><p>See for details</p>{TABLE_HTML}<p>Contact or</p></body></html>"
    )
    body = _parse(_alternative(plain, html))
    lines = body.splitlines()
    assert "PEN-100 |  | 5" in lines  # HTML-derived, blank slot kept
    assert PREFIX not in body


def test_email_address_and_cid_tokens_in_plain_do_not_count_against_html() -> None:
    plain = (
        TABLE_FLATTENED
        + "\nReply to someone.else@example.test [cid:image001@example.test]\n"
    )
    html = f"<html><body>{TABLE_HTML}<p>Reply to</p></body></html>"
    body = _parse(_alternative(plain, html))
    assert "PEN-100 |  | 5" in body.splitlines()
    assert PREFIX not in body


def test_plain_with_extra_paragraph_falls_back_to_plain_with_warning() -> None:
    extra = "Please confirm the delivery schedule before the end of the week."
    plain = f"{TABLE_FLATTENED}\n\n{extra}\n"
    html = f"<html><body>{TABLE_HTML}</body></html>"
    body = _parse(_alternative(plain, html))
    first, rest = _split_warning(body)
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert rest == plain.strip()
    assert extra in rest
    assert " | " not in body  # no HTML-derived grid


def test_warning_text_is_the_exact_spec_string() -> None:
    assert parser.PLAIN_TABLE_FIDELITY_WARNING == (
        "[表格失真提示] 本信的表格在純文字版中可能已經攤平，"
        "欄位對應無法確定，請勿依文字判斷數值，請對照原信"
    )


def test_both_warning_constants_share_the_prefix() -> None:
    assert parser.TABLE_FIDELITY_WARNING.startswith(PREFIX)
    assert parser.PLAIN_TABLE_FIDELITY_WARNING.startswith(PREFIX)


# --- boundary: 0.95 ----------------------------------------------------------


def test_exactly_95_percent_coverage_uses_html() -> None:
    msg, _ = _boundary_message(missing=5)  # coverage = 1 - 5/100 = 0.95
    body = _parse(msg)
    assert " | " in body  # HTML-derived grid row
    assert "z" not in body
    assert PREFIX not in body


def test_just_under_95_percent_coverage_uses_plain_with_warning() -> None:
    msg, plain = _boundary_message(missing=6)  # coverage = 0.94
    body = _parse(msg)
    first, rest = _split_warning(body)
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert rest == plain


# --- CJK: coverage counted per character ------------------------------------

CJK_PLAIN = "".join(chr(0x4E00 + i) for i in range(100))  # 100 distinct CJK chars


def _cjk_message(kept: int) -> EmailMessage:
    html = f"<html><body>{TABLE_HTML}<p>{CJK_PLAIN[:kept]}</p></body></html>"
    return _alternative(CJK_PLAIN, html)


def test_cjk_paragraph_missing_from_html_triggers_fallback() -> None:
    msg = _alternative(
        TABLE_FLATTENED + "\n" + "這是一段預留的測試文字內容用來佔位",
        f"<html><body>{TABLE_HTML}</body></html>",
    )
    first, rest = _split_warning(_parse(msg))
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert "這是一段預留的測試文字內容用來佔位" in rest


def test_cjk_small_loss_stays_within_tolerance() -> None:
    body = _parse(_cjk_message(kept=97))  # coverage 0.97 per character
    assert " | " in body
    assert PREFIX not in body


def test_cjk_loss_beyond_tolerance_falls_back_per_character() -> None:
    first, rest = _split_warning(_parse(_cjk_message(kept=90)))  # coverage 0.90
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


# --- pins unchanged behaviour -----------------------------------------------


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


def test_matching_large_bodies_are_decided_in_linear_time() -> None:
    # Guard for rule 4; green before and after unless the bag diff goes quadratic.
    words = " ".join(f"word{i}" for i in range(40_000))  # ~ 300k characters
    plain = f"{words}\n{TABLE_FLATTENED}"
    html = f"<html><body><p>{words}</p>{TABLE_HTML}</body></html>"
    msg = _alternative(plain, html)
    start = time.perf_counter()
    body = _parse(msg)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0
    assert "PEN-100 |  | 5" in body.splitlines()


def test_near_miss_noise_runs_stay_linear() -> None:
    # PC-231: long runs that almost look like an address or URL (no "@", no
    # "://") must not make the coverage check quadratic.
    run = "a" * 200_000 + " " + ("x" * 50 + ".") * 4_000
    start = time.perf_counter()
    assert parser._html_covers_plain(run, run)
    assert time.perf_counter() - start < 2.0


# --- review round 1 fixes (spec-1d): text glued to an address or URL --------


def test_coverage_bag_counts_prefix_glued_to_an_address() -> None:
    assert parser._coverage_bag("URGENT:owner@example.test") == Counter("urgent")


def test_coverage_bag_counts_prefix_glued_to_a_url() -> None:
    assert parser._coverage_bag("see:https://example.test/x") == Counter("see")


def test_coverage_bag_drops_bare_address_url_and_prefixed_tokens() -> None:
    # pins unchanged behaviour
    for token in (
        "owner@example.test",
        "https://example.test/x",
        "<https://example.test/x>",
        "mailto:someone@example.test",
        "cid:image001",
        "[cid:image001@example.test]",
    ):
        assert not parser._coverage_bag(token), token


def test_coverage_bag_keeps_neighbouring_words() -> None:
    # pins unchanged behaviour
    bag = parser._coverage_bag(
        "keep owner@example.test also https://example.test/x end"
    )
    assert bag == Counter("keepalsoend")


def test_plain_instruction_glued_to_an_address_triggers_plain_fallback() -> None:
    plain = f"{TABLE_FLATTENED}\nURGENT:owner@example.test\n"
    html = f"<html><body>{TABLE_HTML}</body></html>"
    first, rest = _split_warning(_parse(_alternative(plain, html)))
    assert first == parser.PLAIN_TABLE_FIDELITY_WARNING
    assert rest == plain.strip()


def test_coverage_bag_drops_outlook_mailto_wrappers() -> None:
    # Outlook plain parts write links as "Name [mailto:x@y]" and
    # "x@y<mailto:x@y>"; the scheme belongs to the address, not the text.
    assert parser._coverage_bag("Owner [mailto:owner@example.test]") == Counter("owner")
    assert parser._coverage_bag("owner@example.test<mailto:owner@example.test>") == (
        Counter()
    )
