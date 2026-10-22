"""Single dedicated write accessor for the MCP server.

:class:`ReadOnlyMailReader` (``mcp_readonly``) is the only way the MCP server
reads the database; this module is the only way it writes anything outside
the process. The two are obtained separately at server construction, so
which tools can write is enumerable from where this accessor is used, not
from a flag on the reader. Attachment export never opens the database:
callers read the rows they need through the reader and hand them over.
Drafting reads its anchor message read-only; sending goes through the
CLI's shared submit path (#189), whose post-submit finalization records the
sent message in the database exactly as ``bes send-draft`` does.

It writes host files in three controlled ways:
1. Attachment copies from the content-addressed store into a host directory
   the caller names (issue #28). It never overwrites: an existing destination
   file is reported as skipped and left byte-for-byte untouched. It never
   lands a file type Windows runs when the file is opened (issue #205).
2. Reply drafts in the MCP-dedicated drafts folder (issue #189), assembled
   via :func:`~mailattic.outbound.assemble_draft_reply`.
3. Outbound mail submission of a reviewed MCP draft (issue #189), after
   re-verifying the draft's SHA-256 fingerprint against the draft on disk.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .attachment_store import (
    AttachmentExportItem,
    export_attachments,
    resolve_store_root,
)
from .imap_runtime import load_env_file
from .outbound import (
    ENV_VAR_SMTP_ENV_FILE,
    DraftReplyResult,
    SmtpConfig,
    _prepare_draft_for_send,
    _read_marker,
    _submit_prepared_draft,
    _verify_fingerprint,
    assemble_draft_reply,
    load_smtp_config,
    own_address_without_smtp_settings,
    reply_font_style,
)

_SEND_LOCK = threading.Lock()


def _not_sent(what: str, fix: str, cause: OSError) -> ValueError:
    """#307: a pre-SMTP failure worded like #292 -- outcome, then next step."""
    return ValueError(
        f"Not sent: {what}. Fix {fix}, then call mail_send again with the same "
        f"draft_id and fingerprint. Cause: {cause}"
    )


# Issue #205: file types Windows executes, installs from, loads, or follows
# when the file is merely opened -- which is all an auto-run location (the
# Startup folder, a Run key, a scheduled task) does at logon. CoWork steered
# by mail content can name any dest_dir, and auto-run locations cannot be
# enumerated, so the guard is on the type of the file that would land. Types
# that still need a user decision after opening (macro documents under the
# default "disable with notification", archives) are not on the list; the
# folders that remove that decision are refused separately below.
_BLOCKED_EXPORT_EXTENSIONS = frozenset(
    extension.upper()
    for extension in {
        # executables and installers
        ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp", ".mst",
        ".msc", ".appx", ".appxbundle", ".msix", ".msixbundle",
        ".msu", ".appinstaller", ".application", ".appref-ms", ".vsto",
        ".xbap", ".jnlp",
        ".gadget", ".diagcab",
        # batch files and script hosts
        ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
        ".ws", ".wsc", ".sct", ".hta", ".ps1", ".psm1", ".psd1", ".ps1xml",
        ".psc1", ".msh", ".msh1", ".msh2", ".mshxml", ".msh1xml", ".msh2xml",
        ".jar", ".sh",
        # Python: run on open, and .pth runs at every interpreter start
        ".py", ".pyw", ".pyz", ".pyzw", ".pyc", ".pyo", ".pth",
        # shortcuts and shell-followed pointers
        ".lnk", ".url", ".scf", ".website", ".shb", ".shs",
        ".settingcontent-ms", ".appcontent-ms", ".library-ms",
        ".searchconnector-ms",
        # registry, setup, help and theme files that run or load code
        ".reg", ".inf", ".ins", ".isp", ".chm", ".theme", ".themepack",
        ".deskthemepack",
        # Office add-ins, which load code rather than open a document
        ".xll", ".xla", ".xlam", ".wll", ".ppa", ".ppam",
    }
)  # fmt: skip


def _refuse_runnable_filename(filename: str) -> str | None:
    """Why *filename* must not be exported through MCP, or ``None``."""
    # Windows drops trailing dots and spaces, and matches extensions by
    # upper-casing -- so ".vb\u017f" is ".VBS" to it, which lower() misses.
    suffix = Path(filename.rstrip(" .")).suffix
    if suffix.upper() in _BLOCKED_EXPORT_EXTENSIONS:
        return f"runnable type {suffix} not exported; save it from Outlook"
    return None


