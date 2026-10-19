"""Regression tests for #202: mislabelled-charset header and body decoding.

Two real-world shapes from the issue:
  1. encoded-word declared ``gb2312`` whose payload is actually GBK -- a strict gb2312
     decode fails on the GBK-extension bytes and the subject came out as
     replacement characters;
  2. a big5 subject stored with surrogate escapes of UTF-8 bytes -- a header
     string that still carries undecoded raw bytes.

The label-to-decoder mapping follows the WHATWG Encoding Standard, which
decodes the whole GB family through GB 18030 and ``iso-8859-1`` as
``windows-1252`` -- the same thing mail clients do.
"""

from __future__ import annotations

import base64
from html import escape as html_escape
from pathlib import Path

import pytest

from mailattic.outbound import (
    build_quoted_reply_bodies,
    build_thread_headers,
)
from mailattic.parser import (
    _detect_and_decode,
    _extract_raw_headers,
    _parse_eml_file,
    decode_header_text,
    subject_from_raw_headers,
)

U_FFFD = "�"

# Stored form of a gb2312-labelled GBK subject: strict gb2312 decode with replacement.
_MOJIBAKE_SUBJECT = (
    "RE: " + "海灣甲嶽".encode("gbk").decode("gb2312", "replace") + " stale"
)

# Synthetic header; the CJK name has a GBK-extension char absent from gb2312.
_GB2312_AS_GBK_SUBJECT = (
    b"Subject: "
    b"=?gb2312?B?UkU6ILqjnrO8145bIFBBUEVSLUE0LTgwIE9SREVS?=\r\n"
    b"\t=?gb2312?B?LTAwMDEgJiBERUxJVkVSWSAoSU5WLTAwMDEp?=\r\n"
)
_GB2312_AS_GBK_EXPECTED = "RE: 海灣甲嶽 PAPER-A4-80 ORDER-0001 & DELIVERY (INV-0001)"

# Synthetic header split into a B word and a Q word.
_BIG5_SPLIT_SUBJECT = (
    b"Subject:\r\n"
    b" =?big5?B?UkU6IK78xlel0sCuICBPUkRFUiAgTk8gMDAwMSAmIDAw?=\r\n"
    b" =?big5?Q?02_?=\r\n"
)
_BIG5_SPLIT_EXPECTED = "RE: 海灣甲嶽  ORDER  NO 0001 & 0002"


def _eml(subject_header: bytes, *, body: bytes = b"body\r\n") -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Message-ID: <orig@example.com>\r\n" + subject_header + b"\r\n" + body
    )


def _parse(tmp_path: Path, raw: bytes) -> dict[str, object]:
    path = tmp_path / "message.eml"
    path.write_bytes(raw)
    return _parse_eml_file(path)


# ---------------------------------------------------------------------------
# Subject at ingest
# ---------------------------------------------------------------------------


def test_gb2312_declared_gbk_payload_subject_decodes(tmp_path: Path) -> None:
    parsed = _parse(tmp_path, _eml(_GB2312_AS_GBK_SUBJECT))

    assert str(parsed["subject"]).strip() == _GB2312_AS_GBK_EXPECTED


def test_big5_split_b_and_q_words_subject_decodes(tmp_path: Path) -> None:
    parsed = _parse(tmp_path, _eml(_BIG5_SPLIT_SUBJECT))

    assert str(parsed["subject"]).strip() == _BIG5_SPLIT_EXPECTED


def test_multibyte_char_split_across_encoded_words_is_rejoined(
    tmp_path: Path,
) -> None:
    # Outlook splits long subjects on byte boundaries, not character
    # boundaries: each word alone is an invalid GBK sequence.
    payload = "海灣甲嶽".encode("gbk")
    first = base64.b64encode(payload[:3]).decode()
    second = base64.b64encode(payload[3:]).decode()
    header = f"Subject: =?gb2312?B?{first}?=\r\n =?gb2312?B?{second}?=\r\n".encode()

    parsed = _parse(tmp_path, _eml(header))

    assert parsed["subject"] == "海灣甲嶽"


def test_raw_eight_bit_utf8_subject_decodes(tmp_path: Path) -> None:
    parsed = _parse(tmp_path, _eml("Subject: RE: 海灣甲嶽\r\n".encode()))

    assert parsed["subject"] == "RE: 海灣甲嶽"


def test_plain_ascii_subject_unchanged(tmp_path: Path) -> None:
    parsed = _parse(tmp_path, _eml(b"Subject: Re:  two  spaces kept\r\n"))

    assert parsed["subject"] == "Re:  two  spaces kept"


def test_mixed_literal_and_encoded_word_keeps_separating_space(
    tmp_path: Path,
) -> None:
    word = base64.b64encode("報價".encode()).decode()
    header = f"Subject: FW: =?utf-8?B?{word}?= PO 123\r\n".encode()

    parsed = _parse(tmp_path, _eml(header))

    assert parsed["subject"] == "FW: 報價 PO 123"


