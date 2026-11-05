"""Tests for scripts/body_rederive_stage.py (issue #14 phase 2, PST source).

Pins the stage builder's rules: the canonical database is only ever read; a
paired PST row whose re-derived body differs is staged as ``rewrite`` with a
pre-image fingerprint; an identical body is only counted; a Message-ID
mismatch at the same locator is never staged; a row with no Message-ID is
never staged (and the table sub-count is exact); a missing PST is counted and
the run continues; an existing stage is refused without ``--overwrite``.

readpst itself is stubbed: ``ReadpstParser._extract_pst`` writes a real mbox
into the output directory, so everything after unpacking (mbox read, MIME
parse, ``_extract_body_text``, the table decision) is the production code.
All data is synthetic (example.test).
"""

from __future__ import annotations

import hashlib
import json
import mailbox
import sqlite3
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import body_rederive_stage as stage_module  # noqa: E402

from mailattic.parser import ReadpstParser  # noqa: E402
from mailattic.storage import connect_database, initialize_database  # noqa: E402

PLAIN_FLAT = "Item Qty Price A 1 5"
TABLE_HTML = (
    "<html><body><table>"
    "<tr><td>Item</td><td>Qty</td><td>Price</td></tr>"
    "<tr><td>A</td><td>1</td><td>5</td></tr>"
    "</table></body></html>"
)
TABLE_BODY = "Item | Qty | Price\nA | 1 | 5"


