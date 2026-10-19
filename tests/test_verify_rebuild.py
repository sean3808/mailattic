"""Tests for scripts/verify-rebuild.py — pre-swap integrity verification.

Covers five individual checks (PASS + FAIL paths) and two end-to-end
subprocess tests (exit 0 / exit 1).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the script as a module (lives in scripts/, not src/)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "verify-rebuild.py"


def _load_verify_module():  # type: ignore[return]
    spec = importlib.util.spec_from_file_location("verify_rebuild", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_vr = _load_verify_module()

check_integrity = _vr.check_integrity
check_per_source_counts = _vr.check_per_source_counts
check_null_hash_by_mode = _vr.check_null_hash_by_mode
check_cas_spot_check = _vr.check_cas_spot_check
check_fts_rowid_alignment = _vr.check_fts_rowid_alignment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _init_db(tmp_path: Path) -> Callable[[str], sqlite3.Connection]:
    """Return a factory: call it with a stem to get an open Connection to a
    freshly initialised DB (schema via initialize_database)."""
    from mailattic.storage import initialize_database

    def factory(stem: str = "test") -> sqlite3.Connection:
        db_path = initialize_database(tmp_path / f"{stem}.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


def _insert_message(
    conn: sqlite3.Connection,
    *,
    pst_source: str = "/test/path/archive.pst",
    source_input_mode: str = "file",
    msg_id_suffix: str = "1",
) -> int:
    """Insert a minimal message row; returns the inserted rowid."""
    cur = conn.execute(
        """
        INSERT INTO messages (
            subject, sender_email, sent_at, body_text,
            message_id, pst_source, folder_path, logical_locator,
            source_input_mode, source_requested_from, dedup_key, dedup_basis
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            f"Subject {msg_id_suffix}",
            "sender@example.com",
            "2024-01-01T00:00:00",
            "body",
            f"<msg{msg_id_suffix}@example.com>",
            pst_source,
            "Inbox",
            f"Inbox/{msg_id_suffix}",
            source_input_mode,
            pst_source,
            f"dedup_{msg_id_suffix}",
            "message_id",
        ],
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_fts(conn: sqlite3.Connection, message_id: int) -> None:
    """Insert a matching FTS row for *message_id*."""
    conn.execute(
        """
        INSERT INTO message_fts (rowid, message_rowid, subject, sender,
                                  recipients, body_text, attachment_names)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [message_id, message_id, "Subject", "sender@example.com", "", "body", ""],
    )


def _insert_attachment(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    content_hash: str | None = "abc" * 21 + "ab",  # 64-char placeholder
    storage_path: str | None = None,
    size_bytes: int | None = 100,
) -> None:
    """Insert an attachment row linked to *message_id*."""
    conn.execute(
        """
        INSERT INTO message_attachments
            (message_id, attachment_name, content_hash, size_bytes, storage_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        [message_id, "file.bin", content_hash, size_bytes, storage_path],
    )


@pytest.fixture()
def _make_cas_file(tmp_path: Path) -> Callable[[Path, bytes], str]:
    """Return a helper: write *content* into the CAS tree under *root*, return
    the storage_path string (``attachments/<l1>/<l2>/<hash>.bin``)."""

    def make(root: Path, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        l1, l2 = digest[0:2], digest[2:4]
        dest = root / l1 / l2 / f"{digest}.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return f"attachments/{l1}/{l2}/{digest}.bin"

    return make


# ---------------------------------------------------------------------------
# Check A — integrity_check
# ---------------------------------------------------------------------------


def test_integrity_check_pass_on_clean_db(_init_db: Callable) -> None:
    conn = _init_db("integrity_pass")
    try:
        result = check_integrity(conn)
    finally:
        conn.close()

    assert result["passed"] is True
    assert result["result"] == "ok"


# ---------------------------------------------------------------------------
# Check B — per_source_counts
# ---------------------------------------------------------------------------


REQUIRED = ["arc_a.pst", "arc_b.pst", "main.pst"]


def _baseline(tmp_path: Path, sources: list[dict] | str | None = None) -> Path:
    """Baseline file: 3 required PSTs (min 2 each) + optional archive.pst."""
    if sources is None:
        sources = [{"name": n, "min_count": 2} for n in REQUIRED] + [
            {"name": "archive.pst", "min_count": 5, "optional": True}
        ]
    path = tmp_path / "baseline.json"
    if isinstance(sources, str):
        path.write_text(sources, encoding="utf-8")
    else:
        path.write_text(
            json.dumps({"version": 1, "sources": sources}), encoding="utf-8"
        )
    return path


def _populate(conn: sqlite3.Connection, counts: dict[str, int]) -> None:
    n = 0
    for src, cnt in counts.items():
        for _ in range(cnt):
            n += 1
            _insert_fts(
                conn, _insert_message(conn, pst_source=src, msg_id_suffix=f"p{n}")
            )
    conn.commit()


def _per_source(
    _init_db: Callable, tmp_path: Path, counts: dict[str, int], baseline: Path | None
) -> dict:
    conn = _init_db(f"source{len(list(tmp_path.glob('*.db')))}")
    _populate(conn, counts)
    try:
        return check_per_source_counts(conn, baseline)
    finally:
        conn.close()


def _status(result: dict) -> dict[str, str]:
    return {s["name"]: s["status"] for s in result["sources"]}


def test_per_source_three_required_at_minimum_pass_optional_absent(
    _init_db: Callable, tmp_path: Path
) -> None:
    # Replaces the old "four PSTs pass / three PSTs fail" rule (decision comment on
    # #228: baseline-driven, three-source corpus is legitimate, archive.pst optional).
    result = _per_source(
        _init_db, tmp_path, dict.fromkeys(REQUIRED, 2), _baseline(tmp_path)
    )
    assert result["passed"] is True
    assert result["refusal_reason"] is None
    assert _status(result)["archive.pst"] == "absent_approved"
    assert all(s["actual"] == 2 for s in result["sources"] if s["name"] in REQUIRED)


def test_per_source_missing_required_source_fails_despite_others(
    _init_db: Callable, tmp_path: Path
) -> None:
    result = _per_source(
        _init_db,
        tmp_path,
        {"arc_a.pst": 50, "arc_b.pst": 50, "other.pst": 50},
        _baseline(tmp_path),
    )
    assert result["passed"] is False
    assert _status(result)["main.pst"] == "missing"
    assert result["unlisted_pst_sources"] == {"other.pst": 50}


def test_per_source_one_row_short_fails(_init_db: Callable, tmp_path: Path) -> None:
    result = _per_source(
        _init_db,
        tmp_path,
        {"arc_a.pst": 2, "arc_b.pst": 2, "main.pst": 1},
        _baseline(tmp_path),
    )
    assert result["passed"] is False
    short = next(s for s in result["sources"] if s["name"] == "main.pst")
    assert (short["status"], short["actual"], short["threshold"]) == ("short", 1, 2)


def test_per_source_unrelated_sources_cannot_fill_gap(
    _init_db: Callable, tmp_path: Path
) -> None:
    result = _per_source(
        _init_db,
        tmp_path,
        {
            "arc_a.pst": 2,
            "arc_b.pst": 2,
            "fourth.pst": 99,
            "outbound-bes": 99,
            "imap:me@x.test:INBOX": 99,
            "archive:msg.eml": 99,
        },
        _baseline(tmp_path),
    )
    assert result["passed"] is False
    assert _status(result)["main.pst"] == "missing"
    assert result["excluded_non_pst"] == {"outbound-bes": 99, "imap": 99, "archive": 99}


def test_per_source_optional_present_still_needs_its_minimum(
    _init_db: Callable, tmp_path: Path
) -> None:
    counts = dict.fromkeys(REQUIRED, 2) | {"D:/old/archive.pst": 1}
    result = _per_source(_init_db, tmp_path, counts, _baseline(tmp_path))
    assert result["passed"] is False
    assert _status(result)["archive.pst"] == "short"


def test_per_source_approved_gap_lowers_threshold_and_is_reported(
    _init_db: Callable, tmp_path: Path
) -> None:
    sources = [
        {
            "name": "arc_a.pst",
            "min_count": 10,
            "approved_gap": 3,
            "gap_reason": "dedup",
        },
        {"name": "main.pst", "min_count": 2},
    ]
    result = _per_source(
        _init_db,
        tmp_path,
        {"arc_a.pst": 7, "main.pst": 2},
        _baseline(tmp_path, sources),
    )
    assert result["passed"] is True
    entry = result["sources"][0]
    assert (entry["expected_min_count"], entry["approved_gap"], entry["threshold"]) == (
        10,
        3,
        7,
    )
    assert entry["gap_reason"] == "dedup"


def test_per_source_moved_path_and_platform_match_by_file_name(
    _init_db: Callable, tmp_path: Path
) -> None:
    counts = {
        "/root/pst/arc_a.pst": 2,
        "C:\\Users\\x\\Outlook\\ARC_B.pst": 2,
        "E:/new/main.pst": 2,
    }
    result = _per_source(_init_db, tmp_path, counts, _baseline(tmp_path))
    assert result["passed"] is True


def test_per_source_same_name_different_sources_is_refused_until_mapped(
    _init_db: Callable, tmp_path: Path
) -> None:
    counts = {"D:/a/main.pst": 2, "D:/b/main.pst": 2, "arc_a.pst": 2, "arc_b.pst": 2}
    result = _per_source(_init_db, tmp_path, counts, _baseline(tmp_path))
    assert result["passed"] is False
    assert _status(result)["main.pst"] == "ambiguous"
    assert "pst_source_paths" in result["refusal_reason"]

    sources = [{"name": n, "min_count": 2} for n in ("arc_a.pst", "arc_b.pst")] + [
        {"name": "main.pst", "min_count": 2, "pst_source_paths": ["D:/b/main.pst"]}
    ]
    mapped = _per_source(_init_db, tmp_path, counts, _baseline(tmp_path, sources))
    assert mapped["passed"] is True
    assert mapped["sources"][2]["matched_pst_sources"] == ["D:/b/main.pst"]
    assert mapped["unlisted_pst_sources"] == {"D:/a/main.pst": 2}


def _one(**entry: object) -> str:
    return json.dumps({"version": 1, "sources": [{"name": "a.pst", **entry}]})


@pytest.mark.parametrize(
    "baseline_text",
    [
        "",
        "not json",
        "{}",
        '{"version": 1, "sources": []}',
        json.dumps({"version": 2, "sources": [{"name": "a.pst", "min_count": 1}]}),
        # True == 1 and 1.0 == 1 in Python; neither is a valid version.
        json.dumps({"version": True, "sources": [{"name": "a.pst", "min_count": 1}]}),
        json.dumps({"version": 1.0, "sources": [{"name": "a.pst", "min_count": 1}]}),
        _one(min_count=0),
        _one(min_cnt=1),
        _one(name="outbound-bes", min_count=1),
        _one(min_count=1, optional=True),
        _one(min_count=3, approved_gap=1),
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {"name": "a.pst", "min_count": 1},
                    {"name": "A.PST", "min_count": 1},
                ],
            }
        ),
    ],
)
def test_per_source_bad_baseline_refused_with_reason(
    _init_db: Callable, tmp_path: Path, baseline_text: str
) -> None:
    result = _per_source(
        _init_db, tmp_path, {"a.pst": 100}, _baseline(tmp_path, baseline_text)
    )
    assert result["passed"] is False
    assert result["refusal_reason"]


