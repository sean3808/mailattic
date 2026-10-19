"""tests/test_mcp_send.py
======================
Anchor and contract tests for Contract #7: outbound conversational
authorization (MCP form), plus unit and integration tests for
mail_draft_reply and mail_send.
"""

from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import mcp.types as mcp_types
import pytest
from mcp import Client

from mailattic import outbound as outbound_module
from mailattic.identity import add_self_address
from mailattic.mcp_server import create_server
from mailattic.mcp_writer import MailWriter
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import (
    ENV_VAR_SMTP_ENV_FILE,
    _marker_path,
    draft_fingerprint,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SMTP_USER = "sean3808@mailattic.test"


def _call_tool(
    server, name: str, arguments: dict[str, object]
) -> mcp_types.CallToolResult:
    async def _call() -> mcp_types.CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(_call())


def _seed_message(
    db_path: Path, message_id: str = "<anchor-msg@mailattic.test>"
) -> str:
    conn = connect_database(db_path)
    try:
        norm = NormalizedMessage(
            subject="Original Subject",
            sender=MailboxParticipant(
                display_name="External Sender", email="external@example.com"
            ),
            to_recipients=(MailboxParticipant(display_name="Sean", email=SMTP_USER),),
            cc_recipients=(),
            bcc_recipients=(),
            sent_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
            received_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
            body_text="Hello, this is the original message.",
            attachments=(),
            message_id=message_id,
            in_reply_to=None,
            references=None,
            pst_source="pst:test",
            folder_path="Inbox",
            logical_locator="test:1",
            source_input_mode="test",
            source_requested_from="test",
        )
        persist_message(conn, norm)
        conn.commit()
    finally:
        conn.close()
    return message_id


def _create_env_file(path: Path, *, extra_keys: dict[str, str] | None = None) -> Path:
    lines = [
        "SMTP_HOST=smtp.example.com",
        "SMTP_PORT=465",
        f"SMTP_USER={SMTP_USER}",
        "SMTP_PASSWORD=secret",
        "IMAP_HOST=imap.example.com",
        "IMAP_PORT=993",
        f"IMAP_USER={SMTP_USER}",
        "IMAP_PASSWORD=secret",
    ]
    if extra_keys:
        for k, v in extra_keys.items():
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _patch_smtp(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_smtp = MagicMock()
    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.return_value.__enter__.return_value = mock_smtp
    monkeypatch.setattr(
        "mailattic.outbound.smtplib.SMTP_SSL",
        mock_smtp_ssl,
    )
    return mock_smtp


# ---------------------------------------------------------------------------
# Anchor 1: Fingerprint mismatch refused without marker
# ---------------------------------------------------------------------------


def test_mcp_send_fingerprint_mismatch_refused_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract #7 Anchor 1:
    When mail_send is called with a mismatched fingerprint, the send is refused,
    no send-once marker is written, and no SMTP submission occurs.
    The error message must NOT leak the current fingerprint.
    """
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    env_file = _create_env_file(tmp_path / "smtp.env")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    server = create_server(database_path=db_path)

    # 1. Draft reply
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body text"},
    )
    assert not draft_res.is_error
    draft_data = draft_res.structured_content
    assert draft_data is not None
    draft_id = draft_data["draft_id"]
    real_fp = draft_data["fingerprint"]
    mcp_draft_file = tmp_path / "outbound" / "mcp-drafts" / f"{draft_id}.eml"
    assert mcp_draft_file.is_file()

    # Tamper with draft bytes or provide mismatched fingerprint
    fake_fp = "0" * 64
    mock_smtp = _patch_smtp(monkeypatch)

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fake_fp},
    )
    assert send_res.is_error
    error_msg = send_res.content[0].text
    assert "fingerprint mismatch" in error_msg
    # Must NOT echo the actual fingerprint to prevent bypassing review
    assert real_fp not in error_msg

    # Verify no marker written and no SMTP submission
    message_id_header = draft_data["message_id"]
    marker_file = _marker_path(db_path, message_id_header)
    assert not marker_file.exists()
    mock_smtp.sendmail.assert_not_called()


# ---------------------------------------------------------------------------
# Anchor 2: CLI drafts refused
# ---------------------------------------------------------------------------


def test_mcp_send_refuses_cli_drafts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract #7 Anchor 2:
    mail_send reads ONLY from the MCP-dedicated drafts directory.
    Drafts located in the CLI draft directory (outbound/drafts/) are refused,
    even when supplied with their valid fingerprint.
    """
    db_path = initialize_database(tmp_path / "test.db")
    env_file = _create_env_file(tmp_path / "smtp.env")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    # Create a draft in CLI drafts folder
    cli_drafts_dir = tmp_path / "outbound" / "drafts"
    cli_drafts_dir.mkdir(parents=True, exist_ok=True)
    cli_draft_id = "cli-draft-1234"
    draft_msg_id = "<cli-draft-1234@mailattic.test>"

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "client@example.com"
    msg["Subject"] = "CLI Draft"
    msg["Message-ID"] = draft_msg_id
    msg.set_content("CLI draft body")
    draft_bytes = msg.as_bytes()
    cli_draft_path = cli_drafts_dir / f"{cli_draft_id}.eml"
    cli_draft_path.write_bytes(draft_bytes)
    cli_fp = draft_fingerprint(draft_bytes)

    server = create_server(database_path=db_path)
    mock_smtp = _patch_smtp(monkeypatch)

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": cli_draft_id, "fingerprint": cli_fp},
    )
    assert send_res.is_error
    err_text = send_res.content[0].text
    assert (
        "not found in MCP drafts folder" in err_text or "not found" in err_text.lower()
    )

    # No marker, no SMTP
    assert not _marker_path(db_path, draft_msg_id).exists()
    mock_smtp.sendmail.assert_not_called()


# ---------------------------------------------------------------------------
# Anchor 3: Production callers whitelist for _submit_prepared_draft
# ---------------------------------------------------------------------------


def test_submit_prepared_draft_caller_whitelist() -> None:
    """Contract #7 Anchor 3:
    Enumerate all production callers of `_submit_prepared_draft` inside
    `src/mailattic/`.
    The set of callers MUST BE EXACTLY:
      - `send_draft` in `outbound.py`
      - `send_batch` in `outbound.py`
      - `MailWriter.send_draft` in `mcp_writer.py`
    Any unexpected fourth caller fails this test. Every reference to the name
    counts, not only direct calls (``f = _submit_prepared_draft``); importing
    it under another name, or naming it in a string (``getattr``-style
    reflection), fails outright — both would hide a caller from this scan.
    """
    target = "_submit_prepared_draft"
    repo_root = Path(__file__).resolve().parent.parent
    src_dir = repo_root / "src" / "mailattic"

    actual_callers: set[tuple[str, str]] = set()
    evasions: list[str] = []

    for py_file in src_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        file_name = py_file.name

        class Visitor(ast.NodeVisitor):
            def __init__(self, filename: str) -> None:
                self.filename = filename
                self.scope_stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def _record(self) -> None:
                actual_callers.add((self.filename, ".".join(self.scope_stack)))

            def visit_Name(self, node: ast.Name) -> None:
                if node.id == target:
                    self._record()

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr == target:
                    self._record()
                self.generic_visit(node)

            def visit_alias(self, node: ast.alias) -> None:
                if node.name == target and node.asname not in (None, target):
                    evasions.append(f"{self.filename}: import as {node.asname}")

            def visit_Constant(self, node: ast.Constant) -> None:
                if node.value == target:
                    evasions.append(f"{self.filename}: string reference")

        Visitor(file_name).visit(tree)

    assert not evasions, (
        f"{target} referenced in a way this scan cannot follow: {evasions}"
    )

    expected_callers = {
        ("outbound.py", "send_draft"),
        ("outbound.py", "send_batch"),
        ("mcp_writer.py", "MailWriter.send_draft"),
    }

    assert actual_callers == expected_callers, (
        f"Production callers of _submit_prepared_draft drifted!\n"
        f"Expected: {expected_callers}\n"
        f"Actual: {actual_callers}"
    )


# ---------------------------------------------------------------------------
# Settings file tests: Option B (Sean 2026-09-24)
# ---------------------------------------------------------------------------


def test_mcp_send_refuses_when_smtp_env_file_unset_no_default_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MAILATTIC_SMTP_ENV_FILE is unset, mail_send refuses clearly.
    It must NEVER fall back to DEFAULT_SMTP_ENV_FILE, even if that file exists!
    """
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)

    # Place a valid env file at DEFAULT_SMTP_ENV_FILE path to verify it is NEVER read
    mock_default = tmp_path / "fake_default.env"
    _create_env_file(mock_default)
    monkeypatch.setattr(outbound_module, "DEFAULT_SMTP_ENV_FILE", mock_default)

    # Ensure env var is completely unset
    monkeypatch.delenv(ENV_VAR_SMTP_ENV_FILE, raising=False)

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body"},
    )
    assert not draft_res.is_error
    draft_id = draft_res.structured_content["draft_id"]
    fp = draft_res.structured_content["fingerprint"]

    mock_smtp = _patch_smtp(monkeypatch)

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fp},
    )
    assert send_res.is_error
    assert "MAILATTIC_SMTP_ENV_FILE" in send_res.content[0].text
    target_msg_id = draft_res.structured_content["message_id"]
    assert not _marker_path(db_path, target_msg_id).exists()
    mock_smtp.sendmail.assert_not_called()


@pytest.mark.parametrize("bad_val", ["", "   ", "\t\n"])
def test_mcp_send_refuses_when_smtp_env_file_empty_or_whitespace(
    tmp_path: Path,
    bad_val: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, bad_val)

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body"},
    )
    draft_id = draft_res.structured_content["draft_id"]
    fp = draft_res.structured_content["fingerprint"]

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fp},
    )
    assert send_res.is_error
    assert "MAILATTIC_SMTP_ENV_FILE" in send_res.content[0].text


def test_mcp_send_refuses_when_smtp_env_file_missing_or_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)
    non_existent = tmp_path / "does_not_exist.env"
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(non_existent))

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body"},
    )
    draft_id = draft_res.structured_content["draft_id"]
    fp = draft_res.structured_content["fingerprint"]

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fp},
    )
    assert send_res.is_error
    assert "does not exist or is unreadable" in send_res.content[0].text


# ---------------------------------------------------------------------------
# Loaded env mapping passed to _submit_prepared_draft (Issue #114)
# ---------------------------------------------------------------------------


def test_mcp_send_passes_env_mapping_to_submit_prepared_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env dict loaded from MAILATTIC_SMTP_ENV_FILE MUST be passed to
    _submit_prepared_draft so that APPEND to Sent is not silently skipped (#114).
    """
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    custom_key = "CUSTOM_AUDIT_TAG"
    env_file = _create_env_file(
        tmp_path / "smtp.env", extra_keys={custom_key: "audit_12345"}
    )
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body"},
    )
    draft_id = draft_res.structured_content["draft_id"]
    fp = draft_res.structured_content["fingerprint"]

    _patch_smtp(monkeypatch)

    captured_env: dict[str, str] | None = None
    real_submit = outbound_module._submit_prepared_draft

    def spy_submit(prepared, database_path, smtp_config, env=None):
        nonlocal captured_env
        captured_env = dict(env) if env else None
        return real_submit(prepared, database_path, smtp_config, env=env)

    monkeypatch.setattr(
        "mailattic.mcp_writer._submit_prepared_draft",
        spy_submit,
    )

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fp},
    )
    assert not send_res.is_error
    assert captured_env is not None
    assert captured_env.get(custom_key) == "audit_12345"
    assert captured_env.get("IMAP_HOST") == "imap.example.com"


