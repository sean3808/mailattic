from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailattic.datetime_bounds import TAIPEI
from mailattic.models import PstSource
from mailattic.outlook_pst import (
    PR_ADDRTYPE,
    PR_EMAIL_ADDRESS,
    PR_SMTP_ADDRESS,
    OutlookComReader,
    OutlookPstParser,
    PstExtractionAccount,
    RawOutlookFailure,
    RawOutlookMessage,
    RawOutlookRecipient,
    _load_outlook_namespace,
    normalize_outlook_recipients,
)


@pytest.mark.parametrize(
    ("recipient_type", "display_name", "address", "expected_role", "expected_name"),
    [
        (1, " Jo\u0308rg ", "to@example.com", "to", "Jörg"),
        (2, "Copy", "cc@example.com", "cc", "Copy"),
        (3, "Blind", "bcc@example.com", "bcc", "Blind"),
    ],
)
def test_normalize_outlook_recipient_roles_and_display_names(
    recipient_type: int,
    display_name: str,
    address: str,
    expected_role: str,
    expected_name: str,
) -> None:
    result = normalize_outlook_recipients(
        [
            RawOutlookRecipient(
                display_name=display_name,
                smtp_address=address,
                email_address="fallback@example.com",
                addrtype="SMTP",
                recipient_type=recipient_type,
            )
        ]
    )

    assert result.recipients == (
        {
            "name": expected_name,
            "email": address,
            "recipient_type": expected_role,
        },
    )


def test_normalize_rejects_packed_smtp_but_accepts_quoted_local_part() -> None:
    with pytest.raises(ValueError, match="not atomic"):
        normalize_outlook_recipients(
            [
                RawOutlookRecipient(
                    None,
                    "a@example.com; b@example.com",
                    None,
                    "SMTP",
                    1,
                )
            ]
        )

    result = normalize_outlook_recipients(
        [
            RawOutlookRecipient(
                "Quoted",
                '"owner.legacy@export-hub"@mailattic.test',
                None,
                "SMTP",
                1,
            )
        ]
    )
    assert result.recipients[0]["email"] == ('"owner.legacy@export-hub"@mailattic.test')
    assert result.unresolved_by_reason == {
        "missing_address": 0,
        "missing_addrtype": 0,
        "non_smtp_addrtype": 0,
        "unknown_recipient_type": 0,
    }


def test_normalize_rejects_empty_local_part_as_valueerror() -> None:
    # `@org-e.example` (empty local part) makes headerregistry raise
    # HeaderParseError, which is NOT a ValueError subclass. Unless it is
    # translated here, it escapes the caller's `except ValueError` and aborts
    # the whole PST instead of costing one message.
    with pytest.raises(ValueError, match="not atomic"):
        normalize_outlook_recipients(
            [RawOutlookRecipient(None, "@org-e.example", None, "SMTP", 1)]
        )


def test_smtp_address_precedes_email_address_and_blank_smtp_falls_back() -> None:
    result = normalize_outlook_recipients(
        [
            RawOutlookRecipient(
                display_name="Primary",
                smtp_address=" primary@example.com ",
                email_address="fallback@example.com",
                addrtype="smtp",
                recipient_type=1,
            ),
            RawOutlookRecipient(
                display_name="Fallback",
                smtp_address="   ",
                email_address=" fallback@example.com ",
                addrtype="SMTP",
                recipient_type=2,
            ),
        ]
    )

    assert [recipient["email"] for recipient in result.recipients] == [
        "primary@example.com",
        "fallback@example.com",
    ]


def test_duplicate_and_quoted_local_part_addresses_remain_atomic_rows() -> None:
    quoted_address = '"owner.legacy@export-hub"@mailattic.test'
    repeated = RawOutlookRecipient(
        display_name=None,
        smtp_address=quoted_address,
        email_address=None,
        addrtype="SMTP",
        recipient_type=3,
    )

    result = normalize_outlook_recipients([repeated, repeated])

    assert [recipient["email"] for recipient in result.recipients] == [
        quoted_address,
        quoted_address,
    ]
    assert [recipient["recipient_type"] for recipient in result.recipients] == [
        "bcc",
        "bcc",
    ]


