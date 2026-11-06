"""Tests for scripts/body_rederive_apply.py (issue #14 phase 2).

The apply tool rewrites ``messages.body_text`` and ``message_fts.body_text`` as
a pair for the planned rows of a stage database, and nothing else. These tests
pin the guards: dry-run writes nothing; --confirm is bound to a reviewed
dry-run of the same plan; drifted / missing / wrong-mode rows are excluded and
counted; the pre-image sidecar exists before the first canonical write; the
write is one transaction that rolls back on drift, exception or reconciliation
mismatch; restore round-trips and skips rows changed since the apply.

Canonical DBs use the real schema (``initialize_database``); stage DBs use the
stage builder's own ``_STAGE_SCHEMA``. Placeholders only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import body_rederive_apply as apply_module  # noqa: E402
from body_rederive_stage import _STAGE_SCHEMA  # noqa: E402

from mailattic.cjk_segmentation import segment_cjk  # noqa: E402
from mailattic.storage import connect_database, initialize_database  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _add_message(
    conn: sqlite3.Connection, key: str, mode: str, body: str, subject: str = "Subj"
) -> int:
    cur = conn.execute(
        """
        INSERT INTO messages (
            subject, sender_email, message_id, body_text, pst_source,
            folder_path, logical_locator, source_input_mode, dedup_key
        ) VALUES (?, 'a@example.test', ?, ?, 'p.pst', '/Inbox', ?, ?, ?)
        """,
        (subject, f"<{key}@example.test>", body, key, mode, key),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO message_fts (rowid, message_rowid, subject, sender, "
        "recipients, body_text, attachment_names) VALUES (?, ?, ?, ?, '', ?, '')",
        (mid, mid, subject, "a@example.test", segment_cjk(body)),
    )
    return mid


def _stage_row(
    mid: int,
    kind: str,
    pre_body: str,
    new_body: str,
    status: str = "rewrite",
    key: str = "ghost",
) -> tuple:
    # expected_message_id is the *normalized* form of <key@example.test>
    return (mid, kind, _sha(pre_body), new_body, status, f"{key}@example.test")


STAGE_REPORT = {
    "mode": "pst",
    "counts": {"pst_unreadable": 2, "excluded_marker": 5},
    # Default stages carry a complete verdict (apply refuses any other).
    "verdict": {"complete": True, "reasons": [], "missing_sources": []},
}


def _make_stage(
    path: Path,
    rows: list[tuple],
    with_meta: bool = True,
    with_report: bool = True,
    report: dict | None = None,
) -> Path:
    con = sqlite3.connect(path)
    try:
        con.executescript(_STAGE_SCHEMA)
        con.executemany("INSERT INTO body_rederive VALUES (?, ?, ?, ?, ?, ?)", rows)
        if with_meta:
            con.execute("INSERT INTO stage_meta VALUES ('source_kind', 'pst')")
            if with_report:
                con.execute(
                    "INSERT INTO stage_meta VALUES ('report_json', ?)",
                    (json.dumps(STAGE_REPORT if report is None else report),),
                )
        con.commit()
    finally:
        con.close()
    return path


def _snapshot(db: Path) -> dict:
    con = connect_database(db, read_only=True)
    try:
        return {
            "messages": [
                tuple(r) for r in con.execute("SELECT * FROM messages ORDER BY id")
            ],
            "fts": [
                tuple(r)
                for r in con.execute("SELECT rowid, * FROM message_fts ORDER BY rowid")
            ],
        }
    finally:
        con.close()


def _body(db: Path, mid: int) -> str:
    con = connect_database(db, read_only=True)
    try:
        return con.execute(
            "SELECT body_text FROM messages WHERE id=?", (mid,)
        ).fetchone()[0]
    finally:
        con.close()


def _fts_body(db: Path, mid: int) -> str:
    con = connect_database(db, read_only=True)
    try:
        return con.execute(
            "SELECT body_text FROM message_fts WHERE rowid=?", (mid,)
        ).fetchone()[0]
    finally:
        con.close()


def _fts_match(db: Path, term: str) -> list[int]:
    con = connect_database(db, read_only=True)
    try:
        return [
            r[0]
            for r in con.execute(
                "SELECT rowid FROM message_fts "
                "WHERE message_fts MATCH ? ORDER BY rowid",
                (term,),
            )
        ]
    finally:
        con.close()


def _set_body(db: Path, mid: int, body: str) -> None:
    con = connect_database(db)
    try:
        con.execute("UPDATE messages SET body_text=? WHERE id=?", (body, mid))
        con.commit()
    finally:
        con.close()


class World:
    """Canonical + stage with a known plan {A, B, C} and excluded rows."""

    OLD = {
        "A": "old alphaword text",
        "B": "old bravoword text",
        "C": "old charlieword text",
        "D": "untouched deltaword",
        "E": "old echoword text",
        "F": "old foxtrotword text",
    }
    NEW_A = "new betaword text\r\nline two"
    NEW_B = "新的內容 report2026年"
    NEW_C = "gammanewword"

    def __init__(self, tmp_path: Path):
        self.db = tmp_path / "canonical.db"
        initialize_database(self.db)
        con = connect_database(self.db)
        try:
            self.ids = {
                "A": _add_message(con, "A", "file", self.OLD["A"]),
                "B": _add_message(con, "B", "file", self.OLD["B"]),
                "C": _add_message(con, "C", "maildir", self.OLD["C"]),
                "D": _add_message(con, "D", "file", self.OLD["D"]),
                "E": _add_message(con, "E", "file", self.OLD["E"]),
                "F": _add_message(con, "F", "file", self.OLD["F"]),
            }
            con.commit()
        finally:
            con.close()
        i = self.ids
        rows = [
            _stage_row(i["A"], "pst", self.OLD["A"], self.NEW_A, key="A"),
            _stage_row(i["B"], "pst", self.OLD["B"], self.NEW_B, key="B"),
            _stage_row(i["C"], "imap", self.OLD["C"], self.NEW_C, key="C"),
            # drifted: stage was built from a different pre-image
            _stage_row(i["E"], "pst", "some other body", "echo new", key="E"),
            # mode mismatch: imap row pointing at a 'file' message
            _stage_row(i["F"], "imap", self.OLD["F"], "foxtrot new", key="F"),
            # canonical row missing
            _stage_row(9999, "pst", "x", "y"),
            # not a rewrite
            _stage_row(
                i["D"], "pst", self.OLD["D"], "delta new", status="skip", key="D"
            ),
        ]
        self.stage = _make_stage(tmp_path / "stage.db", rows)
        self.tmp = tmp_path

    def args(self, *extra: str) -> list[str]:
        return ["--stage", str(self.stage), "--canonical", str(self.db), *extra]

    @property
    def dryrun_report(self) -> Path:
        return self.tmp / "stage-apply-report-dryrun.json"

    @property
    def sidecar(self) -> Path:
        return self.tmp / "stage-apply-preimage.db"

    def dry_run(self, capsys) -> int:
        code = apply_module.main(self.args())
        capsys.readouterr()
        return code


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _expected_new(world: World) -> dict[str, str]:
    return {
        "A": "new betaword text\nline two",
        "B": world.NEW_B,
        "C": world.NEW_C,
    }


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing_and_reports_counts(world, capsys):
    before_bytes = world.db.read_bytes()
    code = apply_module.main(world.args())
    out = capsys.readouterr().out
    assert code == 0
    assert world.db.read_bytes() == before_bytes
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    assert report["read_only"] is True
    assert report["canonical"] == str(world.db.resolve())
    assert report["counts"] == {
        "stage_rows": 7,
        "not_rewrite": 1,
        "missing_row": 1,
        "missing_fts": 0,
        "mode_mismatch": 1,
        "message_id_changed": 0,
        "drifted": 1,
        "planned": 3,
    }
    assert report["plan_digest"] and report["stage_sha256"]
    blob = out + world.dryrun_report.read_text(encoding="utf-8")
    for text in ("betaword", "alphaword", "gammanewword", "report2026"):
        assert text not in blob
    assert not world.sidecar.exists()


def test_dry_run_refuses_when_canonical_is_stage(world, capsys):
    code = apply_module.main(
        ["--stage", str(world.stage), "--canonical", str(world.stage)]
    )
    assert code == 1
    assert not world.dryrun_report.exists()


def test_stage_without_meta_is_refused(tmp_path, capsys):
    db = tmp_path / "c.db"
    initialize_database(db)
    stage = _make_stage(tmp_path / "s.db", [], with_meta=False)
    code = apply_module.main(["--stage", str(stage), "--canonical", str(db)])
    assert code == 1
    assert "source_kind" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# binding to the reviewed dry-run
# ---------------------------------------------------------------------------
def test_confirm_without_dry_run_report_is_refused(world, capsys):
    before = _snapshot(world.db)
    code = apply_module.main(world.args("--confirm"))
    assert code == 1
    assert "no dry-run report" in capsys.readouterr().err
    assert _snapshot(world.db) == before
    assert not world.sidecar.exists()


def test_confirm_refused_when_stage_edited_after_dry_run(world, capsys):
    assert world.dry_run(capsys) == 0
    before = _snapshot(world.db)
    con = sqlite3.connect(world.stage)
    con.execute(
        "UPDATE body_rederive SET new_body_text = 'tampered' WHERE message_id = ?",
        (world.ids["A"],),
    )
    con.commit()
    con.close()
    assert apply_module.main(world.args("--confirm")) == 1
    assert "does not match" in capsys.readouterr().err
    assert _snapshot(world.db) == before
    assert not world.sidecar.exists()


def test_confirm_refused_when_canonical_edited_after_dry_run(world, capsys):
    assert world.dry_run(capsys) == 0
    _set_body(world.db, world.ids["B"], "edited behind the reviewer's back")
    before = _snapshot(world.db)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "does not match" in capsys.readouterr().err
    assert _snapshot(world.db) == before


def test_confirm_refused_when_report_says_not_read_only(world, capsys):
    assert world.dry_run(capsys) == 0
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    report["read_only"] = False
    world.dryrun_report.write_text(json.dumps(report), encoding="utf-8")
    before = _snapshot(world.db)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "read_only" in capsys.readouterr().err
    assert _snapshot(world.db) == before


def test_confirm_refused_for_report_of_other_canonical_path(world, capsys):
    assert world.dry_run(capsys) == 0
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    report["canonical"] = str(world.tmp / "copy.db")
    world.dryrun_report.write_text(json.dumps(report), encoding="utf-8")
    before = _snapshot(world.db)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "canonical" in capsys.readouterr().err
    assert _snapshot(world.db) == before


# ---------------------------------------------------------------------------
# successful apply
# ---------------------------------------------------------------------------
def test_confirm_updates_exactly_the_planned_rows_in_both_tables(world, capsys):
    before = _snapshot(world.db)
    assert world.dry_run(capsys) == 0
    assert apply_module.main(world.args("--confirm")) == 0
    capsys.readouterr()
    new = _expected_new(world)
    for key, body in new.items():
        mid = world.ids[key]
        assert _body(world.db, mid) == body
        assert _fts_body(world.db, mid) == segment_cjk(body)
    # an FTS MATCH finds a word only in the new body, not one only in the old
    assert _fts_match(world.db, "betaword") == [world.ids["A"]]
    assert _fts_match(world.db, "alphaword") == []
    assert _fts_match(world.db, "gammanewword") == [world.ids["C"]]
    assert _fts_match(world.db, "年") == [world.ids["B"]]
    # nothing else moved: compare full snapshots with planned rows masked
    after = _snapshot(world.db)
    planned = {world.ids[k] for k in new}
    assert [r for r in after["messages"] if r[0] not in planned] == [
        r for r in before["messages"] if r[0] not in planned
    ]
    assert [r for r in after["fts"] if r[0] not in planned] == [
        r for r in before["fts"] if r[0] not in planned
    ]
    # planned rows changed only in body_text (+ FTS body_text)
    for old_row, new_row in zip(before["messages"], after["messages"], strict=True):
        if old_row[0] in planned:
            diff = [
                i
                for i, (x, y) in enumerate(zip(old_row, new_row, strict=True))
                if x != y
            ]
            assert len(diff) == 1
    assert (world.tmp / "stage-apply-report-confirm.json").exists()


def test_excluded_rows_are_never_written(world, capsys):
    before = _snapshot(world.db)
    assert world.dry_run(capsys) == 0
    assert apply_module.main(world.args("--confirm")) == 0
    after = _snapshot(world.db)
    for key in ("D", "E", "F"):
        mid = world.ids[key]
        assert [r for r in after["messages"] if r[0] == mid] == [
            r for r in before["messages"] if r[0] == mid
        ]
        assert [r for r in after["fts"] if r[0] == mid] == [
            r for r in before["fts"] if r[0] == mid
        ]


def test_rows_without_fts_are_excluded_and_counted(world, capsys):
    con = connect_database(world.db)
    con.execute("DELETE FROM message_fts WHERE rowid = ?", (world.ids["B"],))
    con.commit()
    con.close()
    assert world.dry_run(capsys) == 0
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    assert report["counts"]["missing_fts"] == 1
    assert report["counts"]["planned"] == 2


# ---------------------------------------------------------------------------
# sidecar
# ---------------------------------------------------------------------------
def test_sidecar_holds_pre_images_before_first_canonical_write(
    world, capsys, monkeypatch
):
    assert world.dry_run(capsys) == 0
    seen = {}
    real = apply_module._write_row

    def spy(con, entry):
        if "rows" not in seen:
            side = sqlite3.connect(world.sidecar)
            seen["rows"] = side.execute(
                "SELECT id, old_body_text FROM pre_image ORDER BY id"
            ).fetchall()
            side.close()
        real(con, entry)

    monkeypatch.setattr(apply_module, "_write_row", spy)
    assert apply_module.main(world.args("--confirm")) == 0
    assert seen["rows"] == [
        (world.ids["A"], World.OLD["A"]),
        (world.ids["B"], World.OLD["B"]),
        (world.ids["C"], World.OLD["C"]),
    ]


def test_confirm_refuses_when_sidecar_path_exists(world, capsys):
    assert world.dry_run(capsys) == 0
    world.sidecar.write_bytes(b"earlier run")
    before = _snapshot(world.db)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "already exists" in capsys.readouterr().err
    assert _snapshot(world.db) == before
    assert world.sidecar.read_bytes() == b"earlier run"


# ---------------------------------------------------------------------------
# rollback paths
# ---------------------------------------------------------------------------
def test_failure_mid_transaction_rolls_back_both_tables(world, capsys, monkeypatch):
    assert world.dry_run(capsys) == 0
    before = _snapshot(world.db)
    real = apply_module._write_row
    calls = {"n": 0}

    def flaky(con, entry):
        if calls["n"] == 2:
            raise RuntimeError("injected failure")
        calls["n"] += 1
        real(con, entry)

    monkeypatch.setattr(apply_module, "_write_row", flaky)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "injected failure" in capsys.readouterr().err
    assert calls["n"] == 2  # two rows were written, then rolled back
    assert _snapshot(world.db) == before


def test_row_drift_inside_transaction_rolls_back_whole_run(world, capsys, monkeypatch):
    assert world.dry_run(capsys) == 0
    real = apply_module._write_pre_image_sidecar

    def drift_after_sidecar(*a, **kw):
        result = real(*a, **kw)
        _set_body(world.db, world.ids["C"], "changed by a concurrent sync")
        return result

    monkeypatch.setattr(apply_module, "_write_pre_image_sidecar", drift_after_sidecar)
    assert apply_module.main(world.args("--confirm")) == 1
    capsys.readouterr()
    assert _body(world.db, world.ids["A"]) == World.OLD["A"]
    assert _fts_body(world.db, world.ids["A"]) == segment_cjk(World.OLD["A"])
    assert _body(world.db, world.ids["B"]) == World.OLD["B"]
    assert _body(world.db, world.ids["C"]) == "changed by a concurrent sync"


def test_fts_drift_inside_transaction_rolls_back_whole_run(world, capsys, monkeypatch):
    assert world.dry_run(capsys) == 0
    real = apply_module._write_pre_image_sidecar

    def drift_fts_after_sidecar(*a, **kw):
        result = real(*a, **kw)
        con = connect_database(world.db)
        con.execute(
            "UPDATE message_fts SET body_text='drifted index' WHERE rowid=?",
            (world.ids["B"],),
        )
        con.commit()
        con.close()
        return result

    monkeypatch.setattr(
        apply_module, "_write_pre_image_sidecar", drift_fts_after_sidecar
    )
    assert apply_module.main(world.args("--confirm")) == 1
    assert "message_fts row" in capsys.readouterr().err
    assert _body(world.db, world.ids["A"]) == World.OLD["A"]
    assert _fts_body(world.db, world.ids["B"]) == "drifted index"


def _set_message_id(db: Path, mid: int, value: str) -> None:
    con = connect_database(db)
    try:
        con.execute("UPDATE messages SET message_id=? WHERE id=?", (value, mid))
        con.commit()
    finally:
        con.close()


def test_row_whose_message_id_changed_since_stage_is_excluded(world, capsys):
    # same id, same body, different message: a rebuilt canonical DB
    _set_message_id(world.db, world.ids["A"], "<someone-else@example.test>")
    before = _snapshot(world.db)
    assert apply_module.main(world.args()) == 0
    capsys.readouterr()
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    assert report["counts"]["message_id_changed"] == 1
    assert report["counts"]["planned"] == 2
    assert apply_module.main(world.args("--confirm")) == 0
    capsys.readouterr()
    after = _snapshot(world.db)
    mid = world.ids["A"]
    assert [r for r in after["messages"] if r[0] == mid] == [
        r for r in before["messages"] if r[0] == mid
    ]
    assert [r for r in after["fts"] if r[0] == mid] == [
        r for r in before["fts"] if r[0] == mid
    ]
    assert _body(world.db, world.ids["B"]) == World.NEW_B
    assert _body(world.db, world.ids["C"]) == World.NEW_C


def test_equivalent_message_id_spelling_still_matches(world, capsys):
    # outer angle brackets / whitespace are not a different identity
    _set_message_id(world.db, world.ids["A"], "  <A@example.test>  ")
    assert apply_module.main(world.args()) == 0
    capsys.readouterr()
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    assert report["counts"]["message_id_changed"] == 0
    assert report["counts"]["planned"] == 3


def test_confirm_refused_when_message_id_changed_after_dry_run(world, capsys):
    assert world.dry_run(capsys) == 0
    _set_message_id(world.db, world.ids["A"], "<someone-else@example.test>")
    before = _snapshot(world.db)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "does not match" in capsys.readouterr().err
    assert _snapshot(world.db) == before
    assert not world.sidecar.exists()


def test_plan_digest_binds_the_stage_expected_message_id(world, capsys):
    # Canonical id and the stage's expectation are changed together: bodies,
    # shas and planned ids are identical, only expected_message_id differs.
    assert world.dry_run(capsys) == 0
    _set_message_id(world.db, world.ids["A"], "<swapped@example.test>")
    con = sqlite3.connect(world.stage)
    con.execute(
        "UPDATE body_rederive SET expected_message_id = 'swapped@example.test' "
        "WHERE message_id = ?",
        (world.ids["A"],),
    )
    con.commit()
    con.close()
    before = _snapshot(world.db)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "plan_digest" in capsys.readouterr().err
    assert _snapshot(world.db) == before


def test_message_id_change_inside_transaction_rolls_back_whole_run(
    world, capsys, monkeypatch
):
    assert world.dry_run(capsys) == 0
    real = apply_module._write_pre_image_sidecar

    def reassign_after_sidecar(*a, **kw):
        result = real(*a, **kw)
        _set_message_id(world.db, world.ids["B"], "<reassigned@example.test>")
        return result

    monkeypatch.setattr(
        apply_module, "_write_pre_image_sidecar", reassign_after_sidecar
    )
    assert apply_module.main(world.args("--confirm")) == 1
    assert "Message-ID changed" in capsys.readouterr().err
    assert _body(world.db, world.ids["A"]) == World.OLD["A"]
    assert _body(world.db, world.ids["B"]) == World.OLD["B"]
    assert _body(world.db, world.ids["C"]) == World.OLD["C"]


def test_stage_without_expected_message_id_column_is_refused(tmp_path, capsys):
    db = tmp_path / "c.db"
    initialize_database(db)
    stage = tmp_path / "old-stage.db"
    con = sqlite3.connect(stage)
    con.executescript(
        "CREATE TABLE body_rederive (message_id INTEGER PRIMARY KEY, "
        "source_kind TEXT NOT NULL, pre_image_sha256 TEXT NOT NULL, "
        "new_body_text TEXT NOT NULL, status TEXT NOT NULL);"
        "CREATE TABLE stage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO stage_meta VALUES ('source_kind', 'pst');"
    )
    con.commit()
    con.close()
    for extra in ((), ("--confirm",)):
        code = apply_module.main(
            ["--stage", str(stage), "--canonical", str(db), *extra]
        )
        assert code == 1
        assert "rebuild the stage" in capsys.readouterr().err
    assert not (tmp_path / "old-stage-apply-report-dryrun.json").exists()


def test_unwritable_dry_run_report_exits_nonzero(world, capsys):
    # a directory squatting on the report path: write_text raises OSError
    world.dryrun_report.mkdir()
    assert apply_module.main(world.args()) == 1
    err = capsys.readouterr().err
    assert "could not be written" in err


def test_write_outside_the_plan_is_caught_and_rolled_back(world, capsys, monkeypatch):
    assert world.dry_run(capsys) == 0
    before = _snapshot(world.db)
    real = apply_module._write_row

    def sloppy(con, entry):
        real(con, entry)
        con.execute(
            "UPDATE messages SET body_text='collateral' WHERE id=?", (world.ids["D"],)
        )

    monkeypatch.setattr(apply_module, "_write_row", sloppy)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "outside the plan" in capsys.readouterr().err
    assert _snapshot(world.db) == before


def test_wrong_fts_value_is_caught_by_reconciliation(world, capsys, monkeypatch):
    assert world.dry_run(capsys) == 0
    before = _snapshot(world.db)

    def wrong_fts(con, entry):
        con.execute(
            "UPDATE messages SET body_text=? WHERE id=?", (entry.new_body, entry.id)
        )
        con.execute(
            "UPDATE message_fts SET body_text='wrong' WHERE rowid=?", (entry.id,)
        )

    monkeypatch.setattr(apply_module, "_write_row", wrong_fts)
    assert apply_module.main(world.args("--confirm")) == 1
    assert "mismatch" in capsys.readouterr().err
    assert _snapshot(world.db) == before


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
def _apply(world, capsys) -> None:
    assert world.dry_run(capsys) == 0
    assert apply_module.main(world.args("--confirm")) == 0
    capsys.readouterr()


def test_restore_round_trips_bodies_and_fts(world, capsys):
    before = _snapshot(world.db)
    _apply(world, capsys)
    assert _snapshot(world.db) != before
    args = ["--restore", str(world.sidecar), "--canonical", str(world.db)]
    after_apply = _snapshot(world.db)
    # dry-run restore writes nothing
    assert apply_module.main(args) == 0
    assert _snapshot(world.db) == after_apply
    capsys.readouterr()
    assert apply_module.main([*args, "--confirm"]) == 0
    assert _snapshot(world.db) == before
    assert _fts_match(world.db, "alphaword") == [world.ids["A"]]
    assert _fts_match(world.db, "betaword") == []


def test_restore_skips_rows_changed_since_apply(world, capsys):
    before = _snapshot(world.db)
    _apply(world, capsys)
    _set_body(world.db, world.ids["B"], "edited after the apply")
    args = ["--restore", str(world.sidecar), "--canonical", str(world.db), "--confirm"]
    assert apply_module.main(args) == 0
    out = capsys.readouterr().out
    assert "drifted_since_apply: 1" in out
    assert _body(world.db, world.ids["B"]) == "edited after the apply"
    for key in ("A", "C"):
        mid = world.ids[key]
        assert _body(world.db, mid) == World.OLD[key]
        assert _fts_body(world.db, mid) == segment_cjk(World.OLD[key])
    after = _snapshot(world.db)
    untouched = {world.ids[k] for k in ("D", "E", "F")}
    assert [r for r in after["messages"] if r[0] in untouched] == [
        r for r in before["messages"] if r[0] in untouched
    ]


def test_restore_refuses_sidecar_of_another_canonical(world, capsys, tmp_path):
    _apply(world, capsys)
    other = tmp_path / "other.db"
    initialize_database(other)
    code = apply_module.main(
        ["--restore", str(world.sidecar), "--canonical", str(other), "--confirm"]
    )
    assert code == 1
    assert "sidecar was written for canonical" in capsys.readouterr().err


def test_restore_failure_rolls_back(world, capsys, monkeypatch):
    _apply(world, capsys)
    after_apply = _snapshot(world.db)
    real = apply_module._write_row
    calls = {"n": 0}

    def flaky(con, entry):
        if calls["n"] == 1:
            raise RuntimeError("injected restore failure")
        calls["n"] += 1
        real(con, entry)

    monkeypatch.setattr(apply_module, "_write_row", flaky)
    code = apply_module.main(
        ["--restore", str(world.sidecar), "--canonical", str(world.db), "--confirm"]
    )
    assert code == 1
    assert _snapshot(world.db) == after_apply


def test_cli_requires_exactly_one_of_stage_or_restore(world):
    with pytest.raises(SystemExit):
        apply_module.main(["--canonical", str(world.db)])


# ---------------------------------------------------------------------------
# review round 2: URI-special paths, stage report travels with the stage
# ---------------------------------------------------------------------------
def test_uri_special_characters_in_paths_work_end_to_end(tmp_path, capsys):
    special = tmp_path / "a#b%20c#d"
    special.mkdir()
    world = World(special)
    before = _snapshot(world.db)
    _apply(world, capsys)
    assert _snapshot(world.db) != before
    args = ["--restore", str(world.sidecar), "--canonical", str(world.db)]
    assert apply_module.main(args) == 0
    capsys.readouterr()
    assert apply_module.main([*args, "--confirm"]) == 0
    assert _snapshot(world.db) == before


def test_stage_without_report_json_is_refused(tmp_path, capsys):
    db = tmp_path / "c.db"
    initialize_database(db)
    stage = _make_stage(tmp_path / "s.db", [], with_report=False)
    for extra in ((), ("--confirm",)):
        code = apply_module.main(
            ["--stage", str(stage), "--canonical", str(db), *extra]
        )
        assert code == 1
        assert "stage incomplete" in capsys.readouterr().err
    assert not (tmp_path / "s-apply-report-dryrun.json").exists()


def test_dry_run_report_and_output_carry_the_stage_counts(world, capsys):
    assert apply_module.main(world.args()) == 0
    out = capsys.readouterr().out
    report = json.loads(world.dryrun_report.read_text(encoding="utf-8"))
    assert report["stage_report"] == STAGE_REPORT
    assert "[stage] pst_unreadable: 2" in out
    assert "[stage] excluded_marker: 5" in out


# ---------------------------------------------------------------------------
# completeness verdict gate
# ---------------------------------------------------------------------------
def _without_verdict() -> dict:
    return {k: v for k, v in STAGE_REPORT.items() if k != "verdict"}


def test_stage_without_verdict_is_refused(tmp_path, capsys):
    db = tmp_path / "c.db"
    initialize_database(db)
    stage = _make_stage(tmp_path / "s.db", [], report=_without_verdict())
    for extra in ((), ("--confirm",)):
        code = apply_module.main(
            ["--stage", str(stage), "--canonical", str(db), *extra]
        )
        assert code == 1
        err = capsys.readouterr().err
        assert "no completeness verdict" in err
        assert "rebuild the stage" in err
    assert not (tmp_path / "s-apply-report-dryrun.json").exists()


def test_incomplete_verdict_is_refused_with_reasons(tmp_path, capsys):
    db = tmp_path / "c.db"
    initialize_database(db)
    report = {
        **STAGE_REPORT,
        "verdict": {
            "complete": False,
            "reasons": ["alpha.pst: extraction incomplete", "second reason"],
            "missing_sources": [],
        },
    }
    stage = _make_stage(tmp_path / "s.db", [], report=report)
    for extra in ((), ("--confirm",)):
        code = apply_module.main(
            ["--stage", str(stage), "--canonical", str(db), *extra]
        )
        assert code == 1
        err = capsys.readouterr().err
        assert "alpha.pst: extraction incomplete" in err
        assert "second reason" in err
        assert "rebuild the stage" in err
    assert not (tmp_path / "s-apply-report-dryrun.json").exists()


@pytest.mark.parametrize(
    "verdict", [{"complete": "true"}, {"complete": 1}, {}, "complete", None, []]
)
def test_non_bool_or_malformed_verdict_is_refused(tmp_path, capsys, verdict):
    db = tmp_path / "c.db"
    initialize_database(db)
    stage = _make_stage(
        tmp_path / "s.db", [], report={**STAGE_REPORT, "verdict": verdict}
    )
    for extra in ((), ("--confirm",)):
        code = apply_module.main(
            ["--stage", str(stage), "--canonical", str(db), *extra]
        )
        assert code == 1
        assert "rebuild the stage" in capsys.readouterr().err
    assert not (tmp_path / "s-apply-report-dryrun.json").exists()


def test_dry_run_prints_verdict_and_missing_sources(tmp_path, capsys):
    db = tmp_path / "c.db"
    initialize_database(db)
    report = {
        **STAGE_REPORT,
        "verdict": {
            "complete": True,
            "reasons": [],
            "missing_sources": ["gone-archive.pst"],
        },
    }
    stage = _make_stage(tmp_path / "s.db", [], report=report)
    assert apply_module.main(["--stage", str(stage), "--canonical", str(db)]) == 0
    out = capsys.readouterr().out
    assert "stage verdict: complete" in out
    assert "gone-archive.pst" in out


# ---------------------------------------------------------------------------
# imap_pst stage kind (#59)
# ---------------------------------------------------------------------------
def test_imap_pst_stage_row_applies_on_maildir_and_is_mode_mismatch_on_file(
    tmp_path, capsys
):
    db = tmp_path / "canonical.db"
    initialize_database(db)
    con = connect_database(db)
    try:
        maildir_id = _add_message(con, "M", "maildir", "old mailword text")
        file_id = _add_message(con, "P", "file", "old pstword text")
        con.commit()
    finally:
        con.close()
    rows = [
        _stage_row(
            maildir_id, "imap_pst", "old mailword text", "new mailbody", key="M"
        ),
        _stage_row(file_id, "imap_pst", "old pstword text", "new pstbody", key="P"),
    ]
    stage = _make_stage(tmp_path / "stage.db", rows)
    args = ["--stage", str(stage), "--canonical", str(db)]

    assert apply_module.main(args) == 0
    capsys.readouterr()
    report = json.loads(
        (tmp_path / "stage-apply-report-dryrun.json").read_text(encoding="utf-8")
    )
    assert report["counts"]["planned"] == 1
    assert report["counts"]["mode_mismatch"] == 1

    assert apply_module.main([*args, "--confirm"]) == 0
    capsys.readouterr()
    assert _body(db, maildir_id) == "new mailbody"
    assert _body(db, file_id) == "old pstword text"