def test_mcp_draft_reply_opens_database_read_only_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract #1: the MCP server never opens the database writable to read.
    Drafting reads its anchor through outbound + query, two modules below the
    AST guard's reach, so pin the connection mode itself."""
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)
    server = create_server(database_path=db_path)

    modes: list[bool] = []

    def spy(module: str):
        real = __import__(module, fromlist=["connect_database"]).connect_database

        def wrapped(*args, **kwargs):
            modes.append(bool(kwargs.get("read_only", False)))
            return real(*args, **kwargs)

        monkeypatch.setattr(f"{module}.connect_database", wrapped)

    spy("mailattic.outbound")
    spy("mailattic.query")

    res = _call_tool(
        server, "mail_draft_reply", {"message_id": msg_id, "body_text": "Draft body"}
    )
    assert not res.is_error
    assert modes, "draft path opened no connection through the spied modules"
    assert all(modes), modes


def _env_file_for_account(tmp_path: Path, account: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        _create_env_file(tmp_path / ".env.base")
        .read_text(encoding="utf-8")
        .replace(SMTP_USER, account),
        encoding="utf-8",
    )
    return env_file


def test_mcp_draft_reply_from_is_the_configured_smtp_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#268 (overturns #189's draft-time rule, D-20261013-1): the draft's
    From follows the configured account, so drafting then sending with an
    account outside the placeholder domain passes the from-invariant."""
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    monkeypatch.setenv(
        ENV_VAR_SMTP_ENV_FILE, str(_env_file_for_account(tmp_path, "other@example.com"))
    )
    mock_smtp = _patch_smtp(monkeypatch)

    writer = MailWriter.for_database(db_path)
    draft = writer.draft_reply(message_id=msg_id, body_text="Draft body")
    assert draft.from_address == "other@example.com"

    writer.send_draft(draft.draft_id, draft.fingerprint)
    mock_smtp.sendmail.assert_called_once()


def test_mcp_draft_reply_falls_back_to_the_one_declared_self_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    monkeypatch.delenv(ENV_VAR_SMTP_ENV_FILE, raising=False)
    _declare_self(db_path, "me@example.com")

    draft = MailWriter.for_database(db_path).draft_reply(
        message_id=msg_id, body_text="Draft body"
    )
    assert draft.from_address == "me@example.com"


@pytest.mark.parametrize("declared", [(), ("me@example.com", "alias@example.com")])
def test_mcp_draft_reply_refused_without_settings_or_one_self_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: tuple[str, ...],
) -> None:
    """#268: no built-in address. With no readable settings and zero or
    several declared self addresses there is no draft, and the refusal names
    both things the operator can set."""
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    monkeypatch.delenv(ENV_VAR_SMTP_ENV_FILE, raising=False)
    _declare_self(db_path, *declared)

    writer = MailWriter.for_database(db_path)
    with pytest.raises(outbound_module.DraftReplyError) as refusal:
        writer.draft_reply(message_id=msg_id, body_text="Draft body")

    assert "SMTP_USER" in str(refusal.value)
    assert "bes identity add" in str(refusal.value)
    assert not list(writer.draft_dir.glob("*.eml"))


def _declare_self(db_path: Path, *addresses: str) -> None:
    conn = connect_database(db_path)
    try:
        for address in addresses:
            add_self_address(conn, address)
        conn.commit()
    finally:
        conn.close()


def test_concurrent_mcp_sends_of_one_draft_submit_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework runs sync tools on worker threads, so CoWork can issue
    two mail_send calls for one draft at once. The send-once marker is read
    then written, so without serialization both calls pass it and the mail
    goes out twice. Exactly one SMTP submission must happen."""
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    env_file = _create_env_file(tmp_path / "smtp.env")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    writer = MailWriter.for_database(db_path)
    draft = writer.draft_reply(message_id=msg_id, body_text="Draft body")

    mock_smtp = _patch_smtp(monkeypatch)
    # Widen the marker read -> "sending" write window (envelope extraction
    # runs between them) so an unserialized pair both pass the read.
    real_extract = outbound_module._extract_envelope_recipients

    def slow_extract(*args, **kwargs):
        time.sleep(0.3)
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(outbound_module, "_extract_envelope_recipients", slow_extract)

    start = threading.Barrier(2)
    outcomes: list[str] = []

    def send() -> None:
        start.wait()
        try:
            writer.send_draft(draft.draft_id, draft.fingerprint)
            outcomes.append("sent")
        except Exception as exc:
            outcomes.append(f"refused: {exc}")

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert mock_smtp.sendmail.call_count == 1, outcomes
    assert outcomes.count("sent") == 1, outcomes