def test_each_unresolved_recipient_has_one_explicit_reason() -> None:
    result = normalize_outlook_recipients(
        [
            RawOutlookRecipient("No address", None, None, "SMTP", 1),
            RawOutlookRecipient("No type", "a@example.com", None, None, 1),
            RawOutlookRecipient("Exchange", None, "/O=ORG/CN=USER", "EX", 2),
            RawOutlookRecipient("Unknown role", "b@example.com", None, "SMTP", 9),
        ]
    )

    assert result.recipients == ()
    assert result.unresolved_by_reason == {
        "missing_address": 1,
        "missing_addrtype": 1,
        "non_smtp_addrtype": 1,
        "unknown_recipient_type": 1,
    }


class FakeOutlookReader:
    def __init__(self, messages: list[RawOutlookMessage]) -> None:
        self._messages = messages

    def iter_messages(self, source: PstSource):
        del source
        yield from self._messages


def test_parser_recovers_smtp_address_when_display_value_has_only_a_name(
    tmp_path: Path,
) -> None:
    source = PstSource(
        path=tmp_path / "archive.pst",
        input_mode="file",
        requested_from=tmp_path / "archive.pst",
    )
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Name-only display"},
                folder_path="Sent Items",
                logical_locator="entry-name-only",
                display_recipients={"to": "王小明", "cc": "", "bcc": ""},
                recipients=(
                    RawOutlookRecipient(
                        "王小明", "wang.xiaoming@example.com", None, "SMTP", 1
                    ),
                ),
            )
        ]
    )

    result = next(iter(OutlookPstParser(reader=reader).iter_extraction_results(source)))

    assert result.message is not None
    assert result.message.raw_message["to"] == [
        {"name": "王小明", "email": "wang.xiaoming@example.com"}
    ]


def test_parser_uses_eight_table_recipients_not_seven_display_entries(
    tmp_path: Path,
) -> None:
    source = PstSource(
        path=tmp_path / "archive.pst",
        input_mode="file",
        requested_from=tmp_path / "archive.pst",
    )
    table_values = [
        ("carol", "carol@org-a.example"),
        ("Dave Example", "dave.example@org-b.example"),
        ("ERIN", "erin@org-a.example"),
        ("乙小姐", "yi@org-c.example"),
        ("frank", "frank@org-a.example"),
        ("grace@org-a.example", "grace@org-a.example"),
        ("heidi@org-d.example", "heidi@org-d.example"),
        ("Dana Roe", "dana.roe@org-b.example"),
    ]
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "RE: Order+Delivery"},
                folder_path="Sent Items",
                logical_locator="entry-0001",
                display_recipients={
                    "to": "carol; Dave Example; ERIN; 乙小姐; frank; "
                    "grace@org-a.example; heidi@org-d.example",
                    "cc": "",
                    "bcc": "",
                },
                recipients=tuple(
                    RawOutlookRecipient(name, address, None, "SMTP", 1)
                    for name, address in table_values
                ),
            )
        ]
    )

    parser = OutlookPstParser(reader=reader)
    results = list(parser.iter_extraction_results(source))

    assert len(results) == 1
    extracted = results[0].message
    assert extracted is not None
    assert extracted.folder_path == "Sent Items"
    assert extracted.logical_locator == "entry-0001"
    assert extracted.raw_message["to"] == [
        {"name": name, "email": address} for name, address in table_values
    ]
    assert len(extracted.raw_message["to"]) == 8
    assert extracted.raw_message["cc"] == []
    assert extracted.raw_message["bcc"] == []
    assert parser.summary is not None
    assert parser.summary.messages_seen == 1
    assert parser.summary.messages_emitted == 1
    assert parser.summary.recipients_seen == 8
    assert parser.summary.recipients_emitted == 8
    assert parser.summary.recipients_missing_from_display_count == 1
    parser.summary.validate()


def test_balanced_source_account_contains_every_required_counter() -> None:
    account = PstExtractionAccount(
        source_path="archive.pst",
        messages_seen=2,
        messages_emitted=1,
        parse_failures=1,
        recipients_seen=5,
        recipients_emitted=3,
        unresolved_by_reason={
            "missing_address": 1,
            "missing_addrtype": 0,
            "non_smtp_addrtype": 1,
            "unknown_recipient_type": 0,
        },
        completed=True,
    )

    account.validate()

    assert account.message_reconciliation_difference == 0
    assert account.recipient_reconciliation_difference == 0


