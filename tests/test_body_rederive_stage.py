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
import imaplib
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

from mailattic.imap_native import NativeImapClient  # noqa: E402
from mailattic.mutf7 import encode_mutf7  # noqa: E402
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
    assert staged[mid]["expected_message_id"] == "m1@example.test"
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


def test_pst_map_reads_a_moved_pst_while_pairing_by_canonical_source(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _make_pst(tmp_path / "old", "moved.pst")
    db, (mid,) = _canonical(
        tmp_path,
        [_row(pst=old, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT)],
    )
    old.unlink()
    new = _make_pst(tmp_path / "new", "moved-here.pst")
    mboxes["moved-here.pst"] = [_table_message("<m1@example.test>")]
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst, "--pst-map", f"{old.resolve()}={new}") == 0

    report = _report(stage)
    assert report["counts"]["pst_missing"] == 0
    assert report["counts"]["rewrite"] == 1
    assert report["per_pst"][0]["pst_source"] == str(old.resolve())
    assert report["per_pst"][0]["read_from"] == str(new)
    assert set(_staged(stage)) == {mid}


def test_unreadable_pst_is_counted_separately_and_exits_nonzero(
    tmp_path, stub_readpst, monkeypatch
):
    readpst, mboxes = stub_readpst
    locked = _make_pst(tmp_path, "locked.pst")
    ok = _make_pst(tmp_path, "ok.pst")
    mboxes["ok.pst"] = [_table_message("<m2@example.test>")]
    db, (_, mid2) = _canonical(
        tmp_path,
        [
            _row(pst=locked, index=1, message_id="<m1@example.test>", body="x"),
            _row(pst=ok, index=1, message_id="<m2@example.test>", body=PLAIN_FLAT),
        ],
    )
    stubbed = ReadpstParser._extract_pst

    def extract_or_fail(self, source_path: Path, output_root: Path) -> None:
        if source_path.name == "locked.pst":
            raise RuntimeError(f"readpst failed for {source_path}: Error opening File")
        stubbed(self, source_path, output_root)

    monkeypatch.setattr(ReadpstParser, "_extract_pst", extract_or_fail)
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 2

    counts = _report(stage)["counts"]
    assert counts["pst_unreadable"] == 1
    assert counts["parse_failure"] == 0
    assert counts["rewrite"] == 1
    assert set(_staged(stage)) == {mid2}


def test_pst_map_key_not_in_canonical_is_refused_before_any_stage(
    tmp_path, stub_readpst
):
    readpst, _ = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    db, _ = _canonical(
        tmp_path, [_row(pst=pst, index=1, message_id="<m1@example.test>", body="x")]
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst, "--pst-map", f"{tmp_path / 'typo.pst'}={pst}") == 1
    assert not stage.exists()


def test_overwrite_with_bad_pst_map_keeps_the_previous_stage(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message("<m1@example.test>")]
    db, _ = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"
    assert _run(db, stage, readpst) == 0
    before = stage.read_bytes()

    code = _run(
        db,
        stage,
        readpst,
        "--overwrite",
        "--pst-map",
        f"{tmp_path / 'typo.pst'}={pst}",
    )

    assert code == 1
    assert stage.exists()
    assert stage.read_bytes() == before


