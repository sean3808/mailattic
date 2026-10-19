"""Issue #285 invariant test: attachments in legal MIME shapes must not vanish.

Every non-text leaf node or explicit attachment part must leave an attachment
record in the database (with payload in the CAS) or an ingest_failures row,
and attachments without a filename must receive an identifiable fallback name
searchable via `bes attachments`.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sqlite3
from pathlib import Path

from mailattic.attachment_store import read_attachment
from mailattic.cli import main
from mailattic.query import SearchFilters, search_attachments, search_messages

PDF_PAYLOAD = b"%PDF-1.4 synthetic quote payload for issue 285"
PDF_B64 = base64.b64encode(PDF_PAYLOAD).decode("ascii")
CSV_PAYLOAD = b"item,price\r\nwidget,100\r\n"
CAL_PAYLOAD = (
    b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Visit\r\nEND:VEVENT\r\nEND:VCALENDAR"
)


def _write_eml(cur: Path, slug: str, raw_text: str) -> None:
    path = cur / f"{slug}.eml"
    path.write_bytes(
        raw_text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    )


def test_attachment_shapes_persist_and_never_silently_drop(tmp_path: Path) -> None:
    account_root = tmp_path / "maildir" / "acct"
    cur = account_root / "INBOX" / "cur"
    cur.mkdir(parents=True)
    db = tmp_path / "test.db"
    attachment_root = tmp_path / "attachments"

    # Shape 1 (control): fully labelled attachment
    _write_eml(
        cur,
        "01-control",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 01-control
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <01-control@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Please find the quotation attached.
--b
Content-Type: application/pdf
Content-Disposition: attachment; filename="control.pdf"
Content-Transfer-Encoding: base64

{PDF_B64}
--b--
""",
    )

    # Shape 2: named only by Content-Type name=, no Content-Disposition
    _write_eml(
        cur,
        "02-name-only",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 02-name-only
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <02-name-only@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Please find the quotation attached.
--b
Content-Type: application/pdf; name="quote-2024.pdf"
Content-Transfer-Encoding: base64

{PDF_B64}
--b--
""",
    )

    # Shape 3: entire email is single-part PDF (same name=, no disposition)
    _write_eml(
        cur,
        "03-single-pdf",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 03-single-pdf
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <03-single-pdf@example.com>
MIME-Version: 1.0
Content-Type: application/pdf; name="single-2024.pdf"
Content-Transfer-Encoding: base64

{PDF_B64}
""",
    )

    # Shape 4: text/csv; name="prices.csv" (no disposition)
    _write_eml(
        cur,
        "04-text-csv",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 04-text-csv
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <04-text-csv@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Prices attached.
--b
Content-Type: text/csv; name="prices.csv"

{CSV_PAYLOAD.decode("ascii")}
--b--
""",
    )

    # Shape 5: text/calendar; name="meeting.ics" (no disposition)
    _write_eml(
        cur,
        "05-text-cal",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 05-text-cal
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <05-text-cal@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Meeting attached.
--b
Content-Type: text/calendar; name="meeting.ics"

{CAL_PAYLOAD.decode("ascii")}
--b--
""",
    )

    # Shape 6: Content-Disposition: attachment with no filename at all
    _write_eml(
        cur,
        "06-no-filename",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 06-no-filename
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <06-no-filename@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

File attached without filename.
--b
Content-Type: application/pdf
Content-Disposition: attachment
Content-Transfer-Encoding: base64

{PDF_B64}
--b--
""",
    )

    # Shape 7: unquoted RFC 2047 encoded-word as filename ("報價單.pdf")
    _write_eml(
        cur,
        "07-unquoted-ew",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 07-unquoted-ew
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <07-unquoted-ew@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Quote attached with unquoted encoded word.
--b
Content-Type: application/pdf
Content-Disposition: attachment; filename==?utf-8?B?5aCx5YO55ZauLnBkZg==?=
Content-Transfer-Encoding: base64

{PDF_B64}
--b--
""",
    )

    # Shape 8: calendar invite in multipart/mixed (no filename, no disposition)
    _write_eml(
        cur,
        "08-cal-mixed",
        f"""From: alice@example.com
To: bob@example.test
Subject: quote 08-cal-mixed
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <08-cal-mixed@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Calendar event in mixed.
--b
Content-Type: text/calendar; method=REQUEST; charset=utf-8

{CAL_PAYLOAD.decode("ascii")}
--b--
""",
    )

    # Shape 9 (boundary check): multipart/alternative with calendar alternative
    # Must NOT become an attachment; body plain text is preserved.
    _write_eml(
        cur,
        "09-alt-cal",
        f"""From: alice@example.com
To: bob@example.test
Subject: invite 09-alt-cal
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <09-alt-cal@example.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="alt"

--alt
Content-Type: text/plain; charset=utf-8

Please attend the meeting.
--alt
Content-Type: text/calendar; method=REQUEST; charset=utf-8

{CAL_PAYLOAD.decode("ascii")}
--alt--
""",
    )

    # Run import through the real CLI entry point
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        exit_code = main(
            [
                "import",
                "--maildir",
                str(account_root),
                "--account-email",
                "bob@example.test",
                "--database",
                str(db),
                "--attachment-root",
                str(attachment_root),
                "--json",
            ]
        )

    assert exit_code == 0, f"import failed with code {exit_code}: {out.getvalue()}"
    summary = json.loads(out.getvalue())
    assert summary["failure_count"] == 0
    assert summary["imported_messages"] == 9

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        messages = {
            row["subject"]: row
            for row in conn.execute(
                "SELECT id, subject, body_text, attachment_search_text FROM messages"
            ).fetchall()
        }
        attachments = conn.execute(
            """
            SELECT
                m.subject,
                a.attachment_name,
                a.storage_path,
                a.size_bytes,
                a.content_hash
            FROM message_attachments a
            JOIN messages m ON m.id = a.message_id
            ORDER BY a.id
            """
        ).fetchall()
        failures_count = conn.execute(
            "SELECT COUNT(*) FROM ingest_failures"
        ).fetchone()[0]

    assert failures_count == 0

    # Group attachments by message subject
    att_by_subject: dict[str, list[sqlite3.Row]] = {}
    for att in attachments:
        att_by_subject.setdefault(att["subject"], []).append(att)

    # Invariant: each of the 8 attachment shapes must have at least one attachment
    for subject_num in (
        "01-control",
        "02-name-only",
        "03-single-pdf",
        "04-text-csv",
        "05-text-cal",
        "06-no-filename",
        "07-unquoted-ew",
        "08-cal-mixed",
    ):
        full_subject = f"quote {subject_num}"
        assert full_subject in att_by_subject, (
            f"Attachment silently dropped for {full_subject!r}!"
        )
        assert len(att_by_subject[full_subject]) == 1

    # Negative boundary: 09-alt-cal must NOT have an attachment row
    assert "invite 09-alt-cal" not in att_by_subject
    assert "Please attend the meeting." in messages["invite 09-alt-cal"]["body_text"]

    # Verify specific filenames
    assert att_by_subject["quote 01-control"][0]["attachment_name"] == "control.pdf"
    assert (
        att_by_subject["quote 02-name-only"][0]["attachment_name"] == "quote-2024.pdf"
    )
    assert (
        att_by_subject["quote 03-single-pdf"][0]["attachment_name"] == "single-2024.pdf"
    )
    assert messages["quote 03-single-pdf"]["body_text"] == ""

    assert att_by_subject["quote 04-text-csv"][0]["attachment_name"] == "prices.csv"
    assert att_by_subject["quote 05-text-cal"][0]["attachment_name"] == "meeting.ics"

    # Shape 6: fallback name must be identifiable (unnamed-attachment-*)
    no_fn_name = att_by_subject["quote 06-no-filename"][0]["attachment_name"]
    assert no_fn_name == "unnamed-attachment-1.pdf"

    # Shape 7: decoded filename, NOT renamed to fallback
    assert att_by_subject["quote 07-unquoted-ew"][0]["attachment_name"] == "報價單.pdf"

    # Shape 8: calendar in mixed gets identifiable name
    cal_mixed_name = att_by_subject["quote 08-cal-mixed"][0]["attachment_name"]
    assert cal_mixed_name == "unnamed-attachment-1.ics"

    # Verify payload integrity in CAS
    control_storage = att_by_subject["quote 01-control"][0]["storage_path"]
    assert read_attachment(control_storage, attachment_root) == PDF_PAYLOAD

    no_fn_storage = att_by_subject["quote 06-no-filename"][0]["storage_path"]
    assert read_attachment(no_fn_storage, attachment_root) == PDF_PAYLOAD

    csv_storage = att_by_subject["quote 04-text-csv"][0]["storage_path"]
    assert read_attachment(csv_storage, attachment_root) == CSV_PAYLOAD

    # Acceptance criterion 2: bes attachments can find the unnamed attachment
    unnamed_results = search_attachments(
        database_path=db,
        filters=SearchFilters(attachment_name="unnamed"),
    )
    assert len(unnamed_results) >= 2
    unnamed_names = {r["attachment_name"] for r in unnamed_results}
    assert "unnamed-attachment-1.pdf" in unnamed_names
    assert "unnamed-attachment-1.ics" in unnamed_names

    ext_results = search_attachments(
        database_path=db,
        filters=SearchFilters(attachment_ext="pdf"),
    )
    ext_names = {r["attachment_name"] for r in ext_results}
    assert "unnamed-attachment-1.pdf" in ext_names
    assert "control.pdf" in ext_names
    assert "quote-2024.pdf" in ext_names
    assert "single-2024.pdf" in ext_names
    assert "報價單.pdf" in ext_names