# ---------------------------------------------------------------------------
# mail_draft_reply end-to-end and options
# ---------------------------------------------------------------------------


def test_mcp_draft_reply_creates_draft_and_returns_full_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)

    # Attachments
    attach1 = tmp_path / "spec.pdf"
    attach1.write_bytes(b"%PDF-sample-bytes")
    attach2 = tmp_path / "notes.txt"
    attach2.write_text("plain notes", encoding="utf-8")

    server = create_server(database_path=db_path)

    reply_text = "Thank you for the update.\nBest regards,\nSean"
    res = _call_tool(
        server,
        "mail_draft_reply",
        {
            "message_id": msg_id,
            "body_text": reply_text,
            "to": ["extra_to@example.com"],
            "cc": ["extra_cc@example.com"],
            "attachments": [str(attach1), str(attach2)],
        },
    )
    assert not res.is_error
    data = res.structured_content
    assert data is not None

    draft_id = data["draft_id"]
    fp = data["fingerprint"]
    assert len(fp) == 64  # SHA-256 hex string

    # Verify draft file exists in outbound/mcp-drafts, NOT outbound/drafts
    mcp_file = tmp_path / "outbound" / "mcp-drafts" / f"{draft_id}.eml"
    cli_file = tmp_path / "outbound" / "drafts" / f"{draft_id}.eml"
    assert mcp_file.is_file()
    assert not cli_file.exists()

    # Fingerprint matches disk file bytes
    assert draft_fingerprint(mcp_file.read_bytes()) == fp

    # Verify return payload fields
    assert "external@example.com" in data["to"]
    assert "extra_to@example.com" in data["to"]
    assert "extra_cc@example.com" in data["cc"]
    assert data["from"] == SMTP_USER
    assert "Original Subject" in data["subject"]
    assert reply_text in data["body_text"]
    assert data["response_truncated"] is False

    # Attachments metadata: host path, name, size
    attachments = data["attachments"]
    assert len(attachments) == 2
    paths = {att["path"] for att in attachments}
    assert str(attach1.resolve()) in paths
    assert str(attach2.resolve()) in paths
    names = {att["name"] for att in attachments}
    assert "spec.pdf" in names
    assert "notes.txt" in names
    sizes = {att["name"]: att["size_bytes"] for att in attachments}
    assert sizes["spec.pdf"] == len(b"%PDF-sample-bytes")