def test_source_account_rejects_missing_message() -> None:
    account = PstExtractionAccount(
        source_path="archive.pst",
        messages_seen=2,
        messages_emitted=1,
        parse_failures=0,
        completed=True,
    )

    with pytest.raises(RuntimeError, match="message reconciliation difference: 1"):
        account.validate()


def test_source_account_rejects_missing_recipient() -> None:
    account = PstExtractionAccount(
        source_path="archive.pst",
        recipients_seen=2,
        recipients_emitted=1,
        completed=True,
    )

    with pytest.raises(RuntimeError, match="recipient reconciliation difference: 1"):
        account.validate()


def test_source_account_rejects_unknown_or_missing_unresolved_categories() -> None:
    unknown = PstExtractionAccount(
        source_path="archive.pst",
        unresolved_by_reason={
            "missing_address": 0,
            "missing_addrtype": 0,
            "non_smtp_addrtype": 0,
            "unknown_recipient_type": 0,
            "catch_all": 0,
        },
        completed=True,
    )
    missing = PstExtractionAccount(
        source_path="archive.pst",
        unresolved_by_reason={
            "missing_address": 0,
            "missing_addrtype": 0,
            "non_smtp_addrtype": 0,
        },
        completed=True,
    )

    with pytest.raises(RuntimeError, match="unresolved reason keys"):
        unknown.validate()
    with pytest.raises(RuntimeError, match="unresolved reason keys"):
        missing.validate()


class FakeStores:
    def __init__(self, *paths: str) -> None:
        self._stores = [SimpleNamespace(FilePath=path) for path in paths]
        self.Count = len(self._stores)

    def Item(self, index: int):
        return self._stores[index - 1]


class FakeCollection:
    def __init__(self, *values) -> None:
        self._values = values
        self.Count = len(values)

    def Item(self, index: int):
        return self._values[index - 1]


class FakeAccessor:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._values = values or {}

    def GetProperty(self, name: str):
        value = self._values[name]
        if isinstance(value, Exception):
            raise value
        return value


class FakeStore:
    def __init__(self, path: Path, root_folder) -> None:
        self.FilePath = str(path)
        self._root_folder = root_folder

    def GetRootFolder(self):
        return self._root_folder


class FakeFolder:
    def __init__(self, name: str, *, items=(), folders=()) -> None:
        self.Name = name
        self.Items = FakeCollection(*items)
        self.Folders = FakeCollection(*folders)


class FakeNamespace:
    def __init__(self, *stores) -> None:
        self.Stores = FakeCollection(*stores)


def test_outlook_reader_resolves_exact_mounted_store_case_insensitively(
    tmp_path: Path,
) -> None:
    pst_path = tmp_path / "Archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    stores = FakeStores(str(tmp_path / "other.pst"), str(pst_path).upper())
    reader = OutlookComReader(namespace=SimpleNamespace(Stores=stores))

    store = reader.resolve_store(source)

    assert store is stores.Item(2)


def test_outlook_reader_rejects_unmounted_source(tmp_path: Path) -> None:
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    reader = OutlookComReader(
        namespace=SimpleNamespace(Stores=FakeStores(str(tmp_path / "other.pst")))
    )

    with pytest.raises(RuntimeError, match=r"already mounted.*archive\.pst"):
        reader.resolve_store(source)


def test_outlook_reader_rejects_multiple_exact_store_matches(tmp_path: Path) -> None:
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    reader = OutlookComReader(
        namespace=SimpleNamespace(Stores=FakeStores(str(pst_path), str(pst_path)))
    )

    with pytest.raises(RuntimeError, match=r"Multiple Outlook stores.*archive\.pst"):
        reader.resolve_store(source)


def test_outlook_adapter_source_contains_no_mutating_com_calls() -> None:
    import mailattic.outlook_pst as outlook_pst

    source = inspect.getsource(outlook_pst)

    for method_name in (
        "Save",
        "Send",
        "Display",
        "Delete",
        "Move",
        "AddStore",
        "RemoveStore",
    ):
        assert f".{method_name}(" not in source