def test_per_source_missing_baseline_file_and_no_baseline_refused(
    _init_db: Callable, tmp_path: Path
) -> None:
    for baseline in (tmp_path / "nope.json", None):
        result = _per_source(_init_db, tmp_path, {"a.pst": 100}, baseline)
        assert result["passed"] is False
        assert result["refusal_reason"]


def test_verify_opens_candidate_db_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "ro.db"
    attachment_root = tmp_path / "attachments"
    _build_healthy_db(db_path, attachment_root)
    before = db_path.read_bytes()
    report, passed = _vr.verify(db_path, attachment_root, _baseline(tmp_path))
    assert passed is True and report["overall"] == "PASS"
    assert db_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Check C — null_hash_by_mode
# ---------------------------------------------------------------------------


def test_null_hash_by_mode_pass_with_no_nulls(_init_db: Callable) -> None:
    conn = _init_db("null_hash_pass")
    mid = _insert_message(
        conn,
        source_input_mode="file",
        pst_source="/test/path/sample.pst",
        msg_id_suffix="nh1",
    )
    _insert_attachment(conn, mid, content_hash="a" * 64)
    _insert_fts(conn, mid)
    conn.commit()

    result = check_null_hash_by_mode(conn)
    conn.close()

    assert result["passed"] is True
    assert result["counts"].get("pst", 0) == 0


