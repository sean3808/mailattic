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
from mailattic.models import ExtractionResult, IngestFailure  # noqa: E402
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
def lspst_overrides() -> dict[str, tuple[int, str, str]]:
    """PST file name -> (return code, stdout, stderr) the stub lspst answers
    with. A PST without an entry gets the default: one ``Email`` line per
    message its fake mbox holds (a complete, truthful ledger)."""
    return {}


@pytest.fixture
def stub_readpst(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lspst_overrides: dict[str, tuple[int, str, str]],
):
    """Returns (readpst_path, mboxes): mboxes maps a PST file name to the list
    of messages its (fake) unpacked ``Inbox/mbox`` holds. A stub ``lspst`` sits
    next to the stub readpst (default resolution finds it); ``_run_lspst`` is
    stubbed through ``lspst_overrides``."""
    readpst = tmp_path / "stub-readpst.exe"
    readpst.write_text("", encoding="utf-8")
    (tmp_path / "lspst.exe").write_text("", encoding="utf-8")
    mboxes: dict[str, list[EmailMessage]] = {}

    def fake_run_lspst(lspst: Path, pst: Path) -> tuple[int, str, str]:
        if pst.name in lspst_overrides:
            return lspst_overrides[pst.name]
        return _emails(len(mboxes.get(pst.name, [])))

    monkeypatch.setattr(stage_module, "_run_lspst", fake_run_lspst)

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


def _emails(count: int) -> tuple[int, str, str]:
    """What ``lspst`` prints for a PST listing ``count`` messages."""
    body = "".join(f"Email\tFrom: x{i}\n" for i in range(count))
    return 0, "Folder\tInbox\n" + body, ""


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
# Extraction completeness ledger (lspst). The stub lspst answers through the
# ``lspst_overrides`` fixture; everything after it is production code.
# ---------------------------------------------------------------------------


def _ledger_world(tmp_path: Path, stub_readpst, *, in_mbox: int, rows: int):
    """One PST holding ``in_mbox`` messages (<m0>..); canonical rows #1..#rows
    carry the ids <m0>.. and an old body."""
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_plain_message(f"<m{i}@example.test>") for i in range(in_mbox)]
    db, ids = _canonical(
        tmp_path,
        [
            _row(
                pst=pst,
                index=i,
                message_id=f"<m{i - 1}@example.test>",
                body="old body",
            )
            for i in range(1, rows + 1)
        ],
    )
    return pst, db, ids, readpst, tmp_path / "out" / "stage.db"


def _script_extractions(monkeypatch, mboxes, per_call):
    """Replace the stubbed ``_extract_pst``: call N (1-based, per PST file name)
    of a PST answers with ``per_call[N-1]`` -- a list of messages to unpack, or
    an Exception instance to raise. Returns the list of (pst name, output dir)
    seen, so a test can check the call count and the temp dirs."""
    calls: list[tuple[str, Path]] = []

    def scripted(self, source_path: Path, output_root: Path) -> None:
        calls.append((source_path.name, output_root))
        step = per_call[sum(1 for name, _ in calls if name == source_path.name) - 1]
        if isinstance(step, Exception):
            raise step
        folder = output_root / "Inbox"
        folder.mkdir(parents=True)
        box = mailbox.mbox(folder / "mbox", create=True)
        try:
            for message in step:
                box.add(message)
            box.flush()
        finally:
            box.close()

    monkeypatch.setattr(ReadpstParser, "_extract_pst", scripted)
    return calls


def _fail_recount(monkeypatch, mboxes):
    """Main pass unpacks ``mboxes['a.pst']``; the recount's readpst fails."""
    return _script_extractions(
        monkeypatch,
        mboxes,
        [mboxes["a.pst"], RuntimeError("readpst failed for a.pst: synthetic")],
    )


def test_ledger_ok_and_complete_extraction_exits_zero(tmp_path, stub_readpst):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=2, rows=2)

    assert _run(db, stage, readpst) == 0

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "ok"
    assert entry["ledger_emails"] == 2
    assert entry["ledger_note"] is None
    assert entry["extraction_complete"] is True
    assert _report(stage)["counts"]["extraction_incomplete"] == 0


def test_extraction_short_of_the_ledger_exits_two_and_does_not_pool(
    tmp_path, stub_readpst, lspst_overrides, capsys
):
    # lspst lists 4 messages, readpst yielded 2 (<m0> at #1, <m1> at #2). Row B
    # points at a locator (#3) with no message, but its Message-ID <m1> exists
    # elsewhere in the PST, so the fallback WOULD resolve it if it were pooled.
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_plain_message(f"<m{i}@example.test>") for i in range(2)]
    stage = tmp_path / "out" / "stage.db"
    db, (paired, extra) = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id="<m0@example.test>", body="old body"),
            _row(pst=pst, index=3, message_id="<m1@example.test>", body="old body"),
        ],
    )
    lspst_overrides["a.pst"] = _emails(4)

    assert _run(db, stage, readpst) == 2

    counts = _report(stage)["counts"]
    assert counts["extraction_incomplete"] == 1
    assert counts["locator_not_found"] == 0
    assert counts["paired_by_message_id"] == 0
    assert extra not in _staged(stage)
    assert set(_staged(stage)) == {paired}  # already-paired rows stay staged
    assert counts["rewrite"] == 1
    entry = _report(stage)["per_pst"][0]
    assert entry["extraction_complete"] is False
    assert entry["ledger_emails"] == 4
    out = capsys.readouterr().out
    assert "VERDICT: NOT complete" in out
    assert "extraction incomplete" in out
    assert str(pst.resolve()) in out