def test_attachment_storage_failure_records_ingest_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """When an attachment cannot be saved, it leaves an ingest_failures row."""
    account_root = tmp_path / "maildir" / "acct"
    cur = account_root / "INBOX" / "cur"
    cur.mkdir(parents=True)
    db = tmp_path / "test.db"
    attachment_root = tmp_path / "attachments"

    _write_eml(
        cur,
        "failing-attachment",
        f"""From: alice@example.com
To: bob@example.test
Subject: failing attachment
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <fail-01@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Please find the quotation attached.
--b
Content-Type: application/pdf; name="quote.pdf"
Content-Transfer-Encoding: base64

{PDF_B64}
--b--
""",
    )

    import mailattic.attachment_store as att_store_module

    def failing_write(*args, **kwargs):
        raise OSError("Disk full / permission denied")

    monkeypatch.setattr(att_store_module, "write_attachment", failing_write)

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        main(
            [
                "import",
                "--maildir",
                str(account_root),
                "--account-email",
                "bob@example.test",
                "--database",
                str(db),
                "--attachment-root",
                str(attachment_root),
                "--json",
            ]
        )

    summary = json.loads(out.getvalue())
    assert summary["failure_count"] > 0
    assert summary["imported_messages"] == 1

    with sqlite3.connect(db) as conn:
        failures = conn.execute(
            "SELECT error_stage, attachment_name FROM ingest_failures"
        ).fetchall()

    assert len(failures) == 1
    assert failures[0][0] == "attachment-store"
    assert failures[0][1] == "quote.pdf"


