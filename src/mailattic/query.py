from __future__ import annotations

import mimetypes
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from html import unescape
from pathlib import Path

from .cjk_segmentation import _is_cjk, segment_cjk
from .datetime_bounds import coerce_to_taipei_aware
from .identity import compute_direction, resolve_self_addresses
from .storage import SYNC_STATE_KEY, connect_database

# ---------------------------------------------------------------------------
# Thread walker: references_header tokenizer
# ---------------------------------------------------------------------------
# Spike findings (spike-notes.md): all 30 sampled rows wrap Message-IDs in
# angle brackets; primary separator is whitespace (space or tab, RFC 5322
# folding). No commas or newlines observed. Primary path: regex `<([^>]+)>`.
# Fallback: whitespace-split for hypothetical bracket-free formats.
_REF_RE = re.compile(r"<([^>]+)>")


_CFWS_COMMENT = r"(?:\([^()]*\)\s*)*"
_BRACKETED_ID_RE = re.compile(rf"^\s*{_CFWS_COMMENT}<([^<>]*)>\s*{_CFWS_COMMENT}$")
_BARE_ID_RE = re.compile(rf"^\s*{_CFWS_COMMENT}([^\s()<>]+)\s*{_CFWS_COMMENT}$")


def normalize_message_id(value: str | None) -> str:
    """Canonical derived identity of a Message-ID (#22).

    Outer CFWS (whitespace and ``(comments)`` outside the angle brackets, RFC
    5322 3.6.4) is dropped, so ``"<a@x>  "``, ``"(c) <a@x> (note)"`` and ``"a@x"``
    are one identity. The id inside the brackets keeps its case and content
    (only edge whitespace is trimmed). Stored headers are never rewritten.
    """
    text = value or ""
    match = _BRACKETED_ID_RE.match(text) or _BARE_ID_RE.match(text)
    if match:
        return match.group(1).strip()
    return text.strip().strip("<>").strip()


def select_rows_by_message_id(
    connection: sqlite3.Connection,
    clean_id: str,
    columns: str,
    exclude_id: int | None = None,
) -> list[sqlite3.Row]:
    """Rows whose stored ``message_id`` normalises to *clean_id*.

    Index range seeks form a superset: the bare and bracketed prefixes, plus the
    rare stored values that start with whitespace or a ``(comment)``. Each row
    is then verified exactly with :func:`normalize_message_id`, so any outer
    CFWS is found without a full scan. ``columns`` must be trusted constants;
    ``message_id`` is always appended as ``_mid``.
    """
    if not clean_id:
        return []
    ranges = (
        (clean_id, clean_id + "\U0010ffff"),
        (f"<{clean_id}", f"<{clean_id}\U0010ffff"),
        ("", "!"),  # leading whitespace / control characters
        ("(", ")"),  # leading comment
    )
    rows: dict[int, sqlite3.Row] = {}
    for low, high in ranges:
        cursor = connection.execute(
            f"SELECT {columns}, message_id AS _mid FROM messages"
            " WHERE message_id >= ? AND message_id < ? AND id IS NOT ?",
            [low, high, exclude_id],
        )
        for r in cursor:
            if normalize_message_id(r["_mid"]) == clean_id:
                rows[int(r["id"])] = r
    return list(rows.values())


def _tokenize_references(header: str | None) -> list[str]:
    """Extract Message-IDs from a ``References`` header value.

    Returns a list of bare IDs (angle brackets stripped). Accepts ``None``
    or empty string and returns ``[]``.

    Algorithm per spike-notes.md:
    - Primary: ``re.findall(r'<([^>]+)>', header)`` – covers 100% of observed
      samples (all use angle-bracket notation, space/tab separated).
    - Fallback: whitespace-split and strip stray brackets – for hypothetical
      bracket-free formats encountered from unseen mail clients.
    """
    if not header:
        return []
    matches = _REF_RE.findall(header)
    if matches:
        return matches
    return [tok.strip("<>") for tok in header.split() if tok.strip("<>")]


# ---------------------------------------------------------------------------
# HTML cleaner / plain-text renderer
# ---------------------------------------------------------------------------
# HTML-to-text cleanup (`clean_html()`).
# Extended to accept None input (returns empty string).
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}", re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# Standard HTML tag names recognized for cleaning (issue #240).
# Plain text comparison conditions (e.g. 'x < 7 and y > 40') and
# angle-bracket email addresses / URLs ('<sales@example.test>', '<https://...>')
# do not match valid HTML tags and are preserved intact.
#
# Known boundary / limitation (issue #240):
# When a plain-text email literally contains a standard HTML tag name (such as
# '<b>' or '<p>'), render_text cannot distinguish it from HTML markup without
# out-of-band MIME metadata at display time. The cleaner treats syntactically
# valid HTML tags as markup and strips them.
_HTML_TAG_NAMES: frozenset[str] = frozenset(
    {
        "a",
        "abbr",
        "acronym",
        "address",
        "applet",
        "area",
        "article",
        "aside",
        "audio",
        "b",
        "base",
        "basefont",
        "bdi",
        "bdo",
        "big",
        "blockquote",
        "body",
        "br",
        "button",
        "canvas",
        "caption",
        "center",
        "cite",
        "code",
        "col",
        "colgroup",
        "data",
        "datalist",
        "dd",
        "del",
        "details",
        "dfn",
        "dialog",
        "dir",
        "div",
        "dl",
        "dt",
        "em",
        "embed",
        "fieldset",
        "figcaption",
        "figure",
        "font",
        "footer",
        "form",
        "frame",
        "frameset",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hgroup",
        "hr",
        "html",
        "i",
        "iframe",
        "img",
        "input",
        "ins",
        "isindex",
        "kbd",
        "label",
        "legend",
        "li",
        "link",
        "main",
        "map",
        "mark",
        "marquee",
        "menu",
        "meta",
        "meter",
        "nav",
        "noframes",
        "noscript",
        "object",
        "ol",
        "optgroup",
        "option",
        "output",
        "p",
        "param",
        "picture",
        "pre",
        "progress",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "script",
        "search",
        "section",
        "select",
        "small",
        "source",
        "span",
        "strike",
        "strong",
        "style",
        "sub",
        "summary",
        "sup",
        "svg",
        "table",
        "tbody",
        "td",
        "template",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "time",
        "title",
        "tr",
        "track",
        "tt",
        "u",
        "ul",
        "var",
        "video",
        "wbr",
        "xml",
    }
)
_TAG_NAME_PATTERN = "|".join(sorted(_HTML_TAG_NAMES, key=len, reverse=True))
# XML namespace tags used by Microsoft Outlook / Office (e.g. o:p, w:wrap, v:shape)
_XML_NS_TAG_PATTERN = r"[a-zA-Z]+:[a-zA-Z0-9]+"
_ANY_TAG_NAME = rf"(?:{_TAG_NAME_PATTERN}|{_XML_NS_TAG_PATTERN})"

_TAG_RE = re.compile(
    rf"<(?:/(?:{_ANY_TAG_NAME})\s*|!DOCTYPE\b[^>]*|\?(?:xml\b)?[^>]*\?|{_ANY_TAG_NAME}(?:\s+[^>]*)?\s*/?)>",
    re.I,
)

# Wrapper pattern for @media blocks with nested declaration blocks (issue #289 / L2).
_MEDIA_BLOCK_RE = re.compile(
    r"@media[^{@]*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.S | re.I
)

# Refined CSS selector grammar recognizing simple selectors and compound/combinator runs
# (issues #289 / L2 / L3).
_SIMPLE_CSS_SEL = (
    rf"(?:{_ANY_TAG_NAME}|[a-zA-Z]+\\:[*a-zA-Z0-9_-]+|\*|\.[a-zA-Z0-9_-]+|#[a-zA-Z0-9_-]+|"
    rf"@[a-zA-Z0-9_-]+(?:\s+[a-zA-Z0-9_-]+)?|::?[a-zA-Z0-9_-]+)"
    # Suffixes: class / id, pseudo-class or pseudo-element (``:hover``,
    # ``::before``, ``:nth-child(2)``), attribute. All possessive: no backtracking.
    rf"(?:[.#][a-zA-Z0-9_-]+|::?[a-zA-Z0-9_-]+(?:\([^()]*+\))?+|\[[^\]\[]*+\])*+"
)

# One maximal selector run. Deliberately not anchored to the end of the text:
# ``search`` with a trailing ``$`` restarts inside a run that turns out not to
# reach the brace ("p p p ... word {") and goes quadratic. ``finditer`` consumes
# each run once; the caller checks that the last run ends at the brace (L3).
_CSS_SEL_RUN_RE = re.compile(
    rf"(?:^|[\s;}}>])(?P<sel>{_SIMPLE_CSS_SEL}(?>\s*,\s*{_SIMPLE_CSS_SEL}|\s++{_SIMPLE_CSS_SEL})*+)",
    re.I,
)

_CSS_DECL = r"[a-zA-Z-][a-zA-Z0-9-]*\s*:[^;}]*"
_CSS_DECLS_RE = re.compile(
    rf"^(?:[ \t\r\n]*|(?:\s*{_CSS_DECL}(?:;[ \t\r\n]*{_CSS_DECL})*;?\s*))$",
    re.S,
)