def test_imap_overwrite_keeps_previous_stage_when_connection_fails(
    tmp_path, fake_imap, monkeypatch
):
    env_file, _install = fake_imap
    db, _ = _imap_canonical(
        tmp_path,
        [_maildir_row("INBOX/new/uid-11.eml", "<i1@example.test>", PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"
    stage.write_bytes(b"precious")

    def refuse(env):
        raise OSError("connection refused")

    monkeypatch.setattr(stage_module, "NativeImapClient", refuse)

    assert _run_imap(db, stage, env_file, "--overwrite") == 1
    assert stage.read_bytes() == b"precious"


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


# ---------------------------------------------------------------------------
# Message-ID fallback (rows whose locator pairing failed because the PST
# changed after import).
# ---------------------------------------------------------------------------

M1 = "<m1@example.test>"
M2 = "<m2@example.test>"


def test_shifted_locator_in_same_pst_is_staged_via_message_id_fallback(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [
        _plain_message("<other@example.test>"),  # now occupies #1
        _table_message(M1),  # shifted to #2
        _table_message(M2),  # at #3; the row still says #9
    ]
    db, (mid1, mid2) = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id=M1, body=PLAIN_FLAT),
            _row(pst=pst, index=9, message_id=M2, body=PLAIN_FLAT),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    staged = _staged(stage)
    assert set(staged) == {mid1, mid2}
    assert staged[mid1]["new_body_text"] == TABLE_BODY
    assert staged[mid1]["expected_message_id"] == "m1@example.test"
    assert (
        staged[mid1]["pre_image_sha256"]
        == hashlib.sha256(PLAIN_FLAT.encode("utf-8")).hexdigest()
    )
    counts = _report(stage)["counts"]
    assert counts["rewrite"] == 2
    assert counts["paired_by_message_id"] == 2
    assert counts["message_id_mismatch"] == 0
    assert counts["locator_not_found"] == 0


def test_row_of_pst_a_found_only_in_pst_b_is_staged_and_attributed_to_a(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    a = _make_pst(tmp_path, "a.pst")
    b = _make_pst(tmp_path, "b.pst")
    mboxes["a.pst"] = [_plain_message("<other@example.test>")]
    mboxes["b.pst"] = [_plain_message("<b1@example.test>"), _table_message(M1)]
    db, (mid_a, _mid_b) = _canonical(
        tmp_path,
        [
            _row(pst=a, index=1, message_id=M1, body=PLAIN_FLAT),
            _row(pst=b, index=1, message_id="<b1@example.test>", body="Hello there"),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert set(_staged(stage)) == {mid_a}
    report = _report(stage)
    by_pst = {Path(p["pst_source"]).name: p["counts"] for p in report["per_pst"]}
    assert by_pst["a.pst"]["rewrite"] == 1
    assert by_pst["a.pst"]["paired_by_message_id"] == 1
    assert by_pst["b.pst"]["rewrite"] == 0
    assert by_pst["b.pst"]["unchanged"] == 1
    assert by_pst["b.pst"]["paired_by_message_id"] == 0
    assert report["counts"]["rewrite"] == 1


def test_same_message_id_twice_with_identical_bodies_is_staged_once(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [
        _plain_message("<other@example.test>"),
        _table_message(M1),
        _table_message(M1),
    ]
    db, (mid,) = _canonical(
        tmp_path, [_row(pst=pst, index=9, message_id=M1, body=PLAIN_FLAT)]
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert set(_staged(stage)) == {mid}
    counts = _report(stage)["counts"]
    assert counts["rewrite"] == 1
    assert counts["message_id_ambiguous"] == 0


def test_same_message_id_with_different_bodies_is_ambiguous_and_not_staged(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [
        _plain_message("<other@example.test>"),
        _table_message(M1),
        _plain_message(M1, "A different body entirely"),
    ]
    db, _ = _canonical(
        tmp_path, [_row(pst=pst, index=9, message_id=M1, body=PLAIN_FLAT)]
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["message_id_ambiguous"] == 1
    assert counts["locator_not_found"] == 0
    assert counts["rewrite"] == 0
    assert counts["unchanged"] == 0


def test_message_id_absent_everywhere_keeps_the_original_locator_reason(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_plain_message("<other@example.test>")]
    db, _ = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id=M1, body=PLAIN_FLAT),  # mismatch
            _row(pst=pst, index=9, message_id=M2, body=PLAIN_FLAT),  # not found
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["message_id_mismatch"] == 1
    assert counts["locator_not_found"] == 1
    assert counts["message_id_ambiguous"] == 0
    assert counts["paired_by_message_id"] == 0


def test_rows_without_message_id_never_use_the_fallback(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    # An id-less extracted message exists, but the id-less row's locator (#9)
    # finds nothing: it must stay locator_not_found, never be paired to it.
    mboxes["a.pst"] = [_table_message(None)]
    db, _ = _canonical(
        tmp_path, [_row(pst=pst, index=9, message_id=None, body=PLAIN_FLAT)]
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["locator_not_found"] == 1
    assert counts["rewrite"] == 0
    assert counts["paired_by_message_id"] == 0


def test_locator_paired_row_is_not_repaired_by_the_fallback(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    # The row pairs at #1; an identical copy of the same message sits at #2.
    mboxes["a.pst"] = [_table_message(M1), _table_message(M1)]
    db, (mid,) = _canonical(
        tmp_path, [_row(pst=pst, index=1, message_id=M1, body=PLAIN_FLAT)]
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert set(_staged(stage)) == {mid}
    counts = _report(stage)["counts"]
    assert counts["rewrite"] == 1
    assert counts["paired_by_message_id"] == 0


def test_fallback_database_is_removed_after_a_run(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message(M1)]
    db, _ = _canonical(
        tmp_path, [_row(pst=pst, index=5, message_id=M1, body=PLAIN_FLAT)]
    )
    stage = tmp_path / "out" / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert not (tmp_path / "out" / "stage-work").exists()


def test_leftover_fallback_database_from_a_killed_run_does_not_block(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message(M1)]
    db, _ = _canonical(
        tmp_path, [_row(pst=pst, index=5, message_id=M1, body=PLAIN_FLAT)]
    )
    stage = tmp_path / "out" / "stage.db"
    work = tmp_path / "out" / "stage-work"
    work.mkdir(parents=True)
    leftover = sqlite3.connect(work / "message-id-fallback.db")
    leftover.executescript(
        "CREATE TABLE extracted (x); CREATE TABLE pool (x); "
        "INSERT INTO pool VALUES (1);"
    )
    leftover.commit()
    leftover.close()

    assert _run(db, stage, readpst) == 0

    assert len(_staged(stage)) == 1
    assert not work.exists()


class _Boom(Exception):
    pass


def test_fallback_database_is_removed_when_the_run_raises(
    tmp_path, stub_readpst, monkeypatch
):
    readpst, mboxes = stub_readpst
    first = _make_pst(tmp_path, "a.pst")
    second = _make_pst(tmp_path, "b.pst")
    mboxes["a.pst"] = [_table_message(M1)]
    db, _ = _canonical(
        tmp_path,
        [
            _row(pst=first, index=5, message_id=M1, body=PLAIN_FLAT),
            _row(pst=second, index=1, message_id=M2, body="x"),
        ],
    )
    stage = tmp_path / "out" / "stage.db"
    work = tmp_path / "out" / "stage-work"
    stubbed = ReadpstParser._extract_pst
    seen_during_run: list[bool] = []

    def extract_or_blow_up(self, source_path: Path, output_root: Path) -> None:
        if source_path.name == "b.pst":
            seen_during_run.append((work / "message-id-fallback.db").exists())
            raise _Boom("unexpected failure mid-run")
        stubbed(self, source_path, output_root)

    monkeypatch.setattr(ReadpstParser, "_extract_pst", extract_or_blow_up)

    with pytest.raises(_Boom):
        _run(db, stage, readpst)

    assert seen_during_run == [True]  # it did exist mid-run
    assert not work.exists()


def test_extracted_messages_are_reported_per_pst(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    a = _make_pst(tmp_path, "a.pst")
    gone = _make_pst(tmp_path, "gone.pst")
    mboxes["a.pst"] = [
        _plain_message("<x1@example.test>"),
        _plain_message(None, "id-less"),
        _plain_message("<x3@example.test>"),
    ]
    db, _ = _canonical(
        tmp_path,
        [
            _row(pst=a, index=1, message_id="<x1@example.test>", body="Hello there"),
            _row(pst=gone, index=1, message_id=M1, body="x"),
        ],
    )
    gone.unlink()
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    by_pst = {Path(p["pst_source"]).name: p for p in _report(stage)["per_pst"]}
    assert by_pst["a.pst"]["extracted_messages"] == 3
    assert by_pst["gone.pst"]["extracted_messages"] == 0


# ---------------------------------------------------------------------------
# imap subcommand. The IMAP connection is a recording fake injected under
# ``NativeImapClient``; everything after the wire (LIST parsing, wire-name
# mapping, ``fetch_rfc822``, MIME parse, ``_extract_body_text``) is production
# code. No network. Data is synthetic (example.test).
# ---------------------------------------------------------------------------

_READ_ONLY_COMMANDS = {"LIST", "STATUS", "SELECT", "UID FETCH", "CLOSE", "LOGOUT"}


class FakeImapConnection:
    """imaplib-shaped fake. ``folders`` maps wire name -> {uid: rfc822 bytes}."""

    def __init__(
        self,
        folders: dict[str, dict[int, bytes]],
        uidvalidity: int = 7,
        drop_on_fetch_number: int | None = None,
    ) -> None:
        self.folders = folders
        self.uidvalidity = uidvalidity
        self.drop_on_fetch_number = drop_on_fetch_number
        self.commands: list[tuple] = []
        self.selected: str | None = None
        self.fetches = 0

    def list(self):
        self.commands.append(("LIST",))
        return "OK", [f'() "/" "{wire}"'.encode() for wire in self.folders]

    def status(self, quoted, what):
        self.commands.append(("STATUS", quoted, what))
        return "OK", [f"{quoted} (UIDVALIDITY {self.uidvalidity})".encode()]

    def select(self, quoted, readonly=False):
        self.commands.append(("SELECT", quoted, readonly))
        self.selected = quoted.strip('"')
        return "OK", [b"1"]

    def uid(self, command, uid, what):
        self.commands.append((f"UID {command}", uid, what))
        self.fetches += 1
        if self.drop_on_fetch_number == self.fetches:
            raise imaplib.IMAP4.abort("connection dropped")
        assert self.selected is not None
        body = self.folders[self.selected].get(int(uid))
        if body is None:
            return "OK", [None]
        return "OK", [(f"1 (UID {uid} BODY[] {{{len(body)}}}".encode(), body), b")"]

    def close(self):
        self.commands.append(("CLOSE",))

    def logout(self):
        self.commands.append(("LOGOUT",))

    def names(self) -> list[str]:
        return [c[0] for c in self.commands]


def _eml(message: EmailMessage) -> bytes:
    return message.as_bytes()


def _maildir_row(locator: str, message_id: str | None, body: str, mode="maildir"):
    return {
        "locator": locator,
        "message_id": message_id,
        "body": body,
        "mode": mode,
    }


def _imap_canonical(tmp_path: Path, rows: list[dict]) -> tuple[Path, list[int]]:
    db = initialize_database(tmp_path / "canonical.db")
    con = connect_database(db)
    ids = []
    try:
        for index, row in enumerate(rows):
            cur = con.execute(
                "INSERT INTO messages (subject, body_text, message_id, pst_source, "
                "folder_path, logical_locator, source_input_mode, dedup_key) "
                "VALUES ('s', ?, ?, 'maildir-placeholder', 'f', ?, ?, ?)",
                (
                    row["body"],
                    row["message_id"],
                    row["locator"],
                    row["mode"],
                    f"imap-test:{index}",
                ),
            )
            assert cur.lastrowid is not None
            ids.append(cur.lastrowid)
        con.commit()
    finally:
        con.close()
    return db, ids


@pytest.fixture
def fake_imap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Returns (env_file, install) where install(fake) routes the script's
    client construction to a NativeImapClient wrapping *fake*."""
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(
        "\n".join(
            [
                "IMAP_HOST=imap.example.test",
                "IMAP_PORT=993",
                "IMAP_USER=user@example.test",
                "IMAP_PASSWORD=placeholder",
                "IMAP_ACCOUNT_NAME=example-account",
            ]
        ),
        encoding="utf-8",
    )

    def install(fake: FakeImapConnection) -> None:
        monkeypatch.setattr(
            stage_module,
            "NativeImapClient",
            lambda env: NativeImapClient(env, _connection=fake),
        )

    return env_file, install


def _run_imap(db: Path, stage: Path, env_file: Path, *extra: str) -> int:
    return stage_module.main(
        [
            "imap",
            "--database",
            str(db),
            "--stage",
            str(stage),
            "--env-file",
            str(env_file),
            *extra,
        ]
    )


def test_imap_table_row_is_refetched_and_staged_as_rewrite(tmp_path, fake_imap):
    env_file, install = fake_imap
    fake = FakeImapConnection(
        {"INBOX": {11: _eml(_table_message("<i1@example.test>"))}}
    )
    install(fake)
    db, (mid,) = _imap_canonical(
        tmp_path,
        [_maildir_row("INBOX/new/uid-11.eml", "<i1@example.test>", PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    staged = _staged(stage)
    assert set(staged) == {mid}
    assert staged[mid]["new_body_text"] == TABLE_BODY
    assert staged[mid]["status"] == "rewrite"
    assert staged[mid]["source_kind"] == "imap"
    assert staged[mid]["expected_message_id"] == "i1@example.test"
    assert (
        staged[mid]["pre_image_sha256"]
        == hashlib.sha256(PLAIN_FLAT.encode("utf-8")).hexdigest()
    )
    con = sqlite3.connect(stage)
    try:
        meta = dict(con.execute("SELECT key, value FROM stage_meta"))
    finally:
        con.close()
    assert meta["source_kind"] == "imap"
    report = _report(stage)
    assert report["counts"]["rewrite"] == 1
    assert report["per_folder"][0]["folder"] == "INBOX"


def test_imap_identical_body_is_unchanged_and_not_staged(tmp_path, fake_imap):
    env_file, install = fake_imap
    install(
        FakeImapConnection({"INBOX": {5: _eml(_plain_message("<i1@example.test>"))}})
    )
    db, _ = _imap_canonical(
        tmp_path,
        [_maildir_row("INBOX/new/uid-5.eml", "<i1@example.test>", "Hello there")],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["unchanged"] == 1
    assert counts["rewrite"] == 0


def test_imap_message_id_mismatch_from_uid_reuse_is_not_staged(tmp_path, fake_imap):
    env_file, install = fake_imap
    # The server's UID 5 now holds a different message (UIDVALIDITY reuse).
    install(
        FakeImapConnection({"INBOX": {5: _eml(_table_message("<other@example.test>"))}})
    )
    db, _ = _imap_canonical(
        tmp_path,
        [_maildir_row("INBOX/new/uid-5.eml", "<i1@example.test>", PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert _staged(stage) == {}
    counts = _report(stage)["counts"]
    assert counts["message_id_mismatch"] == 1
    assert counts["rewrite"] == 0


def test_imap_uid_gone_from_server_is_not_on_server(tmp_path, fake_imap):
    env_file, install = fake_imap
    install(FakeImapConnection({"INBOX": {}}))
    db, _ = _imap_canonical(
        tmp_path,
        [_maildir_row("INBOX/new/uid-5.eml", "<i1@example.test>", PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert _staged(stage) == {}
    report = _report(stage)
    assert report["counts"]["not_on_server"] == 1
    assert report["gone_from_server"] == 1


def test_imap_folder_gone_from_server_counts_all_its_rows(tmp_path, fake_imap):
    env_file, install = fake_imap
    fake = FakeImapConnection({"INBOX": {}})
    install(fake)
    db, _ = _imap_canonical(
        tmp_path,
        [
            _maildir_row("Old/new/uid-1.eml", "<a@example.test>", "x"),
            _maildir_row("Old/new/uid-2.eml", "<b@example.test>", "y"),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    report = _report(stage)
    assert report["counts"]["folder_missing"] == 2
    assert report["gone_from_server"] == 2
    assert "SELECT" not in fake.names()
    assert "UID FETCH" not in fake.names()


def test_imap_old_mbsync_locator_is_unparsable_and_never_fetched(tmp_path, fake_imap):
    env_file, install = fake_imap
    fake = FakeImapConnection({"INBOX": {1: _eml(_table_message("<i1@example.test>"))}})
    install(fake)
    db, _ = _imap_canonical(
        tmp_path,
        [
            _maildir_row(
                "INBOX/new/1700000000.M1P2.host,U=1:2,S", "<i1@example.test>", "x"
            )
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert _report(stage)["counts"]["locator_unparsable"] == 1
    assert _staged(stage) == {}
    assert "UID FETCH" not in fake.names()
    assert "SELECT" not in fake.names()


def test_imap_non_ascii_folder_resolved_through_list_wire_name(tmp_path, fake_imap):
    env_file, install = fake_imap
    display = "客戶"
    wire = encode_mutf7(display)
    assert wire != display
    fake = FakeImapConnection({wire: {3: _eml(_table_message("<c1@example.test>"))}})
    install(fake)
    db, (mid,) = _imap_canonical(
        tmp_path,
        [_maildir_row(f"{display}/new/uid-3.eml", "<c1@example.test>", PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert set(_staged(stage)) == {mid}
    selects = [c for c in fake.commands if c[0] == "SELECT"]
    assert selects == [("SELECT", f'"{wire}"', True)]
    assert _report(stage)["per_folder"][0]["wire_name"] == wire


def test_imap_only_read_only_commands_are_sent(tmp_path, fake_imap):
    env_file, install = fake_imap
    fake = FakeImapConnection(
        {
            "INBOX": {1: _eml(_table_message("<i1@example.test>"))},
            "Sent": {2: _eml(_plain_message("<i2@example.test>"))},
        }
    )
    install(fake)
    db, _ = _imap_canonical(
        tmp_path,
        [
            _maildir_row("INBOX/new/uid-1.eml", "<i1@example.test>", PLAIN_FLAT),
            _maildir_row("Sent/new/uid-2.v7.eml", "<i2@example.test>", "Hello there"),
            _maildir_row("Sent/new/uid-9.eml", "<i9@example.test>", "gone"),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert set(fake.names()) <= _READ_ONLY_COMMANDS
    selects = [c for c in fake.commands if c[0] == "SELECT"]
    assert len(selects) == 2
    assert all(c[2] is True for c in selects)
    fetches = [c for c in fake.commands if c[0] == "UID FETCH"]
    assert len(fetches) == 3
    assert all(c[2] == "(BODY.PEEK[])" for c in fetches)
    assert fake.names()[-2:] == ["CLOSE", "LOGOUT"]


def test_imap_canonical_bytes_unchanged_and_other_modes_out_of_scope(
    tmp_path, fake_imap
):
    env_file, install = fake_imap
    install(
        FakeImapConnection({"INBOX": {1: _eml(_table_message("<i1@example.test>"))}})
    )
    db, _ = _imap_canonical(
        tmp_path,
        [
            _maildir_row("INBOX/new/uid-1.eml", "<i1@example.test>", PLAIN_FLAT),
            _maildir_row(
                "INBOX/new/uid-1.eml", "<a@example.test>", "a", mode="archive"
            ),
            _maildir_row("Out/new/uid-2.eml", "<o@example.test>", "o", mode="outbound"),
            _maildir_row("Inbox/mbox#1", "<p@example.test>", "p", mode="file"),
        ],
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    report = _report(stage)
    assert report["rows_in_scope"] == 1
    assert report["counts"]["rewrite"] == 1


def test_imap_uidvalidity_change_is_counted_but_message_id_decides(tmp_path, fake_imap):
    env_file, install = fake_imap
    fake = FakeImapConnection(
        {"INBOX": {4: _eml(_table_message("<i1@example.test>"))}}, uidvalidity=99
    )
    install(fake)
    db, (mid,) = _imap_canonical(
        tmp_path,
        [_maildir_row("INBOX/new/uid-4.v7.eml", "<i1@example.test>", PLAIN_FLAT)],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    counts = _report(stage)["counts"]
    assert counts["uidvalidity_changed"] == 1
    assert counts["rewrite"] == 1
    assert set(_staged(stage)) == {mid}


def test_imap_row_without_message_id_is_never_fetched_or_staged(tmp_path, fake_imap):
    env_file, install = fake_imap
    fake = FakeImapConnection({"INBOX": {1: _eml(_table_message(None))}})
    install(fake)
    db, _ = _imap_canonical(
        tmp_path, [_maildir_row("INBOX/new/uid-1.eml", None, PLAIN_FLAT)]
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert _staged(stage) == {}
    assert _report(stage)["counts"]["no_message_id"] == 1
    assert "UID FETCH" not in fake.names()


def test_imap_lost_connection_commits_staged_rows_and_exits_nonzero(
    tmp_path, fake_imap
):
    env_file, install = fake_imap
    messages = {
        uid: _eml(_table_message(f"<i{uid}@example.test>")) for uid in (1, 2, 3)
    }
    fake = FakeImapConnection({"INBOX": messages}, drop_on_fetch_number=2)
    install(fake)
    db, ids = _imap_canonical(
        tmp_path,
        [
            _maildir_row(
                f"INBOX/new/uid-{uid}.eml", f"<i{uid}@example.test>", PLAIN_FLAT
            )
            for uid in (1, 2, 3)
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 2

    assert set(_staged(stage)) == {ids[0]}
    report = _report(stage)
    assert report["aborted"] is True
    assert report["counts"]["fetch_error"] == 2
    assert "LOGOUT" in fake.names()


def test_imap_existing_stage_refused_and_limit_applies(tmp_path, fake_imap):
    env_file, install = fake_imap
    install(
        FakeImapConnection(
            {
                "INBOX": {
                    1: _eml(_table_message("<i1@example.test>")),
                    2: _eml(_table_message("<i2@example.test>")),
                }
            }
        )
    )
    db, _ = _imap_canonical(
        tmp_path,
        [
            _maildir_row("INBOX/new/uid-1.eml", "<i1@example.test>", PLAIN_FLAT),
            _maildir_row("INBOX/new/uid-2.eml", "<i2@example.test>", PLAIN_FLAT),
        ],
    )
    stage = tmp_path / "stage.db"
    stage.write_bytes(b"precious")

    assert _run_imap(db, stage, env_file) == 1
    assert stage.read_bytes() == b"precious"

    assert _run_imap(db, stage, env_file, "--overwrite", "--limit", "1") == 0
    assert len(_staged(stage)) == 1
    assert _report(stage)["rows_in_scope"] == 1


def _stage_meta(stage: Path) -> dict[str, str]:
    con = sqlite3.connect(stage)
    try:
        return dict(con.execute("SELECT key, value FROM stage_meta"))
    finally:
        con.close()


def _one_row_world(tmp_path: Path, stub_readpst, out_name: str = "out"):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_table_message("<m1@example.test>")]
    db, _ids = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m1@example.test>", body=PLAIN_FLAT)],
    )
    return db, tmp_path / out_name / "stage.db", readpst


def test_stage_meta_carries_the_report_equal_to_the_report_file(tmp_path, stub_readpst):
    db, stage, readpst = _one_row_world(tmp_path, stub_readpst)
    assert _run(db, stage, readpst) == 0
    assert json.loads(_stage_meta(stage)["report_json"]) == _report(stage)


def test_unwritable_report_file_exits_nonzero_with_message(
    tmp_path, stub_readpst, capsys
):
    db, stage, readpst = _one_row_world(tmp_path, stub_readpst)
    # a directory squatting on the sidecar path: write_text raises OSError
    (stage.parent / "stage-report.json").mkdir(parents=True)
    assert _run(db, stage, readpst) == 1
    assert "report file" in capsys.readouterr().err
    # the counts still travel with the stage
    assert "report_json" in _stage_meta(stage)


def test_stage_and_report_paths_with_uri_special_characters(tmp_path, stub_readpst):
    db, stage, readpst = _one_row_world(tmp_path, stub_readpst, "a#b%20c#d")
    assert _run(db, stage, readpst) == 0
    assert _report(stage)["counts"]["rewrite"] == 1