def test_outlook_reader_extracts_core_fields_recipients_and_folder_provenance(
    tmp_path: Path,
) -> None:
    from mailattic.outlook_pst import (
        PR_INTERNET_MESSAGE_ID,
        PR_INTERNET_REFERENCES,
    )

    recipient = SimpleNamespace(
        Name="乙小姐",
        Type=2,
        PropertyAccessor=FakeAccessor(
            {
                PR_SMTP_ADDRESS: "yi@org-c.example",
                PR_EMAIL_ADDRESS: "legacy@example.com",
                PR_ADDRTYPE: "SMTP",
            }
        ),
    )
    sent_at = datetime(2026, 1, 21, 14, 48, tzinfo=UTC)
    item = SimpleNamespace(
        Class=43,
        EntryID="entry-0001",
        Subject="RE: Order+Delivery",
        SenderName="Sean Example",
        SenderEmailType="SMTP",
        SenderEmailAddress="sean@example.com",
        SentOn=sent_at,
        ReceivedTime=sent_at,
        Body="Evidence body",
        To="Sean",
        CC="乙小姐",
        BCC="",
        Recipients=FakeCollection(recipient),
        Attachments=FakeCollection(),
        PropertyAccessor=FakeAccessor(
            {
                PR_INTERNET_MESSAGE_ID: "<msg0001@example.com>",
                PR_INTERNET_REFERENCES: "<root@example.com>",
            }
        ),
    )
    sent_folder = FakeFolder("Sent Items", items=(item,))
    root = FakeFolder("Archive", folders=(sent_folder,))
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(namespace=FakeNamespace(FakeStore(pst_path, root)))
    source = PstSource(pst_path, "file", pst_path)

    results = list(reader.iter_messages(source))

    assert len(results) == 1
    message = results[0]
    assert isinstance(message, RawOutlookMessage)
    assert message.folder_path == "Sent Items"
    assert message.logical_locator == "entry-0001"
    assert message.raw_message["subject"] == "RE: Order+Delivery"
    assert message.raw_message["sender"] == {
        "name": "Sean Example",
        "email": "sean@example.com",
    }
    # COM hands back Taipei wall-clock numbers under a UTC label, so the reader
    # relabels rather than passes through: same numbers, real zone. This
    # assertion used to expect `sent_at` verbatim, which enshrined the 8-hour
    # error (issue #183).
    assert message.raw_message["sent_at"] == datetime(
        2026, 1, 21, 14, 48, tzinfo=TAIPEI
    )
    assert message.raw_message["body_text"] == "Evidence body"
    assert message.raw_message["message_id"] == "<msg0001@example.com>"
    assert message.raw_message["references"] == "<root@example.com>"
    assert message.raw_message["raw_headers"] is None
    assert message.recipients == (
        RawOutlookRecipient(
            "乙小姐",
            "yi@org-c.example",
            "legacy@example.com",
            "SMTP",
            2,
        ),
    )


class _FakeComDatetime(datetime):
    """Stands in for `pywintypes.datetime`: a datetime subclass, like the real
    COM value. `.replace()` preserves the subclass, a component rebuild does
    not -- which is what the type assertion below distinguishes."""


def test_com_timestamps_keep_the_instant_not_the_wrong_utc_label(tmp_path) -> None:
    # Synthetic values modelled on Outlook COM behaviour: a message whose true
    # send instant is 06:42:33Z comes back from `SentOn` as 14:42:33 tagged
    # UTC -- Taipei wall-clock numbers under the wrong label. Asserting the
    # resulting UTC instant (not the tzinfo) is what makes this test fail if
    # the relabel is removed: a pass-through leaves 14:42:33Z.
    com_value = _FakeComDatetime(2025, 3, 26, 14, 42, 33, tzinfo=UTC)
    item = SimpleNamespace(
        Class=43,
        EntryID="entry-tz",
        Subject="RE: Ask for desk lamps",
        SenderName="Sender",
        SenderEmailType="SMTP",
        SenderEmailAddress="sender@example.com",
        SentOn=com_value,
        ReceivedTime=com_value,
        Body="body",
        To="",
        CC="",
        BCC="",
        Recipients=FakeCollection(),
        Attachments=FakeCollection(),
        PropertyAccessor=FakeAccessor({}),
    )
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(
        namespace=FakeNamespace(
            FakeStore(pst_path, FakeFolder("Archive", items=(item,)))
        )
    )

    message = next(iter(reader.iter_messages(PstSource(pst_path, "file", pst_path))))

    assert isinstance(message, RawOutlookMessage)
    true_instant = datetime(2025, 3, 26, 6, 42, 33, tzinfo=UTC)
    assert message.raw_message["sent_at"].astimezone(UTC) == true_instant
    assert message.raw_message["received_at"].astimezone(UTC) == true_instant
    # The COM subclass must not survive the boundary: it reaches sqlite3 bind
    # intact and only fails there ("type 'pywintypes.datetime' is not
    # supported"). Switching the rebuild to `.replace()` fails this line.
    assert type(message.raw_message["sent_at"]) is datetime