def test_ledger_abort_line_is_unavailable_exits_two_and_warns(
    tmp_path, stub_readpst, lspst_overrides, capsys, monkeypatch
):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=2, rows=2)
    _fail_recount(monkeypatch, stub_readpst[1])  # recount has its own tests
    lspst_overrides["a.pst"] = (
        0,
        "Email\tFrom: x\nA second message_store has been found. Sorry, this "
        "must be an error.\n",
        "",
    )

    # No ledger from lspst nor the recount: completeness unknown -> exit 2.
    assert _run(db, stage, readpst) == 2

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "unavailable"
    assert entry["ledger_emails"] is None
    assert "message_store" in entry["ledger_note"]
    assert entry["extraction_complete"] is None
    out = capsys.readouterr().out
    assert "VERDICT: NOT complete" in out
    assert "no completeness ledger" in out


def test_lspst_nonzero_exit_is_unavailable(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch
):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=1, rows=1)
    _fail_recount(monkeypatch, stub_readpst[1])  # recount has its own tests
    lspst_overrides["a.pst"] = (3, "", "boom")

    assert _run(db, stage, readpst) == 2

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "unavailable"
    assert "3" in entry["ledger_note"]
    assert entry["extraction_complete"] is None


def test_lspst_listing_no_email_items_is_unavailable(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch
):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=1, rows=1)
    _fail_recount(monkeypatch, stub_readpst[1])  # recount has its own tests
    lspst_overrides["a.pst"] = (0, 'Folder "x"\n', "")

    assert _run(db, stage, readpst) == 2

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "unavailable"
    assert entry["extraction_complete"] is None


def test_only_tab_separated_email_lines_count(tmp_path, stub_readpst, lspst_overrides):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=1, rows=1)
    lspst_overrides["a.pst"] = (0, "Email\tFrom: a\nEmails about x\nEmail2 y\n", "")

    assert _run(db, stage, readpst) == 0

    assert _report(stage)["per_pst"][0]["ledger_emails"] == 1


def test_missing_lspst_refuses_and_keeps_the_previous_stage(
    tmp_path, stub_readpst, monkeypatch
):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=1, rows=1)
    assert _run(db, stage, readpst) == 0
    before = stage.read_bytes()
    (tmp_path / "lspst.exe").unlink()
    monkeypatch.setattr(stage_module.shutil, "which", lambda name: None)

    assert _run(db, stage, readpst, "--overwrite") == 1
    assert stage.read_bytes() == before

    explicit = tmp_path / "nowhere" / "lspst.exe"
    assert _run(db, stage, readpst, "--overwrite", "--lspst-path", str(explicit)) == 1
    assert stage.read_bytes() == before


def test_explicit_lspst_path_is_used(tmp_path, stub_readpst, monkeypatch):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=1, rows=1)
    chosen = tmp_path / "elsewhere-lspst.exe"
    chosen.write_text("", encoding="utf-8")
    used: list[Path] = []
    original = stage_module._run_lspst

    def spy(lspst: Path, pst: Path):
        used.append(lspst)
        return original(lspst, pst)

    monkeypatch.setattr(stage_module, "_run_lspst", spy)

    assert _run(db, stage, readpst, "--lspst-path", str(chosen)) == 0
    assert used == [chosen.resolve()]


def test_failed_messages_count_toward_the_ledger(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch
):
    # lspst lists 3; readpst yields 2 messages plus 1 per-message parse failure:
    # nothing is missing, so the PST is complete and the failed row is a
    # parse_failure (not extraction_incomplete).
    _, db, ids, readpst, stage = _ledger_world(
        tmp_path, stub_readpst, in_mbox=2, rows=2
    )
    con = connect_database(db)
    try:
        failed = _insert(
            con,
            pst=tmp_path / "a.pst",
            index=9,
            message_id="<failed@example.test>",
            body="old body",
        )
        con.commit()
    finally:
        con.close()
    lspst_overrides["a.pst"] = _emails(3)
    original = ReadpstParser.iter_extraction_results

    def with_one_failure(self, source):
        yield from original(self, source)
        yield ExtractionResult(
            failure=IngestFailure.from_source(
                source,
                folder_path="Inbox",
                logical_locator="Inbox/mbox#9",
                error_stage="parse-pst",
                error_type="ValueError",
                error_message="synthetic",
            )
        )

    monkeypatch.setattr(ReadpstParser, "iter_extraction_results", with_one_failure)

    assert _run(db, stage, readpst) == 0

    entry = _report(stage)["per_pst"][0]
    assert entry["extraction_complete"] is True
    counts = _report(stage)["counts"]
    assert counts["parse_failure"] == 1
    assert counts["extraction_incomplete"] == 0
    assert failed not in _staged(stage)


def test_more_extracted_than_listed_is_complete(
    tmp_path, stub_readpst, lspst_overrides
):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=3, rows=3)
    lspst_overrides["a.pst"] = _emails(2)

    assert _run(db, stage, readpst) == 0

    entry = _report(stage)["per_pst"][0]
    assert entry["extraction_complete"] is True
    assert entry["extracted_messages"] == 3


def test_missing_pst_has_no_ledger_and_no_warning(tmp_path, stub_readpst, capsys):
    pst, db, _, readpst, stage = _ledger_world(
        tmp_path, stub_readpst, in_mbox=1, rows=1
    )
    pst.unlink()

    assert _run(db, stage, readpst) == 0

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "unavailable"
    assert entry["extraction_complete"] is None
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "VERDICT: complete" in out
    assert str(pst) in out  # listed as a missing source


# ---------------------------------------------------------------------------
# Recount ledger: a PST whose lspst ledger is unavailable is unpacked once more
# and counted. The scripted ``_extract_pst`` answers call 1 (main pass) and
# call 2 (recount) differently.
# ---------------------------------------------------------------------------

_LSPST_ABORT = (0, "A second message_store has been found. Sorry.\n", "")


def _msgs(count: int) -> list[EmailMessage]:
    return [_plain_message(f"<m{i}@example.test>") for i in range(count)]


