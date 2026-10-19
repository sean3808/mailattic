"""ball-in-court must not turn a truncated or unordered thread into a verdict (#234)."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills/mailattic/scripts/ball-in-court.py"
)


def _run(tmp_path: Path, messages: list[dict], stderr: str = ""):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(messages), encoding="utf-8")
    err = tmp_path / "err.txt"
    err.write_text(stderr, encoding="utf-8")
    (tmp_path / "uv.cmd").write_text(f"@echo off\necho {tmp_path}\n")
    (tmp_path / "bes.cmd").write_text(
        f'@echo off\ntype "{payload}"\ntype "{err}" 1>&2\n', encoding="utf-8"
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--seed-db-id", "1"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path};{os.environ['PATH']}",
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _msg(
    i: int, direction: str | None, body: str = "Handled.", minute: int | None = None
):
    return {
        "id": i,
        "sent_at": f"2026-09-01T{(minute if minute is not None else i) // 60:02d}:"
        f"{(minute if minute is not None else i) % 60:02d}:00",
        "sender_email": "x@example.com",
        "direction": direction,
        "body_text": body,
    }


def test_capped_thread_refuses_verdict_and_keeps_warning(tmp_path: Path):
    msgs = [_msg(i, "outbound") for i in range(1, 101)]
    warning = "[warn] thread exceeded 100 messages, truncated to first 100 by sent_at"
    result = _run(tmp_path, msgs, warning)
    assert result.returncode != 0
    assert "[our-side]" not in result.stdout
    assert "[BALL-IN-COURT]" not in result.stdout
    assert "[indeterminate]" in result.stdout
    assert "truncated" in result.stderr


def test_success_stderr_warning_is_forwarded(tmp_path: Path):
    result = _run(tmp_path, [_msg(1, "inbound", "Thanks")], "[warn] something")
    assert result.returncode == 0
    assert "[warn] something" in result.stderr


def test_latest_is_chosen_by_sent_at_not_position(tmp_path: Path):
    msgs = [
        _msg(2, "inbound", "Please quote?", minute=30),
        _msg(1, "outbound", minute=5),
    ]
    result = _run(tmp_path, msgs)
    assert "[BALL-IN-COURT]" in result.stdout
    assert "db_id=2" in result.stdout


def test_latest_is_chosen_by_instant_across_offsets(tmp_path: Path):
    # id 1 is the later instant (01:00Z) but the smaller string; string order
    # would pick id 2 (08:00+08:00 = 00:00Z) and call it our side.
    msgs = [
        {**_msg(2, "outbound"), "sent_at": "2026-09-01T08:00:00+08:00"},
        {**_msg(1, "inbound", "Please quote?"), "sent_at": "2026-09-01T01:00:00+00:00"},
    ]
    result = _run(tmp_path, msgs)
    assert "[BALL-IN-COURT]" in result.stdout
    assert "db_id=1" in result.stdout


def test_naive_sent_at_is_read_as_utc_like_the_stored_corpus(tmp_path: Path):
    # A stored sent_at without an offset is UTC (query.py SearchFilters docstring,
    # D-20260723-1): 01:00 naive is one hour later than 08:00+08:00.
    inbound = {**_msg(2, "inbound", "Please quote?"), "sent_at": "2026-09-01T01:00:00"}
    outbound = {**_msg(1, "outbound"), "sent_at": "2026-09-01T08:00:00+08:00"}
    result = _run(tmp_path, [inbound, outbound])
    assert "[BALL-IN-COURT]" in result.stdout
    assert "db_id=2" in result.stdout


def test_unknown_direction_is_not_called_our_side(tmp_path: Path):
    result = _run(tmp_path, [_msg(1, None, "Please quote?")])
    assert result.returncode == 0
    assert "[our-side]" not in result.stdout
    assert "[unknown-direction]" in result.stdout


def test_unparseable_sent_at_in_a_thread_refuses_verdict(tmp_path: Path):
    # The other party's latest chaser carries a broken date: sorting it as the
    # oldest would call the thread handled by our earlier reply.
    for broken in ("", None, "not-a-date"):
        msgs = [
            _msg(1, "outbound", minute=5),
            {**_msg(2, "inbound", "Please quote?"), "sent_at": broken},
        ]
        result = _run(tmp_path, msgs)
        assert result.returncode == 3, broken
        assert "[indeterminate]" in result.stdout
        assert "db_id=2" in result.stdout
        assert "[our-side]" not in result.stdout
        assert "[BALL-IN-COURT]" not in result.stdout


def test_single_message_needs_no_date_to_be_the_latest(tmp_path: Path):
    result = _run(tmp_path, [{**_msg(1, "inbound", "Please quote?"), "sent_at": None}])
    assert result.returncode == 0
    assert "[BALL-IN-COURT]" in result.stdout
