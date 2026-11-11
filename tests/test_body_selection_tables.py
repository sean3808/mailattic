"""Body selection when an HTML part carries a data table (#14, addendum 1b).

Sender clients generate the text/plain alternative by flattening tables (one
cell per line, blank cells vanish), so the plain part loses column positions.
When an HTML part holds a data table (a warned one counts) the stored body is
the HTML-derived text; otherwise non-empty plain parts still win and HTML is
only the fallback. A table too large for the grid cap (> 100,000 slots) is
rendered plain with the warning and does not count as a data table.

Tests marked "pins unchanged behaviour" are expected to be green against the
code before this change; every other test encodes new behaviour (red first).
"""

from __future__ import annotations

from email import message_from_bytes, policy
from email.message import EmailMessage

import mailattic.parser as parser

PLAIN_FLATTENED = "Item\nQty\nNote\nPEN-100\n5\n"

DATA_TABLE_HTML = (
    "<html><body><p>Dear Owner,</p>"
    "<table>"
    "<tr><th>Item</th><th>Qty</th><th>Note</th></tr>"
    "<tr><td>PEN-100</td><td></td><td>5</td></tr>"
    "</table><p>Best regards</p></body></html>"
)

LAYOUT_ONLY_HTML = (
    "<html><body>"
    "<table><tr><td>Layout cell one</td></tr>"
    "<tr><td>Layout cell two</td></tr></table>"
    "</body></html>"
)

NO_TABLE_HTML = "<html><body><p>Hello from HTML</p></body></html>"


def _body(msg: EmailMessage) -> str:
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    return parser._extract_body_text(parsed)


def _alternative(plain: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def _html_only(html: str) -> EmailMessage:
    msg = EmailMessage()
    msg.set_content(html, subtype="html")
    return msg


def _big_table(rows: int, cols: int) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>r{r}c{c}</td>" for c in range(cols)) + "</tr>"
        for r in range(rows)
    )
    return f"<html><body><table>{body}</table></body></html>"


# --- new behaviour: HTML wins when it carries a data table ----------------


def test_alternative_with_data_table_uses_html_text_and_keeps_blank_slot() -> None:
    body = _body(_alternative(PLAIN_FLATTENED, DATA_TABLE_HTML))
    lines = body.splitlines()
    assert "Item | Qty | Note" in lines
    assert "PEN-100 |  | 5" in lines
    assert "Dear Owner," in lines
    assert "Best regards" in lines


def test_alternative_with_warned_data_table_uses_html_and_keeps_warning() -> None:
    prose = "word " * 60  # > 200 characters in one cell -> warned, rendered plain
    html = (
        "<html><body><table>"
        f"<tr><td>PEN-100</td><td>{prose}</td></tr>"
        "<tr><td>PEN-200</td><td>7</td></tr>"
        "</table></body></html>"
    )
    body = _body(_alternative("PEN-100\nPEN-200\n", html))
    assert parser.TABLE_FIDELITY_WARNING in body.splitlines()
    assert "PEN-200" in body


def test_data_table_in_one_of_several_html_parts_switches_to_html() -> None:
    msg = EmailMessage()
    msg.set_content(PLAIN_FLATTENED)
    msg.add_alternative(NO_TABLE_HTML, subtype="html")
    # A second html part (same message) carries the data table.
    msg.get_payload()[1].add_alternative(DATA_TABLE_HTML, subtype="html")
    body = _body(msg)
    assert "PEN-100 |  | 5" in body.splitlines()


# --- pins unchanged behaviour: plain wins unless a data table exists -------


def test_alternative_with_layout_only_table_keeps_plain_part() -> None:
    # pins unchanged behaviour
    body = _body(_alternative("Plain wins\n", LAYOUT_ONLY_HTML))
    assert body == "Plain wins"


def test_alternative_without_table_keeps_plain_part() -> None:
    # pins unchanged behaviour
    body = _body(_alternative("Plain wins\n", NO_TABLE_HTML))
    assert body == "Plain wins"


def test_html_only_message_is_html_derived_text() -> None:
    # pins unchanged behaviour
    body = _body(_html_only(DATA_TABLE_HTML))
    lines = body.splitlines()
    assert "Item | Qty | Note" in lines
    assert "PEN-100 |  | 5" in lines


def test_html_only_without_table_is_html_derived_text() -> None:
    # pins unchanged behaviour
    assert _body(_html_only(NO_TABLE_HTML)) == "Hello from HTML"


def test_empty_plain_part_still_falls_back_to_html() -> None:
    # pins unchanged behaviour
    body = _body(_alternative("   \n", NO_TABLE_HTML))
    assert body == "Hello from HTML"


def test_plain_only_message_is_unchanged() -> None:
    # pins unchanged behaviour
    msg = EmailMessage()
    msg.set_content("Just plain\n")
    assert _body(msg) == "Just plain"


# --- total-slot cap -------------------------------------------------------


def test_oversize_table_renders_plain_with_warning_and_is_fast() -> None:
    html = _big_table(1001, 101)  # 101,101 slots > 100,000
    text = parser._html_to_text(html)
    lines = text.splitlines()
    assert lines[0] == parser.TABLE_FIDELITY_WARNING
    assert " | " not in text
    assert "r0c0 r0c1" in lines[1]
    assert "r1000c100" in text


def test_oversize_table_layout_work_stays_within_the_cap(monkeypatch) -> None:
    # The cap bounds layout work, so measure work, not wall-clock time: a
    # host under load made a 1-second threshold flaky (#61). The input has
    # about twice the cap in cells; laying out every cell before rejecting
    # the table (the slow path the cap exists to prevent) visits them all.
    visited = 0
    real_cell_text = parser._cell_text

    def counting_cell_text(*args, **kwargs):
        nonlocal visited
        visited += 1
        return real_cell_text(*args, **kwargs)

    monkeypatch.setattr(parser, "_cell_text", counting_cell_text)
    text = parser._html_to_text(_big_table(1001, 200))  # 200,200 cells
    assert text.splitlines()[0] == parser.TABLE_FIDELITY_WARNING
    assert visited <= parser._HTML_GRID_SLOT_LIMIT + 1


def test_table_at_the_cap_is_still_a_grid() -> None:
    # 1000 x 100 = 100,000 slots is allowed (the cap is "exceeds 100,000").
    # Behaviour check of the boundary; green before and after the change.
    text = parser._html_to_text(_big_table(1000, 100))
    lines = text.splitlines()
    assert parser.TABLE_FIDELITY_WARNING not in lines
    assert lines[0].startswith("r0c0 | r0c1 | ")


def test_oversize_table_in_html_keeps_a_warning_on_the_plain_body() -> None:
    # Review round 2: a warned table must not be dropped with the HTML. The
    # oversize table is warned, and this HTML does not cover the plain text,
    # so the plain body is kept with the plain-part warning.
    body = _body(_alternative("Plain wins\n", _big_table(1001, 101)))
    assert body == f"{parser.PLAIN_TABLE_FIDELITY_WARNING}\nPlain wins"


def test_warned_nested_table_survives_a_plain_alternative() -> None:
    # Review round 2 repro: the plain part flattens the table; the HTML has a
    # nested table inside a data row (warned, not a data table).
    html = (
        "<table><tr><th>Item</th><th>Qty</th></tr>"
        "<tr><td>PEN</td><td><table><tr><td>5</td></tr></table></td></tr></table>"
    )
    body = _body(_alternative("Item\nQty\nPEN\n5\n", html))
    assert body.splitlines()[0].startswith("[表格失真提示]")
