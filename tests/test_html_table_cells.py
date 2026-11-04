"""HTML table rows keep their column positions in the stored text (#14, point 5).

Before: cells were joined by spaces and an empty cell vanished, so the values
after it shifted one column left (an empty unit price made the quantity read as
the price). Now a row with two or more filled cells is written as one line with
`` | `` between cells, and an empty cell keeps its slot. Rows the converter
cannot judge (a cell with line breaks inside, a nested table, text outside any
cell, a row with a single filled cell) keep the previous output.
"""

from __future__ import annotations

from mailattic.parser import _html_to_text


def _lines(html: str) -> list[str]:
    return _html_to_text(html).splitlines()


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
    html = (
        "<table><tr><td>"
        "<table><tr><td>A</td><td></td><td>5</td></tr></table>"
        "</td><td>side</td></tr></table>"
    )
    assert _lines(html) == ["A |  | 5", "side"]


def test_layout_row_with_one_filled_cell_is_unchanged() -> None:
    html = "<table><tr><td></td><td>Dear Owner, price 450</td><td></td></tr></table>"
    assert _lines(html) == ["Dear Owner, price 450"]


def test_cell_with_line_break_keeps_previous_output() -> None:
    html = "<table><tr><td>Line one<br>Line two</td><td>X</td></tr></table>"
    assert _lines(html) == ["Line one", "Line two X"]


def test_text_outside_cells_keeps_previous_output() -> None:
    html = "<table><tr>stray<td>A</td><td>B</td></tr></table>"
    assert _lines(html) == ["stray A B"]


def test_cells_outside_a_table_keep_previous_output() -> None:
    html = "<div><td>A</td><td>B</td></div>"
    assert _lines(html) == ["A B"]