def _auto_open_folders() -> tuple[Path, ...]:
    """Folders whose files open by themselves, with no decision left.

    Office loads every file in its startup folders as a trusted location, so
    a macro workbook there runs without the prompt it gets anywhere else;
    the Startup folder opens everything at logon. Office's default trusted
    locations also include the per-user Templates and Addins folders: a
    macro document there does not open itself, but once opened it runs
    with no prompt, which removes the same decision. Refusing these costs
    nothing -- nobody exports mail attachments into them on purpose.
    """
    # Fail closed: a launcher that trims the environment must not switch
    # the refusal off, so fall back to the profile's default location.
    appdata = os.environ.get("APPDATA", "").strip()
    roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    microsoft = roaming / "Microsoft"
    return (
        microsoft / "Excel" / "XLSTART",
        microsoft / "Word" / "STARTUP",
        microsoft / "Windows" / "Start Menu" / "Programs" / "Startup",
        microsoft / "Templates",
        microsoft / "Addins",
    )


def _lands_in(directory: Path, folder: Path) -> bool:
    """Whether existing *directory* is *folder* or somewhere below it.

    Spelling is compared the way Windows does (upper-cased), and identity is
    compared on the file system, so a spelling Windows normalises away --
    ``\\\\?\\`` prefixes, trailing dots or spaces, 8.3 short names --
    still matches once the directory exists.
    """
    folder = folder.expanduser().resolve()
    if Path(str(directory).upper()).is_relative_to(Path(str(folder).upper())):
        return True
    if not folder.is_dir():
        return False
    for candidate in (directory, *directory.parents):
        try:
            if os.path.samefile(candidate, folder):
                return True
        except OSError:
            continue
    return False


def _load_mcp_smtp_env() -> tuple[dict[str, str], SmtpConfig]:
    """Load SMTP settings from ``MAILATTIC_SMTP_ENV_FILE`` only.

    Unlike the CLI's :func:`~mailattic.outbound.resolve_smtp_env_file`,
    there is deliberately no fallback to the built-in default file (#189,
    Sean 2026-09-24): unset, blank, missing, or unparsable raises ``ValueError``.
    """
    raw_env_file = os.environ.get(ENV_VAR_SMTP_ENV_FILE)
    if raw_env_file is None or not raw_env_file.strip():
        raise ValueError(
            f"Not sent: {ENV_VAR_SMTP_ENV_FILE} environment variable is unset or "
            "empty, so mail cannot be sent via MCP. The operator must point it at "
            "an SMTP settings file."
        )
    env_path = Path(raw_env_file.strip())
    if not env_path.is_file():
        raise ValueError(
            "Not sent: SMTP settings file does not exist or is unreadable. The "
            f"operator must fix {ENV_VAR_SMTP_ENV_FILE}. File: {env_path}"
        )
    try:
        env_mapping = load_env_file(env_path)
        return env_mapping, load_smtp_config(env_mapping)
    except Exception as exc:
        raise ValueError(
            "Not sent: Failed to load SMTP settings. The operator must fix the "
            f"file. Reason: {exc} (file: {env_path})"
        ) from exc


