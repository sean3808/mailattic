from __future__ import annotations

import base64
import binascii
import codecs
import mailbox
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator
from email import policy
from email.errors import InvalidHeaderDefect
from email.parser import BytesParser, HeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import charset_normalizer

from . import attachment_store
from .models import (
    ExtractedMessage,
    ExtractionResult,
    IngestFailure,
    MaildirSource,
    PstSource,
)

# CJK-specific encodings that are permitted as detection-override targets.
# Comparison must use codecs.lookup(name).name for normalisation (alias-safe).
# utf-8 and utf-16 are intentionally excluded.
_CJK_SPECIFIC_CODECS: frozenset[str] = frozenset(
    codecs.lookup(n).name
    for n in (
        "big5",
        "cp950",
        "big5hkscs",
        "gbk",
        "gb2312",
        "gb18030",
        "shift_jis",
        "cp932",
        "euc-jp",
        "euc-kr",
    )
)

# Single-byte permissive encodings: these decode (almost) any byte without a
# strict-fail signal, so a successful decode tells us nothing about correctness.
# latin-1/iso-8859-1 map all 256 bytes; cp1252 leaves a few undefined slots
# (handled with errors="replace" on the preserve path below).
# us-ascii is NOT in this set — it raises on bytes >= 0x80 (strict-fail path).
_PERMISSIVE_SUPERSET_CODECS: frozenset[str] = frozenset(
    codecs.lookup(n).name for n in ("iso-8859-1", "latin-1", "cp1252")
)

# charset_normalizer false-positively detects short, BOM-less CJK byte streams
# as UTF-16/UTF-32. Genuine UTF-16/32 bodies are declared (handled by the strict
# path) or carry a BOM; excluding them from detection guards CJK recovery.
_DETECTION_EXCLUDED_CODECS: frozenset[str] = frozenset(
    {
        "utf_16",
        "utf_16_be",
        "utf_16_le",
        "utf_32",
        "utf_32_be",
        "utf_32_le",
    }
)


# What a declared label is actually decoded as, in order. Mail clients (and
# the WHATWG Encoding Standard browsers follow) never trust these labels
# literally: Chinese Outlook labels GBK text "gb2312", Windows labels cp950
# "big5" and cp1252 "iso-8859-1". Each list starts with -- or is a strict
# superset of -- the declared codec, so correctly labelled text decodes
# exactly as before; only text a strict decode would reject gets rescued.
_DECLARED_CHARSET_DECODERS: dict[str, tuple[str, ...]] = {
    "gb2312": ("gb2312", "gb18030"),
    "gbk": ("gbk", "gb18030"),
    "big5": ("big5", "cp950", "big5hkscs"),
    # cp1252 first: latin-1 never fails, so it could not come second. The two
    # agree on every byte outside 0x80-0x9F, where latin-1 has only C1
    # controls; latin-1 stays as the fallback for cp1252's undefined slots.
    "iso8859-1": ("cp1252", "iso8859-1"),
}


def _text_codec_name(label: str) -> str | None:
    """Normalise a charset label; ``None`` for unknown or non-text codecs.

    ``codecs.lookup`` also resolves bytes-to-bytes codecs (``rot13``, ``hex``,
    ``zlib`` ...) that ``bytes.decode`` then refuses with ``LookupError``; a
    sender-controlled label must not be able to fail the whole message.
    """
    try:
        info = codecs.lookup(label)
    except LookupError:
        return None
    if not getattr(info, "_is_text_encoding", True):
        return None
    return info.name


def _decode_as_declared(raw: bytes, declared_norm: str) -> str | None:
    """Strictly decode *raw* as what *declared_norm* means in practice.

    Returns ``None`` when no decoder for the label accepts the bytes.
    """
    for codec in _DECLARED_CHARSET_DECODERS.get(declared_norm, (declared_norm,)):
        try:
            return raw.decode(codec, errors="strict")
        except UnicodeDecodeError:
            continue
    return None


def _detect_best(raw: bytes) -> Any:
    """Run charset detection, excluding UTF-16/32 false positives."""
    return charset_normalizer.from_bytes(
        raw, cp_exclusion=list(_DETECTION_EXCLUDED_CODECS)
    ).best()


