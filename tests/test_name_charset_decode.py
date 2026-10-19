"""Regression tests for #287: mislabelled-charset names (display names, filenames).

#202 repaired the Subject only. Sender / recipient display names and attachment
filenames of the same mail went through the stdlib's strict decode and were
stored as mojibake. Chinese Outlook labels cp950 text ``big5`` and GBK text
``gb2312``; names must decode exactly like the subject of the same mail.
All names below are invented.
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import quote

import pytest

from mailattic.parser import _parse_eml_file

U_FFFD = "�"

# (label, text) -- text only decodes under the label's practical superset
# (恒 is cp950-only, 镕 is GBK-only).
_CP950_AS_BIG5 = ("big5", "恒甲商行")
_GBK_AS_GB2312 = ("gb2312", "镕乙商行")
_MISLABELLED = [_CP950_AS_BIG5, _GBK_AS_GB2312]


def _b(label: str, text: str, codec: str | None = None) -> str:
    raw = text.encode(codec or ("cp950" if label == "big5" else "gbk"))
    return f"=?{label}?B?{base64.b64encode(raw).decode()}?="


def _mail(
    *,
    from_h: str = "sender@example.com",
    to_h: str = "recipient@example.com",
    cc_h: str | None = None,
    subject_h: str = "hello",
    part_headers: str = 'Content-Disposition: attachment; filename="a.pdf"\r\n',
) -> bytes:
    cc = f"Cc: {cc_h}\r\n" if cc_h else ""
    return (
        f"From: {from_h}\r\nTo: {to_h}\r\n{cc}Subject: {subject_h}\r\n"
        "Message-ID: <n@example.com>\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
        f"--B\r\nContent-Type: application/pdf\r\n{part_headers}"
        "Content-Transfer-Encoding: base64\r\n\r\nAAAA\r\n--B--\r\n"
    ).encode("ascii")


def _parse(tmp_path: Path, raw: bytes) -> dict[str, object]:
    path = tmp_path / "message.eml"
    path.write_bytes(raw)
    return _parse_eml_file(path)


def _only_attachment_name(parsed: dict[str, object]) -> str:
    return str(parsed["attachment_names"][0])  # type: ignore[index]


# ---------------------------------------------------------------------------
# Mislabelled charsets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "text"), _MISLABELLED)
def test_subject_sender_and_filename_decode_identically(
    tmp_path: Path, label: str, text: str
) -> None:
    word = _b(label, text)
    parsed = _parse(
        tmp_path,
        _mail(
            from_h=f"{word} <sender@example.com>",
            subject_h=word,
            part_headers=f'Content-Disposition: attachment; filename="{word}.pdf"\r\n',
        ),
    )

    assert parsed["subject"] == text
    assert parsed["sender"] == {"name": text, "email": "sender@example.com"}  # type: ignore[comparison-overlap]
    assert _only_attachment_name(parsed) == f"{text}.pdf"


@pytest.mark.parametrize(("label", "text"), _MISLABELLED)
def test_recipient_display_names_to_and_cc_decode(
    tmp_path: Path, label: str, text: str
) -> None:
    word = _b(label, text)
    parsed = _parse(
        tmp_path,
        _mail(
            to_h=f"{word} <to@example.com>, plain@example.com",
            cc_h=f"{word} <cc@example.com>",
        ),
    )

    assert parsed["to"] == [
        {"name": text, "email": "to@example.com"},
        {"name": None, "email": "plain@example.com"},
    ]
    assert parsed["cc"] == [{"name": text, "email": "cc@example.com"}]


@pytest.mark.parametrize(("label", "text"), _MISLABELLED)
def test_unquoted_rfc2047_filename_decodes(
    tmp_path: Path, label: str, text: str
) -> None:
    parsed = _parse(
        tmp_path,
        _mail(
            part_headers=(
                f"Content-Disposition: attachment; filename={_b(label, text)}\r\n"
            )
        ),
    )

    assert _only_attachment_name(parsed) == text


@pytest.mark.parametrize(("label", "text"), _MISLABELLED)
def test_rfc2231_filename_decodes(tmp_path: Path, label: str, text: str) -> None:
    codec = "cp950" if label == "big5" else "gbk"
    pct = quote(text.encode(codec))
    parsed = _parse(
        tmp_path,
        _mail(
            part_headers=(
                f"Content-Disposition: attachment; filename*={label}''{pct}.pdf\r\n"
            )
        ),
    )

    assert _only_attachment_name(parsed) == f"{text}.pdf"


@pytest.mark.parametrize(("label", "text"), _MISLABELLED)
def test_rfc2231_continuation_filename_decodes(
    tmp_path: Path, label: str, text: str
) -> None:
    codec = "cp950" if label == "big5" else "gbk"
    raw = text.encode(codec)
    first, second = quote(raw[:3]), quote(raw[3:])
    parsed = _parse(
        tmp_path,
        _mail(
            part_headers=(
                "Content-Disposition: attachment;\r\n"
                f" filename*0*={label}''{first};\r\n"
                f" filename*1*={second}\r\n"
            )
        ),
    )

    assert _only_attachment_name(parsed) == text


@pytest.mark.parametrize(("label", "text"), _MISLABELLED)
def test_name_param_only_filename_decodes(
    tmp_path: Path, label: str, text: str
) -> None:
    # #285 shape: no Content-Disposition, only Content-Type name=.
    parsed = _parse(
        tmp_path,
        _mail(
            part_headers=f'Content-Type: application/pdf; name="{_b(label, text)}"\r\n'
        ),
    )

    assert _only_attachment_name(parsed) == text


def test_mislabelled_name_search_text_has_no_replacement_character(
    tmp_path: Path,
) -> None:
    word = _b(*_GBK_AS_GB2312)
    parsed = _parse(
        tmp_path,
        _mail(
            from_h=f"{word} <sender@example.com>",
            part_headers=f'Content-Disposition: attachment; filename="{word}.pdf"\r\n',
        ),
    )

    assert U_FFFD not in repr(parsed["sender"])
    assert U_FFFD not in _only_attachment_name(parsed)


# ---------------------------------------------------------------------------
# Correctly handled names must not change
# ---------------------------------------------------------------------------


def test_correctly_labelled_names_decode_as_before(tmp_path: Path) -> None:
    utf8 = _b("utf-8", "報價單", "utf-8")
    big5 = _b("big5", "海灣", "big5")
    gb = _b("gb2312", "报价", "gb2312")
    parsed = _parse(
        tmp_path,
        _mail(
            from_h=f"{utf8} <a@example.com>",
            to_h=f"{big5} <b@example.com>, {gb} <c@example.com>",
            part_headers=f'Content-Disposition: attachment; filename="{big5}.pdf"\r\n',
        ),
    )

    assert parsed["sender"]["name"] == "報價單"  # type: ignore[index]
    assert [a["name"] for a in parsed["to"]] == ["海灣", "报价"]  # type: ignore[attr-defined]
    assert _only_attachment_name(parsed) == "海灣.pdf"


def test_ascii_and_quoted_comma_names_decode_as_before(tmp_path: Path) -> None:
    parsed = _parse(
        tmp_path,
        _mail(
            from_h="Alice Example <alice@example.com>",
            to_h='"Example, Bob" <bob@example.com>, carol@example.com',
            part_headers='Content-Disposition: attachment; filename="report 1.pdf"\r\n',
        ),
    )

    assert parsed["sender"] == {"name": "Alice Example", "email": "alice@example.com"}  # type: ignore[comparison-overlap]
    assert parsed["to"] == [
        {"name": "Example, Bob", "email": "bob@example.com"},
        {"name": None, "email": "carol@example.com"},
    ]
    assert _only_attachment_name(parsed) == "report 1.pdf"


def test_encoded_word_followed_by_plain_text_keeps_both(tmp_path: Path) -> None:
    word = _b(*_CP950_AS_BIG5)
    parsed = _parse(
        tmp_path,
        _mail(from_h=f"{word} Stationery <sender@example.com>"),
    )

    assert parsed["sender"]["name"] == f"{_CP950_AS_BIG5[1]} Stationery"  # type: ignore[index]


def test_quoted_comma_name_mixed_with_mislabelled_name(tmp_path: Path) -> None:
    word = _b(*_GBK_AS_GB2312)
    parsed = _parse(
        tmp_path,
        _mail(to_h=f'"Example, Bob" <bob@example.com>, {word} <kj@example.com>'),
    )

    assert parsed["to"] == [
        {"name": "Example, Bob", "email": "bob@example.com"},
        {"name": _GBK_AS_GB2312[1], "email": "kj@example.com"},
    ]


def test_raw_8bit_utf8_filename_decodes_as_before(tmp_path: Path) -> None:
    # Unencoded UTF-8 bytes in the header: not RFC-clean, but common, and the
    # stdlib reads it correctly. The mislabel repair must not take it over.
    raw = _mail().replace(b'filename="a.pdf"', 'filename="報價單.pdf"'.encode())
    assert _only_attachment_name(_parse(tmp_path, raw)) == "報價單.pdf"


def test_same_address_twice_under_different_names_is_left_alone(
    tmp_path: Path,
) -> None:
    # The repair matches by address; when one address carries two different
    # names it must not hand the first name to both entries.
    first = _b(*_CP950_AS_BIG5)
    second = _b(*_GBK_AS_GB2312)
    parsed = _parse(
        tmp_path,
        _mail(to_h=f"{first} <dup@example.com>, {second} <dup@example.com>"),
    )
    names = [entry["name"] for entry in parsed["to"]]  # type: ignore[union-attr]
    assert len(names) == 2
    assert names[0] != names[1]
