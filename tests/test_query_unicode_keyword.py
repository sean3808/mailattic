"""Tests for unquoted Unicode keywords in search (issue #241).

FTS keyword builder preserves bareword Unicode characters (such as
accented names like 'Bellễn' and accented letters like 'café') without splitting them
into ASCII fragments or dropping accented letters as separators.
"""

from __future__ import annotations

import unicodedata
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailattic.ingest import import_maildir_sources
from mailattic.models import MaildirSource
from mailattic.query import (
    _FTS_MATCH_IMPOSSIBLE,
    SearchFilters,
    _build_fts_match_expr,
    search_messages,
)
from mailattic.storage import initialize_database


def _import_emails(
    root: Path,
    messages: list[tuple[str, str, str]],  # (subject, body, message_id)
    *,
    db_name: str = "test",
    account_email: str = "buyer@example.test",
    to_header: str | None = None,
) -> Path:
    folder = root / "maildir" / db_name / "INBOX"
    (folder / "new").mkdir(parents=True, exist_ok=True)
    for subject, body, message_id in messages:
        msg = EmailMessage()
        msg["From"] = "supplier@example.test"
        msg["To"] = to_header or account_email
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{message_id}>"
        msg.set_content(body)
        (folder / "new" / f"{message_id}.eml").write_bytes(msg.as_bytes())

    db_path = root / f"{db_name}.db"
    initialize_database(db_path)
    source = MaildirSource(folder, account_email, "audit", "INBOX", folder.parent)
    import_maildir_sources(
        database_path=db_path,
        sources=[source],
        attachment_root=root / "cas",
    )
    return db_path


def _create_and_import_email(
    root: Path,
    *,
    subject: str,
    body: str,
    message_id: str,
    account_email: str = "buyer@example.test",
) -> Path:
    return _import_emails(
        root,
        [(subject, body, message_id)],
        db_name=message_id.replace("@", "_").replace(".", "_"),
        account_email=account_email,
    )


def test_unicode_keyword_table_seven_queries(tmp_path: Path) -> None:
    """Issue #241 table: all 7 queries must return 1 hit via maildir import."""
    db_path = _create_and_import_email(
        tmp_path,
        subject="Unicode quotation",
        body="Bellễn café 報價 quotation",
        message_id="unicode-table-001@example.test",
    )

    expected_expressions = {
        "Bellễn": '"Bellễn"',
        '"Bellễn"': '"Bellễn"',
        "café": '"café"',
        '"café"': '"café"',
        "報價": '"報 價"',
        "quotation": '"quotation"',
        "quotation AND 報價": '"quotation" AND "報 價"',
    }

    for word, expected_expr in expected_expressions.items():
        actual_expr = _build_fts_match_expr(word)
        assert actual_expr == expected_expr, f"Expression mismatch for {word!r}"

        results = search_messages(
            database_path=db_path,
            filters=SearchFilters(keyword=word),
            read_only=True,
        )
        assert len(results) == 1, (
            f"Expected 1 hit for keyword {word!r}, got {len(results)}"
        )
        assert results[0]["subject"] == "Unicode quotation"


def test_nfc_and_nfd_fts_tokenizer_behavior(tmp_path: Path) -> None:
    """Precomposed (NFC) vs decomposed (NFD) tokens in FTS tokenizer.

    SQLite unicode61 folds diacritics for Latin-1 supplement letters ('é'),
    so both NFC 'café' and NFD 'cafe\\u0301' match both NFC and NFD records.
    However, for Latin letters beyond Latin-1 ('ễ' vs 'e\\u0302\\u0303'),
    unicode61 preserves U+1EC5 in NFC while stripping combining marks in NFD.
    Therefore NFC 'Bellễn' matches NFC, and NFD 'Bellễn' matches NFD.
    """
    nfc_body = unicodedata.normalize("NFC", "Bellễn café precomposed")
    nfd_body = unicodedata.normalize("NFD", "Bellễn café decomposed")

    db_path = _import_emails(
        tmp_path,
        [
            ("NFC Message", nfc_body, "nfc"),
            ("NFD Message", nfd_body, "nfd"),
        ],
        db_name="nfc_nfd",
    )

    # 1. 'café': Latin-1 diacritic is folded by unicode61, so NFC and NFD match both
    nfc_cafe = unicodedata.normalize("NFC", "café")
    nfd_cafe = unicodedata.normalize("NFD", "café")
    hits_nfc_cafe = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword=nfc_cafe),
        read_only=True,
    )
    hits_nfd_cafe = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword=nfd_cafe),
        read_only=True,
    )
    assert len(hits_nfc_cafe) == 2
    assert len(hits_nfd_cafe) == 2

    # 2. 'Bellễn': NFC token is 'bellễn', NFD token is 'bellen'
    nfc_bellen = unicodedata.normalize("NFC", "Bellễn")
    nfd_bellen = unicodedata.normalize("NFD", "Bellễn")

    hits_nfc_bellen = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword=nfc_bellen),
        read_only=True,
    )
    assert len(hits_nfc_bellen) == 1
    assert hits_nfc_bellen[0]["subject"] == "NFC Message"

    hits_nfd_bellen = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword=nfd_bellen),
        read_only=True,
    )
    assert len(hits_nfd_bellen) == 1
    assert hits_nfd_bellen[0]["subject"] == "NFD Message"