def test_null_hash_by_mode_fail_with_pst_import_null(_init_db: Callable) -> None:
    conn = _init_db("null_hash_fail")
    mid = _insert_message(
        conn,
        source_input_mode="file",
        pst_source="/test/path/sample.pst",
        msg_id_suffix="nh2",
    )
    _insert_attachment(conn, mid, content_hash=None)
    _insert_fts(conn, mid)
    conn.commit()

    result = check_null_hash_by_mode(conn)
    conn.close()

    assert result["passed"] is False
    assert result["counts"].get("pst", 0) > 0


def test_null_hash_by_mode_ignores_archive_scrape_nulls(_init_db: Callable) -> None:
    conn = _init_db("null_hash_scrape")
    mid = _insert_message(
        conn,
        source_input_mode="archive",
        pst_source="archive:test-message.eml",
        msg_id_suffix="nh3",
    )
    _insert_attachment(conn, mid, content_hash=None)
    _insert_fts(conn, mid)
    conn.commit()

    result = check_null_hash_by_mode(conn)
    conn.close()

    # archive family NULLs are explicitly allowed — must not fail
    assert result["passed"] is True
    # Counts may include "archive" key but pst family must be absent/0
    assert result["counts"].get("pst", 0) == 0


# ---------------------------------------------------------------------------
# Check D — cas_spot_check
# ---------------------------------------------------------------------------