class RecipientAccessFailureItem:
    Class = 43
    EntryID = "entry-broken-recipients"

    @property
    def Recipients(self):
        raise RuntimeError("recipient table unavailable")


def test_outlook_reader_reports_recipient_table_failure_without_display_fallback(
    tmp_path: Path,
) -> None:
    item = RecipientAccessFailureItem()
    item.To = "Plausible <plausible@example.com>"
    root = FakeFolder("Archive", items=(item,))
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(namespace=FakeNamespace(FakeStore(pst_path, root)))

    results = list(reader.iter_messages(PstSource(pst_path, "file", pst_path)))

    assert len(results) == 1
    failure = results[0]
    assert isinstance(failure, RawOutlookFailure)
    assert failure.error_stage == "read-recipient-table"
    assert "recipient table unavailable" in failure.error_message


class SubjectAccessFailureItem:
    """A COM read failure on core message evidence, not an optional field.

    Adjudication: "Bare except blocks report success on corrupted rows" --
    `_safe_attr` used to swallow this and let the message persist with a
    `None` subject while still counting toward `messages_emitted`.
    """

    Class = 43
    EntryID = "entry-broken-subject"
    Recipients = FakeCollection()

    @property
    def Subject(self):
        raise RuntimeError("subject unavailable")


def test_outlook_reader_reports_message_property_failure_not_silent_none(
    tmp_path: Path,
) -> None:
    item = SubjectAccessFailureItem()
    root = FakeFolder("Archive", items=(item,))
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(namespace=FakeNamespace(FakeStore(pst_path, root)))

    results = list(reader.iter_messages(PstSource(pst_path, "file", pst_path)))

    assert len(results) == 1
    failure = results[0]
    assert isinstance(failure, RawOutlookFailure)
    assert failure.error_stage == "read-message-properties"
    assert "subject unavailable" in failure.error_message


def test_parser_does_not_emit_a_message_when_property_read_fails(
    tmp_path: Path,
) -> None:
    """The account, not just the reader, must reflect the failure: a
    property-read failure counts as a parse failure, never as an emitted,
    persistable message."""
    item = SubjectAccessFailureItem()
    root = FakeFolder("Archive", items=(item,))
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(namespace=FakeNamespace(FakeStore(pst_path, root)))
    source = PstSource(pst_path, "file", pst_path)

    parser = OutlookPstParser(reader=reader)
    results = list(parser.iter_extraction_results(source))

    assert len(results) == 1
    assert results[0].message is None
    assert results[0].failure is not None
    assert parser.summary is not None
    assert parser.summary.messages_seen == 1
    assert parser.summary.messages_emitted == 0
    assert parser.summary.parse_failures == 1
    parser.summary.validate()


def test_outlook_reader_reports_unsupported_item_class(tmp_path: Path) -> None:
    item = SimpleNamespace(Class=26, EntryID="entry-meeting")
    root = FakeFolder("Archive", items=(item,))
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(namespace=FakeNamespace(FakeStore(pst_path, root)))

    results = list(reader.iter_messages(PstSource(pst_path, "file", pst_path)))

    assert len(results) == 1
    failure = results[0]
    assert isinstance(failure, RawOutlookFailure)
    assert failure.error_stage == "unsupported-item-class"