def _table_message(message_id: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "sender@example.test"
    msg["To"] = "rcpt@example.test"
    msg["Subject"] = "Quote"
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg.set_content(PLAIN_FLAT)
    msg.add_alternative(TABLE_HTML, subtype="html")
    return msg


def _plain_message(message_id: str | None, text: str = "Hello there") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "sender@example.test"
    msg["To"] = "rcpt@example.test"
    msg["Subject"] = "Plain"
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg.set_content(text)
    return msg


@pytest.fixture
def stub_readpst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Returns (readpst_path, mboxes): mboxes maps a PST file name to the list
    of messages its (fake) unpacked ``Inbox/mbox`` holds."""
    readpst = tmp_path / "stub-readpst.exe"
    readpst.write_text("", encoding="utf-8")
    mboxes: dict[str, list[EmailMessage]] = {}

    def fake_extract(self, source_path: Path, output_root: Path) -> None:
        folder = output_root / "Inbox"
        folder.mkdir(parents=True)
        box = mailbox.mbox(folder / "mbox", create=True)
        try:
            for message in mboxes[source_path.name]:
                box.add(message)
            box.flush()
        finally:
            box.close()

    monkeypatch.setattr(ReadpstParser, "_extract_pst", fake_extract)
    return readpst, mboxes


def _make_pst(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"pst")
    return path


def _insert(
    con: sqlite3.Connection,
    *,
    pst: Path,
    index: int,
    message_id: str | None,
    body: str,
    input_mode: str = "file",
) -> int:
    locator = f"Inbox/mbox#{index}"
    cur = con.execute(
        "INSERT INTO messages (subject, body_text, message_id, pst_source, "
        "folder_path, logical_locator, source_input_mode, dedup_key) "
        "VALUES ('s', ?, ?, ?, 'Inbox', ?, ?, ?)",
        (
            body,
            message_id,
            str(pst.resolve()),
            locator,
            input_mode,
            f"{pst.name}:{index}",
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _row(**fields) -> dict:
    return fields


def _canonical(tmp_path: Path, rows: list[dict]) -> tuple[Path, list[int]]:
    db = initialize_database(tmp_path / "canonical.db")
    con = connect_database(db)
    try:
        ids = [_insert(con, **row) for row in rows]
        con.commit()
    finally:
        con.close()
    return db, ids


def _run(db: Path, stage: Path, readpst: Path, *extra: str) -> int:
    return stage_module.main(
        [
            "pst",
            "--database",
            str(db),
            "--stage",
            str(stage),
            "--readpst-path",
            str(readpst),
            *extra,
        ]
    )


def _report(stage: Path) -> dict:
    return json.loads((stage.parent / f"{stage.stem}-report.json").read_text("utf-8"))


def _staged(stage: Path) -> dict[int, sqlite3.Row]:
    con = sqlite3.connect(stage)
    con.row_factory = sqlite3.Row
    try:
        return {r["message_id"]: r for r in con.execute("SELECT * FROM body_rederive")}
    finally:
        con.close()


def test_table_row_is_staged_as_rewrite_with_pre_image_fingerprint(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message("<m1@example.test>")]
    db, (mid,) = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT)],
    )
    stage = tmp_path / "out" / "stage.db"

    assert _run(db, stage, readpst) == 0

    staged = _staged(stage)
    assert set(staged) == {mid}
    assert staged[mid]["new_body_text"] == TABLE_BODY
    assert staged[mid]["new_body_text"] != PLAIN_FLAT
    assert staged[mid]["status"] == "rewrite"
    assert staged[mid]["source_kind"] == "pst"
    assert (
        staged[mid]["pre_image_sha256"]
        == hashlib.sha256(PLAIN_FLAT.encode("utf-8")).hexdigest()
    )
    report = _report(stage)
    assert report["counts"]["rewrite"] == 1
    assert report["per_pst"][0]["elapsed_seconds"] >= 0


def test_identical_body_is_counted_unchanged_and_not_staged(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_plain_message("<m1@example.test>")]
    db, _ = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m1@example.test>", body="Hello there")],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["unchanged"] == 1
    assert counts["rewrite"] == 0


def test_message_id_mismatch_at_same_locator_is_not_staged(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message("<other@example.test>")]
    db, _ = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["message_id_mismatch"] == 1
    assert counts["rewrite"] == 0


def test_rows_without_message_id_are_never_staged_and_table_subcount_is_exact(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [
        _table_message(None),  # no id, has a data table
        _plain_message(None, "No table here, different text"),  # no id, no table
    ]
    db, _ = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id=None, body=PLAIN_FLAT),
            _row(pst=pst, index=2, message_id=None, body="old body"),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["no_message_id"] == 2
    assert counts["no_message_id_with_table"] == 1
    assert counts["rewrite"] == 0


def test_missing_pst_is_counted_and_run_continues(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    gone = _make_pst(tmp_path, "gone.pst")
    present = _make_pst(tmp_path, "present.pst")
    mboxes["present.pst"] = [_table_message("<m2@example.test>")]
    db, (_, mid2) = _canonical(
        tmp_path,
        [
            _row(pst=gone, index=1, message_id="<m1@example.test>", body="x"),
            _row(pst=present, index=1, message_id="<m2@example.test>", body=PLAIN_FLAT),
        ],
    )
    gone.unlink()
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    counts = _report(stage)["counts"]
    assert counts["pst_missing"] == 1
    assert counts["rewrite"] == 1
    assert set(_staged(stage)) == {mid2}


def test_existing_stage_refused_without_overwrite(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message("<m1@example.test>")]
    db, _ = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"
    stage.write_bytes(b"precious")

    assert _run(db, stage, readpst) == 1
    assert stage.read_bytes() == b"precious"

    assert _run(db, stage, readpst, "--overwrite") == 0
    assert len(_staged(stage)) == 1


def test_canonical_database_bytes_unchanged_and_non_pst_rows_out_of_scope(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message("<m1@example.test>")]
    db, _ = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT),
            _row(
                pst=pst,
                index=2,
                message_id="<imap@example.test>",
                body="imap",
                input_mode="maildir",
            ),
        ],
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert _report(stage)["rows_in_scope"] == 1


def test_locator_not_found_and_pst_filter(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    a = _make_pst(tmp_path, "a.pst")
    b = _make_pst(tmp_path, "b.pst")
    mboxes["a.pst"] = [_table_message("<m1@example.test>")]
    mboxes["b.pst"] = []
    db, _ = _canonical(
        tmp_path,
        [
            _row(pst=a, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT),
            _row(pst=a, index=2, message_id="<m9@example.test>", body="gone"),
            _row(pst=b, index=1, message_id="<m3@example.test>", body="b"),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst, "--pst", str(a)) == 0

    report = _report(stage)
    assert report["rows_in_scope"] == 2
    assert report["counts"]["locator_not_found"] == 1
    assert report["counts"]["rewrite"] == 1
