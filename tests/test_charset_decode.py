"""tests/test_charset_decode.py — Regression tests for CJK charset-mismatch decoding.

Covers Acceptance criteria (a)–(l) from:
  openspec/changes/fix-cjk-charset-decode/design.md  (lines 72-84)
  openspec/changes/fix-cjk-charset-decode/specs/message-ingestion/spec.md

Two entry points under test:
  1. _detect_and_decode(raw, declared_charset)  — unit-level (situations a–g, k, l)
  2. _extract_body_text(message)                — end-to-end via BytesParser (h, i, j)
"""

from __future__ import annotations

import base64
import quopri
from email import policy
from email.parser import BytesParser

import pytest

from mailattic.parser import _detect_and_decode, _extract_body_text

# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------

U_FFFD = "�"

# Spec-pinned string (spec.md line 14–16, design.md acceptance criterion (a)):
# bytes = '有收到貴公司採購單'.encode('big5')
_SPEC_A_STR = "有收到貴公司採購單"
_SPEC_A_BYTES = _SPEC_A_STR.encode("big5")

# Long Big5 string for (b) — empirically long enough for charset_normalizer to
# confidently identify as big5 rather than cp949/utf-16 etc.
# Verified stable: produces 'big5' from charset_normalizer.from_bytes().best()
_B_STR = "貴公司您好，謝謝您的詢價，附件為本次報價單，請查收。"
_B_BYTES = _B_STR.encode("big5")


def _build_message(raw_mime_bytes: bytes):
    """Parse raw MIME bytes into an email.message.Message (policy=default)."""
    return BytesParser(policy=policy.default).parsebytes(raw_mime_bytes)


def _mime_message(
    body_bytes: bytes,
    *,
    subtype: str = "plain",
    declared_charset: str = "utf-8",
    transfer_encoding: str = "8bit",
) -> bytes:
    """Construct a minimal text/<subtype> MIME message with an arbitrary byte body.

    The declared_charset in the Content-Type header is set independently of how
    body_bytes was actually encoded — this lets us simulate the mismatch cases.
    """
    headers = (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: test\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/"
        + subtype.encode()
        + b'; charset="'
        + declared_charset.encode()
        + b'"\r\n'
        b"Content-Transfer-Encoding: " + transfer_encoding.encode() + b"\r\n"
        b"\r\n"
    )
    return headers + body_bytes


# ---------------------------------------------------------------------------
# (a) declared utf-8, actual Big5 — strict-fail path → detection → CJK restore
#     Spec pin: bytes = '有收到貴公司採購單'.encode('big5')
#               stored body == '有收到貴公司採購單' and zero U+FFFD
# ---------------------------------------------------------------------------


def test_a_declared_utf8_actual_big5_restores_cjk():
    """(a) Declared utf-8 but bytes are Big5 → restore CJK, zero U+FFFD."""
    result = _detect_and_decode(_SPEC_A_BYTES, "utf-8")
    assert result == _SPEC_A_STR, f"Expected '{_SPEC_A_STR}', got '{result}'"
    assert U_FFFD not in result


# ---------------------------------------------------------------------------
# (b) declared iso-8859-1, actual Big5 — permissive path → CJK-specific detection
#     Use a long Big5 string so charset_normalizer detects confidently.
# ---------------------------------------------------------------------------


def test_b_declared_latin1_actual_big5_restores_cjk():
    """(b) Declared iso-8859-1 but bytes are Big5 → detection yields CJK → restore."""
    result = _detect_and_decode(_B_BYTES, "iso-8859-1")
    assert result == _B_STR, f"Expected '{_B_STR}', got '{result}'"
    assert U_FFFD not in result


# ---------------------------------------------------------------------------
# (c) Correctly declared utf-8 CJK — strict-success path, no change
# ---------------------------------------------------------------------------