def test_mcp_send_success_with_matching_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    env_file = _create_env_file(tmp_path / "smtp.env")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Approved content"},
    )
    assert not draft_res.is_error
    draft_id = draft_res.structured_content["draft_id"]
    fp = draft_res.structured_content["fingerprint"]
    outbound_msg_id = draft_res.structured_content["message_id"]

    mock_smtp = _patch_smtp(monkeypatch)

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fp},
    )
    assert not send_res.is_error
    send_data = send_res.structured_content
    assert send_data["status"] == "sent"
    assert send_data["message_id"] == outbound_msg_id
    assert send_data["draft_id"] == draft_id

    mock_smtp.sendmail.assert_called_once()

    # Marker updated to sent
    marker_file = _marker_path(db_path, outbound_msg_id)
    assert marker_file.is_file()
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    assert marker["status"] == "sent"

    # Message persisted in DB with pst_source='outbound-bes'
    conn = connect_database(db_path)
    try:
        row = conn.execute(
            "SELECT pst_source, body_text FROM messages WHERE message_id=?",
            [outbound_msg_id],
        ).fetchone()
        assert row is not None
        assert row[0] == "outbound-bes"
        assert "Approved content" in row[1]
    finally:
        conn.close()


def test_mcp_draft_reply_ladder_reduces_large_body(
    tmp_path: Path,
) -> None:
    """When the draft body is massive, the degradation ladder shrinks body_text
    to fit within MCP_RESPONSE_BUDGET_CHARS, reporting response_truncated=True.
    """
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)

    # 130k characters body (exceeds 120,000 budget)
    massive_body = "A" * 130_000

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": massive_body},
    )
    assert not draft_res.is_error
    data = draft_res.structured_content
    assert data is not None
    assert data["response_truncated"] is True
    assert len(data["body_text"]) < len(massive_body)
    assert len(draft_res.content[0].text) <= 120_000
    assert "... [" in data["body_text"]
    assert "truncated" in data["body_text"]
    # The user never saw the full draft, so there is no reviewed draft to send.
    assert data["fingerprint"] is None


