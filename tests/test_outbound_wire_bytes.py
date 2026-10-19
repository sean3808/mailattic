"""#295: the bytes put on the wire are the reviewed draft minus Bcc, CRLF.

Unlike the other send tests, nothing about ``smtplib`` is replaced by a fake
here: the real stdlib ``SMTP`` client talks to a tiny receiver bound to
127.0.0.1 on an ephemeral port, and the test compares what the receiver got
with the draft file. ``SMTP.send_message`` rewrote body lines starting with
``From `` to ``>From `` on the way out, which only a real serialisation sees.
"""

from __future__ import annotations

import smtplib
import socketserver
import threading
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailattic.outbound import SendRefused, SmtpConfig, draft_fingerprint, send_draft
from mailattic.storage import initialize_database
from tests.test_outbound_draft_attach import _strip_bcc_and_use_crlf

SMTP_USER = "sean3808@mailattic.test"
BCC_ADDRESS = "hidden@example.com"


class _Received:
    def __init__(self) -> None:
        self.mail_from: str | None = None
        self.mail_params: str = ""
        self.rcpts: list[str] = []
        self.data: bytes | None = None


class _Handler(socketserver.StreamRequestHandler):
    server: _Receiver

    def _reply(self, text: str) -> None:
        self.wfile.write(text.encode() + b"\r\n")

    def handle(self) -> None:
        received = self.server.received
        self._reply("220 receiver.test ready")
        while line := self.rfile.readline():
            command = line.decode("utf-8", "replace").rstrip("\r\n")
            verb = command.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                lines = ["receiver.test", "AUTH PLAIN"]
                if self.server.smtputf8:
                    lines.append("SMTPUTF8")
                lines.append("8BITMIME")
                for item in lines[:-1]:
                    self._reply(f"250-{item}")
                self._reply(f"250 {lines[-1]}")
            elif verb == "AUTH":
                self._reply("235 ok")
            elif verb == "MAIL":
                address, _, params = command[len("MAIL FROM:") :].partition(">")
                received.mail_from = address.strip().lstrip("<")
                received.mail_params = params.strip()
                self._reply("250 ok")
            elif verb == "RCPT":
                address = command[len("RCPT TO:") :].strip().strip("<>")
                if address in self.server.refuse:
                    self._reply("550 refused")
                else:
                    received.rcpts.append(address)
                    self._reply("250 ok")
            elif verb == "DATA":
                self._reply("354 go")
                chunks: list[bytes] = []
                while (body_line := self.rfile.readline()) != b".\r\n":
                    if not body_line:
                        return
                    chunks.append(body_line.removeprefix(b"."))
                received.data = b"".join(chunks)
                self._reply("250 queued")
            elif verb == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("502 unsupported")


class _Receiver(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *, smtputf8: bool = True, refuse: set[str] = frozenset()):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.received = _Received()
        self.smtputf8 = smtputf8
        self.refuse = refuse


@pytest.fixture
def receiver_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    servers: list[_Receiver] = []

    def start(**kwargs: object) -> _Received:
        server = _Receiver(**kwargs)  # type: ignore[arg-type]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        port = server.server_address[1]
        # Plain real SMTP client, 127.0.0.1 only (production uses SMTP_SSL).
        monkeypatch.setattr(
            "mailattic.outbound.smtplib.SMTP_SSL",
            lambda host, _port, timeout, context: smtplib.SMTP(
                "127.0.0.1", port, timeout=timeout
            ),
        )
        return server.received

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _config() -> SmtpConfig:
    return SmtpConfig(host="smtp.example.com", port=465, user=SMTP_USER, password="x")


def _make_draft(
    tmp_path: Path,
    draft_id: str,
    body: str,
    *,
    cte: str | None = None,
    to: str = "Alice Example <alice@example.com>",
    bcc: str = BCC_ADDRESS,
    utf8: bool = False,
) -> tuple[Path, bytes]:
    db_path = tmp_path / "wire.db"
    if not db_path.exists():
        initialize_database(db_path)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg["Bcc"] = bcc
    msg["Subject"] = "wire check"
    msg["Date"] = "Tue, 13 Oct 2026 10:00:00 +0800"
    msg["Message-ID"] = f"<{draft_id}@mailattic.test>"
    if cte is None:
        msg.set_content(body)
    else:
        msg.set_content(body, cte=cte)
    raw = msg.as_bytes(policy=msg.policy.clone(utf8=utf8))
    drafts = tmp_path / "outbound" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / f"{draft_id}.eml").write_bytes(raw)
    return db_path, raw


