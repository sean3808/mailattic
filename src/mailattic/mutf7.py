"""Modified UTF-7 codec for IMAP mailbox names (RFC 3501 section 5.1.3).

Standalone: no dependency on ``imaplib``, so it is unit-testable in isolation
and reusable by the folder-metadata backfill script.

Modified UTF-7 differs from standard UTF-7 (RFC 2152) in two ways:
  - ``,`` (comma) replaces ``/`` (slash) in the modified base64 alphabet.
  - There is no ``+`` shift character used the RFC 2152 way for shifting into
    a run of encoded UTF-16BE code units. Instead ``&`` is the shift
    character. A literal ``&`` is encoded as ``&-``.

Encoding: a maximal run of non-ASCII characters (or an ampersand itself) is
UTF-16BE encoded, base64-encoded with the modified alphabet and without
padding, and wrapped in ``&...-``.

Decoding: a ``&`` introduces a shift sequence terminated by ``-``; the bytes
between are base64-decoded (modified alphabet), interpreted as UTF-16BE. A
special case is ``&-``, which decodes to a literal ``&``.

A malformed shift sequence -- unterminated, containing a character outside
the modified base64 alphabet (this includes a literal ``=`` padding
character, which Python's permissive ``base64.b64decode`` would otherwise
silently discard rather than reject), a payload that does not decode to a
whole number of UTF-16BE code units, or a payload that encodes a character
RFC 3501 requires to be sent directly (printable ASCII, 0x20-0x7E) -- is a
defensive fallback: per the Implementation Contract this decodes to the
original wire string, unchanged, rather than raising — a sync run must never
be stopped by an unparsable folder name, and a decoder that let malformed
input through as a plausible-looking wrong name would be worse than one that
refuses it, because this decoder's output is used to permanently rewrite
folder metadata in the live database (see the folder-metadata backfill
script).
"""

from __future__ import annotations

import base64
import binascii

_B64_STANDARD_TO_MODIFIED = str.maketrans("/", ",")
_B64_MODIFIED_TO_STANDARD = str.maketrans(",", "/")

# The modified base64 alphabet (RFC 3501 section 5.1.3): standard base64
# minus padding, with ',' in place of '/'. A shift-sequence payload
# containing any character outside this set -- notably a literal '='
# (padding) or any other stray character -- is malformed and must be
# rejected outright rather than passed to base64.b64decode, whose default
# validate=False would silently discard such characters instead of
# reporting the payload as invalid.
_MODIFIED_B64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+,"
)


def encode_mutf7(name: str) -> str:
    """Encode a display-form folder name to its modified UTF-7 wire form.

    ASCII characters other than ``&`` pass through unchanged. A literal ``&``
    encodes to ``&-``. Any other character starts (or continues) a shifted
    run that is UTF-16BE encoded, base64-encoded with ``,`` in place of ``/``,
    and terminated by ``-``.
    """
    result: list[str] = []
    shift_run: list[str] = []

    def _flush_shift_run() -> None:
        if not shift_run:
            return
        utf16_bytes = "".join(shift_run).encode("utf-16-be")
        b64 = base64.b64encode(utf16_bytes).decode("ascii").rstrip("=")
        result.append("&" + b64.translate(_B64_STANDARD_TO_MODIFIED) + "-")
        shift_run.clear()

    for char in name:
        if char == "&":
            _flush_shift_run()
            result.append("&-")
        elif " " <= char <= "~":
            # Printable ASCII (0x20-0x7E) other than '&' passes through as-is.
            _flush_shift_run()
            result.append(char)
        else:
            shift_run.append(char)

    _flush_shift_run()
    return "".join(result)


def decode_mutf7(name: str) -> str:
    """Decode a modified UTF-7 wire-form folder name to its display form.

    Returns the input unchanged if it contains a malformed shift sequence,
    rather than raising — see module docstring.

    Also returns the input unchanged if it decodes without raising but is not
    the *canonical* modified UTF-7 encoding of the result -- e.g. unused
    trailing bits in a shift sequence's base64 payload that are non-zero
    (``base64.b64decode(validate=True)`` accepts these; it only rejects
    out-of-alphabet characters, not non-canonical padding bits), or two
    adjacent shift runs that a canonical encoder would have merged into one.
    This is checked with a single round-trip: re-encode the decoded result
    and compare it to the original input. A non-canonical wire form is not a
    string this decoder should treat as equivalent to its canonical form --
    doing so risks the folder-metadata backfill script (which calls this
    function) permanently rewriting a row based on a decoding the server
    never actually meant.
    """
    try:
        decoded = _decode_mutf7_strict(name)
    except ValueError:
        return name
    if encode_mutf7(decoded) != name:
        return name
    return decoded


def _decode_mutf7_strict(name: str) -> str:
    result: list[str] = []
    i = 0
    length = len(name)
    while i < length:
        char = name[i]
        if char != "&":
            result.append(char)
            i += 1
            continue

        end = name.find("-", i + 1)
        if end == -1:
            raise ValueError(f"unterminated shift sequence in {name!r}")

        payload = name[i + 1 : end]
        if payload == "":
            # "&-" is the literal-ampersand escape.
            result.append("&")
            i = end + 1
            continue

        if any(c not in _MODIFIED_B64_ALPHABET for c in payload):
            raise ValueError(
                f"payload contains a character outside the modified base64 "
                f"alphabet in {name!r}"
            )

        standard_b64 = payload.translate(_B64_MODIFIED_TO_STANDARD)
        padding_needed = (-len(standard_b64)) % 4
        try:
            utf16_bytes = base64.b64decode(
                standard_b64 + "=" * padding_needed, validate=True
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"invalid base64 payload in {name!r}") from exc

        if len(utf16_bytes) % 2 != 0:
            raise ValueError(f"non-whole UTF-16 payload in {name!r}")

        try:
            decoded_run = utf16_bytes.decode("utf-16-be")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-16BE payload in {name!r}") from exc

        if any(" " <= ch <= "~" for ch in decoded_run):
            # RFC 3501 requires printable ASCII to be sent directly, never
            # inside a shift sequence -- e.g. "&AEE-" decodes to "A", but "A"
            # is not a valid encoding of itself, so this is malformed.
            raise ValueError(f"shift sequence encodes printable ASCII in {name!r}")

        result.append(decoded_run)
        i = end + 1

    return "".join(result)