def _recount_world(tmp_path, stub_readpst, lspst_overrides, monkeypatch, per_call):
    """PST a.pst with lspst unavailable; canonical rows: #1 (<m0>) pairs by
    locator, #3 (<m1>) has no message at #3 but <m1> exists in the PST, so the
    fallback WOULD resolve it were it pooled."""
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = []
    lspst_overrides["a.pst"] = _LSPST_ABORT
    db, ids = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id="<m0@example.test>", body="old body"),
            _row(pst=pst, index=3, message_id="<m1@example.test>", body="old body"),
        ],
    )
    calls = _script_extractions(monkeypatch, mboxes, per_call)
    return pst, db, ids, readpst, tmp_path / "out" / "stage.db", calls


def test_recount_higher_than_main_is_incomplete_exit_two_not_pooled(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch, capsys
):
    pst, db, (paired, extra), readpst, stage, calls = _recount_world(
        tmp_path, stub_readpst, lspst_overrides, monkeypatch, [_msgs(2), _msgs(4)]
    )

    assert _run(db, stage, readpst) == 2

    counts = _report(stage)["counts"]
    assert counts["extraction_incomplete"] == 1
    assert counts["locator_not_found"] == 0
    assert counts["paired_by_message_id"] == 0
    assert set(_staged(stage)) == {paired}
    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "recount"
    assert entry["ledger_emails"] == 4
    assert "second message_store" in entry["ledger_note"]
    assert entry["extraction_complete"] is False
    assert isinstance(entry["recount_seconds"], float)
    out = capsys.readouterr().out
    assert "VERDICT: NOT complete" in out
    assert "ledger lists 4" in out
    assert len(calls) == 2


def test_recount_equal_to_main_is_complete_exit_zero(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch, capsys
):
    pst, db, (paired, extra), readpst, stage, calls = _recount_world(
        tmp_path, stub_readpst, lspst_overrides, monkeypatch, [_msgs(2), _msgs(2)]
    )

    assert _run(db, stage, readpst) == 0

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "recount"
    assert entry["ledger_emails"] == 2
    assert entry["extraction_complete"] is True
    counts = _report(stage)["counts"]
    assert counts["extraction_incomplete"] == 0
    assert counts["paired_by_message_id"] == 1  # the fallback ran for the row
    assert set(_staged(stage)) == {paired, extra}
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "VERDICT: complete" in out


def test_recount_lower_than_main_leaves_no_ledger_and_exits_two(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch, capsys
):
    # PST really holds 5, main pass yields 4, recount yields 2: the passes
    # disagree, so the main pass cannot be proven complete.
    pst, db, _, readpst, stage, calls = _recount_world(
        tmp_path, stub_readpst, lspst_overrides, monkeypatch, [_msgs(4), _msgs(2)]
    )

    assert _run(db, stage, readpst) == 2

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "unavailable"
    assert entry["ledger_emails"] is None
    assert "recount 2 < main 4" in entry["ledger_note"]
    assert entry["extraction_complete"] is None
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert str(pst.resolve()) in out
    assert "found 2" in out
    assert "yielded 4" in out


def test_recount_that_fails_leaves_ledger_unavailable_and_warns(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch, capsys
):
    pst, db, _, readpst, stage, calls = _recount_world(
        tmp_path,
        stub_readpst,
        lspst_overrides,
        monkeypatch,
        [_msgs(2), RuntimeError("readpst failed for a.pst: synthetic")],
    )

    # Neither ledger available: completeness unknown is not success.
    assert _run(db, stage, readpst) == 2

    entry = _report(stage)["per_pst"][0]
    assert entry["ledger"] == "unavailable"
    assert entry["ledger_emails"] is None
    assert "second message_store" in entry["ledger_note"]
    assert "recount failed" in entry["ledger_note"]
    assert entry["extraction_complete"] is None
    out = capsys.readouterr().out
    assert "VERDICT: NOT complete" in out
    assert "no completeness ledger" in out


def test_ok_ledger_is_not_recounted(tmp_path, stub_readpst, monkeypatch):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = _msgs(2)  # default stub lspst lists 2 -> state ok
    db, _ = _canonical(
        tmp_path,
        [_row(pst=pst, index=1, message_id="<m0@example.test>", body="old body")],
    )
    calls = _script_extractions(monkeypatch, mboxes, [_msgs(2), _msgs(2)])

    assert _run(db, tmp_path / "out" / "stage.db", readpst) == 0

    assert len(calls) == 1
    entry = _report(tmp_path / "out" / "stage.db")["per_pst"][0]
    assert entry["ledger"] == "ok"
    assert entry["recount_seconds"] is None


@pytest.mark.parametrize("recount_step", ["ok", "raises", "unexpected"])
def test_recount_temp_dir_is_removed(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch, recount_step
):
    steps = {
        "ok": _msgs(2),
        "raises": RuntimeError("readpst failed for a.pst: synthetic"),
        "unexpected": OSError("disk gone"),
    }
    pst, db, _, readpst, stage, calls = _recount_world(
        tmp_path,
        stub_readpst,
        lspst_overrides,
        monkeypatch,
        [_msgs(2), steps[recount_step]],
    )

    # A failed recount leaves no ledger at all -> exit 2 (completeness unknown).
    assert _run(db, stage, readpst) == (0 if recount_step == "ok" else 2)

    assert len(calls) == 2
    recount_output = calls[1][1]
    assert recount_output.parent.name.startswith("recount-")
    assert not recount_output.parent.exists()
    assert not (tmp_path / "out" / "stage-work").exists()


def test_recount_temp_dir_is_removed_when_counting_blows_up(
    tmp_path, stub_readpst, lspst_overrides, monkeypatch
):
    pst, db, _, readpst, stage, calls = _recount_world(
        tmp_path, stub_readpst, lspst_overrides, monkeypatch, [_msgs(2), _msgs(2)]
    )

    def boom(output_root):
        raise _Boom("counting failed")

    monkeypatch.setattr(stage_module, "_count_extraction", boom)

    with pytest.raises(_Boom):
        _run(db, stage, readpst)

    assert not calls[1][1].parent.exists()


