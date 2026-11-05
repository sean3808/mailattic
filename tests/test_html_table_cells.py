"""HTML table fidelity in the stored text (#14, phase 1).

A table is either rendered so every value keeps its column position, or is
preceded by the table fidelity warning line. The table is laid out as a grid
(colspan / rowspan repeat the cell text in every slot they cover), a data table
(a leaf table with at least one row of two or more filled slots) writes one
line per row with `` | `` between slots, and anything else stays plain.

Tests marked "pins unchanged behaviour" are expected to be green against the
code before #14 phase 1; every other test encodes new behaviour.
"""

from __future__ import annotations

import time

import pytest

from mailattic.parser import _html_to_text


def _lines(html: str) -> list[str]:
    return _html_to_text(html).splitlines()


def _warning() -> str:
    # Looked up inside each test so a missing constant fails that test only.
    import mailattic.parser as parser

    return parser.TABLE_FIDELITY_WARNING


# --- existing behaviour the spec keeps ------------------------------------


def test_empty_cell_keeps_its_column_slot() -> None:
    html = (
        "<table>"
        "<tr><th>品名</th><th>單價</th><th>數量</th></tr>"
        "<tr><td>PEN-100</td><td></td><td>5</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["品名 | 單價 | 數量", "PEN-100 |  | 5"]


def test_nbsp_only_cell_counts_as_empty() -> None:
    html = "<table><tr><td>PEN-100</td><td>&nbsp;</td><td>5</td></tr></table>"
    assert _lines(html) == ["PEN-100 |  | 5"]


def test_multiword_cell_stays_one_cell() -> None:
    html = "<table><tr><td>Blue  ink\n pen</td><td>450</td></tr></table>"
    assert _lines(html) == ["Blue ink pen | 450"]


def test_paragraph_wrapped_cells_are_still_columns() -> None:
    # Outlook/Word HTML wraps every cell's text in <p class=MsoNormal>.
    html = (
        "<table><tr>"
        "<td><p class=MsoNormal>PEN-100</p></td>"
        "<td><p class=MsoNormal>&nbsp;</p></td>"
        "<td><p class=MsoNormal><span>5</span></p></td>"
        "</tr></table>"
    )
    assert _lines(html) == ["PEN-100 |  | 5"]


def test_table_left_open_still_splits_cells() -> None:
    assert _lines("<p>Hi</p><table><tr><td>A<td><td>5") == ["Hi", "A |  | 5"]


def test_omitted_end_tags_still_split_cells_and_rows() -> None:
    html = "<table><tr><td>A<td><td>5<tr><td>B<td>2<td>3</table>"
    assert _lines(html) == ["A |  | 5", "B | 2 | 3"]


def test_inner_data_table_is_delimited_inside_a_layout_table() -> None:
    # Pins unchanged behaviour: the outer table holds a nested table, so it is
    # a layout table; the inner leaf table is delimited on its own.
    html = (
        "<table><tr><td>"
        "<table><tr><td>A</td><td></td><td>5</td></tr></table>"
        "</td><td>side</td></tr></table>"
    )
    assert _lines(html) == ["A |  | 5", "side"]


def test_nested_table_makes_outer_layout_and_inner_data_delimited() -> None:
    # Pins unchanged behaviour: outer row has two filled slots but contains a
    # nested table, so it is not delimited.
    html = (
        "<table><tr><td>Left</td><td>"
        "<table><tr><td>A</td><td>B</td></tr></table>"
        "</td></tr></table>"
    )
    assert _lines(html) == ["Left", "A | B"]


def test_cells_outside_a_table_keep_previous_output() -> None:
    # Pins unchanged behaviour.
    html = "<div><td>A</td><td>B</td></div>"
    assert _lines(html) == ["A B"]


# --- layout tables --------------------------------------------------------


def test_layout_table_with_one_filled_cell_is_unchanged() -> None:
    # Pins unchanged behaviour: no row has >= 2 filled slots, so no warning.
    html = "<table><tr><td></td><td>Dear Owner, price 450</td><td></td></tr></table>"
    assert _lines(html) == ["Dear Owner, price 450"]


def test_layout_table_keeps_line_structure_inside_cells() -> None:
    # Pins unchanged behaviour.
    html = (
        "<table>"
        "<tr><td>Hello<br>World</td><td></td></tr>"
        "<tr><td><p>Second</p></td></tr>"
        "</table>"
    )
    assert _lines(html) == ["Hello", "World", "Second"]


# --- grid placement -------------------------------------------------------


def test_colspan_repeats_value_across_slots() -> None:
    html = (
        "<table>"
        "<tr><td colspan=2>X</td><td>5</td></tr>"
        "<tr><td>a</td><td>b</td><td>c</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["X | X | 5", "a | b | c"]


def test_rowspan_repeats_value_down() -> None:
    html = "<table><tr><td rowspan=2>K</td><td>1</td></tr><tr><td>2</td></tr></table>"
    assert _lines(html) == ["K | 1", "K | 2"]


def test_mixed_colspan_and_rowspan_push_later_cells_right() -> None:
    html = (
        "<table>"
        "<tr><td rowspan=2>A</td><td colspan=2>B</td></tr>"
        "<tr><td>C</td><td>D</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["A | B | B", "A | C | D"]


def test_ragged_short_row_pads_with_empty_slots() -> None:
    # Assumption: padded trailing slots stay in the line (no rstrip), so the
    # line ends with the separator.
    html = (
        "<table>"
        "<tr><td>a</td><td>b</td><td>c</td></tr>"
        "<tr><td>d</td><td>e</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["a | b | c", "d | e | "]


# --- row rules in a data table -------------------------------------------


def test_single_filled_row_in_data_table_keeps_its_position() -> None:
    html = (
        "<table>"
        "<tr><td>H1</td><td>H2</td><td>H3</td></tr>"
        "<tr><td></td><td></td><td>5</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["H1 | H2 | H3", " |  | 5"]


def test_full_width_title_row_stays_plain() -> None:
    html = (
        "<table>"
        "<tr><td colspan=3>Quarterly list</td></tr>"
        "<tr><td>a</td><td>b</td><td>c</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["Quarterly list", "a | b | c"]


def test_all_empty_row_yields_no_line() -> None:
    html = (
        "<table>"
        "<tr><td>a</td><td>b</td></tr>"
        "<tr><td></td><td>&nbsp;</td></tr>"
        "<tr><td>c</td><td>d</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["a | b", "c | d"]


def test_cell_with_line_break_is_joined_with_a_space() -> None:
    html = "<table><tr><td>Line one<br>Line two</td><td>X</td></tr></table>"
    assert _lines(html) == ["Line one Line two | X"]


def test_cell_with_two_paragraphs_is_joined_with_a_space() -> None:
    html = "<table><tr><td><p>Alpha</p><p>Beta</p></td><td>Y</td></tr></table>"
    assert _lines(html) == ["Alpha Beta | Y"]


# --- warning --------------------------------------------------------------


def test_text_outside_cells_warns_and_still_delimits() -> None:
    # Assumption: where the stray word itself lands is not pinned; only that
    # the warning comes first and the cells are still delimited.
    html = "<table><tr>stray<td>A</td><td>B</td></tr></table>"
    lines = _lines(html)
    assert lines[0] == _warning()
    assert "A | B" in "\n".join(lines[1:])


def test_warning_appears_once_per_table_right_before_its_first_line() -> None:
    html = (
        "<p>Intro</p>"
        "<table>"
        "<tr>s1<td>A</td><td>B</td></tr>"
        "<tr>s2<td>C</td><td>D</td></tr>"
        "</table>"
        "<p>Outro</p>"
    )
    lines = _lines(html)
    assert lines[0] == "Intro"
    assert lines[1] == _warning()
    assert lines.count(_warning()) == 1
    assert lines[-1] == "Outro"


def test_prose_cell_with_more_than_five_lines_warns_and_renders_plain() -> None:
    html = "<table><tr><td>l1<br>l2<br>l3<br>l4<br>l5<br>l6</td><td>X</td></tr></table>"
    assert _lines(html) == [_warning(), "l1", "l2", "l3", "l4", "l5", "l6 X"]


def test_cell_with_exactly_five_lines_is_not_prose() -> None:
    html = "<table><tr><td>l1<br>l2<br>l3<br>l4<br>l5</td><td>X</td></tr></table>"
    assert _lines(html) == ["l1 l2 l3 l4 l5 | X"]


def test_cell_over_200_characters_warns_and_renders_plain() -> None:
    long_text = "w" * 201
    html = f"<table><tr><td>{long_text}</td><td>X</td></tr></table>"
    assert _lines(html) == [_warning(), f"{long_text} X"]


def test_cell_of_exactly_200_characters_is_not_prose() -> None:
    text = "w" * 200
    html = f"<table><tr><td>{text}</td><td>X</td></tr></table>"
    assert _lines(html) == [f"{text} | X"]


def test_clamped_colspan_warns_and_still_delimits() -> None:
    html = "<table><tr><td colspan=101>X</td><td>5</td></tr></table>"
    expected = " | ".join(["X"] * 100 + ["5"])
    assert _lines(html) == [_warning(), expected]


def test_clamped_rowspan_warns_and_still_delimits() -> None:
    html = "<table><tr><td rowspan=101>K</td><td>1</td></tr><tr><td>2</td></tr></table>"
    assert _lines(html) == [_warning(), "K | 1", "K | 2"]


def test_span_of_exactly_100_is_not_clamped() -> None:
    html = "<table><tr><td colspan=100>X</td><td>5</td></tr></table>"
    assert _lines(html) == [" | ".join(["X"] * 100 + ["5"])]


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_colspan_counts_as_one_without_warning(value: str) -> None:
    # Pins unchanged behaviour (spans were ignored before).
    html = (
        "<table>"
        f"<tr><td colspan={value}>A</td><td>B</td></tr>"
        "<tr><td>C</td><td>D</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["A | B", "C | D"]


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_rowspan_counts_as_one_without_warning(value: str) -> None:
    # Pins unchanged behaviour (spans were ignored before).
    html = (
        "<table>"
        f"<tr><td rowspan={value}>A</td><td>B</td></tr>"
        "<tr><td>C</td><td>D</td></tr>"
        "</table>"
    )
    assert _lines(html) == ["A | B", "C | D"]


# --- outside tables -------------------------------------------------------


def test_text_outside_tables_is_unchanged() -> None:
    # Pins unchanged behaviour.
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<h1>Heading</h1><p>Hello <b>world</b> again</p>"
        "<ul><li>one</li><li>two</li></ul><script>x=1</script>"
        "<table><tr><td>A</td><td>B</td></tr></table>"
        "<p>Trailing</p>loose text<br>end</body></html>"
    )
    assert _lines(html) == [
        "Heading",
        "Hello world again",
        "one",
        "two",
        "A | B",
        "Trailing",
        "loose text",
        "end",
    ]


# --- performance (PC-231) -------------------------------------------------


def test_large_table_converts_quickly() -> None:
    # Pins unchanged behaviour on speed: guards against a quadratic grid.
    rows = "".join(
        "<tr>" + "".join(f"<td>r{r}c{c}</td>" for c in range(20)) + "</tr>"
        for r in range(2000)
    )
    html = f"<table>{rows}</table>"
    start = time.perf_counter()
    lines = _lines(html)
    elapsed = time.perf_counter() - start
    assert len(lines) == 2000
    assert lines[0].startswith("r0c0 | r0c1 |")
    assert elapsed < 1.0


def test_many_rowspan_100_cells_convert_quickly() -> None:
    # Speed pin: every row adds a rowspan=100 cell, so about 100 carried
    # slots live per row. 900 rows stay under the 100,000-slot cap (the real
    # grid, carried slots included; spec-1d) and still render as a grid.
    rows = "".join(
        f"<tr><td rowspan=100>a{r}</td><td>b{r}</td></tr>" for r in range(900)
    )
    html = f"<table>{rows}</table>"
    start = time.perf_counter()
    lines = _lines(html)
    elapsed = time.perf_counter() - start
    assert len(lines) == 900
    assert _warning() not in lines
    assert elapsed < 1.0


# --- review round 1 fixes (spec-1d) ----------------------------------------


def test_nested_table_in_data_row_warns_and_renders_plain() -> None:
    # The value 5 sits in a nested table, so it silently loses its column.
    html = (
        "<table><tr><th>Item</th><th>Qty</th></tr>"
        "<tr><td>PEN</td><td><table><tr><td>5</td></tr></table></td></tr></table>"
    )
    assert _lines(html) == [_warning(), "Item Qty", "PEN", "5"]


def test_two_nested_tables_in_one_row_count_as_two_filled_cells() -> None:
    html = (
        "<table><tr><th>H1</th><th>H2</th></tr><tr>"
        "<td><table><tr><td>L</td></tr></table></td>"
        "<td><table><tr><td>R</td></tr></table></td>"
        "</tr></table>"
    )
    lines = _lines(html)
    assert lines[0] == _warning()
    assert lines.count(_warning()) == 1
    assert lines[1:] == ["H1 H2", "L", "R"]


def test_nested_tables_side_by_side_without_a_text_row_do_not_warn() -> None:
    # Two-column layout: no row has two text cells, so nothing reads as
    # columns that a nested table could fall out of.
    html = (
        "<table><tr>"
        "<td><table><tr><td>L</td></tr></table></td>"
        "<td><table><tr><td>R</td></tr></table></td>"
        "</tr></table>"
    )
    assert _lines(html) == ["L", "R"]


def test_layout_container_with_one_filled_cell_per_row_does_not_warn() -> None:
    # Newsletter shape: each outer row has at most one filled cell (a nested
    # table counts as filled); the inner data table is delimited on its own.
    html = (
        "<table>"
        "<tr><td></td><td><table><tr><td>A</td><td>B</td></tr></table></td></tr>"
        "<tr><td>Footer</td><td></td></tr>"
        "</table>"
    )
    assert _lines(html) == ["A | B", "Footer"]


def test_rowspan_carry_over_beyond_slot_limit_warns_and_stays_fast() -> None:
    # rows x max_row_width stays under the pre-check, but rowspan carry-over
    # widens the real grid to millions of slots.
    rows = "".join(
        "<tr>" + "".join(f"<td rowspan=100>r{r}c{c}</td>" for c in range(100)) + "</tr>"
        for r in range(1000)
    )
    html = f"<table>{rows}</table>"
    start = time.perf_counter()
    lines = _lines(html)
    elapsed = time.perf_counter() - start
    assert lines[0] == _warning()
    assert " | " not in "\n".join(lines)  # plain fallback, no grid
    assert elapsed < 2.0


def test_cells_without_a_row_tag_open_an_implicit_row_and_keep_slots() -> None:
    # spec-1e rule 6 (new behaviour): a td/th opened inside a table with no
    # open row opens a row, as browsers do, so the empty slot is kept.
    html = "<table><td>A</td><td></td><td>5</td></table>"
    assert _lines(html) == ["A |  | 5"]


# --- inline tags do not split words (#14) ------------------------------------


def test_inline_tags_inside_a_number_do_not_split_it() -> None:
    assert _lines("<p>Price NTD<b>8</b>5 or <span>12</span><span>50</span></p>") == [
        "Price NTD85 or 1250"
    ]


def test_inline_tag_inside_a_table_cell_keeps_the_value_whole() -> None:
    html = "<table><tr><td>Qty</td><td>1<span>20</span></td></tr></table>"
    assert _lines(html) == ["Qty | 120"]


def test_whitespace_or_block_tags_still_separate_words() -> None:
    # pins unchanged behaviour
    assert _lines("<p>Hello <b>big</b>\n world</p><p>next</p>") == [
        "Hello big world",
        "next",
    ]
    assert _lines("a<br>b") == ["a", "b"]
    assert _lines("<table><tr><td>A</td><td>B</td></tr></table>") == ["A | B"]


def test_colspan_crossing_a_carried_rowspan_warns() -> None:
    # Review: D claims two columns but B (rowspan) already holds one of them.
    html = (
        "<table>"
        '<tr><td>A</td><td rowspan="2">B</td><td>C</td></tr>'
        '<tr><td colspan="2">D</td></tr>'
        "</table>"
    )
    lines = _lines(html)
    assert lines[0] == _warning()
    assert lines.count(_warning()) == 1