def _send(db_path: Path, draft_id: str, raw: bytes) -> dict[str, object]:
    return send_draft(
        draft_id, db_path, _config(), {}, fingerprint=draft_fingerprint(raw)
    )


FROM_LINES = (
    "Hello,\nFrom next week we ship every Tuesday.\n"
    "Bcc: body line, not a header\nFrom Alice\nBye\n"
)
LONG_QP = "From " + "the quick brown fox jumps over the lazy dog " * 6 + "end é\n"
CHINESE = "您好，附上報價單，請查收。\nFrom next week we ship every Tuesday.\n再見\n"


@pytest.mark.parametrize(
    ("name", "body", "cte"),
    [
        ("plain", FROM_LINES, None),
        ("qp", LONG_QP, "quoted-printable"),
        ("cjk-8bit", CHINESE, "8bit"),
        ("cjk-default", CHINESE, None),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 12 else None,
)
def test_wire_bytes_are_draft_minus_bcc_with_from_lines_intact(
    tmp_path: Path, receiver_factory, name: str, body: str, cte: str | None
) -> None:
    received = receiver_factory()
    db_path, raw = _make_draft(tmp_path, f"d-{name}", body, cte=cte)
    assert b"\nFrom " in raw.replace(b"\r\n", b"\n") or name == "cjk-default"

    result = _send(db_path, f"d-{name}", raw)

    assert received.data == _strip_bcc_and_use_crlf(raw)
    assert b">From " not in received.data
    assert received.mail_from == SMTP_USER
    assert sorted(received.rcpts) == sorted(["alice@example.com", BCC_ADDRESS])
    assert b"Bcc:" not in received.data.split(b"\r\n\r\n")[0]
    assert "refused" not in result


def test_folded_bcc_header_is_removed_whole(tmp_path: Path, receiver_factory) -> None:
    received = receiver_factory()
    bcc = ", ".join(f"hidden-recipient-{n}@example.com" for n in range(8))
    db_path, raw = _make_draft(tmp_path, "d-fold", FROM_LINES, bcc=bcc)
    assert b"\n hidden-recipient-" in raw  # the header really folded

    _send(db_path, "d-fold", raw)

    assert received.data == _strip_bcc_and_use_crlf(raw)
    assert b"hidden-recipient" not in received.data
    assert len(received.rcpts) == 9  # Bcc recipients still get the mail


def test_non_ascii_address_uses_smtputf8_and_stays_byte_exact(
    tmp_path: Path, receiver_factory
) -> None:
    received = receiver_factory(smtputf8=True)
    db_path, raw = _make_draft(
        tmp_path, "d-eai", FROM_LINES, to="Zoë <zoë@example.com>", utf8=True
    )
    assert "zoë@example.com".encode() in raw  # 8-bit header, as in the file

    _send(db_path, "d-eai", raw)

    assert received.data == _strip_bcc_and_use_crlf(raw)
    assert "SMTPUTF8" in received.mail_params
    assert "BODY=8BITMIME" in received.mail_params
    assert "zoë@example.com" in received.rcpts


def test_non_ascii_address_without_server_smtputf8_is_refused_before_mail(
    tmp_path: Path, receiver_factory
) -> None:
    received = receiver_factory(smtputf8=False)
    db_path, raw = _make_draft(
        tmp_path, "d-eai-no", FROM_LINES, to="Zoë <zoë@example.com>", utf8=True
    )

    with pytest.raises(SendRefused, match="require internationalized email"):
        _send(db_path, "d-eai-no", raw)

    assert received.mail_from is None
    assert received.data is None


def test_partial_refusal_still_reports_refused_recipients(
    tmp_path: Path, receiver_factory
) -> None:
    received = receiver_factory(refuse={"alice@example.com"})
    db_path, raw = _make_draft(tmp_path, "d-refuse", FROM_LINES)

    result = _send(db_path, "d-refuse", raw)

    assert [r["address"] for r in result["refused"]] == ["alice@example.com"]  # type: ignore[index]
    assert received.rcpts == [BCC_ADDRESS]
    assert received.data == _strip_bcc_and_use_crlf(raw)