def test_count_extraction_counts_mbox_keys_and_eml_files(tmp_path):
    folder = tmp_path / "Inbox"
    folder.mkdir()
    box = mailbox.mbox(folder / "mbox", create=True)
    try:
        for message in _msgs(3):
            box.add(message)
        box.flush()
    finally:
        box.close()
    (folder / "1.eml").write_bytes(b"Subject: x\n\nbody\n")
    (folder / "2.eml").write_bytes(b"Subject: y\n\nbody\n")

    assert stage_module._count_extraction(tmp_path) == 5


@pytest.mark.skipif(sys.platform != "win32", reason="uses a .cmd stub")
def test_run_lspst_runs_the_binary_without_a_shell(tmp_path):
    cmd = tmp_path / "lspst.cmd"
    cmd.write_bytes(b"@echo off\r\necho Email\tFrom: a\r\nexit /b 0\r\n")
    pst = _make_pst(tmp_path, "a.pst")

    code, out, _ = stage_module._run_lspst(cmd, pst)

    assert code == 0
    assert out.startswith("Email")
    assert stage_module._lspst_ledger(cmd, pst) == {
        "state": "ok",
        "emails": 1,
        "note": None,
    }


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


# ---------------------------------------------------------------------------
# Degradation guard: keep the stored body when the re-derived one is worse
# ---------------------------------------------------------------------------

OLD_CJK = "測試範例備貨出口通知單請查收附件內容" * 2
# Big5 bytes decoded as Latin-1: the exact shape the parser defect produces.
MOJIBAKE_NEW = OLD_CJK.encode("big5").decode("latin-1")
B64_LINES = "\n".join(
    ["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2"] * 4
)
OLD_TEXT = "Delivery status: the message could not be delivered.\nPlease retry later."
M3 = "<m3@example.test>"


def test_degradation_reason_flags_big5_as_latin1_mojibake():
    assert stage_module._degradation_reason(OLD_CJK, MOJIBAKE_NEW) == "mojibake"


def test_degradation_reason_ignores_accented_text_without_cjk_in_old():
    old = "Bonjour, voici la facture."
    new = "Bonjour, voici la facture. Résumé: déjà vu, café, façade, élève, über, ñandú"
    assert sum(1 for c in new if 0xA1 <= ord(c) <= 0xFF) >= 10
    assert stage_module._degradation_reason(old, new) is None
    vietnamese = "Xin chào, đây là hóa đơn của bạn. Cảm ơn quý khách rất nhiều ạ."
    assert stage_module._degradation_reason(vietnamese, vietnamese + " ớ ờ ở") is None


def test_degradation_reason_ignores_cjk_reduction_without_latin1_high_chars():
    assert stage_module._degradation_reason(OLD_CJK, "Plain English body only") is None
    # Enough Latin-1 high chars but CJK kept: not mojibake.
    assert stage_module._degradation_reason(OLD_CJK, OLD_CJK + "é" * 30) is None
    # Old has < 10 CJK chars: the signature does not apply.
    assert stage_module._degradation_reason("測試範例", "é" * 30) is None


def test_degradation_reason_flags_base64_new_over_text_old():
    assert stage_module._degradation_reason(OLD_TEXT, B64_LINES) == "base64"


def test_degradation_reason_ignores_base64_in_both():
    assert stage_module._degradation_reason(B64_LINES, B64_LINES + "\nmore") is None
    # Half base64 is not "more than half".
    half = B64_LINES.splitlines()[0] + "\nshort text"
    assert stage_module._degradation_reason(OLD_TEXT, half) is None


def test_pst_degraded_rewrites_are_kept_and_good_rewrite_still_staged(
    tmp_path, stub_readpst
):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [
        _plain_message(M1, MOJIBAKE_NEW),  # locator-paired, mojibake
        _table_message(M2),  # locator-paired, good rewrite
        _plain_message(M3, B64_LINES),  # row says #9: fallback-paired, base64
    ]
    db, (mid1, mid2, mid3) = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=1, message_id=M1, body=OLD_CJK),
            _row(pst=pst, index=2, message_id=M2, body=PLAIN_FLAT),
            _row(pst=pst, index=9, message_id=M3, body=OLD_TEXT),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    assert set(_staged(stage)) == {mid2}
    report = _report(stage)
    assert report["counts"]["degraded_kept"] == 2
    assert report["counts"]["rewrite"] == 1
    assert report["counts"]["paired_by_message_id"] == 1
    entry_counts = report["per_pst"][0]["counts"]
    assert entry_counts["degraded_kept"] == 2
    assert report["degraded_sample_ids"] == [
        {"id": mid1, "reason": "mojibake"},
        {"id": mid3, "reason": "base64"},
    ]