def test_cas_spot_check_pass_when_files_match(
    _init_db: Callable,
    tmp_path: Path,
    _make_cas_file: Callable,
) -> None:
    attachment_root = tmp_path / "attachments"
    conn = _init_db("cas_pass")
    contents = [b"file content alpha", b"file content beta", b"file content gamma"]
    for i, payload in enumerate(contents):
        digest = hashlib.sha256(payload).hexdigest()
        storage_path = _make_cas_file(attachment_root, payload)
        mid = _insert_message(conn, msg_id_suffix=f"cp{i}")
        _insert_attachment(
            conn,
            mid,
            content_hash=digest,
            storage_path=storage_path,
            size_bytes=len(payload),
        )
        _insert_fts(conn, mid)
    conn.commit()

    result = check_cas_spot_check(conn, attachment_root)
    conn.close()

    assert result["passed"] is True
    assert len(result["samples"]) == 3
    assert all(s["hash_match"] for s in result["samples"])


def test_cas_spot_check_fail_when_hash_mismatch(
    _init_db: Callable,
    tmp_path: Path,
    _make_cas_file: Callable,
) -> None:
    attachment_root = tmp_path / "attachments"
    conn = _init_db("cas_fail")
    # Write 3 files with known content, but record a wrong hash in the DB
    payloads = [b"data one", b"data two", b"data three"]
    wrong_hash = "f" * 64
    for i, payload in enumerate(payloads):
        # Write actual file with real sha256 path
        real_digest = hashlib.sha256(payload).hexdigest()
        l1, l2 = real_digest[0:2], real_digest[2:4]
        dest = attachment_root / l1 / l2 / f"{real_digest}.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        storage_path = f"attachments/{l1}/{l2}/{real_digest}.bin"

        mid = _insert_message(conn, msg_id_suffix=f"cf{i}")
        _insert_attachment(
            conn,
            mid,
            content_hash=wrong_hash,  # deliberately mismatched
            storage_path=storage_path,
            size_bytes=len(payload),
        )
        _insert_fts(conn, mid)
    conn.commit()

    result = check_cas_spot_check(conn, attachment_root)
    conn.close()

    assert result["passed"] is False
    assert any(not s["hash_match"] for s in result["samples"])


def test_cas_spot_check_skipped_only_when_no_attachments(
    _init_db: Callable, tmp_path: Path
) -> None:
    conn = _init_db("cas_none")
    result = check_cas_spot_check(conn, tmp_path / "attachments")
    conn.close()

    assert result["passed"] is True
    assert result["skipped_reason"] == "no-attachments"
    assert result["samples"] == []


def test_cas_spot_check_checks_fewer_than_3_attachments(
    _init_db: Callable,
    tmp_path: Path,
    _make_cas_file: Callable,
) -> None:
    attachment_root = tmp_path / "attachments"
    conn = _init_db("cas_skip")
    # Insert only 2 non-NULL attachment rows; the second one's file is missing
    for i in range(2):
        payload = f"payload {i}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        storage_path = _make_cas_file(attachment_root, payload)
        if i == 1:
            (attachment_root / storage_path.removeprefix("attachments/")).unlink()
        mid = _insert_message(conn, msg_id_suffix=f"cs{i}")
        _insert_attachment(
            conn,
            mid,
            content_hash=digest,
            storage_path=storage_path,
            size_bytes=len(payload),
        )
        _insert_fts(conn, mid)
    conn.commit()

    result = check_cas_spot_check(conn, attachment_root)
    conn.close()

    assert len(result["samples"]) == 2
    assert "skipped_reason" not in result
    assert result["passed"] is False


# ---------------------------------------------------------------------------
# Check E — fts_rowid_alignment
# ---------------------------------------------------------------------------