def test_c_correctly_declared_utf8_cjk_unchanged():
    """(c) Correctly declared utf-8 CJK → identical to original string."""
    original = "這是一封正常的繁體中文郵件，宣告 charset 正確。"
    raw = original.encode("utf-8")
    result = _detect_and_decode(raw, "utf-8")
    assert result == original


# ---------------------------------------------------------------------------
# (d) Pure ASCII — strict-success path, unchanged
# ---------------------------------------------------------------------------


def test_d_ascii_unchanged():
    """(d) Pure ASCII text → unchanged regardless of declared charset."""
    original = "Hello world! This is plain ASCII."
    raw = original.encode("ascii")
    result = _detect_and_decode(raw, "us-ascii")
    assert result == original
    # Also works with utf-8 declaration (backward compat):
    result_utf8 = _detect_and_decode(raw, "utf-8")
    assert result_utf8 == original


# ---------------------------------------------------------------------------
# (e) Undetectable garbage bytes — fallback replace, no exception raised
# ---------------------------------------------------------------------------


def test_e_undetectable_bytes_does_not_raise():
    """(e) Garbage bytes that fail strict + detection → replace fallback, no raise."""
    garbage = b"\x80\x81\x82\xff"
    # Should not raise; result must be a str (possibly with U+FFFD)
    result = _detect_and_decode(garbage, "utf-8")
    assert isinstance(result, str)
    # Fallback replace means U+FFFD will appear — that is the expected behavior.
    # The important contract is: no exception, ingest continues.


def test_e_batch_with_bad_and_good_both_ingest(tmp_path):
    """(e) A batch of two messages: one bad bytes, one valid ASCII — both ingest.

    Validates the 'one bad message does not abort the batch' contract
    via end-to-end _extract_body_text.
    """
    # Bad message: random non-decodable bytes declared as utf-8
    bad_mime = _mime_message(b"\x80\x81\x82\xff", declared_charset="utf-8")
    good_mime = _mime_message(b"Hello ASCII world", declared_charset="us-ascii")

    bad_msg = _build_message(bad_mime)
    good_msg = _build_message(good_mime)

    # Neither should raise
    bad_body = _extract_body_text(bad_msg)
    good_body = _extract_body_text(good_msg)

    assert isinstance(bad_body, str)
    assert good_body == "Hello ASCII world"


# ---------------------------------------------------------------------------
# (f) True latin-1 Western-European guard — permissive path, NOT overwritten
#     Detection result is NOT CJK-specific → preserve declared latin-1 decode.
# ---------------------------------------------------------------------------


def test_f_genuine_latin1_preserved():
    """(f) Genuine latin-1 Western European text → not overwritten to CJK/UTF-8."""
    western = "café señor über naïve"
    raw = western.encode("latin-1")
    result = _detect_and_decode(raw, "iso-8859-1")
    # The decoded string must equal the original latin-1 decoded text.
    assert result == western, f"Expected '{western}', got '{result}'"
    assert U_FFFD not in result


# ---------------------------------------------------------------------------
# (g) Cross-CJK dual-valid known limitation — declared gbk, actual Big5
#     Strict decode under gbk succeeds (bytes are valid gbk too) → NO detection
#     override; retain declared-gbk decoding.  This is a KNOWN LIMITATION.
# ---------------------------------------------------------------------------


