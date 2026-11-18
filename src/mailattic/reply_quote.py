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
- Downlevel-revealed conditional sections (``<![if ...]>...<![endif]>``) and
  hybrid conditional comments (``<!--[if ...]><!-->...<!--<![endif]-->`` and the
  ``<!-- -->`` variant), whose body is already live markup and whose markers
  stay byte-identical.
- Ordinary HTML comments (not starting with ``[if``) are outside the render
  view: they are neither sanitised nor scanned for ``cid:``.

Conditional comments whose inner HTML carries no active content are kept
byte-for-byte so Word VML details are not rewritten. ``cid:`` references in
``<a href>`` / ``<area href>`` are not required to resolve (if a matching part
exists it is still attached, otherwise the link ships as written); every other
``cid:`` (images, VML, ``background``, ``style``, ``<style>``) must resolve.
CSS (``style`` attributes and ``<style>`` text) is checked as a renderer reads
it: comments removed and escapes decoded, so ``@\\69mport`` counts as
``@import``. The ``<body>`` becomes a ``<div>`` that keeps its presentation
(``style``, ``class``, ``lang``, ``dir``, ``bgcolor`` / ``text`` /
``background`` as CSS, ``link`` / ``vlink`` / ``alink`` as scoped rules).
After sanitising and attaching ``cid:`` parts, :func:`verify_quoted_html`
re-parses the final HTML string written into the draft, rebuilds the render
view, and fails closed to stored text if any active content, unattached
required ``cid:``, or unclassifiable structure (unclosed/nested conditional
comments, unknown ``<!`` declarations) remains.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Literal

from bs4 import BeautifulSoup, CData, Comment, Declaration, ProcessingInstruction, Tag

from .parser import _MAIL_POLICY, _detect_and_decode, _without_lone_surrogates

QuoteCategory = Literal["no_html", "temporary", "settings", "permanent", "unquotable"]

QUOTE_NOTICE_BY_CATEGORY: dict[QuoteCategory, str] = {
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


def quote_notice_for_category(category: QuoteCategory) -> str:
    """Return the user-facing fallback notice sentence for a reason category."""
    return QUOTE_NOTICE_BY_CATEGORY[category]


class OriginalUnavailable(Exception):
    """The original could not be fetched; the message is the user-facing reason."""

    def __init__(self, reason: str, *, category: QuoteCategory = "temporary") -> None:
        super().__init__(reason)
        self.category: QuoteCategory = category


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
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_CSS_HEX_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\r\n\f]?")
_CSS_LINE_CONTINUATION = re.compile(r"\\(?:\r\n|[\n\r\f])")
_CSS_CHAR_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
# Body attributes that are presentational: carried onto the quote's wrapper.
_BODY_COLOR = re.compile(r"^#?[0-9A-Za-z]{1,32}$")
_BODY_COLOR_STYLE = {"bgcolor": "background-color", "text": "color"}
_BODY_LINK_RULE = {"link": "a:link", "vlink": "a:visited", "alink": "a:active"}
_BODY_KEPT_ATTRIBUTES = ("lang", "dir")
_QUOTE_BODY_CLASS = "mailattic-quoted-body"
# URL characters left as-is inside url("..."); quotes, parentheses, backslash
# and whitespace are percent-encoded so the value cannot end the CSS string.
_URL_SAFE = ":/?#[]@!$&*+,;=%~-._"


def _css_normalised(text: str) -> str:
    """CSS text as a CSS parser reads it: comments removed, escapes decoded.

    ``@\\69mport`` and ``e\\78pression(`` are ``@import`` and ``expression(`` to
    a renderer, so active-content and ``cid:`` checks run on this form.
    """

    def hex_char(match: re.Match[str]) -> str:
        code = int(match.group(1), 16)
        return chr(code) if 0 < code <= 0x10FFFF else "\ufffd"

    text = _CSS_COMMENT.sub("", text)
    text = _CSS_LINE_CONTINUATION.sub("", text)
    text = _CSS_HEX_ESCAPE.sub(hex_char, text)
    return _CSS_CHAR_ESCAPE.sub(lambda match: match.group(1), text)


def _css_is_active(text: str) -> bool:
    return bool(_ACTIVE_STYLE.search(_css_normalised(text)))


def _css_cids(text: str) -> set[str]:
    return set(_CID_REFERENCE.findall(_css_normalised(text)))


_HIDDEN_COND_RE = re.compile(
    r"^(\[if\b[^\]]*\]>)(.*)(<!\[endif\])$", re.DOTALL | re.IGNORECASE
)
_REVEALED_OPEN_RE = re.compile(r"^\s*\[if\b[^\]]*\]\s*$", re.IGNORECASE)
_REVEALED_CLOSE_RE = re.compile(r"^\s*\[endif\]\s*$", re.IGNORECASE)
_HYBRID_OPEN_RE = re.compile(r"^\s*\[if\b[^\]]*\]>\s*<!(?:--\s*)?$", re.IGNORECASE)
_HYBRID_CLOSE_RE = re.compile(r"^\s*<!\[endif\]\s*$", re.IGNORECASE)
_COND_MARKER_RE = re.compile(r"^\s*\[if\b|<!\[endif\]|\[endif\]", re.IGNORECASE)
_NESTED_COND_RE = re.compile(r"\[if\b|<!\[endif\]|\[endif\]", re.IGNORECASE)
_XML_NAMESPACE_PI = re.compile(r"xml:namespace\s[^<>]*\Z", re.IGNORECASE)
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
        or _HYBRID_OPEN_RE.match(text)
        or _HYBRID_CLOSE_RE.match(text)
    )