def test_preserved_behaviors_cjk_ascii_boolean_parens_quotes(tmp_path: Path) -> None:
    """Ensure existing search features remain functional with Unicode keywords."""
    db_path = _create_and_import_email(
        tmp_path,
        subject="Preserved behaviors test",
        body="PAPYRA 報價 PEN-100 quotation Bellễn café",
        message_id="preserved-001@example.test",
    )

    # CJK unigrams
    assert _build_fts_match_expr("報價") == '"報 價"'
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="報價"),
        read_only=True,
    )
    assert len(hits) == 1

    # ASCII bareword
    assert _build_fts_match_expr("PAPYRA") == '"PAPYRA"'
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="PAPYRA"),
        read_only=True,
    )
    assert len(hits) == 1

    # Uppercase boolean operators AND / OR / NOT
    assert _build_fts_match_expr("Bellễn AND quotation") == '"Bellễn" AND "quotation"'
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Bellễn AND quotation"),
        read_only=True,
    )
    assert len(hits) == 1

    assert (
        _build_fts_match_expr("Bellễn OR nonexistingtoken")
        == '"Bellễn" OR "nonexistingtoken"'
    )
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Bellễn OR nonexistingtoken"),
        read_only=True,
    )
    assert len(hits) == 1

    assert _build_fts_match_expr("quotation NOT 報價") == '"quotation" NOT "報 價"'
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="quotation NOT 報價"),
        read_only=True,
    )
    assert len(hits) == 0

    # Lowercase boolean operators are treated as literal terms
    assert _build_fts_match_expr("and or not") == '"and" "or" "not"'

    # Grouping parentheses
    assert (
        _build_fts_match_expr("Bellễn AND (nonexistingtoken OR 報價)")
        == '"Bellễn" AND ("nonexistingtoken" OR "報 價")'
    )
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword="Bellễn AND (nonexistingtoken OR 報價)"),
        read_only=True,
    )
    assert len(hits) == 1

    # Quoted phrase
    assert _build_fts_match_expr('"Bellễn café"') == '"Bellễn café"'
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword='"Bellễn café"'),
        read_only=True,
    )
    assert len(hits) == 1

    # Hyphenated / punctuation separator in barewords
    assert _build_fts_match_expr("PEN-100") == '"PEN" "100"'
    assert _build_fts_match_expr(":") == _FTS_MATCH_IMPOSSIBLE
    hits = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword=":"),
        read_only=True,
    )
    assert len(hits) == 0

    # Invalid syntax produces friendly diagnostic (ValueError)
    assert _build_fts_match_expr("Bellễn (") == '"Bellễn" ('
    with pytest.raises(ValueError, match="Invalid FTS keyword query: Bellễn \\("):
        search_messages(
            database_path=db_path,
            filters=SearchFilters(keyword="Bellễn ("),
            read_only=True,
        )


# ---------------------------------------------------------------------------
# Display side: snippet + match_field (issue #247)
# ---------------------------------------------------------------------------
# #241 also changed how a hit is LOCATED in the stored text (_alphanumeric_runs,
# _is_word_char). Hit counts cannot see that: a row can be found and still
# report the wrong field or centre its snippet somewhere else.


def _search_one(db_path: Path, keyword: str) -> dict[str, object]:
    results = search_messages(
        database_path=db_path,
        filters=SearchFilters(keyword=keyword),
        read_only=True,
    )
    assert len(results) == 1, f"Expected 1 hit for {keyword!r}, got {len(results)}"
    return results[0]


def test_nfc_accented_keyword_reports_body_and_shows_the_word(tmp_path: Path) -> None:
    word = unicodedata.normalize("NFC", "Bellễn")
    db_path = _import_emails(
        tmp_path, [("Contact", f"Please ask {word} about the order", "nfc-display")]
    )

    hit = _search_one(db_path, word)

    assert hit["match_field"] == "body"
    assert word in str(hit["snippet"])


def test_nfd_keyword_snippet_centres_on_the_word_not_a_split_lookalike(
    tmp_path: Path,
) -> None:
    """Combining marks belong to the run, they are not separators.

    Treating them as separators turns NFD ``Bellễn`` into "``Belle``, any
    gap, ``n``", which also matches the unrelated ``Belle n`` at the head of
    this body and centres the snippet there, out of reach of the real word.
    """
    word = unicodedata.normalize("NFD", "Bellễn")
    body = f"Belle n {'filler ' * 40}ask {word} about the order"
    db_path = _import_emails(tmp_path, [("Contact", body, "nfd-display")])

    hit = _search_one(db_path, word)

    assert hit["match_field"] == "body"
    assert word in str(hit["snippet"])
    assert "Belle n" not in str(hit["snippet"])


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_keyword_inside_an_accented_token_is_not_a_body_hit(
    tmp_path: Path, form: str
) -> None:
    """The row matches on its recipient; the body only contains ``rt`` as the
    tail of ``cárt``, a longer token the index never matched it against.

    What precedes ``rt`` is the letter ``á`` (NFC) or a combining mark (NFD);
    either way it fuses into the token, so this is not a whole-token hit.
    """
    body_word = unicodedata.normalize(form, "cárt")
    db_path = _import_emails(
        tmp_path,
        [("Whole token", f"Loaded the {body_word} yesterday", "fragment")],
        to_header="rt@example.test",
    )

    hit = _search_one(db_path, "rt")

    assert hit["match_field"] is None


def test_accented_keyword_only_in_recipients_is_not_a_body_hit(tmp_path: Path) -> None:
    word = unicodedata.normalize("NFC", "Bellễn")
    db_path = _import_emails(
        tmp_path,
        [("Order status", "Shipment leaves on Friday", "recipient-only")],
        to_header=f"{word} <buyer@example.test>",
    )

    hit = _search_one(db_path, word)

    assert hit["match_field"] is None