def test_g_cross_cjk_dual_valid_known_limitation():
    """(g) KNOWN LIMITATION: declared gbk, actual Big5, strict-gbk succeeds.

    When the byte sequence is valid under both big5 and gbk, the strict-path
    decode under the declared charset (gbk) succeeds.  The implementation
    intentionally retains this result without detection override, because
    there is no reliable signal (no U+FFFD, no strict failure).

    This test asserts the _known_ behavior:
      - Result equals raw.decode('gbk')  (mojibake but not an exception)
      - No exception is raised
      - Ingestion continues (non-fatal)

    Root-cause recovery of cross-CJK dual-valid mislabels is tracked as a
    follow-up (coherence-gate approach); it is out of scope for this change.

    Fixture note: '謝謝採購報價'.encode('big5') produces bytes that are also a
    valid GBK sequence.  Verified: raw.decode('gbk') succeeds and returns mojibake
    ('谅谅蹦潦厨基').  The spec-pinned string '有收到貴公司採購單' cannot be used
    here because its Big5 bytes contain 0xa8 which is illegal in GBK — it is NOT
    a dual-valid sequence.
    """
    # '謝謝採購報價' — bytes valid under both Big5 and GBK (empirically verified).
    dual_valid_big5_str = "謝謝採購報價"
    raw = dual_valid_big5_str.encode("big5")
    # Confirm the precondition: GBK strict decode must succeed on this raw.
    expected_gbk_decode = raw.decode("gbk")  # strict gbk → mojibake ('谅谅蹦潦厨基')

    result = _detect_and_decode(raw, "gbk")
    assert result == expected_gbk_decode, (
        f"Expected gbk-decoded '{expected_gbk_decode}', got '{result}'"
    )
    # Must not have raised (if it did, the test itself would have failed above).


# ---------------------------------------------------------------------------
# (h) text/html part declared utf-8, actual Big5 — end-to-end with tag stripping
# ---------------------------------------------------------------------------


def test_h_html_part_declared_utf8_actual_big5_strip_tags():
    """(h) text/html, declared utf-8, bytes are Big5 → tag-stripped CJK, zero U+FFFD."""
    cjk_html_content = "有收到貴公司採購單，謝謝。"
    html_body_bytes = (
        "<html><body><p>" + cjk_html_content + "</p></body></html>"
    ).encode("big5")

    raw_mime = _mime_message(html_body_bytes, subtype="html", declared_charset="utf-8")
    msg = _build_message(raw_mime)
    result = _extract_body_text(msg)

    # After tag stripping, CJK text must be present and no U+FFFD
    assert cjk_html_content in result, (
        f"Expected CJK text '{cjk_html_content}' in result, got: '{result}'"
    )
    assert U_FFFD not in result


# ---------------------------------------------------------------------------
# (i) multipart: one correct utf-8 part + one mislabeled Big5 part
# ---------------------------------------------------------------------------


def test_i_multipart_each_part_decoded_correctly():
    """(i) multipart: utf-8 part + Big5 mislabeled part → both decoded correctly."""
    utf8_text = "This is a correctly encoded UTF-8 plain text part."
    big5_text = "貴公司您好，謝謝您的詢價，附件為本次報價單，請查收。"

    # Build a multipart/mixed message manually
    boundary = b"boundary_test_xyz"
    utf8_bytes = utf8_text.encode("utf-8")
    big5_bytes = big5_text.encode("big5")

    raw_mime = (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: multipart test\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n'
        b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + utf8_bytes + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + big5_bytes + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )

    msg = _build_message(raw_mime)
    result = _extract_body_text(msg)

    # Both parts should appear in the combined output
    assert utf8_text in result, (
        f"Expected UTF-8 part '{utf8_text}' in result, got: '{result}'"
    )
    assert big5_text in result, (
        f"Expected Big5 part '{big5_text}' in result, got: '{result}'"
    )
    assert U_FFFD not in result


# ---------------------------------------------------------------------------
# (j) transfer-encoding: base64 and quoted-printable with mislabeled charset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transfer_encoding,encode_fn",
    [
        ("base64", lambda b: base64.encodebytes(b).replace(b"\n", b"\r\n")),
        ("quoted-printable", lambda b: quopri.encodestring(b, quotetabs=False)),
    ],
)
def test_j_transfer_encoding_cjk_mislabeled(transfer_encoding, encode_fn):
    """(j) base64 / QP CJK content with mislabeled charset.

    Transfer-decode then charset-decode.
    """
    cjk_text = "貴公司您好，謝謝您的詢價，附件為本次報價單，請查收。"
    raw_body = cjk_text.encode("big5")
    encoded_body = encode_fn(raw_body)

    raw_mime = _mime_message(
        encoded_body,
        declared_charset="utf-8",
        transfer_encoding=transfer_encoding,
    )
    msg = _build_message(raw_mime)
    result = _extract_body_text(msg)

    assert cjk_text in result, (
        f"[{transfer_encoding}] Expected CJK '{cjk_text}' in result, got: '{result}'"
    )
    assert U_FFFD not in result