def _clean_css_rules(text: str) -> str:
    """Strip CSS rules leaving plain-text braces intact in O(N) time (#289 / L3).

    Locates non-nested declaration blocks ``{ ... }`` and verifies whether the
    preceding text ends in a recognized CSS selector run with valid declarations.
    Unclosed braces or non-CSS text preceding braces never trigger re-scans.
    """
    if "{" not in text or "}" not in text:
        return text

    out: list[str] = []
    last_idx = 0
    idx = 0
    n = len(text)
    while idx < n:
        open_pos = text.find("{", idx)
        if open_pos == -1:
            out.append(text[last_idx:])
            break
        close_pos = text.find("}", open_pos + 1)
        if close_pos == -1:
            out.append(text[last_idx:])
            break
        next_open = text.find("{", open_pos + 1)
        if next_open != -1 and next_open < close_pos:
            out.append(text[last_idx:next_open])
            last_idx = next_open
            idx = next_open
            continue

        pre_text = text[last_idx:open_pos]
        pre_stripped = pre_text.rstrip()
        m = None
        if pre_stripped and pre_stripped[-1] not in ",;{}/=":
            runs = list(_CSS_SEL_RUN_RE.finditer(pre_text))
            m = runs[-1] if runs else None
            if m and m.end("sel") != len(pre_stripped):
                m = None

        block_content = text[open_pos + 1 : close_pos]
        if m and _CSS_DECLS_RE.match(block_content):
            out.append(pre_text[: m.start("sel")])
            last_idx = close_pos + 1
            idx = close_pos + 1
        else:
            out.append(text[last_idx : close_pos + 1])
            last_idx = close_pos + 1
            idx = close_pos + 1

    return "".join(out)


# Strict entity reference pattern requiring terminating semicolon (issue #289).
# html.unescape() matches semicolon-less legacy entities (e.g. '&reg' in '&region='
# or '&copy' in '&copy=1'), corrupting URL query parameters in plain text.
_STRICT_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z0-9]+);")

_HSPACE_RE = re.compile(r"[ \t]+")
_VSPACE_RE = re.compile(r"\n\s*\n+")


SNIPPET_MAX_CHARS = 160