def test_degraded_sample_ids_are_capped_at_twenty(tmp_path, stub_readpst):
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    count = 25
    mboxes["a.pst"] = [
        _plain_message(f"<d{i}@example.test>", MOJIBAKE_NEW) for i in range(count)
    ]
    db, _ids = _canonical(
        tmp_path,
        [
            _row(pst=pst, index=i + 1, message_id=f"<d{i}@example.test>", body=OLD_CJK)
            for i in range(count)
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run(db, stage, readpst) == 0

    report = _report(stage)
    assert report["counts"]["degraded_kept"] == count
    assert len(report["degraded_sample_ids"]) == 20


def test_imap_degraded_rewrites_are_kept_and_good_rewrite_still_staged(
    tmp_path, fake_imap
):
    env_file, install = fake_imap
    fake = FakeImapConnection(
        {
            "INBOX": {
                11: _eml(_plain_message("<i1@example.test>", MOJIBAKE_NEW)),
                12: _eml(_table_message("<i2@example.test>")),
                13: _eml(_plain_message("<i3@example.test>", B64_LINES)),
            }
        }
    )
    install(fake)
    db, (mid1, mid2, mid3) = _imap_canonical(
        tmp_path,
        [
            _maildir_row("INBOX/new/uid-11.eml", "<i1@example.test>", OLD_CJK),
            _maildir_row("INBOX/new/uid-12.eml", "<i2@example.test>", PLAIN_FLAT),
            _maildir_row("INBOX/new/uid-13.eml", "<i3@example.test>", OLD_TEXT),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    assert set(_staged(stage)) == {mid2}
    report = _report(stage)
    assert report["counts"]["degraded_kept"] == 2
    assert report["counts"]["rewrite"] == 1
    assert report["per_folder"][0]["counts"]["degraded_kept"] == 2
    assert report["degraded_sample_ids"] == [
        {"id": mid1, "reason": "mojibake"},
        {"id": mid3, "reason": "base64"},
    ]


# ---------------------------------------------------------------------------
# Completeness verdict: one whitelist-shaped verdict, persisted, drives exit code
# ---------------------------------------------------------------------------
def _pst_entry(**over) -> dict:
    entry = {
        "pst_source": "alpha.pst",
        "ledger": "ok",
        "ledger_emails": 2,
        "ledger_note": None,
        "extraction_complete": True,
        "extracted_messages": 2,
    }
    entry.update(over)
    return entry


def _pst_report(entries=None, **count_over) -> dict:
    counts = {"pst_unreadable": 0}
    counts.update(count_over)
    return {
        "mode": "pst",
        "counts": counts,
        "per_pst": [_pst_entry()] if entries is None else entries,
    }


def _imap_report(**over) -> dict:
    report = {
        "mode": "imap",
        "aborted": False,
        "counts": {"fetch_error": 0, "select_failed": 0},
    }
    report.update(over)
    return report


def test_verdict_complete_pst_and_recount_ledger():
    verdict = stage_module._verdict(
        _pst_report([_pst_entry(), _pst_entry(pst_source="b.pst", ledger="recount")])
    )
    assert verdict == {"complete": True, "reasons": [], "missing_sources": []}


@pytest.mark.parametrize(
    ("report", "needle"),
    [
        (_pst_report(pst_unreadable=1), "pst_unreadable"),
        (_pst_report(pst_unreadable=None), "pst_unreadable"),
        (
            _pst_report([_pst_entry(ledger="unavailable", ledger_note="lspst exited")]),
            "no completeness ledger",
        ),
        (
            _pst_report([_pst_entry(extraction_complete=False)]),
            "extraction incomplete",
        ),
        (
            _pst_report([_pst_entry(extraction_complete=None)]),
            "extraction incomplete",
        ),
        (_pst_report([_pst_entry(extraction_complete="yes")]), "extraction incomplete"),
        (_pst_report([_pst_entry(ledger=None)]), "no completeness ledger"),
        (_pst_report(["not a dict"]), "malformed per_pst entry"),
        ({"mode": "pst", "counts": {"pst_unreadable": 0}}, "per_pst"),
        ({"mode": "pst", "per_pst": []}, "counts"),
        (_imap_report(aborted=True), "aborted"),
        (_imap_report(aborted=None), "aborted"),
        (_imap_report(counts={"fetch_error": 1, "select_failed": 0}), "fetch_error"),
        (_imap_report(counts={"fetch_error": 0, "select_failed": 3}), "select_failed"),
        (_imap_report(counts={"select_failed": 0}), "fetch_error"),
        ({"mode": "ldap", "counts": {}}, "unknown report mode"),
        ({"counts": {}}, "unknown report mode"),
    ],
)
def test_verdict_each_failing_condition_alone_is_not_complete(report, needle):
    verdict = stage_module._verdict(report)
    assert verdict["complete"] is False
    assert any(needle in reason for reason in verdict["reasons"]), verdict


def test_verdict_missing_pst_is_allowed_and_listed_not_a_reason():
    missing = _pst_entry(
        pst_source="gone.pst",
        ledger="unavailable",
        ledger_note=stage_module._LEDGER_NOTE_PST_MISSING,
        extraction_complete=None,
    )
    verdict = stage_module._verdict(_pst_report([_pst_entry(), missing]))
    assert verdict == {
        "complete": True,
        "reasons": [],
        "missing_sources": ["gone.pst"],
    }


def test_verdict_missing_pst_does_not_excuse_another_incomplete_pst():
    missing = _pst_entry(
        pst_source="gone.pst",
        ledger="unavailable",
        ledger_note=stage_module._LEDGER_NOTE_PST_MISSING,
        extraction_complete=None,
    )
    verdict = stage_module._verdict(
        _pst_report([missing, _pst_entry(extraction_complete=False)])
    )
    assert verdict["complete"] is False
    assert verdict["missing_sources"] == ["gone.pst"]


def test_verdict_complete_imap():
    assert stage_module._verdict(_imap_report())["complete"] is True


def test_verdict_is_persisted_in_report_file_and_stage_meta(
    tmp_path, stub_readpst, capsys
):
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=2, rows=2)
    assert _run(db, stage, readpst) == 0
    expected = {"complete": True, "reasons": [], "missing_sources": []}
    assert _report(stage)["verdict"] == expected
    con = sqlite3.connect(stage)
    try:
        stored = con.execute(
            "SELECT value FROM stage_meta WHERE key = 'report_json'"
        ).fetchone()[0]
    finally:
        con.close()
    assert json.loads(stored)["verdict"] == expected
    assert "VERDICT: complete" in capsys.readouterr().out


def test_exit_code_comes_from_the_persisted_verdict(
    tmp_path, stub_readpst, monkeypatch, capsys
):
    # Everything else is clean: only the verdict says "not complete", and the
    # exit code must follow it (and the persisted report must carry it).
    _, db, _, readpst, stage = _ledger_world(tmp_path, stub_readpst, in_mbox=2, rows=2)
    monkeypatch.setattr(
        stage_module,
        "_verdict",
        lambda report: {
            "complete": False,
            "reasons": ["synthetic reason"],
            "missing_sources": [],
        },
    )
    assert _run(db, stage, readpst) == 2
    assert _report(stage)["verdict"]["reasons"] == ["synthetic reason"]
    out = capsys.readouterr().out
    assert "VERDICT: NOT complete" in out
    assert "synthetic reason" in out


# ---------------------------------------------------------------------------
# #59: IMAP rows the server no longer has -> unreachable ids list
# ---------------------------------------------------------------------------
def test_imap_stage_records_unreachable_ids_in_report_and_stage_meta(
    tmp_path, fake_imap
):
    env_file, install = fake_imap
    fake = FakeImapConnection(
        {
            "INBOX": {
                11: _eml(_plain_message("<i1@example.test>")),
                13: _eml(_plain_message("<i3@example.test>")),
            }
        }
    )
    install(fake)
    db, ids = _imap_canonical(
        tmp_path,
        [
            _maildir_row("INBOX/new/uid-11.eml", "<i1@example.test>", "Hello there"),
            _maildir_row("INBOX/new/uid-12.eml", "<i2@example.test>", "x"),  # gone
            _maildir_row("Gone/new/uid-5.eml", "<i4@example.test>", "x"),  # folder
            _maildir_row("INBOX/new/old-mbsync-name", "<i5@example.test>", "x"),
            # server holds a different message under this UID: not unreachable
            _maildir_row("INBOX/new/uid-13.eml", "<other@example.test>", "x"),
        ],
    )
    stage = tmp_path / "stage.db"

    assert _run_imap(db, stage, env_file) == 0

    expected = sorted(
        [
            {"id": ids[1], "message_id": "i2@example.test"},
            {"id": ids[2], "message_id": "i4@example.test"},
            {"id": ids[3], "message_id": "i5@example.test"},
        ],
        key=lambda e: e["id"],
    )
    report = _report(stage)
    assert report["unreachable"] == expected
    assert "unreachable_ids" not in report
    assert report["unreachable_ids_count"] == 3
    assert json.loads(_stage_meta(stage)["report_json"])["unreachable"] == expected
    assert not list(tmp_path.glob("*unreachable-ids*"))
    counts = report["counts"]
    unreachable = (
        counts["not_on_server"]
        + counts["folder_missing"]
        + counts["locator_unparsable"]
    )
    assert unreachable == 3


# ---------------------------------------------------------------------------
# #59: imap-from-pst
# ---------------------------------------------------------------------------
def _imap_stage_db(
    path: Path,
    ids: list | None,
    *,
    source_kind: str = "imap",
    complete: bool = True,
    canonical: str | None = "canonical.db",
    old_key: bool = False,
) -> Path:
    """A minimal finished ``imap`` stage database whose ``unreachable`` list
    holds *ids*: an int is paired with that row's Message-ID read from the
    ``canonical.db`` beside *path* (unknown id: ""), a ``(id, message_id)``
    tuple is stored as given. None: no list at all. *old_key*: the pre-binding
    shape (a plain ``unreachable_ids`` int list)."""
    path.unlink(missing_ok=True)
    report: dict = {"mode": "imap", "verdict": {"complete": complete, "reasons": []}}
    entries = []
    if ids is not None:
        snapshot = path.parent / "canonical.db"
        for item in ids:
            if isinstance(item, tuple):
                entries.append({"id": item[0], "message_id": item[1]})
                continue
            message_id = ""
            if snapshot.exists():
                con = sqlite3.connect(snapshot)
                try:
                    found = con.execute(
                        "SELECT message_id FROM messages WHERE id = ?", (item,)
                    ).fetchone()
                finally:
                    con.close()
                if found:
                    message_id = stage_module.normalize_message_id(found[0])
            entries.append({"id": item, "message_id": message_id})
        report["unreachable_ids" if old_key else "unreachable"] = (
            [e["id"] for e in entries] if old_key else entries
        )
    con = sqlite3.connect(path)
    try:
        con.executescript(stage_module._STAGE_SCHEMA)
        meta = [("source_kind", source_kind), ("report_json", json.dumps(report))]
        if canonical is not None:
            meta.append(("canonical", canonical))
        con.executemany("INSERT INTO stage_meta VALUES (?, ?)", meta)
        con.commit()
    finally:
        con.close()
    return path


def _from_pst_world(tmp_path: Path, stub_readpst, mailbox_messages, maildir_rows):
    """One PST (a.pst) holding *mailbox_messages*; canonical = one PST-mode row
    (so a.pst is a known pst_source) plus *maildir_rows* ((message_id, body) or
    (message_id, body, mode)). Returns (db, ids of the extra rows, readpst,
    imap_stage, stage)."""
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = mailbox_messages
    db = initialize_database(tmp_path / "canonical.db")
    con = connect_database(db)
    ids = []
    try:
        _insert(con, pst=pst, index=1, message_id="<anchor@example.test>", body="a")
        for offset, entry in enumerate(maildir_rows):
            message_id, body, *mode = entry
            ids.append(
                _insert(
                    con,
                    pst=pst,
                    index=100 + offset,
                    message_id=message_id,
                    body=body,
                    input_mode=mode[0] if mode else "maildir",
                )
            )
        con.commit()
    finally:
        con.close()
    return db, ids, readpst, tmp_path / "stage-imap.db", tmp_path / "out" / "stage.db"


def _run_from_pst(db, stage, readpst, imap_stage, *extra) -> int:
    return stage_module.main(
        [
            "imap-from-pst",
            "--database",
            str(db),
            "--stage",
            str(stage),
            "--imap-stage",
            str(imap_stage),
            "--readpst-path",
            str(readpst),
            *extra,
        ]
    )


def test_from_pst_rewrite_is_staged_with_imap_pst_kind(tmp_path, stub_readpst):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 0

    staged = _staged(stage)
    assert set(staged) == {mid}
    assert staged[mid]["new_body_text"] == TABLE_BODY
    assert staged[mid]["source_kind"] == "imap_pst"
    assert staged[mid]["expected_message_id"] == "g1@example.test"
    assert (
        staged[mid]["pre_image_sha256"]
        == hashlib.sha256(PLAIN_FLAT.encode("utf-8")).hexdigest()
    )
    assert _stage_meta(stage)["source_kind"] == "imap_pst"
    report = _report(stage)
    assert report["mode"] == "imap-from-pst"
    assert report["counts"]["rewrite"] == 1
    assert report["verdict"]["complete"] is True


def test_from_pst_equal_body_is_unchanged_and_not_staged(tmp_path, stub_readpst):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_plain_message("<g1@example.test>")],
        [("<g1@example.test>", "Hello there")],
    )
    _imap_stage_db(imap_stage, [mid])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 0

    assert _staged(stage) == {}
    assert _report(stage)["counts"]["unchanged"] == 1


def test_from_pst_candidates_that_disagree_are_ambiguous_and_not_staged(
    tmp_path, stub_readpst
):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [
            _plain_message("<g1@example.test>", "first version"),
            _plain_message("<g1@example.test>", "second version"),
        ],
        [("<g1@example.test>", "old body")],
    )
    _imap_stage_db(imap_stage, [mid])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 0

    assert _staged(stage) == {}
    assert _report(stage)["counts"]["message_id_ambiguous"] == 1


