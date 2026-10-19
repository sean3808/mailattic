"""A fresh database gets no coverage-gap notice (#303).

The old stderr notice about sent mail between 2026-05-28 and 2026-07-13 was a
fact about one particular mailbox, printed for every database. It is gone:
querying that date range on a brand-new database must say nothing extra.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailattic.cli import main
from mailattic.storage import initialize_database


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "fresh.db")


@pytest.mark.parametrize(
    "date_args",
    [
        pytest.param(["--from-date", "2026-06-01", "--to-date", "2026-06-02"], id="in"),
        pytest.param([], id="undated"),
    ],
)
@pytest.mark.parametrize("subcommand", ["search", "attachments"])
def test_search_and_attachments_print_no_gap_notice(
    fresh_db: Path,
    capsys: pytest.CaptureFixture[str],
    subcommand: str,
    date_args: list[str],
) -> None:
    exit_code = main([subcommand, "--json", "--database", str(fresh_db), *date_args])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == []
    assert "[notice]" not in captured.err
    assert "#79" not in captured.err


def test_contact_history_prints_no_gap_notice(
    fresh_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "contact-history",
            "--domain",
            "example.com",
            "--format",
            "json",
            "--database",
            str(fresh_db),
            "--date-from",
            "2026-07-13",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[notice]" not in captured.err
    assert "#79" not in captured.err