def _detect_and_decode(raw: bytes | None, declared_charset: str | None) -> str:
    """Decode *raw* bytes to str using a layered charset strategy.

    Strategy (mirrors design.md Decision 2):
    1. If declared charset is a strict codec (raises on bad bytes), try strict
       decode first.  Success → return.  UnicodeDecodeError → detect.
       LookupError (unknown codec name) → treat as no declaration, detect.
    2. If declared charset is a permissive superset (iso-8859-1 / latin-1 /
       cp1252) AND raw contains non-ASCII bytes → detect; adopt only when
       confident AND result is CJK-specific (guards genuine Western text).
    3. No declared charset → detect directly; adopt when confident.
    5. Detection: charset_normalizer.from_bytes(raw).best().
    6. Fallback (never raises): validated codec replace-decode, or utf-8 replace.
    """
    if not raw:
        return ""

    # --- normalise declared charset ----------------------------------------
    # Unknown or non-text codec → treated as absent.
    declared_norm = _text_codec_name(declared_charset) if declared_charset else None

    # --- step 2 check: is declared a permissive superset? -------------------
    is_permissive = (
        declared_norm is not None and declared_norm in _PERMISSIVE_SUPERSET_CODECS
    )

    # has_non_ascii is only consulted on the permissive path; short-circuit keeps
    # the strict / no-declaration fast paths from scanning the whole body.
    if is_permissive and any(b >= 0x80 for b in raw):
        # Permissive path: only override when detection yields CJK-specific.
        result = _detect_best(raw)
        if result is not None:
            detected_norm: str | None
            try:
                detected_norm = codecs.lookup(result.encoding).name
            except LookupError:
                detected_norm = None
            if detected_norm is not None and detected_norm in _CJK_SPECIFIC_CODECS:
                return str(result)
        # Detection not confident or not CJK → preserve declared decode.
        # errors="replace" (not strict): cp1252 has a few undefined byte slots
        # (0x81/0x8d/0x8f/0x90/0x9d) that raise under strict; replace keeps this
        # path non-raising per the contract. Genuine latin-1/cp1252 Western text
        # has no undefined bytes, so output is identical to a strict decode.
        decoded = _decode_as_declared(raw, declared_norm)
        if decoded is not None:
            return decoded
        return raw.decode(declared_norm, errors="replace")

    elif declared_norm is not None:
        # Step 1: strict codec declared (or permissive + all-ASCII, where any
        # codec yields identical output) — try strict decode first, through
        # the label's practical supersets (gb2312 → gb18030, ...).
        decoded = _decode_as_declared(raw, declared_norm)
        if decoded is not None:
            return decoded
        # Strict failed → fall through to detection (step 5).
        # (LookupError already handled above; declared_norm is None in that case)

    # Step 3 / 5: no usable declaration or strict failed → detect.
    result = _detect_best(raw)
    if result is not None:
        return str(result)

    # Step 6: fallback — must not raise.
    if declared_norm is not None:
        return raw.decode(declared_norm, errors="replace")
    return raw.decode("utf-8", errors="replace")


# RFC 2047 encoded-word; same shape as the stdlib's ``email.header.ecre``.
_ENCODED_WORD = re.compile(r"=\?([^?]*?)\?([qQbB])\?(.*?)\?=")
_FOLD = re.compile(r"\r?\n(?=[ \t])")
_SURROGATE = re.compile("[\ud800-\udfff]")
_NON_BYTE_SURROGATE = re.compile("[\ud800-\udc7f\udd00-\udfff]")


class _FieldTolerantPolicy(policy.EmailPolicy):
    """``policy.default``, but one undecodable header no longer fails the mail (#288).

    A sender-controlled RFC 2231 parameter (``filename*=`` / ``name*=``) that
    declares a stateful charset (iso-2022-jp, utf-16, utf-7 ...) with bytes
    invalid in it makes the stdlib header parser raise ``UnicodeDecodeError``
    -- from ``get_content_type()`` inside the parse itself, so no call site
    can guard it. Only that field is degraded: it is handed back the way
    ``compat32`` does, as a plain string whose parameters the legacy decoder
    reads with replacement characters. Headers that parse today never reach
    the fallback.
    """

    def header_fetch_parse(self, name: str, value: str) -> Any:
        try:
            return super().header_fetch_parse(name, value)
        except UnicodeError:
            # UnicodeDecodeError: bytes invalid in the declared charset.
            # UnicodeEncodeError: the declared charset (utf-7) decoded to a
            # lone surrogate and the stdlib's own sanitiser choked on it --
            # in a parameter or in an encoded-word of any header (From, To).
            return policy.compat32.header_fetch_parse(name, value)


_MAIL_POLICY = _FieldTolerantPolicy()


def _without_lone_surrogates(text: str) -> str:
    """Body counterpart of the header guarantee in ``decode_header_text``.

    A strict decode of a sender-declared codec (``utf-7``, ``unicode-escape``)
    can yield lone surrogates, which SQLite cannot encode and which would fail
    the whole message at persist time. Text without any is returned unchanged.
    """
    return _SURROGATE.sub("�", text) if _SURROGATE.search(text) else text


def _decode_undeclared(raw: bytes) -> str:
    """Decode header bytes that carry no usable charset label. Never raises.

    UTF-8 or visible U+FFFD, deliberately without charset detection: on
    header-length input detection is confidently wrong (``Caf\\xe9`` comes
    back as Arabic), and plausible-looking wrong text is worse than visibly
    broken text -- it hides from audits and goes out in reply subjects. The
    original bytes stay in ``raw_headers`` for a later re-decode.
    """
    return raw.decode("utf-8", errors="replace")


def _decode_word_run(payload: bytes, charset: str) -> str:
    """Decode the concatenated payload of adjacent same-charset words."""
    declared_norm = _text_codec_name(charset)
    if declared_norm is None:
        return _decode_undeclared(payload)
    decoded = _decode_as_declared(payload, declared_norm)
    if decoded is None:
        # Truncated or corrupt: replace within the widest decoder for the label.
        widest = _DECLARED_CHARSET_DECODERS.get(declared_norm, (declared_norm,))[-1]
        decoded = payload.decode(widest, errors="replace")
    # A strict utf-7 decode can itself yield a lone surrogate (#288).
    return _without_lone_surrogates(decoded)