def test_from_pst_not_found_in_a_complete_pst_is_not_in_pst(tmp_path, stub_readpst):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_plain_message("<other@example.test>")],
        [("<g1@example.test>", "old body")],
    )
    _imap_stage_db(imap_stage, [mid])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 0

    counts = _report(stage)["counts"]
    assert counts["not_in_pst"] == 1
    assert counts["extraction_incomplete"] == 0
    assert _staged(stage) == {}


def test_from_pst_not_found_with_incomplete_extraction_exits_two(
    tmp_path, stub_readpst, lspst_overrides
):
    db, (found, missing), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT), ("<g2@example.test>", "old body")],
    )
    _imap_stage_db(imap_stage, [found, missing])
    lspst_overrides["a.pst"] = _emails(5)  # lspst lists 5, readpst yielded 1

    assert _run_from_pst(db, stage, readpst, imap_stage) == 2

    report = _report(stage)
    assert report["counts"]["extraction_incomplete"] == 1
    assert report["counts"]["not_in_pst"] == 0
    assert report["verdict"]["complete"] is False
    assert set(_staged(stage)) == {found}  # the Message-ID-verified row stays


def test_from_pst_counts_non_imap_and_unknown_ids_and_never_stages_them(
    tmp_path, stub_readpst
):
    db, (file_row, no_mid), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [
            ("<g1@example.test>", PLAIN_FLAT, "file"),
            (None, "old body"),
        ],
    )
    _imap_stage_db(imap_stage, [file_row, no_mid, 987654])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 0

    counts = _report(stage)["counts"]
    assert counts["not_imap_row"] == 2  # the file-mode row and the unknown id
    assert counts["no_message_id"] == 1
    assert _staged(stage) == {}