@pytest.mark.parametrize(
    "traversal_id",
    [
        "../drafts/cli_draft",
        "..\\drafts\\cli_draft",
        "/etc/passwd",
        "sub/draft",
        "..",
        "",
    ],
)
def test_mcp_send_refuses_path_traversal_draft_id(
    tmp_path: Path,
    traversal_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security / Confused Deputy: draft_id cannot traverse outside the
    MCP draft folder.
    """
    db_path = initialize_database(tmp_path / "test.db")
    env_file = _create_env_file(tmp_path / "smtp.env")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    server = create_server(database_path=db_path)
    res = _call_tool(
        server,
        "mail_send",
        {"draft_id": traversal_id, "fingerprint": "a" * 64},
    )
    assert res.is_error
    err_text = res.content[0].text
    assert (
        "Invalid draft_id" in err_text
        or "path traversal" in err_text
        or "Input validation failed" in err_text
    )


def test_mcp_draft_reply_cleans_absent_cc_bcc(
    tmp_path: Path,
) -> None:
    """Absent Cc and Bcc should be returned as empty string, not literal 'None'."""
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    _declare_self(db_path, SMTP_USER)

    server = create_server(database_path=db_path)
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body without cc/bcc"},
    )
    assert not draft_res.is_error
    data = draft_res.structured_content
    assert data is not None
    assert data["cc"] == ""
    assert data["cc"] != "None"
    assert data["bcc"] == SMTP_USER
    assert data["bcc"] != "None"
    assert data["fingerprint"]