def test_outlook_reader_isolates_attachment_binary_failure(tmp_path: Path) -> None:
    from mailattic.outlook_pst import (
        PR_ATTACH_DATA_BIN,
        PR_ATTACH_MIME_TAG,
    )

    attachment = SimpleNamespace(
        FileName="evidence.pdf",
        PropertyAccessor=FakeAccessor(
            {
                PR_ATTACH_DATA_BIN: RuntimeError("binary unavailable"),
                PR_ATTACH_MIME_TAG: "application/pdf",
            }
        ),
    )
    item = SimpleNamespace(
        Class=43,
        EntryID="entry-attachment",
        Subject="Attachment",
        SenderName="Sender",
        SenderEmailType="SMTP",
        SenderEmailAddress="sender@example.com",
        SentOn=None,
        ReceivedTime=None,
        Body="Body",
        To="Recipient",
        CC="",
        BCC="",
        Recipients=FakeCollection(
            SimpleNamespace(
                Name="Recipient",
                Type=1,
                PropertyAccessor=FakeAccessor(
                    {
                        PR_SMTP_ADDRESS: "recipient@example.com",
                        PR_EMAIL_ADDRESS: "recipient@example.com",
                        PR_ADDRTYPE: "SMTP",
                    }
                ),
            )
        ),
        Attachments=FakeCollection(attachment),
        PropertyAccessor=FakeAccessor(),
    )
    root = FakeFolder("Archive", items=(item,))
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    reader = OutlookComReader(
        namespace=FakeNamespace(FakeStore(pst_path, root)),
        attachment_root=tmp_path / "attachments",
    )

    results = list(reader.iter_messages(PstSource(pst_path, "file", pst_path)))

    message = results[0]
    assert isinstance(message, RawOutlookMessage)
    assert message.raw_message["attachments"] == [
        {
            "attachment_name": "evidence.pdf",
            "content_hash": None,
            "size_bytes": None,
            "mime_type": None,
            "storage_path": None,
            "_decode_error": True,
        }
    ]


def _install_fake_win32com(monkeypatch, get_active_object) -> list[str]:
    """Fake win32com.client; returns the list Dispatch appends to when called."""
    dispatch_calls: list[str] = []

    def dispatch(prog_id):
        dispatch_calls.append(prog_id)
        return SimpleNamespace(GetNamespace=lambda _: "dispatched-namespace")

    client = SimpleNamespace(Dispatch=dispatch, GetActiveObject=get_active_object)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return dispatch_calls


def test_load_outlook_namespace_refuses_when_outlook_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywintypes

    def not_running(prog_id):
        # What GetActiveObject actually raises with no instance in the ROT
        # (MK_E_UNAVAILABLE) -- the guard must catch that and nothing wider.
        del prog_id
        raise pywintypes.com_error(-2147221021, "Operation unavailable", None, None)

    dispatch_calls = _install_fake_win32com(monkeypatch, not_running)

    with pytest.raises(RuntimeError, match="Outlook is not running"):
        _load_outlook_namespace()

    # Dispatch would have started Outlook behind the caller's back (#181).
    assert dispatch_calls == []


def test_load_outlook_namespace_does_not_relabel_other_com_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywintypes

    def bad_prog_id(prog_id):
        del prog_id
        # CO_E_CLASSSTRING: what an unregistered ProgID returns (Outlook not
        # installed at all), measured alongside MK_E_UNAVAILABLE.
        raise pywintypes.com_error(-2147221005, "Invalid class string", None, None)

    _install_fake_win32com(monkeypatch, bad_prog_id)

    with pytest.raises(pywintypes.com_error) as caught:
        _load_outlook_namespace()
    assert caught.value.hresult == -2147221005


def test_load_outlook_namespace_does_not_relabel_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_pywin32(prog_id):
        del prog_id
        raise AttributeError("GetActiveObject missing from this pywin32 build")

    _install_fake_win32com(monkeypatch, broken_pywin32)

    # An adapter/pywin32 defect must surface as itself, not as "Outlook is not
    # running" -- that relabelling is what a bare `except Exception` would do.
    with pytest.raises(AttributeError):
        _load_outlook_namespace()


def test_load_outlook_namespace_uses_the_already_running_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = SimpleNamespace(GetNamespace=lambda moniker: f"live:{moniker}")
    dispatch_calls = _install_fake_win32com(monkeypatch, lambda _: application)

    assert _load_outlook_namespace() == "live:MAPI"
    assert dispatch_calls == []