@dataclass(frozen=True)
class MailWriter:
    """Write accessor bound to one resolved attachment store root and database."""

    attachment_root: Path
    database_path: Path
    draft_dir: Path

    @classmethod
    def for_database(cls, database_path: Path) -> MailWriter:
        # Same store-root resolution as the CLI (flag aside): env override,
        # else the ``attachments/`` directory next to the database.
        resolved_database_path = database_path.resolve()
        return cls(
            attachment_root=resolve_store_root(
                database_path,
                env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
            ),
            database_path=resolved_database_path,
            draft_dir=resolved_database_path.parent / "outbound" / "mcp-drafts",
        )

    def export_attachments(
        self,
        attachment_rows: Sequence[Mapping[str, object]],
        destination_dir: Path,
    ) -> list[AttachmentExportItem]:
        """Copy each row's stored payload into *destination_dir*, never
        overwriting; returns one outcome per row, in row order.

        Raises ``ValueError`` when *destination_dir* lies inside the store
        itself -- the store stays read-only to this accessor -- or inside a
        folder whose files open by themselves (#205). Each payload
        is checked against its recorded ``content_hash`` before it is
        written, so a corrupted store file is reported, not handed over.
        A row whose export name is a runnable type is reported as
        ``skipped-blocked-type``; its payload is not read and nothing is
        written for it (#205).
        """
        resolved = destination_dir.expanduser().resolve()
        in_store = ValueError(
            "it is inside the attachment store, which this tool never "
            "writes. Choose a folder outside it."
        )
        if resolved.is_relative_to(self.attachment_root.resolve()):
            raise in_store
        # Created first so the checks below can compare file-system identity;
        # a refused call can leave an empty directory, never a file.
        resolved.mkdir(parents=True, exist_ok=True)
        if _lands_in(resolved, self.attachment_root):
            raise in_store
        if any(_lands_in(resolved, folder) for folder in _auto_open_folders()):
            raise ValueError(
                "files there open automatically or run macros without a prompt "
                "(Startup, an Office startup folder, or Office Templates/"
                "Addins). Choose an ordinary folder."
            )
        return export_attachments(
            attachment_rows=list(attachment_rows),
            store_root=self.attachment_root,
            destination_dir=resolved,
            skip_existing=True,
            verify_content_hash=True,
            refuse_filename=_refuse_runnable_filename,
        ).items

    def draft_reply(
        self,
        message_id: str,
        body_text: str,
        *,
        to: Sequence[str] | None = None,
        cc: Sequence[str] | None = None,
        bcc: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        replace_recipients: bool = False,
        attachments: Sequence[str | Path] | None = None,
    ) -> DraftReplyResult:
        """Assemble a reply draft in the MCP-dedicated drafts folder."""
        # #268 (overturns #189's "settings load only at send time"): From is
        # the configured account, so the send-time from-invariant accepts
        # what was reviewed. No account configured and no single declared
        # self address means no draft -- there is no built-in address.
        env: Mapping[str, str] = {}
        try:
            env, smtp_config = _load_mcp_smtp_env()
            own_address = smtp_config.user
        except ValueError:
            own_address = own_address_without_smtp_settings(
                self.database_path, read_only=True
            )
        return assemble_draft_reply(
            database_path=self.database_path,
            message_id=message_id,
            draft_dir=self.draft_dir,
            own_address=own_address,
            body_text=body_text,
            to=to,
            cc=cc,
            bcc=bcc,
            exclude=exclude,
            replace_recipients=replace_recipients,
            attachments=attachments,
            read_only=True,
            font_style=reply_font_style(env),
        )

    def send_draft(
        self,
        draft_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        """Send a reviewed draft from the MCP drafts folder.

        Enforces:
        - Settings file must be specified via MAILATTIC_SMTP_ENV_FILE.
          Unset, empty, whitespace-only, or unreadable raises ValueError
          with no fallback to DEFAULT_SMTP_ENV_FILE.
        - The draft must exist in the MCP-dedicated draft directory.
        - The fingerprint must match the draft's SHA-256 fingerprint.
        - Re-checks send-once marker and anti-spoofing from-invariant.
        - Passes the loaded env mapping to _submit_prepared_draft for IMAP APPEND.
        """
        if (
            not draft_id
            or "/" in draft_id
            or "\\" in draft_id
            or ".." in draft_id
            or Path(draft_id).name != draft_id
        ):
            raise ValueError(
                "Invalid draft_id (must be a bare filename without path "
                "components); not sent. Use the draft_id returned by "
                f"mail_draft_reply. Received: '{draft_id}'"
            )

        # Belt-and-braces: the bare-name check above already rules this out.
        target_file = (self.draft_dir / f"{draft_id}.eml").resolve()
        try:
            target_file.relative_to(self.draft_dir.resolve())
        except ValueError as exc:
            raise ValueError(
                "Invalid draft_id (path traversal outside MCP drafts "
                "directory); not sent. Use the draft_id returned by "
                f"mail_draft_reply. Received: '{draft_id}'"
            ) from exc

        env_mapping, smtp_config = _load_mcp_smtp_env()

        # The framework runs sync tools on worker threads, so two mail_send
        # calls for one draft can overlap; the send-once marker is read then
        # written, so both could pass. Serialize marker check through submit.
        # ponytail: process-wide lock only; a concurrent CLI send of the same
        # draft is another process (tracked as a #195 follow-up).
        with _SEND_LOCK:
            try:
                prepared = _prepare_draft_for_send(
                    draft_id,
                    self.database_path,
                    draft_dir=self.draft_dir,
                )
            except FileNotFoundError:
                raise  # worded by mcp_server.mail_send
            except OSError as exc:
                # #307: the only bare OSError _prepare_draft_for_send lets
                # through is the draft read itself; SMTP has not started.
                raise _not_sent(
                    "the draft file could not be read", "the file access", exc
                ) from exc

            _verify_fingerprint(prepared, fingerprint)

            try:
                return _submit_prepared_draft(
                    prepared,
                    database_path=self.database_path,
                    smtp_config=smtp_config,
                    env=env_mapping,
                )
            except OSError as exc:
                # #307: claim "not sent" only on evidence, not on which code
                # path an OSError is believed to come from. The 'sending'
                # marker is written before any SMTP call and the draft was
                # unmarked at preparation, so a still-absent marker proves
                # SMTP never started. Marker present or unreadable: outcome
                # unknown -- re-raise unchanged rather than invite a resend.
                try:
                    marker = _read_marker(self.database_path, prepared.message_id)
                except Exception as read_exc:
                    raise exc from read_exc
                if marker is not None:
                    raise
                raise _not_sent(
                    "the send-once marker could not be read or written",
                    "the outbound folder access",
                    exc,
                ) from exc
