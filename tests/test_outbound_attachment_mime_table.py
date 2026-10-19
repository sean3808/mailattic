"""Tests for the outbound attachment MIME table.

Covers openspec/changes/pin-outbound-attachment-mime-types/specs/outbound-send/spec.md.

Every case here neutralizes the host extension map before asserting, because
the defect being guarded is precisely that the value came from the host: on a
machine with Office installed, `mimetypes.guess_type` answers correctly and an
assertion that consults it is green whether the table exists or not.
"""

from __future__ import annotations

import pytest

from mailattic.outbound import (
    _ATTACHMENT_MIME_TYPES,
    resolve_attachment_mime_type,
)

# The types this project sends under, restated independently of the table the
# implementation reads. Deriving these from `_ATTACHMENT_MIME_TYPES` would make
# the assertion compare the table to itself (PC-079): editing an entry would
# move both sides at once and the test would stay green.
EXPECTED_TYPES = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}


def test_table_covers_the_office_and_interchange_formats() -> None:
    """The suffixes named in the requirement are all pinned, none missing."""
    assert set(_ATTACHMENT_MIME_TYPES) == set(EXPECTED_TYPES)


@pytest.mark.parametrize(("suffix", "expected"), sorted(EXPECTED_TYPES.items()))
def test_pinned_types_resolve_without_the_host_map(
    suffix: str,
    expected: str,
    blind_host_mime_map: None,
) -> None:
    maintype, subtype = resolve_attachment_mime_type(f"report{suffix}")

    assert f"{maintype}/{subtype}" == expected


@pytest.mark.parametrize("filename", ["REPORT.XLSX", "Report.Xlsx", "report.xlsX"])
def test_suffix_match_is_case_insensitive(
    filename: str,
    blind_host_mime_map: None,
) -> None:
    maintype, subtype = resolve_attachment_mime_type(filename)

    assert f"{maintype}/{subtype}" == EXPECTED_TYPES[".xlsx"]


def test_out_of_table_suffix_falls_back_to_octet_stream(
    blind_host_mime_map: None,
) -> None:
    assert resolve_attachment_mime_type("data.q7z") == (
        "application",
        "octet-stream",
    )


def test_file_with_no_suffix_falls_back_to_octet_stream(
    blind_host_mime_map: None,
) -> None:
    assert resolve_attachment_mime_type("Makefile") == ("application", "octet-stream")


def test_out_of_table_suffix_still_uses_the_host_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table owns its eight suffixes and nothing else.

    The long tail keeps the previous behaviour deliberately, so this asserts
    the table does not swallow extensions it never claimed.
    """
    monkeypatch.setattr(
        "mailattic.outbound.mimetypes.guess_type",
        lambda *_args, **_kwargs: ("image/png", None),
    )

    assert resolve_attachment_mime_type("logo.png") == ("image", "png")


def test_pinned_suffix_ignores_a_disagreeing_host_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that answers, but answers wrongly, does not win either.

    Blinding the map proves the table is consulted; this proves it is
    consulted *first*, which is the half a no-Office machine cannot show.
    """
    monkeypatch.setattr(
        "mailattic.outbound.mimetypes.guess_type",
        lambda *_args, **_kwargs: ("application/octet-stream", None),
    )

    maintype, subtype = resolve_attachment_mime_type("quotation.xlsx")

    assert f"{maintype}/{subtype}" == EXPECTED_TYPES[".xlsx"]