# ---------------------------------------------------------------------------
# (k) empty body part → returns "", no exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"", None])
def test_k_empty_inputs_return_empty_string(raw):
    """(k) Empty bytes or None → _detect_and_decode returns '' without raising."""
    assert _detect_and_decode(raw, "utf-8") == ""


def test_k_empty_body_end_to_end():
    """(k) MIME message with empty body part → _extract_body_text returns '' safely."""
    raw_mime = _mime_message(b"", declared_charset="utf-8")
    msg = _build_message(raw_mime)
    result = _extract_body_text(msg)
    assert isinstance(result, str)
    assert result == ""


# ---------------------------------------------------------------------------
# (l) Unknown declared codec + undetectable bytes
#     → no LookupError, UTF-8 replace fallback
# ---------------------------------------------------------------------------


def test_l_unknown_declared_codec_does_not_raise():
    """(l) charset="x-unknown-foo" + undetectable bytes → no LookupError, str returned.

    Contract: unknown codec name must not raise LookupError; ingest must not abort.
    The implementation maps unknown codec → declared_norm=None → detection path →
    fallback (utf-8 replace) when detection returns None.

    Fixture note: b'\\xfe\\xff\\x00' is empirically verified to produce
    charset_normalizer.best() == None (detection not confident), so the
    implementation reaches the utf-8-replace fallback branch.
    """
    # \xfe\xff\x00 — charset_normalizer.best() returns None for this sequence
    # (verified: from_bytes produces no confident result with excluded UTF-16 codecs).
    undetectable = b"\xfe\xff\x00"
    result = _detect_and_decode(undetectable, "x-unknown-foo")
    # Must not raise LookupError or any other exception.
    assert isinstance(result, str)
    # Unknown codec → declared_norm=None → detection fails → utf-8 replace fallback.
    assert result == undetectable.decode("utf-8", errors="replace")


def test_l_unknown_codec_ingest_continues():
    """(l) End-to-end: MIME with unknown charset declaration.

    _extract_body_text returns str.
    """
    raw_mime = _mime_message(b"\x80\x81\x82\xff", declared_charset="x-unknown-foo")
    msg = _build_message(raw_mime)
    result = _extract_body_text(msg)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# (m) Permissive-declared (cp1252) body with an undefined cp1252 byte
#     → preserve path must not raise (cp1252 is NOT a full 256-byte superset)
# ---------------------------------------------------------------------------


def test_m_cp1252_undefined_byte_non_fatal():
    """(m) Declared cp1252 with an undefined byte (0x81) → no raise, U+FFFD substituted.

    cp1252 leaves 0x81/0x8d/0x8f/0x90/0x9d undefined; a strict decode on the
    permissive preserve path would raise UnicodeDecodeError and abort ingest,
    violating the contract that decoding never raises. The preserve path uses
    errors="replace", so the undefined byte becomes U+FFFD and ingest continues.
    """
    # 0x81 is undefined in cp1252; surrounding bytes are valid Western text.
    raw = b"Preis 100 \x81 EUR"
    result = _detect_and_decode(raw, "cp1252")
    assert isinstance(result, str)
    assert result == raw.decode("cp1252", errors="replace")
    # The valid Western portions survive; only the undefined byte is replaced.
    assert "Preis 100 " in result
    assert "EUR" in result
    # Ingest must not abort; result is a str (possibly with replacement chars)
