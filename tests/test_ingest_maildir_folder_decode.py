"""Tests for `ingest.py::resolve_maildir_sources` folder-name decoding.

Closes the second mojibake door (design Decision 1, add-mutf7-folder-codec):
the mbsync / Docker ingestion path reads folder names verbatim off the
maildir directory structure, and those directory names are the IMAP WIRE
name (mbsync writes them that way). `MaildirSource.folder_name` becomes
`folder_path` / `pst_source` / `logical_locator`, so it must carry the
decoded display form — while `MaildirSource.path` must keep pointing at the
real on-disk (wire-named) directory, since that is what every downstream
file read uses to locate messages.
"""

from __future__ import annotations

from pathlib import Path

from mailattic.ingest import resolve_maildir_sources

_ACCOUNT_EMAIL = "sean3808@mailattic.test"
_ACCOUNT_NAME = "sean3808@mailattic.test"
_WIRE_FOLDER = "&W8RO9lCZTv0-"
_DISPLAY_FOLDER = "寄件備份"


def test_mojibake_directory_decodes_to_display_form_folder_name(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / _ACCOUNT_NAME
    (account_root / _WIRE_FOLDER).mkdir(parents=True)

    sources = resolve_maildir_sources(
        maildir_root=tmp_path,
        account_email=_ACCOUNT_EMAIL,
        account_name=_ACCOUNT_NAME,
    )

    assert len(sources) == 1
    source = sources[0]
    # `path` still points at the real on-disk directory (the wire name) --
    # nothing that locates files on disk may depend on the decoded value.
    assert source.path == (account_root / _WIRE_FOLDER).resolve()
    # `folder_name` -- which becomes folder_path / pst_source /
    # logical_locator -- carries the decoded display form.
    assert source.folder_name == _DISPLAY_FOLDER


def test_inbox_directory_name_is_unaffected(tmp_path: Path) -> None:
    account_root = tmp_path / _ACCOUNT_NAME
    (account_root / "INBOX").mkdir(parents=True)

    sources = resolve_maildir_sources(
        maildir_root=tmp_path,
        account_email=_ACCOUNT_EMAIL,
        account_name=_ACCOUNT_NAME,
    )

    assert len(sources) == 1
    source = sources[0]
    assert source.path == (account_root / "INBOX").resolve()
    assert source.folder_name == "INBOX"