def test_unknown_charset_encoded_word_does_not_raise(tmp_path: Path) -> None:
    word = base64.b64encode("報價".encode()).decode()
    header = f"Subject: =?x-no-such-charset?B?{word}?=\r\n".encode()

    parsed = _parse(tmp_path, _eml(header))

    assert parsed["subject"] == "報價"


def test_malformed_base64_word_is_kept_literally(tmp_path: Path) -> None:
    parsed = _parse(tmp_path, _eml(b"Subject: =?utf-8?B?***?= tail\r\n"))

    assert parsed["subject"] == "=?utf-8?B?***?= tail"


def test_missing_subject_is_none(tmp_path: Path) -> None:
    raw = b"From: a@example.com\r\nMessage-ID: <x@example.com>\r\n\r\nbody\r\n"

    assert _parse(tmp_path, raw)["subject"] is None


def test_iso_8859_1_label_decodes_as_windows_1252() -> None:
    # 0x93 / 0x94 are C1 controls in ISO-8859-1 but curly quotes in cp1252;
    # Windows senders routinely label cp1252 text as iso-8859-1.
    word = base64.b64encode(b"\x93quoted\x94").decode()

    assert decode_header_text(f"=?iso-8859-1?B?{word}?=") == "“quoted”"


# ---------------------------------------------------------------------------
# Header strings that still carry undecoded bytes (the surrogate-escape shape)
# ---------------------------------------------------------------------------


def test_surrogate_escaped_utf8_is_recovered() -> None:
    # What `search_messages` returns for the header above.
    broken = (
        "RE: \udce6\udcb5\udcb7\udce7\udc81\udca3甲\udce5\udcb6\udcbd"
        "  ORDER  NO 0001 & 0002"
    )

    assert decode_header_text(broken) == _BIG5_SPLIT_EXPECTED


def test_decoded_header_never_contains_lone_surrogates() -> None:
    # Undecodable raw bytes become U+FFFD rather than surrogates that break
    # UTF-8 encoding downstream (SQLite, JSON, SMTP).
    result = decode_header_text("abc \udcff\udcfe def")

    assert not any(0xDC80 <= ord(char) <= 0xDCFF for char in result)
    assert result.startswith("abc ")
    assert result.endswith(" def")


# ---------------------------------------------------------------------------
# Body charset
# ---------------------------------------------------------------------------


def test_body_declared_gb2312_actual_gbk_decodes() -> None:
    raw = "RE: 海灣甲嶽 PAPER-A4-80".encode("gbk")

    result = _detect_and_decode(raw, "gb2312")

    assert result == "RE: 海灣甲嶽 PAPER-A4-80"


def test_body_declared_big5_with_cp950_extension_decodes() -> None:
    # 0xA3E1 (euro sign) exists in cp950 -- what Windows actually emits for
    # "big5" -- but not in strict Big5.
    raw = "報價 ".encode("big5") + b"\xa3\xe1" + " 100".encode("big5")

    result = _detect_and_decode(raw, "big5")

    assert result == "報價 € 100"


def test_body_correct_gb2312_unchanged() -> None:
    raw = "中文测试".encode("gb2312")

    assert _detect_and_decode(raw, "gb2312") == "中文测试"


# ---------------------------------------------------------------------------
# draft-reply: re-decode the subject from the stored raw headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (_GB2312_AS_GBK_SUBJECT, _GB2312_AS_GBK_EXPECTED),
        (_BIG5_SPLIT_SUBJECT, _BIG5_SPLIT_EXPECTED),
    ],
    ids=["gb2312-as-gbk", "big5-split"],
)
def test_reply_subject_comes_from_raw_headers_over_stored_subject(
    header: bytes, expected: str
) -> None:
    # Rows ingested before the fix still hold the broken subject; the draft
    # must not re-encode that and send it to the external recipient.
    original = {
        "message_id": "<orig@example.com>",
        "subject": _MOJIBAKE_SUBJECT,
        "raw_headers": _extract_raw_headers(_eml(header)),
    }

    assert build_thread_headers(original)["subject"] == f"Re: {expected}"


def test_reply_subject_falls_back_to_stored_subject_without_raw_headers() -> None:
    original = {
        "message_id": "<orig@example.com>",
        "subject": "Quote request",
        "raw_headers": None,
    }

    assert build_thread_headers(original)["subject"] == "Re: Quote request"


def test_reply_subject_falls_back_when_raw_headers_have_no_subject() -> None:
    original = {
        "message_id": "<orig@example.com>",
        "subject": "Quote request",
        "raw_headers": "From: a@example.com\r\nMessage-ID: <orig@example.com>",
    }

    assert build_thread_headers(original)["subject"] == "Re: Quote request"


def test_reply_subject_already_prefixed_is_not_doubled() -> None:
    raw_headers = _extract_raw_headers(_eml(b"Subject: Re: Quote\r\n"))
    original = {
        "message_id": "<orig@example.com>",
        "subject": "Re: Quote",
        "raw_headers": raw_headers,
    }

    assert build_thread_headers(original)["subject"] == "Re: Quote"


