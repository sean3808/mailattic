"""Entry-level tests for skills/.../scripts/find-cross-filter.py (issue #233)."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "mailattic"
    / "scripts"
    / "find-cross-filter.py"
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = initialize_database(tmp_path / "probe.db")
    source = PstSource(
        path=tmp_path / "s.pst", input_mode="file", requested_from=tmp_path / "s.pst"
    )
    conn = connect_database(path)
    rows = [
        ("taipei-morning", "2026-09-01T08:00:00+08:00", "must quote a"),
        ("utc-early", "2026-09-01T01:00:00+00:00", "must quote b"),
        ("taipei-first-second", "2026-09-01T00:00:00+08:00", "must quote c"),
        ("taipei-last-second", "2026-09-01T23:59:59+08:00", "must quote d"),
        ("before", "2026-08-31T23:59:59+08:00", "must quote e"),
        ("after", "2026-09-02T00:00:00+08:00", "must quote f"),
    ]
    for label, stamp, body in rows:
        raw = {
            "subject": label,
            "sender": {"email": "a@x.example"},
            "to": [{"email": "b@x.example"}],
            "sent_at": stamp,
            "body_text": body,
            "message_id": f"<{label}@t.example>",
        }
        persist_message(
            conn,
            build_normalized_message(
                raw, source=source, folder_path="Inbox", logical_locator=label
            ),
        )
    conn.commit()
    conn.close()
    return path


def run(
    db: Path, *extra: str, context: str = "quote", frm="2026-09-01", to="2026-09-01"
):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--must",
            "must",
            "--context",
            context,
            "--from-date",
            frm,
            "--to-date",
            to,
            "--database",
            str(db),
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def subjects(stdout: str) -> set[str]:
    return {
        line.split("Subject:", 1)[1].strip()
        for line in stdout.splitlines()
        if "Subject:" in line
    }


def test_same_day_window_is_taipei_whole_day_inclusive(db: Path) -> None:
    r = run(db)
    assert r.returncode == 0
    assert subjects(r.stdout) == {
        "taipei-morning",
        "utc-early",
        "taipei-first-second",
        "taipei-last-second",
    }


def test_context_case_does_not_change_candidates(db: Path) -> None:
    lower = subjects(run(db, to="2026-09-02").stdout)
    assert len(lower) == 5
    assert subjects(run(db, context="QUOTE", to="2026-09-02").stdout) == lower


def test_missing_snippet_anchor_keeps_sql_hits_visible(db: Path) -> None:
    r = run(db, "--snippet-keywords", "nonexistent")
    assert r.returncode == 0
    assert len(subjects(r.stdout)) == 4
    assert "must quote" in r.stdout  # original body text still shown
    assert "anchor" in r.stderr


@pytest.mark.parametrize("bad", ["2026-13-01", "2026/09/01", "garbage"])
def test_invalid_date_is_rejected_loudly(db: Path, bad: str) -> None:
    r = run(db, frm=bad)
    assert r.returncode == 2
    assert "--from-date" in r.stderr and bad in r.stderr
    r = run(db, to=bad)
    assert r.returncode == 2
    assert "--to-date" in r.stderr


# --- #298: --must matches literally (LIKE wildcards are not special) ---------

MUST_BODIES = {
    "us-ok": "PEN_100 usd450 quote",
    "us-wild": "PENX100 usd500 quote",
    "pct-ok": "discount 10% quote",
    "pct-wild": "discount 100 percent quote",
    "bs-ok": r"part A\B quote",
    "bs-wild": "part AB quote",
    "br-ok": "part [X] quote",
    "br-wild": "part X quote",
    "hy-ok": "PEN-100 quote",
    "cjk-ok": "客戶乙商行 quote",
    "case-ok": "Plain Keyword quote",
}


@pytest.fixture
def must_db(tmp_path: Path) -> Path:
    path = initialize_database(tmp_path / "must.db")
    source = PstSource(
        path=tmp_path / "s.pst", input_mode="file", requested_from=tmp_path / "s.pst"
    )
    conn = connect_database(path)
    for label, body in MUST_BODIES.items():
        raw = {
            "subject": label,
            "sender": {"email": "a@x.example"},
            "to": [{"email": "b@x.example"}],
            "sent_at": "2026-09-01T08:00:00+08:00",
            "body_text": body,
            "message_id": f"<{label}@t.example>",
        }
        persist_message(
            conn,
            build_normalized_message(
                raw, source=source, folder_path="Inbox", logical_locator=label
            ),
        )
    conn.commit()
    conn.close()
    return path


def run_must(db: Path, must: str, context: str = "quote"):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--must",
            must,
            "--context",
            context,
            "--from-date",
            "2026-09-01",
            "--to-date",
            "2026-09-01",
            "--database",
            str(db),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("must", "expected"),
    [
        ("PEN_100", {"us-ok"}),
        ("10%", {"pct-ok"}),
        ("A\\B", {"bs-ok"}),
        ("[X]", {"br-ok"}),
        # healthy shapes stay as before
        ("PEN-100", {"hy-ok"}),
        ("乙商行", {"cjk-ok"}),
        ("plain keyword", {"case-ok"}),
        ("PLAIN KEYWORD", {"case-ok"}),
    ],
)
def test_must_matches_literally(must_db: Path, must: str, expected: set[str]) -> None:
    r = run_must(must_db, must)
    assert r.returncode == 0
    assert subjects(r.stdout) == expected


def test_context_keeps_pattern_semantics(must_db: Path) -> None:
    # --context is deliberately still a LIKE pattern (#298 non-goal): "_" is a wildcard.
    r = run_must(must_db, "discount", context="10_ ")
    assert subjects(r.stdout) == {"pct-ok", "pct-wild"}


# --- #301: Stage 3.5 customer split, command taken from the skill document ----

SKILL_DOC = SCRIPT.parents[1] / "references" / "capability-b-find.md"

# (label, sender, recipient). Every mail matches the same must/context terms, so
# only the domain flag can tell the customers apart.
CUSTOMER_MAILS = [
    ("acme-in", "alice@acme.example", "me@own.test"),
    ("acme-out", "me@own.test", "bob@acme.example"),
    ("acme-upper", "carol@ACME.example", "me@own.test"),
    ("other-in", "dave@other.example", "me@own.test"),
    ("other-out", "me@own.test", "erin@other.example"),
    ("lookalike-prefix", "frank@notacme.example", "me@own.test"),
    ("lookalike-suffix", "me@own.test", "gina@acme.example.test"),
    ("lookalike-sub", "hank@mail.acme.example", "me@own.test"),
    ("wild-ok", "ivy@a_c.example", "me@own.test"),
    ("wild-bad", "jack@abc.example", "me@own.test"),
]


@pytest.fixture
def customers_db(tmp_path: Path) -> Path:
    path = initialize_database(tmp_path / "customers.db")
    source = PstSource(
        path=tmp_path / "s.pst", input_mode="file", requested_from=tmp_path / "s.pst"
    )
    conn = connect_database(path)
    for label, sender, recipient in CUSTOMER_MAILS:
        raw = {
            "subject": label,
            "sender": {"email": sender},
            "to": [{"email": recipient}],
            "sent_at": "2026-09-01T08:00:00+08:00",
            "body_text": "must quote",
            "message_id": f"<{label}@t.example>",
        }
        persist_message(
            conn,
            build_normalized_message(
                raw, source=source, folder_path="Inbox", logical_locator=label
            ),
        )
    conn.commit()
    conn.close()
    return path


def documented_stage35_argv() -> list[str]:
    """The fenced find-cross-filter command inside the Stage 3.5 section."""
    text = "\n".join(SKILL_DOC.read_text(encoding="utf-8").splitlines())
    section = text.split("### Stage 3.5", 1)[1].split("\n### ", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    commands = [re.sub(r"\\\n", " ", b) for b in blocks if "find-cross-filter.py" in b]
    assert len(commands) == 1, "Stage 3.5 must show exactly one cross-filter command"
    return shlex.split(commands[0])


def run_documented_stage35(db: Path, domain: str):
    argv = documented_stage35_argv()
    assert argv[:2] == ["uv", "run"] and "{SKILL_DIR}" in argv[2]
    # Mechanical substitution: interpreter + script path, then each documented
    # flag value is replaced by the test's value; unknown flags stay and fail.
    values = {
        "--must": "must",
        "--context": "quote",
        "--from-date": "2026-09-01",
        "--to-date": "2026-09-01",
        "--participants-domain": domain,
    }
    out = [sys.executable, str(SCRIPT), "--database", str(db)]
    rest = argv[3:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        out.append(flag)
        if flag in values:
            out.append(values[flag])
            i += 1
        elif i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            out.append(rest[i + 1])
            i += 1
        i += 1
    return subprocess.run(out, capture_output=True, text=True, encoding="utf-8")


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("acme.example", {"acme-in", "acme-out", "acme-upper"}),
        ("other.example", {"other-in", "other-out"}),
        ("a_c.example", {"wild-ok"}),
    ],
)
def test_documented_stage35_command_keeps_only_named_domain(
    customers_db: Path, domain: str, expected: set[str]
) -> None:
    r = run_documented_stage35(customers_db, domain)
    assert r.returncode == 0, r.stderr
    assert subjects(r.stdout) == expected


def test_participants_domain_is_repeatable_or(customers_db: Path) -> None:
    r = run(
        customers_db,
        "--participants-domain",
        "other.example",
        "--participants-domain",
        "notacme.example",
    )
    assert r.returncode == 0, r.stderr
    assert subjects(r.stdout) == {"other-in", "other-out", "lookalike-prefix"}
