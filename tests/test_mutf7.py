"""Tests for the modified UTF-7 (RFC 3501 section 5.1.3) codec.

Written first per TDD — this module runs red until
``mailattic.mutf7`` exists and implements the codec (task 1.2).

Case source: the codec example table in
``openspec/changes/add-mutf7-folder-codec/specs/imap-sync/spec.md``.
"""

from __future__ import annotations

from mailattic.mutf7 import decode_mutf7, encode_mutf7


def test_decode_chinese_sent_folder() -> None:
    assert decode_mutf7("&W8RO9lCZTv0-") == "寄件備份"


def test_encode_chinese_sent_folder() -> None:
    assert encode_mutf7("寄件備份") == "&W8RO9lCZTv0-"


def test_decode_chinese_drafts_folder() -> None:
    assert decode_mutf7("&g0l6Pw-") == "草稿"


def test_encode_chinese_drafts_folder() -> None:
    assert encode_mutf7("草稿") == "&g0l6Pw-"


def test_inbox_is_identity_under_decode() -> None:
    assert decode_mutf7("INBOX") == "INBOX"


def test_inbox_is_identity_under_encode() -> None:
    assert encode_mutf7("INBOX") == "INBOX"


def test_literal_ampersand_encodes_to_ampersand_hyphen() -> None:
    assert encode_mutf7("R&D") == "R&-D"


def test_ampersand_hyphen_decodes_to_literal_ampersand() -> None:
    assert decode_mutf7("R&-D") == "R&D"


def test_malformed_shift_sequence_decodes_to_itself_without_raising() -> None:
    # "&AB" is not a well-formed modified UTF-7 shift sequence (no terminating
    # '-' and not a multiple-of-4 base64 payload). The decoder must return it
    # unchanged rather than raise.
    assert decode_mutf7("&AB") == "&AB"


def test_payload_with_character_outside_modified_alphabet_decodes_to_itself() -> None:
    # "!" is not in the modified base64 alphabet. Python's permissive
    # base64.b64decode(validate=False) would otherwise silently discard it
    # and decode this into a plausible-looking wrong name; the decoder must
    # reject it and return the wire string unchanged instead.
    assert decode_mutf7("&W8R!O9lCZTv0-") == "&W8R!O9lCZTv0-"


def test_payload_with_literal_padding_character_decodes_to_itself() -> None:
    # A literal '=' inside the payload is not part of the modified base64
    # alphabet (the codec never emits '=' -- padding is computed, not
    # transmitted). This must be rejected, not silently stripped.
    assert decode_mutf7("&W8RO9lCZTv0=-") == "&W8RO9lCZTv0=-"


def test_shift_sequence_encoding_printable_ascii_decodes_to_itself() -> None:
    # "&AEE-" decodes to "A" (U+0041), but printable ASCII (0x20-0x7E) must be
    # sent directly per RFC 3501, never inside a shift sequence. This is a
    # malformed encoding of "A", not a valid one, so it must not decode.
    assert decode_mutf7("&AEE-") == "&AEE-"


def test_non_canonical_trailing_bits_decode_to_wire_string_unchanged() -> None:
    # "&W8RO9lCZTv0-" is the canonical encoding of 寄件備份. Flipping the last
    # base64 character to '1' still passes base64.b64decode(validate=True)
    # (it only rejects out-of-alphabet characters, not non-zero unused
    # padding bits) and decodes to the SAME UTF-16BE bytes as the canonical
    # form, because those trailing bits are discarded rather than used. Since
    # re-encoding the decoded result reproduces "&W8RO9lCZTv0-", not this
    # input, this input is not canonical and must pass through unchanged.
    non_canonical = "&W8RO9lCZTv1-"
    assert decode_mutf7(non_canonical) == non_canonical


def test_adjacent_shift_runs_that_a_canonical_encoder_would_merge_pass_through() -> (
    None
):
    # Two adjacent single-character shift runs, each individually well-formed
    # in isolation, whose decoded characters a canonical encoder would have
    # merged into ONE shift run rather than two separate ones. This is
    # non-canonical, so the input passes through unchanged rather than being
    # decoded as if it were the canonical merged form.
    run_a = encode_mutf7("寄")
    run_b = encode_mutf7("件")
    two_runs = run_a + run_b
    merged = encode_mutf7("寄件")
    assert merged != two_runs  # sanity: genuinely a different wire form
    assert decode_mutf7(two_runs) == two_runs