def test_named_text_plain_and_html_stay_body_unless_disposition_attachment(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / "maildir" / "acct"
    cur = account_root / "INBOX" / "cur"
    cur.mkdir(parents=True)
    db = tmp_path / "test.db"
    attachment_root = tmp_path / "attachments"

    # Shape A: single-part text/plain; name="message.txt", no Content-Disposition
    _write_eml(
        cur,
        "01-plain-named",
        """From: alice@example.com
To: bob@example.test
Subject: l1-plain-named
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <01-plain-named@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8; name="message.txt"

Unique text plain content alpha
""",
    )

    # Shape B: single-part text/html; name="mail.html", no Content-Disposition
    _write_eml(
        cur,
        "02-html-named",
        """From: alice@example.com
To: bob@example.test
Subject: l1-html-named
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <02-html-named@example.com>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8; name="mail.html"

<html><body><p>Unique html content beta</p></body></html>
""",
    )

    # Shape C: multipart/alternative whose html part carries name="body.htm"
    _write_eml(
        cur,
        "03-alt-html-named",
        """From: alice@example.com
To: bob@example.test
Subject: l1-alt-html-named
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <03-alt-html-named@example.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="alt"

--alt
Content-Type: text/plain; charset=utf-8

Unique alternative plain content gamma
--alt
Content-Type: text/html; charset=utf-8; name="body.htm"

<html><body><p>Unique alternative html content delta</p></body></html>
--alt--
""",
    )

    # Control: text/plain with Content-Disposition: attachment; filename="control.txt"
    _write_eml(
        cur,
        "04-plain-control-attach",
        """From: alice@example.com
To: bob@example.test
Subject: l1-plain-control-attach
Date: Mon, 05 Feb 2024 10:00:00 +0800
Message-ID: <04-plain-control-attach@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mix"

--mix
Content-Type: text/plain; charset=utf-8

Please find control text attached.
--mix
Content-Type: text/plain; charset=utf-8
Content-Disposition: attachment; filename="control.txt"

Unique control attachment text epsilon
--mix--
""",
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        exit_code = main(
            [
                "import",
                "--maildir",
                str(account_root),
                "--account-email",
                "bob@example.test",
                "--database",
                str(db),
                "--attachment-root",
                str(attachment_root),
                "--json",
            ]
        )

    assert exit_code == 0, f"import failed with code {exit_code}: {out.getvalue()}"
    summary = json.loads(out.getvalue())
    assert summary["failure_count"] == 0
    assert summary["imported_messages"] == 4

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        messages = {
            row["subject"]: row
            for row in conn.execute(
                "SELECT id, subject, body_text FROM messages"
            ).fetchall()
        }
        attachments = conn.execute(
            """
            SELECT
                m.subject,
                a.attachment_name,
                a.storage_path
            FROM message_attachments a
            JOIN messages m ON m.id = a.message_id
            ORDER BY a.id
            """
        ).fetchall()

    att_by_subject: dict[str, list[sqlite3.Row]] = {}
    for att in attachments:
        att_by_subject.setdefault(att["subject"], []).append(att)

    # 1. Three named plain/html shapes produce NO attachment row
    assert "l1-plain-named" not in att_by_subject
    assert "l1-html-named" not in att_by_subject
    assert "l1-alt-html-named" not in att_by_subject

    # 2. Control produces 1 attachment row
    assert len(att_by_subject.get("l1-plain-control-attach", [])) == 1
    assert (
        att_by_subject["l1-plain-control-attach"][0]["attachment_name"] == "control.txt"
    )
    control_storage = att_by_subject["l1-plain-control-attach"][0]["storage_path"]
    assert (
        read_attachment(control_storage, attachment_root)
        == b"Unique control attachment text epsilon"
    )

    # 3. Body text is preserved in database
    assert "Unique text plain content alpha" in messages["l1-plain-named"]["body_text"]
    assert "Unique html content beta" in messages["l1-html-named"]["body_text"]
    assert (
        "Unique alternative plain content gamma"
        in messages["l1-alt-html-named"]["body_text"]
    )

    # 4. Searchable by keyword
    alpha_hits = search_messages(
        database_path=db, filters=SearchFilters(keyword="alpha")
    )
    assert len(alpha_hits) == 1
    assert alpha_hits[0]["subject"] == "l1-plain-named"

    beta_hits = search_messages(database_path=db, filters=SearchFilters(keyword="beta"))
    assert len(beta_hits) == 1
    assert beta_hits[0]["subject"] == "l1-html-named"

    gamma_hits = search_messages(
        database_path=db, filters=SearchFilters(keyword="gamma")
    )
    assert len(gamma_hits) == 1
    assert gamma_hits[0]["subject"] == "l1-alt-html-named"