def render_text(body: str | None) -> str:
    """Render a raw ``body_text`` field as plain text.

    Removes HTML tags, embedded CSS blocks (``<style>``, ``@font-face``),
    HTML comments, unescapes HTML entities, collapses horizontal whitespace
    to a single space, and collapses multiple blank lines to a single blank
    line.

    Accepts ``None`` and returns ``""`` in that case.
    No external dependencies are introduced.
    """
    if not body:
        return ""
    text = _COMMENT_RE.sub("", body)
    text = _STYLE_RE.sub("", text)
    text = _FONT_FACE_RE.sub("", text)
    if "@" in text:  # _MEDIA_BLOCK_RE is case-insensitive; so is this gate
        text = _MEDIA_BLOCK_RE.sub("", text)
    if "{" in text:
        text = _clean_css_rules(text)
    text = _TAG_RE.sub(" ", text)
    if "&" in text:
        text = _STRICT_ENTITY_RE.sub(lambda m: unescape(m.group(0)), text)
    text = _HSPACE_RE.sub(" ", text)
    text = _VSPACE_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Quote-tail stripper (issue #9 / add-new-only-quote-strip)
# ---------------------------------------------------------------------------
# Concept and pattern set adapted from talon (Apache-2.0, Mailgun),
# email-reply-parser (MIT, Zapier), mail-parser-reply (MIT, alfonsrv).
# Reimplemented as stdlib re with self-built CJK Outlook header patterns
# (the CJK token set is novel — no verbatim copy of any upstream library).

# Rank 1: > quoted line -- see _is_gt_quote_line (render_text may leave one
# leading space)

# Rank 2: English Outlook header line — one line of a consecutive block
_EN_HEADER_LINE_RE = re.compile(r"^(From|Sent|To|Subject|Cc):[ \t]+")

# Rank 3: CJK Outlook header tokens
# Anchor tokens: at least one REQUIRED in a header run
_CJK_ANCHOR = frozenset(
    {
        "寄件者",
        "寄件人",
        "收件者",
        "收件人",
        "主旨",
        "副本",  # Traditional Chinese
        "发件人",
        "主题",  # Simplified Chinese
    }
)
# Context tokens: may appear in a run, but CANNOT trigger a run alone
_CJK_CONTEXT = frozenset({"寄件日期", "日期", "時間", "发送时间"})
_CJK_ALL = _CJK_ANCHOR | _CJK_CONTEXT
# Sort by length descending so longer tokens are tried first in alternation
_CJK_TOKEN_ALT = "|".join(re.escape(t) for t in sorted(_CJK_ALL, key=len, reverse=True))
_CJK_HEADER_LINE_RE = re.compile(rf"^[ \t]?(?P<tok>{_CJK_TOKEN_ALT})[:：]")

# Rank 4a: -----Original Message----- (case-insensitive, flexible dashes/spaces)
_ORIG_MSG_RE = re.compile(
    r"^[ \t]*-{3,}[ \t]*original[ \t]+message[ \t]*-{3,}[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

# Rank 4b: Draft separator — ≥32 consecutive underscores on a line
_DRAFT_SEP_RE = re.compile(r"^[ \t]*_{32,}[ \t]*$", re.MULTILINE)

# Rank 5: On … wrote: / 某某 於 … 寫道： attribution lines
# Requires BOTH a 4-digit year (19xx/20xx) AND an email "@", in that order,
# between "On" and a line-final "wrote:".  Real Gmail/Apple attribution lines
# always carry a full date *and* the sender's address
# ("On Mon, Jun 30, 2026 at 9:01 AM, John <j@x.com> wrote:"), so this shape is
# their reliable signature.  Prose that merely ends in "wrote:" — even with a
# stray year like "On the 2026 forecast I wrote:" or an "@" like
# "On twitter @john I wrote:" — lacks the year+email pair and is NOT cut.  The
# Outlook-centric corpus rarely uses this form at all (it quotes via
# -----Original Message----- + header blocks), so the few name-only "wrote:"
# lines this misses are an acceptable under-cut.  Bounded quantifiers (.{0,300})
# guard against catastrophic backtracking.  Conservative principle: under-cut
# is acceptable, over-cut (data loss) is not.
_ON_WROTE_RE = re.compile(
    r"^On\b.{0,300}(?:19|20)\d{2}.{0,300}@.{0,300}\bwrote:[ \t]*$", re.MULTILINE
)
_ZH_WROTE_ENDINGS = ("寫道:", "寫道：", "写道:", "写道：")


def _is_zh_wrote_line(line: str) -> bool:
    """``某某 於 … 寫道：`` -- plain string tests, so cost is linear in the line."""
    s = line.rstrip(" \t")
    return len(s) <= 600 and s.endswith(_ZH_WROTE_ENDINGS) and ("於" in s or "于" in s)


# Decorative banner / callout line: e.g. >>> URGENT <<<
_CALLOUT_RE = re.compile(r"^[ \t]*>+[^<\n]+<+[ \t]*$")

# Rank 6: External gateway banner — exact token whitelist only (NOT a generic
# base64 pattern, per data-loss guard in the audit report).
# Anchored to line start (^[ \t]*) so that mid-sentence text mentioning
# e.g. "[EXTERNAL]" as prose is NOT treated as a banner boundary.
# Gateways always inject the banner at the start of a line.
_BANNER_RE = re.compile(
    r"^[ \t]*(?:ZjQcmQRYFpfptBannerStart"
    r"|\[EXTERNAL\]|\[External\]|CAUTION:[ \t]+This email)",
    re.MULTILINE,
)


def _iter_lines_with_offsets(text: str):
    """Yield ``(char_offset, end_offset, line_without_ending)`` for each line in *text*.

    Uses ``splitlines(keepends=True)`` so that the char offset correctly
    tracks both ``\\n`` and ``\\r\\n`` line endings.
    """
    pos = 0
    for line in text.splitlines(keepends=True):
        lend = pos + len(line)
        yield pos, lend, line.rstrip("\r\n")
        pos = lend


def _is_gt_quote_line(line: str) -> bool:
    """Whether a line is an email '>' quote line.

    A quote line starts with optional whitespace followed by '>'.
    Lines starting with '>=' (comparison operator) or callouts like
    '>>> URGENT <<<' are not quote lines.
    """
    s = line.lstrip(" \t")
    if not s.startswith(">"):
        return False
    if s.startswith(">="):
        return False
    return not bool(_CALLOUT_RE.match(line))


def _find_hard_cut(normalized: str) -> int | None:
    """Find the earliest position of whole-history boundaries (ranks 2, 3, 4, 6)."""
    hard_candidates: list[int] = []

    en_run_start: int | None = None
    en_boundary: int | None = None
    cjk_run_start: int | None = None
    cjk_has_anchor = False
    cjk_boundary: int | None = None

    for lpos, _, line in _iter_lines_with_offsets(normalized):
        if _EN_HEADER_LINE_RE.match(line):
            if en_run_start is None:
                en_run_start = lpos
            elif en_boundary is None:
                en_boundary = en_run_start
        else:
            en_run_start = None

        cm = _CJK_HEADER_LINE_RE.match(line)
        if cm:
            token = cm.group("tok")
            if cjk_run_start is None:
                cjk_run_start = lpos
                cjk_has_anchor = token in _CJK_ANCHOR
            else:
                if token in _CJK_ANCHOR:
                    cjk_has_anchor = True
                if cjk_has_anchor and cjk_boundary is None:
                    cjk_boundary = cjk_run_start
        else:
            cjk_run_start = None
            cjk_has_anchor = False

    if en_boundary is not None:
        hard_candidates.append(en_boundary)
    if cjk_boundary is not None:
        hard_candidates.append(cjk_boundary)

    m = _ORIG_MSG_RE.search(normalized)
    if m:
        hard_candidates.append(m.start())

    m = _DRAFT_SEP_RE.search(normalized)
    if m:
        hard_candidates.append(m.start())

    m = _BANNER_RE.search(normalized)
    if m:
        banner_pos = m.start()
        if normalized[:banner_pos].strip():
            hard_candidates.append(banner_pos)

    return min(hard_candidates) if hard_candidates else None


@dataclass(frozen=True, slots=True)
class _QuoteAnalysis:
    """Result of parsing quote boundaries and reply structure from a body."""

    stripped_text: str
    is_top_posted: bool
    top_posted_cut: int | None
    hard_cut: int | None
    line_spans: list[tuple[int, int, str]]


def _parse_body_quotes(text: str) -> _QuoteAnalysis:
    """Parse quote boundaries, distinguishing top from bottom/inline replies."""
    if not text:
        return _QuoteAnalysis(text, True, None, None, [])

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    hard_cut = _find_hard_cut(normalized)
    scanned_text = normalized[:hard_cut] if hard_cut is not None else normalized

    line_spans: list[tuple[int, int, str]] = []
    lines: list[tuple[int, str, str]] = []
    for lpos, lend, line_text in _iter_lines_with_offsets(scanned_text):
        s = line_text.strip()
        if not s:
            kind = "blank"
        elif _is_gt_quote_line(line_text):
            kind = "quote"
        elif _ON_WROTE_RE.match(line_text):
            kind = "attr"
        elif _is_zh_wrote_line(line_text):
            kind = "zh_attr"
        else:
            kind = "normal"
        lines.append((lpos, line_text, kind))
        line_spans.append((lpos, lend, kind))

    # "…於…寫道：" is also ordinary prose ("如同我於合約第三條寫道："), and unlike
    # the English form it carries no year+address signature to tell the two
    # apart. It counts as an attribution only when a ">" quote follows it;
    # otherwise it is the sender's own text and must never become a cut point.
    next_kind = "normal"
    for i in range(len(lines) - 1, -1, -1):
        kind = lines[i][2]
        if kind == "zh_attr":
            kind = "attr" if next_kind == "quote" else "normal"
            lines[i] = (lines[i][0], lines[i][1], kind)
            line_spans[i] = (line_spans[i][0], line_spans[i][1], kind)
        if kind != "blank":
            next_kind = kind

    has_quote = any(k == "quote" for _, _, k in lines)
    has_attr = any(k == "attr" for _, _, k in lines)

    if not has_quote and not has_attr:
        if hard_cut is None:
            return _QuoteAnalysis(text, True, None, None, line_spans)
        res = normalized[:hard_cut].rstrip()
        if not res:
            return _QuoteAnalysis(text, True, None, hard_cut, line_spans)
        return _QuoteAnalysis(res, True, hard_cut, hard_cut, line_spans)

    if has_attr and not has_quote:
        first_attr_idx = next(i for i, (_, _, k) in enumerate(lines) if k == "attr")
        first_attr_pos = lines[first_attr_idx][0]
        has_normal_before = any(k == "normal" for _, _, k in lines[:first_attr_idx])
        if has_normal_before:
            cut = first_attr_pos
            if hard_cut is not None and hard_cut < cut:
                cut = hard_cut
            res = normalized[:cut].rstrip()
            if not res:
                return _QuoteAnalysis(text, True, None, hard_cut, line_spans)
            return _QuoteAnalysis(res, True, cut, hard_cut, line_spans)
        else:
            return _QuoteAnalysis(text, True, None, hard_cut, line_spans)

    first_quote_idx = next(i for i, (_, _, k) in enumerate(lines) if k == "quote")
    idx = first_quote_idx - 1
    while idx >= 0 and lines[idx][2] == "blank":
        idx -= 1
    if idx >= 0 and lines[idx][2] == "attr":
        first_quote_block_idx = idx
    else:
        first_quote_block_idx = first_quote_idx
    first_quote_pos = lines[first_quote_block_idx][0]

    has_normal_after = any(k == "normal" for _, _, k in lines[first_quote_idx + 1 :])
    has_normal_before = any(k == "normal" for _, _, k in lines[:first_quote_block_idx])

    if not has_normal_after:
        if not has_normal_before:
            return _QuoteAnalysis(text, True, None, hard_cut, line_spans)
        cut = first_quote_pos
        if hard_cut is not None and hard_cut < cut:
            cut = hard_cut
        res = normalized[:cut].rstrip()
        if not res:
            return _QuoteAnalysis(text, True, None, hard_cut, line_spans)
        return _QuoteAnalysis(res, True, cut, hard_cut, line_spans)

    # Bottom reply or interleaved reply: extract normal blocks
    blocks: list[str] = []
    current_block: list[str] = []
    for _, line_text, kind in lines:
        if kind == "normal":
            current_block.append(line_text)
        else:
            if current_block:
                blocks.append("\n".join(current_block))
                current_block = []
    if current_block:
        blocks.append("\n".join(current_block))

    res = "\n\n".join(blocks).strip()
    if not res:
        return _QuoteAnalysis(text, False, None, hard_cut, line_spans)
    return _QuoteAnalysis(res, False, None, hard_cut, line_spans)


def strip_quote_tail(text: str) -> str:
    """Remove the quoted-history tail from a ``render_text()``-cleaned body.

    Finds the reliable boundary markers across all ranked patterns and
    returns the sender's own new content (trailing whitespace stripped).
    Conservative fallback: no marker → return the full text unchanged.
    Undecidable cut: if stripping would leave an empty string for a non-empty
    body, returns the full text unchanged.

    Handles top-posted replies, bottom-posted replies (quote above, reply below),
    and interleaved replies (answers inline between quotes).

    Ranked marker set:

    1. ``>``-quote run — lines matching ``^[ \\t]?>+`` (excluding comparison
       operators such as ``>=`` and decorative callouts such as ``>>> URGENT <<<``)
    2. English Outlook header block — ≥2 consecutive ``From:/Sent:/To:/
       Subject:/Cc:`` lines
    3. CJK Outlook header block — ≥2 consecutive CJK header lines with
       ``[:：]``, where ≥1 is an *anchor* token (寄件者/主旨/etc.)
    4. ``-----Original Message-----`` separator; draft underscore separator
       (≥32 ``_`` on a line)
    5. Attribution lines (``On … wrote:``, ``某某 於 … 寫道：``)
    6. External gateway banner (exact token whitelist:
       ``ZjQcmQRYFpfptBannerStart``, ``[EXTERNAL]``, ``[External]``,
       ``CAUTION: This email``).  Top-banner guard: if the banner is the
       very first content in the text, it is NOT treated as a boundary
       (to avoid emptying messages that carry the banner at the top).

    Concept adapted from talon (Apache-2.0) and email-reply-parser (MIT);
    CJK header patterns are self-built (no verbatim copy of any upstream library).
    No external dependency — stdlib ``re`` only.

    Note: when a boundary is found, the returned text uses ``\\n`` line
    endings.  If no boundary is found, the original text is returned
    unchanged (``\\r\\n`` preserved if present).
    """
    return _parse_body_quotes(text).stripped_text


# ---------------------------------------------------------------------------
# Thread walker
# ---------------------------------------------------------------------------
_THREAD_LIMIT = 100

# Signed 64-bit range: the accepted magnitude for a thread db-id identifier
# (design.md > "Decide magnitude on the string, sign- and zero-normalized").
_THREAD_ID_INT64_MIN = -(2**63)
_THREAD_ID_INT64_MAX = 2**63 - 1
# Decimal bound strings, one per sign -- both 19 digits long. Comparing an
# effective digit string against these never requires int() on caller input.
_THREAD_ID_POSITIVE_BOUND = str(_THREAD_ID_INT64_MAX)  # "9223372036854775807"
_THREAD_ID_NEGATIVE_BOUND = str(-_THREAD_ID_INT64_MIN)  # "9223372036854775808"


class ThreadIdentifierError(ValueError):
    """Raised when a thread identifier is not one of the accepted forms.

    Accepted forms: an integer stored id within the signed 64-bit range,
    its decimal string form, or an RFC822 Message-ID string. The message
    never embeds the rejected value (design.md > "Raise a dedicated
    ValueError subclass from the query module") -- for an out-of-range
    ``int`` rendering the value to decimal text is exactly the operation
    the gate exists to avoid.
    """


_THREAD_IDENTIFIER_ERROR_MESSAGE = (
    "Thread identifier must be an integer stored id within the signed "
    "64-bit range, its decimal string form, or an RFC822 Message-ID string."
)


def _normalize_thread_db_id_string(identifier_str: str) -> int:
    """Normalize a digit-shaped thread identifier string to its int db id.

    Strips one optional leading ``-``, maps each remaining character to its
    ASCII decimal digit via its Unicode decimal value, strips leading zeros
    (falling back to ``"0"`` when nothing remains), then decides magnitude
    on the effective digit string -- never by calling ``int()`` on the raw
    caller string (design.md > "Decide magnitude on the string, sign- and
    zero-normalized"). Raises :class:`ThreadIdentifierError` when a
    digit-shaped character carries no decimal value, or when the effective
    magnitude is outside the signed 64-bit range for its sign.
    """
    negative = identifier_str.startswith("-")
    digits_part = identifier_str[1:] if negative else identifier_str

    ascii_digit_chars: list[str] = []
    for char in digits_part:
        decimal_value = unicodedata.decimal(char, None)
        if decimal_value is None:
            raise ThreadIdentifierError(_THREAD_IDENTIFIER_ERROR_MESSAGE)
        ascii_digit_chars.append(str(decimal_value))
    effective_digits = "".join(ascii_digit_chars).lstrip("0") or "0"

    bound = _THREAD_ID_NEGATIVE_BOUND if negative else _THREAD_ID_POSITIVE_BOUND
    if len(effective_digits) > len(bound) or (
        len(effective_digits) == len(bound) and effective_digits > bound
    ):
        raise ThreadIdentifierError(_THREAD_IDENTIFIER_ERROR_MESSAGE)

    magnitude = int(effective_digits)
    return -magnitude if negative else magnitude


def walk_thread(
    database_path: str | Path,
    identifier: int | str,
    *,
    read_only: bool = False,
) -> list[dict[str, object]]:
    """Reconstruct a full conversation thread from a single message identifier.

    Accepts either a numeric ``db_id`` (``int``) or an RFC 822 Message-ID
    string (``str``, with or without angle brackets).  Returns a list of
    ``MessageDetail`` dicts (same shape as :func:`get_message`) ordered by
    ``sent_at`` ascending, capped at 100 messages (earliest by ``sent_at``).

    Algorithm (design.md Decision 1 + 3):
    1. Resolve identifier → read the row's ``thread_key``.
       - Unknown identifier → raises ``KeyError`` (same error as before).
       - Out-of-range numeric identifier → raises
         :class:`ThreadIdentifierError` (change fix-thread-resolver-out-of-
         range-identifier), before any database connection is opened.
       - ``thread_key IS NULL`` (DB not yet backfilled): fall back to the
         row's own ``db_id`` as the key, so a lone message returns itself.
         This is a defensive fallback; normal operation assumes backfill has run.
    2. Resolve the full thread component in ``sent_at`` order with
         :func:`resolve_thread_message_ids`. EXPLAIN QUERY PLAN must show
         ``SEARCH … USING INDEX (idx_messages_thread_key)``.
    3. If the component exceeds 100 messages, emit a truncation warning to
       stderr and return the earliest 100 by ``sent_at``.
    4. Return full ``MessageDetail`` dicts sorted by ``sent_at`` ASC.

    Superseded BFS / ``_BFS_HARD_CEILING`` logic has been removed (Decision 3):
    the indexed ``thread_key`` query takes the complete component's true
    earliest-100, which is strictly more correct for large threads.
    """
    final_ids = resolve_thread_message_ids(
        database_path, identifier, read_only=read_only
    )

    if len(final_ids) > _THREAD_LIMIT:
        print(
            f"[warn] thread exceeded {_THREAD_LIMIT} messages, "
            f"truncated to first {_THREAD_LIMIT} by sent_at",
            file=sys.stderr,
        )
        final_ids = final_ids[:_THREAD_LIMIT]

    # Fetch full MessageDetail dicts (separate connection per get_message
    # is fine for correctness; N ≤ 100 so no perf concern).
    messages = [
        get_message(database_path, db_id, read_only=read_only) for db_id in final_ids
    ]

    return messages


def resolve_thread_message_ids(
    database_path: str | Path,
    identifier: int | str,
    *,
    read_only: bool = False,
) -> list[int]:
    """Resolve a thread identifier to the full stored message-id list.

    Returns all stored message ids in the same thread, ordered by ``sent_at``
    ascending, with no cap applied.

    Raises:
        ThreadIdentifierError: *identifier* is numeric-shaped (``int`` or a
            digit ``str``, with an optional leading ``-``) but outside the
            signed 64-bit range, or a digit-shaped ``str`` character carries
            no Unicode decimal value. Shape classification and this gate run
            entirely before any database connection is opened, so a rejected
            identifier issues no query -- not even against a nonexistent
            database path (design.md > "Gate magnitude inside the shared
            resolver, after shape classification").
        KeyError: *identifier* is well-formed but no matching row exists.
    """
    # Shape classification (unchanged) + magnitude gate both run before the
    # connection opens. The int branch is decided by numeric comparison,
    # never by rendering the value to decimal text -- CPython's digit limit
    # caps int-to-str exactly as it caps parsing (design.md > "Decide
    # magnitude on the string, sign- and zero-normalized").
    resolved_db_id: int | None = None
    clean_id: str | None = None
    if isinstance(identifier, int):
        if not (_THREAD_ID_INT64_MIN <= identifier <= _THREAD_ID_INT64_MAX):
            raise ThreadIdentifierError(_THREAD_IDENTIFIER_ERROR_MESSAGE)
        resolved_db_id = identifier
    else:
        identifier_str = identifier.strip()
        is_db_id = (
            identifier_str.lstrip("-").isdigit()
            and "@" not in identifier_str
            and "<" not in identifier_str
        )
        if is_db_id:
            resolved_db_id = _normalize_thread_db_id_string(identifier_str)
        else:
            clean_id = normalize_message_id(identifier_str)

    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        if resolved_db_id is None:
            found = select_rows_by_message_id(
                connection, clean_id or "", "id, thread_key"
            )
            start_row = min(found, key=lambda r: int(r["id"])) if found else None
        else:
            start_row = connection.execute(
                "SELECT id, thread_key FROM messages WHERE id = ?",
                [resolved_db_id],
            ).fetchone()

        if start_row is None:
            # Echo the normalized db id (not the raw caller string) for db-id
            # lookups: the CLI used to convert before calling, so its not-found
            # text showed the integer form -- keep that byte-for-byte.
            lookup_echo = identifier if resolved_db_id is None else resolved_db_id
            raise KeyError(f"No message found for identifier {lookup_echo!r}")

        thread_key = start_row["thread_key"]
        if thread_key is None:
            return [int(start_row["id"])]

        component_rows = connection.execute(
            "SELECT id FROM messages "
            "WHERE thread_key = ? "
            "ORDER BY COALESCE(unixepoch(sent_at), -62135596800) ASC, id ASC",
            [thread_key],
        ).fetchall()
    finally:
        connection.close()

    return [int(row["id"]) for row in component_rows]


# ---------------------------------------------------------------------------
# FTS MATCH expression builder (Task 3.1 + 3.2)
# ---------------------------------------------------------------------------
# Boolean-aware MATCH expression builder:
#   1. Allow a controlled FTS5 subset: uppercase AND/OR/NOT, parentheses,
#      and double-quoted phrases.
#   2. Keep query-time CJK segmentation aligned with index-time segmentation.
#   3. Quote bareword runs so punctuation such as hyphen remains a separator
#      instead of leaking raw FTS5 syntax.
#
# Empty / whitespace-only keyword → caller should NOT add a keyword condition.
# Non-empty but sanitize-to-zero runs → return sentinel _FTS_MATCH_IMPOSSIBLE
# so caller produces zero rows without error.

_FTS_MATCH_IMPOSSIBLE = '"__impossible_fts5_sentinel_xyzzy__"'
_FTS_OPERATORS = frozenset({"AND", "OR", "NOT"})


def _is_alnum_or_mark(ch: str) -> bool:
    """Return True if *ch* is alphanumeric or a Unicode combining mark."""
    return ch.isalnum() or unicodedata.category(ch).startswith("M")


def _is_word_char(ch: str) -> bool:
    """Alphanumeric or combining mark that ``unicode61`` would fuse into one token.

    CJK is excluded because the index holds it as unigrams (see
    :mod:`.cjk_segmentation`), where a substring hit IS a token hit. Latin
    letters carrying diacritics are NOT excluded: ``é`` fuses into its token
    like any other letter, so ``art`` inside ``cárt`` must not count.
    Combining marks (Unicode category M) are included so decomposed (NFD)
    graphemes remain fused into their base letter token.
    """
    return _is_alnum_or_mark(ch) and not _is_cjk(ch)


def _is_cjk_word_char(ch: str) -> bool:
    """CJK ideograph / fullwidth alphanumeric; excludes CJK punctuation and U+3000.

    ``_is_cjk`` also spans CJK symbols and fullwidth punctuation, which unicode61
    drops at index time. Inside an unquoted bareword run they must act as
    separators (like ASCII ``,`` / ``&``), not glue two terms into one phrase.
    """
    return _is_cjk(ch) and _is_alnum_or_mark(ch)


def _build_fts_match_expr(keyword: str) -> str | None:
    """Build a controlled FTS5 MATCH expression for *keyword*.

    Returns:
        ``None``   – keyword is empty / whitespace-only; caller omits condition.
        ``str``    – a FTS5 MATCH expression (may be the impossible
                     sentinel if all chars were discarded).

    The returned string allows only the documented boolean subset. Invalid
    expressions are intentionally left invalid so SQLite FTS5 can report the
    syntax error and the caller can convert it to a friendly diagnostic.
    """
    if not keyword or not keyword.strip():
        return None

    tokens: list[str] = []

    i = 0
    n = len(keyword)

    def quote_phrase(text: str, *, close: bool = True) -> str:
        escaped = segment_cjk(text).replace('"', '""')
        suffix = '"' if close else ""
        return f'"{escaped}{suffix}'

    while i < n:
        ch = keyword[i]
        if ch.isspace():
            i += 1
        elif ch == '"':
            end = keyword.find('"', i + 1)
            if end == -1:
                tokens.append(quote_phrase(keyword[i + 1 :], close=False))
                break
            tokens.append(quote_phrase(keyword[i + 1 : end]))
            i = end + 1
        elif ch in "()":
            tokens.append(ch)
            i += 1
        elif _is_cjk_word_char(ch):
            run_chars: list[str] = []
            while i < n and _is_cjk_word_char(keyword[i]):
                run_chars.append(keyword[i])
                i += 1
            tokens.append(quote_phrase("".join(run_chars)))
        elif _is_word_char(ch):
            run_chars: list[str] = []
            while i < n and _is_word_char(keyword[i]):
                run_chars.append(keyword[i])
                i += 1
            raw_run = "".join(run_chars)
            if raw_run in _FTS_OPERATORS:
                tokens.append(raw_run)
            else:
                tokens.append(quote_phrase(raw_run))
        else:
            # Separator / punctuation. Prefix search (*) is intentionally
            # deferred; supporting it would require a broader syntax policy.
            # ponytail: if prefix search is needed later, teach this branch to
            # recognize a trailing * only after a validated bareword token.
            i += 1

    if not tokens or all(token in {"(", ")"} for token in tokens):
        # Non-empty keyword but zero valid search tokens, e.g. pure punctuation
        # ":" / "&" / ":&()".
        # → return impossible sentinel so caller gets 0 rows, not all rows.
        return _FTS_MATCH_IMPOSSIBLE

    return " ".join(tokens).replace("( ", "(").replace(" )", ")")


# ---------------------------------------------------------------------------
# Match evidence (issue #177)
# ---------------------------------------------------------------------------
# A filtered result set that cannot say WHY a row matched is indistinguishable
# from an unfiltered one: the reported symptom was a caller concluding the
# keyword was being ignored, when every row did carry the terms -- inside its
# quoted history, past the head-anchored snippet. Two scalars and a
# hit-centred snippet close that gap.

#: Characters of context kept on each side of a hit (issue #177).
_SNIPPET_RADIUS = 120

#: Cap for a hit-centred snippet. Larger than SNIPPET_MAX_CHARS, which stays
#: the cap for the head-anchored snippet returned when there is no keyword:
#: a hit-centred window has to carry both sides of the hit to be worth
#: reading, and the caller asked for this one. Matches the MCP layer's own
#: presentation cap so the value is not trimmed twice with two different
#: ideas of how long a snippet is.
_KEYWORD_SNIPPET_MAX_CHARS = 300


def _keyword_display_terms(keyword: str) -> list[str]:
    """Literal terms of *keyword*, for locating a hit in the stored text.

    A term is a maximal run of characters that are none of: whitespace, a
    double quote, or a parenthesis. A quoted phrase is kept whole, and a run
    equal to a boolean operator is dropped.

    Parentheses are DELIMITERS, not characters to strip off the ends: the FTS
    parser ends a bareword at ``(`` with no whitespace required, so
    ``foo OR(bar)`` is a legal query naming ``foo`` and ``bar``. Stripping
    instead of splitting yields the term ``OR(bar``, which then matches
    nothing, and a row that hit on ``bar`` reports no evidence at all.

    Terms stay EXACTLY as typed -- deliberately unlike
    :func:`_build_fts_match_expr`, which segments CJK and splits ``PEN-100``
    into the two tokens the index actually holds. That split is right for
    matching and wrong for locating: ``100`` on its own hits any quantity in the
    body and would centre the snippet on noise. Filtering has already
    happened by the time these are used; their only job is to find, in the
    raw text, the thing the caller asked about.
    """
    terms: list[str] = []
    i = 0
    n = len(keyword)
    while i < n:
        if keyword[i].isspace():
            i += 1
            continue
        if keyword[i] == '"':
            end = keyword.find('"', i + 1)
            phrase = keyword[i + 1 :] if end == -1 else keyword[i + 1 : end]
            if phrase.strip():
                terms.append(phrase.strip())
            i = n if end == -1 else end + 1
            continue
        if keyword[i] in "()":
            i += 1
            continue
        start = i
        while i < n and not keyword[i].isspace() and keyword[i] not in '"()':
            i += 1
        token = keyword[start:i]
        if token and token not in _FTS_OPERATORS:
            terms.append(token)
    return terms


#: Occurrences examined per term when choosing where to centre the snippet.
#: Only affects which window gets picked, never whether a row matched, so a
#: term repeated past this many times simply stops offering new candidates.
_MAX_OCCURRENCES_PER_TERM = 50


def _at_token_boundary(text: str, start: int, end: int) -> bool:
    """Whether an occurrence spanning ``[start, end)`` is a whole-token hit.

    ``unicode61`` fuses runs of alphanumerics into one token, so a term only
    ever matches whole tokens in the index: a plain substring scan would call
    ``art`` a hit inside ``partial`` and ``15`` a hit inside ``115``, and
    then report ``match_field: body`` for a row that actually matched on its
    recipients. The check is applied per END, and only where that end is a
    word character -- a term ending in punctuation has no token edge to
    protect.

    Known limit in the other direction: ``unicode61`` also folds diacritics,
    so the index treats ``cafe`` and ``café`` as one token while this literal
    scan does not. That direction fails safe -- the row still matched, and
    reports ``match_field: null``, which already means "the hit is not in the
    text shown here". Closing it would mean folding both sides and then
    mapping offsets back through a length-changing normalization, which does
    not pay for itself on a corpus of Chinese and English business mail.
    """
    if _is_word_char(text[start]) and start > 0 and _is_word_char(text[start - 1]):
        return False
    return not (
        _is_word_char(text[end - 1]) and end < len(text) and _is_word_char(text[end])
    )


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Separator-tolerant matcher for one term, mirroring how it is indexed.

    A term is indexed as its runs of alphanumerics, with everything between
    them acting only as a token separator -- so the phrase ``"PEN-100"``
    matches ``PEN 100`` as well as ``PEN-100``. Scanning for the raw string
    would find only the second, and report ``match_field: null`` for the
    first: exactly the "it matched but I can't see why" complaint this whole
    change exists to remove, on the query form the skill actively tells
    callers to use for part numbers.

    Runs are joined by "one or more non-alphanumerics", never zero, because
    ``PEN100`` is a single different token and must not match.
    """
    runs = ["".join(group) for group in _alphanumeric_runs(term)]
    if len(runs) <= 1:
        return re.compile(re.escape(term.lower()))
    return re.compile(r"[\W_]+".join(re.escape(run.lower()) for run in runs))


def _alphanumeric_runs(term: str) -> list[list[str]]:
    runs: list[list[str]] = []
    for ch in term:
        if _is_alnum_or_mark(ch):
            if not runs or not runs[-1]:
                runs.append([ch])
            else:
                runs[-1].append(ch)
        elif runs and runs[-1]:
            runs.append([])
    return [run for run in runs if run]


def _term_offsets(text: str, terms: Sequence[str]) -> dict[str, list[tuple[int, int]]]:
    """Bounded whole-token occurrence spans per term; absent terms omitted.

    Each value is a list of ``(offset, length)`` -- the length is not
    ``len(term)`` because a separator-tolerant match can be longer or shorter
    than the term as typed (``"PEN-100"`` matching ``PEN   100``).
    """
    lowered = text.lower()
    found: dict[str, list[tuple[int, int]]] = {}
    for term in terms:
        if not term:
            continue
        spans: list[tuple[int, int]] = []
        for match in _term_pattern(term).finditer(lowered):
            if len(spans) >= _MAX_OCCURRENCES_PER_TERM:
                break
            start, end = match.span()
            if end > start and _at_token_boundary(lowered, start, end):
                spans.append((start, end - start))
        if spans:
            found[term] = spans
    return found


def _best_hit_in(text: str, terms: Sequence[str]) -> tuple[int, int, int] | None:
    """Best window in one field as ``(coverage, offset, length)``.

    "Best" is the occurrence whose context window would cover the most
    DISTINCT terms, earliest position winning a tie. Earliest occurrence
    alone is the wrong rule, as observed on real reply chains: when a query
    names a part number and a price (say ``PEN-100 15``) in a long reply
    chain, the earliest ``PEN-100`` sits in a boilerplate
    paragraph while the line the caller wants is the one carrying both terms.
    Co-occurrence is what "this is the line you asked about" looks like once a
    query names more than one thing.
    """
    found = _term_offsets(text, terms)
    if not found:
        return None
    best: tuple[int, int, int] | None = None
    for spans in found.values():
        for offset, length in spans:
            low = offset - _SNIPPET_RADIUS
            high = offset + length + _SNIPPET_RADIUS
            coverage = sum(
                1
                for other in found.values()
                if any(low <= candidate <= high for candidate, _ in other)
            )
            if best is None or (-coverage, offset) < (-best[0], best[1]):
                best = (coverage, offset, length)
    return best


def _locate_keyword_hit(
    subject: str, body: str, terms: Sequence[str]
) -> tuple[str, int, int] | None:
    """Where to centre the snippet, across both text fields.

    Coverage is compared BEFORE any field preference: a subject carrying the
    whole query beats a body carrying one stray term, which is what "the
    position covering the most of what was asked for" has to mean if the
    tool's own description is to hold. Body only wins the tie, because its
    surrounding sentence is the more informative context of the two.

    Returns ``(field, offset, length)`` or ``None`` when no term appears in
    either field. ``None`` is a real answer, not a failure: ``message_fts``
    also indexes recipients and attachment names, so a row can legitimately
    match with the literal absent from both text fields.
    """
    # Sort key: most coverage, then body over subject, then earliest.
    candidates: list[tuple[int, int, int, str, int]] = []
    for rank, (field, text) in enumerate((("body", body), ("subject", subject))):
        best = _best_hit_in(text, terms)
        if best is not None:
            coverage, offset, length = best
            candidates.append((-coverage, rank, offset, field, length))
    if not candidates:
        return None
    _, _, offset, field, length = min(candidates)
    return field, offset, length


def _keyword_context(text: str, offset: int, length: int) -> str:
    """Context window centred on a hit, whitespace collapsed to one line."""
    start = max(0, offset - _SNIPPET_RADIUS)
    end = min(len(text), offset + length + _SNIPPET_RADIUS)
    fragment = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def _match_location(body: str, offset: int) -> str:
    """Whether the hit the snippet shows sits in this message's own new
    content or in its quoted history.

    This is the distinction "which message is the original quote?" is made
    of: every reply in a chain carries the terms, but only one of them wrote
    them.

    Reported for the WINNING hit -- the one the snippet is centred on, i.e.
    the position covering the most of what was asked for -- rather than as a
    per-region roll-up over every term. A roll-up saying "some term appears
    on each side" mislabels the very message this exists to find: an
    original quote that itself replies to an enquiry naming the product has
    the whole query in its new content and one stray term in the tail, and
    would be reported as carrying quoted evidence when it is the source.
    Tying the answer to the shown hit also keeps the two consistent -- the
    caller cannot be told `new` while reading a snippet from the tail.

    The boundary comes from :func:`strip_quote_tail` / :func:`_parse_body_quotes`,
    whose conservative fallback is the whole text, so a message with no detectable
    quote boundary reports ``new`` -- which is what "we found no quoted history
    here" should mean.

    *body* must already have ``\\n`` line endings; ``strip_quote_tail``
    normalizes them when it finds a boundary, and comparing offsets across
    the two conventions would misplace the boundary by one character per
    stripped ``\\r``.
    """
    analysis = _parse_body_quotes(body)
    if analysis.stripped_text == body:
        return "new"

    if analysis.hard_cut is not None and offset >= analysis.hard_cut:
        return "quoted"

    if analysis.is_top_posted:
        if analysis.top_posted_cut is not None:
            return "new" if offset < analysis.top_posted_cut else "quoted"
        return "new"

    for lpos, lend, kind in analysis.line_spans:
        if lpos <= offset < lend:
            if kind in ("quote", "attr"):
                return "quoted"
            return "new"

    return "new"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Structured filter set for :func:`search_messages` / :func:`search_attachments`.

    ``sent_from`` / ``sent_to`` are the inclusive send-time bounds, stored
    exactly as the caller passed them -- reading ``filters.sent_from`` /
    ``filters.sent_to`` back always returns that same value, offset-aware
    or naive. The query layer applies the naive-to-Taipei coercion (see
    :func:`mailattic.datetime_bounds.coerce_to_taipei_aware`)
    only when it renders a bound into a SQL parameter, not at construction
    time (openspec/changes/unify-date-filter-timezone/design.md > "The
    query layer exposes no new public function; it applies the naive
    coercion internally when it renders a send-time filter into a SQL
    parameter").

    This is separate from how a *stored* ``sent_at`` value without a UTC
    offset is read: the ingestion parser emits a naive stored timestamp
    only when the original ``Date`` header itself carried no offset, and
    the true local offset of that sender was never captured, so no rule
    can recover it after the fact. Such a stored value is interpreted as
    UTC -- a bounded, best-effort semantics rather than a guess -- because
    that is the reading SQLite's ``unixepoch()`` already applies to a
    naive literal, and assuming Asia/Taipei instead would be wrong for
    any sender outside that timezone (the stored corpus spans 17 distinct
    UTC offsets). See docs/decisions/D-20260723-1-taipei-day-date-filter-semantics.md.
    """

    keyword: str | None = None
    sender: str | None = None
    recipient: str | None = None
    sender_domain: str | list[str] | None = None
    recipient_domain: str | list[str] | None = None
    participants_domain: str | list[str] | None = None
    sent_from: datetime | None = None
    sent_to: datetime | None = None
    attachment_name: str | None = None
    attachment_ext: str | None = None


def search_messages(
    *,
    database_path: str | Path,
    filters: SearchFilters,
    limit: int = 20,
    full_body: bool = False,
    read_only: bool = False,
) -> list[dict[str, object]]:
    if limit < 0:
        raise ValueError("Search limit must be zero or greater.")

    select_columns = [
        "m.id",
        "m.subject",
        "m.sender_name",
        "m.sender_email",
        "m.sent_at",
        "m.received_at",
        "m.pst_source",
        "m.folder_path",
        "m.logical_locator",
        """(
            SELECT group_concat(attachment_name, char(31))
            FROM (
                SELECT attachment_name
                FROM message_attachments
                WHERE message_id = m.id
                ORDER BY id
            )
        ) AS attachment_names_serialized""",
        # Task 5.1: snippet must come from messages original text (m.body_text /
        # m.subject), NOT from snippet(message_fts, ...) which would leak the
        # CJK segmentation whitespace into user-visible output. Selected raw
        # here and rendered in Python (issue #35): the raw prefix of a
        # PST-sourced body is its HTML <style> block, which is zero signal.
        # Deliberately the WHOLE body, not a prefix: capping the fetch
        # lets CSS-junk snippets back in, because a truncated <style> block
        # no longer matches its closing tag and survives rendering. Costs
        # one full body read per summary row; the returned payload is
        # unchanged (payload slimming is #35's deferred side dish B).
        "m.body_text",
    ]
    if full_body:
        select_columns.extend(
            [
                "m.message_id",
                "m.dedup_key",
                "m.in_reply_to",
                "m.references_header",
            ]
        )
    query_parts = [
        "SELECT",
        "    " + ",\n    ".join(select_columns),
        "FROM messages AS m",
    ]
    parameters: list[object] = []
    conditions: list[str] = []

    # Task 3.1 + 3.2: build safe FTS MATCH expression via _build_fts_match_expr.
    keyword_ranked = False
    if filters.keyword:
        match_expr = _build_fts_match_expr(filters.keyword)
        if match_expr is not None:
            # Non-empty keyword (may be impossible sentinel for pure-punctuation)
            query_parts.append("JOIN message_fts ON message_fts.rowid = m.id")
            conditions.append("message_fts MATCH ?")
            parameters.append(match_expr)
            keyword_ranked = True

    _append_search_filter_conditions(
        filters=filters,
        conditions=conditions,
        parameters=parameters,
        attachment_name_sql="m.attachment_search_text",
    )

    if conditions:
        query_parts.append("WHERE " + " AND ".join(conditions))

    recency_order = "COALESCE(unixepoch(m.sent_at), -62135596800) DESC, m.id DESC"
    if keyword_ranked:
        # issue #177: `limit` cuts the candidate set, so ordering decides what
        # a keyword search can find at all -- not merely what order it reads
        # in. Under pure recency the message a quoted price ORIGINATED in is
        # the last one returned, because every later reply carries the same
        # terms in its quoted tail and every later reply is newer. bm25's
        # length normalization is what surfaces it: the short original scores
        # above the long chains repeating it. Recency stays as the tiebreaker,
        # and stays the whole order when there is nothing to rank on.
        query_parts.append(f"ORDER BY bm25(message_fts), {recency_order}")
    else:
        query_parts.append(f"ORDER BY {recency_order}")
    query_parts.append("LIMIT ?")
    parameters.append(limit)

    query = "\n".join(query_parts)
    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        try:
            rows = connection.execute(query, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if filters.keyword and _is_invalid_fts_query(exc):
                raise ValueError(
                    f"Invalid FTS keyword query: {filters.keyword}"
                ) from exc
            raise
        attachments_by_message = _load_attachments_for_messages(
            connection, [int(row["id"]) for row in rows]
        )
        recipients_by_message = (
            _load_recipients_for_messages(connection, [int(row["id"]) for row in rows])
            if full_body
            else {}
        )
        # Resolved once per call, on the same connection, while it is still
        # open (issue #169) -- every row's direction reads this one set.
        self_addresses = resolve_self_addresses(connection)
    finally:
        connection.close()

    keyword_terms = _keyword_display_terms(filters.keyword) if filters.keyword else []

    results: list[dict[str, object]] = []
    for row in rows:
        message_id = int(row["id"])
        rendered_body = render_text(row["body_text"])
        snippet = rendered_body[:SNIPPET_MAX_CHARS]
        match_field: str | None = None
        match_in: str | None = None
        if keyword_terms:
            # Offsets are taken against \n-normalized text so they stay
            # comparable with strip_quote_tail's boundary; rendered_body
            # itself is handed back unchanged, so a full-body result still
            # reads exactly as mail_show renders the same message.
            normalized_body = rendered_body.replace("\r\n", "\n").replace("\r", "\n")
            subject_text = row["subject"] or ""
            hit = _locate_keyword_hit(subject_text, normalized_body, keyword_terms)
            if hit is not None:
                match_field, offset, hit_length = hit
                source = normalized_body if match_field == "body" else subject_text
                snippet = _keyword_context(source, offset, hit_length)[
                    :_KEYWORD_SNIPPET_MAX_CHARS
                ]
                if match_field == "body":
                    match_in = _match_location(normalized_body, offset)
        result: dict[str, object] = {
            "id": row["id"],
            "subject": row["subject"],
            "sender_name": row["sender_name"],
            "sender_email": row["sender_email"],
            "direction": compute_direction(row["sender_email"], self_addresses),
            "sent_at": row["sent_at"],
            "received_at": row["received_at"],
            "pst_source": row["pst_source"],
            "folder_path": row["folder_path"],
            "logical_locator": row["logical_locator"],
            "attachments": attachments_by_message.get(message_id, []),
            "attachment_names": [
                attachment["attachment_name"]
                for attachment in attachments_by_message.get(message_id, [])
            ],
            # Same pipeline as rendered_body_text below, so a search hit and
            # the message it points at read alike (issue #35). Centred on the
            # hit when a keyword located one, head-anchored otherwise
            # (issue #177).
            "snippet": snippet,
            # Why this row matched, as far as the stored text can say.
            # `match_field` is None when the literal appears in neither
            # subject nor body -- the row matched on an indexed field that is
            # not shown here (recipients, attachment names). `match_in` is
            # None unless the hit is in the body.
            "match_field": match_field,
            "match_in": match_in,
        }
        if full_body:
            recipients = recipients_by_message.get(
                message_id, {"to": [], "cc": [], "bcc": []}
            )
            result.update(
                {
                    "rendered_body_text": rendered_body,
                    "message_id": row["message_id"],
                    "dedup_key": row["dedup_key"],
                    "in_reply_to": row["in_reply_to"],
                    "references": row["references_header"],
                    "to": recipients["to"],
                    "cc": recipients["cc"],
                    "bcc": recipients["bcc"],
                }
            )
        results.append(result)
    return results


def search_attachments(
    *,
    database_path: str | Path,
    filters: SearchFilters,
    dedupe: str | None = None,
    read_only: bool = False,
) -> list[dict[str, object]]:
    if dedupe not in {None, "content-hash"}:
        raise ValueError(f"Unsupported attachment dedupe mode: {dedupe}")

    query_parts = [
        "SELECT",
        "    ma.id AS attachment_id,",
        "    m.id AS message_db_id,",
        "    m.message_id AS rfc_message_id,",
        "    m.dedup_key,",
        "    m.sent_at,",
        "    COALESCE(unixepoch(m.sent_at), -62135596800) AS sent_at_epoch,",
        "    m.sender_email,",
        "    m.subject,",
        "    ma.attachment_name,",
        "    ma.size_bytes,",
        "    ma.mime_type,",
        "    ma.content_hash,",
        "    ma.storage_path,",
        "    m.folder_path",
        "FROM message_attachments AS ma",
        "JOIN messages AS m ON m.id = ma.message_id",
    ]
    parameters: list[object] = []
    conditions: list[str] = []

    _append_search_filter_conditions(
        filters=filters,
        conditions=conditions,
        parameters=parameters,
        attachment_name_sql="COALESCE(ma.attachment_name, '')",
    )

    attachment_ext = _normalize_attachment_ext(filters.attachment_ext)
    if attachment_ext is not None:
        extension_conditions = [
            "LOWER(RTRIM(TRIM(COALESCE(ma.attachment_name, '')), '.')) "
            "LIKE ? ESCAPE '\\'"
        ]
        extension_parameters: list[object] = [f"%{_escape_like(attachment_ext)}"]
        inferred_mime_type = mimetypes.guess_type(
            f"attachment{attachment_ext}", strict=False
        )[0]
        if inferred_mime_type is not None:
            extension_conditions.append(
                "(attachment_name_extension(ma.attachment_name) = '' "
                "AND LOWER(COALESCE(ma.mime_type, '')) = ?)"
            )
            extension_parameters.append(inferred_mime_type.lower())
        conditions.append("(" + " OR ".join(extension_conditions) + ")")
        parameters.extend(extension_parameters)

    if conditions:
        query_parts.append("WHERE " + " AND ".join(conditions))

    query_parts.append(
        "ORDER BY COALESCE(unixepoch(m.sent_at), -62135596800) DESC, "
        "m.id DESC, ma.id ASC"
    )

    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        if attachment_ext is not None:
            connection.create_function(
                "attachment_name_extension",
                1,
                _attachment_name_extension,
                deterministic=True,
            )
        rows = connection.execute("\n".join(query_parts), parameters).fetchall()
    finally:
        connection.close()

    results = [
        {
            "attachment_id": row["attachment_id"],
            "message_db_id": row["message_db_id"],
            "rfc_message_id": row["rfc_message_id"],
            "dedup_key": row["dedup_key"],
            "sent_at": row["sent_at"],
            "sent_at_epoch": row["sent_at_epoch"],
            "sender_email": row["sender_email"],
            "subject": row["subject"],
            "attachment_name": row["attachment_name"],
            "size_bytes": row["size_bytes"],
            "mime_type": row["mime_type"],
            "content_hash": row["content_hash"],
            "storage_path": row["storage_path"],
            "folder_path": row["folder_path"],
            "exportable": row["storage_path"] is not None,
            "carrying_messages": [_message_summary_from_attachment_row(row)],
        }
        for row in rows
    ]

    if dedupe == "content-hash":
        results = _dedupe_attachment_results(results)

    # sent_at_epoch only exists to give the dedupe sort keys below an
    # absolute-instant comparison (same unixepoch() convention as every
    # other ordering in this module) instead of comparing sent_at strings
    # lexically across offsets -- it is not part of the public row shape.
    for result in results:
        result.pop("sent_at_epoch", None)
        for message in result["carrying_messages"]:
            message.pop("sent_at_epoch", None)
    return results


def get_attachments_by_ids(
    database_path: str | Path,
    attachment_ids: list[int],
    *,
    read_only: bool = False,
) -> list[dict[str, object]]:
    """Return the export-relevant fields for each existing attachment id.

    Ids with no stored attachment are simply absent from the result; the
    caller decides how to report them. Order is not guaranteed.
    """
    if not attachment_ids:
        return []
    placeholders = ", ".join("?" for _ in attachment_ids)
    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        rows = connection.execute(
            f"""
            SELECT
                ma.id AS attachment_id,
                m.sent_at,
                ma.attachment_name,
                ma.size_bytes,
                ma.mime_type,
                ma.content_hash,
                ma.storage_path
            FROM message_attachments AS ma
            JOIN messages AS m ON m.id = ma.message_id
            WHERE ma.id IN ({placeholders})
            """,
            attachment_ids,
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def get_message(
    database_path: str | Path,
    stored_message_id: int,
    *,
    read_only: bool = False,
) -> dict[str, object]:
    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        row = connection.execute(
            """
            SELECT
                id,
                subject,
                sender_name,
                sender_email,
                sent_at,
                received_at,
                body_text,
                message_id,
                in_reply_to,
                references_header,
                pst_source,
                folder_path,
                logical_locator,
                source_input_mode,
                source_requested_from,
                raw_headers
            FROM messages
            WHERE id = ?
            """,
            [stored_message_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"Stored message id {stored_message_id} was not found.")

        recipient_map = _load_recipients_for_messages(
            connection, [stored_message_id]
        ).get(stored_message_id, {"to": [], "cc": [], "bcc": []})
        attachments_list = _load_attachments_for_messages(
            connection, [stored_message_id]
        ).get(stored_message_id, [])
        # Same connection, still open (issue #169) -- resolved once per call.
        self_addresses = resolve_self_addresses(connection)
    finally:
        connection.close()

    return {
        "id": row["id"],
        "subject": row["subject"],
        "sender_name": row["sender_name"],
        "sender_email": row["sender_email"],
        "direction": compute_direction(row["sender_email"], self_addresses),
        "sent_at": row["sent_at"],
        "received_at": row["received_at"],
        "body_text": row["body_text"],
        "message_id": row["message_id"],
        "in_reply_to": row["in_reply_to"],
        "references": row["references_header"],
        "pst_source": row["pst_source"],
        "folder_path": row["folder_path"],
        "logical_locator": row["logical_locator"],
        "source_input_mode": row["source_input_mode"],
        "source_requested_from": row["source_requested_from"],
        "raw_headers": row["raw_headers"],  # None when column not yet backfilled
        "to": recipient_map["to"],
        "cc": recipient_map["cc"],
        "bcc": recipient_map["bcc"],
        "attachments": attachments_list,
        "attachment_names": [
            attachment["attachment_name"] for attachment in attachments_list
        ],
    }


def get_sync_status(
    database_path: str | Path,
    *,
    read_only: bool = False,
) -> dict[str, object]:
    """Return the four sync-freshness facts the MCP status tool reports.

    Reads the single ``sync_state`` row written by
    :func:`mailattic.storage.record_sync_run_start` /
    :func:`mailattic.storage.record_sync_run_end`, together
    with the newest stored message timestamp. The newest-message timestamp
    is derived at query time from ``messages`` rather than a stored counter
    (design.md > "Report four flat fields and compute no verdict": "The
    newest-message timestamp is derived at query time from the stored
    messages, not maintained as a separate counter").

    When no ``sync_state`` row exists yet, the outcome is reported as
    ``never_synced`` with null run timestamps rather than raising or
    returning nothing (design.md's Implementation Contract table).

    A database predating this feature has no ``sync_state`` table at all,
    and is reported the same way. That is not a degenerate case: the MCP
    server opens read-only and can never create the table, so until the
    next sync run does, "the table is absent" and "the row is absent" are
    the same fact -- no sync state exists -- and both mean ``never_synced``.
    """
    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        has_sync_state = (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ["sync_state"],
            ).fetchone()
            is not None
        )
        row = (
            connection.execute(
                "SELECT started_at, ended_at, outcome FROM sync_state "
                "WHERE sync_key = ?",
                [SYNC_STATE_KEY],
            ).fetchone()
            if has_sync_state
            else None
        )
        # Ordered the same way every other sent_at ordering in this module is:
        # unixepoch() parses the offset, lexical comparison does not. The
        # stored sent_at values carry mixed offsets (not just +08:00),
        # plus rows with no offset at all,
        # so MAX() over the raw text returns the wrong message.
        latest_row = connection.execute(
            "SELECT sent_at AS latest_sent_at FROM messages "
            "ORDER BY COALESCE(unixepoch(sent_at), -62135596800) DESC, id DESC "
            "LIMIT 1"
        ).fetchone()
    finally:
        connection.close()

    latest_message_at = latest_row["latest_sent_at"] if latest_row else None

    # One return, so the delivered key set is written once: a second copy of
    # these four keys is the same drift risk the MCP layer assembles field by
    # field to avoid.
    if row is None:
        started_at = ended_at = None
        outcome = "never_synced"
    else:
        started_at = row["started_at"]
        ended_at = row["ended_at"]
        outcome = row["outcome"]

    return {
        "last_sync_started_at": started_at,
        "last_sync_ended_at": ended_at,
        "last_sync_status": outcome,
        "latest_message_at": latest_message_at,
    }


def _load_attachments_for_messages(
    connection: sqlite3.Connection, message_ids: list[int]
) -> dict[int, list[dict[str, object]]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = connection.execute(
        f"""
        SELECT
            message_id,
            id,
            attachment_name,
            content_hash,
            size_bytes,
            mime_type,
            storage_path
        FROM message_attachments
        WHERE message_id IN ({placeholders})
        ORDER BY message_id, id
        """,
        message_ids,
    ).fetchall()
    grouped: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(int(row["message_id"]), []).append(
            {
                "id": row["id"],
                "attachment_name": row["attachment_name"],
                "content_hash": row["content_hash"],
                "size_bytes": row["size_bytes"],
                "mime_type": row["mime_type"],
                "storage_path": row["storage_path"],
            }
        )
    return grouped


def _load_recipients_for_messages(
    connection: sqlite3.Connection, message_ids: list[int]
) -> dict[int, dict[str, list[dict[str, str | None]]]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = connection.execute(
        f"""
        SELECT message_id, recipient_type, display_name, email
        FROM message_recipients
        WHERE message_id IN ({placeholders})
        ORDER BY message_id, id
        """,
        message_ids,
    ).fetchall()
    grouped: dict[int, dict[str, list[dict[str, str | None]]]] = {}
    for row in rows:
        buckets = grouped.setdefault(
            int(row["message_id"]), {"to": [], "cc": [], "bcc": []}
        )
        recipient_type = row["recipient_type"]
        if recipient_type not in buckets:
            continue
        buckets[recipient_type].append(
            {
                "name": row["display_name"],
                "email": row["email"],
            }
        )
    return grouped


def _is_invalid_fts_query(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "fts5: syntax error",
            "malformed match expression",
            "unterminated string",
        )
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_like_pattern(value: str) -> str:
    return f"%{_escape_like(value)}%"


def _append_search_filter_conditions(
    *,
    filters: SearchFilters,
    conditions: list[str],
    parameters: list[object],
    attachment_name_sql: str,
) -> None:
    sender_email_sql = "m.sender_email"
    sender_name_sql = "m.sender_name"
    recipient_sql = "m.recipient_search_text"
    sent_at_sql = "m.sent_at"
    if filters.sender:
        conditions.append(
            f"({sender_email_sql} LIKE ? ESCAPE '\\' "
            f"OR {sender_name_sql} LIKE ? ESCAPE '\\')"
        )
        wildcard = _to_like_pattern(filters.sender)
        parameters.extend([wildcard, wildcard])

    if filters.recipient:
        conditions.append(f"{recipient_sql} LIKE ? ESCAPE '\\'")
        parameters.append(_to_like_pattern(filters.recipient))

    if filters.attachment_name:
        conditions.append(f"{attachment_name_sql} LIKE ? ESCAPE '\\'")
        parameters.append(_to_like_pattern(filters.attachment_name))

    _sender_domains = _normalize_domain_filter(filters.sender_domain)
    if _sender_domains:
        or_clauses = " OR ".join(
            sender_domain_sql(sender_email_sql) for _ in _sender_domains
        )
        conditions.append(f"({or_clauses})")
        for domain in _sender_domains:
            parameters.extend(sender_domain_params(domain))

    _recipient_domains = _normalize_domain_filter(filters.recipient_domain)
    if _recipient_domains:
        or_clauses = " OR ".join(
            f"recipient_domain_match({recipient_sql}, ?)" for _ in _recipient_domains
        )
        conditions.append(f"({or_clauses})")
        parameters.extend(_recipient_domains)

    _participants_domains = _normalize_domain_filter(filters.participants_domain)
    if _participants_domains:
        or_clauses = " OR ".join(
            f"{sender_domain_sql(sender_email_sql)} "
            f"OR recipient_domain_match({recipient_sql}, ?)"
            for _ in _participants_domains
        )
        conditions.append(f"({or_clauses})")
        for domain in _participants_domains:
            parameters.extend(sender_domain_params(domain))
            parameters.append(domain)

    if filters.sent_from:
        # NOT redundant with the CLI/MCP callers already passing aware values:
        # this is the only thing giving an externally-supplied naive datetime
        # a defined meaning; deleting it silently returns the Python query
        # API to a UTC reading.
        conditions.append(f"unixepoch({sent_at_sql}) >= unixepoch(?)")
        parameters.append(coerce_to_taipei_aware(filters.sent_from).isoformat())

    if filters.sent_to:
        conditions.append(f"unixepoch({sent_at_sql}) <= unixepoch(?)")
        parameters.append(coerce_to_taipei_aware(filters.sent_to).isoformat())


def _normalize_domain_filter(
    value: str | list[str] | None,
) -> list[str]:
    """Normalize a domain filter value to a list of non-empty domain strings.

    - ``None`` → ``[]`` (filter not set)
    - ``str`` → ``[str]`` if non-empty after strip, else ``[]``
    - ``list[str]`` → stripped, empty-after-strip values discarded
    """
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    return [v.strip() for v in value if v.strip()]


def sender_domain_sql(column: str) -> str:
    """Sender-domain condition. Plain address rows keep the single suffix LIKE;
    only packed values (contain ``<``) that also contain the domain reach the
    Python verifier. Params: :func:`sender_domain_params`."""
    return (
        f"({column} LIKE ? ESCAPE '\\' "
        f"OR ({column} LIKE '%<%' AND {column} LIKE ? ESCAPE '\\' "
        f"AND sender_domain_match({column}, ?)))"
    )


def sender_domain_params(domain: str) -> list[object]:
    bare = domain.lstrip("@")
    return [_to_domain_suffix_pattern(domain), f"%@{_escape_like(bare)}%", bare]


def _to_domain_suffix_pattern(domain: str) -> str:
    return f"%@{_escape_like(domain.lstrip('@'))}"


def _normalize_attachment_ext(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().lower()
    if not stripped:
        return None
    return stripped if stripped.startswith(".") else f".{stripped}"


def _attachment_name_extension(value: object) -> str:
    candidate = Path(str(value or "").strip().rstrip("."))
    if not candidate.name or not candidate.suffix or not candidate.stem:
        return ""
    return candidate.suffix.lower()


def _message_summary_from_attachment_row(
    row: dict[str, object],
) -> dict[str, object]:
    return {
        "message_db_id": row["message_db_id"],
        "rfc_message_id": row["rfc_message_id"],
        "dedup_key": row["dedup_key"],
        "sent_at": row["sent_at"],
        "sent_at_epoch": row["sent_at_epoch"],
        "sender_email": row["sender_email"],
        "subject": row["subject"],
    }


def _attachment_result_sort_key(result: dict[str, object]) -> tuple[int, int, int]:
    # Absolute instant, matching the unixepoch() ordering convention every
    # other query in this module uses -- see search_attachments' own ORDER
    # BY. Comparing sent_at strings lexically picks the wrong winner across
    # UTC offsets (issue #193).
    return (
        int(result["sent_at_epoch"]),
        int(result["message_db_id"]),
        -int(result["attachment_id"]),
    )


def _dedupe_attachment_results(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    passthrough: list[dict[str, object]] = []
    grouped: dict[str, list[dict[str, object]]] = {}

    for row in rows:
        content_hash = row["content_hash"]
        if content_hash is None:
            passthrough.append(row)
            continue
        grouped.setdefault(str(content_hash), []).append(row)

    deduped: list[dict[str, object]] = list(passthrough)
    for group_rows in grouped.values():
        winner = max(group_rows, key=_attachment_result_sort_key)
        carrying_messages: dict[int, dict[str, object]] = {}
        for group_row in group_rows:
            message_summary = _message_summary_from_attachment_row(group_row)
            carrying_messages.setdefault(
                int(message_summary["message_db_id"]),
                message_summary,
            )
        winner_row = dict(winner)
        winner_row["carrying_messages"] = sorted(
            carrying_messages.values(),
            key=lambda m: (int(m["sent_at_epoch"]), int(m["message_db_id"])),
            reverse=True,
        )
        deduped.append(winner_row)

    return sorted(deduped, key=_attachment_result_sort_key, reverse=True)
