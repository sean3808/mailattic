"""Quote an original message's own HTML in a reply (#136).

A reply used to quote the stored ``body_text``: the sender's plain-text
alternative or the flattened HTML, which reads as a text dump in Outlook and
makes the customer's own earlier message look edited. The fix quotes the
original as it arrived (the RFC822 bytes fetched on demand; nothing is stored,
D-20261105-1), with its inline ``cid:`` images.

Sanitising removes active content only, so the quote shows what the sender
wrote: scripts, frames, embedded objects, forms and their controls,
``<meta>`` (refresh), ``<base>``, ``<link>``, ``on*`` event attributes,
script URLs in any attribute, links to ``data:`` documents, style
``expression()``, and Outlook conditional comments that carry any of these.
Styles, remote images, links and Outlook conditional comments stay.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser

from bs4 import BeautifulSoup, Comment

from .parser import _MAIL_POLICY, _detect_and_decode, _without_lone_surrogates


class OriginalUnavailable(Exception):
    """The original could not be fetched; the message is the user-facing reason."""


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
    r"expression\s*\(|(?:javascript|vbscript)\s*:", re.IGNORECASE
)
_CID_REFERENCE = re.compile(r"cid:([^\"'\s)>]+)", re.IGNORECASE)
_ACTIVE_IN_COMMENT = re.compile(
    r"<\s*(?:" + "|".join(_ACTIVE_TAGS) + r")\b"
    r"|\son[a-z]+\s*="
    r"|(?:javascript|vbscript)\s*:",
    re.IGNORECASE,
)


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
    found = {part.content_id.casefold() for part in inline_parts}
    return QuotableOriginal(
        html_fragment=fragment,
        inline_parts=inline_parts,
        unresolved_cids=tuple(
            sorted(cid for cid in referenced if cid.casefold() not in found)
        ),
    )


def _sanitised_fragment(source_html: str) -> tuple[str, set[str]]:
    """The quotable HTML and the ``cid:`` ids it references.

    The ids are read from parsed attribute values and style text, not from the
    serialised HTML, which escapes characters such as ``&`` in them.
    """
    soup = BeautifulSoup(source_html, "html.parser")
    for tag in soup.find_all(_ACTIVE_TAGS):
        tag.decompose()
    referenced: set[str] = set()
    # Outlook conditional comments (`<!--[if mso]>...<![endif]-->`) hold markup
    # the parser leaves as opaque text, which Outlook still renders. One that
    # carries active content goes whole; the rest stay, and their cid images
    # count as referenced.
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        if _ACTIVE_IN_COMMENT.search(comment):
            comment.extract()
        else:
            referenced.update(_CID_REFERENCE.findall(comment))
    for tag in soup.find_all(True):
        for name in list(tag.attrs):
            value = tag.attrs[name]
            if _is_active_attribute(name.casefold(), value):
                del tag.attrs[name]
            elif isinstance(value, str):
                referenced.update(_CID_REFERENCE.findall(value))
        if tag.name == "style" and tag.string:
            referenced.update(_CID_REFERENCE.findall(tag.string))

    body = soup.body
    if body is None:
        return str(soup), referenced
    # Styles and Outlook conditional comments usually live in <head>; they
    # belong to the quote, so they move with the body content, in order.
    styles = "".join(
        node.output_ready() if isinstance(node, Comment) else str(node)
        for node in soup.descendants
        if (isinstance(node, Comment) or getattr(node, "name", None) == "style")
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


def _is_active_attribute(name: str, value: object) -> bool:
    """An event handler, or a value that runs script when followed or styled.

    Script URLs are refused in every attribute, not a list of URL attributes,
    so SVG animation targets and unknown attributes cannot carry one. A link
    may not open a ``data:`` document; images may still use ``data:``.
    """
    if name.startswith("on"):
        return True
    if not isinstance(value, str):
        return False
    if _SCRIPT_URL.match(value):
        return True
    if name in _LINK_ATTRIBUTES and _DATA_URL.match(value):
        return True
    return name == "style" and bool(_ACTIVE_STYLE.search(value))


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