def _reject_unsupported_node(node: object) -> None:
    # Outlook's quoted history carries `<?xml:namespace prefix="o" ns="..." />`:
    # an inert namespace declaration, kept as written. Any other PI is unknown.
    if isinstance(node, ProcessingInstruction) and _XML_NAMESPACE_PI.match(node):
        return
    if isinstance(node, (CData, Declaration, ProcessingInstruction)):
        raise _UnclassifiableHtml(f"unsupported {type(node).__name__.lower()}")


def _check_subdoc_nodes(soup: BeautifulSoup) -> None:
    for node in soup.descendants:
        _reject_unsupported_node(node)
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
        _reject_unsupported_node(node)
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
                    suffix=suffix,
                    soup=inner_soup,
                )
            )
            continue
        if _REVEALED_OPEN_RE.match(text) or _HYBRID_OPEN_RE.match(text):
            revealed_depth += 1
            if revealed_depth > 1:
                raise _UnclassifiableHtml("nested conditional comment")
            continue
        if _REVEALED_CLOSE_RE.match(text) or _HYBRID_CLOSE_RE.match(text):
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
    fragment, required_cids, optional_cids = _sanitised_fragment(source_html)
    inline_parts = _referenced_inline_parts(message, required_cids | optional_cids)
    attached_images = {
        part.content_id.casefold() for part in inline_parts if part.maintype == "image"
    }
    rejection = verify_quoted_html(fragment, attached_images)
    unresolved = tuple(
        sorted(cid for cid in required_cids if cid.casefold() not in attached_images)
    )
    return QuotableOriginal(
        html_fragment=fragment,
        inline_parts=inline_parts,
        unresolved_cids=unresolved,
        rejection_reason=rejection,
    )


def _is_link_href(tag_name: str, attr_name: str) -> bool:
    return attr_name == "href" and tag_name in ("a", "area")


def _inspect_soup(
    soup: BeautifulSoup, *, remove_active: bool
) -> tuple[bool, set[str], set[str], str | None]:
    """Scan ``soup`` for active content and ``cid:`` references.

    Returns ``(changed, required_cids, optional_cids, violation)``. ``cid:``
    values on ``<a href>`` / ``<area href>`` are optional (attached when present,
    never required to resolve); every other ``cid:`` is required. When
    ``remove_active`` is true, active tags/attributes/styles are stripped in
    place; otherwise the first active-content violation is returned.
    """
    changed = False
    if remove_active:
        for tag in soup.find_all(_ACTIVE_TAGS):
            tag.decompose()
            changed = True
    required_cids: set[str] = set()
    optional_cids: set[str] = set()

    def violation(reason: str) -> tuple[bool, set[str], set[str], str]:
        return changed, required_cids, optional_cids, reason

    for tag in soup.find_all(True):
        tag_name = tag.name.casefold()
        if not remove_active and tag_name in _ACTIVE_TAGS:
            return violation(f"original HTML retained active tag <{tag.name}>")
        for name in list(tag.attrs):
            attr_name = name.casefold()
            value = tag.attrs[name]
            if _is_active_attribute(attr_name, value):
                if not remove_active:
                    return violation(f"original HTML retained active attribute {name}")
                del tag.attrs[name]
                changed = True
            else:
                cids = _extract_cids_from_attr(value)
                if attr_name == "style":
                    for text in _attr_strings(value):
                        cids |= _css_cids(text)
                if _is_link_href(tag_name, attr_name):
                    optional_cids.update(cids)
                else:
                    required_cids.update(cids)
        if tag_name == "style":
            style_text = tag.get_text()
            if _css_is_active(style_text):
                if not remove_active:
                    return violation("original HTML retained active style rule")
                tag.decompose()
                changed = True
            elif style_text:
                required_cids.update(_css_cids(style_text))
    return changed, required_cids, optional_cids, None


