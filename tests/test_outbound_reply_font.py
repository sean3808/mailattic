"""The reply's HTML part carries the sender's font from the SMTP settings file (#13)."""

from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

import pytest

from mailattic.cli import build_parser
from mailattic.mcp_writer import MailWriter
from mailattic.outbound import (
    ENV_VAR_SMTP_ENV_FILE,
    DraftReplyError,
    build_quoted_reply_bodies,
    reply_font_style,
)
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.storage import initialize_database
from tests.test_mcp_send import _seed_message
from tests.test_outbound_draft import PLACEHOLDER_SMTP_SETTINGS
from tests.test_outbound_quoted_reply import _make_original, _seed_db

FONT_SETTINGS = "MAIL_FONT_LATIN=Aptos\nMAIL_FONT_CJK=微軟正黑體\nMAIL_FONT_SIZE=12pt\n"
FONT_ENV = {
    "MAIL_FONT_LATIN": "Aptos",
    "MAIL_FONT_CJK": "微軟正黑體",
    "MAIL_FONT_SIZE": "12pt",
}


def _env_file(tmp_path: Path, extra: str = "") -> Path:
    env_file = tmp_path / "smtp.env"
    env_file.write_text(PLACEHOLDER_SMTP_SETTINGS + extra, encoding="utf-8")
    return env_file


def _draft_html(draft_path: Path) -> str:
    parsed = message_from_bytes(draft_path.read_bytes(), policy=policy.default)
    html_part = parsed.get_body(preferencelist=("html",))
    assert html_part is not None
    return html_part.get_content()


def _cli_draft(tmp_path: Path, env_file: Path) -> int:
    body_file = tmp_path / "body.txt"
    body_file.write_text("回覆內容 reply", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<quoted-reply-test@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(_seed_db(tmp_path)),
            "--env-file",
            str(env_file),
        ]
    )
    return _handle_draft_reply(args)


def test_font_style_orders_latin_then_fallback_then_cjk() -> None:
    style = reply_font_style(FONT_ENV)

    order = ["'Aptos'", "'Calibri'", "'微軟正黑體'", "'PingFang TC'", "sans-serif"]
    positions = [style.index(name) for name in order]
    assert positions == sorted(positions)
    assert "mso-fareast-font-family: '微軟正黑體'" in style
    assert "font-size: 12pt" in style


def test_no_font_settings_means_no_style() -> None:
    assert reply_font_style({"SMTP_USER": "sean3808@mailattic.test"}) == ""


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAIL_FONT_LATIN", 'Aptos"><script>'),
        ("MAIL_FONT_CJK", "微軟正黑體; color: red"),
        ("MAIL_FONT_SIZE", "12"),
        ("MAIL_FONT_SIZE", "12pt; color: red"),
    ],
)
def test_malformed_font_setting_is_refused(key: str, value: str) -> None:
    with pytest.raises(DraftReplyError, match=key):
        reply_font_style({key: value})


def test_font_style_wraps_reply_and_quoted_block() -> None:
    original = _make_original(body_text="Original body text")
    style = reply_font_style(FONT_ENV)

    _, html_body = build_quoted_reply_bodies(original, "My reply", font_style=style)

    assert html_body.startswith(f'<div style="{style}">')
    assert html_body.rstrip().endswith("</div>")
    assert html_body.index("My reply") > len(style)
    assert "Original body text" in html_body


def test_empty_font_style_leaves_bodies_unchanged() -> None:
    original = _make_original()

    assert build_quoted_reply_bodies(
        original, "My reply", font_style=""
    ) == build_quoted_reply_bodies(original, "My reply")


def test_cli_draft_html_carries_configured_font(tmp_path: Path) -> None:
    assert _cli_draft(tmp_path, _env_file(tmp_path, FONT_SETTINGS)) == 0

    draft = next((tmp_path / "outbound" / "drafts").glob("*.eml"))
    assert reply_font_style(FONT_ENV) in _draft_html(draft)


def test_cli_draft_without_font_settings_has_no_font(tmp_path: Path) -> None:
    assert _cli_draft(tmp_path, _env_file(tmp_path)) == 0

    draft = next((tmp_path / "outbound" / "drafts").glob("*.eml"))
    assert "font-family" not in _draft_html(draft)


def test_cli_draft_refuses_malformed_font_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = _env_file(tmp_path, "MAIL_FONT_SIZE=large\n")

    assert _cli_draft(tmp_path, env_file) == 2
    assert "MAIL_FONT_SIZE" in capsys.readouterr().err
    assert not list((tmp_path / "outbound" / "drafts").glob("*.eml"))


def test_mcp_draft_html_carries_configured_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(_env_file(tmp_path, FONT_SETTINGS)))

    draft = MailWriter.for_database(db_path).draft_reply(
        message_id=msg_id, body_text="Draft body"
    )

    assert reply_font_style(FONT_ENV) in _draft_html(draft.draft_path)
