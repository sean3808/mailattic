"""CJK unigram segmentation — single source of truth for both index and query.

Design contract (design.md "Decisions > app 層 unigram 切分"):

* Scan input character by character.
* Insert a single space between two adjacent alphanumeric characters when at
  least one of them is a CJK ideograph. This isolates every CJK ideograph into
  its own ``unicode61`` token even when it is glued to ASCII letters or digits
  (e.g. ``"2026年"`` → ``"2026 年"``, ``"PDF檔"`` → ``"PDF 檔"``), which is what
  makes a CJK substring keyword findable inside a mixed token.
* ``unicode61`` only fuses adjacent alphanumeric-class characters into one
  token, so no space is needed at a CJK↔punctuation or CJK↔whitespace boundary —
  those are already token boundaries. Email addresses, domain names, and pure
  ASCII alphanumeric tokens contain no CJK and are therefore preserved exactly.

CJK codepoint ranges (frozen in design.md "Open Questions > CJK 判定 Unicode 區間"):

  U+4E00  – U+9FFF   CJK Unified Ideographs (core block)
  U+3400  – U+4DBF   CJK Unified Ideographs Extension A
  U+F900  – U+FAFF   CJK Compatibility Ideographs
  U+3000  – U+303F   CJK Symbols and Punctuation (full-width, expression marks)
  U+FF00  – U+FFEF   Halfwidth and Fullwidth Forms (includes CJK-related full-width)
  U+20000 – U+2FFFF  CJK Unified Ideographs Extension B and above (Supplementary)

Boundary rule: a space is inserted between two adjacent alphanumeric
characters when at least one is CJK (CJK↔CJK and CJK↔ASCII-alphanumeric).
Boundaries where either side is punctuation or whitespace, and pure non-CJK
boundaries, are left unchanged (``unicode61`` already tokenizes those).
"""

from __future__ import annotations


def _is_cjk(ch: str) -> bool:
    """Return True if *ch* falls within the CJK codepoint ranges in design.md."""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF  # Extension A
        or 0xF900 <= cp <= 0xFAFF  # Compatibility Ideographs
        or 0x3000 <= cp <= 0x303F  # CJK Symbols and Punctuation
        or 0xFF00 <= cp <= 0xFFEF  # Halfwidth and Fullwidth Forms
        or cp >= 0x20000  # Extension B and above (Supplementary CJK)
    )


def segment_cjk(text: str | None) -> str:
    """Isolate every CJK ideograph into its own ``unicode61`` token.

    A space is inserted between two adjacent alphanumeric characters when at
    least one is a CJK ideograph. This separates CJK ideographs both from each
    other and from glued ASCII letters/digits (``"2026年"`` → ``"2026 年"``),
    so a CJK substring keyword can match content where the ideograph is fused to
    ASCII. Punctuation/whitespace boundaries and pure non-CJK boundaries are
    left untouched — ``unicode61`` already tokenizes those. The function is
    idempotent: a second application inserts no new spaces.

    Args:
        text: Input string, or ``None``.  ``None`` is treated as empty string.

    Returns:
        The segmented string, with single spaces inserted only at
        alphanumeric boundaries that involve a CJK ideograph.

    Examples::

        >>> segment_cjk("ACME: 範例發票&對帳單")
        'ACME: 範 例 發 票&對 帳 單'
        >>> segment_cjk("2026年度")
        '2026 年 度'
        >>> segment_cjk("a@b.com")
        'a@b.com'
        >>> segment_cjk("mailattic.test")
        'mailattic.test'
        >>> segment_cjk("發票")
        '發 票'
        >>> segment_cjk("")
        ''
        >>> segment_cjk(None)
        ''
    """
    if not text:
        return ""

    result: list[str] = []
    last_index = len(text) - 1
    for i, ch in enumerate(text):
        result.append(ch)
        if i >= last_index:
            continue
        nxt = text[i + 1]
        # unicode61 only fuses adjacent alphanumeric-class characters into a
        # single token, so a space is needed exactly when both sides are
        # alphanumeric and at least one is a CJK ideograph. This isolates the
        # ideograph even when glued to ASCII letters/digits. CJK↔punctuation
        # and CJK↔whitespace boundaries are already token boundaries.
        if ch.isalnum() and nxt.isalnum() and (_is_cjk(ch) or _is_cjk(nxt)):
            result.append(" ")
    return "".join(result)