def test_no_replacement_characters_in_fixed_subjects(tmp_path: Path) -> None:
    for header in (_GB2312_AS_GBK_SUBJECT, _BIG5_SPLIT_SUBJECT):
        assert U_FFFD not in str(_parse(tmp_path, _eml(header))["subject"])


# ---------------------------------------------------------------------------
# Review follow-ups: never raise, never guess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["rot13", "hex", "zlib", "base64"])
def test_non_text_codec_label_does_not_fail_the_message(
    tmp_path: Path, label: str
) -> None:
    # codecs.lookup() resolves these, but bytes.decode() refuses them with
    # LookupError; a sender-controlled label must not fail ingest (contract
    # #5 would then stall the folder's watermark on this UID).
    header = f"Subject: =?{label}?Q?4142?=\r\n".encode()
    body_part = (
        f'Content-Type: text/plain; charset="{label}"\r\n'.encode()
        + b"Content-Transfer-Encoding: 8bit\r\n"
    )

    parsed = _parse(tmp_path, _eml(header + body_part, body=b"plain body\r\n"))

    assert parsed["subject"] == "4142"
    assert parsed["body_text"] == "plain body"


def test_undeclared_non_utf8_header_bytes_stay_visibly_broken(
    tmp_path: Path,
) -> None:
    # Detection on header-length input guesses confidently wrong; U+FFFD is
    # the honest answer and raw_headers keeps the bytes for a re-decode.
    parsed = _parse(tmp_path, _eml(b"Subject: Caf\xe9 menu\r\n"))

    assert parsed["subject"] == "Caf� menu"


def test_truncated_encoded_word_is_replaced_not_guessed() -> None:
    word = base64.b64encode("報價".encode("gbk")[:3]).decode()

    result = decode_header_text(f"=?gb2312?B?{word}?= end")

    assert result.startswith("报") or U_FFFD in result
    assert result.endswith(" end")
    assert U_FFFD in result


@pytest.mark.parametrize("value", ["x\ud800", "\udce9\udc41", "a\udfff\udce5b"])
def test_any_lone_surrogate_is_removed_without_raising(value: str) -> None:
    result = decode_header_text(value)

    assert not any("\ud800" <= char <= "\udfff" for char in result)


def test_subject_from_raw_headers_rejects_non_byte_image() -> None:
    # Outlook COM rebuild rows store already-decoded transport headers.
    assert subject_from_raw_headers("Subject: 海灣甲嶽\r\n") is None


@pytest.mark.parametrize(
    "header",
    [
        # the sender's encoded-word carries a leading and two trailing spaces.
        "Subject: =?big5?B?IL1Uu3utccHKIEZGTCAg?=\r\n",
        # encoded trailing newlines.
        "Subject: =?utf-8?q?Meet_maplecrest=0A=0A?=\r\n",
    ],
)
def test_subject_from_raw_headers_matches_stored_form_without_edge_whitespace(
    header: str,
) -> None:
    # Ingest strips subjects (models._coerce_text); repairs written from raw
    # headers must land in the same form (#222).
    subject = subject_from_raw_headers(header)

    assert subject is not None
    assert subject == subject.strip()


def test_subject_from_raw_headers_whitespace_only_subject_is_unusable() -> None:
    # An empty repair would overwrite a broken-but-nonempty stored subject.
    assert subject_from_raw_headers("Subject: =?utf-8?Q?___?=\r\n") is None


def test_clean_stored_subject_is_not_overridden_by_raw_headers() -> None:
    original = {
        "message_id": "<orig@example.com>",
        "subject": "Café menu",
        "raw_headers": "Subject: Caf\xe9 menu\r\nMessage-ID: <orig@example.com>",
    }

    assert build_thread_headers(original)["subject"] == "Re: Café menu"


def test_quoted_block_uses_repaired_subject() -> None:
    original = {
        "message_id": "<orig@example.com>",
        "subject": _MOJIBAKE_SUBJECT,
        "raw_headers": _extract_raw_headers(_eml(_GB2312_AS_GBK_SUBJECT)),
        "sender_email": "gloria@example.com",
        "sent_at": "2026-09-15T14:10:31+08:00",
        "body_text": "original body",
    }

    plain, html = build_quoted_reply_bodies(original, "reply")

    assert f"Subject: {_GB2312_AS_GBK_EXPECTED}" in plain
    assert html_escape(_GB2312_AS_GBK_EXPECTED) in html
    assert U_FFFD not in plain + html


def test_clean_stored_subject_wins_even_when_raw_headers_decode_cleanly() -> None:
    # Rows whose subject decoded fine keep exactly the subject users see in
    # search/show; only broken ones are repaired.
    original = {
        "message_id": "<orig@example.com>",
        "subject": "Quote v2",
        "raw_headers": "Subject: Quote\r\nMessage-ID: <orig@example.com>",
    }

    assert build_thread_headers(original)["subject"] == "Re: Quote v2"
