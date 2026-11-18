"""Quote an original message's own HTML in a reply (#136).

A reply used to quote the stored ``body_text``: the sender's plain-text
alternative or the flattened HTML, which reads as a text dump in Outlook and
makes the customer's own earlier message look edited. The fix quotes the
original as it arrived (the RFC822 bytes fetched on demand; nothing is stored,
D-20261105-1), with its inline ``cid:`` images.

Sanitising, ``cid:`` collection, and the fail-closed post-condition gate all
share one explicit **render view** (:func:`_build_render_view`):

- Live markup in the HTML document.
- The inner HTML of every downlevel-hidden conditional comment
  (``<!--[if ...]>...<![endif]-->``, whatever the condition), parsed recursively
  as a sub-document.
- Downlevel-revealed conditional sections (``<![if ...]>...<![endif]>``), whose
  body is already live markup.
- Ordinary HTML comments (not starting with ``[if``) are outside the render
  view: they are neither sanitised nor scanned for ``cid:``.

Conditional comments whose inner HTML carries no active content are kept
byte-for-byte so Word VML details are not rewritten. After sanitising and
attaching ``cid:`` image parts, :func:`verify_quoted_html` re-parses the final
HTML string written into the draft, rebuilds the render view, and fails closed
to stored text if any active content, unattached ``cid:``, or unclassifiable
structure (unclosed/nested conditional comments, unknown ``<!`` declarations)
remains.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

from bs4 import BeautifulSoup, CData, Comment, Declaration, ProcessingInstruction

from .parser import _MAIL_POLICY, _detect_and_decode, _without_lone_surrogates

QUOTE_NOTICE_BY_CATEGORY: dict[str, str] = {
    "no_html": (
        "the original was plain text, so the quote reads the same as what they sent"
    ),
    "temporary": (
        "the customer will see their earlier mail as flattened text "
        "(lost images, tables as | text); send as is, regenerate the draft "
        "later, or reply from Outlook"
    ),
    "settings": (
        "the customer will see their earlier mail as flattened text; "
        "fix the send settings, then regenerate the draft"
    ),
    "permanent": (
        "the customer will see their earlier mail as flattened text "
        "(lost images, tables as | text); regenerating will not change this"
    ),
    "unquotable": (
        "the customer will see their earlier mail as flattened text; "
        "the original's HTML could not be quoted safely, and regenerating "
        "gives the same result"
    ),
}


def quote_notice_for_category(category: str) -> str:
    """Return the user-facing fallback notice sentence for a reason category."""
    return QUOTE_NOTICE_BY_CATEGORY.get(category, QUOTE_NOTICE_BY_CATEGORY["temporary"])


class OriginalUnavailable(Exception):
    """The original could not be fetched; the message is the user-facing reason."""

    def __init__(self, reason: str, *, category: str = "temporary") -> None:
        super().__init__(reason)
        self.category = category


class _UnclassifiableHtml(Exception):
    """Raised when the render view encounters HTML it cannot safely classify."""


@dataclass(frozen=True, slots=True)
class InlinePart:
    """An inline image (or other part) the quoted HTML references by ``cid:``."""

    content_id: str  # without angle brackets
    maintype: str
    subtype: str
    payload: bytes
    filename: str | None


@dataclass(frozen=True, slots=True)
class QuotableOriginal:
    html_fragment: str
    inline_parts: tuple[InlinePart, ...]
    # ``cid:`` references with no matching part in the original (as written).
    unresolved_cids: tuple[str, ...] = ()
    # Non-None when the fail-closed post-condition gate rejected the quote.
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ConditionalSubdoc:
    comment: Comment
    prefix: str
    inner: str
    suffix: str
    soup: BeautifulSoup


@dataclass(frozen=True, slots=True)
class _RenderView:
    root: BeautifulSoup
    conditional_subdocs: tuple[_ConditionalSubdoc, ...]

    def documents(self) -> tuple[BeautifulSoup, ...]:
        return (self.root, *(sub.soup for sub in self.conditional_subdocs))


_ACTIVE_TAGS = (
    "meta",
    "script",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "applet",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "base",
    "link",
)
_LINK_ATTRIBUTES = ("href", "xlink:href", "action", "formaction")
_SCRIPT_URL = re.compile(r"^\s*(?:javascript|vbscript)\s*:", re.IGNORECASE)
_DATA_URL = re.compile(r"^\s*data\s*:", re.IGNORECASE)
_ACTIVE_STYLE = re.compile(
    r"expression\s*\(|(?:javascript|vbscript)\s*:|@import\b", re.IGNORECASE
)
_CID_REFERENCE = re.compile(r"cid:([^\"'\s)>]+)", re.IGNORECASE)

_HIDDEN_COND_RE = re.compile(
    r"^(\[if\b[^\]]*\]>)(.*)(<!\[endif\])$", re.DOTALL | re.IGNORECASE
)
_REVEALED_OPEN_RE = re.compile(r"^\s*\[if\b[^\]]*\]\s*$", re.IGNORECASE)
_REVEALED_CLOSE_RE = re.compile(r"^\s*\[endif\]\s*$", re.IGNORECASE)
_COND_MARKER_RE = re.compile(r"^\s*\[if\b|<!\[endif\]|\[endif\]", re.IGNORECASE)
_NESTED_COND_RE = re.compile(r"\[if\b|<!\[endif\]|\[endif\]", re.IGNORECASE)
_REVEALED_DECL_PREFIX_RE = re.compile(r"<!\[(?:if\b|endif\])", re.IGNORECASE)


class _UnknownDeclScanner(HTMLParser):
    """Detect unknown ``<!`` declarations and ``<![CDATA[`` sections."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found: str | None = None

    def parse_bogus_comment(self, i: int, report: int = 1) -> int:
        if self.found is None:
            snippet = self.rawdata[i : i + 24]
            if not _REVEALED_DECL_PREFIX_RE.match(snippet):
                self.found = snippet
        return super().parse_bogus_comment(i, report)

    def unknown_decl(self, data: str) -> None:
        if self.found is None:
            self.found = data[:24]


def _scan_unknown_declarations(html_text: str) -> None:
    scanner = _UnknownDeclScanner()
    scanner.feed(html_text)
    scanner.close()
    if scanner.found is not None:
        raise _UnclassifiableHtml(f"unknown declaration {scanner.found!r}")


def _is_conditional_comment_node(node: Comment) -> bool:
    text = str(node)
    return bool(
        _HIDDEN_COND_RE.match(text)
        or _REVEALED_OPEN_RE.match(text)
        or _REVEALED_CLOSE_RE.match(text)
    )


def _check_subdoc_nodes(soup: BeautifulSoup) -> None:
    for node in soup.descendants:
        if isinstance(node, (CData, Declaration, ProcessingInstruction)):
            raise _UnclassifiableHtml(f"unsupported {type(node).__name__.lower()}")
        if isinstance(node, Comment) and _COND_MARKER_RE.search(str(node)):
            raise _UnclassifiableHtml("nested conditional comment")


def _build_render_view(html_text: str) -> _RenderView:
    """Parse ``html_text`` into its render view: live markup + conditional comments.

    Ordinary HTML comments (not starting with ``[if``) are excluded.
    Raises :class:`_UnclassifiableHtml` for unclosed or nested conditional
    comments or unknown ``<!`` declarations.
    """
    _scan_unknown_declarations(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    subdocs: list[_ConditionalSubdoc] = []
    revealed_depth = 0
    for node in soup.descendants:
        if isinstance(node, (CData, Declaration, ProcessingInstruction)):
            raise _UnclassifiableHtml(f"unsupported {type(node).__name__.lower()}")
        if not isinstance(node, Comment):
            continue
        text = str(node)
        hidden = _HIDDEN_COND_RE.match(text)
        if hidden is not None:
            prefix, inner, suffix = hidden.groups()
            if _NESTED_COND_RE.search(inner):
                raise _UnclassifiableHtml("nested conditional comment")
            _scan_unknown_declarations(inner)
            inner_soup = BeautifulSoup(inner, "html.parser")
            _check_subdoc_nodes(inner_soup)
            subdocs.append(
                _ConditionalSubdoc(
                    comment=node,
                    prefix=prefix,
                    inner=inner,
                    suffix=suffix,
                    soup=inner_soup,
                )
            )
            continue
        if _REVEALED_OPEN_RE.match(text):
            revealed_depth += 1
            if revealed_depth > 1:
                raise _UnclassifiableHtml("nested conditional comment")
            continue
        if _REVEALED_CLOSE_RE.match(text):
            revealed_depth -= 1
            if revealed_depth < 0:
                raise _UnclassifiableHtml("unmatched conditional comment endif")
            continue
        if _COND_MARKER_RE.search(text):
            raise _UnclassifiableHtml("unclosed conditional comment")
    if revealed_depth != 0:
        raise _UnclassifiableHtml("unclosed conditional comment")
    return _RenderView(root=soup, conditional_subdocs=tuple(subdocs))


def quotable_original_from_rfc822(rfc822: bytes) -> QuotableOriginal | None:
    """Return the original's sanitised HTML body and its referenced inline parts.

    ``None`` when the original has no HTML body (a plain-text original already
    reads the same as the stored text).
    """
    message = BytesParser(policy=_MAIL_POLICY).parsebytes(rfc822)
    if not isinstance(message, EmailMessage):
        return None
    html_part = message.get_body(preferencelist=("html",))
    if html_part is None:
        return None
    raw = html_part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return None
    # Same decoding as ingest, so mislabelled Big5 and friends read correctly.
    source_html = _without_lone_surrogates(
        _detect_and_decode(raw, html_part.get_content_charset())
    )
    fragment, referenced = _sanitised_fragment(source_html)
    inline_parts = _referenced_inline_parts(message, referenced)
    attached_images = {
        part.content_id.casefold() for part in inline_parts if part.maintype == "image"
    }
    rejection = verify_quoted_html(fragment, attached_images)
    unresolved = tuple(
        sorted(cid for cid in referenced if cid.casefold() not in attached_images)
    )
    return QuotableOriginal(
        html_fragment=fragment,
        inline_parts=inline_parts,
        unresolved_cids=unresolved,
        rejection_reason=rejection,
    )


def _sanitise_soup(soup: BeautifulSoup) -> tuple[bool, set[str]]:
    """Remove active content from ``soup`` and collect ``cid:`` references."""
    changed = False
    for tag in soup.find_all(_ACTIVE_TAGS):
        tag.decompose()
        changed = True
    referenced: set[str] = set()
    for tag in soup.find_all(True):
        for name in list(tag.attrs):
            value = tag.attrs[name]
            if _is_active_attribute(name.casefold(), value):
                del tag.attrs[name]
                changed = True
            else:
                referenced.update(_extract_cids_from_attr(value))
        if tag.name == "style":
            style_text = tag.get_text()
            if _ACTIVE_STYLE.search(style_text):
                tag.decompose()
                changed = True
            elif style_text:
                referenced.update(_CID_REFERENCE.findall(style_text))
    return changed, referenced


def _sanitised_fragment(source_html: str) -> tuple[str, set[str]]:
    """The quotable HTML and the ``cid:`` ids it references.

    Uses :func:`_build_render_view` so sanitising and ``cid:`` collection share
    the exact same view of live markup and conditional comments.
    """
    try:
        view = _build_render_view(source_html)
    except _UnclassifiableHtml:
        return source_html, set()

    _, referenced = _sanitise_soup(view.root)
    for subdoc in view.conditional_subdocs:
        sub_changed, sub_cids = _sanitise_soup(subdoc.soup)
        referenced.update(sub_cids)
        if sub_changed:
            subdoc.comment.replace_with(
                Comment(f"{subdoc.prefix}{subdoc.soup}{subdoc.suffix}")
            )

    body = view.root.body
    if body is None:
        return str(view.root), referenced
    # Styles and Outlook conditional comments usually live in <head>; they
    # belong to the quote, so they move with the body content, in order.
    styles = "".join(
        node.output_ready() if isinstance(node, Comment) else str(node)
        for node in view.root.descendants
        if (
            getattr(node, "name", None) == "style"
            or (isinstance(node, Comment) and _is_conditional_comment_node(node))
        )
        and node.find_parent("body") is None
        and node.find_parent("style") is None
    )
    body_style = body.get("style")
    style_attr = (
        f' style="{html.escape(body_style, quote=True)}"'
        if isinstance(body_style, str)
        else ""
    )
    return f"{styles}<div{style_attr}>{body.decode_contents()}</div>", referenced


def verify_quoted_html(html_text: str, attached_cids: Iterable[str]) -> str | None:
    """Fail-closed post-condition gate on the final HTML written into a draft.

    Re-parses ``html_text``, builds the render view, and returns ``None`` only
    when (a) no active content appears anywhere in the render view and (b)
    every ``cid:`` in the render view maps to an attached image part. Otherwise
    returns a user-facing rejection reason.
    """
    try:
        view = _build_render_view(html_text)
    except _UnclassifiableHtml as exc:
        return f"original HTML has unsupported structure: {exc}"

    cids_in_view: set[str] = set()
    for doc in view.documents():
        for tag in doc.find_all(True):
            if tag.name.casefold() in _ACTIVE_TAGS:
                return f"original HTML retained active tag <{tag.name}>"
            for name, value in tag.attrs.items():
                if _is_active_attribute(name.casefold(), value):
                    return f"original HTML retained active attribute {name}"
                cids_in_view.update(_extract_cids_from_attr(value))
            if tag.name == "style":
                style_text = tag.get_text()
                if _ACTIVE_STYLE.search(style_text):
                    return "original HTML retained active style rule"
                if style_text:
                    cids_in_view.update(_CID_REFERENCE.findall(style_text))

    attached_folded = {cid.casefold() for cid in attached_cids}
    missing = sorted(
        cid for cid in cids_in_view if cid.casefold() not in attached_folded
    )
    if missing:
        return (
            "original references inline images missing from the message: "
            + ", ".join(missing)
        )
    return None


def _attr_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _extract_cids_from_attr(value: object) -> set[str]:
    cids: set[str] = set()
    for text in _attr_strings(value):
        cids.update(_CID_REFERENCE.findall(text))
    return cids


def _is_active_attribute(name: str, value: object) -> bool:
    """An event handler, or a value that runs script when followed or styled.

    Script URLs are refused in every attribute, not a list of URL attributes,
    so SVG animation targets and unknown attributes cannot carry one. A link
    may not open a ``data:`` document; images may still use ``data:``.
    """
    if name.startswith("on"):
        return True
    for text in _attr_strings(value):
        if _SCRIPT_URL.match(text):
            return True
        if name in _LINK_ATTRIBUTES and _DATA_URL.match(text):
            return True
        if name == "style" and _ACTIVE_STYLE.search(text):
            return True
    return False


def _referenced_inline_parts(
    message: EmailMessage, referenced_cids: set[str]
) -> tuple[InlinePart, ...]:
    referenced = {cid.casefold() for cid in referenced_cids}
    if not referenced:
        return ()
    parts: list[InlinePart] = []
    seen: set[str] = set()
    for part in message.walk():
        if part.is_multipart():
            continue
        raw_cid = part.get("Content-ID")
        if not raw_cid:
            continue
        cid = str(raw_cid).strip().strip("<>").strip()
        key = cid.casefold()
        if key not in referenced or key in seen:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        seen.add(key)
        parts.append(
            InlinePart(
                content_id=cid,
                maintype=part.get_content_maintype(),
                subtype=part.get_content_subtype(),
                payload=payload,
                filename=part.get_filename(),
            )
        )
    return tuple(parts)