def test_from_pst_degraded_body_is_kept(tmp_path, stub_readpst):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_plain_message("<g1@example.test>", B64_LINES)],
        [("<g1@example.test>", OLD_TEXT)],
    )
    _imap_stage_db(imap_stage, [mid])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 0

    assert _staged(stage) == {}
    report = _report(stage)
    assert report["counts"]["degraded_kept"] == 1
    assert report["degraded_sample_ids"] == [{"id": mid, "reason": "base64"}]


def test_from_pst_bad_map_key_is_refused_before_the_old_stage_is_deleted(
    tmp_path, stub_readpst
):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])
    assert _run_from_pst(db, stage, readpst, imap_stage) == 0
    before = stage.read_bytes()

    code = _run_from_pst(
        db,
        stage,
        readpst,
        imap_stage,
        "--overwrite",
        "--pst-map",
        f"{tmp_path / 'typo.pst'}={tmp_path / 'a.pst'}",
    )

    assert code == 1
    assert stage.read_bytes() == before


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda p, mid: p.write_bytes(b"not sqlite"), id="not-a-db"),
        pytest.param(lambda p, mid: p.unlink(), id="missing"),
        pytest.param(
            lambda p, mid: _imap_stage_db(p, [mid], source_kind="pst"), id="wrong-kind"
        ),
        pytest.param(
            lambda p, mid: _imap_stage_db(p, [mid], complete=False),
            id="incomplete-verdict",
        ),
        pytest.param(lambda p, mid: _imap_stage_db(p, None), id="no-list"),
        pytest.param(
            lambda p, mid: _imap_stage_db(p, [mid], old_key=True), id="old-id-list"
        ),
        pytest.param(
            lambda p, mid: _imap_stage_db(p, [mid], canonical=None), id="no-canonical"
        ),
    ],
)
def test_from_pst_unusable_imap_stage_is_refused_before_deletion(
    tmp_path, stub_readpst, build
):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])
    assert _run_from_pst(db, stage, readpst, imap_stage) == 0
    before = stage.read_bytes()
    build(imap_stage, mid)

    assert _run_from_pst(db, stage, readpst, imap_stage, "--overwrite") == 1
    assert stage.read_bytes() == before