def test_fts_rowid_alignment_pass(_init_db: Callable) -> None:
    conn = _init_db("fts_pass")
    for i in range(3):
        mid = _insert_message(conn, msg_id_suffix=f"fp{i}")
        _insert_fts(conn, mid)
    conn.commit()

    result = check_fts_rowid_alignment(conn)
    conn.close()

    assert result["passed"] is True
    assert result["orphans"] == 0


def test_fts_rowid_alignment_fail(_init_db: Callable) -> None:
    conn = _init_db("fts_fail")
    # Insert a message WITHOUT a matching FTS row
    _insert_message(conn, msg_id_suffix="ff1")
    # Intentionally skip _insert_fts(conn, mid)
    conn.commit()

    result = check_fts_rowid_alignment(conn)
    conn.close()

    assert result["passed"] is False
    assert result["orphans"] > 0


# ---------------------------------------------------------------------------
# End-to-end subprocess tests (F + G)
# ---------------------------------------------------------------------------


def _build_healthy_db(db_path: Path, attachment_root: Path) -> None:
    """Create a fully valid DB: REQUIRED PSTs x 2 rows, FTS rows, and CAS files."""
    from mailattic.storage import initialize_database

    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    psts = [p for p in REQUIRED for _ in range(2)]
    for i, pst in enumerate(psts):
        mid = _insert_message(conn, pst_source=pst, msg_id_suffix=f"e2e{i}")
        payload = f"e2e attachment {i}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        l1, l2 = digest[0:2], digest[2:4]
        dest = attachment_root / l1 / l2 / f"{digest}.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        storage_path = f"attachments/{l1}/{l2}/{digest}.bin"
        _insert_attachment(
            conn,
            mid,
            content_hash=digest,
            storage_path=storage_path,
            size_bytes=len(payload),
        )
        _insert_fts(conn, mid)

    conn.commit()
    conn.close()


def _run_cli(db_path: Path, attachment_root: Path, *extra: str):
    return subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(_SCRIPT_PATH),
            "--db",
            str(db_path),
            "--attachment-root",
            str(attachment_root),
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=str(_SCRIPT_PATH.parent.parent),
        shell=False,
    )


def test_overall_pass_returns_exit_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "healthy.db"
    attachment_root = tmp_path / "attachments"
    attachment_root.mkdir()
    _build_healthy_db(db_path, attachment_root)
    baseline = _baseline(tmp_path)

    proc = _run_cli(db_path, attachment_root, "--baseline", str(baseline))

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    report = json.loads(proc.stdout)
    assert report["overall"] == "PASS"
    sources = {s["name"]: s for s in report["per_source_counts"]["sources"]}
    assert sources["main.pst"]["actual"] == 2
    assert sources["main.pst"]["threshold"] == 2
    assert sources["archive.pst"]["status"] == "absent_approved"


def test_cli_fails_when_baseline_minimum_not_met(tmp_path: Path) -> None:
    db_path = tmp_path / "short.db"
    attachment_root = tmp_path / "attachments"
    attachment_root.mkdir()
    _build_healthy_db(db_path, attachment_root)
    sources = [
        {"name": "arc_a.pst", "min_count": 2},
        {"name": "main.pst", "min_count": 3},
    ]
    baseline = _baseline(tmp_path, sources)

    proc = _run_cli(db_path, attachment_root, "--baseline", str(baseline))

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    report = json.loads(proc.stdout)
    assert report["overall"] == "FAIL"
    short = report["per_source_counts"]["sources"][1]
    assert (short["status"], short["actual"], short["expected_min_count"]) == (
        "short",
        2,
        3,
    )
    # the other mechanical checks are untouched by the source failure
    assert report["fts_rowid_alignment"]["passed"] is True


@pytest.mark.parametrize("baseline_text", [None, "{ not json", "{}"])
def test_cli_without_usable_baseline_never_passes(
    tmp_path: Path, baseline_text: str | None
) -> None:
    db_path = tmp_path / "healthy.db"
    attachment_root = tmp_path / "attachments"
    attachment_root.mkdir()
    _build_healthy_db(db_path, attachment_root)
    extra: tuple[str, ...] = ()
    if baseline_text is not None:
        extra = ("--baseline", str(_baseline(tmp_path, baseline_text)))

    proc = _run_cli(db_path, attachment_root, *extra)

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    report = json.loads(proc.stdout)
    assert report["overall"] == "FAIL"
    assert report["per_source_counts"]["refusal_reason"]
    assert report["integrity_check"]["passed"] is True