def _sanitise_soup(soup: BeautifulSoup) -> tuple[bool, set[str], set[str]]:
    """Remove active content from ``soup`` and collect ``cid:`` references."""
    changed, required_cids, optional_cids, _ = _inspect_soup(soup, remove_active=True)
    return changed, required_cids, optional_cids


def _sanitised_fragment(source_html: str) -> tuple[str, set[str], set[str]]:
    """The quotable HTML and the required/optional ``cid:`` ids it references.

    Uses :func:`_build_render_view` so sanitising and ``cid:`` collection share
    the exact same view of live markup and conditional comments.
    """
    try:
        view = _build_render_view(source_html)
    except _UnclassifiableHtml:
        return source_html, set(), set()

    _, required_cids, optional_cids = _sanitise_soup(view.root)
    for subdoc in view.conditional_subdocs:
        sub_changed, sub_required, sub_optional = _sanitise_soup(subdoc.soup)
        required_cids.update(sub_required)
        optional_cids.update(sub_optional)
        if sub_changed:
            subdoc.comment.replace_with(
                Comment(f"{subdoc.prefix}{subdoc.soup}{subdoc.suffix}")
            )

    body = view.root.body
    if body is None:
        return str(view.root), required_cids, optional_cids
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
    link_rules, wrapper_attrs = _body_wrapper(body)
    return (
        f"{styles}{link_rules}<div{wrapper_attrs}>{body.decode_contents()}</div>",
        required_cids,
        optional_cids,
    )


def _body_wrapper(body: Tag) -> tuple[str, str]:
    """The ``<body>``'s presentation, carried onto the quote's ``<div>`` (#136).

    ``style``, ``class``, ``lang`` and ``dir`` are copied; ``bgcolor`` / ``text``
    / ``background`` become the equivalent CSS; ``link`` / ``vlink`` /
    ``alink`` become link-colour rules scoped to the quote. Values that are not
    plain colours are dropped rather than written into CSS. Other body
    attributes (margins) are not carried.
    """

    def text_of(name: str) -> str:
        value = body.get(name)
        return value.strip() if isinstance(value, str) else ""

    def color_of(name: str) -> str:
        value = text_of(name)
        return value if _BODY_COLOR.match(value) else ""

    declarations = [
        f"{prop}:{color}"
        for name, prop in _BODY_COLOR_STYLE.items()
        if (color := color_of(name))
    ]
    if background := text_of("background"):
        url = urllib.parse.quote(background, safe=_URL_SAFE)
        declarations.append(f'background-image:url("{url}")')
    if body_style := text_of("style"):
        declarations.append(body_style)
    rules = [
        f".{_QUOTE_BODY_CLASS} {selector}{{color:{color}}}"
        for name, selector in _BODY_LINK_RULE.items()
        if (color := color_of(name))
    ]
    classes = _attr_strings(body.get("class")) + ([_QUOTE_BODY_CLASS] if rules else [])
    attrs: list[str] = []
    if classes:
        attrs.append(f' class="{html.escape(" ".join(classes), quote=True)}"')
    for name in _BODY_KEPT_ATTRIBUTES:
        value = body.get(name)
        if isinstance(value, str):
            attrs.append(f' {name}="{html.escape(value, quote=True)}"')
    if declarations:
        style = ";".join(declarations)
        attrs.append(f' style="{html.escape(style, quote=True)}"')
    link_rules = f"<style>{' '.join(rules)}</style>" if rules else ""
    return link_rules, "".join(attrs)


def verify_quoted_html(html_text: str, attached_cids: Iterable[str]) -> str | None:
    """Fail-closed post-condition gate on the final HTML written into a draft.

    Re-parses ``html_text``, builds the render view, and returns ``None`` only
    when (a) no active content appears anywhere in the render view and (b)
    every required ``cid:`` in the render view maps to an attached image part.
    Otherwise returns a user-facing rejection reason.
    """
    try:
        view = _build_render_view(html_text)
    except _UnclassifiableHtml as exc:
        return f"original HTML has unsupported structure: {exc}"

    cids_in_view: set[str] = set()
    for doc in view.documents():
        _, doc_required_cids, _, violation = _inspect_soup(doc, remove_active=False)
        if violation is not None:
            return violation
        cids_in_view.update(doc_required_cids)

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
        if name == "style" and _css_is_active(text):
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