def test_from_pst_identity_mismatch_is_never_staged_and_verdict_not_complete(
    tmp_path, stub_readpst
):
    # The stored id points at a canonical row with a different Message-ID: the
    # imap stage was built from another snapshot. The PST holds the canonical
    # row's message, so a wrongly accepted id would be staged.
    db, (good, other), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>"), _table_message("<g2@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT), ("<g2@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [good, (other, "someone-else@example.test")])

    assert _run_from_pst(db, stage, readpst, imap_stage) == 2

    report = _report(stage)
    assert report["counts"]["identity_mismatch"] == 1
    assert report["counts"]["rewrite"] == 1
    assert set(_staged(stage)) == {good}
    assert report["verdict"]["complete"] is False
    assert any("identity_mismatch" in r for r in report["verdict"]["reasons"])


def test_from_pst_imap_stage_equal_to_stage_is_refused(tmp_path, stub_readpst):
    db, (mid,), readpst, imap_stage, _ = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])
    before = imap_stage.read_bytes()

    assert _run_from_pst(db, imap_stage, readpst, imap_stage, "--overwrite") == 1
    assert imap_stage.read_bytes() == before


def test_from_pst_unmatched_pst_filter_is_refused_before_deletion(
    tmp_path, stub_readpst
):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])
    assert _run_from_pst(db, stage, readpst, imap_stage) == 0
    before = stage.read_bytes()

    code = _run_from_pst(
        db,
        stage,
        readpst,
        imap_stage,
        "--overwrite",
        "--pst",
        str(tmp_path / "a.pst"),
        "--pst",
        str(tmp_path / "typo.pst"),
    )

    assert code == 1
    assert stage.read_bytes() == before


def test_from_pst_empty_pst_selection_is_refused_before_deletion(
    tmp_path, stub_readpst
):
    readpst, _ = stub_readpst
    db = initialize_database(tmp_path / "canonical.db")  # no file-mode rows at all
    con = connect_database(db)
    try:
        con.execute(
            "INSERT INTO messages (subject, body_text, message_id, pst_source, "
            "folder_path, logical_locator, source_input_mode, dedup_key) VALUES "
            "('s', 'b', '<g1@example.test>', 'm', 'f', 'INBOX/new/uid-1.eml', "
            "'maildir', 'k')"
        )
        mid = con.execute("SELECT id FROM messages").fetchone()[0]
        con.commit()
    finally:
        con.close()
    imap_stage = _imap_stage_db(tmp_path / "stage-imap.db", [mid])
    stage = tmp_path / "stage.db"
    stage.write_bytes(b"precious")

    assert _run_from_pst(db, stage, readpst, imap_stage, "--overwrite") == 1
    assert stage.read_bytes() == b"precious"


def test_from_pst_per_message_extraction_failure_makes_the_verdict_not_complete(
    tmp_path, stub_readpst, monkeypatch
):
    # The failed message may share the Message-ID: agreement is unproven.
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])
    original = ReadpstParser.iter_extraction_results

    def with_failure(self, source):
        yield from original(self, source)
        yield ExtractionResult(
            failure=IngestFailure.from_source(
                source,
                folder_path="Inbox",
                logical_locator="Inbox/mbox#9",
                error_stage="parse-pst",
                error_type="ValueError",
                error_message="synthetic",
            )
        )

    monkeypatch.setattr(ReadpstParser, "iter_extraction_results", with_failure)

    assert _run_from_pst(db, stage, readpst, imap_stage) == 2

    report = _report(stage)
    assert report["per_pst"][0]["extraction_failures"] == 1
    assert report["verdict"]["complete"] is False
    assert any(
        "a.pst" in reason and "failed" in reason
        for reason in report["verdict"]["reasons"]
    )


def test_from_pst_existing_stage_is_refused_without_overwrite(tmp_path, stub_readpst):
    db, (mid,), readpst, imap_stage, stage = _from_pst_world(
        tmp_path,
        stub_readpst,
        [_table_message("<g1@example.test>")],
        [("<g1@example.test>", PLAIN_FLAT)],
    )
    _imap_stage_db(imap_stage, [mid])
    assert _run_from_pst(db, stage, readpst, imap_stage) == 0
    assert _run_from_pst(db, stage, readpst, imap_stage) == 1


def _two_pst_world(tmp_path: Path, stub_readpst):
    """a.pst exists; gone.pst is a canonical pst_source with no file on disk.
    The listed id (g1) is in neither."""
    readpst, mboxes = stub_readpst
    pst = _make_pst(tmp_path, "a.pst")
    mboxes["a.pst"] = [_plain_message("<other@example.test>")]
    gone = tmp_path / "gone.pst"  # never created
    db = initialize_database(tmp_path / "canonical.db")
    con = connect_database(db)
    try:
        _insert(con, pst=pst, index=1, message_id="<anchor@example.test>", body="a")
        _insert(con, pst=gone, index=1, message_id="<anchor2@example.test>", body="a")
        mid = _insert(
            con,
            pst=pst,
            index=100,
            message_id="<g1@example.test>",
            body="old body",
            input_mode="maildir",
        )
        con.commit()
    finally:
        con.close()
    imap_stage = tmp_path / "stage-imap.db"
    _imap_stage_db(imap_stage, [mid])
    return db, readpst, imap_stage, tmp_path / "out" / "stage.db", pst


def test_from_pst_pst_filter_excluding_a_missing_pst_keeps_not_in_pst(
    tmp_path, stub_readpst
):
    db, readpst, imap_stage, stage, pst = _two_pst_world(tmp_path, stub_readpst)

    assert _run_from_pst(db, stage, readpst, imap_stage, "--pst", str(pst)) == 0

    report = _report(stage)
    assert report["verdict"]["complete"] is True
    assert report["counts"]["not_in_pst"] == 1
    assert report["counts"]["extraction_incomplete"] == 0


def test_from_pst_without_pst_filter_a_missing_pst_makes_not_found_incomplete(
    tmp_path, stub_readpst
):
    db, readpst, imap_stage, stage, _ = _two_pst_world(tmp_path, stub_readpst)

    _run_from_pst(db, stage, readpst, imap_stage)

    report = _report(stage)
    assert report["counts"]["extraction_incomplete"] == 1
    assert report["counts"]["not_in_pst"] == 0