def _encoded_word_payload(encoding: str, text: str) -> bytes | None:
    try:
        data = text.encode("ascii")
        if encoding in "bB":
            return base64.b64decode(data + b"=" * (-len(data) % 4), validate=True)
        return binascii.a2b_qp(data, header=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return None


def decode_header_text(value: str) -> str:
    """Decode an unstructured header value (e.g. Subject) to display text.

    *value* is the header as the email package stores it: raw bytes decoded
    ASCII with ``surrogateescape``. Those raw bytes are recovered first, so
    8-bit header text (often UTF-8) and surrogates left behind by an earlier
    decode are decoded instead of kept. RFC 2047 encoded-words are then
    decoded with the same label handling as bodies (gb2312 → gb18030, ...);
    adjacent words of one charset are joined before decoding because senders
    split them mid-character. The result never contains lone surrogates, and
    this never raises.
    """
    text = _FOLD.sub("", value)
    if _SURROGATE.search(text):
        # Escaped raw bytes (U+DC80-U+DCFF) are recovered; any other lone
        # surrogate cannot stand for a byte and becomes U+FFFD.
        text = _NON_BYTE_SURROGATE.sub("\ufffd", text)
        text = _decode_undeclared(text.encode("utf-8", errors="surrogateescape"))

    pieces: list[str] = []
    run_charset: str | None = None
    run_payload = b""
    position = 0

    for match in _ENCODED_WORD.finditer(text):
        between = text[position : match.start()]
        position = match.end()
        charset = match.group(1).split("*", 1)[0]  # drop RFC 2231 language
        payload = _encoded_word_payload(match.group(2), match.group(3))
        # RFC 2047 §6.2: whitespace between adjacent encoded-words is not
        # part of the text.
        adjacent = run_charset is not None and not between.strip()
        if payload is not None and adjacent and charset.casefold() == run_charset:
            run_payload += payload
            continue
        if run_charset is not None:
            pieces.append(_decode_word_run(run_payload, run_charset))
            run_charset, run_payload = None, b""
        if payload is None:
            pieces.append(between + match.group(0))
            continue
        if not adjacent:
            pieces.append(between)
        run_charset, run_payload = charset.casefold(), payload
    if run_charset is not None:
        pieces.append(_decode_word_run(run_payload, run_charset))
    pieces.append(text[position:])
    return "".join(pieces)


def _subject_from_message(message: Any) -> str | None:
    """Decode the first Subject header from its raw stored value."""
    for name, value in message.raw_items():
        if name.casefold() == "subject":
            return decode_header_text(str(value))
    return None


def subject_from_raw_headers(raw_headers: str) -> str | None:
    """Decode the Subject from a stored ``raw_headers`` value.

    ``raw_headers`` is the Latin-1 image of the original header bytes (see
    ``_extract_raw_headers``), so the original bytes are recoverable and the
    subject can be re-decoded without re-ingesting. The result is stripped like
    the stored subject (ingest strips in ``models._coerce_text``), so it can be
    compared with or written over a stored value as is. ``None`` when there is
    no Subject header, it is blank, or the value is unusable.
    """
    try:
        raw_bytes = raw_headers.encode("latin-1")
        message = BytesParser(policy=policy.compat32).parsebytes(
            raw_bytes + b"\r\n\r\n", headersonly=True
        )
        subject = _subject_from_message(message)
    except Exception:
        return None
    return (subject or "").strip() or None


class ParserUnavailableError(RuntimeError):
    """Raised when no PST parser backend is available."""


class ReadpstParser:
    def __init__(
        self,
        *,
        readpst_path: str | Path | None = None,
        work_root: str | Path | None = None,
        parallel_jobs: int | None = None,
        attachment_root: Path | None = None,
    ) -> None:
        if parallel_jobs is not None and parallel_jobs < 1:
            raise ValueError(
                f"parallel_jobs must be a positive integer, got {parallel_jobs!r}"
            )
        self._readpst_path = resolve_readpst_path(readpst_path)
        self._work_root = Path(work_root).resolve() if work_root is not None else None
        self._parallel_jobs = parallel_jobs
        self._attachment_root = attachment_root

    def iter_extraction_results(self, source: PstSource) -> Iterable[ExtractionResult]:
        with tempfile.TemporaryDirectory(dir=self._work_root) as temp_dir_name:
            output_root = Path(temp_dir_name) / "out"
            output_root.mkdir(parents=True, exist_ok=True)
            self._extract_pst(source.path, output_root)

            eml_paths = sorted(output_root.rglob("*.eml"))
            for eml_path in eml_paths:
                yield _parse_extraction_file(
                    source=source,
                    output_root=output_root,
                    message_path=eml_path,
                    error_stage="parse-eml",
                    parse_message=partial(
                        _parse_eml_file,
                        eml_path,
                        attachment_root=self._attachment_root,
                    ),
                )

            for mbox_path in sorted(output_root.rglob("mbox")):
                mbox = mailbox.mbox(mbox_path, create=False)
                try:
                    for index, key in enumerate(mbox.iterkeys(), start=1):
                        raw_bytes = mbox.get_file(key, from_=False).read()
                        message = BytesParser(policy=_MAIL_POLICY).parsebytes(raw_bytes)
                        yield _parse_extraction_file(
                            source=source,
                            output_root=output_root,
                            message_path=mbox_path,
                            error_stage="parse-mbox",
                            logical_locator_suffix=f"#{index}",
                            parse_message=partial(
                                _parse_email_message,
                                message,
                                raw_bytes=raw_bytes,
                                attachment_root=self._attachment_root,
                            ),
                        )
                finally:
                    mbox.close()

    def _extract_pst(self, source_path: Path, output_root: Path) -> None:
        command = self._build_readpst_command(source_path, output_root)
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"readpst failed for {source_path}: "
                f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
            )

    def _build_readpst_command(self, source_path: Path, output_root: Path) -> list[str]:
        command = [str(self._readpst_path), "-r", "-q"]
        if self._parallel_jobs is not None:
            command += ["-j", str(self._parallel_jobs)]
        command += ["-o", str(output_root), str(source_path)]
        return command


#: The maildir subdirectories a message can live in. Exported because callers
#: outside this module gate on "would the parser read anything here?" -- a
#: second copy of this tuple answers that question about a layout the parser
#: may no longer have.
MAILDIR_BUCKETS = ("cur", "new", "tmp")


def iter_maildir_message_paths(source_path: Path) -> Iterable[Path]:
    """Every file the maildir reader will actually open under *source_path*.

    The single answer to "which files does this reader see?" -- callers that
    need to reconcile a directory against what was imported use this rather
    than restating the traversal, because a second statement of it is a second
    thing that can disagree, and the disagreement shows up as mail that
    vanishes without a row, a failure or a non-zero exit.
    """
    for bucket_name in MAILDIR_BUCKETS:
        bucket_root = source_path / bucket_name
        # `is_dir`, not `exists`. No test can tell these apart: a plain file
        # named `cur` passes `exists`, and `rglob` over it yields nothing
        # rather than raising, so both spellings produce the same messages.
        # It is written this way so the traversal does not rest on pathlib
        # swallowing NotADirectoryError for us -- not as a guard, and not
        # something to "verify" by mutation.
        if not bucket_root.is_dir():
            continue

        yield from sorted(path for path in bucket_root.rglob("*") if path.is_file())


def _parse_maildir_message(
    *,
    source: MaildirSource,
    message_path: Path | str,
    attachment_root: Path | None = None,
    resolve_paths: bool = True,
) -> ExtractionResult:
    # The directory walk passes resolve_paths=False: it records the path it
    # walked, so a symlink keeps its own locator and one pointing outside the
    # root cannot abort the batch in relative_to.
    if resolve_paths:
        source_root = source.path.expanduser().resolve()
        resolved_message_path = Path(message_path).expanduser().resolve()
    else:
        source_root = source.path
        resolved_message_path = Path(message_path)
    relative_path = resolved_message_path.relative_to(source_root)
    logical_locator = f"{source.folder_name}/{relative_path.as_posix()}"
    try:
        if attachment_root is None:
            raw_message = _parse_eml_file(resolved_message_path)
        else:
            raw_message = _parse_eml_file(
                resolved_message_path, attachment_root=attachment_root
            )
        return ExtractionResult(
            message=ExtractedMessage(
                raw_message=raw_message,
                folder_path=source.folder_name,
                logical_locator=logical_locator,
            )
        )
    except Exception as exc:
        return ExtractionResult(
            failure=IngestFailure.from_source(
                source,
                folder_path=source.folder_name,
                logical_locator=logical_locator,
                error_stage="parse-maildir",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        )


class MaildirParser:
    def iter_extraction_results(
        self, source: MaildirSource, *, attachment_root: Path | None = None
    ) -> Iterable[ExtractionResult]:
        for message_path in iter_maildir_message_paths(source.path):
            yield _parse_maildir_message(
                source=source,
                message_path=message_path,
                attachment_root=attachment_root,
                resolve_paths=False,
            )


def _parse_extraction_file(
    *,
    source: PstSource,
    output_root: Path,
    message_path: Path,
    error_stage: str,
    parse_message: Callable[[], dict[str, object]],
    logical_locator_suffix: str = "",
) -> ExtractionResult:
    relative_path = message_path.relative_to(output_root)
    if relative_path.parent == Path("."):
        folder_path = "/"
    else:
        folder_path = relative_path.parent.as_posix()
    logical_locator = f"{relative_path.as_posix()}{logical_locator_suffix}"
    try:
        raw_message = parse_message()
        return ExtractionResult(
            message=ExtractedMessage(
                raw_message=raw_message,
                folder_path=folder_path,
                logical_locator=logical_locator,
            )
        )
    except Exception as exc:
        return ExtractionResult(
            failure=IngestFailure.from_source(
                source,
                folder_path=folder_path or None,
                logical_locator=logical_locator,
                error_stage=error_stage,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        )


_HEADER_SEPARATOR_RE = re.compile(rb"\r\n\r\n|\n\n")


def _extract_raw_headers(raw_bytes: bytes | None) -> str | None:
    """Extract the verbatim RFC5322 header section from raw message bytes.

    Slices from the start of *raw_bytes* up to (but not including) the first
    blank line (the RFC5322 header/body separator).  Returns the header section
    decoded with Latin-1 (ISO-8859-1), which provides a lossless byte↔char
    bijection: every byte 0x00–0xFF maps to U+0000–U+00FF, and
    ``.encode('latin-1')`` restores the original bytes exactly.

    Returns ``None`` (never an empty string) when:
    - *raw_bytes* is None or empty
    - no blank-line separator is found (malformed / body-only fragment)
    """
    if not raw_bytes:
        return None

    match = _HEADER_SEPARATOR_RE.search(raw_bytes)
    if match is None or match.start() == 0:
        return None

    return raw_bytes[: match.start()].decode("latin-1")


def _get_legacy_header_values(raw_header_bytes: bytes, header_name: str) -> list[str]:
    """Return verbatim, unfolded header values for *header_name*.

    Re-parses *raw_header_bytes* with the legacy ``compat32`` policy, which
    performs no address-list parsing, so the returned strings are the raw
    header text exactly as written (including a bare semicolon list that the
    ``policy.default`` headerregistry parser would otherwise truncate before
    the semicolon is even visible in its ``str()`` form). Only the header
    section is needed, so the parse is header-only (no body/attachment
    parsing). Never raises; returns ``[]`` on any parse failure or when the
    header is absent.
    """
    try:
        legacy_message = BytesParser(policy=policy.compat32).parsebytes(
            raw_header_bytes, headersonly=True
        )
    except Exception:
        return []
    return [str(value) for value in legacy_message.get_all(header_name, [])]


def _recover_addresses_from_legacy_values(
    legacy_values: list[str],
) -> list[dict[str, str | None]]:
    """Recover ``{name, email}`` pairs from already-parsed legacy header values.

    Implements design.md Decision 2: split each verbatim (compat32-parsed)
    header value on ``;``, strip and drop empty fragments, and run
    ``getaddresses`` over the fragments. Splitting on the semicolon leaves
    comma-bearing quoted display names intact within a fragment;
    ``getaddresses`` still handles any comma-separated addresses inside a
    fragment. *legacy_values* is expected to come from a single
    ``_get_legacy_header_values`` call so the raw bytes are only ever parsed
    once. Never raises.
    """
    fragments: list[str] = []
    for raw_value in legacy_values:
        for fragment in raw_value.split(";"):
            stripped = fragment.strip()
            if stripped:
                fragments.append(stripped)

    if not fragments:
        return []

    return [
        {"name": name or None, "email": email_address or None}
        for name, email_address in getaddresses(fragments)
    ]


def _recover_truncated_recipients(
    *,
    header_name: str,
    header_values: list[Any],
    default_addresses: list[dict[str, str | None]],
    raw_bytes: bytes | None,
) -> list[dict[str, str | None]]:
    """Apply the Decision 1 strict replace condition for one To/Cc/Bcc header.

    ``header_values`` may hold MULTIPLE header instances (RFC allows a
    message to repeat ``To``/``Cc``/``Bcc``). The default-policy
    ``header_values`` and the compat32-reparsed legacy values (via
    ``_get_legacy_header_values``) both walk the same raw header bytes in
    the same order, so instance *i* in one list describes the exact same
    header line as instance *i* in the other — they are paired by index.

    Semicolon recovery is considered for a given instance ONLY when THAT
    SAME instance both (a) carries the address-list invalidity defect
    (``InvalidHeaderDefect``) on the default-policy parse, and (b) its
    compat32 legacy value contains a semicolon. Instances that don't meet
    both conditions on the same instance keep their default-policy parse.
    The per-instance results are combined in header order, and the combined
    result replaces *default_addresses* only when it yields a strict
    superset of valid, non-empty email addresses (the pre-existing Decision
    1 backstop). Otherwise — including when *raw_bytes* is unavailable, no
    instance qualifies, or any step fails — returns *default_addresses*
    unchanged. Never raises.
    """
    if not raw_bytes or not header_values:
        return default_addresses

    try:
        legacy_values = _get_legacy_header_values(raw_bytes, header_name)

        qualifies = [False] * len(header_values)
        for index, header_value in enumerate(header_values):
            if index >= len(legacy_values):
                break
            instance_has_defect = any(
                isinstance(defect, InvalidHeaderDefect)
                for defect in getattr(header_value, "defects", ())
            )
            if instance_has_defect and ";" in legacy_values[index]:
                qualifies[index] = True

        if not any(qualifies):
            return default_addresses

        combined: list[dict[str, str | None]] = []
        for index, header_value in enumerate(header_values):
            if qualifies[index]:
                combined.extend(
                    _recover_addresses_from_legacy_values([legacy_values[index]])
                )
            else:
                combined.extend(_parse_address_list([header_value]))
    except Exception:
        return default_addresses

    default_emails = {
        entry["email"] for entry in default_addresses if entry.get("email")
    }
    combined_emails = {entry["email"] for entry in combined if entry.get("email")}

    if combined_emails > default_emails:
        return combined
    return default_addresses


def _drop_addresses_without_email(
    addresses: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    """Drop entries that carry no email address from a parsed address list.

    An address entry with no email is never valid recipient data: it cannot be
    matched by `--contact`, cannot participate in domain filters, and only adds
    a display-name-shaped row to `message_recipients` and to the searchable
    recipient text.

    `email.policy.default` produces such entries on several header shapes, and
    which shapes it produces them for has changed between CPython patch
    releases. Filtering here holds the invariant on every path out of address
    parsing — recovery applied, recovery declined, and no raw byte source —
    rather than on the shapes one interpreter happens to mangle.

    `models._coerce_participants` already drops an entry whose name *and* email
    are both empty, so this is not a duplicate of that filter: the entries it
    lets through are exactly the name-bearing ones (`To: Alice <>`), which are
    the ones that reach the database.
    """
    return [entry for entry in addresses if (entry.get("email") or "").strip()]


def _repair_mislabelled_names(
    addresses: list[dict[str, str | None]], message: Any, header_name: str
) -> list[dict[str, str | None]]:
    """Re-decode display names the stdlib could not decode (#287).

    ``policy.default`` decodes an encoded-word strictly by its label, so a
    ``big5``-labelled cp950 or ``gb2312``-labelled GBK name comes back with
    U+FFFD. Only those names are touched: the raw header is re-read and its
    encoded-word names go through ``decode_header_text`` (the Subject's
    label handling). Names the stdlib decoded cleanly stay as they were.
    Entries are matched to the raw header by email address.
    """
    if not any("�" in (entry["name"] or "") for entry in addresses):
        return addresses
    raw_names: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name, value in message.raw_items():
        if name.casefold() != header_name:
            continue
        for display, address in getaddresses([_FOLD.sub("", str(value))]):
            if address and "=?" in display:
                key = address.casefold()
                decoded = decode_header_text(display)
                if raw_names.setdefault(key, decoded) != decoded:
                    # One address listed twice under different names: which
                    # entry is which cannot be told by address, so leave both.
                    ambiguous.add(key)
    for key in ambiguous:
        del raw_names[key]
    return [
        {
            **entry,
            "name": raw_names.get((entry["email"] or "").casefold(), entry["name"]),
        }
        if "�" in (entry["name"] or "")
        else entry
        for entry in addresses
    ]


def _parse_email_message(
    message: Any,
    *,
    raw_bytes: bytes | None = None,
    attachment_root: Path | None = None,
) -> dict[str, object]:
    sender_name, sender_email = _parse_single_address(message.get("From"))
    (sender,) = _repair_mislabelled_names(
        [{"name": sender_name, "email": sender_email}], message, "from"
    )
    attachments = _extract_attachments(message, attachment_root=attachment_root)

    recipients: dict[str, list[dict[str, str | None]]] = {}
    for header_name in ("To", "Cc", "Bcc"):
        values = message.get_all(header_name, [])
        recipients[header_name.lower()] = _repair_mislabelled_names(
            _drop_addresses_without_email(
                _recover_truncated_recipients(
                    header_name=header_name,
                    header_values=values,
                    default_addresses=_parse_address_list(values),
                    raw_bytes=raw_bytes,
                )
            ),
            message,
            header_name.lower(),
        )

    return {
        "subject": _subject_from_message(message),
        "sender": sender,
        "to": recipients["to"],
        "cc": recipients["cc"],
        "bcc": recipients["bcc"],
        "sent_at": _parse_datetime_candidates([message.get("Date")]),
        "received_at": _parse_datetime_candidates(
            message.get_all("Received", []), extract_trace_timestamp=True
        ),
        "body_text": _extract_body_text(message),
        "attachments": attachments,
        "attachment_names": [
            str(attachment["attachment_name"]) for attachment in attachments
        ],
        "message_id": message.get("Message-ID"),
        "in_reply_to": message.get("In-Reply-To"),
        "references": message.get("References"),
        "raw_headers": _extract_raw_headers(raw_bytes),
    }


def resolve_readpst_path(readpst_path: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if readpst_path is not None:
        candidates.append(Path(readpst_path))

    env_value = os.environ.get("MAILATTIC_READPST_PATH")
    env_candidate = Path(env_value) if env_value is not None else None
    if env_candidate is not None:
        candidates.append(env_candidate)

    repo_default = (
        Path(__file__).resolve().parents[2]
        / ".tools"
        / "libpst"
        / "tool"
        / "bin"
        / "readpst.exe"
    )
    candidates.append(repo_default)

    which_result = shutil.which("readpst.exe") or shutil.which("readpst")
    if which_result is not None:
        candidates.append(Path(which_result))

    for candidate in candidates:
        expanded = candidate.expanduser().resolve()
        if expanded.exists():
            return expanded

    raise ParserUnavailableError(
        "Unable to locate readpst. Provide --readpst-path, set MAILATTIC_READPST_PATH, "
        "or place readpst.exe under .tools\\libpst\\tool\\bin\\readpst.exe."
    )


def _parse_eml_file(
    eml_path: Path, *, attachment_root: Path | None = None
) -> dict[str, object]:
    raw_bytes = eml_path.read_bytes()
    message = BytesParser(policy=_MAIL_POLICY).parsebytes(raw_bytes)

    return _parse_email_message(
        message, raw_bytes=raw_bytes, attachment_root=attachment_root
    )


def _parse_single_address(raw_header: str | None) -> tuple[str | None, str | None]:
    addresses = getaddresses([raw_header] if raw_header else [])
    if not addresses:
        return (None, None)
    name, email_address = addresses[0]
    return (name or None, email_address or None)


def _parse_address_list(raw_headers: list[str]) -> list[dict[str, str | None]]:
    return [
        {"name": name or None, "email": email_address or None}
        for name, email_address in getaddresses(raw_headers)
    ]


def _parse_datetime_candidates(
    raw_headers: Iterable[str | None],
    *,
    extract_trace_timestamp: bool = False,
) -> object:
    for raw_header in raw_headers:
        if raw_header is None:
            continue

        normalized = raw_header.strip()
        if not normalized:
            continue

        candidates = [normalized]
        if extract_trace_timestamp and ";" in normalized:
            trace_timestamp = normalized.rsplit(";", maxsplit=1)[-1].strip()
            if trace_timestamp:
                candidates.insert(0, trace_timestamp)

        for candidate in candidates:
            try:
                return parsedate_to_datetime(candidate)
            except (TypeError, ValueError, IndexError):
                continue
    return None


def _is_rfc822_attachment_part(part: Any, *, outer_message: Any) -> bool:
    if part is outer_message:
        return False
    if part.get_content_type() != "message/rfc822":
        return False
    if _is_attachment_part(part):
        return True
    return outer_message.get_content_type() != "multipart/report"


def _walk_own_parts_with_context(message: Any) -> Iterator[tuple[Any, bool]]:
    """walk() that does not descend into an attached message/rfc822 subtree,
    yielding (part, in_alternative).
    """
    stack: list[tuple[Any, bool]] = [(message, False)]
    while stack:
        part, in_alt = stack.pop()
        yield part, in_alt
        if part.is_multipart() and not _is_rfc822_attachment_part(
            part, outer_message=message
        ):
            is_alt = in_alt or (part.get_content_type() == "multipart/alternative")
            for child in reversed(part.get_payload()):
                stack.append((child, is_alt))


# Pinned host-independent extensions for standard attachment MIME types.
# Mirrors CODING_STANDARDS.md rule D2 spirit: standard formats must not depend
# on machine-local Windows registry overrides.
_PINNED_MIME_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "text/calendar": ".ics",
    "text/csv": ".csv",
    "message/rfc822": ".eml",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "application/zip": ".zip",
    "application/json": ".json",
    "application/octet-stream": ".bin",
}


def _clean_attachment_filename(raw_fn: str) -> str | None:
    """Normalize and decode a raw filename parameter, stripping whitespace/quotes."""
    if raw_fn.startswith("?") and "?=" in raw_fn:
        raw_fn = "=" + raw_fn
    decoded = decode_header_text(raw_fn).strip(" \"'\t\r\n")
    return decoded or None


def _raw_filename(part: Any) -> str | None:
    """Filename read from the raw header, decoded with the Subject's label handling.

    ``part.get_filename()`` decodes both RFC 2047 encoded-words and RFC 2231
    ``filename*`` strictly by label, so a ``big5``-labelled cp950 or
    ``gb2312``-labelled GBK name comes back with U+FFFD (#287). Same lookup
    order as ``get_filename`` (Content-Disposition ``filename``, then
    Content-Type ``name``). An RFC 2231 value is returned decoded; an RFC 2047
    one is returned raw for ``_clean_attachment_filename``. ``None`` when
    nothing usable is found; the caller then keeps the stdlib result.
    """
    if not hasattr(part, "raw_items"):
        return None
    for header, param in (
        ("content-disposition", "filename"),
        ("content-type", "name"),
    ):
        for name, value in part.raw_items():
            if name.casefold() != header:
                continue
            try:
                parsed = HeaderParser(policy=policy.compat32).parsestr(
                    f"{name}: {value}\n\n"
                )
                found = parsed.get_param(param, header=header)
            except Exception:
                continue
            if not isinstance(found, tuple):
                if found:
                    return found
                continue
            if found[0] and _text_codec_name(found[0]) is not None:
                return _decode_word_run(found[2].encode("latin-1", "replace"), found[0])
    return None


def _extract_part_filename(part: Any) -> str | None:
    """Extract filename from Content-Disposition or Content-Type headers.

    Falls back to legacy / compat32 header parsing when headerregistry
    drops an unquoted RFC 2047 encoded-word parameter as an InvalidHeaderDefect.
    """
    filename = part.get_filename()
    if filename and "�" in filename:
        # Only a name the stdlib could not decode is re-read from the raw
        # header; names it decoded cleanly (including raw 8-bit UTF-8 ones,
        # which the raw path would garble) stay as they were.
        repaired = _raw_filename(part)
        if repaired and "�" not in repaired:
            filename = repaired
    if filename:
        cleaned = _clean_attachment_filename(filename)
        if cleaned:
            return cleaned

    if hasattr(part, "raw_items"):
        for header_name, header_value in part.raw_items():
            if header_name.lower() in ("content-disposition", "content-type"):
                try:
                    parsed_header = HeaderParser(policy=policy.compat32).parsestr(
                        f"{header_name}: {header_value}\n\n"
                    )
                    raw_fn = parsed_header.get_filename()
                    if raw_fn:
                        cleaned = _clean_attachment_filename(raw_fn)
                        if cleaned:
                            return cleaned
                except Exception:
                    continue
    return None


def _fallback_attachment_name(mime_type: str | None, sequence_number: int) -> str:
    """Generate an identifiable fallback name for an attachment lacking one."""
    ext = _PINNED_MIME_EXTENSIONS.get(mime_type.lower()) if mime_type else None
    if not ext and mime_type:
        ext = mimetypes.guess_extension(mime_type)
    if not ext:
        ext = ".bin"
    return f"unnamed-attachment-{sequence_number}{ext}"


def _extract_body_text(message: Any) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part, in_alt in _walk_own_parts_with_context(message):
        if part.is_multipart():
            continue
        if _is_attachment_part(part, in_alternative=in_alt):
            continue
        if part.get_content_maintype() != "text":
            continue

        raw_bytes = part.get_payload(decode=True)
        content = _without_lone_surrogates(
            _detect_and_decode(raw_bytes, part.get_content_charset())
        )
        if part.get_content_subtype() == "plain":
            plain_parts.append(content)
        elif part.get_content_subtype() == "html":
            html_parts.append(_html_to_text(content))

    cleaned_plain_parts = [
        candidate.strip() for candidate in plain_parts if candidate.strip()
    ]
    if cleaned_plain_parts:
        return "\n\n".join(cleaned_plain_parts)

    cleaned_html_parts = [
        candidate.strip() for candidate in html_parts if candidate.strip()
    ]
    return "\n\n".join(cleaned_html_parts)


def _extract_attachments(
    message: Any,
    *,
    attachment_root: Path | None = None,
) -> list[dict[str, object]]:
    attachments: list[dict[str, object]] = []
    unnamed_seq = 0
    for part, in_alt in _walk_own_parts_with_context(message):
        filename = _extract_part_filename(part)
        is_rfc822_attach = _is_rfc822_attachment_part(part, outer_message=message)
        if not (
            _is_attachment_part(part, in_alternative=in_alt, filename=filename)
            or is_rfc822_attach
        ):
            continue

        mime_type: str | None = part.get_content_type()
        if not filename and is_rfc822_attach:
            # _walk_own_parts keeps this subtree out of the parent body, so
            # skipping it for want of a filename would drop the mail entirely.
            try:
                payload_0 = part.get_payload(0)
                subject = (
                    str(payload_0.get("Subject") or "").strip() if payload_0 else ""
                )
            except Exception:
                subject = ""
            filename = f"{subject or 'attached-message'}.eml"
        elif not filename:
            unnamed_seq += 1
            filename = _fallback_attachment_name(mime_type, unnamed_seq)

        attachment: dict[str, object] = {
            "attachment_name": filename,
            "content_hash": None,
            "size_bytes": None,
            "mime_type": None,
            "storage_path": None,
        }
        if attachment_root is not None:
            try:
                if part.get_content_type() == "message/rfc822":
                    # payload is a [Message] list; decode=True would give None
                    payload = part.get_payload(0).as_bytes()
                else:
                    payload = part.get_payload(decode=True)
                if payload is None:
                    attachment["_decode_error"] = True
                else:
                    attachment["_expected_size"] = len(payload)
                    record = attachment_store.write_attachment(
                        payload,
                        filename,
                        mime_type,
                        attachment_root,
                    )
                    attachment["content_hash"] = record.content_hash
                    attachment["size_bytes"] = record.size_bytes
                    attachment["mime_type"] = record.mime_type
                    attachment["storage_path"] = record.storage_path
            except OSError:
                attachment["_store_error"] = True
            except Exception:
                attachment["_decode_error"] = True
        attachments.append(attachment)
    return attachments


def _is_attachment_part(
    part: Any,
    *,
    in_alternative: bool = False,
    filename: str | None = None,
) -> bool:
    if part.is_multipart():
        return False

    disposition = part.get_content_disposition()
    resolved_filename = (
        filename if filename is not None else _extract_part_filename(part)
    )

    if part.get_content_type() == "message/rfc822" or part.get_content_subtype() in (
        "plain",
        "html",
    ):
        return disposition == "attachment" or (
            disposition == "inline" and bool(resolved_filename)
        )

    if disposition == "attachment":
        return True

    if resolved_filename:
        return True

    if part.get_content_maintype() != "text":
        return part.get_content_type() != "message/delivery-status"

    return not in_alternative


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()


_HTML_SKIPPED_TAGS = frozenset({"style", "script", "title"})
_HTML_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol",
        "p", "pre", "section", "table", "tbody", "tfoot", "thead", "tr", "ul",
    }
)  # fmt: skip


class _HTMLTextExtractor(HTMLParser):
    """Text nodes joined by spaces within a line; block tags start a new line.

    ``style`` / ``script`` / ``title`` content is dropped (#294). One pass over
    the parser's events, no regex.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []
        self._words: list[str] = []
        self._skipped: list[str] = []
        self._skip_depth = 0

    def _end_line(self) -> None:
        if self._words:
            self._lines.append(" ".join(self._words))
            self._words = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _HTML_SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._end_line()

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            if not self._skip_depth:
                self._skipped = []
        elif tag in _HTML_BLOCK_TAGS:
            self._end_line()

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        if self._skip_depth:
            self._skipped.append(data.strip())
        else:
            self._words.append(data.strip())

    def get_text(self) -> str:
        if self._skip_depth:
            # The skipped element never closed (malformed HTML), so "skipped"
            # swallowed the rest of the mail. Keep that text rather than store
            # an empty body: stray style text is noise, a lost body is loss.
            self._words.extend(self._skipped)
        self._end_line()
        return "\n".join(self._lines)
